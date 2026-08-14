"""Training plan and workout API endpoints."""

from datetime import date, datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user
from app.core.exceptions import (
    BadRequestException,
    ConflictException,
    NotFoundException,
)
from app.core.ratelimit import rate_limit
from app.database import get_db
from app.models.user import User
from app.schemas.training import (
    PlanGenerateRequest,
    PlanListResponse,
    PlanWorkoutsResponse,
    TrainingPlanDetailResponse,
    TrainingPlanResponse,
    TrainingPhaseResponse,
    WorkoutAssessmentResponse,
    WorkoutDetailResponse,
    WorkoutLinkRideRequest,
    WorkoutResponse,
    WorkoutStepResponse,
    WorkoutUpdateRequest,
)
from app.services.plan_service import (
    generate_plan,
    get_plan,
    get_plan_workouts,
    get_plans,
    get_workout,
    get_workouts_by_date,
    link_ride_to_workout,
    update_workout_status,
)
from app.services.ride_service import get_ride
from app.services.workout_assessment_service import generate_assessment

router = APIRouter(tags=["training"])

# An on-demand review costs a Sonnet call, so it is capped. Per client rather
# than per user (the limiter is IP based), which is the blunt instrument that
# stops a stuck retry loop burning the rider's monthly budget.
_review_limit = rate_limit(5, 3600)  # 5 plan reviews / hour


# --- Plans ---

