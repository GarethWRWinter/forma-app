"""GDPR data-subject rights: export (Article 20) and erasure (Article 17).

Export returns a JSON archive of the personal data we hold. Deletion is a
soft-delete: the account is locked out immediately and its integration tokens
are removed (so third-party processing stops at once), then a scheduled purge
(`scripts/purge_deleted_accounts.py`) hard-deletes everything after the
retention window. Per the PRD, account erasure removes *everything* — the
"retain hidden memory" rule applies only to hide-not-delete within a live
account, not to account deletion. The one exception is the safety records
(SAFETY_RETAINED_TABLES): the purge stamps them and keeps them for legal
claims, three years after the purge or, for an account flagged as a minor,
until the rider's 21st birthday if that is later, counted from the age they
told us (SafetyEvent.stated_age) or the date of birth on file, whichever runs
later (purge_service, safety_service.minor_retention_until), as the privacy
policy says.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.briefing import Briefing
from app.models.chat import ChatMessage, ChatSession
from app.models.chat_attachment import ChatAttachment
from app.models.coach import CoachNudge
from app.models.coach_initiative import CoachInitiative
from app.models.dossier import DossierEntry
from app.models.forma_call import FormaCall
from app.models.founding import FoundingLedger
from app.models.integration import DropboxToken, StravaToken, TrainingPeaksToken, WahooToken
from app.models.memory import MemoryEdge, MemoryEntity
from app.models.metrics import DailyMetrics
from app.models.onboarding import GoalEvent, OnboardingResponse
from app.models.outreach import OutreachLog
from app.models.plan_proposal import PlanProposal
from app.models.ride import Ride
from app.models.safety import (
    ClearanceLimit,
    ConsentEvent,
    HealthScreening,
    RideSessionStart,
    SafetyEvent,
    SafetyHold,
)
from app.models.segment import SegmentEffort
from app.models.training import TrainingPhase, TrainingPlan, Workout, WorkoutStep
from app.models.user import User
from app.models.waitlist import WaitlistEntry, WaitlistReply
from app.services import token_service

# Never export secret material (encrypted, but still — no reason to hand it out).
_SECRET_COLUMNS = {"hashed_password", "access_token", "refresh_token"}
# Uploaded files themselves: the archive is JSON, and the rider has the
# originals. Their names, summaries and our analysis are exported.
_FILE_COLUMNS = {"raw_file"}

# Tables holding the rider's data that the archive leaves out, and why. Every
# other table that holds a rider's rows must be in the archive: the data
# request test (tests/test_purge_retention.py) fails on a table in neither.
NOT_EXPORTED = {
    "refresh_tokens": "sign-in session secrets",
    "strava_tokens": "integration access tokens",
    "dropbox_tokens": "integration access tokens",
    "trainingpeaks_tokens": "integration access tokens",
    "wahoo_tokens": "integration access tokens",
    "ride_data": "per-second telemetry, sent separately on request",
}


def _row_to_dict(obj) -> dict:
    out = {}
    for col in obj.__table__.columns:
        if col.name in _SECRET_COLUMNS or col.name in _FILE_COLUMNS:
            continue
        val = getattr(obj, col.name)
        if isinstance(val, (datetime, date)):
            val = val.isoformat()
        elif isinstance(val, (bytes, bytearray, memoryview)):
            continue
        out[col.name] = val
    return out


def _rows(db: Session, model, user_id: str) -> list[dict]:
    return [_row_to_dict(r) for r in db.query(model).filter(model.user_id == user_id).all()]


def _child_rows(db: Session, model, fk, parent_ids: list[str]) -> list[dict]:
    """Rows of a table that has no user_id — scoped via a parent's ids."""
    if not parent_ids:
        return []
    return [_row_to_dict(r) for r in db.query(model).filter(fk.in_(parent_ids)).all()]


