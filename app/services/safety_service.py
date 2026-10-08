"""Safety: the one gate on how hard a rider may be asked to ride.

Every surface that prescribes effort (plan generation, the coach's plan
tools, plan review, briefings and goal reads, ride mode) asks
allowed_intensity() and obeys it. Three things lower it, and the most
severe wins:

- an open safety hold (a red flag in chat, a coach tool call, the screening);
- a health-screen yes without a declared clearance;
- the layoff gate: the first 14 days back after four weeks or more off the
  bike (28 days after three months or more).

Each hold says how it lifts (lift_kind): "doctor" (a declared clearance),
"fever_self" (the rider declares the fever gone, which turns it into an easy
week that ends by itself), "head_injury" (its own declaration: a doctor has
checked them since and they've had no symptoms for 24 hours, which turns it
into easy riding until two weeks after the injury, with no racing or group
riding before day 21), "layoff" (the easy start after a break, which a
clearance doesn't skip), "expires" (ends on its own at expires_at) or
"admin_only" (an under-18 account, or a hold set by hand: nothing the rider
does lifts it, and only admin_lift does). While an account is held as under
18, nothing the rider does lifts any of its holds (RIDER_LIFTS).

The easy days after a fever or head-injury lift end only by themselves. The
same fever or head injury mentioned again in chat during them opens a full
hold beside them, never in their place (remention_of): marking that a
mistake lifts only it, and it never restarts the 21-day racing clock. A new
one the detector reads as new (Hit.new_event: "crashed again today and hit
my head") is a second injury, not a re-mention: it opens its own hold, and
the two easy weeks and the 21 days before racing start again from that day.

Plain functions over the database; nothing here calls the model. Writers
commit by default; pass commit=False to fold them into a larger transaction.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models.ride import Ride
from app.models.safety import (
    ClearanceLimit,
    ConsentEvent,
    HealthScreening,
    SafetyEvent,
    SafetyHold,
)
from app.models.user import User

# Session types an easy_only hold (or the layoff gate) still allows.
EASY_TYPES = {"recovery", "endurance"}
# Under "easy", no step may ask for more than this fraction of FTP.
EASY_CAP = 0.75
# Highest step allowed for each session type, as a fraction of FTP. A
# workout above its type's ceiling is mislabelled, and ride mode refuses it.
INTENSITY_CEILING = {
    "recovery": 0.60,
    "endurance": 0.80,
    "tempo": 0.92,
    "sweet_spot": 0.97,
    "threshold": 1.10,
    "vo2max": 1.30,
    "sprint": 2.50,
}
# ERG never holds the trainer above this fraction of FTP. Sprints above it
# run with ERG released, against the rider's own resistance.
ERG_CAP = 1.30

SCREENING_VERSION = "screen-v1"
RIDE_MODE_VERSION = "ride-mode-v1"
CLEARANCE_TEXT = (
    "A doctor (or my midwife or physio) has assessed me and cleared me for hard training."
)
# A fever lifts on the rider's own word, never a doctor's: most riders with
# a fever don't see one, and the record must not say they did. The lift
# leaves an easy week behind it (SAFETY LAW 2d).
FEVER_LIFT_VERSION = "fever-v1"
FEVER_LIFT_TEXT = (
    "My fever has been gone for 24 hours without paracetamol or ibuprofen, "
    "and my chest has cleared."
)
FEVER_EASY_DAYS = 7
FEVER_EASY_REASON = "Easy riding for a week after a fever"

# A head injury lifts on its own declaration, never the generic "cleared me
# for hard training": a doctor has checked them since, and they've been free
# of symptoms for 24 hours. The return stays graded (UK grassroots concussion
# guidance, SAFETY LAW 2c): easy riding until two weeks after the injury, and
# no racing or group riding before day 21.
HEAD_LIFT_VERSION = "head-v1"
HEAD_LIFT_TEXT = (
    "A doctor has checked me since I hit my head, and I've had no symptoms for "
    "at least 24 hours."
)
HEAD_EASY_DAYS = 14
HEAD_NO_RACING_DAYS = 21
HEAD_EASY_REASON = "Easy riding while you build back after a head injury"

# Where a rider who thinks a hold is wrong writes to.
FORMA_EMAIL = "gareth@ridewithforma.com"

HOLD_LEVELS = {"easy_only": 1, "hold_all": 2}
# "profile": an under-18 date of birth given on the account itself (at
# re-acceptance or in Settings), which only Forma lifts, like any under-18 hold.
HOLD_SOURCES = {"screening", "detector", "coach_tool", "layoff", "admin", "profile"}
LIFT_HOW = {
    "clearance", "mistake", "admin", "superseded", "fever_self", "head_clearance", "expired",
}
LIFT_KINDS = ("doctor", "fever_self", "head_injury", "layoff", "expires", "admin_only")
# Which lift_kinds each way of lifting may touch. An under-18 hold, or one
# set by hand, lifts only by admin. A doctor's clearance never skips the
# easy week after a fever, the easy start after a break, or a head injury's
# own declaration.
_LIFTABLE = {
    "admin": frozenset(LIFT_KINDS),
    "superseded": frozenset({"doctor", "fever_self", "head_injury", "layoff", "expires"}),
    "expired": frozenset({"expires"}),
    "fever_self": frozenset({"fever_self"}),
    "head_clearance": frozenset({"head_injury"}),
    "clearance": frozenset({"doctor"}),
    "mistake": frozenset({"doctor", "fever_self", "head_injury", "layoff"}),
}
# The ways out the rider takes themselves. While the account is held as under
# 18, every one of them is refused, whatever hold it is aimed at.
RIDER_LIFTS = frozenset({"mistake", "clearance", "fever_self", "head_clearance"})
TIERS ={"none", "easy_only", "hold_all"}
CONSENT_KINDS = {
    "terms", "health_data", "age", "ride_mode", "screening", "clearance", "reaccept",
}

# Layoff rule (SAFETY LAW 2k, guard rail 6).
LAYOFF_GAP_DAYS = 28
LONG_LAYOFF_GAP_DAYS = 90
LAYOFF_GATE_DAYS = 14
LONG_LAYOFF_GATE_DAYS = 28

_ALLOWED_RANK = {"none": 0, "easy": 1, "all": 2}
_EPS = 1e-6


# Who may clear what (red team #8). A physio clears an injury and a midwife
# a pregnancy, and nothing else: a heart symptom, a head injury, a medicine or
# a condition needs a doctor. Anyone else the rider names is taken as a
# doctor. Each entry: the word in "who cleared you", the red flags it covers,
# the screening questions it covers, and how to say what it covers.
_CLINICIAN_SCOPES = (
    ("physio", frozenset({"injury"}), frozenset({"q6"}), "an injury"),
    ("midwife", frozenset({"pregnancy"}), frozenset({"q7"}), "a pregnancy"),
)


def clinician_scope(by: str) -> tuple[str, frozenset, frozenset, str] | None:
    """What the clinician the rider named may clear, or None for a doctor
    (who may clear anything a clearance can lift)."""
    low = (by or "").lower()
    for scope in _CLINICIAN_SCOPES:
        if scope[0] in low:
            return scope
    return None


class ClearanceOutOfScope(ValueError):
    """The clinician named can't clear anything that is holding the rider:
    a physio for a chest-pain hold, say. Nothing is recorded."""

    def __init__(self, word: str, covers: str, needs: str = "doctor"):
        self.word = word
        who, pick = _NEEDS.get(needs, _NEEDS["doctor"])
        super().__init__(
            f"A {word} can clear {covers}, but what's holding you needs {who}. "
            f"Once one has cleared you, pick {pick}."
        )


# Who can clear what is holding the rider, and the buttons that say so.
_NEEDS = {
    "doctor": ("a doctor", "My GP or Another doctor"),
    "physio": ("a physio or a doctor", "My physio, My GP or Another doctor"),
    "midwife": ("a midwife or a doctor", "My midwife, My GP or Another doctor"),
}


def _needs(doctor_holds: list, screen_yes: set[str]) -> str:
    """The narrowest clinician who could clear everything holding the rider."""
    for word, flags, questions, _ in _CLINICIAN_SCOPES:
        if all(h.red_flag in flags for h in doctor_holds if h.source != "screening") and (
            screen_yes <= questions
        ):
            return word
    return "doctor"


class NothingToClear(ValueError):
    """A clearance with nothing it can lift: no hold a doctor's clearance
    covers and no health answer waiting for one. Nothing is recorded, so the
    consent log never says a rider was cleared for nothing."""


class HoldNotLiftable(ValueError):
    """A lift the hold doesn't allow: an under-18 hold by anyone but admin, a
    clearance on a fever, a mistake on the easy week after one."""

    def __init__(self, hold: SafetyHold, how: str):
        self.hold = hold
        self.how = how
        self.lift_kind = lift_kind(hold)
        super().__init__(
            f"Hold {hold.id} ({self.lift_kind}) can't be lifted by {how!r}"
        )


def _uid(user: User | str) -> str:
    return user if isinstance(user, str) else user.id


def _now() -> datetime:
    return datetime.utcnow()


def _today() -> date:
    return _now().date()


def _get(obj, key: str):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _lower(a: str, b: str) -> str:
    return a if _ALLOWED_RANK[a] <= _ALLOWED_RANK[b] else b


def _done(db: Session, commit: bool) -> None:
    if commit:
        db.commit()
    else:
        db.flush()


def _day_month(value: date | datetime) -> str:
    """"15 October": how a date reads in anything the rider sees."""
    return f"{value.day} {value:%B}"


# === Holds ===


def lift_kind(hold) -> str:
    """How this hold lifts. Works on a SafetyHold or a dict.

    "admin_only": an under-18 account or a hold set by hand. "layoff": the
    easy start after a break, which ends by itself at expires_at (two weeks,
    four after three months or more off). "expires": anything else that ends
    by itself at expires_at (the easy weeks after a fever or a head injury).
    "fever_self": the rider declares the fever gone. "head_injury": the
    head injury's own declaration (lift_head_injury). "doctor": everything
    else, by a declared clearance."""
    red_flag = _get(hold, "red_flag")
    source = _get(hold, "source")
    if red_flag == "minor" or source == "admin":
        return "admin_only"
    # Before "expires": the easy start after a break ends by itself too, but
    # it stays a break, which the rider can still mark a mistake.
    if source == "layoff" or red_flag == "layoff":
        return "layoff"
    if _get(hold, "expires_at") is not None:
        return "expires"
    if red_flag == "fever":
        return "fever_self"
    if red_flag == "head_injury":
        return "head_injury"
    return "doctor"


def can_lift(hold, how: str) -> bool:
    """Whether `how` may lift this hold. "mistake" is also refused for the
    rider's own screening answers (they change those instead)."""
    if how not in LIFT_HOW:
        raise ValueError(f"Unknown lift reason: {how!r}")
    if how == "mistake" and _get(hold, "source") == "screening":
        return False
    return lift_kind(hold) in _LIFTABLE[how]


