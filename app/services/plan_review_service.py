"""Plan Review Service — the coach interrogating its own prescription.

A plan written once and never questioned is a document, not coaching. This
service asks the question a good coach asks unprompted: given everything I now
know about this rider, is the work I prescribed still the work that moves them
towards their goal?

The engine reads what the plan asks for, what the rider is actually doing, and
what the goal physiologically demands, then looks for the single highest value
correction. It writes that correction to a PlanProposal and stops. Nothing is
applied. The rider accepts, declines, or opens a conversation about it, and
apply_proposal is the only path from a proposal to a real Workout row.

The bar for speaking is deliberately high. A proposal the data does not clearly
support is worse than silence, because it teaches the rider to skim past the
next one, and the next one might be the one that matters.
"""

import json
import logging
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core import forma_core
from app.core.coach_skills import distilled_persona
from app.core.formulas import rider_profile_scores
from app.core.llm_utils import humanize, response_text
from app.models.onboarding import EventPriority, GoalEvent, GoalStatus
from app.models.plan_proposal import PlanProposal
from app.models.ride import Ride
from app.models.training import (
    PlanStatus,
    TrainingPhase,
    TrainingPlan,
    Workout,
    WorkoutStatus,
    WorkoutType,
)
from app.models.user import User
from app.services.metrics_service import (
    get_current_fitness,
    get_ftp_history,
    get_recent_power_profile,
    get_weekly_training_load,
)

logger = logging.getLogger(__name__)


# How far forward the coach is allowed to edit. A fortnight is far enough to
# fix a training block and near enough that the rider still recognises it.
HORIZON_DAYS = 14

# How far back the evidence runs. Three weeks covers a full loading block plus
# the recovery week, so a single hard week does not read as a pattern.
LOOKBACK_DAYS = 21

# One correction, expressed in the fewest sessions that deliver it. More than
# three edits is a rewrite, and a rewrite is a conversation, not a proposal.
MAX_CHANGES = 3

# Below this, the last three weeks are not evidence, they are noise. A rider
# who has ridden twice has told the coach nothing about their instincts.
MIN_RIDES_FOR_EVIDENCE = 3

VALID_ACTIONS = ("update_workout", "add_workout", "skip_workout")


# ── Small readers ────────────────────────────────────────────────────────────


def _first_name(user: User) -> str:
    return (user.full_name or user.email.split("@")[0]).split()[0]


