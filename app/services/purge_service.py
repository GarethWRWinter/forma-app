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

Three exceptions are deliberate:
- founding_ledger keeps the number and loses the person: a founding number
  is never reissued, even after the account that held it is gone.
- strava_segments are public segment definitions shared by every rider.
- The safety records (holds, red-flag events with the rider's words and the
  coach's reply, consent, health screening) are kept for
  SAFETY_RETENTION_YEARS after the account goes, because they are the proof
  of what the rider was told and agreed to if a claim follows. For an
  account flagged as a minor they are kept until the rider's 21st birthday
  if that is later, worked out from the age they told us, or the youngest
  plausible age when they gave none (safety_service.minor_retention_end),
  and from their date of birth, whichever is later. The purge stamps subject_deleted_at and retain_until on
  them instead of deleting them, and a later sweep deletes them once
  retain_until has passed. They carry user_id with no foreign key, so
  nothing cascades into them.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.safety import SAFETY_RETAINED_TABLES, SafetyEvent, SafetyHold
from app.models.user import User

logger = logging.getLogger(__name__)

RETENTION_DAYS = 30
KEEP_NUMBER_TABLES = {"founding_ledger"}
# Three years: the limitation period for a personal injury claim in England
# and Wales runs three years from the injury (or from when the rider knew of
# it). The privacy policy has to state the same period.
SAFETY_RETENTION_YEARS = 3
# For a minor the limitation clock only starts at 18, so a claim can come up
# to their 21st birthday (Limitation Act 1980 s.28). An account flagged as a
# minor keeps its safety records until then if that is later than three
# years after the purge.
ADULT_AGE = 18
MINOR_CLAIMS_UNTIL_AGE = ADULT_AGE + SAFETY_RETENTION_YEARS

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
        if t != "users" and t not in KEEP_NUMBER_TABLES and t not in SAFETY_RETAINED_TABLES
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


def _retained(owned: set[str], columns: set[tuple[str, str]]) -> list[str]:
    """The safety tables present in this database that the purge stamps
    rather than deletes."""
    return sorted(
        t for t in SAFETY_RETAINED_TABLES
        if t in owned and (t, "subject_deleted_at") in columns
    )


def _stamp_retained(
    db: Session,
    tables: list[str],
    user_id: str,
    now: datetime,
    until: datetime,
    columns: set[tuple[str, str]],
) -> None:
    """Mark this rider's safety records as belonging to a deleted account,
    and say when they may go. A row already stamped keeps its first dates."""
    for table in tables:
        sets = "subject_deleted_at = :now"
        if (table, "retain_until") in columns:
            sets += ", retain_until = :until"
        db.execute(
            text(
                f"UPDATE {_q(table)} SET {sets} "
                "WHERE user_id = :uid AND subject_deleted_at IS NULL"
            ),
            {"uid": user_id, "now": now, "until": until},
        )


def _years_after(moment, years: int):
    """The same day `years` on. 29 February lands on 1 March in a year
    without one: the later of the two, so a record is never let go early."""
    try:
        return moment.replace(year=moment.year + years)
    except ValueError:
        return moment.replace(year=moment.year + years, month=3, day=1)


def _as_date(value) -> date | None:
    """A date column read back as a date, whatever the driver returned."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def flagged_as_minor(
    db: Session,
    user_id: str,
    born: date | None,
    opened: datetime | None,
    owned: set[str],
) -> bool:
    """Whether this account was ever flagged as a minor: a minor hold or a
    minor red flag (from the detector, the coach or a review), or a date of
    birth that made them under 18 when the account was opened. Lifted holds
    count too: the flag is about who the rider may have been, not about what
    they may ride today."""
    if "safety_holds" in owned and db.query(SafetyHold.id).filter(
        SafetyHold.user_id == user_id, SafetyHold.red_flag == "minor"
    ).first() is not None:
        return True
    if "safety_events" in owned and db.query(SafetyEvent.id).filter(
        SafetyEvent.user_id == user_id, SafetyEvent.kind == "minor"
    ).first() is not None:
        return True
    if born is not None and opened is not None:
        return _years_after(born, ADULT_AGE) > _as_date(opened)
    return False


def _kept_through(value) -> datetime | None:
    """A retention date as the moment the sweep may delete from. A plain
    date is read as the last day kept (a 21st birthday), so the records go
    from the start of the day after: read either way, never early."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            # The stamps are naive UTC, like every other time in these tables.
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime.combine(value + timedelta(days=1), time.min)
    return None


def _flagged_ends(
    db: Session, user_id: str, owned: set[str], now: datetime
) -> datetime | None:
    """When the under-18 flags on this account let the records go, from
    safety_service.minor_retention_end: through the 21st birthday of the age
    the rider told us, or with no age to go on, of the youngest rider the
    flag could mean (a hold Forma put on by hand with --force included). A
    minor who signed up must have given a false adult date of birth, so the
    flag's own age is the one that counts. None when no flag gives a date."""
    if not {"safety_events", "safety_holds"} <= owned:
        return None
    from app.services import safety_service

    return _kept_through(safety_service.minor_retention_end(db, user_id, now))


def retention_end(
    db: Session, user_id: str, now: datetime, owned: set[str] | None = None
) -> datetime:
    """When this rider's safety records may be deleted: three years after
    the purge, or, for an account flagged as a minor, through their 21st
    birthday if that is later. The birthday comes from each under-18 flag
    (safety_service.minor_retention_end: the age they told us, or the
    youngest plausible age when none was given) and from the date of birth
    on file; the latest date wins. Read before the users row goes, because
    the date of birth goes with it."""
    until = _years_after(now, SAFETY_RETENTION_YEARS)
    owned = owned if owned is not None else set(SAFETY_RETAINED_TABLES)
    row = (
        db.query(User.date_of_birth, User.created_at).filter(User.id == user_id).first()
    )
    born = _as_date(row[0]) if row is not None else None
    opened = row[1] if row is not None else None
    if not flagged_as_minor(db, user_id, born, opened, owned):
        return until
    ends = [until]
    flagged = _flagged_ends(db, user_id, owned, now)
    if flagged is not None:
        ends.append(flagged)
    if born is not None:
        # Kept through the 21st birthday: deletable from the start of the day after.
        ends.append(_kept_through(_years_after(born, MINOR_CLAIMS_UNTIL_AGE)))
    return max(ends)


def purge_user(db: Session, user_id: str) -> None:
    """Remove every row this rider owns, then the rider. One transaction:
    either the whole account goes or none of it does. The safety records
    stay, stamped with subject_deleted_at and retain_until."""
    p = {"uid": user_id}
    owned, fks, columns = _schema(db)

    now = datetime.utcnow()
    retained = _retained(owned, columns)
    until = retention_end(db, user_id, now, owned)
    missing = [t for t in retained if (t, "retain_until") not in columns]
    if missing and until > _years_after(now, SAFETY_RETENTION_YEARS):
        # Without the column the date of birth, and so the later date, would
        # be lost with the users row. Leave the account for the next run.
        raise RuntimeError(
            "Purge of a minor's account needs retain_until on "
            + ", ".join(missing)
            + "; run the migrations first"
        )
    _stamp_retained(db, retained, user_id, now, until, columns)
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


def _years_before(moment: datetime, years: int) -> datetime:
    try:
        return moment.replace(year=moment.year - years)
    except ValueError:  # 29 February, in a year without one
        return moment.replace(year=moment.year - years, day=28)


def purge_expired_safety_records(
    db: Session,
    years: int = SAFETY_RETENTION_YEARS,
    commit: bool = True,
    now: datetime | None = None,
) -> int:
    """The second pass: delete safety records whose retain_until has passed.
    A row stamped before retain_until existed goes `years` after its
    subject_deleted_at. Rows of riders who still have an account (no
    subject_deleted_at) are never touched. Returns how many rows went, or
    would go on a dry run.

    Plain SQL on purpose: the ORM guard that keeps consent_events
    append-only is for application code, and this is the one deletion the
    retention policy allows."""
    now = now or datetime.utcnow()
    params = {"now": now, "cutoff": _years_before(now, years)}
    # Read the schema through the session's own connection, all before the
    # first delete: an inspector on the engine checks a connection out and
    # back in, and on a shared connection that rolls the deletes back.
    insp = inspect(db.connection())
    tables = {
        table: any(c["name"] == "retain_until" for c in insp.get_columns(table))
        for table in sorted(SAFETY_RETAINED_TABLES & set(insp.get_table_names()))
    }
    total = 0
    for table, has_until in tables.items():
        if has_until:
            clause = (
                "subject_deleted_at IS NOT NULL AND ("
                "(retain_until IS NOT NULL AND retain_until < :now) OR "
                "(retain_until IS NULL AND subject_deleted_at < :cutoff))"
            )
        else:
            clause = "subject_deleted_at IS NOT NULL AND subject_deleted_at < :cutoff"
        if not commit:
            n = db.execute(
                text(f"SELECT COUNT(*) FROM {_q(table)} WHERE {clause}"), params
            ).scalar() or 0
            if n:
                logger.info("[dry run] would delete %d expired row(s) from %s", n, table)
        else:
            n = db.execute(text(f"DELETE FROM {_q(table)} WHERE {clause}"), params).rowcount or 0
        total += n
    if commit:
        db.commit()
    return total


def purge_expired_accounts(db: Session, days: int = RETENTION_DAYS, commit: bool = True) -> int:
    """Purge every account soft-deleted more than `days` ago. Each account
    commits on its own, so one failure never holds up the rest. Then sweep
    the safety records whose retention period has run."""
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

    try:
        swept = purge_expired_safety_records(db, commit=commit)
        if swept and commit:
            logger.info("Deleted %d safety record(s) past retention", swept)
    except Exception:
        db.rollback()
        logger.exception("Safety record retention sweep failed")
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