def _open_holds(db: Session, user_id: str, now: datetime | None = None) -> list[SafetyHold]:
    """Holds not lifted and not past their expiry."""
    now = now or _now()
    return (
        db.query(SafetyHold)
        .filter(
            SafetyHold.user_id == user_id,
            SafetyHold.lifted_at.is_(None),
            or_(SafetyHold.expires_at.is_(None), SafetyHold.expires_at > now),
        )
        .all()
    )


def open_holds(db: Session, user: User | str) -> list[SafetyHold]:
    """Every hold in force for this rider. Query this rather than
    lifted_at IS NULL, which still counts a hold past its expiry."""
    return _open_holds(db, _uid(user))


def _most_severe(holds: list[SafetyHold]) -> SafetyHold | None:
    if not holds:
        return None
    return max(holds, key=lambda h: (HOLD_LEVELS.get(h.level, 0), h.opened_at or datetime.min))


def current_hold(db: Session, user_id: str) -> SafetyHold | None:
    """The most severe hold in force (hold_all beats easy_only; newest breaks
    a tie). A hold past its expires_at doesn't count."""
    return _most_severe(_open_holds(db, _uid(user_id)))


def _expire_due(db: Session, user_id: str, now: datetime) -> None:
    """Stamp holds past their expiry as lifted, so anything reading
    lifted_at directly sees them as over. Writers call it; reads don't need
    it, because they already ignore expired holds."""
    due = (
        db.query(SafetyHold)
        .filter(
            SafetyHold.user_id == user_id,
            SafetyHold.lifted_at.is_(None),
            SafetyHold.expires_at.isnot(None),
            SafetyHold.expires_at <= now,
        )
        .all()
    )
    for hold in due:
        hold.lifted_at = hold.expires_at
        hold.lifted_how = "expired"
    if due:
        db.flush()


def _covers(
    level: str, expires_at: datetime | None, other_level: str, other_expires: datetime | None
) -> bool:
    """Whether a hold at `level` ending at `expires_at` restricts at least as
    much, for at least as long, as one at `other_level` ending at
    `other_expires`. None means it never ends by itself."""
    if HOLD_LEVELS.get(level, 0) < HOLD_LEVELS.get(other_level, 0):
        return False
    if expires_at is None:
        return True
    return other_expires is not None and expires_at >= other_expires


def _ways_out(hold) -> frozenset[str]:
    """Every way this hold may be lifted (can_lift). Works on a SafetyHold
    or a dict."""
    return frozenset(how for how in LIFT_HOW if can_lift(hold, how))


# The red flags whose lift leaves an easy period behind it, ending by itself:
# the easy week after a fever, the easy riding after a head injury.
POST_LIFT_FLAGS = frozenset({"fever", "head_injury"})


def _post_lift_easy(db: Session, user_id: str, red_flag: str | None) -> list[SafetyHold]:
    """Every easy period a fever or head-injury lift has left for this red
    flag, running or not, oldest first."""
    if red_flag not in POST_LIFT_FLAGS:
        return []
    rows = (
        db.query(SafetyHold)
        .filter(
            SafetyHold.user_id == user_id,
            SafetyHold.red_flag == red_flag,
            SafetyHold.expires_at.isnot(None),
        )
        .order_by(SafetyHold.opened_at.asc())
        .all()
    )
    return [h for h in rows if lift_kind(h) == "expires"]


def _easy_running_at(easies: list[SafetyHold], moment: datetime | None) -> SafetyHold | None:
    """The post-lift easy period that was in force at `moment`, if any. One
    that began at that very moment doesn't count: a hold opened then is the
    one whose lift began it, never a mention of it."""
    if moment is None:
        return None
    for easy in easies:
        if (
            easy.opened_at is not None
            and easy.opened_at < moment < easy.expires_at
            and (easy.lifted_at is None or easy.lifted_at > moment)
        ):
            return easy
    return None


# A line in the note on a hold for a new illness or injury: one the detector
# read as new (Hit.new_event, "crashed again today and hit my head").
# open_hold writes it; remention_of reads it, so the hold is never taken for
# the earlier one mentioned again. Written by code only, so it can be
# matched exactly.
NEW_EVENT_MARK = "New, not an earlier one mentioned again."


def is_new_event(hold) -> bool:
    """Whether this hold is for a new illness or injury the detector read as
    new (open_hold's new_event). Works on a SafetyHold or a dict."""
    return NEW_EVENT_MARK in (_get(hold, "note") or "")


def _hit_is_new(hit) -> bool:
    """Hit.new_event from the detector, read defensively: a Hit without the
    field, or anything else, counts as not new."""
    return getattr(hit, "new_event", False) is True


def _remention_in(hold: SafetyHold, easies: list[SafetyHold]) -> SafetyHold | None:
    """The easy period in `easies` this hold re-mentions, or None."""
    if hold.source != "detector" or hold.expires_at is not None or is_new_event(hold):
        return None
    return _easy_running_at(easies, hold.opened_at)


def remention_of(db: Session, hold: SafetyHold) -> SafetyHold | None:
    """The easy period a detector hold re-mentions, or None.

    A rider in the easy week after a fever, or the easy riding after a head
    injury, often brings it up again ("I had a fever last week, feeling much
    better now", "how long after hitting my head can I do intervals?"). The
    detector opens a full hold for it, as it must, but that hold is about the
    same illness or injury: it sits beside the easy period rather than
    replacing it (open_hold), and for a head injury it never restarts the
    21-day racing clock (no_racing_or_group_until) or the two easy weeks
    (lift_head_injury).

    Two holds are never re-mentions, because each is a judgement that this
    is new: one the coach opens on purpose (apply_safety_hold), and one the
    detector read as a new event (open_hold's new_event, NEW_EVENT_MARK). A
    second head injury during the build-back restarts both clocks from its
    own day."""
    if hold.source != "detector" or hold.expires_at is not None or is_new_event(hold):
        return None
    return _easy_running_at(_post_lift_easy(db, hold.user_id, hold.red_flag), hold.opened_at)


