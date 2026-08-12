"""Onboarding quiz and goal event business logic."""

from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.models.onboarding import (
    EventPriority,
    EventType,
    GoalEvent,
    IndoorOutdoorPreference,
    OnboardingResponse,
    PrimaryGoal,
)
from app.models.training import Workout, WorkoutStatus
from app.models.user import ExperienceLevel


def submit_quiz(
    db: Session,
    user_id: str,
    primary_goal: str,
    secondary_goals: list[str] | None = None,
    current_weekly_volume_hours: float | None = None,
    years_cycling: int | None = None,
    indoor_outdoor_preference: str | None = None,
) -> OnboardingResponse:
    """
    Save onboarding quiz answers. Replaces any existing response.
    Also infers experience level from years_cycling + weekly volume.
    """
    # Validate enums
    PrimaryGoal(primary_goal)  # raises ValueError if invalid
    if indoor_outdoor_preference:
        IndoorOutdoorPreference(indoor_outdoor_preference)

    # Delete any existing response
    db.query(OnboardingResponse).filter(
        OnboardingResponse.user_id == user_id
    ).delete()

    response = OnboardingResponse(
        user_id=user_id,
        primary_goal=primary_goal,
        secondary_goals=secondary_goals,
        current_weekly_volume_hours=current_weekly_volume_hours,
        years_cycling=years_cycling,
        indoor_outdoor_preference=indoor_outdoor_preference,
        completed_at=datetime.now(timezone.utc),
    )
    db.add(response)

    # Infer experience level if not already set
    from app.models.user import User
    user = db.query(User).filter(User.id == user_id).first()
    if user and not user.experience_level:
        user.experience_level = _infer_experience_level(
            years_cycling, current_weekly_volume_hours
        )

    # Update weekly hours on user profile
    if user and current_weekly_volume_hours is not None:
        user.weekly_hours_available = current_weekly_volume_hours

    db.commit()
    db.refresh(response)

    # First contact writes the first memories — the relationship starts here.
    try:
        if user:
            from app.services.memory_service import extract_memories

            extract_memories(
                db, user,
                (
                    "Rider's onboarding answers (their own words about who they are):\n"
                    f"- Primary goal: {primary_goal}\n"
                    f"- Secondary goals: {', '.join(secondary_goals) if secondary_goals else 'none'}\n"
                    f"- Years cycling: {years_cycling}\n"
                    f"- Weekly hours available: {current_weekly_volume_hours}\n"
                    f"- Indoor/outdoor preference: {indoor_outdoor_preference}"
                ),
                source="onboarding",
                source_ref=response.id,
            )
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Memory extraction after onboarding failed (user=%s)", user_id
        )

    return response


def get_onboarding_status(db: Session, user_id: str) -> dict:
    """Check if user has completed onboarding."""
    response = (
        db.query(OnboardingResponse)
        .filter(OnboardingResponse.user_id == user_id)
        .first()
    )
    if not response:
        return {"completed": False, "completed_at": None, "primary_goal": None, "secondary_goals": None}

    return {
        "completed": True,
        "completed_at": response.completed_at,
        "primary_goal": response.primary_goal,
        "secondary_goals": response.secondary_goals,
    }


def get_onboarding_response(db: Session, user_id: str) -> OnboardingResponse | None:
    """Get the full onboarding response for a user."""
    return (
        db.query(OnboardingResponse)
        .filter(OnboardingResponse.user_id == user_id)
        .first()
    )


# --- Goal Events ---

def create_goal(
    db: Session,
    user_id: str,
    event_name: str,
    event_date: date,
    event_type: str,
    priority: str,
    target_duration_minutes: int | None = None,
    notes: str | None = None,
    route_url: str | None = None,
    why: str | None = None,
    becoming: str | None = None,
) -> GoalEvent:
    """Create a new goal event."""
    # Validate enums
    EventType(event_type)
    EventPriority(priority)

    goal = GoalEvent(
        user_id=user_id,
        event_name=event_name,
        event_date=event_date,
        event_type=event_type,
        priority=priority,
        target_duration_minutes=target_duration_minutes,
        notes=notes,
        route_url=route_url,
        why=why,
        becoming=becoming,
    )
    db.add(goal)
    db.commit()
    db.refresh(goal)

    # Auto-skip any planned workouts on the goal event date
    _skip_workouts_on_date(db, user_id, event_date)

    return goal


def get_goals(db: Session, user_id: str) -> list[GoalEvent]:
    """Get all goal events for a user, ordered by date."""
    return (
        db.query(GoalEvent)
        .filter(GoalEvent.user_id == user_id)
        .order_by(GoalEvent.event_date)
        .all()
    )


