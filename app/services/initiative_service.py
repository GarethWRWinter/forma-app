"""Initiative Service — the coach going first.

The founder's riders are not the founder. They will not arrive with a good
question, they will not notice that their easy rides stopped being easy, and
they will never volunteer that the knee they mentioned a month ago still
grumbles on the second hour. Waiting to be prompted by a rider who does not
know what to ask is not coaching, it is a search box with a personality.

So this service looks for the one thing worth raising, and raises it.

Three generators, one shape. An open loop is something the rider told the coach
that was never followed up. A ride insight is something measurable in a recent
ride that the rider would never think to ask about. A weekly check in is the
two or three things no device can see. Each produces a headline, a plain
English explanation, and exactly one question.

The safeguards matter more than the generators, because this is the point where
an app starts interrupting people.

- At most ONE initiative is pending at a time, across all three generators. A
  queue of the coach's thoughts is not attentiveness, it is nagging.
- Every generator is allowed to return nothing, and silence is the default. A
  weak initiative is worse than none: it teaches the rider that these can be
  swiped away without reading, and one day one of them will matter.
- A dismissed open loop stays quiet for a fortnight, and a ride the coach has
  already spoken about is never raised twice.
- The weekly check in comes at most once every seven days, and only when the
  coach has nothing better to say.

Every threshold here is deliberately set where a real coach would raise an
eyebrow, not where a spreadsheet would flag an outlier.
"""

import json
import logging
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core import forma_core
from app.core.coach_skills import distilled_persona
from app.core.llm_utils import humanize, response_text
from app.models.coach_initiative import CoachInitiative
from app.models.memory import MemoryEdge, MemoryEntity
from app.models.ride import Ride
from app.models.training import PlanStatus, TrainingPhase, TrainingPlan, Workout, WorkoutStatus
from app.models.user import User

logger = logging.getLogger(__name__)


# ── Where the bar sits ───────────────────────────────────────────────────────

# A loop is only open once it has had time to close on its own. Ten days is
# long enough that a twinge has either settled or become a story, and short
# enough that asking still feels like the coach was paying attention.
OPEN_LOOP_MIN_AGE_DAYS = 10

# Waved away means waved away. Coming back inside a fortnight is nagging.
OPEN_LOOP_DISMISS_COOLDOWN_DAYS = 14

# A memory the rider has touched recently is not an open loop, it is a live
# conversation, and the coach does not need a card to join one.
OPEN_LOOP_QUIET_DAYS = 10

WEEKLY_CHECKIN_INTERVAL_DAYS = 7

# A check in during week one has nothing to check in on. Let the rider ride.
MIN_ACCOUNT_AGE_DAYS_FOR_CHECKIN = 7

# Ride insights read the week just gone. Older than that and the rider has
# moved on, however interesting the number was.
RIDE_WINDOW_DAYS = 7

# Intensity factor is the ride's normalised power as a fraction of FTP: 0.65
# is a genuine easy spin, 0.75 is tempo wearing easy clothes. These are the
# defaults used when the plan did not state a target for the session.
DEFAULT_EASY_IF = {"recovery": 0.55, "endurance": 0.65}

# How far above the prescription counts as notable rather than normal. Roads
# have hills, and no rider hits a target to two decimal places.
EASY_IF_OVERSHOOT = 0.06

# The absolute floor underneath the overshoot rule: below this the ride was
# still genuinely easy, whatever the plan said, and there is nothing to raise.
EASY_IF_FLOOR = 0.68

# Fade: last third normalised power against the first third, as a percentage.
# Matches ride_analysis_service's own "faded" verdict, so the coach's card and
# the ride file never disagree with each other.
FADE_PCT_BAR = -8.0

# Under this, a fade is just the shape of the session rather than the rider
# running out of road.
MIN_FADE_DURATION_SECONDS = 2700