def _enum_value(value) -> str | None:
    """Enum columns load as members, and str() on a str-mixin Enum gives
    "WorkoutType.endurance" rather than "endurance". The coach must never be
    shown the class name, so read .value when it is there."""
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _as_date(value) -> date | None:
    """Dates arrive as date, datetime or ISO string depending on the backend."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _zone_seconds(ride: Ride) -> list[int] | None:
    """Seconds per power zone Z1 to Z7, or None when the ride had no power.

    Powerless rides carry a differently shaped zone_summary, so the length
    check is the guard rather than a version number.
    """
    summary = ride.zone_summary or {}
    z = summary.get("z")
    if isinstance(z, list) and len(z) == 7:
        return [int(v or 0) for v in z]
    return None


def _active_plan(db: Session, user: User) -> TrainingPlan | None:
    return (
        db.query(TrainingPlan)
        .filter(
            TrainingPlan.user_id == user.id,
            TrainingPlan.status == PlanStatus.active,
        )
        .order_by(TrainingPlan.created_at.desc())
        .first()
    )


def _current_phase(plan: TrainingPlan, today: date) -> TrainingPhase | None:
    """The phase the rider is standing in, or the next one due to start."""
    upcoming: list[tuple[date, TrainingPhase]] = []
    for phase in plan.phases:
        start, end = _as_date(phase.start_date), _as_date(phase.end_date)
        if start is None:
            continue
        if end and start <= today <= end:
            return phase
        if start > today:
            upcoming.append((start, phase))
    return min(upcoming, key=lambda pair: pair[0])[1] if upcoming else None


def _phase_for_date(plan: TrainingPlan, day: date) -> TrainingPhase | None:
    """Which phase a new session belongs to.

    A workout with no phase is invisible: the calendar query inner joins
    through phases, so an unattached session would be silently lost.
    """
    fallback = None
    for phase in plan.phases:
        start, end = _as_date(phase.start_date), _as_date(phase.end_date)
        if start and end and start <= day <= end:
            return phase
        if start and start <= day:
            fallback = phase
    return fallback or (plan.phases[0] if plan.phases else None)


def _normalise_observation(text: str) -> str:
    """Comparison key for the duplicate guard: whitespace and case removed."""
    return " ".join((text or "").split()).casefold()


# ── The evidence ─────────────────────────────────────────────────────────────


def build_review_context(db: Session, user: User) -> dict:
    """Everything a coach would actually look at before questioning a plan.

    A few dozen numbers, never raw samples. The reasoning that matters here is
    comparative (prescribed against actual against what the goal demands), and
    that comparison is made harder, not easier, by burying it in data.
    """
    today = date.today()
    horizon = today + timedelta(days=HORIZON_DAYS)
    lookback_start = today - timedelta(days=LOOKBACK_DAYS)

    context: dict = {
        "today": today.isoformat(),
        "rider_name": _first_name(user),
        "ftp": user.ftp,
        "weight_kg": user.weight_kg,
        "w_per_kg_at_ftp": (
            round(user.ftp / user.weight_kg, 2)
            if user.ftp and user.weight_kg else None
        ),
        "experience_level": user.experience_level,
        "weekly_hours_available": user.weekly_hours_available,
        "rest_days_weekday_indexes": user.rest_days or [],
        "preferred_hard_days_weekday_indexes": user.preferred_hard_days or [],
    }

    # ── The plan as prescribed ──
    plan = _active_plan(db, user)
    context["plan"] = None
    context["prescribed_next_14_days"] = []
    if plan:
        phase = _current_phase(plan, today)
        context["plan"] = {
            "id": plan.id,
            "name": plan.name,
            "start_date": str(_as_date(plan.start_date)),
            "end_date": str(_as_date(plan.end_date)),
            "periodization_model": _enum_value(plan.periodization_model),
            "current_phase": {
                "type": _enum_value(phase.phase_type),
                "focus": phase.focus,
                "start_date": str(_as_date(phase.start_date)),
                "end_date": str(_as_date(phase.end_date)),
                "target_weekly_tss": phase.target_weekly_tss,
                "target_weekly_hours": phase.target_weekly_hours,
            } if phase else None,
        }

        phase_ids = [p.id for p in plan.phases]
        prescribed = (
            db.query(Workout)
            .filter(
                Workout.user_id == user.id,
                Workout.phase_id.in_(phase_ids),
                Workout.scheduled_date >= today,
                Workout.scheduled_date <= horizon,
            )
            .order_by(Workout.scheduled_date, Workout.sort_order)
            .all()
        ) if phase_ids else []
        context["prescribed_next_14_days"] = [
            {
                # The id is the handle the coach edits by. Without it a
                # proposal can only describe a change, never make one.
                "workout_id": w.id,
                "date": str(_as_date(w.scheduled_date)),
                "day": (_as_date(w.scheduled_date) or today).strftime("%A"),
                "title": w.title,
                "workout_type": _enum_value(w.workout_type),
                "duration_minutes": (w.planned_duration_seconds or 0) // 60,
                "planned_tss": w.planned_tss,
                "planned_if": w.planned_if,
                "status": _enum_value(w.status),
            }
            for w in prescribed
        ]

    # ── What the rider actually did ──
    rides = (
        db.query(Ride)
        .filter(
            Ride.user_id == user.id,
            Ride.ride_date >= datetime.combine(lookback_start, datetime.min.time()),
        )
        .order_by(Ride.ride_date.desc())
        .all()
    )

    linked_workouts: dict[str, Workout] = {}
    workout_ids = [r.workout_id for r in rides if r.workout_id]
    if workout_ids:
        linked_workouts = {
            w.id: w
            for w in db.query(Workout).filter(
                Workout.id.in_(workout_ids), Workout.user_id == user.id
            ).all()
        }

    actual: list[dict] = []
    z5_plus_seconds = 0
    z4_seconds = 0
    total_tss = 0.0
    extra_count = 0
    for ride in rides:
        matched = linked_workouts.get(ride.workout_id) if ride.workout_id else None
        zones = _zone_seconds(ride)
        if zones:
            z4_seconds += zones[3]
            z5_plus_seconds += sum(zones[4:])
        total_tss += ride.tss or 0
        if not matched:
            extra_count += 1
        actual.append({
            "date": str(_as_date(ride.ride_date)),
            "title": ride.forma_title or ride.title,
            "duration_minutes": (
                ride.moving_time_seconds or ride.duration_seconds or 0
            ) // 60,
            "tss": round(ride.tss) if ride.tss else None,
            "intensity_factor": ride.intensity_factor,
            "dominant_zone": (ride.zone_summary or {}).get("dom"),
            "minutes_at_z4": round(zones[3] / 60) if zones else None,
            "minutes_at_z5_plus": round(sum(zones[4:]) / 60) if zones else None,
            # The tell. An unprescribed session is the rider's own instinct
            # showing, and instinct is exactly what a plan review must read.
            "matched_prescribed_session": (
                f"{matched.title} ({_enum_value(matched.workout_type)})"
                if matched else None
            ),
            "extra_unprescribed": matched is None,
        })
    context["actual_rides_last_21_days"] = actual

    missed = (
        db.query(Workout)
        .filter(
            Workout.user_id == user.id,
            Workout.scheduled_date >= lookback_start,
            Workout.scheduled_date < today,
            Workout.status == WorkoutStatus.planned,
        )
        .count()
    )
    context["last_21_days_summary"] = {
        "rides": len(rides),
        "extra_unprescribed_rides": extra_count,
        "prescribed_sessions_not_completed": missed,
        "total_tss": round(total_tss),
        "minutes_at_z4_threshold": round(z4_seconds / 60),
        "minutes_at_z5_plus_vo2max_and_above": round(z5_plus_seconds / 60),
    }

    # ── Where the rider currently is ──
    try:
        fitness = get_current_fitness(db, user.id)
        context["fitness"] = {
            "ctl": round(fitness["ctl"], 1),
            "atl": round(fitness["atl"], 1),
            "tsb": round(fitness["tsb"], 1),
            "ramp_rate": fitness.get("ramp_rate"),
        }
    except Exception:
        logger.exception("Fitness for plan review failed (user=%s)", user.id)

    # Profile scores are the spine of this whole feature: they are how the
    # coach knows a strength from a limiter, and therefore how it knows when
    # a rider is polishing the wrong system.
    try:
        weight = user.weight_kg or 0
        if weight > 0:
            profile_raw = get_recent_power_profile(db, user.id, days=90)
            power_values = {d: v["best_power"] for d, v in profile_raw.items()}
            scores = rider_profile_scores(power_values, weight)
            scored = sorted(
                ((k, v) for k, v in scores.items() if v > 0),
                key=lambda kv: kv[1],
                reverse=True,
            )
            context["rider_profile_scores"] = scores
            context["profile_strengths"] = [k for k, _ in scored[:2]]
            context["profile_limiters"] = [k for k, _ in scored[-2:]]
    except Exception:
        logger.exception("Profile scores for plan review failed (user=%s)", user.id)

    try:
        history = get_ftp_history(db, user.id)
        if history:
            context["ftp_history"] = [
                {"date": str(h["date"]), "ftp": h["ftp"]} for h in history[-6:]
            ]
    except Exception:
        logger.exception("FTP history for plan review failed (user=%s)", user.id)

    try:
        weekly = get_weekly_training_load(db, user.id, weeks=4)
        if weekly:
            context["weekly_load_last_4_weeks"] = [
                {
                    "week_start": str(w["week_start"]),
                    "tss": round(w["total_tss"] or 0),
                    "rides": w["ride_count"],
                    "avg_if": w["avg_intensity_factor"],
                }
                # Newest four. get_weekly_training_load returns oldest first
                # and buckets the current part-week too, so slicing from the
                # front would hand the coach a month-old week and hide the one
                # it is actually judging.
                for w in weekly[-4:]
            ]
    except Exception:
        logger.exception("Weekly load for plan review failed (user=%s)", user.id)

    # ── What the rider is building towards ──
    goal = (
        db.query(GoalEvent)
        .filter(
            GoalEvent.user_id == user.id,
            GoalEvent.event_date >= today,
            GoalEvent.status == GoalStatus.upcoming,
            GoalEvent.priority == EventPriority.a_race,
        )
        .order_by(GoalEvent.event_date)
        .first()
    )
    if goal:
        goal_date = _as_date(goal.event_date) or today
        context["a_race"] = {
            "id": goal.id,
            "event_name": goal.event_name,
            "event_date": str(goal_date),
            "days_remaining": (goal_date - today).days,
            "event_type": _enum_value(goal.event_type),
            "target_duration_minutes": goal.target_duration_minutes,
            # The why and the becoming are the fuel. A correction argued
            # against the rider's own words lands; one argued against a
            # number does not.
            "why": goal.why,
            "becoming": goal.becoming,
            "notes": goal.notes,
        }
    else:
        context["a_race"] = None

    try:
        from app.services.memory_service import get_context as get_memory_context

        memory_block = get_memory_context(db, user, limit=12)
        if memory_block:
            context["long_term_memory"] = memory_block.split("\n")
    except Exception:
        logger.exception("Memory context for plan review failed (user=%s)", user.id)

    return context


# ── The instructions ─────────────────────────────────────────────────────────

REVIEW_INSTRUCTIONS = """

