"""Safety records: holds, red-flag events, consent, health screening, and
what the trainer was told at the start of each ride.

Holds, events, consent, screenings and clearance limits are kept for legal
claims. They carry user_id with no foreign key, so deleting an account never
cascades into them; the purge stamps subject_deleted_at and retain_until
instead of removing the rows (SAFETY_RETAINED_TABLES is the list it reads). ride_session_starts
is ordinary rider data and goes with the account like everything else.

Values are short strings rather than database enums: a new red-flag key or
consent kind should never need a migration.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, event, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, generate_uuid

# Kept through an account purge (subject_deleted_at is stamped instead).
SAFETY_RETAINED_TABLES = frozenset(
    {
        "safety_holds",
        "safety_events",
        "consent_events",
        "health_screenings",
        "clearance_limits",
    }
)

# jsonb on Postgres, plain JSON on the SQLite test database.
_JSON = JSON().with_variant(JSONB(), "postgresql")


class SafetyHold(Base):
    """A brake on what the plan, the coach and ride mode may prescribe.

    level: "easy_only" (recovery and endurance only) | "hold_all" (nothing).
    source: "screening" | "detector" | "coach_tool" | "layoff" | "admin" |
    "profile" (an under-18 date of birth given on the account).
    lifted_how: "clearance" | "mistake" | "admin" | "superseded" |
    "fever_self" | "head_clearance" | "expired".
    expires_at: set only on a hold that ends by itself, such as the easy week
    after a fever or the two weeks of easy riding after a head injury. Past
    it, the hold no longer counts, even before anything stamps lifted_at.
    """

    __tablename__ = "safety_holds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    red_flag: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lifted_how: Mapped[str | None] = mapped_column(String(20), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    subject_deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set by the purge: the sweep deletes the row after this (three years
    # from deletion, or the 21st birthday of an account held as a minor).
    retain_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SafetyEvent(Base):
    """One red-flag hit: what matched, what the rider was shown, whether
    Gareth was alerted, and his review."""

    __tablename__ = "safety_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    # Red-flag key: chest_pain, fainting, head_injury, fever, medication,
    # restriction, minor, crisis, ...
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    matched: Mapped[str] = mapped_column(String(200), nullable=False)
    card_shown: Mapped[str | None] = mapped_column(String(20), nullable=True)
    hold_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    founder_alerted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The exchange itself, kept with the record: chat_messages go with the
    # account, so without these only `matched` would survive a purge.
    rider_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    coach_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The age an under-18 rider told us ("I'm 15"), when they gave one. It
    # decides how long the records are kept (safety_service.
    # minor_retention_until): a real minor can only have joined with a false
    # adult date of birth, so the age they said is the one that counts.
    stated_age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    subject_deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set by the purge: the sweep deletes the row after this (three years
    # from deletion, or the 21st birthday of an account held as a minor).
    retain_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ConsentEvent(Base):
    """Append-only proof of what a rider agreed to, word for word, and when.

    kind: "terms" | "health_data" | "age" | "ride_mode" | "screening" |
    "clearance" | "reaccept". Never updated: a new agreement is a new row.
    """

    __tablename__ = "consent_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    doc_version: Mapped[str] = mapped_column(String(40), nullable=False)
    text_shown: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    app_build: Mapped[str | None] = mapped_column(String(40), nullable=True)
    subject_deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set by the purge: the sweep deletes the row after this (three years
    # from deletion, or the 21st birthday of an account held as a minor).
    retain_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


@event.listens_for(ConsentEvent, "before_update")
def _consent_is_append_only(mapper, connection, target) -> None:
    # The one permitted change is the purge marking the rider as gone.
    state = inspect(target)
    changed = {a.key for a in state.attrs if a.history.has_changes()}
    if changed - {"subject_deleted_at", "retain_until"}:
        raise ValueError("consent_events is append-only; record a new row instead")


@event.listens_for(ConsentEvent, "before_delete")
def _consent_is_never_deleted(mapper, connection, target) -> None:
    raise ValueError("consent_events is append-only; rows are never deleted")


class HealthScreening(Base):
    """The rider's answers to the eight health questions, and any clearance
    they later declare. A re-screen supersedes the previous row.

    tier: "none" | "easy_only" | "hold_all".
    """

    __tablename__ = "health_screenings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    answers: Mapped[dict] = mapped_column(_JSON, nullable=False)
    long_break: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    any_yes: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tier: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    clearance_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    clearance_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    clearance_limits: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    subject_deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set by the purge: the sweep deletes the row after this (three years
    # from deletion, or the 21st birthday of an account held as a minor).
    retain_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ClearanceLimit(Base):
    """What a doctor, midwife or physio told the rider to avoid, recorded when
    they declared clearance. The coach treats every active one as a hard
    constraint.

    Kept on its own, not on the screening, so nothing that happens later can
    lose it: a re-screen writes a new screening row, a second clearance may
    come with no limits, and a rider held from chat may never have screened.
    Append-only: a new clearance adds a row. The one change allowed is an
    admin retiring a limit (retired_at, retired_note), plus the purge stamp.
    """

    __tablename__ = "clearance_limits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    limits: Mapped[str] = mapped_column(Text, nullable=False)
    cleared_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # What the clearance lifted, for the record: hold reasons or screening.
    cleared_for: Mapped[str | None] = mapped_column(Text, nullable=True)
    consent_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retired_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject_deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set by the purge: the sweep deletes the row after this (three years
    # from deletion, or the 21st birthday of an account held as a minor).
    retain_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


_LIMIT_MAY_CHANGE = {"retired_at", "retired_note", "subject_deleted_at", "retain_until"}


@event.listens_for(ClearanceLimit, "before_update")
def _limits_are_append_only(mapper, connection, target) -> None:
    state = inspect(target)
    changed = {a.key for a in state.attrs if a.history.has_changes()}
    if changed - _LIMIT_MAY_CHANGE:
        raise ValueError("clearance_limits is append-only; record a new row instead")


@event.listens_for(ClearanceLimit, "before_delete")
def _limits_are_never_deleted(mapper, connection, target) -> None:
    raise ValueError("clearance_limits is append-only; retire a limit instead")


class RideSessionStart(Base):
    """What the trainer was told when a ride-mode session began: the steps
    (as a hash), the FTP they were scaled from, ERG on or off, and the
    highest target. Ordinary rider data."""

    __tablename__ = "ride_session_starts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    workout_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    steps_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ftp: Mapped[int] = mapped_column(Integer, nullable=False)
    erg: Mapped[bool] = mapped_column(Boolean, nullable=False)
    max_target_watts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