def open_hold(
    db: Session,
    user: User | str,
    level: str,
    reason: str,
    source: str,
    red_flag: str | None = None,
    note: str | None = None,
    *,
    expires_at: datetime | None = None,
    new_event: bool = False,
    hit=None,
    commit: bool = True,
) -> SafetyHold:
    """Open a hold, or return the open one that already covers it.

    new_event (or a detector `hit` whose Hit.new_event is true, read with
    getattr so a Hit without the field counts as not new) says this is a new
    illness or injury, not one already on record mentioned again: "crashed
    again today and hit my head". It always gets its own hold, marked with
    NEW_EVENT_MARK, beside whatever is open: it never folds into a hold for
    an earlier day's injury, nor into the same injury mentioned again, and it
    supersedes nothing, so marking it a mistake never drops the first
    injury's hold. For a head injury that restarts the two easy weeks and
    the 21 days before racing or group riding from today (remention_of,
    lift_head_injury, no_racing_or_group_until). Only a hold opened today,
    and not for an earlier injury mentioned again, covers it.

    Holds are kept per concern: the same source and red flag. A second
    chest-pain message from the detector returns the chest-pain hold already
    open (when it is at the same level or higher, and lasts as long). A
    higher hold for the same concern supersedes the lower one, but only when
    nothing could lift the new hold that couldn't lift the old one: the easy
    week after a fever, or the easy riding after a head injury, ends only by
    itself, so a full hold for the same fever or head injury mentioned again
    sits beside it. Marking that new hold a mistake then lifts only the new
    hold, and the easy days run on. A different concern always gets its own
    hold, even under a higher one: "hit my head and my wrist is swollen"
    records both, so marking the head injury a mistake leaves the wrist on
    easy riding. Lifting one concern never quietly drops a restriction
    something else asked for."""
    if level not in HOLD_LEVELS:
        raise ValueError(f"Unknown hold level: {level!r}")
    if source not in HOLD_SOURCES:
        raise ValueError(f"Unknown hold source: {source!r}")
    uid = _uid(user)
    now = _now()
    flag = red_flag[:40] if red_flag else None
    _expire_due(db, uid, now)

    new_event = bool(new_event) or _hit_is_new(hit)
    easies = _post_lift_easy(db, uid, flag)

    same = [h for h in _open_holds(db, uid, now) if h.source == source and h.red_flag == flag]
    if new_event:
        # Only a hold opened today, and not for an earlier injury mentioned
        # again, is the same event.
        same = [
            h for h in same
            if h.opened_at is not None
            and h.opened_at.date() == now.date()
            and _remention_in(h, easies) is None
        ]
    for existing in same:
        if _covers(existing.level, existing.expires_at, level, expires_at):
            return existing
    new_ways = _ways_out(
        {"red_flag": flag, "source": source, "expires_at": expires_at, "level": level}
    )
    for lower in [] if new_event else same:
        if _covers(level, expires_at, lower.level, lower.expires_at) and (
            new_ways <= _ways_out(lower)
        ):
            lower.lifted_at = now
            lower.lifted_how = "superseded"

    if new_event:
        easy = _easy_running_at(easies, now) if expires_at is None else None
        line = NEW_EVENT_MARK
        if easy is not None:
            line += (
                f" The rider told us during the easy riding of hold {easy.id}, which "
                "stays in force beside it."
            )
        if flag == "head_injury" and (
            easy is not None or no_racing_or_group_until(db, uid, now.date()) is not None
        ):
            line += (
                " A second head injury, so the two easy weeks and the 21 days before "
                "racing or group riding count from today."
            )
        note = f"{note}\n{line}" if note else line
    elif source == "detector" and expires_at is None:
        easy = _easy_running_at(easies, now)
        if easy is not None:
            line = (
                f"Mentioned again during the easy riding of hold {easy.id}, which stays "
                "in force."
                + (
                    " The same injury, so racing waits for day 21 of the first mention."
                    if flag == "head_injury" else ""
                )
            )
            note = f"{note}\n{line}" if note else line

    hold = SafetyHold(
        user_id=uid,
        level=level,
        reason=(reason or "").strip()[:200] or "Safety hold",
        red_flag=flag,
        source=source,
        note=note,
        opened_at=now,
        expires_at=expires_at,
    )
    db.add(hold)
    _done(db, commit)
    return hold


def _stamp_lift(hold: SafetyHold, how: str, note: str | None, now: datetime) -> None:
    hold.lifted_at = now
    hold.lifted_how = how
    if note:
        hold.note = f"{hold.note}\n{note}" if hold.note else note


def lift_holds(
    db: Session, user: User | str, how: str, note: str | None = None, *, commit: bool = True
) -> int:
    """Lift every open hold that `how` may lift (see can_lift). An under-18
    hold, or one set by hand, stays unless how is "admin". While the account
    is held as under 18, nothing the rider does lifts anything (RIDER_LIFTS).
    Returns how many were lifted."""
    if how not in LIFT_HOW:
        raise ValueError(f"Unknown lift reason: {how!r}")
    uid = _uid(user)
    now = _now()
    _expire_due(db, uid, now)
    if how in RIDER_LIFTS and minor_hold(db, uid) is not None:
        return 0
    lifted = [h for h in _open_holds(db, uid, now) if can_lift(h, how)]
    for hold in lifted:
        _stamp_lift(hold, how, note, now)
    _done(db, commit)
    return len(lifted)


def lift_hold(
    db: Session,
    user: User | str,
    hold_id: str,
    how: str,
    note: str | None = None,
    *,
    commit: bool = True,
) -> SafetyHold | None:
    """Lift one of this rider's holds. None if it isn't theirs. Lifting a
    hold that is already lifted changes nothing. Raises HoldNotLiftable when
    `how` may not lift it (an under-18 hold by anything but admin), and for
    anything the rider does while the account is held as under 18: a
    mistake on a head-injury hold is refused then, like a clearance."""
    if how not in LIFT_HOW:
        raise ValueError(f"Unknown lift reason: {how!r}")
    uid = _uid(user)
    hold = (
        db.query(SafetyHold)
        .filter(SafetyHold.id == hold_id, SafetyHold.user_id == uid)
        .first()
    )
    if hold is None:
        return None
    if how in RIDER_LIFTS:
        minor = minor_hold(db, uid)
        if minor is not None:
            raise HoldNotLiftable(minor, how)
    now = _now()
    _expire_due(db, uid, now)
    if hold.lifted_at is None:
        if not can_lift(hold, how):
            raise HoldNotLiftable(hold, how)
        _stamp_lift(hold, how, note, now)
        _done(db, commit)
    return hold


def minor_hold(db: Session, user: User | str) -> SafetyHold | None:
    """The open under-18 hold, if this account has one."""
    for hold in _open_holds(db, _uid(user)):
        if hold.red_flag == "minor":
            return hold
    return None


# === Screening and the layoff gate ===


def latest_screening(db: Session, user_id: str) -> HealthScreening | None:
    """The rider's current screening (the newest one not superseded)."""
    return (
        db.query(HealthScreening)
        .filter(HealthScreening.user_id == user_id, HealthScreening.superseded_at.is_(None))
        .order_by(HealthScreening.created_at.desc())
        .first()
    )


# The health questions are asked again at least this often (and whenever
# the question set changes, or a red flag in chat is newer than the last
# answers and the last clearance).
RESCREEN_AFTER_DAYS = 365
# After a clinician's clearance (a doctor's, a midwife's or a physio's, or a
# head injury's own check) the yearly re-screen counts from the clearance:
# asking the questions again any sooner only turns a truthful yes ("do you
# ever get chest pain?", "a concussion in the past three months?") back into
# a hold that needs a second clearance. The same span as the yearly cadence,
# never a shorter one. A fever gone on the rider's own word isn't a
# clinician's look, so it doesn't move the yearly date; the fever itself
# stops counting as a red flag once lifted (rescreen_red_flags_after).
RESCREEN_QUIET_DAYS = RESCREEN_AFTER_DAYS


def latest_clearance_at(
    db: Session, user: User | str, *, clinician_only: bool = False
) -> datetime | None:
    """When the rider last declared a clearance, or None.

    Every clearance writes a consent row of kind "clearance": a clinician's
    (confirm_clearance), the head injury's own check (lift_head_injury) and a
    fever gone (lift_fever, doc_version FEVER_LIFT_VERSION). clinician_only
    leaves the fever out."""
    query = db.query(func.max(ConsentEvent.accepted_at)).filter(
        ConsentEvent.user_id == _uid(user), ConsentEvent.kind == "clearance"
    )
    if clinician_only:
        query = query.filter(ConsentEvent.doc_version != FEVER_LIFT_VERSION)
    return query.scalar()


