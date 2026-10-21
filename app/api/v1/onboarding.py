"""Onboarding quiz and health screening API endpoints.

Behind get_current_user only, never the paywall: the health answers drive
the safety gate, so a rider must always be able to give or update them.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user
from app.core.exceptions import BadRequestException, ForbiddenException
from app.core.ratelimit import rate_limit
from app.database import get_db
from app.models.user import User
from app.schemas.onboarding import (
    OnboardingQuizRequest,
    OnboardingResponse,
    OnboardingStatusResponse,
    ScreeningRecord,
    ScreeningResult,
    ScreeningSubmit,
)
from app.services.onboarding_service import (
    MINOR_SCREENING_REFUSAL,
    ScreeningRefused,
    get_onboarding_response,
    get_onboarding_status,
    get_screening,
    screening_closed,
    submit_quiz,
    submit_screening,
)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


@router.post("/quiz", response_model=OnboardingResponse, status_code=201)
def submit_onboarding_quiz(
    body: OnboardingQuizRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Submit onboarding quiz answers. Replaces previous answers if re-submitted."""
    try:
        response = submit_quiz(
            db,
            current_user.id,
            primary_goal=body.primary_goal,
            secondary_goals=body.secondary_goals,
            current_weekly_volume_hours=body.current_weekly_volume_hours,
            years_cycling=body.years_cycling,
            indoor_outdoor_preference=body.indoor_outdoor_preference,
        )
    except ValueError as e:
        raise BadRequestException(detail=str(e))

    return response


@router.get("/status", response_model=OnboardingStatusResponse)
def check_onboarding_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check if onboarding is complete."""
    status = get_onboarding_status(db, current_user.id)
    return OnboardingStatusResponse(**status)


@router.get("/response", response_model=OnboardingResponse)
def get_quiz_response(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get the full onboarding response (quiz answers)."""
    response = get_onboarding_response(db, current_user.id)
    if not response:
        raise BadRequestException(detail="Onboarding not completed yet")
    return response


HEALTH_CONSENT_FIRST = (
    "Before the health questions, tick the box that lets Forma use your health "
    "details. Refresh the page to see it."
)


@router.post(
    "/screening",
    response_model=ScreeningResult,
    dependencies=[Depends(rate_limit(30, 3600))],
)
def submit_health_screening(
    body: ScreeningSubmit,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The eight health questions (and the long-break question). Stores the
    answers, opens a hold for any yes, records what the rider was shown, and
    returns the message for their answers with the new safety state.

    Refused with 403, storing nothing, while the account is held as under
    18: no health data is taken from a child. Otherwise refused until the
    rider has given box 2, the explicit consent to use their health details
    (POST /auth/health-consent for an account made before it was asked)."""
    if screening_closed(db, current_user):
        raise ForbiddenException(detail=MINOR_SCREENING_REFUSAL)
    if getattr(current_user, "health_consent_at", None) is None:
        raise BadRequestException(detail=HEALTH_CONSENT_FIRST)
    try:
        return submit_screening(
            db, current_user, body.answers.model_dump(), body.long_break, request=request
        )
    except ScreeningRefused as refused:
        db.rollback()
        raise ForbiddenException(detail=str(refused))
    except ValueError as e:
        raise BadRequestException(detail=str(e))


@router.get("/screening", response_model=ScreeningRecord)
def get_health_screening(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The question set and the rider's latest answers, for Settings, then Health."""
    return get_screening(db, current_user)