## This surface: the plan review
You are auditing your own prescription. Nobody asked you to. You are doing it
because a plan written once and never questioned stops being coaching, and
because the rider trusted you with the choice of what to do next.

You are given the active plan and the phase they are in, the sessions you have
prescribed for the next fortnight with their ids, every ride they actually did
in the last 21 days and whether each matched a prescribed session or was extra,
their profile scores, their CTL, ATL and TSB, their FTP and weight with any
history, and the A-race they are building towards with its why.

Work in this order.

1. Compare prescribed against actual. Compliance is the shallow reading. The
   deep reading is in what they chose: the sessions they added, moved or quietly
   dropped. That is where their instincts show, and instincts are honest.
2. Compare both against what the goal physiologically demands. Work out which
   system the event actually taxes, then check whether three weeks of real
   riding built it. For example, a hill climb of roughly half an hour is won by
   sustainable power to weight, so threshold and endurance move the clock while
   a sharper one minute power does not, however good it feels to produce.
3. Find the SINGLE highest value correction. One. Not a tidy list of
   improvements. If two things are wrong, you raise the one costing the most
   time on the goal and you leave the other alone.

The bar for speaking is high. A proposal the data does not clearly support is
worse than saying nothing, because it teaches the rider to skim past your
proposals, and one day one of them will matter. If the plan is broadly right,
or the evidence is thin, or they have simply had an ordinary quiet week, return
change_needed false and be at peace with it.

