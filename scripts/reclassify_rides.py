"""Match every unmatched ride in the active plan's window to its session.

    railway run --service Postgres bash -c \
      'DATABASE_URL="$DATABASE_PUBLIC_URL" .venv/bin/python scripts/reclassify_rides.py --email gareth@example.com'

Idempotent: a ride already matched is left alone. Deterministic scoring only,
no model calls. Prints the three-way compliance for the last 28 days after.
"""

import argparse
from datetime import date, timedelta

from app.database import SessionLocal
from app.models.user import User
from app.services.plan_compliance_service import classify_unlinked_rides, compliance_summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--since", help="YYYY-MM-DD (default: plan start)")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == args.email.strip().lower()).first()
        if user is None:
            raise SystemExit(f"No rider {args.email}")
        since = date.fromisoformat(args.since) if args.since else None
        result = classify_unlinked_rides(db, user.id, since)
        print("matching:", result)
        today = date.today()
        cs = compliance_summary(db, user.id, today - timedelta(days=28), today)
        if not cs:
            print("no active plan"); return
        for k in ("plan", "planned_sessions", "as_prescribed", "deviated", "missed", "skipped", "off_plan_rides", "off_plan_on_rest_days"):
            print(f"  {k}: {cs[k]}")
        for d in cs.get("deviations", []):
            print(f"  deviated {d['date']}  {d['planned']}  score {d['score']}  {d['how']}")
        for o in cs.get("off_plan", []):
            print(f"  off-plan {o['date']}  {o['title']}  type={o['type']}  tss={o['tss']}  rest_day={o['on_rest_day']}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
