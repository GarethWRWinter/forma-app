"""GDPR data-subject rights: export (Article 20) and erasure (Article 17).

Export returns a JSON archive of the personal data we hold. Deletion is a
soft-delete: the account is locked out immediately and its integration tokens
are removed (so third-party processing stops at once), then a scheduled purge
(`scripts/purge_deleted_accounts.py`) hard-deletes everything after the
retention window. Per the PRD, account erasure removes *everything* — the
"retain hidden memory" rule applies only to hide-not-delete within a live
account, not to account deletion.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.chat import ChatMessage, ChatSession
from app.models.coach import CoachNudge
from app.models.integration import DropboxToken, StravaToken, TrainingPeaksToken, WahooToken
from app.models.memory import MemoryEdge, MemoryEntity
from app.models.metrics import DailyMetrics
from app.models.onboarding import GoalEvent, OnboardingResponse
from app.models.ride import Ride
from app.models.training import TrainingPhase, TrainingPlan, Workout
from app.models.user import User
from app.services import token_service

# Never export secret material (encrypted, but still — no reason to hand it out).
_SECRET_COLUMNS = {"hashed_password", "access_token", "refresh_token"}


def _row_to_dict(obj) -> dict:
    out = {}
    for col in obj.__table__.columns:
        if col.name in _SECRET_COLUMNS:
            continue
        val = getattr(obj, col.name)
        out[col.name] = val.isoformat() if isinstance(val, datetime) else val
    return out


def _rows(db: Session, model, user_id: str) -> list[dict]:
    return [_row_to_dict(r) for r in db.query(model).filter(model.user_id == user_id).all()]


def _child_rows(db: Session, model, fk, parent_ids: list[str]) -> list[dict]:
    """Rows of a table that has no user_id — scoped via a parent's ids."""
    if not parent_ids:
        return []
    return [_row_to_dict(r) for r in db.query(model).filter(fk.in_(parent_ids)).all()]


def export_user_data(db: Session, user: User) -> dict:
    """A portable JSON archive of the rider's personal data.

    Excludes secrets and the high-volume per-second ride telemetry (available
    as the original FIT files); everything else the rider gave us is included.
    """
    plan_ids = [p.id for p in db.query(TrainingPlan.id).filter(TrainingPlan.user_id == user.id)]
    session_ids = [s.id for s in db.query(ChatSession.id).filter(ChatSession.user_id == user.id)]
    return {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "account": _row_to_dict(user),
        "onboarding_responses": _rows(db, OnboardingResponse, user.id),
        "goals": _rows(db, GoalEvent, user.id),
        "rides": _rows(db, Ride, user.id),  # summaries; per-second streams via FIT export
        "training_plans": _rows(db, TrainingPlan, user.id),
        "training_phases": _child_rows(db, TrainingPhase, TrainingPhase.plan_id, plan_ids),
        "workouts": _rows(db, Workout, user.id),
        "daily_metrics": _rows(db, DailyMetrics, user.id),
        "chat_sessions": _rows(db, ChatSession, user.id),
        "chat_messages": _child_rows(db, ChatMessage, ChatMessage.session_id, session_ids),
        "coach_nudges": _rows(db, CoachNudge, user.id),
        "memory_entities": _rows(db, MemoryEntity, user.id),
        "memory_edges": _rows(db, MemoryEdge, user.id),
        "note": (
            "Per-second ride telemetry and original FIT files are not included "
            "in this archive; request them separately. Integration access "
            "tokens are omitted for security."
        ),
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
