"""End expired plans and build between-goals blocks, by hand.

The app runs the same pass every hour (app/services/season_service.py), so
this is for checking what it would do before the first pass, or forcing one.

Dry run by default: logs what it would change. Pass --commit to execute.

Usage:  python -m scripts.season_keeper [--commit]
"""
import argparse
import logging

from app.database import SessionLocal
from app.services.season_service import ensure_all

logging.basicConfig(level=logging.INFO)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="actually change plans (default: dry run)")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        counts = ensure_all(db, dry_run=not args.commit)
        logging.info("%s: %s", "Done" if args.commit else "Dry run", dict(counts))
    finally:
        db.close()


if __name__ == "__main__":
    main()
