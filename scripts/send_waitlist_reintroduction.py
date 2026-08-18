"""Send the re-introduction to riders who joined before the letters existed.

Eighteen people signed up between July and early August through the old sheet
and heard nothing since. The standard Letter 0 opens with "your place is held",
which reads as nonsense to someone who held it six weeks ago, so they get a
letter that owns the silence instead.

Sends once, marks letter0_sent, and from then on they are in the normal weekly
rhythm with everyone else.

    railway run --service Postgres bash -c \\
      'DATABASE_URL="$DATABASE_PUBLIC_URL" .venv/bin/python \\
       scripts/send_waitlist_reintroduction.py --before 2026-08-10'

Dry run by default. Nothing sends without --commit.
"""

import argparse
import asyncio
from datetime import datetime

from app.api.v1.waitlist import _position
from app.database import SessionLocal
from app.models.waitlist import WaitlistEntry
from app.services import email_service


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--before",
        required=True,
        help="Only riders who joined before this date (YYYY-MM-DD). This is what "
        "separates the old sheet cohort from anyone who has joined since.",
    )
    ap.add_argument("--commit", action="store_true", help="Actually send.")
    args = ap.parse_args()

    cutoff = datetime.strptime(args.before, "%Y-%m-%d")
    db = SessionLocal()
    try:
        riders = (
            db.query(WaitlistEntry)
            .filter(
                WaitlistEntry.letter0_sent.is_(False),
                WaitlistEntry.created_at < cutoff,
            )
            .order_by(WaitlistEntry.created_at.asc())
            .all()
        )
        print(f"{len(riders)} riders joined before {args.before} and have had nothing.\n")

        for r in riders:
            pos = _position(db, r)
            month = r.created_at.strftime("%B")
            print(f"  {r.email:<38} joined {r.created_at.date()}  position {pos}")
            if not args.commit:
                continue
            ok = await email_service.send_waitlist_reintroduction(
                r.email, name=r.name, position=pos, joined_month=month
            )
            if ok:
                r.letter0_sent = True
                db.commit()
            else:
                print(f"    FAILED, left unsent so it can be retried: {r.email}")

        if args.commit:
            print(f"\nSent to {len(riders)}. They are in the weekly rhythm from here.")
        else:
            print(f"\nDRY RUN. Would send {len(riders)}. Nothing sent, nothing marked.")
            print("Re-run with --commit when you are happy with the list.")
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
