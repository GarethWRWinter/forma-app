"""The coach's read — Forma's honest verdict on a goal.

Goalcraft applied to a specific target with the rider's real data on the
table: is it the right size (50/50), is it an end goal with a why or a
means goal wearing a number, does the bridge from here to race day actually
exist in the hours they have, and what would make it sing. Persisted on the
goal so it survives page loads; regenerated on demand.
"""

import json
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import forma_core
from app.core.coach_skills import distilled_persona
from app.core.llm_utils import humanize, response_text
from app.models.onboarding import GoalEvent
from app.models.ride import Ride
from app.models.user import User

logger = logging.getLogger(__name__)

READ_INSTRUCTIONS = """\
You are giving the rider your honest read on a goal they have set: part
sports scientist, part goalcraft coach. 120 to 200 words, flowing prose,
second person, no headings, no lists.

Cover, in whatever order serves this rider:
1. The size verdict: given their data (fitness, hours, weeks remaining,
   route demands), is this goal roughly 50/50 (right), a training plan
   wearing a bow (too safe), or currently a fantasy (needs a bridge or a
   rethink)? Be specific about WHY, with their numbers.
2. The soul check: if the goal has a why, honour it in one line. If it
   reads like a means goal (a number with no life in it), say what
   question you'd ask to find the end goal underneath.
3. One enhancement: the single change that would make this goal work
   harder for them (a checkpoint, a sharper definition, a process layer,
   or more audacity if it's too safe).
4. Close with one direct question that invites them to talk to you.

Ground every claim in the data provided; where data is missing, say what
you'd want to know rather than inventing it. Never pad. Never flatter.
The tone is the team car, not a horoscope."""


def generate_goal_read(db: Session, user: User, goal: GoalEvent) -> GoalEvent:
    """Write (or rewrite) the coach's read for an upcoming goal."""
    today = date.today()
    event_date = goal.event_date
    days_until = (event_date - today).days if event_date >= today else None

    # Recent training reality: the last six weeks in rough strokes.
    since = datetime.utcnow() - timedelta(days=42)
    agg = (
        db.query(
            func.count(Ride.id),
            func.coalesce(func.sum(Ride.duration_seconds), 0),
            func.coalesce(func.sum(Ride.distance_meters), 0),
        )
        .filter(Ride.user_id == user.id, Ride.ride_date >= since)
        .one()
    )
    rides_6w, secs_6w, metres_6w = agg

    route = goal.route_data or {}
    context = {
        "goal": {
            "name": goal.event_name,
            "date": str(goal.event_date),
            "days_until": days_until,
            "type": str(goal.event_type),
            "priority": str(goal.priority),
            "why": goal.why,
            "becoming": goal.becoming,
            "notes": goal.notes,
            "target_duration_minutes": goal.target_duration_minutes,
            "route_distance_km": route.get("total_distance_km"),
            "route_elevation_gain_m": route.get("elevation_gain_m"),
        },
        "rider": {
            "ftp": user.ftp,
            "weight_kg": user.weight_kg,
            "experience_level": user.experience_level,
            "weekly_hours_available": user.weekly_hours_available,
        },
        "last_6_weeks": {
            "rides": rides_6w,
            "hours": round(secs_6w / 3600, 1),
            "km": round(metres_6w / 1000),
        },
    }

    response = forma_core.call(
        user_id=user.id,
        task="goal_read",
        surface="goals",
        system=distilled_persona(user.coach_name, user.coach_tone)
        + "\n\n"
        + READ_INSTRUCTIONS,
        messages=[
            {
                "role": "user",
                "content": (
                    "Give me your read on this goal:\n```json\n"
                    + json.dumps(context, default=str)
                    + "\n```"
                ),
            }
        ],
    )
    goal.coach_read = humanize(response_text(response).strip())
    goal.coach_read_at = datetime.utcnow()
    db.commit()
    db.refresh(goal)
    return goal
