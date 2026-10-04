"""Hard-delete accounts past the GDPR retention window, by hand.

The app runs the same purge once a day (app/services/purge_service.py), so
this is for checking what it would do, or forcing a run.

Dry run by default: prints what it would delete. Pass --commit to execute.

Usage:  python -m scripts.purge_deleted_accounts [--commit] [--days N]
"""
import argparse
import logging

from app.database import SessionLocal
from app.services.purge_service import RETENTION_DAYS, purge_expired_accounts

logging.basicConfig(level=logging.INFO)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--days", type=int, default=RETENTION_DAYS)
    args = ap.parse_args()
    db = SessionLocal()
    try:
        n = purge_expired_accounts(db, days=args.days, commit=args.commit)
        if args.commit:
            logging.info("Purged %d account(s).", n)
    finally:
        db.close()


if __name__ == "__main__":
    main()
