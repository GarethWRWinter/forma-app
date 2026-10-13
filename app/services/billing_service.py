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


class AccountOnHold(Exception):
    """No new membership for an account held as possibly under 18: it is
    waiting to be closed and refunded, or lifted, by review."""


def create_checkout_session(db: Session, user: User) -> str:
    """A Stripe-hosted checkout for the founding subscription. Returns URL.
    Raises AccountOnHold for an account held as possibly under 18."""
    from app.services import safety_service

    if safety_service.minor_hold(db, user) is not None:
        raise AccountOnHold(user.id)
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
    """Stripe's hosted portal: card changes, invoices, cancellation.
    Raises AccountOnHold for an account held as possibly under 18: the
    portal would let it turn back on the renewal the hold stopped."""
    from app.services import safety_service

    if safety_service.minor_hold(db, user) is not None:
        raise AccountOnHold(user.id)
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


# Statuses that can still take a payment at the next renewal.
_RENEWING = ("active", "trialing", "past_due", "unpaid")


def _cancel_at_ts(sub) -> int | None:
    """Stripe's cancel_at as a timestamp, or None when there is none."""
    cancel_at = sub.get("cancel_at")
    if not cancel_at:
        return None
    try:
        return int(cancel_at)
    except (TypeError, ValueError):
        # Not a timestamp (the request-only words such as "max_period_end"):
        # the period end is the best reading of it.
        return _period_end_ts(sub)


def _cancel_pending(sub) -> bool:
    """The subscription ends by the close of the period already paid for and
    takes no further payment, in either of the two shapes Stripe uses.

    Older payloads, and Forma's own stop_renewal_for_review, set
    cancel_at_period_end. From the dahlia API versions (2026) a rider who
    cancels in the customer portal gets cancel_at, a timestamp equal to the
    period end, while cancel_at_period_end stays false and the status stays
    active until that date (found live on 13 Oct 2026).

    A cancel_at after the period end is not this: Stripe renews at the
    period end and charges for the part of the next period up to cancel_at
    (a custom cancel date set in the dashboard or the API). That one still
    renews, once. With no period end to compare against, cancel_at is taken
    at its word."""
    if sub.get("cancel_at_period_end"):
        return True
    cancel_at = _cancel_at_ts(sub)
    if cancel_at is None:
        return False
    period_end = _period_end_ts(sub)
    return period_end is None or cancel_at <= period_end


def _ends_at_ts(sub) -> int | None:
    """When a subscription with a pending cancellation ends: cancel_at when
    Stripe set one, else the period end for cancel_at_period_end, whichever
    comes first if it carries both. None while it renews, including a
    cancel_at in a later period (see _later_cancel_at_ts)."""
    if not _cancel_pending(sub):
        return None
    period_end = _period_end_ts(sub)
    ends = [
        t
        for t in (_cancel_at_ts(sub), period_end if sub.get("cancel_at_period_end") else None)
        if t
    ]
    return min(ends) if ends else period_end


def _later_cancel_at_ts(sub) -> int | None:
    """A cancel_at after the current period ends: Stripe renews once more,
    with a charge for the days up to it, then ends the subscription on that
    date. None otherwise."""
    if _cancel_pending(sub):
        return None
    return _cancel_at_ts(sub)


def _renews(sub) -> bool:
    """Will Stripe try to take another payment when this period ends?"""
    return sub.get("status") in _RENEWING and not _cancel_pending(sub)


def _renewal_back_on(sub) -> dict:
    """The Subscription.modify arguments that take a pending cancellation off,
    for whichever shape it is in. One shape per call, since Stripe can refuse
    both together: cancel_at_period_end false should also clear a cancel_at
    Stripe mirrored from it (resume_renewal_after_review checks Stripe's
    answer in case it didn't), and an empty cancel_at is how Stripe clears a
    cancellation date (stripe-python's SubscriptionModifyParams.cancel_at
    takes ""). Empty when it renews."""
    if sub.get("cancel_at_period_end"):
        return {"cancel_at_period_end": False}
    if sub.get("cancel_at"):
        return {"cancel_at": ""}
    return {}


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


