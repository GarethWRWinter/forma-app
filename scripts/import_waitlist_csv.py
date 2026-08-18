"""Import waitlist signups collected before the API existed.

The first eighteen riders joined through a Google Sheet between July and
August 2026, before the landing form posted to the API. They had no position,
no referral code and no letters. This carries them across so there is one
record of the list instead of two.

It SENDS NOTHING. Every imported row is marked letter0_sent = False, so the
letters stay a deliberate act: review the list, then fire the admin
send-pending endpoint when you actually mean to.

Emails are read from a CSV you export, never committed here, because a real
list of people does not belong in a git repository.

    railway run python scripts/import_waitlist_csv.py ~/Downloads/waitlist.csv
    railway run python scripts/import_waitlist_csv.py ~/Downloads/waitlist.csv --commit

Without --commit it is a dry run and touches nothing.
"""

import csv
import sys
from datetime import datetime

from app.database import SessionLocal
from app.models.waitlist import WaitlistEntry, generate_referral_code

# Sheets exports US dates; the ISO form is here too so a tidied file also works.
_DATE_FORMATS = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def _parse_when(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _rows(path: str) -> list[tuple[str, datetime | None, str | None]]:
    """Pull (email, joined, name) out of whatever the export looks like."""
    out = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            lower = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            email = lower.get("email", "").lower()
            if not email or "@" not in email:
                continue
            joined = _parse_when(lower.get("timestamp") or lower.get("joined", ""))
            out.append((email, joined, lower.get("name") or None))
    return out


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    commit = "--commit" in sys.argv
    if not args:
        print(__doc__)
        sys.exit(1)

    rows = _rows(args[0])
    print(f"Read {len(rows)} rows from {args[0]}\n")

    db = SessionLocal()
    added = skipped = 0
    try:
        taken = {c for (c,) in db.query(WaitlistEntry.code).all()}
        for email, joined, name in rows:
            if db.query(WaitlistEntry).filter(WaitlistEntry.email == email).first():
                print(f"  skip    {email}  (already on the list)")
                skipped += 1
                continue

            code = generate_referral_code()
            while code in taken:
                code = generate_referral_code()
            taken.add(code)

            entry = WaitlistEntry(
                email=email,
                name=name,
                code=code,
                # Their real join date, so the queue reflects who genuinely put
                # their hand up first rather than the order of this import.
                created_at=joined or datetime.utcnow(),
                # Nothing has been sent to these people yet, and this script is
                # not the thing that decides to.
                letter0_sent=False,
            )
            db.add(entry)
            added += 1
            when = (joined or datetime.utcnow()).date()
            print(f"  import  {email}  joined {when}  code {code}")

        if commit:
            db.commit()
            print(f"\nCommitted. {added} imported, {skipped} already there.")
            print("No email has been sent. To send Letter 0 to everyone still")
            print("waiting on it, call POST /api/v1/waitlist/admin/send-pending.")
        else:
            db.rollback()
            print(f"\nDRY RUN. Would import {added}, skip {skipped}. Nothing written.")
            print("Re-run with --commit when the list above looks right.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
