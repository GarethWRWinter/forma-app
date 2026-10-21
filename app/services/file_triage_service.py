"""What Forma recognises in a ride file shared in chat, before asking anything.

A rider drops a FIT, GPX or TCX file into the coach chat. Often the ride is
theirs and Forma has it already: an archive import, a Wahoo sync, the same
file sent last week. Asking "is this yours, shall I save it?" about a ride
that is already in Rides is the question that makes a coach sound like a
form. So at upload, with no model call, the file is checked against the
rider's saved rides and given one of three states:

in_history    the same bytes as a ride already saved from chat, or the same
              outing by find_duplicate_ride's rule (start within 2 minutes,
              duration within 5 percent or 60 s). Linked, and no question.
part_of_ride  at least half of the file's time sits inside one saved ride: a
              trimmed segment, or a second device on the same outing. The
              rider's own, and never saved again.
new           nothing matches. The card under the message asks.

A time match whose numbers clearly disagree with the saved ride over the
same seconds (heart rate, or power averaged over half a minute) is not the
same data. It is most often a riding partner's file from the same outing, so
it stays new, with that as a hint, and it cannot be saved: the time it
covers is in Rides already. A saved ride sitting mostly inside a new file
(the whole outing, where a stretch of it was saved before) is a hint too,
because saving the file would count that stretch twice. Hints shape how the
card asks and which choices it offers, never whose the file is.

The result lives in the attachment's summary JSON as "triage". Rides arrive
after a file does (an archive import, a Wahoo sync, another file saved from
chat), so filings() looks again at every file still open to a choice and
re-recognises the ones whose rides have moved, and a save always looks again
before it files anything. A ride deleted from Rides since stops being
linked, and card() turns all of it into what the app shows under the
message.
"""

import gzip
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from statistics import median

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, load_only

from app.models.chat_attachment import ChatAttachment
from app.models.ride import Ride, RideData
from app.models.user import User
from app.services.ride_service import find_duplicate_ride, parse_ride_file

logger = logging.getLogger(__name__)

STATES = ("in_history", "part_of_ride", "new")
DECISIONS = ("save", "chat_only", "someone_else")
LINKED = ("in_history", "part_of_ride")

# At least this share of the file's time inside one saved ride makes it part
# of that ride. Under the second figure some of it is a ride of its own, so
# the card may offer to keep it separately.
PART_OF_RIDE_OVERLAP = 0.5
SEPARATE_RIDE_BELOW = 0.9
# At least this share of a saved ride's own time inside the file: the file
# holds that ride, and saving it would count the shared minutes twice.
HOLDS_RIDE = 0.5

# Quiet hints, used only to phrase the question. The best power held for
# each length of effort, as a share of the rider's FTP, past which the file
# is probably someone else's: a shorter effort sits further above FTP, so
# its bar is higher. Longest first, so the line names the longest effort.
FASTER_THAN_FTP = {1200: 1.25, 480: 1.35, 300: 1.45}
HR_ABOVE_MAX_BPM = 8
# Heart rate held this long, not a single reading: a strap spike lasts a
# second or two.
HR_HELD_S = 30

# Two recordings of one outing by one rider agree second by second: one
# strap, one power meter, or two that read within a few percent. A riding
# partner can match the averages and still differ here.
HR_DIFFERS_BPM = 5  # median gap over the same seconds
POWER_DIFFERS = 0.08  # mean gap between 30 s averages, as a share of power
POWER_BUCKET_S = 30
# With only a saved ride's averages to go on, the bar is wider: averages
# with and without zeros alone can sit 10 percent apart.
AVG_POWER_DIFFERS = 0.15
AVG_HR_DIFFERS_BPM = 10
MIN_SAMPLES = 60

# The longest saved ride a file could sit inside, for the overlap search.
LONGEST_RIDE = timedelta(hours=36)


def blank_triage() -> dict:
    """A triage with every key present, so its shape never varies."""
    return {
        "state": "new",
        "matched": None,
        "ride_id": None,
        "ride_date": None,
        "ride_title": None,
        "overlap": None,
        "hints": {},
        # The saved rides that lined up with the file in time when it was
        # last recognised (_lines_up), so a change to them is noticed.
        "near": [],
        "decided": False,
        "decision": None,
        "label": None,
        "decided_at": None,
        "decided_by": None,
    }


# What the rider chose: kept whenever the file is recognised again.
CHOICE_KEYS = ("decided", "decision", "label", "decided_at", "decided_by")


