"""Review the safety record by hand: red-flag events, holds only Forma can
lift, and closing an account that belongs to someone under 18.

    # Unreviewed red-flag events, newest first, and the open admin-only holds:
    railway run --service Postgres bash -c \
      'DATABASE_URL="$DATABASE_PUBLIC_URL" .venv/bin/python scripts/review_safety_event.py'

    # Mark one event reviewed:
    ... scripts/review_safety_event.py --reviewed EVENT_ID --note "Read it, reply was right" --commit

    # Lift a hold by hand, for example an adult the check read as under 18:
    ... scripts/review_safety_event.py --lift-hold HOLD_ID --note "Emailed me: 47, not 17" --commit

    # Close an under-18 account: cancel every Stripe subscription, delete the
    # account through the GDPR path, mark its events reviewed, then print the
    # refund steps for the Stripe dashboard:
    ... scripts/review_safety_event.py --close-minor rider@example.com --commit

    # Under 18 found outside the chat, with the age they gave (it sets how
    # long the safety records are kept; with no age, as if 13):
    ... scripts/review_safety_event.py --close-minor rider@example.com --force --age 15 --commit

Dry run by default: it prints what it would do and writes nothing, in the
database or in Stripe. --commit does it. Refunds are never made from here:
the script prints the steps, and you make them in the Stripe dashboard.

--lift-hold and --close-minor stop, with nothing changed, when the rider has
a Stripe customer but the Stripe keys aren't loaded: otherwise they would
report no subscriptions and no payments when there may be both.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session  # noqa: E402

from app.models.safety import SafetyEvent, SafetyHold  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import billing_service, gdpr_service, safety_service  # noqa: E402

# A failed ordinary reply (no red flag) is an outage record, not a safety
# concern: counted in the list, shown only with --include-failures.
_FAILED_ORDINARY = ("reply_failed", "none")
_CLIP = 160
CLOSE_NOTE = "Closed as under 18 on review"


def _clip(text: str | None, limit: int = _CLIP) -> str:
    flat = " / ".join(line.strip() for line in (text or "").splitlines() if line.strip())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def _when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def _email(db: Session, user_id: str) -> str:
    user = db.get(User, user_id)
    if user is None:
        return "(account purged)"
    return f"{user.email} (closed)" if user.deleted_at else user.email


def _stripe_missing(user: User | None) -> list[str]:
    """Lines saying why the tool stops, when this rider has a Stripe customer
    but the Stripe keys aren't loaded (under `railway run --service Postgres`
    alone, or a local .env with none). Empty when it may go on."""
    customer = getattr(user, "stripe_customer_id", None)
    if not customer or billing_service.is_configured():
        return []
    return [
        f"Stripe isn't configured here, but this rider has Stripe customer {customer}, "
        "so nothing was changed. Without the keys the tool would report no "
        "subscriptions and no payments when there may be both. Run it again with the "
        "API service's variables loaded as well (STRIPE_SECRET_KEY and STRIPE_PRICE_ID)."
    ]


def _under_18_date(user: User | None) -> date | None:
    """The date of birth on file when it makes the rider under 18 today."""
    born = getattr(user, "date_of_birth", None)
    if born is None:
        return None
    if isinstance(born, datetime):
        born = born.date()
    today = datetime.utcnow().date()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return born if age < 18 else None


def _hold_state(hold: SafetyHold | None) -> str:
    if hold is None:
        return "none"
    if hold.lifted_at is None:
        return f"{hold.id} (open, {safety_service.lift_kind(hold)})"
    return f"{hold.id} (lifted {_when(hold.lifted_at)}, {hold.lifted_how})"


# === The list ===


def list_unreviewed(db: Session, *, limit: int = 50, include_failures: bool = False) -> list[str]:
    """Unreviewed red-flag events, newest first, then every open hold only
    Forma can lift (under 18, or set by hand)."""
    rows = (
        db.query(SafetyEvent)
        .filter(SafetyEvent.reviewed_at.is_(None))
        .order_by(SafetyEvent.created_at.desc())
        .all()
    )
    failures = [e for e in rows if (e.kind, e.matched) == _FAILED_ORDINARY]
    if not include_failures:
        rows = [e for e in rows if (e.kind, e.matched) != _FAILED_ORDINARY]
    out = [f"Unreviewed safety events: {len(rows)}"]
    if failures and not include_failures:
        out.append(
            f"({len(failures)} failed ordinary replies not shown; add --include-failures to list them)"
        )
    for event in rows[:limit]:
        hold = db.get(SafetyHold, event.hold_id) if event.hold_id else None
        alerted = _when(event.founder_alerted_at) if event.founder_alerted_at else "no"
        out += [
            "",
            f"{event.id}  {_when(event.created_at)}  {_email(db, event.user_id)}",
            f"    kind: {event.kind} ({event.source})  matched: \"{_clip(event.matched, 120)}\"",
            f"    hold: {_hold_state(hold)}  alerted: {alerted}",
            f"    rider: {_clip(event.rider_message) or '(not kept)'}",
            f"    coach: {_clip(event.coach_reply) or '(not kept)'}",
        ]
    if len(rows) > limit:
        out.append(f"\n... and {len(rows) - limit} older. Add --limit to see more.")

    held = [
        h for h in db.query(SafetyHold).filter(SafetyHold.lifted_at.is_(None)).all()
        if safety_service.lift_kind(h) == "admin_only"
        and (h.expires_at is None or h.expires_at > datetime.utcnow())
    ]
    out += ["", f"Open holds only Forma can lift: {len(held)}"]
    for hold in sorted(held, key=lambda h: h.opened_at or datetime.min, reverse=True):
        out.append(
            f"  {hold.id}  {_email(db, hold.user_id)}  {hold.red_flag or hold.source}  "
            f"opened {_when(hold.opened_at)}  \"{_clip(hold.reason, 80)}\""
        )
    return out


# === One event reviewed ===


def mark_reviewed(db: Session, event_id: str, note: str, *, commit: bool) -> tuple[int, list[str]]:
    event = db.get(SafetyEvent, event_id)
    if event is None:
        return 1, [f"No safety event {event_id}."]
    head = [
        f"Event {event.id}  {_when(event.created_at)}  {_email(db, event.user_id)}",
        f"    kind: {event.kind}  matched: \"{_clip(event.matched, 120)}\"",
    ]
    if event.reviewed_at is not None:
        # A later review keeps the first date and adds a dated line.
        head.append(
            f"    reviewed {_when(event.reviewed_at)}: {_clip(event.review_note) or '(no note)'}"
        )
        if not commit:
            return 0, head + [f'Dry run: already reviewed; would add the note "{note}".']
        safety_service.mark_event_reviewed(db, event.id, note, commit=False)
        db.commit()
        return 0, head + ["Note added to the review."]
    if not commit:
        return 0, head + [f'Dry run: would mark it reviewed with the note "{note}".']
    safety_service.mark_event_reviewed(db, event.id, note, commit=False)
    db.commit()
    return 0, head + ["Marked reviewed."]


# === A hold lifted by hand ===


def lift_hold(db: Session, hold_id: str, note: str, *, commit: bool) -> tuple[int, list[str]]:
    hold = db.get(SafetyHold, hold_id)
    if hold is None:
        return 1, [f"No hold {hold_id}."]
    user = db.get(User, hold.user_id)
    head = [
        f"Hold {hold.id}  {_email(db, hold.user_id)}",
        f"    {hold.level}, {hold.red_flag or '-'} from {hold.source}, opened "
        f"{_when(hold.opened_at)}: \"{_clip(hold.reason, 120)}\"",
    ]
    if hold.lifted_at is not None:
        return 0, head + [
            f"Already lifted on {_when(hold.lifted_at)} ({hold.lifted_how}). Nothing changed."
        ]
    minor = hold.red_flag == "minor"
    # An age is one fact about the account: admin_lift lifts every open
    # under-18 hold on it together, so their events are reviewed together.
    hold_ids = {hold.id}
    if minor:
        hold_ids |= {h.id for h in safety_service.open_holds(db, hold.user_id) if h.red_flag == "minor"}
    events = (
        db.query(SafetyEvent)
        .filter(SafetyEvent.hold_id.in_(hold_ids), SafetyEvent.reviewed_at.is_(None))
        .all()
    )
    if minor:
        missing = _stripe_missing(user)
        if missing:
            return 1, head + missing
    # Lifting an under-18 hold says the rider is an adult. A child's date of
    # birth left on file would hold them again at the next re-acceptance,
    # and keep the detector's quiet window from starting, so it goes: the app
    # asks for their date of birth again when they next open it.
    wrong_date = _under_18_date(user) if minor else None
    steps = [f'lift it by hand with the note "{note}"']
    if events:
        steps.append(f"mark its {len(events)} unreviewed event(s) reviewed")
    if wrong_date is not None:
        steps.append(
            f"clear the under-18 date of birth on file ({wrong_date.isoformat()}), so the "
            "app asks for it again"
        )
    if minor:
        steps.append("turn renewal back on for any subscription Forma stopped at the hold")
    if not commit:
        return 0, head + ["Dry run: would " + ", ".join(steps) + "."]

    # Lifts every under-18 hold on the account at once, and takes the on-hold
    # labels off the planned sessions.
    safety_service.admin_lift(db, hold.id, note, commit=False)
    for event in events:
        safety_service.mark_event_reviewed(db, event.id, note, commit=False)
    if wrong_date is not None:
        user.date_of_birth = None
    db.commit()
    out = head + ["Lifted."]
    if events:
        out.append(f"Marked {len(events)} event(s) reviewed.")
    if wrong_date is not None:
        out.append(
            f"Cleared the date of birth on file ({wrong_date.isoformat()}). The app asks the "
            "rider for it again when they next open it."
        )
    if minor and user is not None and safety_service.minor_hold(db, user) is None:
        try:
            resumed = billing_service.resume_renewal_after_review(user)
            out.append(f"Renewal back on for {resumed} subscription(s).")
        except Exception as e:
            out.append(
                f"Stripe couldn't be reached to turn renewal back on ({e}). In the Stripe "
                f"dashboard, open {billing_service.dashboard_base()}/customers/"
                f"{user.stripe_customer_id}, open the subscription, and remove the scheduled "
                "cancellation."
            )
    return 0, out


# === An under-18 account closed ===


def _money(amount: int, currency: str) -> str:
    return f"{currency} {amount / 100:.2f}"


def refund_steps(user: User, payments: list[dict] | None, stripe_error: str | None) -> list[str]:
    """The exact steps to refund this rider in the Stripe dashboard."""
    base = billing_service.dashboard_base()
    if not user.stripe_customer_id:
        return ["Refunds: no Stripe customer on this account, so nothing was paid and nothing needs refunding."]
    customer = f"{base}/customers/{user.stripe_customer_id}"
    out = [
        "Refund steps, in the Stripe dashboard:",
        f"1. Open {customer}.",
        '2. Check every subscription shows "Canceled". If one doesn\'t, open it, click '
        '"Actions", then "Cancel subscription", choose "Immediately", and confirm.',
    ]
    how = (
        'click "Refund", keep the full amount, set the reason to "Requested by '
        'customer", and click "Refund".'
    )
    if stripe_error is not None:
        out.append(
            f"3. Stripe couldn't be read from here ({stripe_error}), so the payments aren't "
            'listed. On the customer page, open each payment under "Payments" that shows '
            f'"Succeeded", and {how}'
        )
    elif not payments:
        out.append("3. No payments are waiting for a refund. Nothing more to do.")
        return out
    else:
        out.append(f"3. Refund each payment below: open the link, {how}")
        for p in payments:
            ref = p.get("payment_intent") or p["id"]
            out.append(f"   {p['date']}  {_money(p['amount'], p['currency'])}  {base}/payments/{ref}")
    out.append(
        '4. Check each payment now shows "Refunded". The money reaches the card in 5 to 10 '
        "working days."
    )
    return out


def close_minor(
    db: Session,
    email: str,
    *,
    note: str | None,
    commit: bool,
    force: bool = False,
    age: int | None = None,
) -> tuple[int, list[str]]:
    email = (email or "").strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        return 1, [f"No rider with email {email}."]
    hold = safety_service.minor_hold(db, user)
    if hold is None and not force:
        return 1, [
            f"{email} has no open under-18 hold, so nothing was done. If you know from "
            "elsewhere that the rider is under 18, run it again with --force: that puts "
            "the hold on the record first, so the safety records are kept to the "
            "under-18 rule."
        ]
    missing = _stripe_missing(user)
    if missing:
        return 1, [f"Rider: {user.full_name or '-'} <{user.email}>", *missing]
    note = (note or "").strip() or CLOSE_NOTE
    events = (
        db.query(SafetyEvent)
        .filter(SafetyEvent.user_id == user.id, SafetyEvent.reviewed_at.is_(None))
        .all()
    )
    out = [
        f"Rider: {user.full_name or '-'} <{user.email}>"
        + (f"  (already closed {_when(user.deleted_at)})" if user.deleted_at else ""),
        f"Under-18 hold: {hold.id} opened {_when(hold.opened_at)}" if hold else
        "Under-18 hold: none yet (--force puts one on the record)",
    ]

    subs, stripe_error = [], None
    try:
        subs = billing_service.subscription_summaries(user)
    except Exception as e:
        stripe_error = str(e)
    live = [s for s in subs if s["status"] not in ("canceled", "incomplete_expired")]
    if stripe_error:
        out.append(f"Stripe subscriptions: couldn't read them ({stripe_error}).")
    elif not user.stripe_customer_id:
        out.append("Stripe subscriptions: no Stripe customer on this account.")
    else:
        out.append(f"Live Stripe subscriptions found: {len(live)}")
        out += [
            f"  {s['id']}  {s['status']}"
            + ("  (set to end at the period end)" if s["cancel_at_period_end"] else "")
            + (f"  period ends {s['period_end']}" if s["period_end"] else "")
            for s in live
        ]

    payments, pay_error = None, None
    try:
        payments = billing_service.refundable_payments(user)
    except Exception as e:
        pay_error = str(e)

    if not commit:
        out += [
            "",
            "Dry run: with --commit this would",
            "  1. cancel every Stripe subscription now (nothing is deleted if Stripe can't be reached),",
            "  2. close the account through the GDPR delete path (locked out now, purged later),",
            f"  3. mark {len(events)} unreviewed event(s) reviewed with the note \"{note}\",",
            "  4. " + _age_step(age),
            "  5. print the refund steps below.",
            "",
        ]
        return 0, out + refund_steps(user, payments, pay_error)

    # The membership ends first: closing the account while the card goes on
    # being charged is the one outcome worse than trying again.
    try:
        ended = billing_service.cancel_all_subscriptions(user)
    except Exception as e:
        return 1, out + [
            f"Stripe couldn't cancel the subscription ({e}), so nothing was changed. "
            "Run it again in a minute."
        ]
    out.append(f"Cancelled {ended} subscription(s) in Stripe.")

    if hold is None:
        hold = safety_service.open_hold(
            db, user, "hold_all", "Closed as under 18 on review", "admin",
            red_flag="minor", note=note, commit=False,
        )
        out.append(f"Under-18 hold {hold.id} put on the record.")
    if age is not None:
        # The age sets how long the safety records are kept: through the
        # 21st birthday it implies (safety_service.minor_retention_end).
        db.flush()
        now = datetime.utcnow()
        db.add(SafetyEvent(
            user_id=user.id, kind="minor", source="admin", hold_id=hold.id,
            matched=f"Age {age}, recorded on review", stated_age=age,
            reviewed_at=now, review_note=f"{now:%Y-%m-%d %H:%M} {note}",
        ))
        out.append(f"Age {age} recorded on the under-18 record.")
    for event in events:
        safety_service.mark_event_reviewed(db, event.id, note, commit=False)
    # Commits the reviews and the hold with the account's closure.
    gdpr_service.delete_account(db, user)
    out += [
        f"Marked {len(events)} event(s) reviewed.",
        "Account closed: locked out now, and the purge removes it after the retention "
        "window. The under-18 hold stays on the record, so the safety records are kept "
        "to the under-18 rule.",
        "",
    ]
    return 0, out + refund_steps(user, payments, pay_error)


def _age_step(age: int | None) -> str:
    if age is None:
        return (
            "keep the safety records as if the rider were 13 when flagged, unless an "
            "age is already on the record (add --age to give the one they told you),"
        )
    return f"record age {age}, so the safety records are kept through their 21st birthday,"


# === Entry point ===


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = ap.add_mutually_exclusive_group()
    action.add_argument("--reviewed", metavar="EVENT_ID", help="mark this safety event reviewed")
    action.add_argument("--lift-hold", metavar="HOLD_ID", help="lift this hold by hand")
    action.add_argument("--close-minor", metavar="USER_EMAIL", help="close this under-18 account")
    ap.add_argument("--note", help="why: kept on the record (needed for --reviewed and --lift-hold)")
    ap.add_argument("--commit", action="store_true", help="do it (default: dry run)")
    ap.add_argument("--force", action="store_true",
                    help="--close-minor with no under-18 hold on record: put one on first")
    ap.add_argument("--age", type=int,
                    help="--close-minor: the age the rider told you (under 18); it sets how "
                         "long the safety records are kept")
    ap.add_argument("--limit", type=int, default=50, help="events to list (default 50)")
    ap.add_argument("--include-failures", action="store_true",
                    help="list failed ordinary replies too")
    args = ap.parse_args(argv)
    if (args.reviewed or args.lift_hold) and not (args.note or "").strip():
        ap.error("--note is required with --reviewed and --lift-hold")
    if args.age is not None:
        if not args.close_minor:
            ap.error("--age goes with --close-minor")
        if not 0 < args.age < 18:
            ap.error("--age is the rider's age under 18, from 1 to 17")
    return args


def run(db: Session, args: argparse.Namespace) -> tuple[int, list[str]]:
    note = (args.note or "").strip()
    if args.reviewed:
        return mark_reviewed(db, args.reviewed.strip(), note, commit=args.commit)
    if args.lift_hold:
        return lift_hold(db, args.lift_hold.strip(), note, commit=args.commit)
    if args.close_minor:
        return close_minor(
            db, args.close_minor, note=note, commit=args.commit, force=args.force,
            age=args.age,
        )
    return 0, list_unreviewed(db, limit=args.limit, include_failures=args.include_failures)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        code, lines = run(db, args)
    finally:
        db.close()
    print("\n".join(lines))
    if not args.commit and (args.reviewed or args.lift_hold or args.close_minor) and code == 0:
        print("\nDry run: nothing written. Add --commit to do it.")
    return code


if __name__ == "__main__":
    sys.exit(main())
