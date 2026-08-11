"""Pre-ride briefings: the team car before the stage.

Two registers, one per day, cached:
- daily: a light touch before training or an unstructured ride. Conditions
  at the rider's last-known location, where they help and hurt, what to
  wear and take, chain prep (doctrine: wax over oil, always).
- goal: the full talk on the morning of a goal event. Pep talk and mindset,
  conditions and how to use them, pacing grounded in the rider's numbers.

Provenance law applies: the forecast is data, the plan is data, everything
else the coach wants it asks for. No invented bedtimes.
"""

import json
import logging
from datetime import date as date_type

from sqlalchemy.orm import Session

from app.core import forma_core
from app.core.coach_skills import distilled_persona
from app.core.llm_utils import humanize, response_text
from app.models.briefing import Briefing
from app.models.onboarding import GoalEvent, GoalStatus
from app.models.ride import Ride, RideData
from app.models.training import Workout
from app.models.user import User

logger = logging.getLogger(__name__)

DAILY_INSTRUCTIONS = """\
Write today's PRE-RIDE BRIEFING for this rider: the quick word from the
team car window before they roll out. 120 to 180 words, warm and practical.

Cover, in flowing prose (a short list for kit is fine):
- Today's session (or an open ride if nothing is planned) in one line.
- The conditions and how to USE them: where the wind helps, where it bites,
  what the rain chance means for route or timing choices.
- What to wear and carry for these exact numbers (layers, shell, bottles).
- Chain prep when relevant. Doctrine: wax over oil, always.

Ground every claim in the forecast and plan provided. If no forecast is
available, brief without weather and say so plainly. Never invent the
rider's location, sleep or feelings. End with one line that makes them
want to ride."""

GOAL_INSTRUCTIONS = """\
Today is the rider's GOAL EVENT. Write the full team-car briefing they
read with their morning coffee. 250 to 400 words. This is the talk before
the stage: calm, confident, personal.

Structure it as flowing prose with short paragraphs:
1. Open with what today is and what they've done to earn it (use their
   actual training history numbers if provided).
2. Mindset: one steadying idea to return to when it gets hard.
3. Conditions: what the forecast means for THIS event, where it helps,
   where to be careful, kit for the numbers given.
4. Pacing: concrete guidance from their FTP and the event's shape. Give
   real watts for the long steady work and the rule for the hard moments.
5. Fuel and logistics in two lines. Chain doctrine: wax over oil.
6. Close like a directeur sportif who believes in them. No hype words,
   no exclamation pile-ups. One flamme line, then out.

Ground everything in the data provided. The forecast is data; their
numbers are data; anything else you want, you do not have, so do not
invent it."""


def _last_known_fix(db: Session, user_id: str) -> tuple[float, float, str | None] | None:
    """The start point of the rider's most recent GPS ride: our best honest
    guess at where they'll ride today."""
    recent = (
        db.query(Ride)
        .filter(Ride.user_id == user_id, Ride.location_name.isnot(None), Ride.location_name != "")
        .order_by(Ride.ride_date.desc())
        .first()
    )
    if recent is None:
        return None
    fix = (
        db.query(RideData.latitude, RideData.longitude)
        .filter(RideData.ride_id == recent.id, RideData.latitude.isnot(None))
        .order_by(RideData.elapsed_seconds)
        .first()
    )
    if fix is None:
        return None
    return (fix[0], fix[1], recent.location_name)


async def get_or_create_briefing(db: Session, user: User) -> Briefing:
    today = date_type.today()

    goal_today = (
        db.query(GoalEvent)
        .filter(
            GoalEvent.user_id == user.id,
            GoalEvent.event_date == today,
            GoalEvent.status == GoalStatus.upcoming,
        )
        .first()
    )
    kind = "goal" if goal_today else "daily"

    cached = (
        db.query(Briefing)
        .filter(Briefing.user_id == user.id, Briefing.date == today, Briefing.kind == kind)
        .first()
    )
    if cached:
        return cached

    # ---- Context ----
    rider_name = (user.full_name or user.email.split("@")[0]).split()[0]

    workouts_today = (
        db.query(Workout)
        .filter(Workout.user_id == user.id, Workout.scheduled_date == today)
        .all()
    )

    forecast = None
    locale = None
    fix = _last_known_fix(db, user.id)
    if fix is not None:
        from app.services import weather_service

        locale = fix[2]
        forecast = await weather_service.forecast_today(fix[0], fix[1])

    from app.services.metrics_service import get_current_fitness

    fitness = get_current_fitness(db, user.id)

    context: dict = {
        "rider_name": rider_name,
        "date": today.isoformat(),
        "ftp_watts": user.ftp,
        "fitness": fitness,
        "last_known_riding_area": locale,
        "forecast_at_that_area": forecast,
        "planned_sessions_today": [
            {
                "name": w.title,
                "type": str(w.workout_type) if getattr(w, "workout_type", None) else None,
                "planned_duration_min": (w.planned_duration_seconds or 0) // 60 or None,
                "planned_tss": w.planned_tss,
                "description": getattr(w, "description", None),
            }
            for w in workouts_today
        ],
    }
    if goal_today:
        context["goal_event"] = {
            "name": goal_today.event_name,
            "type": str(goal_today.event_type),
            "priority": str(goal_today.priority),
            "notes": goal_today.notes,
            "target_duration_minutes": goal_today.target_duration_minutes,
        }

    try:
        from app.services.dossier_service import dossier_context

        dossier_block = dossier_context(db, user.id)
    except Exception:
        dossier_block = ""

    instructions = GOAL_INSTRUCTIONS if kind == "goal" else DAILY_INSTRUCTIONS
    response = forma_core.call(
        user_id=user.id,
        task=f"briefing_{kind}",
        surface="today",
        system=distilled_persona(user.coach_name, user.coach_tone)
        + "\n\n" + instructions
        + (f"\n\n{dossier_block}" if dossier_block else ""),
        messages=[{
            "role": "user",
            "content": f"Brief me:\n```json\n{json.dumps(context, default=str)}\n```",
        }],
    )
    content = humanize(response_text(response).strip())

    briefing = Briefing(
        user_id=user.id,
        date=today,
        kind=kind,
        content=content,
        conditions=(forecast or {}).get("now") if forecast else None,
    )
    db.add(briefing)
    db.commit()
    db.refresh(briefing)
    return briefing
