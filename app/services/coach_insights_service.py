"""
Coach Insights Service — Forma's proactive presence across the app.

Three types of insight:
1. Daily nudge — contextual coaching card on the dashboard
2. Ride debrief — auto-generated post-ride feedback (standalone, not tied to a workout)
3. Metric explanation — tap-to-explain any metric in YOUR context

Uses Claude Haiku for nudges and explanations (fast, cheap),
Claude Sonnet for debriefs (deeper analysis).
"""

import json
import logging
from datetime import date, datetime, timedelta

from app.core.session_naming import session_display_name, workout_type_label

from sqlalchemy.orm import Session

from app.core import forma_core
from app.models.ride import Ride
from app.models.training import TrainingPlan, TrainingPhase, Workout, PlanStatus, WorkoutStatus
from app.models.user import User
from app.services.metrics_service import get_current_fitness, get_weekly_training_load
from app.core.llm_utils import humanize, response_text
from app.core.coach_skills import distilled_persona

logger = logging.getLogger(__name__)


# ── Daily Nudge ──────────────────────────────────────────────────────────────

NUDGE_SYSTEM_PROMPT = """

## This surface: the daily dashboard nudge
Generate a SHORT coaching nudge (2-3 sentences max) based on the rider's current state.

Rules:
1. Address the rider by first name.
2. Be supportive, specific, and actionable. Never generic.
3. Reference actual numbers from the data (CTL, TSB, recent rides, today's workout).
4. Match your tone to the situation: encouraging on rest days, motivating before hard sessions, \
   celebrating consistency, cautioning when fatigued.
5. Return ONLY the nudge text — no JSON, no markdown headings, just the message.
6. Keep it conversational, like a text from a coach you trust.
7. Use the rider's long-term memory (goals, gaps, habits, values, life events) to make it \
   PERSONAL — reference something specific you know about them when it's relevant. \
   Respect any [HIDDEN] items: use them for judgement, never mention them."""


def _build_nudge_context(
    db: Session, user: User
) -> dict:
    """Build lightweight context for nudge generation."""
    today = date.today()
    rider_name = (user.full_name or user.email.split("@")[0]).split()[0]

    # Current fitness
    fitness = get_current_fitness(db, user.id)

    # Today's planned workout (if any)
    today_workout = (
        db.query(Workout)
        .join(TrainingPhase)
        .join(TrainingPlan)
        .filter(
            Workout.user_id == user.id,
            Workout.scheduled_date == today,
            TrainingPlan.status == PlanStatus.active,
        )
        .first()
    )

    # Yesterday's ride (most recent)
    yesterday = today - timedelta(days=1)
    recent_ride = (
        db.query(Ride)
        .filter(
            Ride.user_id == user.id,
            Ride.ride_date >= datetime.combine(yesterday, datetime.min.time()),
        )
        .order_by(Ride.ride_date.desc())
        .first()
    )

    # Weekly load trend (last 4 weeks)
    weekly_load = get_weekly_training_load(db, user.id, weeks=4)

    # Compliance: completed vs total workouts this week
    week_start = today - timedelta(days=today.weekday())
    week_workouts = (
        db.query(Workout)
        .join(TrainingPhase)
        .join(TrainingPlan)
        .filter(
            Workout.user_id == user.id,
            Workout.scheduled_date >= week_start,
            Workout.scheduled_date <= today,
            TrainingPlan.status == PlanStatus.active,
        )
        .all()
    )
    completed = sum(1 for w in week_workouts if w.status == WorkoutStatus.completed)
    total = len(week_workouts)

    context = {
        "rider_name": rider_name,
        "today": today.isoformat(),
        "day_of_week": today.strftime("%A"),
        "fitness": {
            "ctl": round(fitness["ctl"], 1),
            "atl": round(fitness["atl"], 1),
            "tsb": round(fitness["tsb"], 1),
            "ramp_rate": fitness.get("ramp_rate"),
        },
        "ftp": user.ftp,
        "this_week_compliance": f"{completed}/{total} sessions completed",
    }

    if today_workout:
        context["todays_workout"] = {
            "title": session_display_name(
                today_workout.workout_type, today_workout.id
            ),
            "type": workout_type_label(today_workout.workout_type),
            "structure": today_workout.title,
            "description": today_workout.description,
            "planned_duration_minutes": (today_workout.planned_duration_seconds or 0) // 60,
            "planned_tss": today_workout.planned_tss,
            "status": today_workout.status,
        }
    else:
        context["todays_workout"] = None  # Rest day

    if recent_ride:
        context["last_ride"] = {
            "title": recent_ride.title,
            "date": str(recent_ride.ride_date.date() if hasattr(recent_ride.ride_date, 'date') else recent_ride.ride_date),
            "tss": recent_ride.tss,
            "duration_minutes": (recent_ride.moving_time_seconds or recent_ride.duration_seconds or 0) // 60,
            "normalized_power": recent_ride.normalized_power,
            "intensity_factor": recent_ride.intensity_factor,
        }

    if weekly_load:
        context["weekly_tss_trend"] = [
            {"week": w.get("week_start", ""), "tss": w.get("total_tss", 0)}
            for w in weekly_load[:4]
        ]

    # Long-term memory — the nudge must come from a coach who KNOWS this rider.
    try:
        from app.services.memory_service import get_context as get_memory_context

        memory_block = get_memory_context(db, user, limit=12)
        if memory_block:
            context["long_term_memory"] = memory_block.split("\n")
    except Exception:
        logger.exception("Memory context for nudge failed (user=%s)", user.id)

    # Rider Dossier — the personal layer (heartset, time reality, fuel...)
    # so the morning note speaks to this rider's life, not just their PMC.
    try:
        from app.services.dossier_service import dossier_context

        dossier_block = dossier_context(db, user.id)
        if dossier_block:
            context["rider_dossier"] = dossier_block.split("\n")
    except Exception:
        logger.exception("Dossier context for nudge failed (user=%s)", user.id)

    return context