When you do speak, write like a coach who respects them. Name what you saw with
their actual numbers and dates. Say plainly why it matters for THIS goal, not
for cycling in general. Never scold. Training your strength because it feels
good is a deeply human thing to do rather than a failure, and if that is what
you are looking at, say so before you say anything else.

Return STRICT JSON and nothing else. No markdown fence, no preamble, no
commentary after the closing brace.

When no change is warranted, return exactly:
{"change_needed": false}

When a change is warranted:
{
  "change_needed": true,
  "observation": "what you noticed, 2 to 3 sentences, your own voice, with their real numbers and dates",
  "rationale": "why it matters for this goal and this rider, 2 to 4 sentences",
  "changes": [
    {
      "action": "update_workout",
      "workout_id": "id copied exactly from prescribed_next_14_days, or null for add_workout",
      "scheduled_date": "YYYY-MM-DD",
      "title": "the session name the rider will see",
      "workout_type": "endurance|tempo|sweet_spot|threshold|vo2max|sprint|recovery|rest",
      "planned_duration_seconds": 3600,
      "planned_tss": 65,
      "description": "what the session actually is, in enough detail to ride it",
      "why": "one line, why THIS edit"
    }
  ]
}

Rules for the changes list.
- At most three edits. This is one correction expressed in the fewest sessions
  that deliver it, not a rewrite of the block.
- action is one of update_workout, add_workout, skip_workout.
- update_workout and skip_workout MUST carry a workout_id copied exactly from
  prescribed_next_14_days. Never invent an id, and never edit a session that is
  not in that list.