def get_goal(db: Session, goal_id: str, user_id: str) -> GoalEvent | None:
    """Get a single goal event by ID."""
    return (
        db.query(GoalEvent)
        .filter(GoalEvent.id == goal_id, GoalEvent.user_id == user_id)
        .first()
    )


def update_goal(
    db: Session, goal: GoalEvent, update_data: dict
) -> GoalEvent:
    """Update a goal event."""
    # Validate enums if provided
    if "event_type" in update_data and update_data["event_type"] is not None:
        EventType(update_data["event_type"])
    if "priority" in update_data and update_data["priority"] is not None:
        EventPriority(update_data["priority"])

    date_changed = "event_date" in update_data and update_data["event_date"] != goal.event_date

    for field, value in update_data.items():
        if value is not None:
            setattr(goal, field, value)

    db.commit()
    db.refresh(goal)

    # If the event date changed, skip workouts on the new date
    if date_changed:
        _skip_workouts_on_date(db, goal.user_id, goal.event_date)

    return goal


def _skip_workouts_on_date(db: Session, user_id: str, event_date: date) -> None:
    """Auto-skip any planned workouts on a goal event date."""
    workouts = (
        db.query(Workout)
        .filter(
            Workout.user_id == user_id,
            Workout.scheduled_date == event_date,
            Workout.status == WorkoutStatus.planned,
        )
        .all()
    )
    for w in workouts:
        w.status = WorkoutStatus.skipped
    if workouts:
        db.commit()


def delete_goal(db: Session, goal: GoalEvent) -> None:
    """Delete a goal event."""
    db.delete(goal)
    db.commit()


def assess_goal_readiness(
    db: Session, goal: GoalEvent, user_id: str
) -> dict:
    """
    Assess fitness readiness for a goal event.
    Returns current fitness metrics, target estimates, and recommendations.
    """
    from app.models.user import User
    from app.services.metrics_service import get_current_fitness

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return _empty_readiness(goal)

    # Get current fitness metrics
    try:
        fitness = get_current_fitness(db, user_id)
        current_ctl = fitness.get("ctl", 0) if fitness else 0
        current_atl = fitness.get("atl", 0) if fitness else 0
        current_tsb = fitness.get("tsb", 0) if fitness else 0
    except Exception:
        current_ctl = 0
        current_atl = 0
        current_tsb = 0

    # Determine target CTL based on event type and duration
    target_ctl = _estimate_target_ctl(
        goal.event_type, goal.target_duration_minutes, goal.route_data
    )

    # Calculate days until
    today = date.today()
    event_date = goal.event_date
    if hasattr(event_date, "date") and callable(getattr(event_date, "date", None)):
        event_date = event_date.date()
    days_until = (event_date - today).days

    # Project TSB on event day (linear projection based on current trend)
    projected_tsb = None
    if days_until > 0:
        # Assume a gradual taper: TSB moves toward +5 to +15 over last 2 weeks
        if days_until <= 14:
            # Tapering phase: TSB should be rising
            projected_tsb = current_tsb + (days_until * 1.5)
        else:
            # Building phase: TSB will be negative, then taper
            build_days = days_until - 14
            # During build, TSB drops slightly then recovers in taper
            projected_tsb = current_tsb - (build_days * 0.3) + (14 * 1.5)

    # Calculate readiness score (0-100)
    ctl_ratio = min(current_ctl / max(target_ctl, 1), 1.5)
    time_factor = min(days_until / 30, 1.0) if days_until > 0 else 0

    # Weighted score
    readiness_score = int(
        min(100, max(0, (ctl_ratio * 60) + (time_factor * 40)))
    )

    # Readiness label
    if readiness_score >= 70:
        readiness_label = "On Track"
    elif readiness_score >= 40:
        readiness_label = "Needs Work"
    else:
        readiness_label = "At Risk"

    # Build recommendations
    recommendations = _build_recommendations(
        current_ctl, target_ctl, current_tsb, days_until,
        goal.event_type, user.ftp, goal.route_data
    )

    # W/kg calculation
    w_per_kg = None
    if user.ftp and user.weight_kg and user.weight_kg > 0:
        w_per_kg = round(user.ftp / user.weight_kg, 2)

    return {
        "goal_id": goal.id,
        "current_ctl": round(current_ctl, 1),
        "target_ctl": round(target_ctl, 1),
        "current_ftp": user.ftp,
        "w_per_kg": w_per_kg,
        "current_tsb": round(current_tsb, 1),
        "projected_tsb_on_event": round(projected_tsb, 1) if projected_tsb is not None else None,
        "days_until": max(days_until, 0),
        "readiness_score": readiness_score,
        "readiness_label": readiness_label,
        "recommendations": recommendations,
    }