def rescreen_quiet_until(db: Session, user: User | str) -> datetime | None:
    """When the yearly re-screen falls due, when a clinician's clearance has
    put it later than a year after the rider's answers, or None when nothing
    holds it back.

    It is RESCREEN_AFTER_DAYS after the latest clinician's clearance
    (latest_clearance_at, clinician_only), the normal yearly cadence counted
    from the last time someone qualified looked. A fever gone on the rider's
    word doesn't move it. It never holds back a rider who has never answered
    the questions, or one whose answers are on an older version of them: new
    questions are always asked. Nor does it hide a red flag newer than the
    clearance (rescreen_red_flags_after)."""
    uid = _uid(user)
    screening = latest_screening(db, uid)
    if screening is None or screening.version != SCREENING_VERSION:
        return None
    cleared = latest_clearance_at(db, uid, clinician_only=True)
    if cleared is None:
        return None
    until = cleared + timedelta(days=RESCREEN_AFTER_DAYS)
    return until if until > _now() else None


def rescreen_red_flags_after(db: Session, user: User | str, screening) -> datetime:
    """The moment after which a red flag in chat makes the re-screen due:
    the later of the rider's last answers and their last clearance of any
    kind (a clinician's, the head check, or a fever gone).

    A red flag a clearance has dealt with is older than the clearance, so it
    never reopens the questions: a doctor clears chest pain, and the rider
    isn't asked again a month later only to say yes and be held again. A new
    one after the clearance still asks them."""
    cleared = latest_clearance_at(db, user)
    answered = screening.created_at
    return cleared if cleared is not None and cleared > answered else answered


def _gate_days(gap_days: int) -> int:
    return LONG_LAYOFF_GATE_DAYS if gap_days >= LONG_LAYOFF_GAP_DAYS else LAYOFF_GATE_DAYS


def layoff_hold_ends(days_off: float | None, now: datetime | None = None) -> datetime:
    """When a hold for a break the rider told us about ends by itself: two
    weeks from now, four after three months or more off. A break of unknown
    length gets the four: never cut an easy start short on a guess."""
    now = now or _now()
    if days_off is None:
        return now + timedelta(days=LONG_LAYOFF_GATE_DAYS)
    return now + timedelta(days=_gate_days(int(days_off)))


def _gate_from_rides(db: Session, user_id: str, today: date) -> date | None:
    last = (
        db.query(func.max(Ride.ride_date))
        .filter(Ride.user_id == user_id, Ride.ride_date.isnot(None))
        .scalar()
    )
    if last is None:
        # No rides at all: the screening's long-break answer covers them.
        return None
    idle = (today - last.date()).days
    if idle >= LAYOFF_GAP_DAYS:
        # Still off the bike: whenever they come back, the gate runs from then.
        return today + timedelta(days=_gate_days(idle))

    # Back on the bike. A return inside the longest possible gate window is
    # the only one that can still be gating, so look at that window plus the
    # last ride before it.
    window_start = datetime.combine(today - timedelta(days=LONG_LAYOFF_GATE_DAYS), time.min)
    recent = sorted({
        r.date()
        for (r,) in db.query(Ride.ride_date).filter(
            Ride.user_id == user_id, Ride.ride_date >= window_start
        )
    })
    before = (
        db.query(func.max(Ride.ride_date))
        .filter(Ride.user_id == user_id, Ride.ride_date < window_start)
        .scalar()
    )
    prev = before.date() if before is not None else None
    gate = None
    for day in recent:
        if prev is not None:
            gap = (day - prev).days
            if gap >= LAYOFF_GAP_DAYS:
                until = day + timedelta(days=_gate_days(gap))
                gate = until if gate is None else max(gate, until)
        prev = day
    return gate if gate is not None and gate > today else None


def layoff_gate_until(
    db: Session, user: User | str, today: date | None = None
) -> date | None:
    """The first day hard sessions are allowed again after a layoff, or None
    if no layoff gate applies.

    From the screening: "four weeks or more off in the past three months"
    gates 14 days from the answer. From rides: none in the last 28 days gates
    until 14 days from today (28 if 90 days or more); a return after a gap
    of 28 days or more gates until 14 days after the first ride back (28
    after a 90-day gap). When both apply, the later date wins. Rides without
    a date are ignored."""
    uid = _uid(user)
    today = today or _today()
    gates = []

    screening = latest_screening(db, uid)
    if screening is not None and screening.long_break and screening.created_at is not None:
        until = screening.created_at.date() + timedelta(days=LAYOFF_GATE_DAYS)
        if today < until:
            gates.append(until)

    from_rides = _gate_from_rides(db, uid, today)
    if from_rides is not None:
        gates.append(from_rides)

    return max(gates) if gates else None


# === The gate ===


def _combine(
    hold: SafetyHold | None,
    screening: HealthScreening | None,
    gate: date | None,
    on_date: date,
) -> str:
    allowed = "all"
    if hold is not None:
        allowed = _lower(allowed, "none" if hold.level == "hold_all" else "easy")
    if (
        screening is not None
        and screening.tier != "none"
        and screening.clearance_confirmed_at is None
    ):
        allowed = _lower(allowed, "none" if screening.tier == "hold_all" else "easy")
    if gate is not None and on_date < gate:
        allowed = _lower(allowed, "easy")
    return allowed


def _holds_on(holds: list[SafetyHold], on_date: date, now: datetime) -> list[SafetyHold]:
    """The holds that still apply on `on_date`. Today (or earlier) a hold
    counts until the moment it expires; a later day counts while it is on or
    before the expiry date, so the last day of an easy week stays easy."""
    today = now.date()
    out = []
    for hold in holds:
        if hold.expires_at is None:
            out.append(hold)
        elif on_date <= today:
            if hold.expires_at > now:
                out.append(hold)
        elif on_date <= hold.expires_at.date():
            out.append(hold)
    return out


def allowed_intensity(
    db: Session, user: User | str, on_date: date | None = None
) -> str:
    """How hard this rider may be asked to ride on `on_date` (default today):
    "none" (nothing prescribed), "easy" (recovery and endurance at EASY_CAP
    or below, no tests) or "all"."""
    uid = _uid(user)
    now = _now()
    today = now.date()
    if isinstance(on_date, datetime):
        on_date = on_date.date()
    on_date = on_date or today
    return _combine(
        _most_severe(_holds_on(_open_holds(db, uid, now), on_date, now)),
        latest_screening(db, uid),
        layoff_gate_until(db, uid, today=today),
        on_date,
    )


def max_step_pct(workout) -> float:
    """The highest power any step asks for, as a fraction of FTP. Works on a
    Workout row or a dict with "steps"."""
    peak = 0.0
    for step in _get(workout, "steps") or []:
        for key in ("power_target_pct", "power_high_pct"):
            value = _get(step, key)
            if value is not None:
                peak = max(peak, float(value))
    return peak


def _workout_type(workout) -> str | None:
    value = _get(workout, "workout_type")
    return getattr(value, "value", value)


def workout_exceeds_ceiling(workout) -> bool:
    """True when a step asks for more than its session type allows: a
    "recovery" ride carrying VO2max intervals, say. Rest days and unknown
    types are held to the recovery ceiling, so a mystery label fails closed."""
    ceiling = INTENSITY_CEILING.get(_workout_type(workout) or "", INTENSITY_CEILING["recovery"])
    return max_step_pct(workout) > ceiling + _EPS


def workout_allowed(workout, allowed: str) -> bool:
    """Whether a session may be prescribed or ridden under `allowed`.

    "all": anything within its type's ceiling. "easy": recovery and
    endurance only, no step above EASY_CAP. "none": rest only."""
    wtype = _workout_type(workout)
    if wtype == "rest":
        return True
    if allowed == "none":
        return False
    if workout_exceeds_ceiling(workout):
        return False
    if allowed == "easy":
        return wtype in EASY_TYPES and max_step_pct(workout) <= EASY_CAP + _EPS
    return True


# === Clearance and consent ===


def client_ip(request) -> str | None:
    """The rider's address for a consent record, read exactly as the rate
    limiter reads it (app.core.ratelimit.client_ip), so the two never
    disagree about which forwarded hop is the rider. Imported here, not at
    the top, so the import order of the two modules can't break either."""
    from app.core.ratelimit import client_ip as shared_client_ip

    ip = shared_client_ip(request)
    return (ip or "").strip()[:64] or None


def record_consent(
    db: Session,
    user_id: User | str,
    kind: str,
    doc_version: str,
    text_shown: str,
    request=None,
    source: str = "app",
    *,
    commit: bool = True,
) -> ConsentEvent:
    """Append one consent row: the words shown, verbatim, and where from."""
    if kind not in CONSENT_KINDS:
        raise ValueError(f"Unknown consent kind: {kind!r}")
    if not text_shown or not text_shown.strip():
        raise ValueError("A consent record needs the exact text the rider was shown")
    ip = user_agent = None
    if request is not None:
        ip = client_ip(request)
        user_agent = (request.headers.get("user-agent") or "")[:300] or None
    event = ConsentEvent(
        user_id=_uid(user_id),
        kind=kind,
        doc_version=(doc_version or "")[:40],
        text_shown=text_shown,
        accepted_at=_now(),
        ip=ip,
        user_agent=user_agent,
        source=(source or "app")[:20],
        app_build=settings.app_build or None,
    )
    db.add(event)
    _done(db, commit)
    return event


