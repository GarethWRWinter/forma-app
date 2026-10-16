"""
Training plan generation service.

Generates truly personalised training plans based on:
- Goal event (date, type, priority, terrain)
- Current fitness (CTL, ATL, TSB)
- Rider profile (strengths, weaknesses)
- Experience level and available hours
- Progressive overload with recovery weeks
"""

from datetime import date, datetime, timedelta
from math import ceil, sqrt

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.llm_utils import humanize

from app.core.workout_templates import (
    PHASE_WORKOUT_MIX,
    WORKOUT_TEMPLATES,
    estimate_tss,
    get_template,
)
from app.models.onboarding import EventPriority, GoalEvent, GoalStatus
from app.models.ride import Ride
from app.models.training import (
    PeriodizationModel,
    PhaseType,
    PlanStatus,
    TrainingPhase,
    TrainingPlan,
    Workout,
    WorkoutStatus,
    WorkoutStep,
    WorkoutType,
)
from app.models.user import User
from app.services.metrics_service import get_current_fitness


# --- Goal-specific workout emphasis ---
# Maps event type to workout type weights for build/peak phases.
# Higher weight = more sessions of that type scheduled.
GOAL_WORKOUT_EMPHASIS = {
    "time_trial": {
        "threshold": 3, "sweet_spot": 2, "endurance": 2, "vo2max": 1, "tempo": 1, "recovery": 1,
    },
    "hill_climb": {
        "vo2max": 2, "threshold": 3, "sweet_spot": 2, "endurance": 2, "recovery": 1,
    },
    "road_race": {
        "vo2max": 2, "threshold": 2, "sweet_spot": 1, "endurance": 2, "sprint": 1, "recovery": 1,
    },
    "crit": {
        "vo2max": 2, "sprint": 2, "threshold": 2, "endurance": 1, "recovery": 1,
    },
    "sportive": {
        "endurance": 3, "sweet_spot": 2, "tempo": 2, "threshold": 1, "recovery": 1,
    },
    "gran_fondo": {
        "endurance": 3, "sweet_spot": 2, "tempo": 2, "threshold": 1, "recovery": 1,
    },
    "gravel": {
        "endurance": 3, "sweet_spot": 2, "tempo": 1, "threshold": 1, "recovery": 1,
    },
    "mtb": {
        "vo2max": 2, "endurance": 2, "sweet_spot": 1, "threshold": 1, "sprint": 1, "recovery": 1,
    },
    "stage_race": {
        "endurance": 2, "threshold": 2, "sweet_spot": 2, "vo2max": 1, "recovery": 1,
    },
    "century": {
        "endurance": 4, "sweet_spot": 2, "tempo": 2, "recovery": 1,
    },
    "charity_ride": {
        "endurance": 3, "sweet_spot": 1, "tempo": 1, "recovery": 1,
    },
}

# Default emphasis when no goal or unknown type
DEFAULT_EMPHASIS = {
    "endurance": 2, "sweet_spot": 2, "threshold": 1, "tempo": 1, "recovery": 1,
}

# A plan built this soon after an A or B race opens with a recovery week.
RECOVERY_DAYS = 7

# Priority ranking — lower is more important
PRIORITY_RANK = {
    EventPriority.a_race: 1,
    "a_race": 1,
    EventPriority.b_race: 2,
    "b_race": 2,
    EventPriority.c_race: 3,
    "c_race": 3,
}


def _rank_goal(goal: GoalEvent) -> tuple[int, date]:
    """Sort key: highest priority first, then soonest date."""
    return (PRIORITY_RANK.get(goal.priority, 9), goal.event_date)


def _blend_emphasis(
    goals: list[GoalEvent],
) -> dict[str, int]:
    """
    Blend workout emphasis across multiple goals weighted by priority.

    A-race gets full weight (1.0), B-race gets 0.4, C-race gets 0.
    """
    weights_by_priority = {"a_race": 1.0, "b_race": 0.4, "c_race": 0.0}
    merged: dict[str, float] = {}

    for goal in goals:
        w = weights_by_priority.get(str(goal.priority), 0.0)
        if w == 0:
            continue
        emphasis = GOAL_WORKOUT_EMPHASIS.get(goal.event_type, DEFAULT_EMPHASIS)
        for wtype, val in emphasis.items():
            merged[wtype] = merged.get(wtype, 0) + val * w

    if not merged:
        return dict(DEFAULT_EMPHASIS)

    # Normalise back to integer weights (round, min 1 for non-zero)
    max_val = max(merged.values()) or 1
    result = {}
    for wtype, val in merged.items():
        normalised = round(val / max_val * 4)
        if normalised > 0:
            result[wtype] = max(1, normalised)

    return result or dict(DEFAULT_EMPHASIS)


def _get_recovery_cycle(experience: str | None) -> int:
    """How many hard weeks before a recovery week."""
    if experience in ("beginner", None):
        return 2  # 2 hard + 1 easy
    if experience == "intermediate":
        return 3  # 3 hard + 1 easy
    return 3  # advanced/elite: 3 hard + 1 easy (but higher load)


def _ramp_rate_tss_per_week(experience: str | None) -> float:
    """Weekly TSS increase during hard weeks."""
    if experience in ("beginner", None):
        return 15
    if experience == "intermediate":
        return 25
    return 35  # advanced/elite


def _starting_weekly_tss(current_ctl: float, current_tsb: float) -> float:
    """
    Determine starting weekly TSS based on current fitness.
    Weekly TSS ≈ CTL × 7 (CTL is daily average stress).
    If very fatigued (TSB < -20), start lower.
    """
    base_tss = max(100, current_ctl * 7)

    if current_tsb < -30:
        # Very fatigued — start with a recovery-level week
        return base_tss * 0.6
    if current_tsb < -15:
        # Moderately fatigued — ease in
        return base_tss * 0.8

    return base_tss


def _days_per_week(weekly_hours: float, experience: str | None) -> int:
    """Determine training days per week based on available hours."""
    if weekly_hours <= 4:
        return 3
    if weekly_hours <= 6:
        return 4
    if weekly_hours <= 10:
        return 5
    return 6


# --- The long ride ---
# Every event that takes longer than an hour is decided by the weekly long
# ride, and the engine used to have none: an 8-hour Fred Whitton plan topped
# out at 2 hours, because each session's length came from an even share of
# the week's TSS. Found in the launch audit, 4 Oct 2026, on a test rider.