def generate_daily_nudge(db: Session, user: User) -> dict:
    """
    Generate today's coaching nudge. Returns {"nudge": str, "generated_at": str}.
    Cached per user per day via coach_nudges table.
    """
    today = date.today()

    # Check cache
    from app.models.coach import CoachNudge
    existing = (
        db.query(CoachNudge)
        .filter(
            CoachNudge.user_id == user.id,
            CoachNudge.nudge_date == today,
        )
        .first()
    )
    if existing:
        return {
            "nudge": existing.content,
            "generated_at": str(existing.created_at),
            "cached": True,
        }

    # Build context and generate
    context = _build_nudge_context(db, user)

    try:
        response = forma_core.call(
            user_id=user.id,
            task="nudge",
            surface="dashboard",
            system=distilled_persona(user.coach_name, user.coach_tone) + NUDGE_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Generate today's coaching nudge based on this data:\n\n```json\n{json.dumps(context, indent=2, default=str)}\n```",
            }],
        )
        nudge_text = humanize(response_text(response).strip())
    except Exception as e:
        logger.error(f"Nudge generation failed: {e}")
        # Deterministic fallback
        rider_name = context["rider_name"]
        tsb = context["fitness"]["tsb"]
        workout = context.get("todays_workout")
        if workout:
            wtype = workout["type"]
            article = "an" if wtype[:1].lower() in "aeiou" else "a"
            nudge_text = f"Hey {rider_name}, you've got {article} {wtype} session on the plan today. Your form is sitting at {tsb:+.0f}, let's make it count."
        else:
            nudge_text = f"Rest day, {rider_name}. Your body builds fitness during recovery, not just during training. Enjoy the day off."
        nudge_text = humanize(nudge_text)

    # Cache
    nudge = CoachNudge(
        user_id=user.id,
        nudge_date=today,
        content=nudge_text,
    )
    db.add(nudge)
    db.commit()

    return {
        "nudge": nudge_text,
        "generated_at": datetime.utcnow().isoformat(),
        "cached": False,
    }


# ── Ride Debrief ─────────────────────────────────────────────────────────────

DEBRIEF_SYSTEM_PROMPT = """

## This surface: the post-ride debrief
The rider just completed a ride and you are giving them a brief, honest debrief.

Rules:
1. Address the rider by first name.
2. Keep it to 2-3 short paragraphs. Warm, specific, data-informed.
3. Highlight what went well FIRST. Then note anything to watch.
4. Reference specific numbers from the ride (power, duration, TSS, IF).
5. If you can relate this ride to their fitness trend or upcoming goal, do so briefly.
6. End with a forward-looking statement (what's next, how this builds toward their goal).
7. Use the rider's long-term memory: does this ride confirm or contradict a known gap \
   (e.g. "fades in the final hour")? Did they apply an insight you gave them (e.g. fueling)? \
   If advice you gave is visibly working, SAY SO with the evidence — closing that loop is \
   the most valuable thing you can do. Respect [HIDDEN] items: use, never mention.
8. Return ONLY the debrief text as plain paragraphs (markdown OK for bold/italic). No JSON."""