def _empty_readiness(goal: GoalEvent) -> dict:
    """Return empty readiness when no user data available."""
    today = date.today()
    event_date = goal.event_date
    if hasattr(event_date, "date") and callable(getattr(event_date, "date", None)):
        event_date = event_date.date()
    days_until = (event_date - today).days

    return {
        "goal_id": goal.id,
        "current_ctl": 0,
        "target_ctl": 50,
        "current_ftp": None,
        "w_per_kg": None,
        "current_tsb": 0,
        "projected_tsb_on_event": None,
        "days_until": max(days_until, 0),
        "readiness_score": 0,
        "readiness_label": "At Risk",
        "recommendations": ["Upload rides with power data to track your fitness."],
    }


def _estimate_target_ctl(
    event_type: str,
    target_duration_minutes: int | None,
    route_data: dict | None,
) -> float:
    """
    Estimate the CTL needed to comfortably complete an event.
    Based on typical requirements for different event types.
    """
    base_ctl = {
        "road_race": 80,
        "crit": 65,
        "time_trial": 70,
        "gran_fondo": 60,
        "sportive": 50,
        "gravel": 55,
        "mtb": 55,
        "hill_climb": 65,
        "stage_race": 90,
        "charity_ride": 35,
        "century": 50,
    }
    ctl = base_ctl.get(event_type, 50)

    # Adjust for duration (longer events need more base fitness)
    if target_duration_minutes:
        if target_duration_minutes > 360:  # > 6 hours
            ctl += 15
        elif target_duration_minutes > 240:  # > 4 hours
            ctl += 10
        elif target_duration_minutes > 120:  # > 2 hours
            ctl += 5

    # Adjust for elevation
    if route_data and route_data.get("elevation_gain_m"):
        elev = route_data["elevation_gain_m"]
        if elev > 3000:
            ctl += 15
        elif elev > 2000:
            ctl += 10
        elif elev > 1000:
            ctl += 5

    return ctl


def _build_recommendations(
    current_ctl: float,
    target_ctl: float,
    current_tsb: float,
    days_until: int,
    event_type: str,
    ftp: int | None,
    route_data: dict | None,
) -> list[str]:
    """Build a list of actionable recommendations for goal preparation."""
    recs = []

    ctl_gap = target_ctl - current_ctl

    if ctl_gap > 20:
        recs.append(
            f"Your fitness (CTL {current_ctl:.0f}) is {ctl_gap:.0f} points below the "
            f"recommended {target_ctl:.0f} for this event. Focus on consistent training volume."
        )
    elif ctl_gap > 0:
        recs.append(
            f"Your fitness is close to target. Keep training consistently to close the "
            f"{ctl_gap:.0f}-point gap."
        )
    else:
        recs.append("Your fitness level is on target for this event type.")

    if days_until <= 14 and days_until > 0:
        recs.append(
            "You're in the taper window. Reduce volume by 40-60% while keeping intensity. "
            "Focus on rest and nutrition."
        )
    elif days_until <= 7 and days_until > 0:
        recs.append(
            "Final week before event. Very light riding only. Stay off your feet, "
            "hydrate, and prepare equipment."
        )

    if current_tsb < -25:
        recs.append(
            "You're currently very fatigued (TSB below -25). Consider extra rest days "
            "to recover before the event."
        )
    elif current_tsb > 20 and days_until > 14:
        recs.append(
            "Your form is very fresh. You could handle higher training load this week."
        )

    if route_data and route_data.get("elevation_gain_m", 0) > 1500:
        recs.append(
            "This route has significant climbing. Include hill repeats and tempo "
            "climbing in your training."
        )

    if not ftp:
        recs.append(
            "Set your FTP to get more accurate power zone targets and training recommendations."
        )

    return recs


def _infer_experience_level(
    years_cycling: int | None, weekly_hours: float | None
) -> str:
    """Infer rider experience level from quiz answers."""
    years = years_cycling or 0
    hours = weekly_hours or 0

    if years >= 5 and hours >= 12:
        return ExperienceLevel.elite
    elif years >= 3 and hours >= 8:
        return ExperienceLevel.advanced
    elif years >= 1 and hours >= 4:
        return ExperienceLevel.intermediate
    else:
        return ExperienceLevel.beginner
