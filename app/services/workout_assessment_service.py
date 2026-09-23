"""
Workout execution assessment, compares a planned workout to the actual
ride that was ridden, produces a numeric quality score out of 10, and asks
Coach Forma for supportive feedback and adjustments to the next few days.

The numeric score is deterministic (no Claude call), so the UI can show it
instantly. The written feedback is generated on demand by Claude and then
cached on the workout row.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from app.core import forma_core
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.ride import Ride
from app.models.training import Workout, WorkoutStatus, WorkoutType
from app.models.user import User
from app.core.llm_utils import humanize, response_text
from app.core.coach_skills import distilled_persona

logger = logging.getLogger(__name__)


# --- Expected IF band per planned workout type --------------------------------

# Centre IF + half-width, used both to award "workout type match" points and
# to tell Claude what the target intensity looked like.
WORKOUT_TYPE_IF_BAND: dict[str, tuple[float, float]] = {
    WorkoutType.recovery.value: (0.55, 0.10),
    WorkoutType.endurance.value: (0.68, 0.08),
    WorkoutType.tempo.value: (0.82, 0.06),
    WorkoutType.sweet_spot.value: (0.89, 0.05),
    WorkoutType.threshold.value: (0.96, 0.05),
    WorkoutType.vo2max.value: (1.05, 0.08),
    WorkoutType.sprint.value: (1.15, 0.15),
    WorkoutType.rest.value: (0.0, 0.15),
}


# --- Scoring ------------------------------------------------------------------

def _triangle_score(delta: float, full_at: float, zero_at: float) -> float:
    """1.0 when |delta| <= full_at, 0.0 when |delta| >= zero_at, linear between."""
    d = abs(delta)
    if d <= full_at:
        return 1.0
    if d >= zero_at:
        return 0.0
    return 1.0 - (d - full_at) / (zero_at - full_at)


def score_execution(workout: Workout, ride: Ride) -> dict:
    """
    Compute a deterministic execution score out of 10 with one decimal place.

    Breaks the 10 points into four weighted components so the UI / LLM can
    explain what happened:
      - Duration match        (max 2.5)
      - Intensity match (IF)  (max 3.5)
      - TSS match             (max 2.0)
      - Workout type match    (max 2.0)

    Any missing data (e.g. no power meter, so no IF/TSS) is handled gracefully
    by re-distributing weight to the components that *can* be scored.
    """
    components: dict[str, dict] = {}

    # --- Duration ---
    planned_dur = workout.planned_duration_seconds
    actual_dur = ride.moving_time_seconds or ride.duration_seconds
    if planned_dur and actual_dur:
        delta_pct = (actual_dur - planned_dur) / planned_dur
        pts = _triangle_score(delta_pct, full_at=0.15, zero_at=0.50) * 2.5
        components["duration"] = {
            "max": 2.5,
            "earned": round(pts, 2),
            "planned_seconds": planned_dur,
            "actual_seconds": actual_dur,
            "delta_pct": round(delta_pct * 100, 1),
        }

    # --- Intensity factor ---
    planned_if = workout.planned_if
    actual_if = ride.intensity_factor
    if planned_if and actual_if:
        delta = actual_if - planned_if
        pts = _triangle_score(delta, full_at=0.05, zero_at=0.30) * 3.5
        components["intensity"] = {
            "max": 3.5,
            "earned": round(pts, 2),
            "planned_if": round(planned_if, 3),
            "actual_if": round(actual_if, 3),
            "delta": round(delta, 3),
        }

    # --- TSS ---
    planned_tss = workout.planned_tss
    actual_tss = ride.tss
    if planned_tss and actual_tss:
        delta_pct = (actual_tss - planned_tss) / planned_tss
        pts = _triangle_score(delta_pct, full_at=0.15, zero_at=0.50) * 2.0
        components["tss"] = {
            "max": 2.0,
            "earned": round(pts, 2),
            "planned_tss": round(planned_tss, 1),
            "actual_tss": round(actual_tss, 1),
            "delta_pct": round(delta_pct * 100, 1),
        }

    # --- Workout type match ---
    # Award full points when the actual IF sits inside the expected band for
    # the planned workout type; linearly degrade outside it.
    if actual_if and workout.workout_type in WORKOUT_TYPE_IF_BAND:
        centre, half = WORKOUT_TYPE_IF_BAND[workout.workout_type]
        delta = actual_if - centre
        pts = _triangle_score(delta, full_at=half, zero_at=half * 3) * 2.0
        components["workout_type"] = {
            "max": 2.0,
            "earned": round(pts, 2),
            "planned_type": workout.workout_type,
            "expected_if_band": [round(centre - half, 2), round(centre + half, 2)],
            "actual_if": round(actual_if, 3),
        }

    if not components:
        # No comparable metrics at all, give a neutral "ride was completed" score.
        return {
            "score": 5.0,
            "components": {},
            "note": "Not enough overlap between plan and ride to score in detail.",
        }

    # Rescale to /10 based on which components were actually scored.
    earned = sum(c["earned"] for c in components.values())
    possible = sum(c["max"] for c in components.values())
    score_10 = round((earned / possible) * 10.0, 1) if possible > 0 else 5.0

    return {
        "score": score_10,
        "components": components,
    }


# --- Supportive feedback via Claude -------------------------------------------

ASSESSMENT_SYSTEM_PROMPT = """