def confirm_clearance(
    db: Session,
    user: User | str,
    by: str,
    limits: str | None,
    request=None,
    *,
    commit: bool = True,
) -> HealthScreening | None:
    """The rider declares a doctor (or midwife, or physio) has cleared them.

    Forma does not verify it; the terms say so. Stamps the current screening,
    lifts the open holds a doctor can lift (lift_kind "doctor"), and records
    the declaration. A fever hold, the easy week after one, the easy start
    after a break and anything set by hand stay as they are.

    A physio or a midwife clears only their own concern (clinician_scope): a
    physio lifts an injury hold, never a heart, head, medicine or pregnancy
    one, and stamps the screening only when every yes in it is theirs. So a
    physio clearing a knee never counts as clearing a medicine for a year.
    Raises ClearanceOutOfScope, recording nothing, when they could clear
    nothing that is holding the rider.

    Any limits they were given are recorded on their own (clearance_limits),
    for the coach to treat as hard constraints for as long as they stand: a
    later re-screen or a second clearance without limits never loses them.

    A head injury is never lifted here: it has its own declaration
    (lift_head_injury), because "cleared me for hard training" is not what
    a graded return after a head injury needs the rider to confirm.

    Raises NothingToClear, recording nothing, when there is nothing a
    clearance can lift: no hold it covers and no health answer waiting.

    Raises HoldNotLiftable while the account is held as under 18: there is
    nothing a clearance can do for it. Returns the stamped screening, if any."""
    uid = _uid(user)
    by = (by or "").strip()[:200]
    if not by:
        raise ValueError("Say who cleared you")
    minor = minor_hold(db, uid)
    if minor is not None:
        raise HoldNotLiftable(minor, "clearance")
    limits = (limits or "").strip() or None
    now = _now()
    _expire_due(db, uid, now)

    screening = latest_screening(db, uid)
    scope = clinician_scope(by)
    yes = (
        {q for q, answer in (screening.answers or {}).items() if answer}
        if screening is not None else set()
    )
    if scope is None or screening is None:
        screening_in_scope = scope is None
    else:
        screening_in_scope = bool(yes) and yes <= scope[2]
    open_now = _open_holds(db, uid, now)
    doctor_holds = [h for h in open_now if can_lift(h, "clearance")]
    # Held back for a doctor too, though not by this form: a physio or a
    # midwife is told a doctor is needed, as for any other doctor's hold.
    head_holds = [h for h in open_now if lift_kind(h) == "head_injury"]
    lifting = [
        h for h in doctor_holds
        if scope is None
        or (screening_in_scope if h.source == "screening" else h.red_flag in scope[1])
    ]
    screen_waiting = _screening_waiting(screening)
    clears_screening = screen_waiting and screening_in_scope
    if (
        scope is not None
        and not lifting
        and not clears_screening
        and (doctor_holds or head_holds or screen_waiting)
    ):
        raise ClearanceOutOfScope(
            scope[0], scope[3],
            _needs(doctor_holds + head_holds, yes if screen_waiting else set()),
        )
    if not lifting and not clears_screening:
        raise NothingToClear(_nothing_to_clear(open_now))

    if screening is not None and screening_in_scope:
        screening.clearance_confirmed_at = now
        screening.clearance_by = by
        if limits:
            # Never wiped by a later clearance that came with none.
            screening.clearance_limits = limits

    note = f"Cleared by: {by}." + (f" Limits: {limits}" if limits else "")
    for hold in lifting:
        _stamp_lift(hold, "clearance", note, now)
    consent = record_consent(
        db, uid, "clearance",
        screening.version if screening is not None else SCREENING_VERSION,
        CLEARANCE_TEXT, request=request, source="app", commit=False,
    )
    if limits:
        cleared_for = [hold.reason for hold in lifting]
        if screening is not None and screening_in_scope and screening.tier != "none":
            cleared_for.append(f"health screening ({screening.version})")
        db.add(ClearanceLimit(
            user_id=uid,
            limits=limits,
            cleared_by=by,
            cleared_for="; ".join(cleared_for) or None,
            consent_event_id=consent.id,
            recorded_at=now,
        ))
    _done(db, commit)
    return screening


def _nothing_to_clear(open_now: list[SafetyHold]) -> str:
    """Why a clearance has nothing to lift, and what does lift the hold."""
    kinds = {lift_kind(h): h for h in open_now}
    if "head_injury" in kinds:
        return (
            "Your hold is for a head injury, so it has its own check. Tap I've been "
            "cleared on the hold notice and confirm a doctor has checked you since "
            "you hit your head."
        )
    if "fever_self" in kinds:
        return (
            "Your hold is for a fever, so it lifts on your word, not a doctor's. Tap "
            "My fever has gone on the hold notice once it has been gone for 24 hours."
        )
    if "admin_only" in kinds:
        return (
            "This hold was set by Forma, so a clearance can't lift it. If you think "
            f"it's wrong, email {FORMA_EMAIL}."
        )
    ending = kinds.get("expires") or kinds.get("layoff")
    if ending is not None and ending.expires_at is not None:
        return (
            "Nothing here needs a doctor's clearance. Your easy riding ends by itself "
            f"on {_day_month(ending.expires_at)}."
        )
    if ending is not None:
        return "Nothing here needs a doctor's clearance. Your easy start after a break ends by itself."
    return "Nothing is on hold for a clearance to lift, so I haven't recorded one."


def lift_head_injury(
    db: Session,
    user: User | str,
    hold_id: str,
    by: str,
    limits: str | None = None,
    request=None,
    *,
    commit: bool = True,
) -> SafetyHold | None:
    """The rider declares a doctor has checked them since they hit their
    head and they've had no symptoms for at least 24 hours (HEAD_LIFT_TEXT,
    recorded word for word with who checked them).

    Every open head-injury hold lifts, and the return stays graded: an
    easy_only hold takes their place until HEAD_EASY_DAYS after the injury
    (the newest of those holds' opened_at, the day the rider told us),
    ending by itself, and no_racing_or_group_until() keeps racing and group
    riding off until HEAD_NO_RACING_DAYS after it. A rider checked after
    those two weeks have passed goes straight back to full training.

    A physio or a midwife can't make this declaration: raises
    ClearanceOutOfScope, recording nothing. Raises HoldNotLiftable for any
    other kind of hold, or while the account is held as under 18. Returns
    the easy hold, or the lifted hold when no easy days are left; a second
    tap changes nothing and returns the hold. None if the hold isn't this
    rider's."""
    uid = _uid(user)
    by = (by or "").strip()[:200]
    if not by:
        raise ValueError("Say which doctor checked you")
    hold = (
        db.query(SafetyHold)
        .filter(SafetyHold.id == hold_id, SafetyHold.user_id == uid)
        .first()
    )
    if hold is None:
        return None
    minor = minor_hold(db, uid)
    if minor is not None:
        raise HoldNotLiftable(minor, "head_clearance")
    if hold.lifted_at is not None:
        if hold.lifted_how == "head_clearance":
            return hold
        raise HoldNotLiftable(hold, "head_clearance")
    if not can_lift(hold, "head_clearance"):
        raise HoldNotLiftable(hold, "head_clearance")
    scope = clinician_scope(by)
    if scope is not None:
        raise ClearanceOutOfScope(scope[0], scope[3], "doctor")
    limits = (limits or "").strip() or None

    now = _now()
    _expire_due(db, uid, now)
    heads = [h for h in _open_holds(db, uid, now) if lift_kind(h) == "head_injury"]
    # A head injury mentioned again during its easy riding is the same
    # injury: the two weeks still run from the day the rider first told us.
    injured_at = max(_injury_day(db, h, now) for h in heads)
    note = (
        f"Checked by: {by}. The rider declared no symptoms for at least 24 hours."
        + (f" Limits: {limits}" if limits else "")
    )
    for head in heads:
        _stamp_lift(head, "head_clearance", note, now)
    db.flush()

    easy_ends = injured_at + timedelta(days=HEAD_EASY_DAYS)
    easy = None
    if easy_ends > now:
        easy = open_hold(
            db, uid, "easy_only", HEAD_EASY_REASON, hold.source, red_flag="head_injury",
            note=(
                f"Follows hold {hold.id}: a doctor checked the rider and they declared "
                "no symptoms for 24 hours. Ends by itself two weeks after the injury."
            ),
            expires_at=easy_ends, commit=False,
        )
    consent = record_consent(
        db, uid, "clearance", HEAD_LIFT_VERSION, HEAD_LIFT_TEXT,
        request=request, source="app", commit=False,
    )
    if limits:
        db.add(ClearanceLimit(
            user_id=uid,
            limits=limits,
            cleared_by=by,
            cleared_for="; ".join(h.reason for h in heads) or None,
            consent_event_id=consent.id,
            recorded_at=now,
        ))
    _done(db, commit)
    return easy or hold