def export_user_data(db: Session, user: User) -> dict:
    """A portable JSON archive of the rider's personal data, which also
    answers a subject access request.

    Excludes secrets, uploaded files and the high-volume per-second ride
    telemetry (NOT_EXPORTED); everything else we hold on the rider is
    included, the safety records among it: health answers, consent, holds,
    red-flag events with the rider's words and the coach's reply, the limits
    a doctor set, and what the trainer was told at each ride start.
    """
    plan_ids = [p.id for p in db.query(TrainingPlan.id).filter(TrainingPlan.user_id == user.id)]
    session_ids = [s.id for s in db.query(ChatSession.id).filter(ChatSession.user_id == user.id)]
    workout_ids = [w.id for w in db.query(Workout.id).filter(Workout.user_id == user.id)]
    ride_ids = [r.id for r in db.query(Ride.id).filter(Ride.user_id == user.id)]
    # The waitlist knew the rider by email before the account existed.
    waitlist = (
        db.query(WaitlistEntry)
        .filter(func.lower(WaitlistEntry.email) == (user.email or "").strip().lower())
        .all()
    )
    return {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "account": _row_to_dict(user),
        "founding_number": _rows(db, FoundingLedger, user.id),
        "onboarding_responses": _rows(db, OnboardingResponse, user.id),
        "goals": _rows(db, GoalEvent, user.id),
        "rides": _rows(db, Ride, user.id),  # summaries; per-second streams via FIT export
        "segment_efforts": _child_rows(db, SegmentEffort, SegmentEffort.ride_id, ride_ids),
        "training_plans": _rows(db, TrainingPlan, user.id),
        "training_phases": _child_rows(db, TrainingPhase, TrainingPhase.plan_id, plan_ids),
        "workouts": _rows(db, Workout, user.id),
        "workout_steps": _child_rows(db, WorkoutStep, WorkoutStep.workout_id, workout_ids),
        "plan_proposals": _rows(db, PlanProposal, user.id),
        "daily_metrics": _rows(db, DailyMetrics, user.id),
        "chat_sessions": _rows(db, ChatSession, user.id),
        "chat_messages": _child_rows(db, ChatMessage, ChatMessage.session_id, session_ids),
        "chat_attachments": _rows(db, ChatAttachment, user.id),
        "coach_nudges": _rows(db, CoachNudge, user.id),
        "coach_initiatives": _rows(db, CoachInitiative, user.id),
        "coach_usage": _rows(db, FormaCall, user.id),
        "coach_dossier": _rows(db, DossierEntry, user.id),
        "briefings": _rows(db, Briefing, user.id),
        "emails_sent": _rows(db, OutreachLog, user.id),
        "memory_entities": _rows(db, MemoryEntity, user.id),
        "memory_edges": _rows(db, MemoryEdge, user.id),
        "health_screenings": _rows(db, HealthScreening, user.id),
        "consent_events": _rows(db, ConsentEvent, user.id),
        "safety_holds": _rows(db, SafetyHold, user.id),
        "safety_events": _rows(db, SafetyEvent, user.id),
        "clearance_limits": _rows(db, ClearanceLimit, user.id),
        "ride_session_starts": _rows(db, RideSessionStart, user.id),
        "waitlist": [_row_to_dict(w) for w in waitlist],
        "waitlist_replies": _child_rows(
            db, WaitlistReply, WaitlistReply.waitlist_entry_id, [w.id for w in waitlist]
        ),
        "note": (
            "Per-second ride telemetry, original FIT files and the files you "
            "uploaded to the coach are not included in this archive; ask for "
            "them separately. Integration access tokens are left out for "
            "security."
        ),
    }


# The archive key for each table, for the data request test.
EXPORTED_TABLES = {
    "users": "account",
    "founding_ledger": "founding_number",
    "onboarding_responses": "onboarding_responses",
    "goal_events": "goals",
    "rides": "rides",
    "segment_efforts": "segment_efforts",
    "training_plans": "training_plans",
    "training_phases": "training_phases",
    "workouts": "workouts",
    "workout_steps": "workout_steps",
    "plan_proposals": "plan_proposals",
    "daily_metrics": "daily_metrics",
    "chat_sessions": "chat_sessions",
    "chat_messages": "chat_messages",
    "chat_attachments": "chat_attachments",
    "coach_nudges": "coach_nudges",
    "coach_initiatives": "coach_initiatives",
    "forma_calls": "coach_usage",
    "dossier_entries": "coach_dossier",
    "briefings": "briefings",
    "outreach_log": "emails_sent",
    "waitlist": "waitlist",
    "waitlist_replies": "waitlist_replies",
    "mem_entities": "memory_entities",
    "mem_edges": "memory_edges",
    "health_screenings": "health_screenings",
    "consent_events": "consent_events",
    "safety_holds": "safety_holds",
    "safety_events": "safety_events",
    "clearance_limits": "clearance_limits",
    "ride_session_starts": "ride_session_starts",
}


def delete_account(db: Session, user: User) -> None:
    """GDPR erasure request. Soft-delete now (lock out + stop third-party
    processing); the retention-window purge finishes the job. Idempotent."""
    user.is_active = False
    if user.deleted_at is None:
        user.deleted_at = datetime.utcnow()

    # Kill all sessions and cut off external data access immediately.
    token_service.revoke_all_for_user(db, user.id)
    for model in (StravaToken, DropboxToken, TrainingPeaksToken, WahooToken):
        db.query(model).filter(model.user_id == user.id).delete()

    db.commit()