# Variability index is how punchy a ride was: 1.0 is metronomic, above about
# 1.10 the power was all over the place. Intervals and stop-start town riding
# produce a "fade" that is really just structure, so they are excluded.
MAX_FADE_VARIABILITY_INDEX = 1.10

# Reading per second data is the expensive path, so only a couple of rides
# without a cached analysis are ever opened in one pass.
MAX_FADE_SCANS = 2

# A hard session ridden this far under its intensity target was a different
# session from the one prescribed, and worth asking about.
HARD_IF_SHORTFALL = 0.08

HARD_WORKOUT_TYPES = ("threshold", "vo2max", "sweet_spot", "sprint")
EASY_WORKOUT_TYPES = ("endurance", "recovery")

# A session abandoned early is a story about the day, not about intensity.
# Judging it as "you went easy" would be the coach reading the wrong thing.
MIN_HARD_SESSION_COMPLETION = 0.60

# Statuses that mean the memory has already been dealt with. Anything else,
# including the None that most gaps and health signals carry, is still open.
RESOLVED_MEMORY_STATUSES = ("resolved", "closed", "applied", "became_habit", "rejected")

# The prompt asks for about 60 words. This is the point at which the coach has
# clearly written an essay instead, which is worth knowing about in the logs.
MAX_REASONABLE_WORDS = 90

# When two findings both clear their bar by the same margin, this decides.
# The easy ride case leads because it is the one the rider is least likely to
# have spotted and most likely to be quietly paying for.
TRIGGER_PRIORITY = {
    "easy_ride_was_not_easy": 2,
    "late_ride_fade": 1,
    "hard_session_under_target": 0,
}


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