def triage_of(attachment: ChatAttachment) -> dict:
    """The stored triage over the blank one. A file read before triage was
    kept has none and reads as new and undecided."""
    stored = (attachment.summary or {}).get("triage")
    triage = blank_triage()
    if isinstance(stored, dict):
        triage.update({k: v for k, v in stored.items() if k in triage})
    if triage["state"] not in STATES:
        triage["state"] = "new"
    if triage["decision"] not in DECISIONS:
        triage["decision"] = None
    if triage["decision"] is None and (attachment.summary or {}).get("owner") == "someone_else":
        # Recorded before choices were kept: someone else's is a choice.
        triage["decision"] = "someone_else"
    triage["decided"] = triage["decision"] is not None
    if not isinstance(triage["hints"], dict):
        triage["hints"] = {}
    if not isinstance(triage["near"], list):
        triage["near"] = []
    return triage


def own_ride_id(attachment: ChatAttachment, triage: dict | None = None) -> str | None:
    """The ride this upload was itself saved as. imported_ride_id also holds
    the link to a ride the file was recognised as, which is not a save."""
    t = triage or triage_of(attachment)
    if not attachment.imported_ride_id:
        return None
    if t["state"] == "in_history" and attachment.imported_ride_id == t["ride_id"]:
        return None
    return attachment.imported_ride_id


def ride_title(ride: Ride) -> str | None:
    """A ride's name as the Rides list shows it."""
    return ride.forma_title or ride.title


# ── Recognition ─────────────────────────────────────────────────────────────


def _naive_utc(value) -> datetime | None:
    """UTC without a zone, the way the rides table holds its times."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _file_window(summary: dict) -> tuple[datetime, datetime] | None:
    try:
        start = _naive_utc(datetime.fromisoformat(summary["start_time"]))
    except (KeyError, TypeError, ValueError):
        return None
    if start is None:
        return None
    return start, start + timedelta(seconds=int(summary.get("duration_s") or 0))


def _ride_window(ride: Ride) -> tuple[datetime, datetime] | None:
    start = _naive_utc(ride.ride_date)
    length = ride.duration_seconds or ride.moving_time_seconds
    if start is None or not length:
        return None
    return start, start + timedelta(seconds=int(length))


def _ride_ref(ride: Ride) -> dict:
    started = _naive_utc(ride.ride_date)
    return {
        "ride_id": ride.id,
        "ride_date": started.date().isoformat() if started else None,
        "ride_title": ride_title(ride),
    }


def _link(triage: dict, state: str, matched: str, ride: Ride) -> None:
    triage.update(state=state, matched=matched, **_ride_ref(ride))


def _holds_these_bytes(attachment: ChatAttachment) -> bool:
    """Whether the ride an upload is linked to holds the upload's own bytes:
    the upload was saved as it, or recognised as it by these same bytes. A
    link made by time is a different recording of the outing."""
    t = triage_of(attachment)
    if (attachment.summary or {}).get("owner") == "someone_else":
        return False
    if t["state"] == "in_history" and attachment.imported_ride_id == t["ride_id"]:
        return t["matched"] == "bytes"
    return True


def _saved_ride_with_same_bytes(db: Session, user_id: str, sha256: str | None) -> Ride | None:
    """A ride of the rider's that holds exactly these bytes, through an
    earlier upload of them. Rides keep no fingerprint of their own, so only a
    file saved or recognised from chat can match this way."""
    if not sha256:
        return None
    ride_ids = [
        a.imported_ride_id
        for a in (
            db.query(ChatAttachment)
            .options(load_only(
                ChatAttachment.summary, ChatAttachment.imported_ride_id,
                ChatAttachment.created_at,
            ))
            .filter(
                ChatAttachment.user_id == user_id,
                ChatAttachment.imported_ride_id.isnot(None),
                ChatAttachment.summary["sha256"].as_string() == sha256,
            )
            .order_by(ChatAttachment.created_at.desc(), ChatAttachment.id.desc())
        )
        if (a.summary or {}).get("sha256") == sha256 and _holds_these_bytes(a)
    ]
    for ride_id in dict.fromkeys(ride_ids):
        ride = db.query(Ride).filter(Ride.id == ride_id, Ride.user_id == user_id).first()
        if ride is not None:
            return ride
    return None


def _shared(window: tuple[datetime, datetime], ride: Ride) -> tuple[float, float] | None:
    """The time a file and a saved ride share, as a share of the file and as
    a share of the ride, or None when they share none."""
    ride_window = _ride_window(ride)
    if ride_window is None:
        return None
    seconds = (min(window[1], ride_window[1]) - max(window[0], ride_window[0])).total_seconds()
    file_s = (window[1] - window[0]).total_seconds()
    ride_s = (ride_window[1] - ride_window[0]).total_seconds()
    if seconds <= 0 or file_s <= 0 or ride_s <= 0:
        return None
    return min(seconds / file_s, 1.0), min(seconds / ride_s, 1.0)


def _sharing_time(
    db: Session, user_id: str, window: tuple[datetime, datetime]
) -> list[tuple[Ride, float, float]]:
    """Saved rides sharing time with the file: (ride, share of the file,
    share of the ride)."""
    found = []
    for ride in db.query(Ride).filter(
        Ride.user_id == user_id,
        Ride.ride_date <= window[1],
        Ride.ride_date >= window[0] - LONGEST_RIDE,
    ):
        shared = _shared(window, ride)
        if shared is not None:
            found.append((ride, *shared))
    return found


def _lines_up(window: tuple[datetime, datetime], ride: Ride, shared: tuple[float, float]) -> bool:
    """A saved ride recognition weighs: one starting within find_duplicate_
    ride's two minutes, holding half the file, or half inside it."""
    started = _naive_utc(ride.ride_date)
    starts = started is not None and abs((started - window[0]).total_seconds()) <= 120
    return starts or shared[0] >= PART_OF_RIDE_OVERLAP or shared[1] >= HOLDS_RIDE


