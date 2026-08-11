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

You do not know today's route, so keep wind advice general and honest:
"wind from the {direction}, so expect it in your face heading that way
and a push coming home" framed around their region's geography. Never
invent a route.

Ground every claim in the forecast and plan provided. If no forecast is
available, brief without weather and say so plainly. Never invent the
rider's location, sleep or feelings. Close with ONE short clarifying
question about today (route, timing, or how they're feeling), and let
them know they can tap through to talk it through properly with you."""

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

If `route_wind_segments` is provided, this is where you earn your seat in
the car: walk the course in order and turn wind into ENERGY STRATEGY.
Name the stretches by their kilometre marks: where the headwind is a tax
to be paid patiently (sit in, hold steady watts, never chase), where the
tailwind is free speed to bank (this is where the pace goes up for less
cost), and where crosswinds demand attention to positioning. Two or three
decisive stretches, not a segment-by-segment recitation.

Ground everything in the data provided. The forecast is data; their
numbers are data; anything else you want, you do not have, so do not
invent it. Close with ONE sharp clarifying question (their plan for
fuelling, their start-line feeling, or the stretch that worries them),
and let them know the team car channel is open all day: tap through and
talk it out."""


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees clockwise from north."""
    import math

    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360

def _wind_class(bearing: float, wind_from_deg: float) -> str:
    """head | tail | cross for a rider travelling on `bearing` with wind
    blowing FROM `wind_from_deg` (meteorological convention)."""
    wind_to = (wind_from_deg + 180) % 360
    diff = abs(((wind_to - bearing + 540) % 360) - 180)
    if diff <= 60:
        return "tail"
    if diff >= 120:
        return "head"
    return "cross"

def analyze_route_wind(track: list[list[float]], wind_from_deg: float) -> list[dict]:
    """Walk the route, classify each stretch against the wind, and merge
    consecutive same-class stretches into segments the coach can narrate:
    [{"from_km", "to_km", "wind": "head|tail|cross"}]."""
    if not track or len(track) < 2:
        return []
    segments: list[dict] = []
    for i in range(1, len(track)):
        klass = _wind_class(
            _bearing(track[i - 1][0], track[i - 1][1], track[i][0], track[i][1]),
            wind_from_deg,
        )
        if segments and segments[-1]["wind"] == klass:
            segments[-1]["to_km"] = track[i][2]
        else:
            segments.append(
                {"from_km": track[i - 1][2], "to_km": track[i][2], "wind": klass}
            )
    # Absorb blips under 1km into their neighbour: the coach talks in
    # stretches, not GPS noise.
    merged: list[dict] = []
    for seg in segments:
        if merged and (seg["to_km"] - seg["from_km"]) < 1.0:
            merged[-1]["to_km"] = seg["to_km"]
        elif merged and merged[-1]["wind"] == seg["wind"]:
            merged[-1]["to_km"] = seg["to_km"]
        else:
            merged.append(dict(seg))
    return merged

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

    # Where to read the sky: the goal route's start line when today is the
    # day and a route exists; the rider's last-known riding area otherwise.
    forecast = None
    locale = None
    route_data = (goal_today.route_data or {}) if goal_today else {}
    route_track = route_data.get("track") or []
    route_start = route_data.get("start") or {}

    from app.services import weather_service

    if goal_today and route_start.get("lat") is not None:
        locale = goal_today.event_name
        forecast = await weather_service.forecast_today(
            route_start["lat"], route_start["lon"]
        )
    else:
        fix = _last_known_fix(db, user.id)
        if fix is not None:
            locale = fix[2]
            forecast = await weather_service.forecast_today(fix[0], fix[1])

    # Per-segment wind reading for the route, when we know both the route
    # and the wind. This is what turns "windy today" into strategy.
    wind_segments: list[dict] = []
    wind_now = (forecast or {}).get("now") or {}
    if route_track and forecast:
        # Use the forecast's raw wind bearing: pull from the first hour row
        # (compact rows carry compass only), fall back to current.
        wind_deg = None
        for h in (forecast.get("hours") or []):
            if h.get("wind_deg") is not None:
                wind_deg = h["wind_deg"]
                break
        if wind_deg is None and wind_now.get("wind_deg") is not None:
            wind_deg = wind_now["wind_deg"]
        if wind_deg is not None:
            wind_segments = analyze_route_wind(route_track, wind_deg)

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
            "route_distance_km": route_data.get("total_distance_km"),
            "route_elevation_gain_m": route_data.get("elevation_gain_m"),
        }
        if wind_segments:
            context["route_wind_segments"] = wind_segments

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

    conditions: dict | None = None
    if forecast:
        conditions = {"now": forecast.get("now"), "day": forecast.get("day")}
        if route_track and wind_segments:
            # Persisted so the goal-day map survives cache hits: the route,
            # its wind segments, and the wind that produced them.
            conditions["route"] = {
                "track": [[p[0], p[1]] for p in route_track],
                "segments": wind_segments,
                "wind_deg": next(
                    (h.get("wind_deg") for h in (forecast.get("hours") or []) if h.get("wind_deg") is not None),
                    wind_now.get("wind_deg"),
                ),
                "wind_kph": wind_now.get("wind_kph"),
                "km": [p[2] for p in route_track],
            }

    briefing = Briefing(
        user_id=user.id,
        date=today,
        kind=kind,
        content=content,
        conditions=conditions,
    )
    db.add(briefing)
    db.commit()
    db.refresh(briefing)
    return briefing