- add_workout carries workout_id null and a scheduled_date inside the next
  fortnight.
- Respect their rest days and the hours they actually have. If there is no room
  for the work, move load rather than adding it.
- Every change carries its own why, one line. The rider approves each edit
  individually, so each edit owes them a reason.
- British English. Never use em dashes or en dashes, use a comma or a full stop.
"""


# ── The review ───────────────────────────────────────────────────────────────


def _coerce_int(value, low: int, high: int) -> int | None:
    try:
        return max(low, min(high, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def _coerce_float(value, low: float, high: float) -> float | None:
    try:
        return max(low, min(high, round(float(value), 1)))
    except (TypeError, ValueError):
        return None


def _validate_changes(
    raw_changes, allowed_workout_ids: set[str], today: date
) -> list[dict]:
    """Keep only edits the coach is actually entitled to make.

    Edits are dropped individually rather than failing the whole proposal: a
    sound argument with one bad row is still a sound argument, and the rider
    approves each row anyway.
    """
    horizon = today + timedelta(days=HORIZON_DAYS)
    clean: list[dict] = []

    for raw in raw_changes or []:
        # Cap on edits that survived, not on edits attempted, so one malformed
        # row at the top does not cost the rider a good one further down.
        if len(clean) >= MAX_CHANGES:
            break
        if not isinstance(raw, dict):
            continue
        action = str(raw.get("action") or "").strip()
        if action not in VALID_ACTIONS:
            logger.info("Plan review: dropped change with unknown action %r", action)
            continue

        workout_id = raw.get("workout_id") or None
        if action in ("update_workout", "skip_workout"):
            if workout_id not in allowed_workout_ids:
                logger.info(
                    "Plan review: dropped %s for workout_id %r not in the "
                    "prescribed fortnight", action, workout_id,
                )
                continue
        else:
            workout_id = None

        scheduled = _as_date(raw.get("scheduled_date"))
        if action == "add_workout":
            # A new session with no valid date has nowhere to live.
            if scheduled is None or not (today <= scheduled <= horizon):
                logger.info(
                    "Plan review: dropped add_workout with out of range date %r",
                    raw.get("scheduled_date"),
                )
                continue
        elif scheduled is not None and not (today <= scheduled <= horizon):
            scheduled = None  # a move outside the window is simply not a move

        workout_type = str(raw.get("workout_type") or "").strip().lower()
        try:
            workout_type = WorkoutType(workout_type).value
        except ValueError:
            if action == "add_workout":
                logger.info(
                    "Plan review: dropped add_workout with unknown type %r",
                    raw.get("workout_type"),
                )
                continue
            workout_type = None  # an update can leave the existing type alone

        change = {
            "action": action,
            "workout_id": workout_id,
            "scheduled_date": scheduled.isoformat() if scheduled else None,
            "title": humanize(str(raw.get("title") or "").strip())[:255] or None,
            "workout_type": workout_type,
            "planned_duration_seconds": _coerce_int(
                raw.get("planned_duration_seconds"), 600, 8 * 3600
            ),
            "planned_tss": _coerce_float(raw.get("planned_tss"), 0.0, 500.0),
            "description": humanize(str(raw.get("description") or "").strip()) or None,
            "why": humanize(str(raw.get("why") or "").strip()) or None,
        }
        if action == "add_workout" and not change["title"]:
            change["title"] = workout_type.replace("_", " ").title()
        clean.append(change)

    return clean


def _supersede_pending(db: Session, user: User, plan_id: str | None) -> None:
    """Retire older pending proposals for this plan.

    The rider should face one live argument, not a queue of them. An unanswered
    proposal written against last week's evidence is no longer the coach's
    position, so it is retired rather than left to rot at the top of the list.
    """
    query = db.query(PlanProposal).filter(
        PlanProposal.user_id == user.id,
        PlanProposal.status == "pending",
    )
    query = (
        query.filter(PlanProposal.plan_id == plan_id) if plan_id
        else query.filter(PlanProposal.plan_id.is_(None))
    )
    now = datetime.utcnow()
    for stale in query.all():
        stale.status = "superseded"
        stale.decided_at = now


def review_plan(
    db: Session, user: User, trigger: str = "manual"
) -> PlanProposal | None:
    """Interrogate the active plan and, if the evidence demands it, propose a fix.

    Returns the new PlanProposal, or None when nothing warrants changing, when
    the evidence is too thin to argue from, or when the coach is already holding
    an identical unanswered proposal. All three mean the same thing to a caller:
    do not put anything new in front of the rider.
    """
    today = date.today()

    plan = _active_plan(db, user)
    if plan is None:
        return None  # nothing to interrogate

    context = build_review_context(db, user)

    ride_count = context.get("last_21_days_summary", {}).get("rides", 0)
    if ride_count < MIN_RIDES_FOR_EVIDENCE and trigger != "goal_changed":
        # A goal change is reason enough on its own. Anything else needs three
        # weeks of riding behind it, or the coach is guessing out loud.
        return None

    allowed_ids = {
        w["workout_id"] for w in context.get("prescribed_next_14_days", [])
    }

    try:
        response = forma_core.call(
            user_id=user.id,
            task="plan_review",
            surface="plan",
            system=distilled_persona(user.coach_name, user.coach_tone)
            + REVIEW_INSTRUCTIONS,
            messages=[{
                "role": "user",
                "content": (
                    "Review this rider's plan against what they are actually "
                    "doing and what their goal demands. The review was "
                    f"triggered by: {trigger}.\n\n```json\n"
                    f"{json.dumps(context, indent=2, default=str)}\n```"
                ),
            }],
        )
        raw = response_text(response).strip()
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:
        # A failed review is a quiet non-event. The rider's plan is untouched
        # and the next trigger will try again.
        logger.exception("Plan review generation failed (user=%s)", user.id)
        return None

    if not data.get("change_needed"):
        return None

    observation = humanize(str(data.get("observation") or "").strip())
    rationale = humanize(str(data.get("rationale") or "").strip())
    if not observation or not rationale:
        logger.info(
            "Plan review: change_needed with no argument attached (user=%s)", user.id
        )
        return None

    changes = _validate_changes(data.get("changes"), allowed_ids, today)
    if not changes:
        # An observation with no edits is a chat message, not a proposal. The
        # rider is being asked to approve something concrete or nothing at all.
        logger.info(
            "Plan review: no change survived validation (user=%s)", user.id
        )
        return None

    # The same argument, already sitting unanswered in front of the rider.
    # Repeating it adds pressure, not information.
    key = _normalise_observation(observation)
    existing = (
        db.query(PlanProposal)
        .filter(
            PlanProposal.user_id == user.id,
            PlanProposal.status == "pending",
        )
        .all()
    )
    if any(_normalise_observation(p.observation) == key for p in existing):
        logger.info(
            "Plan review: identical pending proposal already stands (user=%s)",
            user.id,
        )
        return None

    _supersede_pending(db, user, plan.id)

    goal = context.get("a_race") or {}
    proposal = PlanProposal(
        user_id=user.id,
        plan_id=plan.id,
        goal_id=goal.get("id"),
        trigger=trigger,
        observation=observation,
        rationale=rationale,
        changes=changes,
        status="pending",
    )
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    logger.info(
        "Plan review: proposal %s raised for user=%s (trigger=%s, %d changes)",
        proposal.id, user.id, trigger, len(changes),
    )
    return proposal


# ── The rider's decision ─────────────────────────────────────────────────────


def _planned_if_for(workout_type: str, duration_seconds: int | None) -> float | None:
    """A changed session type leaves the old intensity target lying. Refresh it
    from the template so the row stays internally honest."""
    try:
        from app.core.workout_templates import get_template

        template = get_template(workout_type, duration_hint=duration_seconds)
        return template.get("planned_if")
    except Exception:
        logger.exception("Template lookup failed for type %s", workout_type)
        return None


def apply_proposal(db: Session, user: User, proposal: PlanProposal) -> int:
    """Apply an accepted proposal to real workouts. Returns how many changed.

    This is the only path from a proposal to the rider's calendar, and it only
    runs on an explicit accept.
    """
    if proposal.user_id != user.id:
        raise ValueError("Proposal does not belong to this rider")
    if proposal.status != "pending":
        # Loud rather than silent: a second accept means a caller lost track of
        # state, and quietly re-applying edits would corrupt the plan.
        raise ValueError(f"Proposal is already {proposal.status}")

    plan = (
        db.query(TrainingPlan)
        .filter(TrainingPlan.id == proposal.plan_id, TrainingPlan.user_id == user.id)
        .first()
        if proposal.plan_id else None
    )

    changed = 0
    for change in proposal.changes or []:
        action = change.get("action")
        workout_id = change.get("workout_id")
        scheduled = _as_date(change.get("scheduled_date"))

        if action in ("update_workout", "skip_workout"):
            workout = (
                db.query(Workout)
                .filter(Workout.id == workout_id, Workout.user_id == user.id)
                .first()
            )
            if workout is None:
                logger.warning(
                    "Applying proposal %s: workout %s is gone, skipping",
                    proposal.id, workout_id,
                )
                continue
            if workout.status == WorkoutStatus.completed:
                # The rider already rode it. Rewriting a session after the fact
                # would falsify their own history, which is the one thing this
                # engine must never do.
                logger.info(
                    "Applying proposal %s: workout %s already completed, left alone",
                    proposal.id, workout_id,
                )
                continue

            if action == "skip_workout":
                workout.status = WorkoutStatus.skipped
                changed += 1
                continue

            if scheduled:
                workout.scheduled_date = scheduled
            if change.get("title"):
                workout.title = change["title"]
            if change.get("description"):
                workout.description = change["description"]
            if change.get("planned_duration_seconds"):
                workout.planned_duration_seconds = change["planned_duration_seconds"]
            if change.get("planned_tss") is not None:
                workout.planned_tss = change["planned_tss"]
            if change.get("workout_type"):
                new_type = WorkoutType(change["workout_type"])
                if new_type != workout.workout_type:
                    workout.workout_type = new_type
                    workout.planned_if = _planned_if_for(
                        new_type.value, workout.planned_duration_seconds
                    )
            # The rider changed this session on purpose, and the plan should
            # remember that it is no longer the generated prescription.
            if workout.status == WorkoutStatus.planned:
                workout.status = WorkoutStatus.modified
            changed += 1

        elif action == "add_workout":
            if scheduled is None or not change.get("workout_type"):
                continue
            phase = _phase_for_date(plan, scheduled) if plan else None
            if phase is None:
                logger.warning(
                    "Applying proposal %s: no phase for %s, the session would "
                    "not appear on the calendar", proposal.id, scheduled,
                )
                continue
            duration = change.get("planned_duration_seconds")
            workout = Workout(
                phase_id=phase.id,
                user_id=user.id,
                scheduled_date=scheduled,
                title=change.get("title") or "Coach session",
                description=change.get("description"),
                workout_type=WorkoutType(change["workout_type"]),
                planned_duration_seconds=duration,
                planned_tss=change.get("planned_tss"),
                planned_if=_planned_if_for(change["workout_type"], duration),
                status=WorkoutStatus.planned,
            )
            db.add(workout)
            changed += 1

    proposal.status = "accepted"
    proposal.decided_at = datetime.utcnow()
    db.commit()
    logger.info(
        "Proposal %s accepted by user=%s, %d workouts changed",
        proposal.id, user.id, changed,
    )
    return changed


def decline_proposal(
    db: Session, user: User, proposal: PlanProposal
) -> PlanProposal:
    """Record a decline. The plan is untouched, which is the whole point."""
    if proposal.user_id != user.id:
        raise ValueError("Proposal does not belong to this rider")
    if proposal.status != "pending":
        raise ValueError(f"Proposal is already {proposal.status}")

    proposal.status = "declined"
    proposal.decided_at = datetime.utcnow()
    db.commit()
    db.refresh(proposal)
    logger.info("Proposal %s declined by user=%s", proposal.id, user.id)
    return proposal


def pending_proposals(db: Session, user: User) -> list[PlanProposal]:
    """Everything still waiting on the rider, newest first."""
    return (
        db.query(PlanProposal)
        .filter(
            PlanProposal.user_id == user.id,
            PlanProposal.status == "pending",
        )
        .order_by(PlanProposal.created_at.desc())
        .all()
    )