def _by_second(samples, start: datetime, lo: datetime, hi: datetime) -> dict[int, tuple]:
    """(power, heart rate) by whole second from lo, for samples in [lo, hi].
    samples are (elapsed seconds from start, power, heart rate)."""
    base = (start - lo).total_seconds()
    span = (hi - lo).total_seconds()
    out: dict[int, tuple] = {}
    for elapsed, power, hr in samples:
        if elapsed is None:
            continue
        t = round(base + elapsed)
        if 0 <= t <= span:
            out[t] = (power, hr)
    return out


def _seconds_differ(mine: dict[int, tuple], theirs: dict[int, tuple]) -> bool | None:
    """True when the same seconds clearly disagree, False when a channel
    both hold agrees, None when too few shared seconds hold one to tell."""
    shared = mine.keys() & theirs.keys()
    told = None

    gaps = [
        abs(mine[t][1] - theirs[t][1])
        for t in shared
        if mine[t][1] is not None and theirs[t][1] is not None
    ]
    if len(gaps) >= MIN_SAMPLES:
        if median(gaps) > HR_DIFFERS_BPM:
            return True
        told = False

    # Power jumps second to second, and two head units can sit a second
    # apart, so it is compared as 30 second averages.
    buckets: dict[int, list] = {}
    count = 0
    for t in shared:
        a, b = mine[t][0], theirs[t][0]
        if a is None or b is None:
            continue
        bucket = buckets.setdefault(t // POWER_BUCKET_S, [0.0, 0.0, 0])
        bucket[0] += a
        bucket[1] += b
        bucket[2] += 1
        count += 1
    if count >= MIN_SAMPLES:
        means = [(a / n, b / n) for a, b, n in buckets.values()]
        level = max(sum(a for a, _ in means), sum(b for _, b in means)) / len(means)
        if level >= 50:
            gap = sum(abs(a - b) for a, b in means) / len(means)
            if gap / level > POWER_DIFFERS:
                return True
            told = False
    return told


def _averages_differ(a: dict, b: dict) -> bool:
    """For a saved ride with no per-second data: whole-ride averages."""
    if a["power"] and b["power"]:
        top = max(a["power"], b["power"])
        if top >= 50 and abs(a["power"] - b["power"]) / top > AVG_POWER_DIFFERS:
            return True
    if a["hr"] and b["hr"] and abs(a["hr"] - b["hr"]) > AVG_HR_DIFFERS_BPM:
        return True
    return False


def _numbers_disagree(
    db: Session, ride: Ride, records: list[dict], summary: dict,
    window: tuple[datetime, datetime], whole_ride: bool,
) -> bool:
    """Whether the file and a saved ride it lines up with in time hold
    clearly different numbers over the seconds they share. Unknown counts as
    agreeing: the time match stands unless the data says otherwise."""
    ride_window = _ride_window(ride)
    if ride_window is None:
        return False
    lo, hi = max(window[0], ride_window[0]), min(window[1], ride_window[1])
    if hi <= lo:
        return False
    first = int((lo - ride_window[0]).total_seconds())
    last = int((hi - ride_window[0]).total_seconds())
    rows = (
        db.query(RideData.elapsed_seconds, RideData.power, RideData.heart_rate)
        .filter(
            RideData.ride_id == ride.id,
            RideData.elapsed_seconds >= first,
            RideData.elapsed_seconds <= last,
        )
        .all()
    )
    if rows:
        theirs = _by_second(rows, ride_window[0], lo, hi)
        mine = _by_second(
            ((r.get("elapsed_seconds"), r.get("power"), r.get("heart_rate")) for r in records),
            window[0], lo, hi,
        )
        return bool(_seconds_differ(mine, theirs))
    if not whole_ride:
        # A stretch of a ride cannot be held against the whole ride's averages.
        return False
    return _averages_differ(
        {"power": summary.get("avg_power"), "hr": summary.get("avg_hr")},
        {"power": ride.average_power, "hr": ride.average_hr},
    )


def _faster(analysis: dict, ftp: int | None) -> dict | None:
    if not ftp:
        return None
    curve = {e.get("duration_s"): e.get("watts") for e in (analysis or {}).get("power_curve") or []}
    for duration, share in FASTER_THAN_FTP.items():
        watts = curve.get(duration)
        if watts and watts > share * ftp:
            return {"duration_s": duration, "watts": watts, "ftp": ftp}
    return None


def _held_hr(records: list[dict]) -> int | None:
    """The highest heart rate held for HR_HELD_S seconds, from the file's
    own samples. None without timed heart rate."""
    points = [
        (r["elapsed_seconds"], r["heart_rate"])
        for r in records
        if r.get("elapsed_seconds") is not None and r.get("heart_rate") is not None
    ]
    if not points:
        return None
    best = None
    total, lo = 0, 0
    for hi, (t, hr) in enumerate(points):
        total += hr
        while points[lo][0] <= t - HR_HELD_S:
            total -= points[lo][1]
            lo += 1
        # Only a full window counts, and only one with readings through it:
        # a gap in the recording is not a held effort.
        if t - points[0][0] >= HR_HELD_S - 1 and (hi - lo + 1) * 5 >= HR_HELD_S:
            avg = total / (hi - lo + 1)
            best = avg if best is None or avg > best else best
    return round(best) if best is not None else None


def _hints(
    db: Session, user: User, summary: dict, analysis: dict, records: list[dict],
    window: tuple[datetime, datetime] | None,
    alongside: Ride | None, holds: tuple[Ride, float] | None,
) -> dict:
    hints: dict = {}
    faster = _faster(analysis, user.ftp)
    if faster:
        hints["faster"] = faster
    held = _held_hr(records)
    if held and user.max_hr and held > user.max_hr + HR_ABOVE_MAX_BPM:
        hints["high_hr"] = {"max_hr": held, "held_s": HR_HELD_S, "rider_max_hr": user.max_hr}
    if alongside is not None:
        hints["alongside"] = _ride_ref(alongside)
    if holds is not None:
        ride, share = holds
        hints["holds"] = {**_ride_ref(ride), "share_of_ride": round(share, 2)}
    has_hr = bool(summary.get("avg_hr") or summary.get("max_hr") or (analysis or {}).get("hr"))
    if not summary.get("has_power") and not has_hr:
        hints["route"] = {}
    if window:
        earliest = _naive_utc(
            db.query(func.min(Ride.ride_date)).filter(Ride.user_id == user.id).scalar()
        )
        if earliest and window[0].date() < earliest.date():
            hints["before_first_ride"] = {"first_ride_date": earliest.date().isoformat()}
    return hints


def recognise(
    db: Session, user: User, summary: dict, analysis: dict, records: list[dict]
) -> dict:
    """The file against the rider's saved rides as they are now.
    Deterministic, and no model call. Returns a fresh triage, undecided."""
    triage = blank_triage()
    window = _file_window(summary)
    alongside: Ride | None = None
    holds: tuple[Ride, float] | None = None

    ride = _saved_ride_with_same_bytes(db, user.id, summary.get("sha256"))
    if ride is not None:
        _link(triage, "in_history", "bytes", ride)
    elif window is not None:
        same = find_duplicate_ride(
            db, user.id, window[0].replace(tzinfo=timezone.utc), summary.get("duration_s")
        )
        if same is not None and _numbers_disagree(db, same, records, summary, window, True):
            alongside, same = same, None
        if same is not None:
            _link(triage, "in_history", "time", same)
        sharing = _sharing_time(db, user.id, window)
        triage["near"] = sorted(r.id for r, *shared in sharing if _lines_up(window, r, shared))
        if same is None:
            near = [n for n in sharing if alongside is None or n[0].id != alongside.id]
            for candidate, of_file, _ in sorted(near, key=lambda n: (-n[1], n[0].id)):
                if of_file < PART_OF_RIDE_OVERLAP:
                    break
                if _numbers_disagree(db, candidate, records, summary, window, False):
                    alongside = alongside or candidate
                    continue
                _link(triage, "part_of_ride", "overlap", candidate)
                triage["overlap"] = round(of_file, 2)
                break
            if triage["state"] == "new":
                # A saved ride mostly inside the file: the file is the
                # whole outing, and the saved ride a stretch of it.
                for candidate, of_file, of_ride in sorted(near, key=lambda n: (-n[2], n[0].id)):
                    if of_ride < HOLDS_RIDE:
                        break
                    if of_file >= PART_OF_RIDE_OVERLAP:
                        continue  # weighed above, and its numbers disagree
                    if _numbers_disagree(db, candidate, records, summary, window, False):
                        alongside = alongside or candidate
                        continue
                    holds = (candidate, of_ride)
                    break

    new = triage["state"] == "new"
    triage["hints"] = _hints(
        db, user, summary, analysis, records, window,
        alongside if new else None, holds if new else None,
    )
    return triage


# ── Looking again ───────────────────────────────────────────────────────────


def _records(attachment: ChatAttachment) -> list[dict]:
    """The file's samples, read again from the bytes kept with it."""
    raw = attachment.raw_file
    if not raw:
        return []
    try:
        return parse_ride_file(gzip.decompress(raw), attachment.filename).get("records") or []
    except Exception:
        logger.warning("File triage: could not read attachment %s again", attachment.id)
        return []


def _apply(attachment: ChatAttachment, fresh: dict) -> None:
    """A fresh recognition onto one upload, keeping what the rider chose and
    the ride the upload was saved as."""
    old = triage_of(attachment)
    triage = {**fresh, **{k: old[k] for k in CHOICE_KEYS}}
    summary = {**(attachment.summary or {})}
    own = own_ride_id(attachment, old)
    if own is not None and triage["ride_id"] == own:
        # It matched the ride it was saved as, which is its own save, not a
        # ride it was recognised as (a file saved before triage was kept).
        blank = blank_triage()
        for key in ("state", "matched", "ride_id", "ride_date", "ride_title", "overlap"):
            triage[key] = blank[key]
    theirs = old["decision"] == "someone_else" or summary.get("owner") == "someone_else"
    if not theirs and triage["state"] != "new" and summary.get("owner") in (None, "unknown"):
        # A ride already in Rides, or a stretch of one, is the rider's own.
        summary["owner"] = "self"
    if own is None:
        if triage["state"] == "in_history" and summary.get("owner") == "self":
            attachment.imported_ride_id = triage["ride_id"]
        else:
            # A link to a ride the file no longer matches.
            attachment.imported_ride_id = None
    summary["triage"] = triage
    # A new dict, because the JSON column does not notice one changed in place.
    attachment.summary = summary


def refresh(
    db: Session, user: User, attachment: ChatAttachment,
    copies: list[ChatAttachment] | None = None,
) -> None:
    """Recognise a file again against the rides as they are now, on it and
    on every copy given (the same file, so the same answer), and commit."""
    fresh = recognise(
        db, user, attachment.summary or {}, attachment.analysis or {}, _records(attachment)
    )
    for a in copies or [attachment]:
        _apply(a, fresh)
    db.commit()


def _open_to_change(attachment: ChatAttachment, t: dict) -> bool:
    """A file whose recognition rides added since could change: one still
    asking, or kept for the chat, and never saved as a ride of its own."""
    return (
        t["state"] == "new"
        and t["decision"] in (None, "chat_only")
        and own_ride_id(attachment, t) is None
        and _file_window(attachment.summary or {}) is not None
    )


def _look_again(db: Session, user_id: str, attachments: list[ChatAttachment]) -> None:
    """Re-recognise the files whose rides have moved since they were read:
    a ride now lines up that did not, or one that did is gone. One
    query for the rides of every file open to change. A file read before
    triage was kept is recognised now, once."""
    unchecked = [a for a in attachments if "triage" not in (a.summary or {})]
    open_ = [
        a for a in attachments
        if "triage" in (a.summary or {}) and _open_to_change(a, triage_of(a))
    ]

    moved: list[ChatAttachment] = []
    if open_:
        windows = {a.id: _file_window(a.summary or {}) for a in open_}
        rides = (
            db.query(Ride)
            .options(load_only(
                Ride.id, Ride.ride_date, Ride.duration_seconds, Ride.moving_time_seconds,
            ))
            .filter(
                Ride.user_id == user_id,
                or_(*(
                    and_(Ride.ride_date <= w[1], Ride.ride_date >= w[0] - LONGEST_RIDE)
                    for w in windows.values()
                )),
            )
            .all()
        )
        for a in open_:
            window = windows[a.id]
            now = {
                ride.id for ride in rides
                if (shared := _shared(window, ride)) is not None
                and _lines_up(window, ride, shared)
            }
            if now != set(triage_of(a)["near"]):
                moved.append(a)

    if not (unchecked or moved):
        return
    user = db.get(User, user_id)
    if user is None:
        return
    for a in unchecked + moved:
        try:
            refresh(db, user, a)
        except Exception:
            logger.exception("File triage: looking again failed for attachment %s", a.id)
            db.rollback()


# ── Reading it back ─────────────────────────────────────────────────────────


@dataclass
class Filing:
    """A file's triage read against the rides as they are now."""

    attachment: ChatAttachment
    # in_history, part_of_ride or new. Linked states fall back to new when
    # their ride has been deleted from Rides since.
    state: str
    # The saved ride the file matched (in_history, part_of_ride).
    ride: Ride | None
    # The ride the file itself was saved as, while it is still in Rides.
    saved: Ride | None
    decision: str | None
    owner: str
    triage: dict = field(default_factory=dict)
    # The ride it was recognised as has been deleted from Rides since, and
    # the rider chose nothing: just for this chat.
    lost_match: bool = False

    @property
    def overlap(self) -> float | None:
        return self.triage.get("overlap") if self.state == "part_of_ride" else None

    @property
    def label(self) -> str | None:
        return self.triage.get("label") if self.decision == "someone_else" else None

    @property
    def hints(self) -> dict:
        return self.triage.get("hints") or {}

    @property
    def view(self) -> str:
        """What the card shows: asking, in_history, part_of_ride, saved,
        chat_only or someone_else."""
        if self.decision == "someone_else":
            return "someone_else"
        if self.saved is not None:
            return "saved"
        if self.state in LINKED:
            return self.state
        if self.decision == "chat_only":
            return "chat_only"
        return "asking"

    @property
    def card_status(self) -> str:
        """For the coach: whether the file's card is still asking."""
        if self.view == "asking":
            return "asking"
        if self.decision is not None or self.view == "saved":
            return "answered"
        return "nothing to ask"

    @property
    def can_keep_separately(self) -> bool:
        return self.state == "part_of_ride" and (self.overlap or 0) < SEPARATE_RIDE_BELOW

    @property
    def can_save(self) -> bool:
        """Whether a save could file it at all: it needs times to sit on a
        date, and a file recorded at the same time as a ride already in
        Rides covers time that is filed already."""
        timed = bool((self.attachment.summary or {}).get("start_time"))
        return timed and "alongside" not in self.hints


def filings(db: Session, user_id: str, attachments: list[ChatAttachment]) -> dict[str, Filing]:
    """Each file's filing by attachment id, after looking again at the ones
    rides added since could change, with one query for the rides."""
    from app.services.attachment_service import file_owner

    _look_again(db, user_id, attachments)
    triages = {a.id: triage_of(a) for a in attachments}
    ride_ids = {t["ride_id"] for t in triages.values() if t["ride_id"]}
    ride_ids |= {a.imported_ride_id for a in attachments if a.imported_ride_id}
    rides = {
        r.id: r
        for r in (
            db.query(Ride).filter(Ride.user_id == user_id, Ride.id.in_(ride_ids)).all()
            if ride_ids else []
        )
    }

    out: dict[str, Filing] = {}
    for a in attachments:
        t = triages[a.id]
        linked = t["state"] in LINKED
        ride = rides.get(t["ride_id"]) if linked else None
        state = t["state"] if ride is not None else "new"
        decision = t["decision"]
        owner = file_owner(a)
        lost_match = False

        # The file's own save. One made before triage was kept left only
        # imported_ride_id behind; a recognised file's link is not a save.
        own = own_ride_id(a, t)
        saved = rides.get(own) if own else None
        if decision != "someone_else":
            if saved is not None:
                decision = "save"
            elif decision == "save" and state != "in_history":
                # Saved, then deleted from Rides: the rider's own, read here
                # only.
                decision = "chat_only"
            elif linked and ride is None and decision is None:
                decision, lost_match = "chat_only", True
        # Recorded as someone else's after a save, the ride stays in Rides
        # until the rider deletes it there, and the card says so (_note).
        out[a.id] = Filing(a, state, ride, saved, decision, owner, t, lost_match)
    return out


def filing(db: Session, user_id: str, attachment: ChatAttachment) -> Filing:
    return filings(db, user_id, [attachment])[attachment.id]


def link_in_history(attachment: ChatAttachment, ride: Ride) -> None:
    """File an upload as the ride already in Rides that a save found: the
    rider's own and linked, with no save of its own recorded."""
    triage = {**triage_of(attachment), **{k: blank_triage()[k] for k in CHOICE_KEYS}}
    _link(triage, "in_history", "time", ride)
    triage["overlap"] = None
    triage["hints"] = {
        k: v for k, v in triage["hints"].items() if k not in ("alongside", "holds")
    }
    attachment.imported_ride_id = ride.id
    attachment.summary = {**(attachment.summary or {}), "owner": "self", "triage": triage}


# ── The card ────────────────────────────────────────────────────────────────

NBSP = " "


def _hold(text: str | None) -> str | None:
    """The last two words joined, so a short line never ends on one word."""
    if not text:
        return text
    head, space, tail = text.rstrip().rpartition(" ")
    return f"{head}{NBSP}{tail}" if space and head else text


def _day(value) -> str | None:
    """A date as the app says it: 3 May 2026, held together so a card line
    never breaks inside it."""
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError:
            return None
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        return None
    return f"{value.day}{NBSP}{value:%b}{NBSP}{value.year}"


def _ride_name(title: str | None, when) -> str:
    day = _day(when)
    if title:
        return f"{title}, {day}" if day else title
    return f"your ride of {day}" if day else "a ride of yours"


def _named(ride: Ride) -> str:
    return _ride_name(ride_title(ride), ride.ride_date)


def _hinted(hint: dict) -> str:
    return _ride_name(hint.get("ride_title"), hint.get("ride_date"))


def shown_label(label: str | None) -> str | None:
    """The rider's name for someone else's file as the card writes it: curly
    apostrophes, and a capital to start."""
    if not label:
        return None
    text = label.replace("'", "\u2019")
    return text[:1].upper() + text[1:]


CHOICE_LABELS = {
    "save": "Mine: save to my rides",
    "chat_only": "Mine: just for this chat",
    "someone_else": "Someone else\u2019s",
}
# What each choice does, under it, so no one choice carries more weight.
CHOICE_DETAILS = {
    "chat_only": "It stays out of Rides and your fitness chart.",
    "someone_else": "For comparison only, and only in this chat.",
}


def _choice(choice: str, label: str | None = None, detail: str | None = None) -> dict:
    return {
        "choice": choice,
        "label": _hold(label or CHOICE_LABELS[choice]),
        "detail": _hold(detail),
        "asks_label": choice == "someone_else",
    }


def _recognised(f: Filing) -> str:
    """One line of what Forma recognised."""
    t = f.triage
    hints = f.hints
    if f.decision == "someone_else" and f.ride is not None and t.get("matched") != "bytes":
        # Lined up with a ride of the rider's, and said to be another
        # person's: a ride they did together, not the rider's own.
        return f"Recorded at the same time as {_named(f.ride)}."
    if f.state == "in_history":
        return f"Already in your rides: {_named(f.ride)}."
    if f.state == "part_of_ride":
        if f.can_keep_separately:
            share = round(f.overlap * 100)
            return f"{share}% of it overlaps {_named(f.ride)}, already in your rides."
        return f"Part of {_named(f.ride)}, already in your rides."
    if t["state"] in LINKED:
        return "The ride it matched is no longer in your rides."
    if "alongside" in hints:
        named = _hinted(hints["alongside"])
        return f"Recorded at the same time as {named}, but the numbers differ."
    # Once the rider has said it is theirs, Forma stops wondering aloud.
    wonders = f.owner != "self"
    if "faster" in hints and wonders:
        h = hints["faster"]
        watts = h.get("watts") or h.get("best_20min_w")
        minutes = (h.get("duration_s") or 1200) // 60
        return f"Its best {minutes} minutes is {watts} W, well above your FTP of {h['ftp']} W."
    if "high_hr" in hints and wonders:
        h = hints["high_hr"]
        if h.get("held_s"):
            return (
                f"Its heart rate holds {h['max_hr']} bpm for {h['held_s']} seconds, above "
                f"your max of {h['rider_max_hr']} bpm."
            )
        return (
            f"Its heart rate reaches {h['max_hr']} bpm, above your max of "
            f"{h['rider_max_hr']} bpm."
        )
    if "holds" in hints:
        return f"It includes {_hinted(hints['holds'])}, already in your rides."
    if "route" in hints:
        return "No power or heart rate in it, so it reads like a route or a GPS trace."
    if "before_first_ride" in hints:
        day = _day((f.attachment.summary or {}).get("start_time"))
        return f"From {day}, older than anything in your rides." if day else "Older than anything in your rides."
    return "Nothing in your rides matches it."


def _question(f: Filing) -> str | None:
    if f.view != "asking":
        return None
    hints = f.hints
    if not f.can_save or ({"faster", "high_hr"} & set(hints) and f.owner != "self"):
        return "Yours, or someone else\u2019s?"
    if f.owner == "self":
        return "Should it join your rides?"
    if "before_first_ride" in hints:
        return "An old ride of yours?"
    return "Whose ride is this?"


def _save_detail(f: Filing) -> str:
    summary = f.attachment.summary or {}
    day = _day(summary.get("start_time"))
    if f.state == "part_of_ride" or {"holds", "alongside"} & set(f.hints):
        return (
            "It joins Rides on its own, so the minutes it shares with that ride "
            "count twice in your fitness chart."
        )
    if not summary.get("has_power"):
        # The fitness chart counts training stress, which needs power.
        return (
            f"It joins Rides for {day}. With no power in it, your fitness chart "
            "won\u2019t move."
        )
    return f"It joins Rides and your fitness chart for {day}."


def _choices(f: Filing) -> tuple[list[dict], list[dict]]:
    """The card's buttons, and the quiet ways to change a choice."""
    view = f.view
    if view == "asking":
        # All three while it asks, even once the coach has heard it is the
        # rider's, so a misread is corrected here. A save only where one can
        # file it (Filing.can_save).
        return [
            _choice(c, detail=_save_detail(f) if c == "save" else CHOICE_DETAILS.get(c))
            for c in DECISIONS
            if c != "save" or f.can_save
        ], []
    if view == "part_of_ride":
        # Nothing to ask: the ride is in Rides. Keeping it apart is a quiet
        # option, offered only while some of it is a ride of its own.
        keep = (
            [_choice("save", "Keep as a separate ride", _save_detail(f))]
            if f.can_keep_separately else []
        )
        return [], keep + [_choice("someone_else", "Not my ride")]
    if view == "in_history":
        # The same bytes are the saved ride itself; a time match could be a
        # riding partner's file from the same outing.
        mine_by_time = f.triage.get("matched") != "bytes"
        return [], [_choice("someone_else", "Not my ride")] if mine_by_time else []
    if view == "chat_only":
        save = [_choice("save", "Save to my rides instead", _save_detail(f))] if f.can_save else []
        return [], save + [_choice("someone_else", detail=CHOICE_DETAILS["someone_else"])]
    if view == "someone_else":
        return [], [_choice("chat_only", "It\u2019s mine")]
    return [], []


def _outcome(f: Filing) -> str | None:
    if f.view == "saved":
        return "Saved to Rides."
    if f.view == "chat_only":
        if f.lost_match:
            return (
                "The ride it matched is no longer in your rides, so it\u2019s just "
                "for this chat."
            )
        return "Just for this chat. Not in your rides."
    if f.view == "someone_else":
        named = shown_label(f.label) or "Someone else\u2019s ride"
        return f"{named}, for comparison only."
    return None


def _note(f: Filing) -> str | None:
    if f.view != "someone_else":
        return None
    if f.saved is not None:
        return "A saved copy is still in Rides. Delete it there if it isn\u2019t yours."
    if f.triage.get("matched") == "bytes" and f.ride is not None:
        return "A ride with the same data is in your rides. Delete it there if it isn\u2019t yours."
    return None


def _card_ride(ride: Ride | None) -> dict | None:
    if ride is None:
        return None
    started = _naive_utc(ride.ride_date)
    return {
        "id": ride.id,
        "title": ride_title(ride),
        "date": started.date().isoformat() if started else None,
    }


def file_key(attachment: ChatAttachment) -> str:
    """Short and opaque: two uploads of one file share it, so the app shows
    one card for both and knows which cards a choice reaches."""
    from app.services.attachment_service import same_file_key

    key = json.dumps(same_file_key(attachment), default=str)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def card(f: Filing) -> dict:
    """The file card the app shows under the message that shared the file."""
    a = f.attachment
    summary = a.summary or {}
    started = summary.get("start_time")
    choices, changes = _choices(f)
    # The ride to link to: the one it was saved as, else the one it matched.
    linked = f.saved or (f.ride if f.view in LINKED else None)
    if f.view == "someone_else" and f.triage.get("matched") == "bytes":
        linked = f.ride
    return {
        "id": a.id,
        "file_key": file_key(a),
        "filename": a.filename,
        "kind": a.kind,
        "date": started[:10] if isinstance(started, str) else None,
        "start_time": started,
        # Without times a file has no length of its own: its samples are
        # points along a route, not seconds.
        "duration": summary.get("duration") if started else None,
        "duration_s": summary.get("duration_s") if started else None,
        "distance_km": summary.get("distance_km"),
        "state": f.state,
        "view": f.view,
        "recognised": _hold(_recognised(f)),
        "question": _hold(_question(f)),
        "outcome": _hold(_outcome(f)),
        "note": _hold(_note(f)),
        "undo": _hold("To undo it, delete the ride from Rides.") if f.view == "saved" else None,
        "ride": _card_ride(linked),
        "overlap": f.overlap,
        "hints": sorted(f.hints),
        "decided": f.decision is not None,
        "decision": f.decision,
        "label": shown_label(f.label),
        "decided_at": f.triage.get("decided_at") if f.triage.get("decision") else None,
        "choices": choices,
        "changes": changes,
    }


def account_held(db: Session, user_id: str) -> bool:
    """Whether the account is held as possibly under 18: nothing about the
    rider is written while it is, so no file is saved and no card offers a
    choice. A failed read counts as held."""
    from app.services.plan_review_service import minor_hold_open

    try:
        return minor_hold_open(db, user_id)
    except Exception:
        logger.exception("Checking for an under-18 hold failed (user=%s)", user_id)
        return True


def cards(db: Session, user_id: str, attachments: list[ChatAttachment]) -> list[dict]:
    """Cards for several files, in the order given. On a held account
    (account_held) a card says what the file is and asks nothing."""
    found = filings(db, user_id, attachments)
    shown = [card(found[a.id]) for a in attachments]
    if shown and account_held(db, user_id):
        for c in shown:
            c.update(question=None, choices=[], changes=[])
    return shown
