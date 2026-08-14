"""API endpoints for Coach Forma's proactive presence."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user
from app.core import forma_core
from app.core.exceptions import BadRequestException, NotFoundException
from app.core.ratelimit import rate_limit
from app.database import get_db
from app.models.ride import Ride
from app.models.user import User
from app.services.coach_insights_service import (
    explain_metric,
    generate_daily_nudge,
    generate_ride_debrief,
)

router = APIRouter(prefix="/coach", tags=["coach-insights"])

# Generating an initiative costs a model call, so it is capped. Per client
# rather than per user (the limiter is IP based), which is the blunt instrument
# that stops a stuck retry loop burning the rider's monthly budget. Set well
# above a dashboard visit an hour, because the real guards are the single open
# initiative rule and the budget cap, not this.
_initiative_limit = rate_limit(12, 3600)


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


# --- Coach initiatives ---
#
# The coach going first. An initiative is one thing Forma wants to raise, in
# the shape the rider can actually use: what it noticed, what it means in plain
# words, and the one question it closes on. Three generators feed it (an open
# loop from memory, something notable in a ride, the weekly check in), and the
# rider only ever sees one at a time.

class InitiativeResponse(BaseModel):
    """One thing the coach wants to raise, and the question it closes on."""
    id: str
    kind: str
    subject_type: str | None = None
    subject_id: str | None = None
    headline: str
    body: str
    question: str
    status: str
    created_at: datetime | None = None


class InitiativeEnvelope(BaseModel):
    """`verdict` explains a null, so the app never dresses silence as an error
    and never dresses an error as silence.

    pending     the rider already has one open, and one is the limit
    none        nothing is waiting on them
    created     the coach has just raised something
    silent      the coach looked and had nothing worth saying, which is fine
    unavailable the coach could not look, so nothing is claimed either way
    """
    verdict: str
    initiative: InitiativeResponse | None = None


class InitiativeDecisionResponse(BaseModel):
    id: str
    status: str


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


@router.get("/initiative", response_model=InitiativeEnvelope)
def get_initiative(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The one thing the coach is waiting to raise, or nothing at all.

    A read and never a generation. Every surface that shows the coach calls
    this, so it has to be free: nothing here costs a model call and nothing
    here creates anything.
    """
    initiative = _pending_initiative(db, current_user.id)
    if not initiative:
        return InitiativeEnvelope(verdict="none")
    return InitiativeEnvelope(
        verdict="pending",
        initiative=_initiative_to_response(initiative),
    )


@router.post(
    "/initiative/generate",
    response_model=InitiativeEnvelope,
    dependencies=[Depends(_initiative_limit)],
)
def generate_initiative(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ask the coach whether it has anything worth raising right now.

    Returns what it raises, or an honest nothing. Silence is the correct
    default and the common answer: a weak observation is worse than none,
    because it teaches the rider to skim past the one that mattered.

    Which kind of initiative is warranted is the coach's judgement, not the
    caller's, so this route takes no arguments to steer it.
    """
    from app.services import initiative_service

    # One open initiative at a time, across all three generators. Checked here
    # too, so a rider with a card already waiting never spends a call to be
    # told what the dashboard is already showing them.
    if initiative_service.has_pending(db, current_user.id):
        existing = _pending_initiative(db, current_user.id)
        return InitiativeEnvelope(
            verdict="pending",
            initiative=_initiative_to_response(existing) if existing else None,
        )

    try:
        initiative = initiative_service.generate(db, current_user)
    except forma_core.BudgetExceededError:
        # The rider did not ask for this, so the quota is not their problem to
        # read about. The app shows nothing, and the verdict keeps the record
        # honest for anyone reading the response.
        return InitiativeEnvelope(verdict="unavailable")
    except ValueError as e:
        raise BadRequestException(detail=str(e))

    if not initiative:
        return InitiativeEnvelope(verdict="silent")
    return InitiativeEnvelope(
        verdict="created",
        initiative=_initiative_to_response(initiative),
    )


@router.post(
    "/initiative/{initiative_id}/dismiss",
    response_model=InitiativeDecisionResponse,
)
def dismiss_initiative(
    initiative_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """One tap, and never a question about why.

    A second tap is not an error. The rider tapping a card that has already
    gone has told us the same thing twice, and answering that with a failure
    would make dismissing feel like a decision they have to get right.
    """
    from app.services import initiative_service

    initiative = _get_own_initiative(db, current_user, initiative_id)
    if _plain_str(initiative.status) != "dismissed":
        try:
            initiative_service.dismiss(db, current_user, initiative)
        except ValueError as e:
            raise BadRequestException(detail=str(e))
    return InitiativeDecisionResponse(
        id=initiative.id,
        status=_plain_str(initiative.status) or "dismissed",
    )


@router.post(
    "/initiative/{initiative_id}/opened",
    response_model=InitiativeDecisionResponse,
)
def mark_initiative_opened(
    initiative_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The rider took it into the chat, so the coach knows it landed.

    This is the difference between a question that was answered and a card
    that was ignored, and the coach needs to be able to tell them apart.
    """
    from app.services import initiative_service

    initiative = _get_own_initiative(db, current_user, initiative_id)
    try:
        initiative_service.mark_opened(db, current_user, initiative)
    except ValueError as e:
        raise BadRequestException(detail=str(e))
    return InitiativeDecisionResponse(
        id=initiative.id,
        status=_plain_str(initiative.status) or "opened",
    )


# --- Initiative helpers ---

def _plain_str(value) -> str | None:
    """A column's value as a plain string, whether it holds a str or an enum.

    Status is a plain string on the model today. The coercion is what stops a
    later enum column leaking `InitiativeStatus.pending` into the API and
    quietly breaking the app's status checks.
    """
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _pending_initiative(db: Session, user_id: str):
    """The rider's open initiative, if they have one.

    Newest first, because if a race ever leaves two rows pending the rider
    should see the current thought rather than the stale one.
    """
    from app.models.coach_initiative import CoachInitiative

    return (
        db.query(CoachInitiative)
        .filter(
            CoachInitiative.user_id == user_id,
            CoachInitiative.status == "pending",
        )
        .order_by(CoachInitiative.created_at.desc())
        .first()
    )


def _get_own_initiative(db: Session, user: User, initiative_id: str):
    """Load one of the rider's own initiatives, or refuse.

    Scoping the query to the caller keeps a guessed id from reaching another
    rider's coaching.
    """
    from app.models.coach_initiative import CoachInitiative

    initiative = (
        db.query(CoachInitiative)
        .filter(
            CoachInitiative.id == initiative_id,
            CoachInitiative.user_id == user.id,
        )
        .first()
    )
    if not initiative:
        raise NotFoundException(detail="Initiative not found")
    return initiative


def _initiative_to_response(initiative) -> InitiativeResponse:
    """Convert CoachInitiative to response schema."""
    return InitiativeResponse(
        id=initiative.id,
        kind=_plain_str(initiative.kind) or "",
        subject_type=_plain_str(initiative.subject_type),
        subject_id=initiative.subject_id,
        headline=initiative.headline or "",
        body=initiative.body or "",
        question=initiative.question or "",
        status=_plain_str(initiative.status) or "",
        created_at=initiative.created_at,
    )
