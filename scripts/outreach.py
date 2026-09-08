"""See, or send, the coach's outreach emails.

    # Who is due right now, and the exact emails (nothing is sent):
    railway run --service Postgres bash -c \
      'DATABASE_URL="$DATABASE_PUBLIC_URL" .venv/bin/python scripts/outreach.py'

    # Compose for one rider whether or not they are due (still nothing sent):
    ... scripts/outreach.py --email rider@example.com

    # Actually send to that one rider and log it:
    ... scripts/outreach.py --email rider@example.com --send

Dry run by default. --send is the only thing that emails anyone.
"""

import argparse
import asyncio

from app.database import SessionLocal
from app.models.outreach import OutreachLog
from app.models.user import User
from app.services import email_service
from app.services.activation_service import activation_state
from app.services.outreach_service import _threshold_due, compose, send_due


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", help="One rider; composes regardless of whether they are due.")
    ap.add_argument("--send", action="store_true", help="Send (and log) instead of printing.")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        if args.email:
            user = db.query(User).filter(User.email == args.email.strip().lower()).first()
            if user is None:
                raise SystemExit(f"No rider with email {args.email}")
            state = activation_state(db, user)
            print(f"Rider: {user.full_name} <{user.email}>")
            print(f"Stage: {state['stage']} | quiet {state['quiet_days']} day(s) | next: {state['next_action']['title'] if state['next_action'] else '-'}")
            subject, body = compose(db, user, state)
            print(f"\nSubject: {subject}\n\n{body}\n")
            if args.send:
                ok = asyncio.run(email_service.send(user.email, subject, body))
                if ok:
                    db.add(OutreachLog(
                        user_id=user.id, stage=state["stage"],
                        threshold_days=_threshold_due(state["quiet_days"]) or 0,
                        subject=subject, body=body,
                    ))
                    db.commit()
                print("Sent and logged." if ok else "Send FAILED (see logs).")
            else:
                print("Dry run. Add --send to send this to the rider.")
            return
        report = asyncio.run(send_due(db, dry_run=not args.send))
        if not report:
            print("Nobody is due.")
        for r in report:
            print(f"--- {r['email']} | stage {r['stage']} | {r['threshold_days']}d quiet ---")
            print(f"Subject: {r['subject']}\n\n{r['body']}\n")
        print("Dry run." if not args.send else f"Sent {sum(1 for r in report if r.get('sent'))} email(s).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