def _as_datetime(value) -> datetime | None:
    """Timestamps arrive as datetime, date or ISO string depending on backend."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except ValueError:
        return None


def _days_since(value) -> int | None:
    moment = _as_datetime(value)
    if moment is None:
        return None
    return max(0, (datetime.utcnow() - moment).days)


# ── The gate ─────────────────────────────────────────────────────────────────


def pending_initiative(db: Session, user_id: str) -> CoachInitiative | None:
    """The one thought the coach is currently holding, if there is one."""
    return (
        db.query(CoachInitiative)
        .filter(
            CoachInitiative.user_id == user_id,
            CoachInitiative.status == "pending",
        )
        .order_by(CoachInitiative.created_at.desc())
        .first()
    )


def has_pending(db: Session, user_id: str) -> bool:
    """Is the rider already holding one of the coach's thoughts.

    The single most important line in this file. One initiative at a time,
    across every generator: the rider answers the coach or waves it away
    before the coach is allowed to raise anything else.
    """
    return pending_initiative(db, user_id) is not None


# ── Generator 1: the open loop ───────────────────────────────────────────────


def _dismissed_subject_ids(
    db: Session, user_id: str, subject_type: str, since: datetime
) -> set[str]:
    """Subjects this rider has waved away recently. Coming back inside the
    cooldown would tell them their dismissal did not count."""
    rows = (
        db.query(CoachInitiative.subject_id)
        .filter(
            CoachInitiative.user_id == user_id,
            CoachInitiative.subject_type == subject_type,
            CoachInitiative.status == "dismissed",
            CoachInitiative.decided_at >= since,
        )
        .all()
    )
    return {r[0] for r in rows if r[0]}


def _raised_subject_ids(db: Session, user_id: str, subject_type: str) -> set[str]:
    """Everything the coach has ever raised about this kind of subject.

    A ride only has one story worth telling unprompted, so once it has been
    raised it is finished with, whatever the rider did about it.
    """
    rows = (
        db.query(CoachInitiative.subject_id)
        .filter(
            CoachInitiative.user_id == user_id,
            CoachInitiative.subject_type == subject_type,
        )
        .all()
    )
    return {r[0] for r in rows if r[0]}


def _memory_neighbours(db: Session, user_id: str, entity_id: str) -> list[str]:
    """Labels of memories wired to this one, so the coach asks about the knee
    knowing it sits next to the hill climb they are training for."""
    edges = (
        db.query(MemoryEdge)
        .filter(
            MemoryEdge.user_id == user_id,
            (MemoryEdge.from_id == entity_id) | (MemoryEdge.to_id == entity_id),
        )
        .limit(8)
        .all()
    )
    neighbour_ids = {
        (e.to_id if e.from_id == entity_id else e.from_id) for e in edges
    }
    if not neighbour_ids:
        return []
    rows = (
        db.query(MemoryEntity)
        .filter(
            MemoryEntity.user_id == user_id,
            MemoryEntity.id.in_(neighbour_ids),
            MemoryEntity.hidden_at.is_(None),
        )
        .limit(4)
        .all()
    )
    return [r.label for r in rows if r.label]


def find_open_loop(db: Session, user: User) -> dict | None:
    """The thing they told the coach that nobody ever came back to.

    Health signals and gaps are the two types that hurt when they are dropped:
    an injury nobody asked about again, a weakness named once and never
    revisited. The oldest unresolved one wins, because the longer a loop stays
    open the more it matters that somebody remembered it.
    """
    now = datetime.utcnow()
    raised_before = now - timedelta(days=OPEN_LOOP_MIN_AGE_DAYS)
    cooldown_since = now - timedelta(days=OPEN_LOOP_DISMISS_COOLDOWN_DAYS)

    dismissed = _dismissed_subject_ids(db, user.id, "memory", cooldown_since)

    candidates = (
        db.query(MemoryEntity)
        .filter(
            MemoryEntity.user_id == user.id,
            MemoryEntity.type.in_(("health_signal", "gap")),
            # Hidden memories inform the coach's judgement and are never
            # mentioned back to the rider. A card is a mention.
            MemoryEntity.hidden_at.is_(None),
            MemoryEntity.observed_at <= raised_before,
        )
        .order_by(MemoryEntity.observed_at.asc())
        .limit(40)
        .all()
    )

    for memory in candidates:
        if memory.id in dismissed:
            continue
        if (memory.status or "").lower() in RESOLVED_MEMORY_STATUSES:
            continue
        # Touched in the last few days means the loop is already moving, and
        # the coach does not need a card to join a live conversation.
        touched_days = _days_since(memory.updated_at)
        if touched_days is not None and touched_days < OPEN_LOOP_QUIET_DAYS:
            continue

        raised_days = _days_since(memory.observed_at) or OPEN_LOOP_MIN_AGE_DAYS
        since = now - timedelta(days=raised_days)

        # What the rider has actually done since they said it. "You mentioned
        # the knee three weeks ago, and you have ridden nine times since" is a
        # far better question than one asked into a vacuum.
        rides_since = (
            db.query(Ride)
            .filter(Ride.user_id == user.id, Ride.ride_date >= since)
            .count()
        )

        return {
            "kind": "open_loop",
            "subject_type": "memory",
            "subject_id": memory.id,
            "memory": {
                "type": memory.type,
                "kind": memory.kind,
                "life_area": memory.life_area,
                "label": memory.label,
                "summary": memory.summary,
                "status": memory.status,
                "raised_on": str(_as_datetime(memory.observed_at) or "")[:10],
                "days_since_raised": raised_days,
                "never_updated_since": memory.updated_at is None,
            },
            "since_then": {
                "rides_logged": rides_since,
                "days": raised_days,
            },
            "related_memories": _memory_neighbours(db, user.id, memory.id),
        }

    return None


# ── Generator 2: the ride insight ────────────────────────────────────────────


def _prescription_for(db: Session, ride: Ride) -> Workout | None:
    """The session this ride was meant to be, whichever side holds the link.

    Both lookups are scoped to the ride's own rider even though the ride is
    already theirs. A stale or crossed workout_id must fail to a None rather
    than hand the coach another rider's prescription to talk about.
    """
    if ride.workout_id:
        workout = (
            db.query(Workout)
            .filter(Workout.id == ride.workout_id, Workout.user_id == ride.user_id)
            .first()
        )
        if workout is not None:
            return workout
    return (
        db.query(Workout)
        .filter(Workout.actual_ride_id == ride.id, Workout.user_id == ride.user_id)
        .first()
    )


def _fade_for(db: Session, user: User, ride: Ride, scans_left: int) -> tuple[dict | None, int]:
    """The ride's first third against its last third, cached result preferred.

    Reuses ride_analysis_service's own fade maths rather than a second opinion,
    so the card and the ride file can never tell the rider different stories.
    """
    cached = (ride.analysis or {}).get("fade")
    if cached:
        return cached, scans_left
    if scans_left <= 0:
        return None, scans_left

    from app.services.ride_analysis_service import _fade, _samples

    rows = _samples(db, ride.id)
    power = [int(r.power) for r in rows if r.power is not None]
    if not power:
        return None, scans_left - 1
    return _fade(power, ride.ftp_at_time or user.ftp), scans_left - 1


def _easy_ride_candidate(ride: Ride, workout: Workout | None) -> dict | None:
    """The canonical one: an easy ride that was not easy.

    Requires a prescription. Without one the coach does not know what the ride
    was meant to be, and "you rode quite hard" is an observation about nothing.
    """
    if workout is None or ride.intensity_factor is None:
        return None
    wtype = _enum_value(workout.workout_type)
    if wtype not in EASY_WORKOUT_TYPES:
        return None

    target = workout.planned_if or DEFAULT_EASY_IF.get(wtype)
    if not target:
        return None

    actual = float(ride.intensity_factor)
    if actual < EASY_IF_FLOOR or actual < target + EASY_IF_OVERSHOOT:
        return None

    return {
        "trigger": "easy_ride_was_not_easy",
        # Every candidate scores how far it clears its OWN bar, so three
        # different findings can be compared without pretending an intensity
        # factor and a power drop are the same unit.
        "strength": round((actual - target) - EASY_IF_OVERSHOOT, 3),
        "numbers": {
            "prescribed_session": workout.title,
            "prescribed_type": wtype,
            "target_intensity_factor": round(target, 2),
            "actual_intensity_factor": round(actual, 2),
            "normalised_power_watts": round(ride.normalized_power)
            if ride.normalized_power else None,
            "ftp_watts": ride.ftp_at_time or None,
            "duration_minutes": (ride.moving_time_seconds or ride.duration_seconds or 0) // 60,
            "tss": round(ride.tss) if ride.tss else None,
        },
    }


def _hard_session_candidate(ride: Ride, workout: Workout | None) -> dict | None:
    """A hard session ridden well under its target. Not a telling off: the
    interesting question is what got in the way."""
    if workout is None or ride.intensity_factor is None or not workout.planned_if:
        return None
    wtype = _enum_value(workout.workout_type)
    if wtype not in HARD_WORKOUT_TYPES:
        return None

    target = float(workout.planned_if)
    actual = float(ride.intensity_factor)
    if actual > target - HARD_IF_SHORTFALL:
        return None

    # A session cut short is a story about the day, not about intensity. The
    # coach would be answering the wrong question if it read this as easing off.
    planned_seconds = workout.planned_duration_seconds or 0
    actual_seconds = ride.moving_time_seconds or ride.duration_seconds or 0
    if planned_seconds and actual_seconds < planned_seconds * MIN_HARD_SESSION_COMPLETION:
        return None

    return {
        "trigger": "hard_session_under_target",
        "strength": round((target - actual) - HARD_IF_SHORTFALL, 3),
        "numbers": {
            "prescribed_session": workout.title,
            "prescribed_type": wtype,
            "target_intensity_factor": round(target, 2),
            "actual_intensity_factor": round(actual, 2),
            "planned_minutes": planned_seconds // 60 if planned_seconds else None,
            "actual_minutes": actual_seconds // 60 if actual_seconds else None,
            "planned_tss": round(workout.planned_tss) if workout.planned_tss else None,
            "actual_tss": round(ride.tss) if ride.tss else None,
        },
    }


def _fade_candidate(ride: Ride, workout: Workout | None, fade: dict | None) -> dict | None:
    """The rider ran out of road in the last third. Worth asking about, because
    the cause is almost always something no file records: food, sleep, wind."""
    if not fade or fade.get("change_pct") is None:
        return None
    if float(fade["change_pct"]) > FADE_PCT_BAR:
        return None

    return {
        "trigger": "late_ride_fade",
        "strength": round(
            (abs(float(fade["change_pct"])) - abs(FADE_PCT_BAR)) / 100, 3
        ),
        "numbers": {
            "prescribed_session": workout.title if workout is not None else None,
            "first_third_normalised_power_watts": fade.get("first_third_np"),
            "last_third_normalised_power_watts": fade.get("last_third_np"),
            "drop_percent": fade.get("change_pct"),
            "duration_minutes": (ride.moving_time_seconds or ride.duration_seconds or 0) // 60,
            "distance_km": round((ride.distance_meters or 0) / 1000, 1) or None,
            "average_hr": ride.average_hr,
        },
    }


def _fade_is_readable(ride: Ride, workout: Workout | None) -> bool:
    """Only rides where a fade means what it appears to mean.

    Intervals have a low first third by design, and a punchy town ride swings
    too much to read. Testing this before opening the file also keeps the
    expensive per second scan off the rides it would only mislead the coach on.
    """
    seconds = ride.moving_time_seconds or ride.duration_seconds or 0
    if seconds < MIN_FADE_DURATION_SECONDS:
        return False
    if ride.variability_index and float(ride.variability_index) > MAX_FADE_VARIABILITY_INDEX:
        return False
    if workout is not None and _enum_value(workout.workout_type) in HARD_WORKOUT_TYPES:
        return False
    return True


def _rank(candidate: dict) -> tuple[float, int]:
    """How far past its bar, then which kind of finding, so one ride week
    always resolves to a single thing worth saying."""
    return (
        float(candidate["strength"]),
        TRIGGER_PRIORITY.get(candidate["trigger"], 0),
    )


def find_ride_insight(db: Session, user: User) -> dict | None:
    """Something genuinely notable in the last week of riding, or nothing.

    Nothing is the common case, and it should be. Every candidate here is a
    measured number against a stated target, never a hunch: the coach earns the
    right to interrupt by being able to show its working.
    """
    since = datetime.utcnow() - timedelta(days=RIDE_WINDOW_DAYS)
    already_raised = _raised_subject_ids(db, user.id, "ride")

    rides = (
        db.query(Ride)
        .filter(Ride.user_id == user.id, Ride.ride_date >= since)
        .order_by(Ride.ride_date.desc())
        .limit(20)
        .all()
    )

    best: dict | None = None
    easy_breaches = 0
    scans_left = MAX_FADE_SCANS

    for ride in rides:
        if ride.id in already_raised:
            continue
        workout = _prescription_for(db, ride)

        candidates: list[dict] = []

        easy = _easy_ride_candidate(ride, workout)
        if easy:
            easy_breaches += 1
            candidates.append(easy)

        hard = _hard_session_candidate(ride, workout)
        if hard:
            candidates.append(hard)

        if _fade_is_readable(ride, workout):
            fade, scans_left = _fade_for(db, user, ride, scans_left)
            faded = _fade_candidate(ride, workout, fade)
            if faded:
                candidates.append(faded)

        for candidate in candidates:
            candidate["subject_type"] = "ride"
            candidate["subject_id"] = ride.id
            candidate["ride"] = {
                "title": ride.forma_title or ride.title,
                "date": str(_as_datetime(ride.ride_date) or "")[:10],
                "days_ago": _days_since(ride.ride_date),
            }
            if best is None or _rank(candidate) > _rank(best):
                best = candidate

    if best is None:
        return None

    best["kind"] = "ride_insight"
    if best["trigger"] == "easy_ride_was_not_easy" and easy_breaches > 1:
        # One warm easy ride is a day. Several is a habit, and the habit is
        # the thing actually costing them the adaptation they trained for.
        best["numbers"]["easy_rides_over_target_this_week"] = easy_breaches
    return best


# ── Generator 3: the weekly check in ─────────────────────────────────────────


def weekly_checkin_due(db: Session, user: User) -> bool:
    """Once a week at most, and only when the coach has nothing better to say."""
    if has_pending(db, user.id):
        return False

    account_age = _days_since(user.created_at)
    if account_age is not None and account_age < MIN_ACCOUNT_AGE_DAYS_FOR_CHECKIN:
        return False

    since = datetime.utcnow() - timedelta(days=WEEKLY_CHECKIN_INTERVAL_DAYS)
    recent = (
        db.query(CoachInitiative)
        .filter(
            CoachInitiative.user_id == user.id,
            CoachInitiative.kind == "weekly_checkin",
            CoachInitiative.created_at >= since,
        )
        .first()
    )
    # Counted whatever the rider did with it: dismissing a check in is itself
    # an answer, and asking again on Tuesday would ignore it.
    return recent is None


def _weekly_checkin_context(db: Session, user: User) -> dict:
    """The little the coach knows, so it can ask about what it does not.

    Deliberately thin. The whole value of a check in is the things no device
    records, and a card stuffed with numbers invites a numbers answer.
    """
    today = date.today()
    week_start = today - timedelta(days=7)

    rides = (
        db.query(Ride)
        .filter(
            Ride.user_id == user.id,
            Ride.ride_date >= datetime.combine(week_start, datetime.min.time()),
        )
        .all()
    )
    hours = sum(
        (r.moving_time_seconds or r.duration_seconds or 0) for r in rides
    ) / 3600

    planned = (
        db.query(Workout)
        .join(TrainingPhase, Workout.phase_id == TrainingPhase.id)
        .join(TrainingPlan, TrainingPhase.plan_id == TrainingPlan.id)
        .filter(
            Workout.user_id == user.id,
            Workout.scheduled_date >= week_start,
            Workout.scheduled_date <= today,
            TrainingPlan.status == PlanStatus.active,
        )
        .all()
    )
    completed = sum(
        1 for w in planned if _enum_value(w.status) == WorkoutStatus.completed.value
    )

    context: dict = {
        "kind": "weekly_checkin",
        "week": {
            "rides": len(rides),
            "hours": round(hours, 1),
            "sessions_completed": f"{completed}/{len(planned)}" if planned else None,
            "total_tss": round(sum(r.tss or 0 for r in rides)) or None,
        },
    }

    try:
        from app.services.metrics_service import get_current_fitness

        fitness = get_current_fitness(db, user.id)
        context["fitness"] = {
            "ctl": round(fitness["ctl"], 1),
            "atl": round(fitness["atl"], 1),
            "tsb": round(fitness["tsb"], 1),
        }
    except Exception:
        logger.exception("Fitness read for weekly check in failed (user=%s)", user.id)

    return context


# ── The instructions ─────────────────────────────────────────────────────────

INITIATIVE_INSTRUCTIONS = """

