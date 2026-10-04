"""Stripe subscriptions: checkout, customer portal, webhook-driven state.

The single source of truth for a rider's access is `user.subscription_status`,
kept in sync by Stripe webhooks. The app never trusts the client about money.
Everything here is dormant until STRIPE_SECRET_KEY exists, and the paywall
only bites when REQUIRE_SUBSCRIPTION flips true.
"""

import json
import logging
from datetime import datetime, timezone

import stripe
from sqlalchemy.orm import Session

from app.config import settings
from app.models.user import User

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(settings.stripe_secret_key and settings.stripe_price_id)


def _client() -> None:
    stripe.api_key = settings.stripe_secret_key


def get_or_create_customer(db: Session, user: User) -> str:
    if user.stripe_customer_id:
        return user.stripe_customer_id
    _client()
    # Idempotent per rider: a double click on Join used to race two requests
    # into two Stripe customers, and the portal only ever shows one of them.
    customer = stripe.Customer.create(
        email=user.email,
        name=user.full_name or None,
        metadata={"forma_user_id": str(user.id)},
        idempotency_key=f"forma-customer-{user.id}",
    )
    user.stripe_customer_id = customer.id
    db.commit()
    return customer.id


def create_checkout_session(db: Session, user: User) -> str:
    """A Stripe-hosted checkout for the founding subscription. Returns URL."""
    _client()
    customer_id = get_or_create_customer(db, user)
    frontend = settings.frontend_url or "http://localhost:3000"
    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": settings.stripe_price_id, "quantity": 1}],
        success_url=f"{frontend}/dashboard/settings?billing=success",
        cancel_url=f"{frontend}/dashboard/settings?billing=cancelled",
        allow_promotion_codes=True,
        subscription_data={"metadata": {"forma_user_id": str(user.id)}},
    )
    return session.url


def create_portal_session(db: Session, user: User) -> str:
    """Stripe's hosted portal: card changes, invoices, cancellation."""
    _client()
    customer_id = get_or_create_customer(db, user)
    frontend = settings.frontend_url or "http://localhost:3000"
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=f"{frontend}/dashboard/settings",
    )
    return session.url


def handle_webhook(payload: bytes, signature: str) -> None:
    """Verify and apply a Stripe event. Raises ValueError on bad signature."""
    _client()
    try:
        # Verification only. The library's event objects stopped behaving like
        # dicts (no .get) in recent versions, which made every handler below
        # raise; caught in a signed-event test on 1 Oct 2026 before launch.
        stripe.Webhook.construct_event(payload, signature, settings.stripe_webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        raise ValueError(f"Invalid webhook: {e}")

    # The signature covers these exact bytes, so reading them as plain JSON is
    # as trustworthy as the library's object, and it is ordinary dicts all the way down.
    event = json.loads(payload)
    kind = event["type"]
    obj = event["data"]["object"]

    if kind in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        _apply_subscription(obj)
    elif kind == "checkout.session.completed":
        # The subscription events carry the real state; this is just a log
        # marker for the funnel.
        logger.info("Checkout completed for customer %s", obj.get("customer"))
    else:
        logger.debug("Unhandled Stripe event: %s", kind)


def _period_end_ts(sub) -> int | None:
    """When the current paid period ends.

    Stripe API versions from 2025-03-31 (basil) moved current_period_end off
    the subscription and onto each subscription item, so read the item first
    and fall back to the old top-level field for older payloads.
    """
    items = (sub.get("items") or {}).get("data") or []
    ends = [i.get("current_period_end") for i in items if i.get("current_period_end")]
    if ends:
        return max(ends)
    return sub.get("current_period_end")


def _current_state(sub: dict) -> dict:
    """The subscription as Stripe holds it now, not as the event saw it.

    Stripe does not promise to deliver events in order. A late
    subscription.updated (active) landing after subscription.deleted would
    otherwise hand a cancelled rider their access back, so ask for the live
    object and fall back to the event's copy only if Stripe can't be reached.
    """
    if not sub.get("id"):
        return sub
    try:
        _client()
        return stripe.Subscription.retrieve(sub["id"]).to_dict()
    except Exception:
        logger.warning("Could not refresh subscription %s; using the event copy", sub.get("id"))
        return sub


def cancel_all_subscriptions(user: User) -> int:
    """End every live subscription for this rider now. Used when an account
    is deleted: an erased account must never go on charging a card."""
    if not (is_configured() and user.stripe_customer_id):
        return 0
    _client()
    ended = 0
    for sub in stripe.Subscription.list(
        customer=user.stripe_customer_id, status="all", limit=20
    ).auto_paging_iter():
        if sub.status in ("active", "trialing", "past_due", "unpaid", "incomplete"):
            stripe.Subscription.cancel(sub.id)
            ended += 1
    return ended


def _apply_subscription(sub: dict) -> None:
    from app.database import SessionLocal

    sub = _current_state(sub)
    customer_id = sub.get("customer")
    status = sub.get("status") or "none"
    if sub.get("status") == "canceled" or sub.get("ended_at"):
        status = "canceled"

    period_end = None
    ts = _period_end_ts(sub)
    if ts:
        period_end = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)

    db = SessionLocal()
    try:
        user = (
            db.query(User).filter(User.stripe_customer_id == customer_id).first()
        )
        if user is None:
            # Fall back to the metadata we stamp on every subscription.
            forma_id = (sub.get("metadata") or {}).get("forma_user_id")
            if forma_id:
                user = db.query(User).filter(User.id == forma_id).first()
        if user is None:
            logger.warning("Stripe webhook for unknown customer %s", customer_id)
            return
        user.subscription_status = status
        user.subscription_period_end = period_end
        if not user.stripe_customer_id:
            user.stripe_customer_id = customer_id
        db.commit()
        logger.info(
            "Subscription %s for user %s (period end %s)", status, user.id, period_end
        )
    finally:
        db.close()


def has_access(user: User) -> bool:
    """Can this rider use the paid product right now?

    Founder/admin accounts always pass. With the launch switch off, everyone
    passes. past_due keeps access (Stripe retries cards for days; a flaky
    card must not kill a training week), canceled does not.
    """
    if user.email.lower() in {e.lower() for e in settings.admin_emails}:
        return True
    if not settings.require_subscription:
        return True
    return user.subscription_status in ("active", "trialing", "past_due")
