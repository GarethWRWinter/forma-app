"""Run once, after the deploy that adds users.subscription_cancel_at: fill it
in for riders who cancelled their membership before the column existed.

Stripe sends nothing more for a membership cancelled in the portal until it
ends, and that event (customer.subscription.deleted) clears the date. So
without this, the riders who cancelled before the deploy (the ones found on
13 Oct 2026) go on seeing "Active" and "Current month runs to" in Settings
until their membership ends.

For every rider with a Stripe customer and a membership Stripe could still
renew, it reads their subscriptions from Stripe and sets the date the way the
webhook does (billing_service): the date a cancelled membership ends with no
further payment, or empty while any subscription still renews. It only reads
from Stripe, and it touches no other column. Safe to run again.

    # Dry run: what it would change, nothing written:
    railway run --service Postgres bash -c \\
      'DATABASE_URL="$DATABASE_PUBLIC_URL" .venv/bin/python scripts/backfill_subscription_cancel_at.py'

    # Write it:
    ... scripts/backfill_subscription_cancel_at.py --commit

It stops with nothing done when the Stripe keys aren't loaded, rather than
report every rider as renewing. It prints which Stripe mode the key is for:
riders on live Stripe need the live key.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stripe  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import billing_service  # noqa: E402


def _naive(ts: int) -> datetime:
    """Naive UTC, as the webhook stores it."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def _subscriptions(customer_id: str) -> list[dict]:
    out = []
    for sub in stripe.Subscription.list(
        customer=customer_id, status="all", limit=20
    ).auto_paging_iter():
        to_dict = getattr(sub, "to_dict", None)
        out.append(to_dict() if callable(to_dict) else dict(sub))
    return out


def ends_at(subs: list[dict]) -> tuple[bool, datetime | None]:
    """(any live subscription, when the membership ends with no further
    payment). The end is None while any live subscription still renews: a
    rider who will be charged again must not be told otherwise. With several
    that all end, the membership runs to the last of them."""
    live = [s for s in subs if s.get("status") in billing_service._RENEWING]
    if not live:
        return False, None
    if any(billing_service._renews(s) for s in live):
        return True, None
    ends = [billing_service._ends_at_ts(s) for s in live]
    ends = [t for t in ends if t]
    return True, (_naive(max(ends)) if ends else None)


def _day(value: datetime | None) -> str:
    return value.strftime("%d %b %Y") if value else "not set"


def run(db: Session, args: argparse.Namespace) -> tuple[int, list[str]]:
    if not billing_service.is_configured():
        return 1, ["The Stripe keys aren't loaded, so nothing was read or changed."]
    billing_service._client()
    test_key = (settings.stripe_secret_key or "").startswith(("sk_test_", "rk_test_"))
    mode = "test" if test_key else "live"
    riders = (
        db.query(User)
        .filter(
            User.stripe_customer_id.isnot(None),
            User.subscription_status.in_(billing_service._RENEWING),
        )
        .order_by(User.created_at)
        .all()
    )
    out = [f"Stripe mode: {mode}. Riders with a membership Stripe could renew: {len(riders)}"]
    changed = unchanged = failed = 0
    for user in riders:
        try:
            found, end = ends_at(_subscriptions(user.stripe_customer_id))
        except Exception as e:
            failed += 1
            out.append(f"  {user.email}  {user.stripe_customer_id}  couldn't read Stripe ({e})")
            continue
        if not found:
            unchanged += 1
            out.append(
                f"  {user.email}  {user.stripe_customer_id}  no live subscription in Stripe, "
                "left as it is"
            )
            continue
        if end == user.subscription_cancel_at:
            unchanged += 1
            continue
        out.append(
            f"  {user.email}  {user.stripe_customer_id}  ends: "
            f"{_day(user.subscription_cancel_at)} becomes {_day(end)}"
        )
        user.subscription_cancel_at = end
        changed += 1
    if args.commit:
        db.commit()
    else:
        db.rollback()
    verb = "Changed" if args.commit else "Would change"
    out.append(f"{verb} {changed}, unchanged {unchanged}, couldn't read {failed}.")
    return (1 if failed else 0), out


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--commit", action="store_true", help="write the dates (default: dry run)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        code, lines = run(db, args)
    finally:
        db.close()
    print("\n".join(lines))
    if not args.commit:
        print("\nDry run: nothing written. Add --commit to do it.")
    return code


if __name__ == "__main__":
    sys.exit(main())
