"""Hard-delete accounts past the GDPR retention window.

Soft-deleted accounts (deleted_at set) are locked out immediately; this purge
finishes the erasure after RETENTION_DAYS by removing every row the user owns
in FK-safe order, then the user. Idempotent and safe to run on a schedule.

Dry-run by default — prints what it *would* delete. Pass --commit to execute.

Usage:  python -m scripts.purge_deleted_accounts [--commit] [--days N]
"""
import argparse
import logging
from datetime import datetime, timedelta

from sqlalchemy import text

from app.database import SessionLocal

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("purge")

RETENTION_DAYS = 30

# Detail tables keyed off a parent's id → deleted via the parent's user_id.
_CHILD = [
    ("ride_data", "rides", "ride_id"),
    ("segment_efforts", "rides", "ride_id"),
    ("workout_steps", "workouts", "workout_id"),
    ("chat_messages", "chat_sessions", "session_id"),
    ("training_phases", "training_plans", "plan_id"),  # no user_id — child of the plan
]
# Direct user_id tables, in FK-safe order (a table is listed only after
# everything that references it). rides<->workouts is circular, so we null
# those two link columns first (below). training_phases → training_plans →
# goal_events; memory_edges → memory_entities.
# chat_attachments holds the rider's raw uploaded ride file, GPS included, so
# it must go before users or an erasure request leaves the most personal data
# of the lot behind. It also carries a hard FK to users.id, so omitting it
# fails the whole purge on the final DELETE rather than failing quietly.
_USER_TABLES = [
    "workouts", "rides", "training_plans", "goal_events",
    "chat_attachments", "chat_sessions", "daily_metrics", "coach_nudges",
    "onboarding_responses",
    "mem_edges", "mem_entities", "forma_calls", "refresh_tokens",
    "strava_tokens", "dropbox_tokens", "trainingpeaks_tokens",
]


def _purge_user(db, user_id: str) -> None:
    p = {"uid": user_id}
    # Break the rides <-> workouts circular foreign key before deleting either.
    db.execute(text("UPDATE rides SET workout_id = NULL WHERE user_id = :uid"), p)
    db.execute(text("UPDATE workouts SET actual_ride_id = NULL WHERE user_id = :uid"), p)
    for tbl, parent, fk in _CHILD:
        db.execute(
            text(f"DELETE FROM {tbl} WHERE {fk} IN "
                 f"(SELECT id FROM {parent} WHERE user_id = :uid)"),
            p,
        )
    for tbl in _USER_TABLES:
        db.execute(text(f"DELETE FROM {tbl} WHERE user_id = :uid"), p)
    db.execute(text("DELETE FROM users WHERE id = :uid"), p)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--days", type=int, default=RETENTION_DAYS)
    args = ap.parse_args()

    cutoff = datetime.utcnow() - timedelta(days=args.days)
    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT id, email FROM users WHERE deleted_at IS NOT NULL AND deleted_at < :c"),
            {"c": cutoff},
        ).fetchall()
        if not rows:
            logger.info("No accounts past the %d-day retention window.", args.days)
            return
        for r in rows:
            if args.commit:
                _purge_user(db, r.id)
                logger.info("Purged account %s", r.id)
            else:
                logger.info("[dry-run] would purge account %s (%s)", r.id, r.email)
        if args.commit:
            db.commit()
            logger.info("Purged %d account(s).", len(rows))
        else:
            logger.info("%d account(s) eligible. Re-run with --commit to purge.", len(rows))
    finally:
        db.close()


if __name__ == "__main__":
    main()
