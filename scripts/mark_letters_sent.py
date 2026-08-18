"""Mark letters as sent when you sent them yourself.

The first eighteen go out by hand, from Gareth's own mail client, so nothing
in the app knows they happened. Without this they stay flagged as owed a
letter, and the next time anyone runs the backfill they get written to twice.

Sending is not this script's job. It only records what already happened.

    railway run --service Postgres bash -c \
      'DATABASE_URL="$DATABASE_PUBLIC_URL" .venv/bin/python \
       scripts/mark_letters_sent.py --before 2026-08-10'

Dry run by default. Nothing is written without --commit.
"""

import argparse
from datetime import datetime

from app.database import SessionLocal
from app.models.waitlist import WaitlistEntry


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--before",
        help="Only riders who joined before this date (YYYY-MM-DD).",
    )
    ap.add_argument(
        "--email",
        action="append",
        default=[],
        help="Mark one address. Repeatable, for when you send a few at a time.",
    )
    ap.add_argument("--commit", action="store_true", help="Write the change.")
    args = ap.parse_args()

    if not args.before and not args.email:
        ap.error("give --before, or one or more --email")

    db = SessionLocal()
    try:
        q = db.query(WaitlistEntry).filter(WaitlistEntry.letter0_sent.is_(False))
        if args.before:
            q = q.filter(
                WaitlistEntry.created_at < datetime.strptime(args.before, "%Y-%m-%d")
            )
        if args.email:
            q = q.filter(WaitlistEntry.email.in_([e.lower() for e in args.email]))

        riders = q.order_by(WaitlistEntry.created_at.asc()).all()
        for r in riders:
            print(f"  {r.email:<38} joined {r.created_at.date()}")

        if not riders:
            print("Nobody matches. They may already be marked.")
            return

        if args.commit:
            for r in riders:
                r.letter0_sent = True
            db.commit()
            print(f"\nMarked {len(riders)} as sent. The backfill will skip them now.")
        else:
            print(f"\nDRY RUN. Would mark {len(riders)}. Nothing written.")
            print("Re-run with --commit once you have actually sent them.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