def _build_debrief_context(
    db: Session, user: User, ride: Ride
) -> dict:
    """Build context for ride debrief generation."""
    rider_name = (user.full_name or user.email.split("@")[0]).split()[0]
    fitness = get_current_fitness(db, user.id)

    # Linked workout (if any)
    linked_workout = None
    if ride.workout_id:
        linked_workout = db.query(Workout).filter(Workout.id == ride.workout_id).first()

    # Upcoming goal
    from app.models.onboarding import GoalEvent, GoalStatus
    upcoming_goal = (
        db.query(GoalEvent)
        .filter(
            GoalEvent.user_id == user.id,
            GoalEvent.event_date >= date.today(),
            GoalEvent.status == GoalStatus.upcoming,
        )
        .order_by(GoalEvent.event_date)
        .first()
    )

    context = {
        "rider_name": rider_name,
        "ride": {
            "title": ride.title,
            "date": str(ride.ride_date),
            # From the file's own GPS. Ground truth: if memory says the
            # rider is elsewhere, the file wins.
            "location_from_ride_gps": ride.location_name or None,
            # Conditions at the start line: a 250W headwind slog is not a
            # bad day. Read the performance against these.
            "weather_at_start": (
                ride.weather if ride.weather and not ride.weather.get("none") else None
            ),
            "duration_minutes": (ride.moving_time_seconds or ride.duration_seconds or 0) // 60,
            "distance_km": round((ride.distance_meters or 0) / 1000, 1),
            "elevation_m": round(ride.elevation_gain_meters or 0),
            "average_power": ride.average_power,
            "normalized_power": ride.normalized_power,
            "intensity_factor": ride.intensity_factor,
            "tss": ride.tss,
            "average_hr": ride.average_hr,
        },
        "fitness_after": {
            "ctl": round(fitness["ctl"], 1),
            "atl": round(fitness["atl"], 1),
            "tsb": round(fitness["tsb"], 1),
        },
        "ftp": user.ftp,
    }

    if linked_workout:
        context["planned_workout"] = {
            "title": session_display_name(
                linked_workout.workout_type, linked_workout.id
            ),
            "type": workout_type_label(linked_workout.workout_type),
            "structure": linked_workout.title,
            "description": linked_workout.description,
            "planned_duration_minutes": (linked_workout.planned_duration_seconds or 0) // 60,
            "planned_tss": linked_workout.planned_tss,
            "planned_if": linked_workout.planned_if,
        }

    if upcoming_goal:
        days_until = (upcoming_goal.event_date - date.today()).days
        context["upcoming_goal"] = {
            "event_name": upcoming_goal.event_name,
            "event_date": str(upcoming_goal.event_date),
            "days_until": days_until,
            "priority": str(upcoming_goal.priority),
        }

    # Long-term memory — the debrief should close loops: known gaps, applied
    # insights, patterns across rides. This is the coach's signature move.
    try:
        from app.services.memory_service import get_context as get_memory_context

        memory_block = get_memory_context(db, user, limit=15)
        if memory_block:
            context["long_term_memory"] = memory_block.split("\n")
    except Exception:
        logger.exception("Memory context for debrief failed (user=%s)", user.id)

    return context


RIDE_STORY_SYSTEM = """You write two tiny pieces for one ride in a cycling
training log, in the coach Forma's voice: warm, specific, a fan of the sport.

1. "title": a name for the ride, 2-6 words, evocative but honest — from the
   ride's actual character (its zones, duration, structure). Examples of the
   register: "Tempo blocks, three deep", "The long steady one", "Forty hard
   minutes". Never generic ("Morning Ride"), never cheesy, no exclamation marks.
2. "story": ONE sentence (max ~22 words) saying what the ride achieved and
   teaching one thing. Use the jargon AND gloss it simply, never condescending.
   No em dashes. British register.

Return STRICT JSON: {"title": "...", "story": "..."}"""


def generate_ride_story(db: Session, user: User, ride: Ride) -> None:
    """Write Forma's title + one-line story for a ride. Once, then cached."""
    if ride.story and ride.forma_title:
        return
    zs = ride.zone_summary or {}
    context = {
        "original_title": ride.title,
        "date": str(ride.ride_date)[:10],
        "location_from_ride_gps": ride.location_name or None,
        "duration_min": round(ride.duration_seconds / 60) if ride.duration_seconds else None,
        "distance_km": round(ride.distance_meters / 1000, 1) if ride.distance_meters else None,
        "elevation_m": round(ride.elevation_gain_meters) if ride.elevation_gain_meters else None,
        "avg_power": round(ride.average_power) if ride.average_power else None,
        "intensity_factor": ride.intensity_factor,
        "tss": round(ride.tss) if ride.tss else None,
        "dominant_zone": zs.get("dom"),
        "seconds_per_zone_z1_to_z7": zs.get("z"),
        "linked_to_planned_workout": bool(ride.workout_id),
    }
    try:
        response = forma_core.call(
            user_id=user.id,
            task="ride_story",
            surface="rides_list",
            system=RIDE_STORY_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"The ride:\n```json\n{json.dumps(context, default=str)}\n```",
            }],
        )
        raw = response_text(response).strip()
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        title = (data.get("title") or "").strip()[:255]
        story = (data.get("story") or "").strip()
        if title:
            ride.forma_title = title
        if story:
            ride.story = humanize(story)
        db.commit()
    except Exception:
        logger.exception("Ride story generation failed (ride=%s)", ride.id)