# Stamped on a subscription whose renewal was stopped because the account was
# held as possibly under 18, so the review can tell it apart from a rider's own
# cancellation and undo only what Forma did.
MINOR_REVIEW_FLAG = "forma_renewal_stopped"
MINOR_REVIEW_VALUE = "under_18_review"
# A cancel date the subscription already had in a later period, which the
# renewal stop brought forward to the period end. The review puts it back for
# an adult, so the rider's own end date survives the hold.
MINOR_REVIEW_PRIOR_CANCEL_AT = "forma_prior_cancel_at"


def _as_dict(obj) -> dict:
    """A Stripe object as plain dicts all the way down (recent library
    versions dropped dict behaviour from their objects)."""
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return dict(obj)


def _subscriptions(customer_id: str) -> list[dict]:
    return [
        _as_dict(sub)
        for sub in stripe.Subscription.list(
            customer=customer_id, status="all", limit=20
        ).auto_paging_iter()
    ]


def stop_renewal_for_review(user) -> int:
    """An account was just held as possibly under 18: set every renewing
    subscription to end at the close of the period already paid for, so no
    further payment is taken before Gareth reviews it. The review tool
    (scripts/review_safety_event.py) then cancels outright and refunds, or
    undoes this for an adult read as a minor (resume_renewal_after_review).

    Best effort: never raises, logs what it did. Returns how many were set.
    Takes anything with .id and .stripe_customer_id, so a caller on another
    thread can pass a snapshot rather than a live database row.

    A subscription the rider already cancelled, in either shape, is left
    alone: it won't renew anyway, and flagging it would let the review turn
    renewal back on for a rider who asked for it off. One with a cancel date
    in a later period would still take one more payment, so that date is
    brought forward to the period end and kept on the subscription for the
    review to put back."""
    customer_id = getattr(user, "stripe_customer_id", None)
    user_id = getattr(user, "id", None)
    if not (is_configured() and customer_id):
        return 0
    stopped = 0
    try:
        _client()
        for sub in _subscriptions(customer_id):
            if not _renews(sub):
                continue
            metadata = {MINOR_REVIEW_FLAG: MINOR_REVIEW_VALUE}
            later = _later_cancel_at_ts(sub)
            if later is None:
                stop = {"cancel_at_period_end": True}
            else:
                # Moved in the field it is already set in, so Stripe is never
                # asked to hold both shapes at once.
                stop = {"cancel_at": _period_end_ts(sub)}
                metadata[MINOR_REVIEW_PRIOR_CANCEL_AT] = str(later)
            stripe.Subscription.modify(sub["id"], **stop, metadata=metadata)
            stopped += 1
    except Exception:
        logger.exception(
            "Stopping renewal for an under-18 review failed (user=%s, customer=%s); "
            "the review tool cancels it outright",
            user_id, customer_id,
        )
        return stopped
    logger.warning(
        "Under-18 hold: renewal stopped on %d subscription(s) for user %s pending review",
        stopped, user_id,
    )
    return stopped


def resume_renewal_after_review(user) -> int:
    """Undo stop_renewal_for_review after the review found an adult: renewal
    comes back on, but only for subscriptions Forma stopped (never one the
    rider cancelled themselves). Raises if Stripe can't be reached, so the
    review tool can say so. Returns how many were resumed.

    Forma stops renewal with cancel_at_period_end, but Stripe may hold it as
    cancel_at under a newer API version, so the cancellation is cleared in
    whichever shape the subscription carries, and Stripe's answer is checked
    for the other shape too. A later cancel date the stop brought forward
    (MINOR_REVIEW_PRIOR_CANCEL_AT) goes back instead of being cleared: the
    subscription renews once more and ends on the rider's own date."""
    customer_id = getattr(user, "stripe_customer_id", None)
    if not (is_configured() and customer_id):
        return 0
    _client()
    resumed = 0
    for sub in _subscriptions(customer_id):
        meta = sub.get("metadata") or {}
        flagged = meta.get(MINOR_REVIEW_FLAG) == MINOR_REVIEW_VALUE
        if not flagged or sub.get("status") not in _RENEWING:
            continue
        # An empty value deletes the key in Stripe.
        clear = {MINOR_REVIEW_FLAG: ""}
        if MINOR_REVIEW_PRIOR_CANCEL_AT in meta:
            clear[MINOR_REVIEW_PRIOR_CANCEL_AT] = ""
        prior = _prior_cancel_at(meta)
        if prior is not None and not sub.get("cancel_at_period_end"):
            # The usual case: the stop moved cancel_at, so move it back.
            first = {"cancel_at": prior}
        else:
            first = _renewal_back_on(sub)
        updated = stripe.Subscription.modify(sub["id"], **first, metadata=clear)
        after = _as_dict(updated) if updated is not None else {}
        if not isinstance(after, dict):
            after = {}
        if prior is None:
            rest = _renewal_back_on(after)
            if rest and rest != first:
                stripe.Subscription.modify(sub["id"], **rest)
        else:
            if after.get("cancel_at_period_end"):
                after = _as_dict(
                    stripe.Subscription.modify(sub["id"], cancel_at_period_end=False) or {}
                )
            if _cancel_at_ts(after) != prior:
                stripe.Subscription.modify(sub["id"], cancel_at=prior)
        resumed += 1
    return resumed