def _injury_day(db: Session, hold: SafetyHold, now: datetime) -> datetime:
    """When the head injury behind this hold happened, as far as Forma
    knows: the day the rider first told us. A re-mention during the easy
    riding (remention_of) is dated from the injury it re-mentions, which is
    two easy weeks before that easy riding ends. A second injury (a coach
    hold, or one the detector read as new) is dated from its own day."""
    easy = remention_of(db, hold)
    if easy is not None:
        return easy.expires_at - timedelta(days=HEAD_EASY_DAYS)
    return hold.opened_at or now


def no_racing_or_group_until(
    db: Session, user: User | str, today: date | None = None
) -> date | None:
    """The first day racing and group riding are allowed again after a head
    injury: HEAD_NO_RACING_DAYS after the day the rider told us about it,
    whether or not a doctor has checked them yet. None when no head injury
    holds it back. A head-injury hold marked a mistake, or lifted by Forma,
    doesn't count; one lifted by its own declaration still does, because
    that is the graded return this date protects. Nor does the same injury
    mentioned again during its easy riding (remention_of): that never
    restarts the clock. A second head injury does (a hold the coach opened,
    or one the detector read as new): racing waits for day 21 of the latest
    injury."""
    uid = _uid(user)
    today = today or _today()
    since = datetime.combine(today - timedelta(days=HEAD_NO_RACING_DAYS), time.min)
    rows = (
        db.query(SafetyHold)
        .filter(
            SafetyHold.user_id == uid,
            SafetyHold.red_flag == "head_injury",
            # The injury itself, not the easy weeks that follow a lift.
            SafetyHold.expires_at.is_(None),
            SafetyHold.opened_at >= since,
            or_(
                SafetyHold.lifted_how.is_(None),
                SafetyHold.lifted_how.notin_(("mistake", "admin")),
            ),
        )
        .all()
    )
    easies = _post_lift_easy(db, uid, "head_injury") if rows else []
    days = [
        hold.opened_at.date() + timedelta(days=HEAD_NO_RACING_DAYS)
        for hold in rows
        if hold.opened_at and _remention_in(hold, easies) is None
    ]
    later = [day for day in days if day > today]
    return max(later) if later else None


def lift_fever(
    db: Session,
    user: User | str,
    hold_id: str,
    request=None,
    *,
    commit: bool = True,
) -> SafetyHold | None:
    """The rider declares their fever gone: 24 hours without paracetamol or
    ibuprofen, and their chest has cleared (FEVER_LIFT_TEXT, recorded word
    for word). No doctor is named, because none is claimed.

    Every open fever hold lifts, and an easy_only hold takes its place for
    FEVER_EASY_DAYS, ending by itself (SAFETY LAW 2d: the first week back is
    easy riding, no intervals and no tests). Returns that easy-week hold; a
    second tap on a hold already lifted this way changes nothing and returns
    it. None if the hold isn't this rider's. Raises HoldNotLiftable for any
    other kind of hold, or while the account is held as under 18."""
    uid = _uid(user)
    hold = (
        db.query(SafetyHold)
        .filter(SafetyHold.id == hold_id, SafetyHold.user_id == uid)
        .first()
    )
    if hold is None:
        return None
    minor = minor_hold(db, uid)
    if minor is not None:
        raise HoldNotLiftable(minor, "fever_self")
    if hold.lifted_at is not None:
        if hold.lifted_how == "fever_self":
            return hold
        raise HoldNotLiftable(hold, "fever_self")
    if not can_lift(hold, "fever_self"):
        raise HoldNotLiftable(hold, "fever_self")

    now = _now()
    _expire_due(db, uid, now)
    for fever in _open_holds(db, uid, now):
        if lift_kind(fever) == "fever_self":
            _stamp_lift(fever, "fever_self", "The rider declared the fever gone.", now)
    db.flush()
    easy = open_hold(
        db, uid, "easy_only", FEVER_EASY_REASON, hold.source, red_flag="fever",
        note=f"Follows hold {hold.id}: the rider declared the fever gone. Ends by itself.",
        expires_at=now + timedelta(days=FEVER_EASY_DAYS), commit=False,
    )
    record_consent(
        db, uid, "clearance", FEVER_LIFT_VERSION, FEVER_LIFT_TEXT,
        request=request, source="app", commit=False,
    )
    _done(db, commit)
    return easy


# === The doctor's limits ===


def active_limits(db: Session, user: User | str) -> list[ClearanceLimit]:
    """Every limit a clinician set that an admin hasn't retired, oldest
    first. A re-screen, a second clearance and a lifted hold never remove
    one. The coach treats each as a hard constraint."""
    return (
        db.query(ClearanceLimit)
        .filter(ClearanceLimit.user_id == _uid(user), ClearanceLimit.retired_at.is_(None))
        .order_by(ClearanceLimit.recorded_at.asc())
        .all()
    )


def _long_date(value: datetime) -> str:
    return f"{value.day} {value:%B %Y}"


def limit_lines(db: Session, user: User | str) -> list[str]:
    """The active limits as lines for the coach: what to avoid, who said
    it and when, so a dated one ("for six weeks") can be read in context."""
    lines = []
    for limit in active_limits(db, user):
        who = limit.cleared_by or "their clinician"
        lines.append(f"{limit.limits} (from {who}, {_long_date(limit.recorded_at)})")
    return lines


def retire_limit(
    db: Session, limit_id: str, note: str, *, commit: bool = True
) -> ClearanceLimit | None:
    """Admin only: stop treating one limit as active (the clinician lifted
    it, say). The row stays, with when and why."""
    note = (note or "").strip()
    if not note:
        raise ValueError("Say why the limit no longer applies")
    limit = db.get(ClearanceLimit, limit_id)
    if limit is None:
        return None
    if limit.retired_at is None:
        limit.retired_at = _now()
        limit.retired_note = note
        _done(db, commit)
    return limit


# === What each hold says ===
#
# One place for the words a hold puts on the plan and on a refused export,
# so a fever never mentions a doctor, an under-18 account never offers a way
# out, and the easy week after illness never reads as a break. The labels
# are fixed prefixes with no dates in them, so a labelled session can be
# recognised (HOLD_LABELS) and its label swapped when the gate changes.

DOCTOR_HOLD_LABEL = "On hold until you tell me a doctor has cleared you. "
ACCOUNT_HOLD_LABEL = "On hold. "
FEVER_HOLD_LABEL = "On hold until your fever has been gone for 24 hours. "
HEAD_HOLD_LABEL = (
    "On hold until a doctor has checked you and you've had no symptoms for 24 hours. "
)
BREAK_LABEL = "Not yet, because you're coming back from a break. Ride easy instead. "
ILLNESS_EASY_LABEL = "Not yet, because this is your easy week after illness. Ride easy instead. "
HEAD_EASY_LABEL = "Not yet, because you're building back after a head injury. Ride easy instead. "
EASY_FOR_NOW_LABEL = "Not yet. Ride easy for now instead. "
HOLD_LABELS = (
    DOCTOR_HOLD_LABEL,
    ACCOUNT_HOLD_LABEL,
    FEVER_HOLD_LABEL,
    HEAD_HOLD_LABEL,
    BREAK_LABEL,
    ILLNESS_EASY_LABEL,
    HEAD_EASY_LABEL,
    EASY_FOR_NOW_LABEL,
)
# For "Easy for now, because {reason}. Hard sessions start again on 15 October."
_EASY_BECAUSE = {
    HEAD_EASY_LABEL: "you're building back after a head injury",
    ILLNESS_EASY_LABEL: "this is your easy week after illness",
    EASY_FOR_NOW_LABEL: "you're easing back in",
    BREAK_LABEL: "you're coming back from a break",
}
# When several holds apply, the one that asks most of the rider speaks:
# an account hold, then a doctor, a head injury, a fever, and last a break.
_KIND_RANK = {
    "admin_only": 0, "doctor": 1, "head_injury": 2, "fever_self": 3, "layoff": 4, "expires": 5,
}


def _screening_waiting(screening: HealthScreening | None) -> bool:
    return (
        screening is not None
        and screening.tier != "none"
        and screening.clearance_confirmed_at is None
    )


def _ranked(holds: list) -> list:
    return sorted(
        holds,
        key=lambda h: (_KIND_RANK[lift_kind(h)], -HOLD_LEVELS.get(_get(h, "level"), 0)),
    )


