"""Onboarding quiz, health screening and goal event business logic."""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.onboarding import (
    EventPriority,
    EventType,
    GoalEvent,
    IndoorOutdoorPreference,
    OnboardingResponse,
    PrimaryGoal,
)
from app.models.safety import HealthScreening, SafetyHold
from app.models.training import Workout, WorkoutStatus
from app.models.user import ExperienceLevel, User
from app.services import safety_service as ss


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


# --- Health screening ---
# Eight yes or no questions between the experience and physical steps. A yes
# never stops a rider joining; it changes how the plan starts. The wording
# here is the wording the rider is shown and the wording the consent record
# keeps, so a change to any of it is a new SCREENING_VERSION.

SCREENING_HEADING = "A few health questions first"
SCREENING_INTRO = (
    "Eight yes or no questions. A yes won't stop you joining. It changes how we "
    "start. If something is happening right now, such as chest pain, call {emergency}."
)
SCREENING_QUESTIONS = {
    "q1": "Has a doctor ever told you that you have a heart condition or high blood pressure?",
    "q2": (
        "Do you ever get pain, pressure or tightness in your chest, whether resting, "
        "going about your day or exercising?"
    ),
    "q3": (
        "In the past 12 months, have you fainted, blacked out, or been so dizzy that "
        "you lost your balance?"
    ),
    "q4": (
        "Has a parent, brother or sister died suddenly, or been diagnosed with an "
        "inherited heart condition, before the age of 50?"
    ),
    "q5": (
        "Do you have a long-term condition such as diabetes, asthma or epilepsy, or "
        "take prescribed medicine regularly?"
    ),
    "q6": (
        "Do you have an injury, or a bone, joint or muscle problem that more training "
        "could make worse, or have you had surgery or a concussion in the past three "
        "months?"
    ),
    "q7": "Are you pregnant, or have you had a baby in the past 12 months?",
    "q8": (
        "Has a doctor or other health professional told you to avoid hard exercise, "
        "or to exercise only under supervision?"
    ),
}
# Asked on the experience step. A training question, not health data; it
# feeds the layoff gate.
LONG_BREAK_QUESTION = "Have you had four weeks or more off the bike in the past three months?"


def emergency_number(country: str | None) -> str:
    """The app's rule (emergencyNumber in safety-rules.ts): 999 in the UK
    and when the country is unknown, 911 in the US and Canada, 112
    elsewhere."""
    c = (country or "").upper()
    if not c or c == "GB":
        return "999"
    if c in ("US", "CA"):
        return "911"
    return "112"


def _in_uk(country: str | None) -> bool:
    return (country or "").upper() in ("", "GB")


def screening_text(country: str | None = None) -> str:
    """The exact text of the health step, as the app shows it to a rider in
    `country`, kept on the consent record."""
    return "\n".join(
        [SCREENING_HEADING, SCREENING_INTRO.format(emergency=emergency_number(country))]
        + [f"{i}. {text}" for i, text in enumerate(SCREENING_QUESTIONS.values(), start=1)]
    )


SCREENING_TEXT = screening_text()

# Chest symptoms or fainting hold everything; any other yes keeps it easy.
HOLD_ALL_QUESTIONS = ("q2", "q3")
EASY_ONLY_QUESTIONS = ("q1", "q4", "q5", "q6", "q7", "q8")

# The UK wording is the agreed wording. Outside the UK the emergency number
# is the local one, and the full-hold message swaps NHS 111 and the GP for
# their local equivalents.
SCREENING_MESSAGES = {
    "none": "Thanks. If anything changes, tell me or update this in Settings, then Health.",
    "hold_all": (
        "Thank you for telling me. Chest pain or fainting needs a doctor's view before "
        "you ride, even gently. Please see your {gp} soon, and if it's new or getting "
        "worse, {urgent}. If you have chest pain now, or it lasts more than "
        "a few minutes, call {emergency}. Your plan is built, but every session stays on "
        "hold until you tell me a doctor has cleared you."
    ),
    "easy_only": (
        "Thank you. Please check with your GP, or the doctor, physio or midwife who "
        "knows your situation, that structured training with some hard efforts is "
        "right for you. Until you tell me you've been cleared, I'll keep your plan to "
        "easy and steady riding, with no hard intervals and no fitness tests."
    ),
}
# Shown under the easy_only message, in question order.
SCREENING_EXTRA_LINES = {
    "q5": (
        "If your medicine changes your heart rate, as beta blockers do, heart-rate "
        "zones won't be accurate for you, so I'll coach by power and feel. Never "
        "change or skip medicine to train."
    ),
    "q6": "For an injury, a physiotherapist is the right first stop.{nhs_physio}",
    "q7": (
        "Your midwife or obstetrician should decide how hard you train. Until then "
        "I'll keep things easy, and riding indoors is safer as pregnancy goes on."
    ),
}

