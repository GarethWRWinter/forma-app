"""Where a rider is on the road from account to habit, and the one next step.

Stages, in order: account, goal, data, first_ride, plan, first_week,
established. Computed from the database on every read, never stored, so it is
always true. The coach reads it on every message so a conversation can end
with the single thing that moves the rider forward (Nathan, 2 Sep 2026, was
told "next time we talk" and never came back). The outreach engine reads it to
know who has gone quiet at which step, and for how long.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.chat import ChatMessage, ChatSession
from app.models.integration import DropboxToken, StravaToken, WahooToken
from app.models.onboarding import GoalEvent, GoalStatus
from app.models.ride import Ride
from app.models.training import PlanStatus, TrainingPlan
from app.models.user import User

STAGES = ["account", "goal", "data", "first_ride", "plan", "first_week", "established"]

# Plain words, exact navigation, on-screen labels only. The coach relays these
# verbatim or in its own words; either way the rider ends up in the right
# place. "Connect Wahoo" is the button's actual label.
NEXT_ACTION: dict[str, dict] = {
    "goal": {
        "title": "Set the goal",
        "instruction": (
            "Tell me the event you're aiming at, or add it yourself in Settings "
            "under your goals. Everything I build hangs off it."
        ),
        "link": "/dashboard/settings",
    },
    "data": {
        "title": "Connect your rides",
        "instruction": (
            "I can't see any riding yet. Connect it: Settings, then Data in, then "
            "Connect Wahoo. Wahoo sends every ride the moment your head unit "
            "syncs. No Wahoo? Upload a ride file from the Rides page, or import "
            "your Strava archive in the same Data in section."
        ),
        "link": "/dashboard/settings",
    },
    "first_ride": {
        "title": "Get one ride in",
        "instruction": (
            "Your data door is open. The first ride that lands shows me how you "
            "actually ride, and I build from that rather than from a form. Go and "
            "ride, or upload a recent file from the Rides page."
        ),
        "link": "/dashboard/rides",
    },
    "plan": {
        "title": "Build the plan",
        "instruction": (
            "I have your goal and your riding. Ask me to build your plan and I'll "
            "write the first block now."
        ),
        "link": "/dashboard/coach",
    },
    "first_week": {
        "title": "Finish the first week",
        "instruction": (
            "The plan is live. Ride this week's sessions and tell me how each one "
            "felt. That is what lets me adjust next week rather than guess."
        ),
        "link": "/dashboard/training",
    },
}


@dataclass(frozen=True)
class Facts:
    has_goal: bool
    data_connected: bool
    ride_count: int
    has_active_plan: bool
    plan_started_at: datetime | None
    rides_since_plan: int
    last_activity: datetime
    now: datetime


def derive_stage(f: Facts) -> str:
    """Pure: the first unmet step, in order."""
    if not f.has_goal:
        return "goal"
    if not f.data_connected:
        return "data"
    if f.ride_count == 0:
        return "first_ride"
    if not f.has_active_plan:
        return "plan"
    week_done = (
        f.plan_started_at is not None
        and (f.now - f.plan_started_at).days >= 7
        and f.rides_since_plan >= 1
    )
    return "established" if week_done else "first_week"


def collect_facts(db: Session, user: User, now: datetime | None = None) -> Facts:
    now = now or datetime.utcnow()
    has_goal = (
        db.query(GoalEvent.id)
        .filter(GoalEvent.user_id == user.id, GoalEvent.status == GoalStatus.upcoming)
        .first()
        is not None
    )
    wahoo = db.query(WahooToken).filter(WahooToken.user_id == user.id).first()
    wahoo_ok = wahoo is not None and not wahoo.needs_reauth
    strava = db.query(StravaToken.id).filter(StravaToken.user_id == user.id).first() is not None
    dropbox = db.query(DropboxToken.id).filter(DropboxToken.user_id == user.id).first() is not None
    ride_count = db.query(func.count(Ride.id)).filter(Ride.user_id == user.id).scalar() or 0
    # A rider with rides in the system has a working door, whatever the tokens say.
    data_connected = wahoo_ok or strava or dropbox or ride_count > 0

    plan = (
        db.query(TrainingPlan)
        .filter(TrainingPlan.user_id == user.id, TrainingPlan.status == PlanStatus.active)
        .order_by(TrainingPlan.start_date.desc())
        .first()
    )
    plan_started_at = None
    rides_since_plan = 0
    if plan is not None:
        plan_started_at = datetime.combine(plan.start_date, datetime.min.time())
        rides_since_plan = (
            db.query(func.count(Ride.id))
            .filter(Ride.user_id == user.id, Ride.ride_date >= plan_started_at)
            .scalar()
            or 0
        )

    # Last sign of life: account creation, the latest thing said to the coach,
    # or the latest ride to arrive. Quiet time is measured from here.
    candidates = [user.created_at]
    last_msg = (
        db.query(func.max(ChatMessage.created_at))
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .filter(ChatSession.user_id == user.id)
        .scalar()
    )
    if last_msg:
        candidates.append(last_msg)
    last_ride = db.query(func.max(Ride.created_at)).filter(Ride.user_id == user.id).scalar()
    if last_ride:
        candidates.append(last_ride)
    last_activity = max(c for c in candidates if c is not None)

    return Facts(
        has_goal=has_goal,
        data_connected=bool(data_connected),
        ride_count=int(ride_count),
        has_active_plan=plan is not None,
        plan_started_at=plan_started_at,
        rides_since_plan=int(rides_since_plan),
        last_activity=last_activity,
        now=now,
    )


def activation_state(db: Session, user: User, now: datetime | None = None) -> dict:
    """What the coach, the dashboard and the outreach engine all read."""
    facts = collect_facts(db, user, now)
    stage = derive_stage(facts)
    quiet_days = max(0, (facts.now - facts.last_activity).days)
    return {
        "stage": stage,
        "stage_index": STAGES.index(stage),
        "next_action": NEXT_ACTION.get(stage),
        "quiet_days": quiet_days,
        "last_activity": facts.last_activity.isoformat(),
        "facts": {
            "has_goal": facts.has_goal,
            "data_connected": facts.data_connected,
            "ride_count": facts.ride_count,
            "has_active_plan": facts.has_active_plan,
        },
    }
