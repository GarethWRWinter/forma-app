"""Safety endpoints: the gate state the app reads, clearance, the fever
lift, the head-injury lift, "this was a mistake", acknowledgements, and what
the trainer was told at session start.

Deliberately behind get_current_user only, never the paywall: a lapsed
membership must not hide a hold or block a clearance. While the account is
held as under 18, all four ways out refuse with MINOR_REFUSAL.
"""

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user
from app.core.exceptions import BadRequestException, ConflictException, NotFoundException
from app.core.ratelimit import rate_limit
from app.database import get_db
from app.models.safety import RideSessionStart, SafetyHold
from app.models.user import User
from app.services import safety_service
from app.services.plan_service import sync_hold_marks

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users/me", tags=["safety"])

FORMA_EMAIL = safety_service.FORMA_EMAIL

# An account held as under 18 is closed by Forma, never lifted by the rider.
MINOR_REFUSAL = (
    "This account is on hold because Forma is for adults, 18 and over, so it "
    f"can't be lifted here. If you think that's wrong, email {FORMA_EMAIL}."
)

# Holds the rider can't wave away as a mistake: their own screening answers
# (they update those in Settings, then Health), holds set by hand, and the
# easy week after a fever they told us had gone.
_NOT_A_MISTAKE = {
    "screening": (
        "This hold comes from your health answers, so it can't be cleared here. "
        "If a doctor has cleared you, use the I've been cleared button. If an answer "
        "was wrong, update it in Settings, then Health."
    ),
    "admin": (
        "This hold was set by hand, so it can't be cleared here. If you think it's "
        f"wrong, email {FORMA_EMAIL}."
    ),
}


def _long_date(value: datetime) -> str:
    return f"{value.day} {value:%B}"


def _refusal(hold: SafetyHold, how: str) -> str:
    """Why the rider can't lift this hold this way, in plain words."""
    kind = safety_service.lift_kind(hold)
    if hold.red_flag == "minor":
        return MINOR_REFUSAL
    if kind == "admin_only":
        return _NOT_A_MISTAKE["admin"]
    if how == "mistake" and hold.source == "screening":
        return _NOT_A_MISTAKE["screening"]
    if kind == "expires":
        if hold.red_flag == "head_injury":
            return (
                "This easy riding follows your head injury, so it can't be cleared "
                f"here. It ends by itself on {_long_date(hold.expires_at)}."
            )
        return (
            "This easy week follows the fever you told me had gone, so it can't be "
            f"cleared here. It ends by itself on {_long_date(hold.expires_at)}."
        )
    if how == "fever_self":
        return "This hold isn't for a fever, so it can't be lifted that way."
    if how == "head_clearance":
        return "This hold isn't for a head injury, so it can't be lifted that way."
    return "This hold can't be lifted that way."


class ClearanceBody(BaseModel):
    by: str = Field(min_length=1, max_length=200)
    limits: str | None = Field(default=None, max_length=2000)


class MistakeBody(BaseModel):
    hold_id: str = Field(min_length=1, max_length=36)


class FeverLiftBody(BaseModel):
    hold_id: str = Field(min_length=1, max_length=36)


class HeadInjuryLiftBody(BaseModel):
    hold_id: str = Field(min_length=1, max_length=36)
    by: str = Field(min_length=1, max_length=200)
    limits: str | None = Field(default=None, max_length=2000)


class AcknowledgementBody(BaseModel):
    kind: Literal["ride_mode"]
    text_shown: str = Field(min_length=1, max_length=5000)


class RideSessionStartBody(BaseModel):
    workout_id: str | None = Field(default=None, max_length=36)
    steps_hash: str = Field(min_length=1, max_length=64)
    ftp: int = Field(ge=1, le=2000)
    erg: bool
    max_target_watts: int | None = Field(default=None, ge=0, le=5000)