# What each yes means, for the hold's reason and red-flag key.
_YES_LABELS = {
    "q1": "heart condition or high blood pressure",
    "q2": "chest pain or tightness",
    "q3": "fainting or dizziness",
    "q4": "family heart history",
    "q5": "long-term condition or medicine",
    "q6": "injury, surgery or concussion",
    "q7": "pregnancy or a recent baby",
    "q8": "told to avoid hard exercise",
}
_RED_FLAGS = {
    "q1": "heart_condition",
    "q2": "chest_pain",
    "q3": "fainting",
    "q4": "family_history",
    "q5": "medication",
    "q6": "injury",
    "q7": "pregnancy",
    "q8": "medical_advice",
}
# Re-screen at least this often (and whenever the question set changes).
RESCREEN_AFTER_DAYS = ss.RESCREEN_AFTER_DAYS


def screening_tier(answers: dict[str, bool]) -> str:
    """hold_all for a yes to Q2 or Q3, easy_only for a yes to any other
    question, otherwise none."""
    if any(answers.get(q) for q in HOLD_ALL_QUESTIONS):
        return "hold_all"
    if any(answers.get(q) for q in EASY_ONLY_QUESTIONS):
        return "easy_only"
    return "none"


def screening_feedback(
    tier: str, answers: dict[str, bool], country: str | None = None
) -> tuple[str, list[str]]:
    """What the rider reads after answering: the tier's message, and the
    extra lines for Q5, Q6 and Q7, which go with the easy_only message only
    (under a full hold the one thing that matters is seeing a doctor)."""
    uk = _in_uk(country)
    words = {
        "gp": "GP" if uk else "doctor",
        "urgent": (
            "call NHS 111 today" if uk
            else "get seen today by your doctor or an out-of-hours service"
        ),
        "emergency": emergency_number(country),
        "nhs_physio": (
            " In many parts of the UK you can refer yourself to NHS physiotherapy."
            if uk else ""
        ),
    }
    extra = (
        [line.format(**words) for q, line in SCREENING_EXTRA_LINES.items() if answers.get(q)]
        if tier == "easy_only"
        else []
    )
    return SCREENING_MESSAGES[tier].format(**words), extra


FORMA_EMAIL = "gareth@ridewithforma.com"

# While an account is held as under 18, Forma takes no health answers from
# it: that would be health data collected from a child. The hold is closed
# by Forma, never by a re-screen, so there is nothing for answers to change.
MINOR_SCREENING_REFUSAL = (
    "This account is on hold because Forma is for adults, 18 and over, so it "
    f"can't take health answers. If you think that's wrong, email {FORMA_EMAIL}."
)


class ScreeningRefused(PermissionError):
    """The rider may not give health answers on this account right now.
    The message is the text they see."""


def screening_closed(db: Session, user: User | str) -> bool:
    """Whether this account may not give health answers: true while it is
    held as under 18."""
    return ss.minor_hold(db, user) is not None


def _hold_reason(tier: str, answers: dict[str, bool]) -> tuple[str, str]:
    questions = HOLD_ALL_QUESTIONS if tier == "hold_all" else EASY_ONLY_QUESTIONS
    yes = [q for q in questions if answers.get(q)]
    return "Health answers: " + "; ".join(_YES_LABELS[q] for q in yes), _RED_FLAGS[yes[0]]