@router.get("/plans", response_model=PlanListResponse)
def list_plans(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all training plans."""
    plans = get_plans(db, current_user.id)
    return PlanListResponse(
        plans=[_plan_to_response(p) for p in plans],
        total=len(plans),
    )


@router.post("/plans/generate", response_model=TrainingPlanDetailResponse, status_code=201)
def generate_training_plan(
    body: PlanGenerateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate a new periodized training plan based on goals and fitness."""
    try:
        plan = generate_plan(
            db, current_user,
            goal_event_id=body.goal_event_id,
            periodization_model=body.periodization_model,
            name=body.name,
        )
    except ValueError as e:
        raise BadRequestException(detail=str(e))

    return _plan_to_detail_response(plan)


@router.get("/plans/{plan_id}", response_model=TrainingPlanDetailResponse)
def get_training_plan(
    plan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a training plan with phases."""
    plan = get_plan(db, plan_id, current_user.id)
    if not plan:
        raise NotFoundException(detail="Plan not found")
    return _plan_to_detail_response(plan)


@router.get("/plans/{plan_id}/workouts", response_model=PlanWorkoutsResponse)
def list_plan_workouts(
    plan_id: str,
    start_date: date | None = None,
    end_date: date | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get all workouts for a plan (calendar data)."""
    workouts = get_plan_workouts(db, plan_id, current_user.id, start_date, end_date)
    return PlanWorkoutsResponse(
        plan_id=plan_id,
        workouts=[WorkoutResponse.model_validate(w) for w in workouts],
        total=len(workouts),
    )


# --- Workouts ---

@router.get("/workouts", response_model=list[WorkoutResponse])
def list_workouts(
    target_date: date | None = Query(None, alias="date"),
    week: date | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List workouts by date or week."""
    workouts = get_workouts_by_date(db, current_user.id, target_date, week)
    return [WorkoutResponse.model_validate(w) for w in workouts]


@router.get("/workouts/{workout_id}", response_model=WorkoutDetailResponse)
def get_workout_detail(
    workout_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a workout with full step details."""
    workout = get_workout(db, workout_id, current_user.id)
    if not workout:
        raise NotFoundException(detail="Workout not found")

    return WorkoutDetailResponse(
        **{k: v for k, v in WorkoutResponse.model_validate(workout).model_dump().items()},
        steps=[WorkoutStepResponse.model_validate(s) for s in workout.steps],
    )


@router.patch("/workouts/{workout_id}", response_model=WorkoutResponse)
def update_workout(
    workout_id: str,
    body: WorkoutUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update workout status (completed/skipped/modified)."""
    workout = get_workout(db, workout_id, current_user.id)
    if not workout:
        raise NotFoundException(detail="Workout not found")

    if body.status:
        try:
            workout = update_workout_status(db, workout, body.status, body.actual_ride_id)
        except ValueError as e:
            raise BadRequestException(detail=str(e))

    return WorkoutResponse.model_validate(workout)


@router.post("/workouts/{workout_id}/link-ride", response_model=WorkoutResponse)
def link_ride(
    workout_id: str,
    body: WorkoutLinkRideRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Link an actual ride to a planned workout."""
    workout = get_workout(db, workout_id, current_user.id)
    if not workout:
        raise NotFoundException(detail="Workout not found")

    # The ride must belong to the caller too, or a forged ride_id links
    # (and later surfaces, via the assessment) another user's ride data.
    ride = get_ride(db, body.ride_id, current_user.id)
    if not ride:
        raise NotFoundException(detail="Ride not found")

    workout = link_ride_to_workout(db, workout, body.ride_id)
    return WorkoutResponse.model_validate(workout)


@router.get(
    "/workouts/{workout_id}/assessment",
    response_model=WorkoutAssessmentResponse,
)
def get_workout_assessment(
    workout_id: str,
    force: bool = Query(False, description="Regenerate feedback even if cached"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Score the actual ride against the planned workout and return supportive
    feedback from Coach Forma plus suggested adjustments to upcoming days.

    The numeric score is always recomputed; the written feedback is cached on
    the workout row and only regenerated when `force=true` or when no cached
    feedback exists yet.
    """
    workout = get_workout(db, workout_id, current_user.id)
    if not workout:
        raise NotFoundException(detail="Workout not found")
    if not workout.actual_ride_id:
        raise BadRequestException(detail="No ride is linked to this workout yet")

    try:
        workout = generate_assessment(db, current_user, workout, force=force)
    except ValueError as e:
        raise BadRequestException(detail=str(e))

    return WorkoutAssessmentResponse(
        workout_id=workout.id,
        score=workout.execution_score or 0.0,
        feedback=workout.execution_feedback or "",
        adjustments=workout.execution_adjustments or [],
        assessed_at=workout.execution_assessed_at,
    )


# --- Plan proposals ---
#
# The coach interrogates the plan it wrote and, when the evidence says the plan
# is wrong, files a proposal. A proposal is an argument, not an edit: the
# rider's calendar only moves through accept, and it moves exactly once.


class PlanProposalResponse(BaseModel):
    """A change the coach is putting to the rider, and its reasoning."""
    id: str
    trigger: str
    observation: str
    rationale: str
    changes: list[dict] = []
    created_at: datetime | None = None


class ProposalListResponse(BaseModel):
    proposals: list[PlanProposalResponse]
    total: int


class ProposalDecisionResponse(BaseModel):
    id: str
    status: str
    workouts_changed: int = 0
    # Same number under the name the app reads. One decision, one count, so a
    # rider is never told two different stories about what just moved.
    changes_applied: int = 0
    message: str = ""


class PlanReviewResponse(BaseModel):
    """`verdict` is what actually happened, so the app never reports "nothing
    to change" for a review that did not run."""
    verdict: str  # "proposal" | "no_change" | "no_plan" | "unavailable"
    message: str
    proposal: PlanProposalResponse | None = None


@router.get("/training/proposals", response_model=ProposalListResponse)
def list_plan_proposals(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Proposals waiting on the rider: what the coach noticed, why it matters,
    and exactly what would change if they say yes."""
    from app.models.plan_proposal import PlanProposal

    proposals = (
        db.query(PlanProposal)
        .filter(
            PlanProposal.user_id == current_user.id,
            PlanProposal.status == "pending",
        )
        .order_by(PlanProposal.created_at.desc())
        .all()
    )
    return ProposalListResponse(
        proposals=[_proposal_to_response(p) for p in proposals],
        total=len(proposals),
    )


@router.post("/training/proposals/{proposal_id}/accept", response_model=ProposalDecisionResponse)
def accept_plan_proposal(
    proposal_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Apply a proposal to the plan. This is the only path that turns a
    proposal into real workouts, so a decided proposal is refused rather than
    applied twice."""
    from app.services.plan_review_service import apply_proposal

    proposal = _get_pending_proposal(db, current_user, proposal_id)
    try:
        changed = apply_proposal(db, current_user, proposal)
    except ValueError as e:
        raise BadRequestException(detail=str(e))

    return ProposalDecisionResponse(
        id=proposal.id,
        status=_plain_str(proposal.status) or "accepted",
        workouts_changed=changed,
        changes_applied=changed,
        message=(
            f"{changed} session{'' if changed == 1 else 's'} updated."
            if changed
            else "Nothing needed changing on the calendar."
        ),
    )


@router.post("/training/proposals/{proposal_id}/decline", response_model=ProposalDecisionResponse)
def decline_plan_proposal(
    proposal_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Turn a proposal down. The plan is untouched and the coach is told, so it
    stops making the same case."""
    from app.services.plan_review_service import decline_proposal

    proposal = _get_pending_proposal(db, current_user, proposal_id)
    try:
        decline_proposal(db, current_user, proposal)
    except ValueError as e:
        raise BadRequestException(detail=str(e))

    return ProposalDecisionResponse(
        id=proposal.id,
        status=_plain_str(proposal.status) or "declined",
        message="Left as it is. Your plan has not changed.",
    )


@router.post(
    "/training/review",
    response_model=PlanReviewResponse,
    dependencies=[Depends(_review_limit)],
)
def review_training_plan(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ask the coach to interrogate the plan now, against everything it knows.

    Returns the proposal it raises, or an honest "nothing to change" when the
    plan still holds. Nothing is applied here either way.
    """
    from app.core import forma_core
    from app.services.plan_review_service import review_plan

    # There is nothing to interrogate without an active plan, and "I would not
    # change anything" would be a strange thing to hear when nothing exists.
    # Checked here so the rider gets the real answer and no call is spent.
    if not any(p.status == "active" for p in get_plans(db, current_user.id)):
        return PlanReviewResponse(
            verdict="no_plan",
            message=(
                "You have no active plan for me to interrogate yet. Give me a "
                "goal and I will write one, then I will keep questioning it."
            ),
        )

    try:
        proposal = review_plan(db, current_user, trigger="manual")
    except forma_core.BudgetExceededError:
        # Saying "nothing to change" for a review that never ran would be a
        # lie the rider could act on, so name what happened.
        return PlanReviewResponse(
            verdict="unavailable",
            message=forma_core.QUOTA_MESSAGE,
        )
    except ValueError as e:
        raise BadRequestException(detail=str(e))

    if not proposal:
        return PlanReviewResponse(
            verdict="no_change",
            message=(
                "I have been back through your plan against your goal and your "
                "recent riding. I would not change anything today."
            ),
        )

    return PlanReviewResponse(
        verdict="proposal",
        message=proposal.observation or "I have a change to put to you.",
        proposal=_proposal_to_response(proposal),
    )


# --- Helpers ---

def _plain_str(value) -> str | None:
    """A column's value as a plain string, whether it holds a str or an enum.

    Status and trigger are plain strings on the model today. The coercion is
    what stops a later enum column leaking `PlanProposalStatus.pending` into
    the API and quietly breaking the app's status checks.
    """
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _get_pending_proposal(db: Session, user: User, proposal_id: str):
    """Load one of the rider's own pending proposals, or refuse.

    Scoping the query to the caller keeps a guessed id from reaching another
    rider's plan; the pending check stops the same change being applied twice
    if the card is tapped from two places.
    """
    from app.models.plan_proposal import PlanProposal

    proposal = (
        db.query(PlanProposal)
        .filter(
            PlanProposal.id == proposal_id,
            PlanProposal.user_id == user.id,
        )
        .first()
    )
    if not proposal:
        raise NotFoundException(detail="Proposal not found")
    if _plain_str(proposal.status) != "pending":
        raise ConflictException(
            detail=f"This proposal has already been {_plain_str(proposal.status)}"
        )
    return proposal


def _proposal_to_response(proposal) -> PlanProposalResponse:
    """Convert PlanProposal to response schema."""
    changes = proposal.changes or []
    return PlanProposalResponse(
        id=proposal.id,
        trigger=_plain_str(proposal.trigger) or "",
        observation=proposal.observation or "",
        rationale=proposal.rationale or "",
        changes=[c for c in changes if isinstance(c, dict)],
        created_at=proposal.created_at,
    )


def _plan_to_response(plan) -> TrainingPlanResponse:
    """Convert TrainingPlan to response schema."""
    start = plan.start_date
    end = plan.end_date
    total_weeks = 0
    if start and end:
        total_weeks = max(1, (end - start).days // 7)

    return TrainingPlanResponse(
        id=plan.id,
        name=plan.name,
        goal_event_id=plan.goal_event_id,
        start_date=plan.start_date,
        end_date=plan.end_date,
        status=plan.status,
        periodization_model=plan.periodization_model,
        created_at=plan.created_at,
        total_weeks=total_weeks,
        phase_count=len(plan.phases) if plan.phases else 0,
    )


def _plan_to_detail_response(plan) -> TrainingPlanDetailResponse:
    """Convert TrainingPlan to detail response with phases."""
    base = _plan_to_response(plan)

    phases = []
    for p in plan.phases:
        phases.append(TrainingPhaseResponse(
            id=p.id,
            phase_type=p.phase_type,
            start_date=p.start_date,
            end_date=p.end_date,
            target_weekly_tss=p.target_weekly_tss,
            target_weekly_hours=p.target_weekly_hours,
            focus=p.focus,
            sort_order=p.sort_order,
            workout_count=len(p.workouts) if p.workouts else 0,
        ))

    return TrainingPlanDetailResponse(
        **base.model_dump(),
        phases=phases,
    )
