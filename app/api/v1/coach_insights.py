"""API endpoints for Coach Forma's proactive presence."""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user
from app.core import forma_core
from app.core.exceptions import NotFoundException
from app.database import get_db
from app.models.ride import Ride
from app.models.user import User
from app.services.coach_insights_service import (
    explain_metric,
    generate_daily_nudge,
    generate_ride_debrief,
)

router = APIRouter(prefix="/coach", tags=["coach-insights"])


# --- Schemas ---

class NudgeResponse(BaseModel):
    nudge: str
    generated_at: str
    cached: bool = False


class DebriefResponse(BaseModel):
    debrief: str
    generated_at: str
    cached: bool = False


class ExplainRequest(BaseModel):
    metric_name: str
    metric_value: str | float


class ExplainResponse(BaseModel):
    explanation: str


class UsageResponse(BaseModel):
    month_spend_usd: float
    month_budget_usd: float
    pct_used: int
    state: str  # "ok" | "soft" | "hard"


# --- Endpoints ---

@router.get("/briefing")
async def get_briefing(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Today's pre-ride briefing: the team car before the stage. Generated
    once per day per rider (goal days get the full talk) and cached."""
    from app.services.briefing_service import get_or_create_briefing

    briefing = await get_or_create_briefing(db, current_user)
    return {
        "kind": briefing.kind,
        "date": briefing.date.isoformat(),
        "content": briefing.content,
        "conditions": briefing.conditions,
    }


@router.get("/usage", response_model=UsageResponse)
def get_usage(current_user: User = Depends(get_current_user)):
    """The rider's month-to-date Forma spend vs their cap. Drives the soft-cap
    warning in the UI and tells the frontend when the quota is exhausted."""
    s = forma_core.budget_status(current_user.id)
    return UsageResponse(
        month_spend_usd=round(s.spent_cents / 100, 4),
        month_budget_usd=round(s.budget_cents / 100, 2),
        pct_used=min(100, round(s.ratio * 100)),
        state=s.state,
    )

@router.get("/nudge", response_model=NudgeResponse)
def get_daily_nudge(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get today's coaching nudge from Forma. Cached per day."""
    return generate_daily_nudge(db, current_user)


@router.get("/ride-debrief/{ride_id}", response_model=DebriefResponse)
def get_ride_debrief(
    ride_id: str,
    force: bool = Query(False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get Forma's post-ride debrief. Cached on the ride record."""
    ride = (
        db.query(Ride)
        .filter(Ride.id == ride_id, Ride.user_id == current_user.id)
        .first()
    )
    if not ride:
        raise NotFoundException(detail="Ride not found")
    return generate_ride_debrief(db, current_user, ride, force=force)


@router.post("/explain", response_model=ExplainResponse)
def explain_metric_endpoint(
    body: ExplainRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ask Forma to explain a metric in your personal context."""
    return explain_metric(db, current_user, body.metric_name, body.metric_value)