def submit_screening(
    db: Session,
    user: User,
    answers: dict[str, bool],
    long_break: bool,
    request=None,
) -> dict:
    """Store the rider's health answers and act on them.

    The new answers supersede the last ones, and so do their holds: a hold
    from an earlier screening is lifted ("superseded") and the new tier, if
    any yes, opens its own. Holds from anything else (the red-flag detector,
    the coach, an admin) are left alone. A clearance does not carry over:
    new answers with a yes need a new one. Returns what the rider sees
    next: {tier, message, extra_lines, safety}.

    Raises ScreeningRefused, before anything is written, while the account
    is held as under 18."""
    if screening_closed(db, user):
        raise ScreeningRefused(MINOR_SCREENING_REFUSAL)
    missing = [q for q in SCREENING_QUESTIONS if q not in answers]
    if missing:
        raise ValueError(f"Answer every question (missing {', '.join(missing)})")
    answers = {q: bool(answers[q]) for q in SCREENING_QUESTIONS}
    tier = screening_tier(answers)
    now = datetime.utcnow()

    first = (
        db.query(HealthScreening.id).filter(HealthScreening.user_id == user.id).first()
        is None
    )
    for previous in db.query(HealthScreening).filter(
        HealthScreening.user_id == user.id, HealthScreening.superseded_at.is_(None)
    ):
        previous.superseded_at = now
    db.add(HealthScreening(
        user_id=user.id,
        version=ss.SCREENING_VERSION,
        answers=answers,
        long_break=bool(long_break),
        any_yes=any(answers.values()),
        tier=tier,
        created_at=now,
    ))

    for hold in db.query(SafetyHold).filter(
        SafetyHold.user_id == user.id,
        SafetyHold.source == "screening",
        SafetyHold.lifted_at.is_(None),
    ).all():
        ss.lift_hold(
            db, user, hold.id, "superseded", note="Replaced by new health answers.",
            commit=False,
        )
    if tier != "none":
        reason, red_flag = _hold_reason(tier, answers)
        yes = ", ".join(q for q, a in answers.items() if a)
        ss.open_hold(
            db, user, tier, reason, "screening", red_flag=red_flag,
            note=f"Screening {ss.SCREENING_VERSION}. Yes to: {yes}.", commit=False,
        )

    ss.record_consent(
        db, user, "screening", ss.SCREENING_VERSION, screening_text(user.country),
        request=request, source="onboarding" if first else "app", commit=False,
    )
    db.commit()

    # Put the on-hold label on the rider's planned sessions, or take it off,
    # to match. A plan built after this sees the new answers itself.
    from app.services.plan_service import sync_hold_marks

    sync_hold_marks(db, user.id)

    message, extra_lines = screening_feedback(tier, answers, user.country)
    return {
        "tier": tier,
        "message": message,
        "extra_lines": extra_lines,
        "safety": ss.safety_state(db, user),
    }


def get_screening(db: Session, user: User) -> dict:
    """The current question set and the rider's latest answers, for
    Settings, then Health. The answers are None if they've never answered.

    rescreen_due is true when the questions have changed, when the answers
    are a year old, or when a red flag in chat is newer than both the
    answers and the rider's last clearance. Never while the account is held
    as under 18: the app must not ask a child the health questions, and the
    screening endpoint refuses them.

    A clearance closes the red flags before it (a doctor's, a midwife's or a
    physio's, the head check, or a fever gone), so the questions aren't
    asked again only to turn a truthful yes back into a hold. After a
    clinician's clearance or the head check, the yearly re-screen counts
    from the clearance (safety_service.rescreen_quiet_until); a fever gone on
    the rider's word leaves it a year after the answers."""
    closed = screening_closed(db, user)
    latest = ss.latest_screening(db, user.id)
    record = {
        "version": ss.SCREENING_VERSION,
        "heading": SCREENING_HEADING,
        "intro": SCREENING_INTRO.format(emergency=emergency_number(user.country)),
        "questions": [{"id": q, "text": text} for q, text in SCREENING_QUESTIONS.items()],
        "long_break_question": LONG_BREAK_QUESTION,
        "clearance_text": ss.CLEARANCE_TEXT,
        "answers": None,
        "long_break": None,
        "tier": None,
        "answered_at": None,
        "answered_version": None,
        "clearance_confirmed": False,
        "clearance_by": None,
        "clearance_limits": None,
        "rescreen_due": not closed,
    }
    if latest is None:
        return record

    # A red flag in chat since these answers means they may be out of date,
    # unless the rider marked that hold a mistake, or a clearance since has
    # dealt with it (the red flags count from the later of the answers and
    # the last clearance). An under-18 flag is about age, not health: it says
    # nothing about the answers, so an adult whose wrong minor hold Forma
    # lifts is not sent back through the questions. Nor do the easy days a
    # fever or head-injury lift leaves behind: they follow a clearance, and
    # they end by themselves.
    now = ss._now()
    red_flag_since = (
        db.query(SafetyHold.id)
        .filter(
            SafetyHold.user_id == user.id,
            SafetyHold.source.in_(("detector", "coach_tool")),
            SafetyHold.opened_at > ss.rescreen_red_flags_after(db, user, latest),
            SafetyHold.expires_at.is_(None),
            or_(SafetyHold.lifted_how.is_(None), SafetyHold.lifted_how != "mistake"),
            or_(SafetyHold.red_flag.is_(None), SafetyHold.red_flag != "minor"),
        )
        .first()
        is not None
    )
    a_year_on = (
        ss.rescreen_quiet_until(db, user) is None
        and latest.created_at <= now - timedelta(days=RESCREEN_AFTER_DAYS)
    )
    record.update(
        answers={q: bool((latest.answers or {}).get(q)) for q in SCREENING_QUESTIONS},
        long_break=latest.long_break,
        tier=latest.tier,
        answered_at=latest.created_at,
        answered_version=latest.version,
        clearance_confirmed=latest.clearance_confirmed_at is not None,
        clearance_by=latest.clearance_by,
        clearance_limits=latest.clearance_limits,
        rescreen_due=not closed and (
            latest.version != ss.SCREENING_VERSION or a_year_on or red_flag_since
        ),
    )
    return record


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