def hold_label(hold) -> str:
    """The label a session carries while this hold keeps the rider off it.
    Works on a SafetyHold or a dict."""
    kind = lift_kind(hold)
    red_flag = _get(hold, "red_flag")
    if kind == "admin_only":
        return ACCOUNT_HOLD_LABEL
    if kind == "layoff":
        # A full hold for a break: nothing to ride, and no doctor to see.
        return ACCOUNT_HOLD_LABEL if _get(hold, "level") == "hold_all" else BREAK_LABEL
    if kind == "expires":
        return {"fever": ILLNESS_EASY_LABEL, "head_injury": HEAD_EASY_LABEL}.get(
            red_flag, EASY_FOR_NOW_LABEL
        )
    if kind == "fever_self":
        return FEVER_HOLD_LABEL
    if kind == "head_injury":
        return HEAD_HOLD_LABEL
    return DOCTOR_HOLD_LABEL


def standing_label(db: Session, user: User | str) -> str:
    """The label for a session the standing gate keeps the rider off: the
    holds that last until something lifts them, and a health answer waiting
    for a doctor. Holds that end by themselves belong to the easy window
    (easy_windows), not here."""
    uid = _uid(user)
    ranked = _ranked([h for h in _open_holds(db, uid) if h.expires_at is None])
    if ranked and lift_kind(ranked[0]) == "admin_only":
        return ACCOUNT_HOLD_LABEL
    if _screening_waiting(latest_screening(db, uid)):
        return DOCTOR_HOLD_LABEL
    return hold_label(ranked[0]) if ranked else DOCTOR_HOLD_LABEL


def easy_windows(db: Session, user: User | str) -> list[tuple[date, str, str]]:
    """Every easy window in force: (first day hard sessions are allowed
    again, label, reason). The layoff gate is a break; a hold that ends by
    itself is the easy week after illness, the build back after a head
    injury, or a break. Most specific first, so easy_window_on picks the
    head injury over illness over a break on a day they share."""
    uid = _uid(user)
    windows = []
    gate = layoff_gate_until(db, uid)
    if gate is not None:
        windows.append((gate, BREAK_LABEL, _EASY_BECAUSE[BREAK_LABEL]))
    for hold in _open_holds(db, uid):
        if hold.expires_at is None:
            continue
        label = hold_label(hold)
        if label not in _EASY_BECAUSE:
            continue
        # The hold covers its last day, so hard sessions start the day after.
        windows.append((hold.expires_at.date() + timedelta(days=1), label, _EASY_BECAUSE[label]))
    order = list(_EASY_BECAUSE)
    return sorted(windows, key=lambda w: (order.index(w[1]), w[0]))


def easy_window_on(
    windows: list[tuple[date, str, str]], day: date
) -> tuple[str, str] | None:
    """The label and reason for a hard session on `day`, from
    easy_windows(), or None when no window covers that day."""
    for until, label, because in windows:
        if day < until:
            return label, because
    return None


def _hold_clause(hold) -> str:
    kind = lift_kind(hold)
    red_flag = _get(hold, "red_flag")
    full = _get(hold, "level") == "hold_all"
    expires_at = _get(hold, "expires_at")
    if red_flag == "minor":
        return "This account is on hold because Forma is for adults, 18 and over"
    if kind == "admin_only":
        return "This account is on hold"
    if kind == "fever_self":
        return "Riding is on hold until your fever has been gone for 24 hours"
    if kind == "head_injury":
        return (
            "Riding is on hold until a doctor has checked you since you hit your head "
            "and you've had no symptoms for at least 24 hours"
        )
    if kind == "expires":
        until = _day_month(expires_at)
        if red_flag == "fever":
            return f"Riding is easy only until {until}, your first week back after illness"
        if red_flag == "head_injury":
            return f"Riding is easy only until {until} while you build back after a head injury"
        return f"Hard sessions are on hold until {until}"
    if kind == "layoff":
        what = "Riding is on hold" if full else "Hard sessions are on hold"
        until = f" until {_day_month(expires_at)}" if expires_at is not None else ""
        return f"{what}{until} while you ease back in after a break"
    what = "Riding is on hold" if full else "Hard sessions are on hold"
    return f"{what} until you tell me a doctor has cleared you"


def hold_explanation(hold) -> str:
    """One plain sentence on what this hold means and what lifts it, in its
    own words: a fever never mentions a doctor, and an under-18 account or a
    hold set by hand says where to write instead of how to lift it. Works on
    a SafetyHold or a dict."""
    clause = _hold_clause(hold)
    if lift_kind(hold) == "admin_only":
        return f"{clause}. If you think that's wrong, email {FORMA_EMAIL}."
    return f"{clause}."


EXPORT_EASY_REFUSAL = (
    "Hard sessions are on hold for now, so I can't export this one yet. Ride it in "
    "Forma's ride mode and you'll get the easy version."
)


def export_refusal(db: Session, user: User | str) -> str:
    """Why a session the gate rules out today can't be exported, worded for
    what holds the rider back: an under-18 account, a fever and a head
    injury each have their own words, and only a hold a doctor lifts says
    "doctor"."""
    uid = _uid(user)
    if allowed_intensity(db, uid) != "none":
        return EXPORT_EASY_REFUSAL
    minor = minor_hold(db, uid)
    if minor is not None:
        return (
            f"{_hold_clause(minor)}, so I can't export sessions. If you think that's "
            f"wrong, email {FORMA_EMAIL}."
        )
    ranked = _ranked([h for h in _open_holds(db, uid) if h.level == "hold_all"])
    if ranked and lift_kind(ranked[0]) == "admin_only":
        return (
            f"{_hold_clause(ranked[0])}, so I can't export sessions. If you think "
            f"that's wrong, email {FORMA_EMAIL}."
        )
    if not ranked or _screening_waiting(latest_screening(db, uid)):
        # Nothing but a health answer holds everything: that needs a doctor.
        return (
            "Riding is on hold until you tell me a doctor has cleared you, so I can't "
            "export this session yet."
        )
    return f"{_hold_clause(ranked[0])}, so I can't export this session yet."


# === Forma's own tools ===


def admin_lift(
    db: Session,
    hold_id: str,
    note: str,
    *,
    commit: bool = True,
    sync_plan: bool = True,
) -> SafetyHold | None:
    """Forma lifts a hold by hand, saying why. The only way an under-18 hold,
    or one set by hand, ever lifts. An under-18 hold lifts with every other
    under-18 hold on the account, because an age is one fact about the
    account: an adult the detector misread as 16 is freed in one step.

    Takes the on-hold labels off the plan as well (sync_plan). Returns the
    hold, or None if there is no such hold. Lifting one already lifted
    changes nothing."""
    note = (note or "").strip()
    if not note:
        raise ValueError("Say why the hold is being lifted")
    hold = db.get(SafetyHold, hold_id)
    if hold is None:
        return None
    now = _now()
    _expire_due(db, hold.user_id, now)
    if hold.lifted_at is None:
        targets = [hold]
        if hold.red_flag == "minor":
            targets += [
                h for h in _open_holds(db, hold.user_id, now)
                if h.red_flag == "minor" and h.id != hold.id
            ]
        for target in targets:
            _stamp_lift(target, "admin", f"Lifted by Forma: {note}", now)
    _done(db, commit)
    if sync_plan:
        # Here, not at the top: plan_service imports this module.
        from app.services.plan_service import sync_hold_marks

        sync_hold_marks(db, hold.user_id, commit=commit)
    return hold


# After Forma lifts an under-18 hold by hand (an adult the detector misread),
# the detector's under-18 rule stays quiet for this long on that account, so
# "I'm 16 and 18 watts up" can't put the same adult straight back on hold.
MINOR_QUIET_DAYS = 180


def _birth_date(user: User | None) -> date | None:
    """The date of birth on file as a date, or None when there is none or
    it can't be read."""
    born = getattr(user, "date_of_birth", None)
    if born is None:
        return None
    if isinstance(born, datetime):
        return born.date()
    if isinstance(born, date):
        return born
    try:
        return date.fromisoformat(str(born)[:10])
    except ValueError:
        return None


def _under_18_on(born: date, day: date) -> bool:
    return _years_after(datetime.combine(born, time.min), 18).date() > day


def _under_18_by_birth(user: User, today: date) -> bool:
    """Whether the date of birth on file makes this rider under 18 today.
    False when there is none, or it can't be read."""
    born = _birth_date(user)
    return born is not None and _under_18_on(born, today)


