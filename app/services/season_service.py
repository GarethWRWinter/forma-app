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
  goal (the block) or at a goal no longer ahead, has the plan rebuilt around
  the goal. That is how the block gives way, by whatever route the goal
  arrived. A plan still aimed at a goal that was filed as not started, moved
  into the past or passed while a later goal kept the plan going is replaced
  the same way: by the block when nothing is ahead, by a plan for the next
  goal when one is.
- A plan built within a week of an A or B race opens with a recovery week
  (plan_service.generate_plan decides), so nobody is handed intensity the
  morning after their race.
- Only riders who have been on a plan before, and who have access, are
  touched. A rider still in setup (even one whose only goal is in the past)
  is left to the setup steps, which build the first plan with them.
- Nothing is built for an account held as possibly under 18, and nothing new
  while the safety record says to wait (_held_back): an injury hold, eating
  very little or fast weight loss in the last 28 days, or words of
  hopelessness in the last 14. Expired plans still end. The block or the
  goal's plan follows on the first pass after the wait is over.
- A plan built within a week of a DNF or DNS the body caused (illness,
  injury, a crash, the body saying no), or for such a DNS filed ahead of the
  day, opens with a recovery week too: the race report has just told the
  rider the next step is one easy ride. Any other DNS was never ridden, so
  plan_service's own rule (no recovery week) stands.

