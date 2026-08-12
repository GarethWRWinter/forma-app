"""Goal event CRUD API endpoints."""

import logging
import os
import uuid
from datetime import date

logger = logging.getLogger(__name__)

from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user
from app.core.exceptions import BadRequestException, NotFoundException
from app.database import get_db
from app.models.user import User
from app.schemas.onboarding import (
    GoalAssessmentSubmit,
    GoalEventCreate,
    GoalEventListResponse,
    GoalEventResponse,
    GoalEventUpdate,
    GoalReadinessResponse,
    PlannedVsActualResponse,
    RaceProjectionResponse,
)
from app.services.assessment_service import (
    get_candidate_rides,
    get_goals_needing_assessment,
    get_planned_vs_actual,
    submit_assessment,
)
from app.services.onboarding_service import (
    assess_goal_readiness,
    create_goal,
    delete_goal,
    get_goal,
    get_goals,
    update_goal,
)

router = APIRouter(prefix="/goals", tags=["goals"])

GPX_UPLOAD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "uploads", "gpx",
)


def _goal_to_response(goal) -> GoalEventResponse:
    """Convert GoalEvent model to response, adding days_until and needs_assessment."""
    today = date.today()
    event_date = goal.event_date
    if hasattr(event_date, 'date'):
        event_date = event_date.date() if callable(getattr(event_date, 'date', None)) else event_date
    days_until = (event_date - today).days if event_date >= today else None

    # Goal needs assessment if event is past and status is still upcoming
    status = goal.status or "upcoming"
    needs_assessment = event_date < today and status == "upcoming"

    return GoalEventResponse(
        id=goal.id,
        event_name=goal.event_name,
        event_date=goal.event_date,
        event_type=goal.event_type,
        priority=goal.priority,
        target_duration_minutes=goal.target_duration_minutes,
        notes=goal.notes,
        why=goal.why,
        becoming=goal.becoming,
        coach_read=goal.coach_read,
        coach_read_at=goal.coach_read_at,
        days_until=days_until,
        route_url=goal.route_url,
        gpx_file_path=goal.gpx_file_path,
        route_data=goal.route_data,
        status=status,
        actual_ride_id=goal.actual_ride_id,
        finish_time_seconds=goal.finish_time_seconds,
        finish_position=goal.finish_position,
        finish_position_total=goal.finish_position_total,
        overall_satisfaction=goal.overall_satisfaction,
        perceived_exertion=goal.perceived_exertion,
        assessment_data=goal.assessment_data,
        assessment_completed_at=goal.assessment_completed_at,
        needs_assessment=needs_assessment,
    )