def generate_ride_debrief(
    db: Session, user: User, ride: Ride, force: bool = False
) -> dict:
    """
    Generate a coaching debrief for a completed ride.
    Cached on the ride record (debrief_text column).
    """
    # Check cache
    if ride.debrief_text and not force:
        return {
            "debrief": ride.debrief_text,
            "generated_at": str(ride.debrief_generated_at),
            "cached": True,
        }

    context = _build_debrief_context(db, user, ride)

    # The Rider Dossier: known facts + curiosity gaps. Post-ride is the most
    # natural drip moment — a bonk invites a fuelling question, a missed
    # session invites a time-reality one.
    try:
        from app.services.dossier_service import dossier_context

        dossier_block = dossier_context(db, user.id)
    except Exception:
        dossier_block = ""

    try:
        response = forma_core.call(
            user_id=user.id,
            task="debrief",
            surface="ride_detail",
            system=distilled_persona(user.coach_name, user.coach_tone)
            + DEBRIEF_SYSTEM_PROMPT
            + (f"\n\n{dossier_block}" if dossier_block else ""),
            messages=[{
                "role": "user",
                "content": f"Generate a post-ride debrief for this ride:\n\n```json\n{json.dumps(context, indent=2, default=str)}\n```",
            }],
        )
        debrief_text = humanize(response_text(response).strip())
    except Exception as e:
        logger.error(f"Debrief generation failed: {e}")
        rider_name = context["rider_name"]
        r = context["ride"]
        debrief_text = (
            f"Nice work out there, {rider_name}. "
            f"You logged {r['duration_minutes']} minutes"
        )
        if r.get("tss"):
            debrief_text += f" for {r['tss']:.0f} TSS"
        debrief_text += ". Every ride counts toward your goal, keep building."
        debrief_text = humanize(debrief_text)

    # Cache on ride
    ride.debrief_text = debrief_text
    ride.debrief_generated_at = datetime.utcnow()
    db.commit()

    return {
        "debrief": debrief_text,
        "generated_at": ride.debrief_generated_at.isoformat(),
        "cached": False,
    }


# ── Metric Explanation ───────────────────────────────────────────────────────

EXPLAIN_SYSTEM_PROMPT = """

## This surface: tap-to-explain a metric
Explain this metric in THE RIDER'S specific context. Personal and actionable.

Rules:
1. Address the rider by first name.
2. Explain what this metric means FOR THEM — not a generic definition.
3. Compare to benchmarks or their own history where relevant.
4. Suggest what to do about it (if applicable).
5. 2-4 sentences max. Be concise and conversational.
6. Return ONLY the explanation text. No JSON, no headings."""


def explain_metric(
    db: Session, user: User, metric_name: str, metric_value: float | str
) -> dict:
    """
    Generate a personalised explanation of a metric.
    No caching — these are cheap Haiku calls.
    """
    rider_name = (user.full_name or user.email.split("@")[0]).split()[0]
    fitness = get_current_fitness(db, user.id)

    context = {
        "rider_name": rider_name,
        "metric_name": metric_name,
        "metric_value": metric_value,
        "ftp": user.ftp,
        "weight_kg": user.weight_kg,
        "experience": user.experience_level,
        "fitness": {
            "ctl": round(fitness["ctl"], 1),
            "atl": round(fitness["atl"], 1),
            "tsb": round(fitness["tsb"], 1),
        },
    }

    # Long-term memory — explain the number in the context of THEIR journey.
    try:
        from app.services.memory_service import get_context as get_memory_context

        memory_block = get_memory_context(db, user, limit=8)
        if memory_block:
            context["long_term_memory"] = memory_block.split("\n")
    except Exception:
        logger.exception("Memory context for explain_metric failed (user=%s)", user.id)

    try:
        response = forma_core.call(
            user_id=user.id,
            task="explain",
            surface="metric_explain",
            system=distilled_persona(user.coach_name, user.coach_tone) + EXPLAIN_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Explain this metric to the rider:\n\n```json\n{json.dumps(context, default=str)}\n```",
            }],
        )
        explanation = humanize(response_text(response).strip())
    except Exception as e:
        logger.error(f"Metric explanation failed: {e}")
        explanation = humanize(f"{rider_name}, your {metric_name} is currently {metric_value}. This reflects your recent training load and recovery balance.")

    return {"explanation": explanation}
