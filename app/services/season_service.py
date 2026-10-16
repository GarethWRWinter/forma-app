"""Between goals: a next step and a plan that keeps going.

16 Oct 2026: Gareth's goal (Sa Calobra sub 30, 4 October) had passed. His plan
ended with it, and nothing ever happened when a plan reached its end date: it
stayed "active", the plan page read "week 7 of 7" over empty days, and the
only thing on offer was a banner asking what he wanted to chase. A rider must
never be left with nothing to do.

The rule, kept here and nowhere else:
- A plan whose end date has passed is ended (completed). Its weeks stay on the
  calendar.
- A rider with no goal ahead and no current plan gets a 12-week block from the
  plan generator, named BLOCK_NAME, so every week has sessions until the next
  goal is set.
- A rider with a goal ahead and no current plan, or a current plan aimed at no
  goal (the block), has the plan rebuilt around the goal. That is how the
  block gives way, by whatever route the goal arrived.
- Only riders who have been on a plan before, and who have access, are
  touched. A rider still in setup (even one whose only goal is in the past)
  is left to the setup steps, which build the first plan with them.

ensure_season runs hourly from inside the app, and straight after every goal
write (the goals API and the coach's goal tools). It is idempotent: the
condition is the guard, because once a block exists there is a current plan.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.models.onboarding import GoalEvent, GoalStatus
from app.models.training import PlanStatus, TrainingPlan
from app.models.user import User

logger = logging.getLogger(__name__)

BLOCK_NAME = "Building to the next goal"


@dataclass
class _Snapshot:
    future: list[GoalEvent]  # upcoming and dated today or later, soonest first
    race_day: bool
    last_goal: GoalEvent | None  # the most recent goal no longer ahead
    current: TrainingPlan | None  # active and not past its end date
    expired: list[TrainingPlan]  # active, but past their end date
    ever_planned: bool


def _status(goal: GoalEvent) -> str:
    return getattr(goal.status, "value", goal.status) or GoalStatus.upcoming.value


def _read(db: Session, user: User, today: date) -> _Snapshot:
    goals = db.query(GoalEvent).filter(GoalEvent.user_id == user.id).all()
    future = sorted(
        (g for g in goals if _status(g) == GoalStatus.upcoming.value and g.event_date >= today),
        key=lambda g: g.event_date,
    )
    # Race day counts as chasing, whatever its result says, so a block
    # never starts on the day of the race itself.
    race_day = any(g.event_date == today for g in goals)
    behind = [
        g for g in goals
        if g.event_date != today
        and (g.event_date < today or _status(g) != GoalStatus.upcoming.value)
    ]
    last_goal = max(behind, key=lambda g: g.event_date, default=None)

    plans = (
        db.query(TrainingPlan)
        .filter(TrainingPlan.user_id == user.id)
        .order_by(TrainingPlan.created_at.desc())
        .all()
    )
    active = [p for p in plans if p.status == PlanStatus.active]
    return _Snapshot(
        future=future,
        race_day=race_day,
        last_goal=last_goal,
        current=next((p for p in active if p.end_date >= today), None),
        expired=[p for p in active if p.end_date < today],
        ever_planned=bool(plans),
    )


def _state(snap: _Snapshot) -> str:
    if snap.future or snap.race_day:
        return "chasing"
    if snap.last_goal is not None:
        return "between_goals"
    return "no_goal"


def _eligible(user: User, snap: _Snapshot) -> bool:
    """Riders the keeper may build for: past setup, and members."""
    from app.services.billing_service import has_access

    if not user.is_active or user.deleted_at is not None:
        return False
    if not snap.ever_planned:
        return False
    return has_access(user)


def _action(user: User, snap: _Snapshot, today: date) -> str | None:
    """What the keeper would build now: "block", "goal_plan" or nothing."""
    if not _eligible(user, snap):
        return None
    if snap.future or snap.race_day:
        # A goal dated today is raced, not planned for.
        if any(g.event_date > today for g in snap.future) and (
            snap.current is None or snap.current.goal_event_id is None
        ):
            return "goal_plan"
        return None
    return "block" if snap.current is None else None


def season_state(db: Session, user: User, today: date | None = None) -> dict:
    """Where the rider stands between goals. Reads only."""
    today = today or date.today()
    snap = _read(db, user, today)
    out: dict = {
        "state": _state(snap),
        "last_goal": None,
        "current_plan": None,
        # Whether a goal filed now rebuilds the plan by itself.
        "rebuilds": _eligible(user, snap),
    }
    if snap.last_goal is not None:
        g = snap.last_goal
        out["last_goal"] = {
            "goal_id": g.id,
            "event_name": g.event_name,
            "event_date": g.event_date,
            "days_ago": (today - g.event_date).days,
            "status": _status(g),
            "result_recorded": _status(g) != GoalStatus.upcoming.value,
        }
    if snap.current is not None:
        p = snap.current
        out["current_plan"] = {
            "id": p.id,
            "name": p.name,
            "start": p.start_date,
            "end": p.end_date,
            "weeks": max(1, (p.end_date - p.start_date).days // 7),
            "goal_less": p.goal_event_id is None,
        }
    return out


def season_note(state: dict, in_chat: bool = True) -> str | None:
    """What the coach is told when the rider has no goal ahead, or None.
    in_chat: the coach can file the goal itself (the morning note cannot)."""
    last = state.get("last_goal")
    if state.get("state") != "between_goals" or not last:
        return None
    days = last["days_ago"]
    if days > 0:
        when = "yesterday" if days == 1 else f"{days} days ago"
        parts = [f"No goal is set beyond today. Their last goal, {last['event_name']}, was {when}."]
    else:
        # Dated ahead, but already settled (a DNS filed early).
        parts = [
            f"No goal is set beyond today. Their last goal, {last['event_name']}, "
            f"set for {last['event_date'].isoformat()}, is recorded as {last['status']}."
        ]
    if not last["result_recorded"]:
        parts.append(
            "Its result is not recorded yet: ask how it went first, and send them "
            "to Goal, then Tell me how it went, which opens the race report."
        )
    plan = state.get("current_plan")
    if plan and plan["goal_less"]:
        parts.append(
            f"They are on '{plan['name']}', a {plan['weeks']}-week block that builds "
            "their base, so every week has sessions until a new goal is set."
        )
    elif plan:
        parts.append(f"They are on '{plan['name']}' until {plan['end'].isoformat()}.")
    else:
        parts.append("They have no current plan, so only the sessions in this_week are booked.")
    if in_chat:
        then = (
            "the plan is rebuilt around it at once." if state.get("rebuilds")
            else "they build the plan from Goal, then Build my season."
        )
        parts.append(
            "Propose the next goal yourself from what you know of them and their "
            "riding; do not ask whether they want one. When they agree, file it "
            f"with create_goal and {then}"
        )
    else:
        parts.append(
            "Propose the next goal yourself from what you know of them and their "
            "riding, and spend the question on settling it. They answer on the "
            "Coach page."
        )
    return " ".join(parts)


def _lock(db: Session, user: User) -> None:
    """Serialise the keeper and a request for the same rider. Postgres holds
    the row until the next commit; SQLite ignores FOR UPDATE, which is fine
    for tests."""
    db.query(User.id).filter(User.id == user.id).with_for_update().first()


def ensure_season(db: Session, user: User, today: date | None = None) -> str:
    """End expired plans and give the rider the plan their goals call for.

    Returns "block", "goal_plan", "retired" (only plans ended) or "unchanged".
    """
    from app.services.plan_service import generate_plan, retire_plan

    today = today or date.today()
    _lock(db, user)
    snap = _read(db, user, today)
    for plan in snap.expired:
        retire_plan(db, plan, today)
    db.flush()
    action = _action(user, snap, today)
    if action is None:
        db.commit()
        return "retired" if snap.expired else "unchanged"
    # generate_plan ends any other active plan and commits, which also
    # releases the lock.
    if action == "block":
        generate_plan(db, user, name=BLOCK_NAME)
    else:
        generate_plan(db, user)
    logger.info("Season keeper: %s for user %s", action, user.id)
    return action


def try_ensure_season(db: Session, user: User) -> str | None:
    """ensure_season for a request that has already done its own work (a goal
    filed, a report saved). A failure here is logged and rolled back, never
    allowed to undo or hide what the rider just did."""
    try:
        return ensure_season(db, user)
    except Exception:
        logger.exception("Season keeper failed for user %s", user.id)
        db.rollback()
        return None


def ensure_all(db: Session, today: date | None = None, dry_run: bool = False) -> Counter:
    """ensure_season for every open account. Each rider commits on their own,
    so one failure never holds up the rest. A dry run writes nothing and
    counts what would happen."""
    today = today or date.today()
    counts: Counter = Counter()
    riders = (
        db.query(User)
        .filter(User.is_active.is_(True), User.deleted_at.is_(None))
        .all()
    )
    for user in riders:
        try:
            if dry_run:
                snap = _read(db, user, today)
                action = _action(user, snap, today)
                outcome = action or ("retired" if snap.expired else "unchanged")
                if outcome != "unchanged":
                    logger.info("[dry run] %s for user %s", outcome, user.id)
            else:
                outcome = ensure_season(db, user, today)
            counts[outcome] += 1
        except Exception:
            db.rollback()
            counts["failed"] += 1
            logger.exception("Season keeper failed for user %s", user.id)
    return counts


_task: asyncio.Task | None = None


async def _loop(interval: int, first_delay: int) -> None:
    from app.database import SessionLocal

    await asyncio.sleep(first_delay)
    while True:
        def _run() -> Counter:
            db = SessionLocal()
            try:
                return ensure_all(db)
            finally:
                db.close()

        try:
            counts = await asyncio.to_thread(_run)
            changed = {k: v for k, v in counts.items() if k != "unchanged"}
            if changed:
                logger.info("Season keeper: %s", changed)
        except Exception:
            logger.exception("Season keeper pass failed")
        await asyncio.sleep(interval)


def start_season_keeper(interval: int = 3600, first_delay: int = 60) -> None:
    """Run the keeper hourly inside the app, so a goal's date passing is
    picked up within the hour of midnight (UTC) without anyone remembering
    to run a script."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(interval, first_delay))