ensure_season runs hourly from inside the app, and straight after every goal
write (the goals API and the coach's goal tools). It is idempotent: the
condition is the guard, because once a block exists there is a current plan.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.models.onboarding import GoalEvent, GoalStatus
from app.models.training import PlanStatus, TrainingPlan
from app.models.user import User

logger = logging.getLogger(__name__)

BLOCK_NAME = "Building to the next goal"


@dataclass
class _Snapshot:
    future: list[GoalEvent]  # upcoming and dated today or later, soonest first
    held: set[str]  # ids of the goals a plan may still aim at: ahead, or raced today
    race_day: bool
    last_goal: GoalEvent | None  # the most recent goal no longer ahead
    current: TrainingPlan | None  # active and not past its end date, the newest
    expired: list[TrainingPlan]  # active, but past their end date
    extra: list[TrainingPlan]  # current too, but older than `current`
    ever_planned: bool
    last_model: str | None  # the periodisation of the newest plan
    minor_held: bool = False  # held as possibly under 18: nothing is built
    held_back: str | None = None  # why nothing new is built now (_held_back)


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
    running = [p for p in active if p.end_date >= today]
    model = plans[0].periodization_model if plans else None
    try:
        from app.services.plan_review_service import minor_hold_open

        minor_held = minor_hold_open(db, user)
    except Exception:
        logger.exception("Checking for an under-18 hold failed (user=%s)", user.id)
        minor_held = True  # fail closed: nothing is built for a child
    return _Snapshot(
        future=future,
        held={g.id for g in future} | {g.id for g in goals if g.event_date == today},
        race_day=race_day,
        last_goal=last_goal,
        current=running[0] if running else None,
        expired=[p for p in active if p.end_date < today],
        extra=running[1:],
        ever_planned=bool(plans),
        last_model=getattr(model, "value", model),
        minor_held=minor_held,
        held_back=_held_back(db, user),
    )


def _held_back(db: Session, user: User) -> str | None:
    """Why nothing new may be built for this rider right now, or None.

    "injury": an injury hold is open, and no ride goes in the plan until it
    lifts (SAFETY LAW 2e). "restriction": eating very little or fast weight
    loss came up in the last 28 days, and nothing adds load (2h).
    "wellbeing": words of hopelessness or worse in the last 14 days, and
    nothing presses them (safety_screen.wellbeing_quiet_until). "unknown":
    the record could not be read, so nothing is built on a guess."""
    from app.services import safety_screen
    from app.services.plan_review_service import open_hold_for, restriction_flagged

    try:
        if open_hold_for(db, user, "injury") is not None:
            return "injury"
        if restriction_flagged(db, user):
            return "restriction"
        quiet = safety_screen.wellbeing_quiet_until(db, user.id)
        if quiet is not None and quiet > datetime.utcnow():
            return "wellbeing"
        return None
    except Exception:
        logger.exception("Reading the safety record for the season failed (user=%s)", user.id)
        return "unknown"


# The race report's reasons (goals/[id]/assess) that say the body stopped
# the rider: after these the next step is one easy ride, never intensity.
_BODY_REASONS = {"dnf_reason": {"crash", "illness", "body"}, "dns_reason": {"illness", "injury"}}


def _fresh_setback(snap: _Snapshot, today: date) -> bool:
    """The last goal was a DNF or DNS the body caused, in the past week or (a
    DNS filed early) still ahead: a plan built now opens with a recovery
    week."""
    from app.services.plan_service import RECOVERY_DAYS

    last = snap.last_goal
    if last is None or last.event_date < today - timedelta(days=RECOVERY_DAYS):
        return False
    key = {GoalStatus.dnf.value: "dnf_reason", GoalStatus.dns.value: "dns_reason"}.get(_status(last))
    reason = (last.assessment_data or {}).get(key) if key else None
    return key is not None and reason in _BODY_REASONS[key]


def _state(snap: _Snapshot) -> str:
    if snap.future or snap.race_day:
        return "chasing"
    if snap.last_goal is not None:
        return "between_goals"
    return "no_goal"


def _eligible(user: User, snap: _Snapshot) -> bool:
    """Riders the keeper may build for: past setup, and members. Never an
    account held as possibly under 18: nothing about them is generated."""
    from app.services.billing_service import has_access

    if not user.is_active or user.deleted_at is not None or snap.minor_held:
        return False
    if not snap.ever_planned:
        return False
    return has_access(user)


def _replaceable(snap: _Snapshot) -> bool:
    """No current plan, or one aimed at no goal ahead: the block, or a plan
    whose goal was filed as not started, moved into the past, deleted or
    already ridden while a later goal kept the plan running."""
    if snap.current is None or snap.current.goal_event_id is None:
        return True
    # A goal raced today still holds its plan until tomorrow, even once its
    # result is filed, so nothing is rebuilt on race day itself.
    return snap.current.goal_event_id not in snap.held


def _action(user: User, snap: _Snapshot, today: date) -> str | None:
    """What the keeper would build now: "block", "goal_plan" or nothing."""
    if not _eligible(user, snap) or not _replaceable(snap):
        return None
    if snap.future or snap.race_day:
        # A goal dated today is raced, not planned for.
        if any(g.event_date > today for g in snap.future):
            return "goal_plan"
        return None
    if snap.current is not None and snap.current.goal_event_id is None:
        return None  # already on the block
    return "block"


def season_state(db: Session, user: User, today: date | None = None) -> dict:
    """Where the rider stands between goals. Reads only."""
    today = today or date.today()
    snap = _read(db, user, today)
    out: dict = {
        "state": _state(snap),
        "last_goal": None,
        "current_plan": None,
        # Whether a goal filed now rebuilds the plan by itself: the same
        # rule ensure_season acts on, so the coach never promises otherwise.
        "rebuilds": (
            settings.season_keeper_enabled
            and _eligible(user, snap)
            and _replaceable(snap)
            and snap.held_back is None
        ),
        "held_back": snap.held_back,
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


def _say_date(d: date) -> str:
    """The date as the coach should say it ("Sunday 20 December 2026")."""
    return f"{d:%A} {d.day} {d:%B} {d.year}"


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
            f"set for {_say_date(last['event_date'])}, is recorded as {last['status']}."
        ]
    if not last["result_recorded"]:
        parts.append(
            "Its result is not recorded yet: ask how it went first, and send them "
            "to Goal, then File the race report."
        )
    plan = state.get("current_plan")
    if plan and plan["goal_less"]:
        parts.append(
            f"They are on '{plan['name']}', a {plan['weeks']}-week block that builds "
            "their base, so every week has sessions until a new goal is set."
        )
    elif plan:
        parts.append(f"They are on '{plan['name']}' until {_say_date(plan['end'])}.")
    else:
        parts.append("They have no current plan, so only the sessions in this_week are booked.")
    # After a DNF or DNS the debrief law in the education leads: no pitch for
    # a new target unless the rider asks, and never "how did it go".
    setback = last["status"] in (GoalStatus.dnf.value, GoalStatus.dns.value)
    held_back = state.get("held_back")
    if held_back:
        # The safety record says not now: no pitch, and nothing is built
        # until the wait is over (_held_back).
        parts += [_WAIT.get(held_back, _WAIT["unknown"]), "Do not propose a new goal."]
        if in_chat and held_back == "wellbeing":
            parts.append(
                "If they ask for one, file it with create_goal; they can build the "
                "plan around it from Goal, then Build my season, when they want to."
            )
        elif in_chat:
            parts.append(
                "If they ask for one, file it with create_goal; nothing is built "
                "around it while this lasts, so say nothing about what the plan will do."
            )
        elif not last["result_recorded"] and not setback and held_back != "wellbeing":
            parts.append("Spend the one question on how the race went.")
        else:
            parts.append("Spend the one question on how they are.")
        return " ".join(parts)
    if in_chat:
        then = (
            "the plan is rebuilt around it at once." if state.get("rebuilds")
            else "they build the plan from Goal, then Build my season."
        )
        if setback:
            parts.append(
                f"It ended as {last['status'].upper()}, so the debrief law for a DNF "
                "or DNS leads: do not propose a new goal unless they ask for one or "
                "say this one still stands. When they do, file it with create_goal "
                f"and {then}"
            )
        else:
            parts.append(
                "Propose the next goal yourself from what you know of them and their "
                "riding; do not ask whether they want one. When they agree, file it "
                f"with create_goal and {then}"
            )
    elif setback:
        parts.append(
            f"It ended as {last['status'].upper()}: do not propose a new goal and "
            "do not ask how the race went. Spend the one question on how they are."
        )
    elif not last["result_recorded"]:
        # The morning message has one question, and it goes on the race.
        parts.append(
            "Spend the one question on how the race went. The next goal comes "
            "after the report is filed; do not propose it here."
        )
    else:
        parts.append(
            "Propose the next goal yourself from what you know of them and their "
            "riding, and make the one question the one that settles it, such as "
            "the event or the date. They answer on the Coach page."
        )
    return " ".join(parts)


# What the coach is told when the safety record says to wait (_held_back),
# in words it can act on without repeating them to the rider.
_WAIT = {
    "injury": "An injury hold is open: no ride goes in the plan until it lifts.",
    "restriction": (
        "Eating very little or fast weight loss came up in the last 28 days: nothing "
        "adds training for now, and say no numbers about food or weight."
    ),
    "wellbeing": (
        "They wrote words of hopelessness or worse in the last 14 days: keep plan "
        "talk gentle, as safety.wellbeing says."
    ),
    "unknown": "Their safety record could not be read just now.",
}


def lock_rider(db: Session, user: User) -> None:
    """Serialise plan building for one rider: the keeper, goal writes and
    Build my season. Postgres holds the row until the next commit, so a
    second build waits and then sees the first one's plan; SQLite ignores
    FOR UPDATE, which is fine for tests."""
    db.query(User.id).filter(User.id == user.id).with_for_update().first()


def ensure_season(db: Session, user: User, today: date | None = None) -> str:
    """End expired plans and give the rider the plan their goals call for.

    Returns "block", "goal_plan", "held" (one was due, but the safety record
    says to wait: _held_back), "retired" (only plans ended) or "unchanged".
    """
    from app.services.plan_service import generate_plan, retire_plan

    today = today or date.today()
    lock_rider(db, user)
    snap = _read(db, user, today)
    # Expired plans end, and so does any older plan still running beside the
    # newest (two builds that raced each other before the lock existed).
    ended = snap.expired + snap.extra
    for plan in ended:
        retire_plan(db, plan, today)
    db.flush()
    action = _action(user, snap, today)
    if action is not None and snap.held_back:
        db.commit()
        logger.info("Season keeper: %s held back (%s) for user %s", action, snap.held_back, user.id)
        return "held"
    if action is None:
        db.commit()
        return "retired" if ended else "unchanged"
    # generate_plan ends any other active plan and commits, which also
    # releases the lock. The rider's periodisation carries over. After a
    # DNF or DNS the body caused it opens with a recovery week; otherwise
    # generate_plan decides (a recovery week after an A or B race).
    model = snap.last_model or "traditional"
    recover = True if _fresh_setback(snap, today) else None
    if action == "block":
        generate_plan(db, user, periodization_model=model, name=BLOCK_NAME, recover_first=recover)
    else:
        generate_plan(db, user, periodization_model=model, recover_first=recover)
    logger.info("Season keeper: %s for user %s", action, user.id)
    return action


def try_ensure_season(db: Session, user: User) -> str | None:
    """ensure_season for a request that has already done its own work (a goal
    filed, a report saved). A failure here is logged and rolled back, never
    allowed to undo or hide what the rider just did. With the keeper switched
    off (SEASON_KEEPER_ENABLED=false) it does nothing at all."""
    if not settings.season_keeper_enabled:
        return "unchanged"
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
                if action is not None and snap.held_back:
                    action = "held"
                outcome = action or ("retired" if snap.expired or snap.extra else "unchanged")
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