def minor_quiet_until(db: Session, user: User | str) -> datetime | None:
    """When the detector's under-18 rule may speak again for this account,
    or None when it may speak now.

    Forma lifting an under-18 hold by hand (admin_lift) is a decision that
    this rider is an adult, so for MINOR_QUIET_DAYS after the latest such
    lift the detector should open no under-18 hold and send no adults-only
    reply on its own reading of their words. The quiet never applies while
    an under-18 hold is open, or when the date of birth on file makes them
    under 18: then the account is a child's on its own record. The coach can
    still flag the account on purpose (apply_safety_hold), and Forma can
    still close it by hand."""
    if isinstance(user, str):
        user = db.get(User, user)
    if user is None:
        return None
    now = _now()
    if _under_18_by_birth(user, now.date()) or minor_hold(db, user.id) is not None:
        return None
    lifted = (
        db.query(func.max(SafetyHold.lifted_at))
        .filter(
            SafetyHold.user_id == user.id,
            SafetyHold.red_flag == "minor",
            SafetyHold.lifted_how == "admin",
        )
        .scalar()
    )
    if lifted is None:
        return None
    until = lifted + timedelta(days=MINOR_QUIET_DAYS)
    return until if until > now else None


def mark_event_reviewed(
    db: Session, event_id: str, note: str, *, commit: bool = True
) -> SafetyEvent | None:
    """Forma records its review of one red-flag event, for the safeguarding
    log. reviewed_at keeps the first review (the protocol asks for one
    within 24 hours); a later review adds a dated line to the note rather
    than replacing it. None if there is no such event."""
    note = (note or "").strip()
    if not note:
        raise ValueError("Say what the review found")
    event = db.get(SafetyEvent, event_id)
    if event is None:
        return None
    now = _now()
    line = f"{now:%Y-%m-%d %H:%M} {note}"
    event.review_note = f"{event.review_note}\n{line}" if event.review_note else line
    event.reviewed_at = event.reviewed_at or now
    _done(db, commit)
    return event


# Limitation Act 1980: a personal injury claim can come three years from the
# injury, and for a minor the clock starts at 18 (s.28), so until their 21st
# birthday.
SAFETY_RETENTION_YEARS = 3
MINOR_CLAIMS_UNTIL_AGE = 21
# A minor flag with no age to go on (the coach's own judgement, an account
# Forma closed as under 18 by hand) is kept as if the rider were the youngest
# who could plausibly be riding with Forma, so the records never go before a
# real child's claims run out.
MINOR_YOUNGEST_AGE = 13


def _years_after(when: datetime, years: int) -> datetime:
    try:
        return when.replace(year=when.year + years)
    except ValueError:
        # 29 February in a year without one: 1 March, never a day early.
        return when.replace(year=when.year + years, month=3, day=1)


def _event_stated_age(event) -> int | None:
    """The age on the record, or for a record written before stated_age was
    kept, the age the rider's own words give (read again by the detector)."""
    age = _get(event, "stated_age")
    if age is not None:
        return int(age)
    # Here, not at the top: safety_screen imports this module.
    from app.services import safety_screen

    for words in (_get(event, "rider_message"), _get(event, "matched")):
        if not words:
            continue
        try:
            found = safety_screen.stated_age_from(str(words))
        except Exception:
            found = None
        if found is not None:
            return int(found)
    return None


def minor_retention_until(event, deleted_at: datetime | None = None) -> datetime | None:
    """How long an under-18 flag's records must be kept (R20), from the age
    the rider told us (SafetyEvent.stated_age, or for an older record the age
    their own words give): the later of three years after the account goes
    and the 21st birthday that age implies. Someone who said 15 turns 21
    within six years of saying it, so counting whole years from the flag is
    never early. The birthday is kept whole, as the date-of-birth rule keeps
    it: the records go from the start of the day after.

    With no age to go on (the coach's own judgement, or an account Forma
    closed as under 18 by hand), the rider is taken to be MINOR_YOUNGEST_AGE
    (13) when flagged, so the records run to the 21st birthday that implies,
    eight years on, or three years after the account goes if that is later.

    None only for a record that isn't an under-18 flag. deleted_at defaults
    to the record's subject_deleted_at, or now, as the purge runs before
    stamping. Works on a SafetyEvent, a SafetyHold (the under-18 hold, dated
    from when it opened) or a dict."""
    kind = _get(event, "kind") or _get(event, "red_flag")
    if kind is not None and kind != "minor":
        return None
    age = _event_stated_age(event)
    if age is None:
        age = MINOR_YOUNGEST_AGE
    deleted = deleted_at or _get(event, "subject_deleted_at") or _now()
    said = _get(event, "created_at") or _get(event, "opened_at") or _now()
    birthday = _years_after(said, max(0, MINOR_CLAIMS_UNTIL_AGE - age))
    return max(
        _years_after(deleted, SAFETY_RETENTION_YEARS),
        datetime.combine(birthday.date() + timedelta(days=1), time.min),
    )


def minor_retention_end(
    db: Session, user: User | str, deleted_at: datetime | None = None
) -> datetime | None:
    """The latest minor_retention_until across everything that flagged this
    account as under 18: each under-18 red-flag event, and each under-18 hold
    no event points to (an account Forma closed by hand with --force, which
    writes the hold alone). None when nothing flagged it. For the purge, so a
    hold with no event behind it still keeps the records to the 21st
    birthday of the youngest rider it could be.

    A record that gives no age, on an account whose date of birth made the
    rider under 18 when it was made, goes by that date of birth instead of
    MINOR_YOUNGEST_AGE: the rider's own date is an age to go on, so their
    records are kept through their 21st birthday and no longer."""
    uid = _uid(user)
    person = user if isinstance(user, User) else db.get(User, uid)
    born = _birth_date(person)
    events = (
        db.query(SafetyEvent)
        .filter(SafetyEvent.user_id == uid, SafetyEvent.kind == "minor")
        .all()
    )
    pointed_to = {e.hold_id for e in events if e.hold_id}
    holds = [
        h for h in db.query(SafetyHold).filter(
            SafetyHold.user_id == uid, SafetyHold.red_flag == "minor"
        )
        if h.id not in pointed_to
    ]
    ends = []
    for record in (*events, *holds):
        said = _get(record, "created_at") or _get(record, "opened_at") or _now()
        if (
            born is not None
            and _under_18_on(born, said.date())
            and _event_stated_age(record) is None
        ):
            deleted = deleted_at or _get(record, "subject_deleted_at") or _now()
            ends.append(max(
                _years_after(deleted, SAFETY_RETENTION_YEARS),
                datetime.combine(
                    _years_after(born, MINOR_CLAIMS_UNTIL_AGE) + timedelta(days=1), time.min
                ),
            ))
            continue
        end = minor_retention_until(record, deleted_at)
        if end is not None:
            ends.append(end)
    return max(ends) if ends else None


# === What the app reads ===


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _hold_view(hold: SafetyHold) -> dict:
    return {
        "id": hold.id,
        "level": hold.level,
        "reason": hold.reason,
        "red_flag": hold.red_flag,
        "source": hold.source,
        "opened_at": _iso(hold.opened_at),
        "expires_at": _iso(hold.expires_at),
        "lift_kind": lift_kind(hold),
    }


def safety_state(db: Session, user: User | str) -> dict:
    """Everything the app needs to gate ride mode, the FTP test and the
    hold banner, in one read (GET /users/me/safety-state).

    hold is the one to show: an under-18 hold whenever there is one, else the
    most severe. hold.lift_kind tells the app which way out to offer.
    limits is every active limit a clinician set. no_racing_or_group_until
    is the first day racing or group riding is allowed again after a head
    injury, or None."""
    if isinstance(user, str):
        user = db.get(User, user)
    today = _today()
    hold = minor_hold(db, user.id) or current_hold(db, user.id)
    screening = latest_screening(db, user.id)
    gate = layoff_gate_until(db, user.id, today=today)
    return {
        "allowed": _combine(hold, screening, gate, today),
        "hold": _hold_view(hold) if hold is not None else None,
        "screening": (
            {
                "tier": screening.tier,
                "version": screening.version,
                "clearance_confirmed": screening.clearance_confirmed_at is not None,
                "limits": screening.clearance_limits,
            }
            if screening is not None
            else None
        ),
        "limits": [
            {
                "id": limit.id,
                "text": limit.limits,
                "by": limit.cleared_by,
                "recorded_at": _iso(limit.recorded_at),
            }
            for limit in active_limits(db, user.id)
        ],
        "layoff_gate_until": _iso(gate),
        "no_racing_or_group_until": _iso(no_racing_or_group_until(db, user.id, today=today)),
        "ride_mode_ack": user.ride_mode_ack_at is not None,
        "ftp": user.ftp,
        "erg_cap": ERG_CAP,
        "easy_cap": EASY_CAP,
        "ceilings": dict(INTENSITY_CEILING),
    }