@router.get("/safety-state")
def get_safety_state(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return safety_service.safety_state(db, current_user)


@router.post("/safety/clearance", dependencies=[Depends(rate_limit(20, 3600))])
def confirm_clearance(
    body: ClearanceBody,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not body.by.strip():
        raise BadRequestException(detail="Tell me who cleared you, for example your GP.")
    if safety_service.minor_hold(db, current_user) is not None:
        raise BadRequestException(detail=MINOR_REFUSAL)
    try:
        safety_service.confirm_clearance(db, current_user, body.by, body.limits, request=request)
    except safety_service.ClearanceOutOfScope as refused:
        db.rollback()
        raise BadRequestException(detail=str(refused))
    except safety_service.NothingToClear as refused:
        # Nothing a clearance can lift: no consent row, nothing stamped.
        db.rollback()
        raise ConflictException(detail=str(refused))
    except safety_service.HoldNotLiftable as refused:
        db.rollback()
        logger.warning(
            "Clearance refused for rider %s: %s hold %s",
            current_user.id, refused.lift_kind, refused.hold.id,
        )
        raise BadRequestException(detail=_refusal(refused.hold, "clearance"))
    # Take the on-hold label off the planned sessions now the hold is lifted.
    sync_hold_marks(db, current_user.id)
    logger.info("Clearance declared by rider %s", current_user.id)
    return safety_service.safety_state(db, current_user)


@router.post("/safety/fever-lift", dependencies=[Depends(rate_limit(20, 3600))])
def lift_fever(
    body: FeverLiftBody,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The rider declares their fever gone. No doctor is named; the hold
    becomes a week of easy riding that ends by itself."""
    try:
        hold = safety_service.lift_fever(db, current_user, body.hold_id, request=request)
    except safety_service.HoldNotLiftable as refused:
        db.rollback()
        raise BadRequestException(detail=_refusal(refused.hold, "fever_self"))
    if hold is None:
        raise NotFoundException(detail="That hold wasn't found.")
    sync_hold_marks(db, current_user.id)
    logger.info("Rider %s declared their fever gone (hold %s)", current_user.id, body.hold_id)
    return safety_service.safety_state(db, current_user)


@router.post("/safety/head-injury-lift", dependencies=[Depends(rate_limit(20, 3600))])
def lift_head_injury(
    body: HeadInjuryLiftBody,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The rider declares a doctor has checked them since they hit their
    head and they've had no symptoms for 24 hours. Every head-injury hold
    lifts and becomes easy riding until two weeks after the latest injury,
    and racing and group riding stay off until its day 21
    (safety_state.no_racing_or_group_until). A second injury during the
    build-back counts from its own day; the first one mentioned again counts
    from the first."""
    if not body.by.strip():
        raise BadRequestException(detail="Tell me which doctor checked you, for example your GP.")
    try:
        hold = safety_service.lift_head_injury(
            db, current_user, body.hold_id, body.by, body.limits, request=request
        )
    except safety_service.ClearanceOutOfScope as refused:
        db.rollback()
        raise BadRequestException(detail=str(refused))
    except safety_service.HoldNotLiftable as refused:
        db.rollback()
        raise BadRequestException(detail=_refusal(refused.hold, "head_clearance"))
    if hold is None:
        raise NotFoundException(detail="That hold wasn't found.")
    sync_hold_marks(db, current_user.id)
    logger.info("Rider %s declared a head injury checked (hold %s)", current_user.id, body.hold_id)
    return safety_service.safety_state(db, current_user)


@router.post("/safety/mistake", dependencies=[Depends(rate_limit(20, 3600))])
def mark_mistake(
    body: MistakeBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The rider says the detector misread them. Lifts that one hold only:
    the same fever or head injury mentioned again during its easy riding
    leaves the easy days running, and a second head injury marked a mistake
    leaves the first one's hold, easy days and racing date as they were.
    Refused outright while the account is held as under 18, like every other
    way out."""
    if safety_service.minor_hold(db, current_user) is not None:
        raise BadRequestException(detail=MINOR_REFUSAL)
    hold = (
        db.query(SafetyHold)
        .filter(SafetyHold.id == body.hold_id, SafetyHold.user_id == current_user.id)
        .first()
    )
    if hold is None:
        raise NotFoundException(detail="That hold wasn't found.")
    in_force = hold.lifted_at is None and (
        hold.expires_at is None or hold.expires_at > datetime.utcnow()
    )
    if in_force and not safety_service.can_lift(hold, "mistake"):
        raise BadRequestException(detail=_refusal(hold, "mistake"))
    try:
        safety_service.lift_hold(
            db, current_user, hold.id, "mistake", note="Rider marked it a mistake."
        )
    except safety_service.HoldNotLiftable as refused:
        db.rollback()
        raise BadRequestException(detail=_refusal(refused.hold, "mistake"))
    sync_hold_marks(db, current_user.id)
    logger.info("Rider %s marked hold %s a mistake (%s)", current_user.id, hold.id, hold.red_flag)
    return safety_service.safety_state(db, current_user)


@router.post("/acknowledgements", dependencies=[Depends(rate_limit(20, 3600))])
def acknowledge(
    body: AcknowledgementBody,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not body.text_shown.strip():
        raise BadRequestException(detail="The acknowledgement needs the text that was shown.")
    safety_service.record_consent(
        db, current_user, body.kind, safety_service.RIDE_MODE_VERSION, body.text_shown,
        request=request, source="ride_mode", commit=False,
    )
    current_user.ride_mode_ack_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.post("/ride-session-starts", dependencies=[Depends(rate_limit(60, 3600))])
def record_ride_session_start(
    body: RideSessionStartBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.add(RideSessionStart(user_id=current_user.id, **body.model_dump()))
    db.commit()
    return {"ok": True}