# Events where duration is the limiter, so the long ride grows towards them.
LONG_EVENT_TYPES = {"sportive", "gran_fondo", "gravel", "road_race", "mtb", "stage_race"}
# When the rider gives no target duration, a typical finishing time.
DEFAULT_EVENT_MINUTES = {
    "sportive": 300, "gran_fondo": 300, "gravel": 300,
    "road_race": 180, "mtb": 180, "stage_race": 240,
}
# Road racers ride at least race distance in training; sportive riders build
# to three-quarters of theirs and let the day's adrenaline carry the rest.
LONG_RIDE_RATIO = {"road_race": 1.0, "stage_race": 1.0}
LONG_RIDE_MAX_S = 5 * 3600
LONG_RIDE_MIN_S = 90 * 60


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value) or "")


def _long_ride_cap_seconds(goal: GoalEvent | None, weekly_hours: float) -> int:
    """How long the weekly long ride grows to over the plan.

    Three-quarters of the event's duration is the classic target: a rider who
    can ride 6 hours steadily can finish an 8-hour sportive on the day. Road
    racers ride the full race duration. Short events (crits, time trials, hill
    climbs) are not decided by duration, so their long ride stops at 2 hours.
    Never more than 55% of the rider's week, never more than 5 hours, never
    less than 90 minutes.
    """
    if goal is None:
        cap = 150 * 60
    else:
        etype = _enum_value(goal.event_type)
        if etype in LONG_EVENT_TYPES:
            minutes = goal.target_duration_minutes or DEFAULT_EVENT_MINUTES.get(etype, 180)
            cap = minutes * 60 * LONG_RIDE_RATIO.get(etype, 0.75)
        else:
            cap = 2 * 3600
    cap = min(cap, LONG_RIDE_MAX_S, weekly_hours * 3600 * 0.55)
    return int(max(cap, LONG_RIDE_MIN_S))


def _round_quarter_hour(seconds: float) -> int:
    return int(round(seconds / 900) * 900)


def _long_ride_seconds(
    cap_s: int,
    phase_type: str,
    cumulative_week: int,
    is_recovery: bool,
    experience: str | None,
) -> int:
    """This week's long ride: 15 minutes longer each week up to the cap
    (10 for beginners), held at the cap in the peak, cut by 40% in recovery
    weeks."""
    beginner = experience in ("beginner", None)
    start = 75 * 60 if beginner else LONG_RIDE_MIN_S
    step = 600 if beginner else 900
    grown = min(cap_s, start + step * cumulative_week)
    if phase_type == "peak":
        grown = cap_s
    if is_recovery:
        grown = grown * 0.6
    return max(3600, _round_quarter_hour(grown))