## This surface: the coach speaking first

Nobody asked you anything. You noticed something and you are raising it,
because this rider will not think to ask. That is the whole job: they are not
sitting there with a good question waiting for a box to type it into, and if
you wait for one you will be silent for months while something quietly costs
them the season.

The finding is already made and handed to you below, with its real numbers. You
are not deciding whether it is true. You are deciding how to say it to a human.

Write three things and nothing else.

1. "headline": one short line, at most about 10 words. What you noticed, said
   like a person. Not a label, not a metric name, not a category.
2. "body": one or two sentences saying what it MEANS for them, in plain words.
   This is the whole value. Other apps hand riders the number and walk away.
   You are not smart because you used the vocabulary, you are smart because the
   rider finishes your sentence understanding something they did not understand
   before. If you use a term like intensity factor, normalised power, TSB or
   durability, translate it in the same breath, briefly, without a hint of
   condescension. "Your intensity factor was 0.78, which is tempo, not the easy
   spin the session asked for" teaches. "Your IF was 0.78" does not.
3. "question": exactly ONE question, and it closes the whole thing. Make it one
   they would not have asked themselves, and one only they can answer, because
   their answer teaches you something no device ever will. Never two questions.
   Never a question with an "and" hiding a second one inside it.

Rules.
- One thing. Not two observations, not a list, not a caveat you could not
  resist adding.
