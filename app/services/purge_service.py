"""Finish GDPR erasure: hard-delete accounts past the retention window.

Deleting an account in the app is a soft delete: the rider is locked out at
once, their integrations are cut, and the data waits RETENTION_DAYS (the
grace window the privacy policy promises) before this removes it for good.

The table list is read from the database, not written down. A hand-kept list
went stale (five tables added after it was written, every one with a hard
foreign key to users), which would have failed every purge on the final
DELETE and left every deleted account in place (launch audit, 4 Oct 2026).
Now any table with a user_id column, and any table hanging off one of those,
is found on each run.

Two exceptions are deliberate:
- founding_ledger keeps the number and loses the person: a founding number
  is never reissued, even after the account that held it is gone.
- strava_segments are public segment definitions shared by every rider.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

RETENTION_DAYS = 30
KEEP_NUMBER_TABLES = {"founding_ledger"}

# Circular references between rider-owned tables, broken before deleting.
_LINKS_TO_NULL = [
    ("rides", "workout_id"),
    ("workouts", "actual_ride_id"),
    ("goal_events", "actual_ride_id"),
]


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _schema(db: Session) -> tuple[set[str], list[tuple[str, str, str, str]], set[tuple[str, str]]]:
    """Tables with a user_id column, every foreign key, and every column."""
    owned = {
        r[0]
        for r in db.execute(text(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'user_id'"
        ))
    }
    fks = [
        (r[0], r[1], r[2], r[3])
        for r in db.execute(text(
            "SELECT kcu.table_name, kcu.column_name, ccu.table_name, ccu.column_name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
            "JOIN information_schema.constraint_column_usage ccu "
            "  ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema "
            "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'"
        ))
    ]
    columns = {
        (r[0], r[1])
        for r in db.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public'"
        ))
    }
    return owned, fks, columns


def _targets(owned: set[str], fks: list[tuple[str, str, str, str]]) -> dict[str, str]:
    """Each table holding this rider's rows, with the WHERE clause that finds
    them. Tables without user_id are reached through the parent they hang
    off, to any depth."""
    where = {
        t: "user_id = :uid"
        for t in owned
        if t != "users" and t not in KEEP_NUMBER_TABLES
    }
    changed = True
    while changed:
        changed = False
        for child, col, parent, pcol in fks:
            if child in where or child in owned or child == "users":
                continue
            if parent in where:
                where[child] = (
                    f"{_q(col)} IN (SELECT {_q(pcol)} FROM {_q(parent)} WHERE {where[parent]})"
                )
                changed = True
    return where


def purge_user(db: Session, user_id: str) -> None:
    """Remove every row this rider owns, then the rider. One transaction:
    either the whole account goes or none of it does."""
    p = {"uid": user_id}
    owned, fks, columns = _schema(db)

    for table, column in _LINKS_TO_NULL:
        if (table, column) in columns and (table, "user_id") in columns:
            db.execute(text(f"UPDATE {_q(table)} SET {_q(column)} = NULL WHERE user_id = :uid"), p)
    for table in KEEP_NUMBER_TABLES & owned:
        db.execute(text(f"UPDATE {_q(table)} SET user_id = NULL WHERE user_id = :uid"), p)

    # Children before parents, found by trying: a delete the database refuses
    # (a child still points at it) waits for the next pass.
    pending = list(_targets(owned, fks).items())
    while pending:
        refused = []
        for table, clause in pending:
            savepoint = db.begin_nested()
            try:
                db.execute(text(f"DELETE FROM {_q(table)} WHERE {clause}"), p)
                savepoint.commit()
            except IntegrityError:
                savepoint.rollback()
                refused.append((table, clause))
        if len(refused) == len(pending):
            raise RuntimeError(
                "Purge cannot make progress; still referenced: "
                + ", ".join(t for t, _ in refused)
            )
        pending = refused

    db.execute(text("DELETE FROM users WHERE id = :uid"), p)


def purge_expired_accounts(db: Session, days: int = RETENTION_DAYS, commit: bool = True) -> int:
    """Purge every account soft-deleted more than `days` ago. Each account
    commits on its own, so one failure never holds up the rest."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = db.execute(
        text("SELECT id FROM users WHERE deleted_at IS NOT NULL AND deleted_at < :c"),
        {"c": cutoff},
    ).fetchall()
    purged = 0
    for (user_id,) in rows:
        if not commit:
            logger.info("[dry run] would purge account %s", user_id)
            continue
        try:
            purge_user(db, user_id)
            db.commit()
            purged += 1
            logger.info("Purged account %s", user_id)
        except Exception:
            db.rollback()
            logger.exception("Purge failed for account %s", user_id)
    return purged


_task: asyncio.Task | None = None


async def _loop(interval: int) -> None:
    from app.database import SessionLocal

    while True:
        def _run() -> int:
            db = SessionLocal()
            try:
                return purge_expired_accounts(db)
            finally:
                db.close()

        try:
            n = await asyncio.to_thread(_run)
            if n:
                logger.info("Daily purge removed %d account(s)", n)
        except Exception:
            logger.exception("Daily purge failed")
        await asyncio.sleep(interval)


def start_purge(interval: int = 24 * 3600) -> None:
    """Run the purge once a day inside the app, so the 30-day promise holds
    without anyone remembering to run a script."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(interval))
