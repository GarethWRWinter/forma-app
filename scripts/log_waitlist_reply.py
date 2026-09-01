"""Log a rider's reply to a waitlist letter, verbatim, into waitlist_replies.

Replies arrive in Gareth's inbox and get answered by hand; this records what
was said so product decisions can be made from a query instead of a memory.
Paste the email body into a text file first, exactly as sent.

    railway run --service Postgres bash -c \
      'DATABASE_URL="$DATABASE_PUBLIC_URL" .venv/bin/python \
       scripts/log_waitlist_reply.py --email rider@example.com \
       --file reply.txt --received 2026-08-28 \
       --theme consistency --theme holiday-planning'

Dry run by default. Nothing is written without --commit.
"""

import argparse
from datetime import datetime
from pathlib import Path

from app.database import SessionLocal
from app.models.waitlist import WaitlistEntry, WaitlistReply


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True, help="The rider's waitlist address.")
    ap.add_argument("--file", required=True, help="Text file holding the reply verbatim.")
    ap.add_argument(
        "--received",
        required=True,
        help="When the reply arrived (YYYY-MM-DD, or YYYY-MM-DDTHH:MM).",
    )
    ap.add_argument(
        "--theme",
        action="append",
        default=[],
        help="Kebab-case theme tag. Repeatable.",
    )
    ap.add_argument("--channel", default="email")
    ap.add_argument("--commit", action="store_true", help="Write the row.")
    args = ap.parse_args()

    raw_text = Path(args.file).read_text(encoding="utf-8").strip()
    if not raw_text:
        raise SystemExit(f"{args.file} is empty; nothing to log.")
    received_at = datetime.fromisoformat(args.received)

    db = SessionLocal()
    try:
        entry = (
            db.query(WaitlistEntry)
            .filter(WaitlistEntry.email == args.email.strip().lower())
            .first()
        )
        if entry is None:
            raise SystemExit(
                f"No waitlist entry for {args.email}. Add them to the list first."
            )

        print(f"Rider: {entry.name or '(no name)'} <{entry.email}>")
        print(f"Received: {received_at:%d %b %Y %H:%M}")
        print(f"Themes: {', '.join(args.theme) or '(none)'}")
        print(f"Reply: {len(raw_text)} characters, starts: {raw_text[:80]!r}")

        if not args.commit:
            print("\nDry run. Re-run with --commit to write it.")
            return

        db.add(WaitlistReply(
            waitlist_entry_id=entry.id,
            channel=args.channel,
            raw_text=raw_text,
            themes=args.theme or None,
            received_at=received_at,
        ))
        db.commit()
        print("\nLogged.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