- About 60 words in total across all three fields. Shorter is better.
- Use their first name at most once, and only where a person would.
- Real numbers from the finding, never invented, never rounded into vagueness.
- The tone is a coach who noticed, not a system that alerted. No urgency you do
  not genuinely feel, no manufactured concern, no exclamation marks.
- British English. Never use em dashes or en dashes, use a comma or a full stop.
- Never mention memories marked [HIDDEN]. They inform your judgement only.

Return STRICT JSON and nothing else. No markdown fence, no preamble, no
commentary after the closing brace:
{"headline": "...", "body": "...", "question": "..."}
"""

# What each generator is actually asking the coach to write about. The finding
# is the same shape every time; the register is not.
KIND_BRIEFS: dict[str, str] = {
    "open_loop": (
        "This is an OPEN LOOP. The rider told you this once and nobody ever "
        "came back to it. The point is not the fact itself, it is that "
        "somebody remembered. Say when they told you and how long it has "
        "been, ask whether it settled or whether they have quietly been "
        "riding around it, and leave room for the answer to be either. If it "
        "is a health signal, be careful rather than dramatic, and never "
        "diagnose."
    ),
    "ride_insight": (
        "This is a RIDE INSIGHT. It was measured, not assumed, so show the "
        "numbers and then translate them. The rider would never have thought "
        "to ask about this, which is exactly why it is worth raising. Do not "
        "scold: riding an easy day too hard is the most human thing in the "
        "sport, and the question is what was going on, not why they "
        "disobeyed."
    ),
    "weekly_checkin": (
        "This is the WEEKLY CHECK IN. Ask about the things no device can see: "
        "how work is, how sleep really is, whether the riding still feels "
        "good. Use the week's numbers only as the way in, lightly, and spend "
        "the question on the part of their life the data cannot reach. If the "
        "week looks ordinary, say so plainly and ask the human question "
        "anyway. That is the point of it."
    ),
}


# ── The orchestrator ─────────────────────────────────────────────────────────


def _clean_question(text: str) -> str:
    """One question, and it ends like one."""
    q = text.strip()
    if q and not q.endswith("?"):
        q = q.rstrip(".!") + "?"
    return q


def generate(
    db: Session, user: User, force_kind: str | None = None
) -> CoachInitiative | None:
    """Find the one thing worth raising and write it, or stay quiet.

    Returns None far more often than it returns an initiative, and that is the
    design. Callers treat None as a complete answer: there is nothing to put in
    front of this rider today.
    """
    if has_pending(db, user.id):
        return None

    finding: dict | None = None

    if force_kind in (None, "open_loop"):
        finding = find_open_loop(db, user)
    if finding is None and force_kind in (None, "ride_insight"):
        finding = find_ride_insight(db, user)
    if finding is None and force_kind in (None, "weekly_checkin"):
        if weekly_checkin_due(db, user):
            finding = _weekly_checkin_context(db, user)

    if finding is None:
        return None

    kind = finding.get("kind") or force_kind
    if kind not in KIND_BRIEFS:
        logger.warning("Initiative: unknown kind %s (user=%s)", kind, user.id)
        return None

    payload = dict(finding)
    payload["rider_name"] = _first_name(user)
    payload["today"] = date.today().isoformat()

    # The coach must know this rider, not just this finding. An open loop about
    # a knee reads completely differently next to a goal three weeks out.
    try:
        from app.services.memory_service import get_context as get_memory_context

        memory_block = get_memory_context(db, user, limit=10)
        if memory_block:
            payload["long_term_memory"] = memory_block.split("\n")
    except Exception:
        logger.exception("Memory context for initiative failed (user=%s)", user.id)

    try:
        response = forma_core.call(
            user_id=user.id,
            task="initiative",
            surface="dashboard",
            system=distilled_persona(user.coach_name, user.coach_tone)
            + INITIATIVE_INSTRUCTIONS
            + "\n\n## This initiative\n"
            + KIND_BRIEFS[kind],
            messages=[{
                "role": "user",
                "content": (
                    "Raise this with the rider.\n\n```json\n"
                    f"{json.dumps(payload, indent=2, default=str)}\n```"
                ),
            }],
        )
        raw = response_text(response).strip()
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:
        # A failed initiative is a quiet non-event. Nothing is shown, nothing
        # is logged against the subject, and the next pass will try again.
        logger.exception("Initiative generation failed (user=%s kind=%s)", user.id, kind)
        return None

    headline = humanize(str(data.get("headline") or "").strip())
    body = humanize(str(data.get("body") or "").strip())
    question = _clean_question(humanize(str(data.get("question") or "").strip()))

    if not headline or not question:
        # A card with no question is a notification, and notifications are what
        # everyone else ships. Better to say nothing at all.
        logger.info(
            "Initiative: incomplete generation discarded (user=%s kind=%s)", user.id, kind
        )
        return None

    words = len(f"{headline} {body} {question}".split())
    if words > MAX_REASONABLE_WORDS:
        logger.info(
            "Initiative: %d words, over the brief (user=%s kind=%s)", words, user.id, kind
        )

    initiative = CoachInitiative(
        user_id=user.id,
        kind=kind,
        subject_type=finding.get("subject_type"),
        subject_id=finding.get("subject_id"),
        headline=headline,
        body=body or None,
        question=question,
        status="pending",
    )
    db.add(initiative)
    db.commit()
    db.refresh(initiative)
    logger.info(
        "Initiative %s raised for user=%s (kind=%s, subject=%s)",
        initiative.id, user.id, kind, finding.get("subject_id"),
    )
    return initiative


# ── The rider's one tap ──────────────────────────────────────────────────────


def dismiss(
    db: Session, user: User, initiative: CoachInitiative
) -> CoachInitiative:
    """Waved away. One tap, no reason asked for, and none ever will be.

    The subject is remembered on the row, so an open loop the rider closed
    stays closed for a fortnight rather than reappearing tomorrow.
    """
    if initiative.user_id != user.id:
        raise ValueError("Initiative does not belong to this rider")
    if initiative.status != "pending":
        raise ValueError(f"Initiative is already {initiative.status}")

    initiative.status = "dismissed"
    initiative.decided_at = datetime.utcnow()
    db.commit()
    db.refresh(initiative)
    logger.info("Initiative %s dismissed by user=%s", initiative.id, user.id)
    return initiative


def mark_opened(
    db: Session, user: User, initiative: CoachInitiative
) -> CoachInitiative:
    """The rider took it into a conversation, which is the whole point of it.

    It stops being pending either way: the coach has had its say, and the
    conversation is now a better place to continue than a card.
    """
    if initiative.user_id != user.id:
        raise ValueError("Initiative does not belong to this rider")
    if initiative.status != "pending":
        raise ValueError(f"Initiative is already {initiative.status}")

    initiative.status = "opened"
    initiative.decided_at = datetime.utcnow()
    db.commit()
    db.refresh(initiative)
    logger.info("Initiative %s opened by user=%s", initiative.id, user.id)
    return initiative