def _long_ride_template(duration_s: int, event_name: str | None = None) -> dict:
    """A Zone 2 ride of exactly the length the week calls for."""
    hours, minutes = divmod(duration_s // 60, 60)
    length = f"{hours} h {minutes:02d}" if minutes else f"{hours} h"
    purpose = (
        f"This is the ride that gets you round {event_name}."
        if event_name
        else "This is the ride that builds the engine everything else runs on."
    )
    warmup, cooldown = 900, 300
    return {
        "name": "Long Ride",
        "workout_type": "endurance",
        "description": (
            f"The long ride: {length} at an easy, conversational effort (Zone 2, "
            f"a pace you could hold a chat at). {purpose} Eat something every "
            "30 minutes from the start, and finish feeling you could have gone on."
        ),
        "duration_seconds": duration_s,
        "planned_if": 0.65,
        "steps": [
            {"step_type": "warmup", "duration_seconds": warmup, "power_target_pct": 0.55},
            {
                "step_type": "steady_state",
                "duration_seconds": duration_s - warmup - cooldown,
                "power_target_pct": 0.68,
                "power_low_pct": 0.56,
                "power_high_pct": 0.75,
                "cadence_target": 88,
            },
            {"step_type": "cooldown", "duration_seconds": cooldown, "power_target_pct": 0.50},
        ],
    }


def _pick_long_ride_day(
    available: list[tuple[int, date]],
    selected_days: list[tuple[int, date]],
    hard_days: set[int],
    actual_days: int,
) -> tuple[tuple[int, date], list[tuple[int, date]]]:
    """Saturday if the rider can ride it, then Sunday, then their latest free
    day. Returns the day and the week's training days with it included."""
    by_dow = {slot[0]: slot for slot in available}
    chosen = by_dow.get(5) or by_dow.get(6) or max(available, key=lambda s: s[0])
    days = list(selected_days)
    if chosen not in days:
        if len(days) < actual_days:
            days.append(chosen)
        else:
            # Give up an easy day before a hard one.
            easy = [d for d in days if d[0] not in hard_days]
            days.remove(easy[-1] if easy else days[-1])
            days.append(chosen)
    days.sort(key=lambda x: x[0])
    return chosen, days


def _build_weekly_workout_types(
    phase_type: str,
    days: int,
    goal_emphasis: dict[str, int],
    is_recovery_week: bool = False,
) -> list[str]:
    """
    Build a workout type list for a week, tailored to phase and goal.
    Recovery weeks shift to more endurance/recovery.
    """
    if is_recovery_week:
        # Recovery week: easy volume, no intensity
        types = ["recovery", "endurance"]
        while len(types) < days:
            types.append("endurance" if len(types) % 2 == 0 else "recovery")
        return types[:days]

    # Phase-specific base distribution
    if phase_type == "base":
        # Base: mostly endurance with some tempo/sweet spot sneaking in
        pool = {"endurance": 3, "tempo": 1, "recovery": 1}
        # Slightly influenced by goal even in base
        for wtype in ("sweet_spot", "tempo"):
            if wtype in goal_emphasis and goal_emphasis[wtype] >= 2:
                pool[wtype] = pool.get(wtype, 0) + 1
    elif phase_type == "build":
        # Build: goal-specific intensity ramps up
        pool = dict(goal_emphasis)
    elif phase_type == "peak":
        # Peak: high intensity, lower volume — sharpen
        pool = {}
        for wtype, weight in goal_emphasis.items():
            if wtype in ("vo2max", "threshold", "sprint"):
                pool[wtype] = weight + 1  # boost intensity types
            elif wtype == "recovery":
                pool[wtype] = weight + 1  # more recovery in peak
            else:
                pool[wtype] = max(1, weight - 1)  # reduce volume types
    elif phase_type == "race":
        # Race week: taper — openers + recovery
        pool = {"recovery": 3, "vo2max": 1, "endurance": 1}
    else:
        pool = {"endurance": 2, "recovery": 1}

    # Expand pool into ordered list (weighted)
    expanded = []
    for wtype, weight in pool.items():
        expanded.extend([wtype] * weight)

    # Select days worth of types, prioritising variety
    selected = []
    # Always start week with endurance if available
    if "endurance" in expanded and phase_type != "race":
        selected.append("endurance")
        expanded.remove("endurance")

    # Fill remaining days, avoiding consecutive same types
    while len(selected) < days and expanded:
        # Prefer types not recently added
        last = selected[-1] if selected else None
        candidates = [t for t in expanded if t != last]
        if not candidates:
            candidates = expanded
        pick = candidates[0]
        selected.append(pick)
        expanded.remove(pick)

    # If still short, pad with endurance/recovery
    while len(selected) < days:
        selected.append("endurance" if len(selected) % 3 != 0 else "recovery")

    # Always end week with recovery if we have 4+ days
    if days >= 4 and selected[-1] != "recovery":
        # Swap last non-recovery with recovery at end
        if "recovery" in selected:
            selected.remove("recovery")
            selected.append("recovery")
        else:
            selected[-1] = "recovery"

    return selected[:days]


def generate_plan(
    db: Session,
    user: User,
    goal_event_id: str | None = None,
    periodization_model: str = "traditional",
    name: str | None = None,
    recover_first: bool | None = None,
) -> TrainingPlan:
    """
    Generate a personalised periodized training plan that considers ALL
    upcoming goals, prioritised by A/B/C race classification.

    - A-race: drives the macro periodization (phases, peak, taper)
    - B-race: gets a 3-day mini-taper and blends into workout emphasis
    - C-race: rest day on event date, no other accommodation

    Adapts to current fitness, experience, available hours, and goal types.
    recover_first: the first seven days are a recovery week. Left as None,
    that happens when an A or B race was ridden in the past week, whoever
    builds the plan: the season keeper the morning after, a new goal, or
    Build my season. A base block once opened with tempo the day after the
    race.
    """
    from app.services.season_service import lock_rider

    today = date.today()
    # One build at a time per rider: the season keeper, a goal write and
    # Build my season pressed at once would otherwise each find no active
    # plan and leave the rider with two.
    lock_rider(db, user)

    # ── 1. End any existing active plans ──
    # Ended, not cancelled: the weeks already ridden stay on the calendar.
    existing_active = (
        db.query(TrainingPlan)
        .filter(
            TrainingPlan.user_id == user.id,
            TrainingPlan.status == PlanStatus.active,
        )
        .all()
    )
    for old_plan in existing_active:
        retire_plan(db, old_plan, today)
    if existing_active:
        db.flush()

    if recover_first is None:
        recover_first = db.query(GoalEvent.id).filter(
            GoalEvent.user_id == user.id,
            GoalEvent.event_date >= today - timedelta(days=RECOVERY_DAYS),
            GoalEvent.event_date < today,
            GoalEvent.priority.in_([EventPriority.a_race, EventPriority.b_race]),
            GoalEvent.status != GoalStatus.dns,
        ).first() is not None

    # ── 2. Gather ALL upcoming goals, sorted by priority then date ──
    all_goals = (
        db.query(GoalEvent)
        .filter(
            GoalEvent.user_id == user.id,
            GoalEvent.event_date >= today,
            GoalEvent.status == GoalStatus.upcoming,
        )
        .all()
    )
    all_goals.sort(key=_rank_goal)

    # Determine primary goal
    primary_goal = None
    if goal_event_id:
        # Explicit goal_event_id takes precedence (backward compat)
        primary_goal = next((g for g in all_goals if g.id == goal_event_id), None)
    if not primary_goal and all_goals:
        # Auto-detect: highest priority, then soonest
        primary_goal = all_goals[0]

    # Plan end date = primary goal event date, or 12 weeks
    if primary_goal:
        end_date = primary_goal.event_date
    else:
        end_date = today + timedelta(weeks=12)

    # If there are goals AFTER the primary, extend the plan to cover them
    # but only if the primary is an A-race (we don't extend for B/C)
    latest_goal_date = max((g.event_date for g in all_goals), default=end_date)
    if latest_goal_date > end_date:
        end_date = latest_goal_date

    weeks_available = max(4, (end_date - today).days // 7)

    # Current fitness
    fitness = get_current_fitness(db, user.id)
    current_ctl = fitness["ctl"]
    current_tsb = fitness["tsb"]

    # User context
    ftp = user.ftp or 200
    weekly_hours = user.weekly_hours_available or 6
    experience = user.experience_level or "intermediate"

    # ── 3. Blend workout emphasis across goals by priority ──
    goals_in_plan = [g for g in all_goals if g.event_date <= end_date]
    if goals_in_plan:
        goal_emphasis = _blend_emphasis(goals_in_plan)
    elif primary_goal:
        goal_emphasis = GOAL_WORKOUT_EMPHASIS.get(
            primary_goal.event_type, DEFAULT_EMPHASIS
        )
    else:
        goal_emphasis = dict(DEFAULT_EMPHASIS)

    # ── 4. Collect event dates + B-race mini-taper windows ──
    goal_event_dates = {g.event_date for g in all_goals}
    # A race today stays clear even once its result is filed.
    if db.query(GoalEvent.id).filter(
        GoalEvent.user_id == user.id, GoalEvent.event_date == today
    ).first():
        goal_event_dates.add(today)
    # Sessions with no phase survive a rebuild, because they belong to no
    # plan: the ones the rider booked with the coach, and the ones the old
    # plan handed over (rewritten or ridden, see retire_plan). Their days are
    # left clear so the new plan never lays a second session, or the long
    # ride, on top of one.
    booked_dates = {
        d for (d,) in db.query(Workout.scheduled_date).filter(
            Workout.user_id == user.id,
            Workout.phase_id.is_(None),
            Workout.scheduled_date >= today,
            Workout.status.in_([
                WorkoutStatus.planned, WorkoutStatus.modified, WorkoutStatus.completed,
            ]),
        )
    }
    # Already ridden today, planned or not: today's work is done.
    if db.query(Ride.id).filter(
        Ride.user_id == user.id,
        Ride.ride_date >= datetime.combine(today, datetime.min.time()),
        Ride.ride_date <= datetime.combine(today, datetime.max.time()),
    ).first():
        booked_dates.add(today)
    # B-race mini-taper: reduce volume 3 days before
    b_race_taper_dates: set[date] = set()
    for g in all_goals:
        if str(g.priority) == "b_race":
            for d in range(1, 4):
                b_race_taper_dates.add(g.event_date - timedelta(days=d))

    # ── 5. Plan naming ──
    if name is None:
        if primary_goal:
            other_count = len(goals_in_plan) - 1
            if other_count > 0:
                name = f"Plan for {primary_goal.event_name} (+{other_count} {'race' if other_count == 1 else 'races'})"
            else:
                name = f"Plan for {primary_goal.event_name}"
        else:
            # No dashes in customer-visible copy, and this is the plan name
            # every rider without a goal reads on their first dashboard.
            name = f"Training Plan, {today.strftime('%b %Y')}"

    # ── 6. Create plan ──
    # Link to primary goal for backward compat
    plan = TrainingPlan(
        user_id=user.id,
        name=name,
        goal_event_id=primary_goal.id if primary_goal else None,
        start_date=today,
        end_date=end_date,
        status=PlanStatus.active,
        periodization_model=periodization_model,
    )
    db.add(plan)
    db.flush()

    # ── 7. Build phases targeting primary goal ──
    # A rider with no goal has no race to taper for: no peak, no race week.
    phases = _build_phases(
        weeks_available, today, end_date, periodization_model,
        has_event=primary_goal is not None,
        long_event=bool(
            primary_goal and _enum_value(primary_goal.event_type) in LONG_EVENT_TYPES
        ),
    )
    long_ride_cap_s = _long_ride_cap_seconds(primary_goal, weekly_hours)

    # ── 8. Calculate progressive TSS targets ──
    starting_tss = _starting_weekly_tss(current_ctl, current_tsb)
    ramp_per_week = _ramp_rate_tss_per_week(experience)
    recovery_cycle = _get_recovery_cycle(experience)
    training_days = _days_per_week(weekly_hours, experience)

    # ── 9. Generate workouts per phase ──
    sort_order = 0
    cumulative_week = 0

    for phase_def in phases:
        phase = TrainingPhase(
            plan_id=plan.id,
            phase_type=phase_def["type"],
            start_date=phase_def["start_date"],
            end_date=phase_def["end_date"],
            target_weekly_hours=weekly_hours,
            focus=phase_def.get("focus"),
            sort_order=sort_order,
        )
        db.add(phase)
        db.flush()
        sort_order += 1

        # Generate fitness-aware workouts
        cumulative_week = _generate_adaptive_workouts(
            db=db,
            phase=phase,
            user=user,
            ftp=ftp,
            weekly_hours=weekly_hours,
            training_days=training_days,
            starting_tss=starting_tss,
            ramp_per_week=ramp_per_week,
            recovery_cycle=recovery_cycle,
            goal_emphasis=goal_emphasis,
            goal_event_dates=goal_event_dates,
            b_race_taper_dates=b_race_taper_dates,
            booked_dates=booked_dates,
            recover_first=recover_first and phase_def is phases[0],
            cumulative_week=cumulative_week,
            long_ride_cap_s=long_ride_cap_s,
            experience=experience,
            event_name=primary_goal.event_name if primary_goal else None,
        )

    db.commit()
    db.refresh(plan)
    return plan


def retire_plan(db: Session, plan: TrainingPlan, today: date) -> None:
    """End a plan and keep what the rider already rode on the calendar.

    A plan that has run for at least a day is completed, ending yesterday at
    the latest, so its past weeks stay where they were ridden and nothing of
    it lies on today or after. One that started today has no past to keep and
    is cancelled. Rebuilding used to cancel outright, which blanked every past
    week of the old plan. Its unanswered proposals go with it: they argue with
    a plan that no longer exists.

    Sessions from today on that the rider has made their own stay on the
    calendar: one the coach or an accepted proposal rewrote or moved
    (modified), and one already ridden (completed). They leave the plan and
    stand on their own, like a session booked in chat, and the next plan
    leaves their days clear. A rebuild used to hide a session the coach had
    just rewritten, and today's ride along with the session it was matched
    to.
    """
    from app.services.plan_review_service import _supersede_pending

    kept = (
        db.query(Workout)
        .join(TrainingPhase, Workout.phase_id == TrainingPhase.id)
        .filter(
            TrainingPhase.plan_id == plan.id,
            Workout.scheduled_date >= today,
            Workout.status.in_([WorkoutStatus.modified, WorkoutStatus.completed]),
        )
        .all()
    )
    for w in kept:
        w.phase_id = None
    if plan.start_date < today:
        plan.status = PlanStatus.completed
        plan.end_date = min(plan.end_date, today - timedelta(days=1))
    else:
        plan.status = PlanStatus.cancelled
    _supersede_pending(db, plan.user, plan.id)


def _generate_adaptive_workouts(
    db: Session,
    phase: TrainingPhase,
    user: User,
    ftp: int,
    weekly_hours: float,
    training_days: int,
    starting_tss: float,
    ramp_per_week: float,
    recovery_cycle: int,
    goal_emphasis: dict[str, int],
    goal_event_dates: set,
    cumulative_week: int,
    b_race_taper_dates: set[date] | None = None,
    booked_dates: set[date] | None = None,
    recover_first: bool = False,
    long_ride_cap_s: int | None = None,
    experience: str | None = None,
    event_name: str | None = None,
) -> int:
    """
    Generate workouts for a phase with progressive overload and recovery weeks.
    Returns the updated cumulative_week count.
    """
    start = phase.start_date
    end = phase.end_date
    if isinstance(start, str):
        start = date.fromisoformat(start)
    if isinstance(end, str):
        end = date.fromisoformat(end)

    phase_type = phase.phase_type
    current_week_start = start
    workout_sort_order = 0

    # Phase-specific TSS multipliers
    phase_tss_mult = {
        "base": 0.85,       # lower intensity
        "build": 1.0,       # full progression
        "peak": 0.9,        # slightly reduced volume, higher intensity
        "race": 0.5,        # taper
        "recovery": 0.5,
        "off_season": 0.6,
    }
    mult = phase_tss_mult.get(phase_type, 1.0)

    while current_week_start <= end:
        week_end = min(current_week_start + timedelta(days=6), end)
        days_in_week = (week_end - current_week_start).days + 1

        # Is this a recovery week? The plan's first week is one when it
        # comes straight after a race.
        is_recovery = (
            cumulative_week > 0
            and cumulative_week % (recovery_cycle + 1) == recovery_cycle
            and phase_type not in ("race", "recovery")
        ) or (recover_first and current_week_start == start)

        # Calculate target weekly TSS with progression
        if is_recovery:
            target_weekly_tss = starting_tss * 0.6  # 60% of base for recovery
        else:
            target_weekly_tss = (starting_tss + ramp_per_week * cumulative_week) * mult

        # Update phase target
        phase.target_weekly_tss = round(target_weekly_tss, 1)

        # Determine workout types for this week
        actual_days = min(training_days, days_in_week)
        workout_types = _build_weekly_workout_types(
            phase_type, actual_days, goal_emphasis, is_recovery,
        )

        # Distribute TSS across workouts (intensity sessions get more TSS)
        intensity_types = {"threshold", "vo2max", "sweet_spot", "sprint"}
        tss_weights = []
        for wtype in workout_types:
            if wtype in intensity_types:
                tss_weights.append(1.3)
            elif wtype == "recovery":
                tss_weights.append(0.5)
            else:
                tss_weights.append(1.0)
        total_weight = sum(tss_weights)

        # ── Schedule workouts respecting user day preferences ──
        hard_days = set(user.preferred_hard_days or [])
        user_rest_days = set(user.rest_days or [])
        intensity_types = {"threshold", "vo2max", "sweet_spot", "sprint"}

        # 1. Build available days (exclude rest days and race days)
        available = []
        taper_set = b_race_taper_dates or set()
        for d in range(days_in_week):
            day_date = current_week_start + timedelta(days=d)
            dow = day_date.weekday()
            if dow in user_rest_days:
                continue
            if goal_event_dates and day_date in goal_event_dates:
                continue
            if booked_dates and day_date in booked_dates:
                continue
            if day_date > end:
                continue
            available.append((dow, day_date))

        if not available:
            cumulative_week += 1
            current_week_start += timedelta(weeks=1)
            continue

        # 2. Pick which days to train (up to actual_days)
        #    Prioritise hard days (always include them), fill rest with easy days
        hard_slots = [a for a in available if a[0] in hard_days]
        easy_slots = [a for a in available if a[0] not in hard_days]
        selected_days = hard_slots[:]  # always include all hard days
        remaining_needed = actual_days - len(selected_days)
        if remaining_needed > 0:
            # Spread easy days evenly
            step_sz = max(1, len(easy_slots) // remaining_needed) if remaining_needed else 1
            selected_days += easy_slots[::step_sz][:remaining_needed]
        selected_days.sort(key=lambda x: x[0])  # sort by day of week
        selected_days = selected_days[:actual_days]

        # 3. Split workout types into hard and easy
        hard_types = [wt for wt in workout_types if wt in intensity_types]
        easy_types = [wt for wt in workout_types if wt not in intensity_types]

        # 3b. The long ride: one per week, on the weekend where possible. Race
        #     week has none (the event is the long ride), and the other taper
        #     week keeps a shortened one so the legs remember the distance.
        next_event = min(
            (d for d in (goal_event_dates or set()) if d >= current_week_start),
            default=None,
        )
        long_s = None
        long_day = None
        if long_ride_cap_s:
            if phase_type in ("race", "recovery", "off_season"):
                # Only a taper week that ends a full week clear of the event
                # gets one; a Saturday long ride before a Sunday sportive is
                # the mistake this guards against.
                if phase_type == "race" and (
                    next_event is None or (next_event - week_end).days >= 6
                ):
                    long_s = max(3600, _round_quarter_hour(long_ride_cap_s * 0.6))
            else:
                long_s = _long_ride_seconds(
                    long_ride_cap_s, phase_type, cumulative_week, is_recovery, experience
                )
        if long_s:
            long_day, selected_days = _pick_long_ride_day(
                available, selected_days, hard_days, actual_days
            )
            # It takes the place of one easy session, never an extra day.
            if "endurance" in easy_types:
                easy_types.remove("endurance")
            elif easy_types:
                easy_types.pop()
            elif hard_types:
                hard_types.pop()

        # 4. Assign: hard types to hard days, easy types to easy days
        ordered = []
        for dow, day in selected_days:
            if long_day and day == long_day[1]:
                ordered.append("endurance")
            elif dow in hard_days and hard_types:
                ordered.append(hard_types.pop(0))
            elif easy_types:
                ordered.append(easy_types.pop(0))
            elif hard_types:
                ordered.append(hard_types.pop(0))
            else:
                ordered.append("endurance")

        # 5. Calculate TSS weights. The long ride's stress comes off the top;
        #    the other sessions share what is left of the week.
        long_tss = 0.0
        long_template = None
        if long_day:
            long_template = _long_ride_template(long_s, event_name)
            long_tss = estimate_tss(long_template, ftp)
        shared_tss = (
            max(target_weekly_tss - long_tss, target_weekly_tss * 0.4)
            if long_day else target_weekly_tss
        )
        tss_weights = []
        for (dow, day), wt in zip(selected_days, ordered):
            if long_day and day == long_day[1]:
                tss_weights.append(0.0)
            elif wt in intensity_types:
                tss_weights.append(1.3)
            elif wt == "recovery":
                tss_weights.append(0.5)
            else:
                tss_weights.append(1.0)
        total_weight = sum(tss_weights) or 1

        for slot_idx, (dow, workout_day) in enumerate(selected_days):
            if slot_idx >= len(ordered):
                break
            wtype = ordered[slot_idx]

            # B-race mini-taper: swap hard workouts to easy on taper days
            if workout_day in taper_set and wtype in intensity_types:
                wtype = "endurance"

            # Race week: nothing over an hour, and nothing hard in the last
            # two days before the event.
            days_to_event = (next_event - workout_day).days if next_event else None
            in_race_week = phase_type == "race" and days_to_event is not None and 0 < days_to_event <= 6
            if in_race_week and days_to_event <= 2 and wtype in intensity_types:
                wtype = "recovery"

            if long_day and workout_day == long_day[1]:
                template = long_template
                if workout_day in taper_set:
                    template = _long_ride_template(
                        max(3600, _round_quarter_hour(long_s * 0.6)), event_name
                    )
                planned_tss = estimate_tss(template, ftp)
                workout = Workout(
                    phase_id=phase.id,
                    user_id=user.id,
                    scheduled_date=workout_day,
                    title=template["name"],
                    description=template["description"],
                    workout_type=wtype,
                    planned_duration_seconds=template["duration_seconds"],
                    planned_tss=round(planned_tss, 1),
                    planned_if=template.get("planned_if"),
                    status=WorkoutStatus.planned,
                    sort_order=workout_sort_order,
                )
                db.add(workout)
                db.flush()
                workout_sort_order += 1
                _create_workout_steps(db, workout, template)
                continue

            # Calculate per-workout TSS target
            workout_tss_target = shared_tss * (tss_weights[slot_idx] / total_weight)

            # Reduce TSS on B-race mini-taper days (70% volume)
            if workout_day in taper_set:
                workout_tss_target *= 0.7

            # Pick template closest to TSS target (via duration hint)
            # TSS ≈ (hours) × IF² × 100, so target_hours ≈ TSS / (IF² × 100)
            template = get_template(wtype)
            planned_if = template.get("planned_if", 0.65)
            target_duration_s = int(workout_tss_target / (planned_if ** 2 * 100) * 3600)
            # Clamp to reasonable bounds
            target_duration_s = max(1800, min(target_duration_s, int(weekly_hours * 3600 * 0.4)))
            if in_race_week:
                target_duration_s = min(target_duration_s, 3600)
            # With a long ride in the week, the other sessions share what is
            # left of the rider's hours rather than adding to them.
            if long_day:
                others = max(1, len(selected_days) - 1)
                budget_s = (weekly_hours * 3600 - long_s) / others
                target_duration_s = max(1800, min(target_duration_s, int(budget_s * 1.15)))

            template = get_template(wtype, duration_hint=target_duration_s)
            planned_tss = estimate_tss(template, ftp)

            workout = Workout(
                phase_id=phase.id,
                user_id=user.id,
                scheduled_date=workout_day,
                title=template["name"],
                description=template.get("description", ""),
                workout_type=wtype,
                planned_duration_seconds=template["duration_seconds"],
                planned_tss=round(planned_tss, 1),
                planned_if=template.get("planned_if"),
                status=WorkoutStatus.planned,
                sort_order=workout_sort_order,
            )
            db.add(workout)
            db.flush()
            workout_sort_order += 1

            _create_workout_steps(db, workout, template)

        cumulative_week += 1
        current_week_start += timedelta(weeks=1)

    return cumulative_week


def _build_phases(
    weeks_available: int,
    start_date: date,
    end_date: date,
    model: str,
    has_event: bool = True,
    long_event: bool = False,
) -> list[dict]:
    """
    Determine training phases based on weeks available and periodization model.

    Traditional: base -> build -> peak -> race -> recovery
    Polarized: base(long) -> build(short) -> peak -> race
    Sweet Spot: base(sweet spot focus) -> build -> peak -> race
    """
    phases = []
    current_date = start_date

    if weeks_available <= 4:
        mid = current_date + timedelta(weeks=weeks_available // 2)
        phases.append({
            "type": PhaseType.build,
            "start_date": current_date,
            "end_date": mid - timedelta(days=1),
            "focus": "Build fitness quickly with goal-specific intensity",
        })
        phases.append({
            "type": PhaseType.peak,
            "start_date": mid,
            "end_date": end_date,
            "focus": "Sharpen form: maintain intensity, reduce volume",
        })
        return phases

    if weeks_available <= 8:
        base_weeks = max(2, weeks_available // 3)
        build_weeks = max(2, (weeks_available - base_weeks) // 2)
        peak_weeks = weeks_available - base_weeks - build_weeks

        d1 = current_date + timedelta(weeks=base_weeks)
        d2 = d1 + timedelta(weeks=build_weeks)

        phases.append({
            "type": PhaseType.base,
            "start_date": current_date,
            "end_date": d1 - timedelta(days=1),
            "focus": "Build aerobic foundation and establish training consistency",
        })
        phases.append({
            "type": PhaseType.build,
            "start_date": d1,
            "end_date": d2 - timedelta(days=1),
            "focus": "Increase intensity with goal-specific workouts",
        })
        phases.append({
            "type": PhaseType.peak,
            "start_date": d2,
            "end_date": end_date,
            "focus": "Sharpen and taper: key sessions only",
        })
        return phases

    # No event: nothing to peak or taper for. Base, then build, to the end.
    if not has_event:
        base_weeks = max(3, int(weeks_available * 0.6))
        d1 = current_date + timedelta(weeks=base_weeks)
        phases.append({
            "type": PhaseType.base,
            "start_date": current_date,
            "end_date": d1 - timedelta(days=1),
            "focus": "Aerobic base: endurance foundation and movement efficiency",
        })
        phases.append({
            "type": PhaseType.build,
            "start_date": d1,
            "end_date": end_date,
            "focus": "Raise the ceiling: progressive intensity on a settled base",
        })
        return phases

    # Full plan (8+ weeks). The taper is a fixed length, not a share of the
    # plan: it used to be whatever was left over, which gave a 31-week plan a
    # 6-week taper and a rider who arrived at the start line detrained.
    race_weeks = 2 if long_event and weeks_available >= 12 else 1
    peak_weeks = max(1, min(3, round(weeks_available * 0.12)))
    remaining = weeks_available - race_weeks - peak_weeks
    base_share = {
        PeriodizationModel.polarized: 0.67,
        PeriodizationModel.sweet_spot: 0.5,
    }.get(model, 0.57)  # traditional: the old 40:30 base-to-build ratio
    base_weeks = max(3, int(remaining * base_share))
    build_weeks = max(2, remaining - base_weeks)

    d1 = current_date + timedelta(weeks=base_weeks)
    d2 = d1 + timedelta(weeks=build_weeks)
    d3 = d2 + timedelta(weeks=peak_weeks)

    phases.append({
        "type": PhaseType.base,
        "start_date": current_date,
        "end_date": d1 - timedelta(days=1),
        "focus": "Aerobic base: endurance foundation and movement efficiency",
    })
    phases.append({
        "type": PhaseType.build,
        "start_date": d1,
        "end_date": d2 - timedelta(days=1),
        "focus": "Build race-specific fitness with progressive intensity",
    })
    phases.append({
        "type": PhaseType.peak,
        "start_date": d2,
        "end_date": d3 - timedelta(days=1),
        "focus": "Peak: reduce volume, maintain sharpness",
    })
    if race_weeks > 0:
        phases.append({
            "type": PhaseType.race,
            "start_date": d3,
            "end_date": end_date,
            "focus": "Race week: taper, activate, perform",
        })

    return phases


def _create_workout_steps(
    db: Session, workout: Workout, template: dict
) -> None:
    """Create WorkoutStep rows from template steps."""
    steps = template.get("steps", [])
    step_order = 0

    for step_def in steps:
        repeat = step_def.get("repeat_count")
        actual_repeats = repeat if repeat and repeat > 1 else 1

        if step_def["step_type"] in ("interval_on", "interval_off") and actual_repeats > 1:
            step = WorkoutStep(
                workout_id=workout.id,
                step_order=step_order,
                step_type=step_def["step_type"],
                duration_seconds=step_def["duration_seconds"],
                power_target_pct=step_def.get("power_target_pct"),
                power_low_pct=step_def.get("power_low_pct"),
                power_high_pct=step_def.get("power_high_pct"),
                cadence_target=step_def.get("cadence_target"),
                repeat_count=actual_repeats,
                notes=step_def.get("notes"),
            )
            db.add(step)
            step_order += 1
        else:
            step = WorkoutStep(
                workout_id=workout.id,
                step_order=step_order,
                step_type=step_def["step_type"],
                duration_seconds=step_def["duration_seconds"],
                power_target_pct=step_def.get("power_target_pct"),
                power_low_pct=step_def.get("power_low_pct"),
                power_high_pct=step_def.get("power_high_pct"),
                cadence_target=step_def.get("cadence_target"),
                repeat_count=step_def.get("repeat_count"),
                notes=step_def.get("notes"),
            )
            db.add(step)
            step_order += 1


def replace_workout_steps(db: Session, workout: Workout, steps: list[dict]) -> None:
    """Swap a session's steps for these (template format), in one flush."""
    workout.steps.clear()
    db.flush()
    _create_workout_steps(db, workout, {"steps": steps})
    db.flush()
    db.expire(workout, ["steps"])


# --- Steps the coach writes ---
# Ride mode, the step chart and every export run on WorkoutStep rows. A
# session booked in chat used to carry none, so "load it onto your head unit"
# had nothing to load (16 Oct 2026). build_steps turns the coach's steps into
# the template format, refusing anything Ride mode or an export would get
# wrong, with an error the coach can act on.

STEP_TYPES = (
    "warmup", "steady_state", "interval_on", "interval_off", "cooldown", "free_ride", "ramp",
)
MAX_STEPS = 40
MAX_SESSION_S = 8 * 3600
_STEP_POWER_REQUIRED = {"steady_state", "interval_on", "interval_off"}
_PCT_KEYS = ("power_pct", "power_low_pct", "power_high_pct")
_WATT_KEYS = ("power_watts", "power_low_watts", "power_high_watts")


def _number(value, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number.")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.") from None


def _whole(value, label: str, low: int, high: int) -> int | None:
    number = _number(value, label)
    if number is None:
        return None
    if number != int(number) or not low <= number <= high:
        raise ValueError(f"{label} must be a whole number from {low} to {high}.")
    return int(number)


def _step_power(raw: dict, n: int, ftp: int | None) -> tuple[float | None, ...]:
    """(target, low, high) as fractions of FTP, from percentages or watts."""
    given_pct = [k for k in _PCT_KEYS if raw.get(k) is not None]
    given_w = [k for k in _WATT_KEYS if raw.get(k) is not None]
    if given_pct and given_w:
        raise ValueError(
            f"Step {n}: give power as a percentage of FTP or in watts, not both."
        )
    if given_w and not ftp:
        raise ValueError(
            f"Step {n}: the rider has no FTP yet, so give power as a percentage "
            f"of FTP (power_pct, 95 means 95%) instead of watts."
        )
    values = []
    for pct_key, watt_key in zip(_PCT_KEYS, _WATT_KEYS):
        if given_w:
            watts = _number(raw.get(watt_key), f"Step {n}: {watt_key}")
            values.append(None if watts is None else watts / ftp)
            continue
        pct = _number(raw.get(pct_key), f"Step {n}: {pct_key}")
        if pct is not None and pct < 20:
            raise ValueError(
                f"Step {n}: {pct_key} is a percentage of FTP, so 95 means 95%. "
                f"{pct:g} is too low to be one."
            )
        values.append(None if pct is None else pct / 100)
    for value in values:
        if value is not None and not 0.30 <= value <= 3.0:
            raise ValueError(
                f"Step {n}: {round(value * 100)}% of FTP is outside 30% to 300%."
            )
    target, low, high = values
    if low is not None and high is not None:
        if low > high:
            raise ValueError(f"Step {n}: the low power is above the high power.")
        if target is None:
            target = (low + high) / 2
    elif target is None and (low is not None or high is not None):
        # One bound alone is a target, not a range.
        target, low, high = (low if low is not None else high), None, None
    return target, low, high


def build_steps(raw_steps, ftp: int | None) -> list[dict]:
    """Validate the coach's steps and return them in template format.

    Power is stored as a fraction of FTP. A repeat_count on an interval_on
    repeats it together with the interval_off directly after it, which is how
    Ride mode, the step chart, ZWO and FIT all read a repeat. Raises
    ValueError with a message written for the coach to fix and resend.
    """
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_STEPS:
        raise ValueError(f"steps must be a list of 1 to {MAX_STEPS} steps.")

    built: list[dict] = []
    for i, raw in enumerate(raw_steps):
        n = i + 1
        if not isinstance(raw, dict):
            raise ValueError(f"Step {n} is not an object.")
        step_type = str(raw.get("step_type") or "").strip().lower()
        if step_type not in STEP_TYPES:
            raise ValueError(f"Step {n}: step_type must be one of {', '.join(STEP_TYPES)}.")
        duration = _whole(raw.get("duration_seconds"), f"Step {n}: duration_seconds", 10, 14400)
        if duration is None:
            raise ValueError(f"Step {n}: duration_seconds is required.")

        target, low, high = _step_power(raw, n, ftp)
        if step_type in _STEP_POWER_REQUIRED and target is None:
            raise ValueError(f"Step {n}: a {step_type} step needs a power target.")
        if step_type == "ramp" and (low is None or high is None):
            raise ValueError(f"Step {n}: a ramp needs both a low and a high power.")

        cadence = _whole(raw.get("cadence"), f"Step {n}: cadence", 50, 130)
        repeat = _whole(raw.get("repeat_count"), f"Step {n}: repeat_count", 1, 30)
        if repeat == 1:
            repeat = None
        if repeat is not None:
            follower = raw_steps[i + 1] if i + 1 < len(raw_steps) else None
            follows = isinstance(follower, dict) and str(
                follower.get("step_type") or ""
            ).strip().lower() == "interval_off"
            if step_type != "interval_on" or not follows:
                raise ValueError(
                    f"Step {n}: repeat_count only goes on an interval_on step that "
                    f"is followed directly by its interval_off. Write any other "
                    f"repeated block out in full, step by step."
                )

        notes = humanize(str(raw.get("notes") or "").strip())[:200] or None
        built.append({
            "step_type": step_type,
            "duration_seconds": duration,
            "power_target_pct": round(target, 4) if target is not None else None,
            "power_low_pct": round(low, 4) if low is not None else None,
            "power_high_pct": round(high, 4) if high is not None else None,
            "cadence_target": cadence,
            "repeat_count": repeat,
            "notes": notes,
        })

    total_s = steps_load(built)[0]
    if total_s > MAX_SESSION_S:
        raise ValueError(
            f"Those steps add up to {round(total_s / 3600, 1)} hours, more than "
            f"the 8 hour limit for one session."
        )
    return built


def ride_order(steps: list) -> list:
    """Steps in the order they are ridden, repeats unrolled.

    Works on template dicts and WorkoutStep rows alike. An interval_on with a
    repeat_count repeats together with the interval_off that follows it.
    """
    def get(step, key):
        return step.get(key) if isinstance(step, dict) else getattr(step, key, None)

    def kind(step):
        value = get(step, "step_type")
        return str(getattr(value, "value", value))

    ridden = []
    i = 0
    while i < len(steps):
        step = steps[i]
        reps = get(step, "repeat_count") or 1
        if kind(step) == "interval_on" and reps > 1:
            off = steps[i + 1] if i + 1 < len(steps) and kind(steps[i + 1]) == "interval_off" else None
            for _ in range(reps):
                ridden.append(step)
                if off is not None:
                    ridden.append(off)
            i += 2 if off is not None else 1
            continue
        ridden.append(step)
        i += 1
    return ridden


def steps_load(steps: list[dict]) -> tuple[int, float, float]:
    """(duration in seconds, TSS, IF) worked out from the steps themselves.

    TSS is the sum over steps of hours x intensity squared x 100, the per step
    form of estimate_tss. A step with no power target counts as easy riding.
    """
    seconds = 0
    tss = 0.0
    for step in ride_order(steps):
        duration = step["duration_seconds"]
        pct = step.get("power_target_pct")
        if pct is None and step.get("power_low_pct") is not None and step.get("power_high_pct") is not None:
            pct = (step["power_low_pct"] + step["power_high_pct"]) / 2
        if pct is None:
            pct = 0.5
        seconds += duration
        tss += duration / 3600 * pct ** 2 * 100
    hours = seconds / 3600
    intensity = sqrt(tss / (hours * 100)) if hours else 0.0
    return seconds, round(tss, 1), round(intensity, 2)


# --- Query functions ---

def get_plans(db: Session, user_id: str) -> list[TrainingPlan]:
    """Get all training plans for a user."""
    return (
        db.query(TrainingPlan)
        .filter(TrainingPlan.user_id == user_id)
        .order_by(TrainingPlan.created_at.desc())
        .all()
    )


def get_plan(db: Session, plan_id: str, user_id: str) -> TrainingPlan | None:
    """Get a single plan with phases loaded."""
    return (
        db.query(TrainingPlan)
        .filter(TrainingPlan.id == plan_id, TrainingPlan.user_id == user_id)
        .first()
    )


def get_plan_workouts(
    db: Session, plan_id: str, user_id: str,
    start_date: date | None = None, end_date: date | None = None,
) -> list[Workout]:
    """Get all workouts for a plan, optionally filtered by date."""
    plan = get_plan(db, plan_id, user_id)
    if not plan:
        return []

    phase_ids = [p.id for p in plan.phases]
    if not phase_ids:
        return []

    query = (
        db.query(Workout)
        .filter(Workout.phase_id.in_(phase_ids))
    )

    if start_date:
        query = query.filter(Workout.scheduled_date >= start_date)
    if end_date:
        query = query.filter(Workout.scheduled_date <= end_date)

    return query.order_by(Workout.scheduled_date, Workout.sort_order).all()


def visible_workouts(db: Session, user_id: str):
    """Every session the rider can see, as a query to narrow further.

    One rule: a session is visible when it belongs to the active plan, to an
    ended (completed) plan on or before its last day, or to no plan at all. A
    session the coach books in chat has no phase, so it outlives a rebuild.
    The calendar used to inner join through phases, and every session the
    coach had ever booked was invisible (16 Oct 2026). An ended plan keeps the
    weeks the rider rode, and nothing after them. Sessions of a cancelled
    plan stay hidden.
    """
    return (
        db.query(Workout)
        .outerjoin(TrainingPhase, Workout.phase_id == TrainingPhase.id)
        .outerjoin(TrainingPlan, TrainingPhase.plan_id == TrainingPlan.id)
        .filter(
            Workout.user_id == user_id,
            or_(
                Workout.phase_id.is_(None),
                TrainingPlan.status == PlanStatus.active,
                and_(
                    TrainingPlan.status == PlanStatus.completed,
                    Workout.scheduled_date <= TrainingPlan.end_date,
                ),
            ),
        )
    )


def get_workouts_by_date(
    db: Session, user_id: str,
    target_date: date | None = None,
    week_start: date | None = None,
) -> list[Workout]:
    """Get the visible workouts for a specific date or week."""
    # Steps come in one query: the coach's context reads them for every
    # session of the fortnight.
    query = visible_workouts(db, user_id).options(
        joinedload(Workout.actual_ride), selectinload(Workout.steps),
    )

    if target_date:
        query = query.filter(Workout.scheduled_date == target_date)
    elif week_start:
        week_end = week_start + timedelta(days=6)
        query = query.filter(
            Workout.scheduled_date >= week_start,
            Workout.scheduled_date <= week_end,
        )

    return query.order_by(Workout.scheduled_date, Workout.sort_order).all()


def get_workout(db: Session, workout_id: str, user_id: str) -> Workout | None:
    """Get a single workout with steps loaded."""
    return (
        db.query(Workout)
        .filter(Workout.id == workout_id, Workout.user_id == user_id)
        .first()
    )


def update_workout_status(
    db: Session, workout: Workout, status: str, ride_id: str | None = None
) -> Workout:
    """Update workout status and optionally link a ride."""
    WorkoutStatus(status)  # validate
    workout.status = status
    if ride_id:
        workout.actual_ride_id = ride_id
    db.commit()
    db.refresh(workout)
    return workout


def link_ride_to_workout(
    db: Session, workout: Workout, ride_id: str
) -> Workout:
    """Link an actual ride to a planned workout."""
    workout.actual_ride_id = ride_id
    workout.status = WorkoutStatus.completed
    db.commit()
    db.refresh(workout)
    return workout