@router.get("", response_model=GoalEventListResponse)
def list_goals(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all goal events for the current user."""
    goals = get_goals(db, current_user.id)
    return GoalEventListResponse(
        goals=[_goal_to_response(g) for g in goals],
        total=len(goals),
    )


@router.post("", response_model=GoalEventResponse, status_code=201)
def create_goal_event(
    body: GoalEventCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new goal event (race, sportive, etc.)."""
    try:
        goal = create_goal(
            db,
            current_user.id,
            event_name=body.event_name,
            event_date=body.event_date,
            event_type=body.event_type,
            priority=body.priority,
            target_duration_minutes=body.target_duration_minutes,
            notes=body.notes,
            route_url=body.route_url,
            why=body.why,
            becoming=body.becoming,
        )
    except ValueError as e:
        raise BadRequestException(detail=str(e))

    # If a route URL was provided, fetch route data in background
    if body.route_url:
        from app.services.route_service import fetch_route_data_bg
        background_tasks.add_task(fetch_route_data_bg, goal.id, body.route_url)

    return _goal_to_response(goal)


@router.post("/{goal_id}/gpx", response_model=GoalEventResponse)
async def upload_goal_gpx(
    goal_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload a GPX file for a goal event's route."""
    goal = get_goal(db, goal_id, current_user.id)
    if not goal:
        raise NotFoundException(detail="Goal not found")

    if not file.filename or not file.filename.lower().endswith(".gpx"):
        raise BadRequestException(detail="File must be a .gpx file")

    # Save GPX file
    os.makedirs(GPX_UPLOAD_DIR, exist_ok=True)
    filename = f"{uuid.uuid4()}.gpx"
    filepath = os.path.join(GPX_UPLOAD_DIR, filename)

    content = await file.read()
    with open(filepath, "wb") as f:
        f.write(content)

    # Update goal with GPX path
    goal.gpx_file_path = filepath

    # Parse GPX synchronously so route_data is available in the response
    from app.services.route_service import parse_gpx_route_data
    try:
        route_data = parse_gpx_route_data(filepath)
        goal.route_data = route_data
    except Exception:
        pass  # File saved; route_data can be reparsed later

    db.commit()
    db.refresh(goal)
    return _goal_to_response(goal)


@router.delete("/{goal_id}/gpx", response_model=GoalEventResponse)
def delete_goal_gpx(
    goal_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove GPX file and route data from a goal event."""
    goal = get_goal(db, goal_id, current_user.id)
    if not goal:
        raise NotFoundException(detail="Goal not found")

    # Delete the physical file if it exists
    if goal.gpx_file_path and os.path.exists(goal.gpx_file_path):
        os.remove(goal.gpx_file_path)

    goal.gpx_file_path = None
    goal.route_data = None
    db.commit()
    db.refresh(goal)
    return _goal_to_response(goal)


@router.get("/{goal_id}", response_model=GoalEventResponse)
def get_goal_event(
    goal_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a single goal event."""
    goal = get_goal(db, goal_id, current_user.id)
    if not goal:
        raise NotFoundException(detail="Goal not found")
    return _goal_to_response(goal)


@router.patch("/{goal_id}", response_model=GoalEventResponse)
def update_goal_event(
    goal_id: str,
    body: GoalEventUpdate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update a goal event."""
    goal = get_goal(db, goal_id, current_user.id)
    if not goal:
        raise NotFoundException(detail="Goal not found")

    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        raise BadRequestException(detail="No fields to update")

    try:
        goal = update_goal(db, goal, update_data)
    except ValueError as e:
        raise BadRequestException(detail=str(e))

    # If route_url was updated, re-fetch route data
    if "route_url" in update_data and update_data["route_url"]:
        from app.services.route_service import fetch_route_data_bg
        background_tasks.add_task(fetch_route_data_bg, goal.id, update_data["route_url"])

    return _goal_to_response(goal)


@router.post("/{goal_id}/coach-read", response_model=GoalEventResponse)
def generate_coach_read(
    goal_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ask Forma for its honest read on this goal: the size verdict, the
    soul check, one enhancement, one question back. Regenerates on demand."""
    from app.services.goal_read_service import generate_goal_read

    goal = get_goal(db, goal_id, current_user.id)
    if not goal:
        raise NotFoundException(detail="Goal not found")
    if goal.status and str(goal.status) not in ("upcoming", "GoalStatus.upcoming"):
        raise BadRequestException(detail="The read is for upcoming goals")
    try:
        goal = generate_goal_read(db, current_user, goal)
    except Exception:
        logger.exception("goal read generation failed")
        raise BadRequestException(
            detail="Forma couldn't write the read just now. Try again shortly."
        )
    return _goal_to_response(goal)


@router.get("/{goal_id}/readiness", response_model=GoalReadinessResponse)
def get_goal_readiness(
    goal_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get fitness readiness assessment for a goal event."""
    goal = get_goal(db, goal_id, current_user.id)
    if not goal:
        raise NotFoundException(detail="Goal not found")
    return assess_goal_readiness(db, goal, current_user.id)


@router.post("/{goal_id}/reparse-gpx", response_model=GoalEventResponse)
def reparse_goal_gpx(
    goal_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Re-parse an existing GPX file to update route_data (e.g., after parser improvements)."""
    goal = get_goal(db, goal_id, current_user.id)
    if not goal:
        raise NotFoundException(detail="Goal not found")

    if not goal.gpx_file_path or not os.path.exists(goal.gpx_file_path):
        raise BadRequestException(detail="No GPX file associated with this goal")

    from app.services.route_service import parse_gpx_route_data
    route_data = parse_gpx_route_data(goal.gpx_file_path)
    goal.route_data = route_data
    db.commit()
    db.refresh(goal)

    return _goal_to_response(goal)


@router.post("/{goal_id}/refetch-route", response_model=GoalEventResponse)
def refetch_goal_route(
    goal_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Re-fetch route data from the goal's route URL (e.g., after scraper improvements)."""
    goal = get_goal(db, goal_id, current_user.id)
    if not goal:
        raise NotFoundException(detail="Goal not found")

    if not goal.route_url:
        raise BadRequestException(detail="No route URL associated with this goal")

    from app.services.route_service import fetch_route_data
    route_data = fetch_route_data(goal.route_url)
    goal.route_data = route_data
    db.commit()
    db.refresh(goal)

    return _goal_to_response(goal)


@router.get("/{goal_id}/race-projection", response_model=RaceProjectionResponse)
def get_race_projection(
    goal_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get race day performance projection for a goal event."""
    goal = get_goal(db, goal_id, current_user.id)
    if not goal:
        raise NotFoundException(detail="Goal not found")

    if not current_user.ftp or not current_user.weight_kg:
        raise BadRequestException(
            detail="FTP and weight are required for race projections. Update your profile."
        )

    route_data = goal.route_data
    if not route_data or not route_data.get("elevation_profile"):
        raise BadRequestException(
            detail="Elevation profile required. Upload a GPX file for this goal."
        )

    from app.services.race_projection_service import get_race_projection as compute_projection
    projection = compute_projection(goal, current_user, db)

    if not projection:
        raise BadRequestException(detail="Unable to compute projection with available data.")

    return projection


@router.get("/needs-assessment", response_model=GoalEventListResponse)
def list_goals_needing_assessment(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List goals that need post-event assessment."""
    goals_list = get_goals_needing_assessment(db, current_user.id)
    return GoalEventListResponse(
        goals=[_goal_to_response(g) for g in goals_list],
        total=len(goals_list),
    )


@router.post("/{goal_id}/assessment", response_model=GoalEventResponse)
def submit_goal_assessment(
    goal_id: str,
    body: GoalAssessmentSubmit,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Submit post-event assessment for a goal."""
    goal = get_goal(db, goal_id, current_user.id)
    if not goal:
        raise NotFoundException(detail="Goal not found")

    try:
        goal = submit_assessment(db, goal, body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise BadRequestException(detail=str(e))

    return _goal_to_response(goal)


@router.get("/{goal_id}/assessment")
def get_goal_assessment(
    goal_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get assessment data and planned-vs-actual for a goal."""
    goal = get_goal(db, goal_id, current_user.id)
    if not goal:
        raise NotFoundException(detail="Goal not found")

    planned_vs_actual = get_planned_vs_actual(db, goal)

    return {
        "goal": _goal_to_response(goal),
        "planned_vs_actual": planned_vs_actual,
    }


@router.get("/{goal_id}/candidate-rides")
def get_goal_candidate_rides(
    goal_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get rides near the event date for linking to assessment."""
    goal = get_goal(db, goal_id, current_user.id)
    if not goal:
        raise NotFoundException(detail="Goal not found")

    rides = get_candidate_rides(db, current_user.id, goal.event_date)
    return {
        "rides": [
            {
                "id": r.id,
                "title": r.title,
                "ride_date": str(r.ride_date.date() if hasattr(r.ride_date, "date") else r.ride_date),
                "duration_seconds": r.duration_seconds,
                "distance_meters": r.distance_meters,
                "normalized_power": r.normalized_power,
                "tss": round(r.tss, 1) if r.tss else None,
                "average_power": r.average_power,
            }
            for r in rides
        ]
    }


@router.delete("/{goal_id}", status_code=204)
def delete_goal_event(
    goal_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a goal event."""
    goal = get_goal(db, goal_id, current_user.id)
    if not goal:
        raise NotFoundException(detail="Goal not found")
    delete_goal(db, goal)