## This surface: workout execution assessment
You are a supportive, data-driven \
cycling coach. A rider just completed a ride and you are giving them feedback \
on how it matched the plan.

Rules for your response:
1. Always be supportive and constructive. Never demoralising. Celebrate what \
   went well before addressing what could improve.
2. Refer to the rider by first name.
3. Use the supplied numeric score as the starting point but add *why* the ride \
   earned that score in plain English (2-3 short paragraphs max).
4. If the actual ride differed meaningfully from the plan (too hard, too easy, \
   too short, too long, wrong energy system), explain the physiological \
   implication briefly and tell the rider how you will adjust the next few \
   days to keep them on track for their goal.
5. Return STRICT JSON only, with this exact shape:
   {
     "feedback": "...supportive 2-3 paragraph response...",
     "adjustments": [
       {"date": "YYYY-MM-DD", "change": "...", "reason": "..."}
     ]
   }
   - `feedback` is a single markdown string (plain paragraphs separated by \
     blank lines, no headings).
   - `adjustments` may be an empty array if no changes are needed.
6. Only suggest changes to workouts in the `upcoming_workouts` list; do not \
   invent new ones. If no change is needed, return an empty `adjustments` array."""


def _build_assessment_prompt(
    user: User,
    workout: Workout,
    ride: Ride,
    score_result: dict,
    upcoming: list[Workout],
) -> str:
    """Build the user message payload for Claude."""
    rider_name = (user.first_name or user.email.split("@")[0]).strip()

    planned = {
        "title": workout.title,
        "description": workout.description,
        "workout_type": workout.workout_type,
        "planned_duration_seconds": workout.planned_duration_seconds,
        "planned_tss": workout.planned_tss,
        "planned_if": workout.planned_if,
        "scheduled_date": str(workout.scheduled_date),
    }

    actual = {
        "title": ride.title,
        "ride_date": str(ride.ride_date),
        "moving_time_seconds": ride.moving_time_seconds,
        "distance_meters": ride.distance_meters,
        "elevation_gain_meters": ride.elevation_gain_meters,
        "average_power": ride.average_power,
        "normalized_power": ride.normalized_power,
        "intensity_factor": ride.intensity_factor,
        "tss": ride.tss,
        "average_hr": ride.average_hr,
    }

    upcoming_list = [
        {
            "workout_id": w.id,
            "date": str(w.scheduled_date),
            "title": w.title,
            "workout_type": w.workout_type,
            "planned_duration_seconds": w.planned_duration_seconds,
            "planned_tss": w.planned_tss,
            "planned_if": w.planned_if,
        }
        for w in upcoming
    ]

    payload = {
        "rider_first_name": rider_name,
        "today": date.today().isoformat(),
        "score_out_of_10": score_result["score"],
        "score_breakdown": score_result.get("components", {}),
        "planned_workout": planned,
        "actual_ride": actual,
        "upcoming_workouts": upcoming_list,
    }

    return (
        "Here is the data for the completed ride and the next few days of the "
        "plan. Follow the rules and respond with JSON only.\n\n```json\n"
        + json.dumps(payload, indent=2, default=str)
        + "\n```"
    )


def _get_upcoming_workouts(
    db: Session, user_id: str, after: date, days: int = 4
) -> list[Workout]:
    """Return the next `days` days of workouts after the given date."""
    end = after + timedelta(days=days)
    return (
        db.query(Workout)
        .filter(
            Workout.user_id == user_id,
            Workout.scheduled_date > after,
            Workout.scheduled_date <= end,
        )
        .order_by(Workout.scheduled_date, Workout.sort_order)
        .all()
    )


def _parse_claude_json(text: str) -> dict:
    """Extract a JSON object from Claude's response, tolerant of code fences."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)
        # t == ["", "json\n{...}", "..."] after split, take the middle piece
        if len(t) >= 2:
            inner = t[1]
            if inner.startswith("json"):
                inner = inner[len("json"):]
            t = inner.strip()
        else:
            t = ""
    else:
        pass
    # Last-resort: grab from first "{" to last "}"
    if isinstance(t, str):
        first = t.find("{")
        last = t.rfind("}")
        if first != -1 and last != -1 and last > first:
            t = t[first : last + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        logger.warning("Failed to parse Claude assessment JSON: %r", text[:400])
        return {"feedback": text.strip(), "adjustments": []}


def generate_assessment(
    db: Session,
    user: User,
    workout: Workout,
    force: bool = False,
) -> Workout:
    """
    Generate (or reuse) an execution assessment for a workout.

    Scoring is always recomputed so it reflects the latest ride metrics.
    Claude feedback is only generated if there is no cached feedback or
    `force` is set, to keep API costs predictable.
    """
    if not workout.actual_ride_id:
        raise ValueError("Workout has no linked ride to assess")

    # user-scope the ride read, a linked ride must belong to the same user
    ride = (
        db.query(Ride)
        .filter(Ride.id == workout.actual_ride_id, Ride.user_id == user.id)
        .first()
    )
    if not ride:
        raise ValueError("Linked ride not found")

    score_result = score_execution(workout, ride)
    workout.execution_score = score_result["score"]

    need_llm = force or not workout.execution_feedback
    if need_llm:
        upcoming = _get_upcoming_workouts(db, user.id, workout.scheduled_date)
        user_msg = _build_assessment_prompt(user, workout, ride, score_result, upcoming)

        # One system, not separate features: assessments see the same memory
        # and dossier every other coach surface sees.
        extra_context = ""
        try:
            from app.services.memory_service import get_context as get_memory_context

            mem = get_memory_context(db, user, limit=16)
            if mem:
                extra_context += f"\n\n{mem}"
        except Exception:
            pass
        try:
            from app.services.dossier_service import dossier_context

            dossier = dossier_context(db, user.id)
            if dossier:
                extra_context += f"\n\n{dossier}"
        except Exception:
            pass

        try:
            response = forma_core.call(
                user_id=user.id,
                task="assessment",
                surface="workout_detail",
                system=distilled_persona(user.coach_name, user.coach_tone)
                + ASSESSMENT_SYSTEM_PROMPT
                + extra_context,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = humanize(response_text(response))
            parsed = _parse_claude_json(text)
            workout.execution_feedback = parsed.get("feedback") or text
            workout.execution_adjustments = parsed.get("adjustments") or []
        except Exception as e:  # noqa: BLE001
            logger.exception("Assessment LLM call failed: %s", e)
            # Fall back to a deterministic message so the UI still has
            # something to show instead of an empty card.
            score = score_result["score"]
            workout.execution_feedback = (
                f"Nice work getting that session done, you scored {score}/10 on "
                "execution vs the plan. I'll write a proper debrief as soon as "
                "I'm back online; in the meantime, stay consistent and the "
                "numbers will take care of themselves."
            )
            workout.execution_adjustments = []

    workout.execution_assessed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(workout)
    return workout


# --- Auto-link ----------------------------------------------------------------

def backfill_auto_links(db: Session, user_id: str, days: int = 14) -> int:
    """
    Retroactively link any rides from the past `days` days that don't yet
    have an associated workout. Idempotent, skips rides already linked.

    Returns the number of newly-linked rides.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    unlinked_rides = (
        db.query(Ride)
        .outerjoin(Workout, Workout.actual_ride_id == Ride.id)
        .filter(
            Ride.user_id == user_id,
            Ride.ride_date >= since,
            Workout.id.is_(None),  # no workout points at this ride
        )
        .order_by(Ride.ride_date.desc())
        .all()
    )

    count = 0
    for ride in unlinked_rides:
        if auto_link_ride_to_workout(db, ride) is not None:
            count += 1
    return count


def auto_link_ride_to_workout(db: Session, ride: Ride) -> Workout | None:
    """
    When a new ride is imported, try to link it to a planned workout for the
    same calendar date that hasn't already been satisfied.

    Returns the linked workout (or None if no match).
    """
    if not ride.ride_date:
        return None

    ride_day = ride.ride_date.date() if hasattr(ride.ride_date, "date") else ride.ride_date

    candidate = (
        db.query(Workout)
        .filter(
            Workout.user_id == ride.user_id,
            Workout.scheduled_date == ride_day,
            Workout.actual_ride_id.is_(None),
            or_(
                Workout.status == WorkoutStatus.planned,
                Workout.status == WorkoutStatus.modified,
            ),
        )
        .order_by(Workout.sort_order)
        .first()
    )

    if not candidate:
        return None

    candidate.actual_ride_id = ride.id
    candidate.status = WorkoutStatus.completed
    db.commit()
    db.refresh(candidate)
    logger.info(
        "Auto-linked ride %s to workout %s (%s on %s)",
        ride.id, candidate.id, candidate.title, ride_day,
    )
    return candidate