def _prior_cancel_at(meta: dict) -> int | None:
    """The later cancel date stop_renewal_for_review kept, or None."""
    try:
        return int(meta.get(MINOR_REVIEW_PRIOR_CANCEL_AT) or 0) or None
    except (TypeError, ValueError):
        return None


def _when(ts) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d %b %Y")


def subscription_summaries(user) -> list[dict]:
    """Every subscription on the rider's Stripe customer, for the review
    tool's dry run: id, status, whether it renews, whether it is set to end
    and when (ends_at), a cancel date after one more renewal (ends_later),
    and when the period ends. cancel_at_period_end is true for a
    cancellation in either of Stripe's shapes (cancel_at or
    cancel_at_period_end), so older callers still read it right. Read only.
    Raises if Stripe can't be reached."""
    customer_id = getattr(user, "stripe_customer_id", None)
    if not (is_configured() and customer_id):
        return []
    _client()
    out = []
    for sub in _subscriptions(customer_id):
        out.append({
            "id": sub.get("id"),
            "status": sub.get("status"),
            "renews": _renews(sub),
            "cancel_at_period_end": _cancel_pending(sub),
            "ends_at": _when(_ends_at_ts(sub)),
            "ends_later": _when(_later_cancel_at_ts(sub)),
            "period_end": _when(_period_end_ts(sub)),
        })
    return out


def refundable_payments(user) -> list[dict]:
    """Every successful payment on the rider's Stripe customer with money
    still to refund: id, the payment intent, amount left to refund (in the
    smallest unit), currency and date. Read only; refunds are made by hand
    in the dashboard. Raises if Stripe can't be reached."""
    customer_id = getattr(user, "stripe_customer_id", None)
    if not (is_configured() and customer_id):
        return []
    _client()
    out = []
    for charge in stripe.Charge.list(customer=customer_id, limit=100).auto_paging_iter():
        c = _as_dict(charge)
        left = (c.get("amount") or 0) - (c.get("amount_refunded") or 0)
        if c.get("status") != "succeeded" or not c.get("paid", True) or left <= 0:
            continue
        out.append({
            "id": c.get("id"),
            "payment_intent": c.get("payment_intent"),
            "amount": left,
            "currency": (c.get("currency") or "").upper(),
            "date": _when(c.get("created")),
        })
    return out


def dashboard_base() -> str:
    """The Stripe dashboard root for the key in use: test-mode records live
    under /test, and a link to the wrong mode shows nothing."""
    key = settings.stripe_secret_key or ""
    test = key.startswith(("sk_test_", "rk_test_"))
    return "https://dashboard.stripe.com/test" if test else "https://dashboard.stripe.com"


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

    # A cancellation that hasn't taken effect yet: Stripe keeps the status
    # active to the end of what was paid for, so this is the only record that
    # the membership won't renew. Set only when no further payment is due
    # before it ends (_cancel_pending), since Settings tells the rider so:
    # a cancel date in a later period renews once more first, and stays None
    # until that renewal makes it the current one. None once it renews again
    # or has ended.
    cancel_at = None
    ends = _ends_at_ts(sub) if status in _RENEWING else None
    if ends:
        cancel_at = datetime.fromtimestamp(ends, tz=timezone.utc).replace(tzinfo=None)

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
        user.subscription_cancel_at = cancel_at
        if not user.stripe_customer_id:
            user.stripe_customer_id = customer_id
        db.commit()
        logger.info(
            "Subscription %s for user %s (period end %s, ends %s)",
            status, user.id, period_end, cancel_at,
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
