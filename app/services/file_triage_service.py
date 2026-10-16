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
same minutes (average power or heart rate) is not the same data. It is most
often a riding partner's file from the same outing, so it stays new, with
that as a hint. Hints only shape how the card asks, never what happens.

The result lives in the attachment's summary JSON as "triage". filing()
reads it back against the rides as they are now, so a ride deleted from
Rides since stops being linked, and card() turns that into what the app
shows under the message.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session, load_only

from app.models.chat_attachment import ChatAttachment
from app.models.ride import Ride, RideData
from app.models.user import User
from app.services.ride_service import find_duplicate_ride

STATES = ("in_history", "part_of_ride", "new")
DECISIONS = ("save", "chat_only", "someone_else")

# At least this share of the file's time inside one saved ride makes it part
# of that ride. Under the second figure some of it is a ride of its own, so
# the card may offer to keep it separately.
PART_OF_RIDE_OVERLAP = 0.5
SEPARATE_RIDE_BELOW = 0.9

# Quiet hints, used only to phrase the question.
FASTER_THAN_FTP = 1.25
HR_ABOVE_MAX_BPM = 8

# Two recordings of one outing by one rider agree on these; a riding
# partner's file from the same outing usually does not.
POWER_DISAGREES = 0.15
HR_DISAGREES_BPM = 10
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
        "decided": False,
        "decision": None,
        "label": None,
        "decided_at": None,
        "decided_by": None,
    }


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
    return triage


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


def _link(triage: dict, state: str, matched: str, ride: Ride) -> None:
    started = _naive_utc(ride.ride_date)
    triage.update(
        state=state,
        matched=matched,
        ride_id=ride.id,
        ride_date=started.date().isoformat() if started else None,
        ride_title=ride.title,
    )


def _saved_ride_with_same_bytes(db: Session, user_id: str, sha256: str | None) -> Ride | None:
    """A ride of the rider's that one of their earlier uploads of these exact
    bytes is linked to. Rides keep no fingerprint of their own, so only a
    file saved or recognised from chat can match this way."""
    if not sha256:
        return None
    ride_ids = [
        a.imported_ride_id
        for a in (
            db.query(ChatAttachment)
            .options(load_only(ChatAttachment.summary, ChatAttachment.imported_ride_id))
            .filter(ChatAttachment.user_id == user_id, ChatAttachment.imported_ride_id.isnot(None))
            .order_by(ChatAttachment.created_at.desc(), ChatAttachment.id.desc())
        )
        if (a.summary or {}).get("sha256") == sha256
        and (a.summary or {}).get("owner") != "someone_else"
    ]
    for ride_id in dict.fromkeys(ride_ids):
        ride = db.query(Ride).filter(Ride.id == ride_id, Ride.user_id == user_id).first()
        if ride is not None:
            return ride
    return None


def _overlapping_rides(
    db: Session, user_id: str, window: tuple[datetime, datetime]
) -> list[tuple[Ride, float]]:
    """Saved rides holding at least PART_OF_RIDE_OVERLAP of the file's time,
    the biggest share first."""
    start, end = window
    length = (end - start).total_seconds()
    if length <= 0:
        return []
    found = []
    for ride in (
        db.query(Ride)
        .filter(
            Ride.user_id == user_id,
            Ride.ride_date <= end,
            Ride.ride_date >= start - LONGEST_RIDE,
        )
    ):
        ride_window = _ride_window(ride)
        if ride_window is None:
            continue
        shared = (min(end, ride_window[1]) - max(start, ride_window[0])).total_seconds()
        share = shared / length
        if share >= PART_OF_RIDE_OVERLAP:
            found.append((ride, min(share, 1.0)))
    found.sort(key=lambda pair: (-pair[1], pair[0].id))
    return found


def _averages(power: list, hr: list) -> dict:
    return {
        "power": sum(power) / len(power) if power else None,
        "power_n": len(power),
        "hr": sum(hr) / len(hr) if hr else None,
        "hr_n": len(hr),
    }


def _file_averages(records: list[dict], file_start: datetime, lo: datetime, hi: datetime) -> dict:
    first = (lo - file_start).total_seconds()
    last = (hi - file_start).total_seconds()
    inside = [
        r for r in records
        if r.get("elapsed_seconds") is not None and first <= r["elapsed_seconds"] <= last
    ]
    return _averages(
        [r["power"] for r in inside if r.get("power") is not None],
        [r["heart_rate"] for r in inside if r.get("heart_rate") is not None],
    )


def _ride_averages(db: Session, ride: Ride, ride_start: datetime, lo: datetime, hi: datetime) -> dict:
    avg_power, power_n, avg_hr, hr_n = (
        db.query(
            func.avg(RideData.power), func.count(RideData.power),
            func.avg(RideData.heart_rate), func.count(RideData.heart_rate),
        )
        .filter(
            RideData.ride_id == ride.id,
            RideData.elapsed_seconds >= int((lo - ride_start).total_seconds()),
            RideData.elapsed_seconds <= int((hi - ride_start).total_seconds()),
        )
        .one()
    )
    return {
        "power": float(avg_power) if avg_power is not None else None,
        "power_n": power_n or 0,
        "hr": float(avg_hr) if avg_hr is not None else None,
        "hr_n": hr_n or 0,
    }


def _differ(a: dict, b: dict) -> bool:
    """True only when a channel both recordings hold clearly disagrees."""
    if (
        a["power_n"] >= MIN_SAMPLES and b["power_n"] >= MIN_SAMPLES
        and a["power"] is not None and b["power"] is not None
    ):
        top = max(a["power"], b["power"])
        if top >= 50 and abs(a["power"] - b["power"]) / top > POWER_DISAGREES:
            return True
    if (
        a["hr_n"] >= MIN_SAMPLES and b["hr_n"] >= MIN_SAMPLES
        and a["hr"] is not None and b["hr"] is not None
        and abs(a["hr"] - b["hr"]) > HR_DISAGREES_BPM
    ):
        return True
    return False


def _numbers_disagree(
    db: Session, ride: Ride, records: list[dict], summary: dict,
    window: tuple[datetime, datetime], whole_ride: bool,
) -> bool:
    """Whether the file and a saved ride it lines up with in time hold
    clearly different numbers over the minutes they share. Unknown counts
    as agreeing: the time match stands unless the data says otherwise."""
    ride_window = _ride_window(ride)
    if ride_window is None:
        return False
    lo, hi = max(window[0], ride_window[0]), min(window[1], ride_window[1])
    if hi <= lo:
        return False
    theirs = _ride_averages(db, ride, ride_window[0], lo, hi)
    if theirs["power_n"] or theirs["hr_n"]:
        return _differ(_file_averages(records, window[0], lo, hi), theirs)
    if not whole_ride:
        # A stretch of a ride cannot be held against the whole ride's averages.
        return False
    enough = MIN_SAMPLES
    whole = {
        "power": ride.average_power, "power_n": enough if ride.average_power else 0,
        "hr": ride.average_hr, "hr_n": enough if ride.average_hr else 0,
    }
    mine = {
        "power": summary.get("avg_power"), "power_n": enough if summary.get("avg_power") else 0,
        "hr": summary.get("avg_hr"), "hr_n": enough if summary.get("avg_hr") else 0,
    }
    return _differ(mine, whole)


def _best_20_minutes(analysis: dict) -> int | None:
    for effort in (analysis or {}).get("power_curve") or []:
        if effort.get("duration_s") == 1200:
            return effort.get("watts")
    return None


def _hints(
    db: Session, user: User, summary: dict, analysis: dict,
    window: tuple[datetime, datetime] | None, alongside: Ride | None,
) -> dict:
    hints: dict = {}
    best = _best_20_minutes(analysis)
    if best and user.ftp and best > FASTER_THAN_FTP * user.ftp:
        hints["faster"] = {"best_20min_w": best, "ftp": user.ftp}
    peak_hr = summary.get("max_hr") or ((analysis or {}).get("hr") or {}).get("max")
    if peak_hr and user.max_hr and peak_hr > user.max_hr + HR_ABOVE_MAX_BPM:
        hints["high_hr"] = {"max_hr": peak_hr, "rider_max_hr": user.max_hr}
    if alongside is not None:
        ref = blank_triage()
        _link(ref, "new", "time", alongside)
        hints["alongside"] = {k: ref[k] for k in ("ride_id", "ride_date", "ride_title")}
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
    """The file against the rider's saved rides, at upload. Deterministic, and
    no model call. Returns a fresh triage, undecided."""
    triage = blank_triage()
    window = _file_window(summary)
    alongside: Ride | None = None

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
        else:
            for candidate, share in _overlapping_rides(db, user.id, window):
                if alongside is not None and candidate.id == alongside.id:
                    continue
                if _numbers_disagree(db, candidate, records, summary, window, False):
                    alongside = alongside or candidate
                    continue
                _link(triage, "part_of_ride", "overlap", candidate)
                triage["overlap"] = round(share, 2)
                break

    triage["hints"] = _hints(
        db, user, summary, analysis, window,
        alongside if triage["state"] == "new" else None,
    )
    return triage


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

    @property
    def overlap(self) -> float | None:
        return self.triage.get("overlap") if self.state == "part_of_ride" else None

    @property
    def label(self) -> str | None:
        return self.triage.get("label") if self.decision == "someone_else" else None

    @property
    def view(self) -> str:
        """What the card shows: asking, in_history, part_of_ride, saved,
        chat_only or someone_else."""
        if self.decision == "someone_else":
            return "someone_else"
        if self.saved is not None:
            return "saved"
        if self.state in ("in_history", "part_of_ride"):
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


def filings(db: Session, user_id: str, attachments: list[ChatAttachment]) -> dict[str, Filing]:
    """Each file's filing by attachment id, with one query for the rides."""
    from app.services.attachment_service import file_owner

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
        linked = t["state"] in ("in_history", "part_of_ride")
        ride = rides.get(t["ride_id"]) if linked else None
        state = t["state"] if ride is not None else "new"
        decision = t["decision"]
        owner = file_owner(a)

        saved = None
        # The file's own save. One made before triage was kept left only
        # imported_ride_id behind; a recognised file's link is not a save.
        if a.imported_ride_id and t["state"] != "in_history":
            saved = rides.get(a.imported_ride_id)
        if decision != "someone_else":
            if saved is not None:
                decision = "save"
            elif (decision == "save" and state != "in_history") or (
                linked and ride is None and decision is None
            ):
                # Saved or matched, then deleted from Rides: the rider's own,
                # read here only.
                decision = "chat_only"
        # Recorded as someone else's after a save, the ride stays in Rides
        # until the rider deletes it there, and the card says so (_note).
        out[a.id] = Filing(a, state, ride, saved, decision, owner, t)
    return out


def filing(db: Session, user_id: str, attachment: ChatAttachment) -> Filing:
    return filings(db, user_id, [attachment])[attachment.id]


# ── The card ────────────────────────────────────────────────────────────────

NBSP = "\u00a0"


def _hold(text: str | None) -> str | None:
    """The last two words joined, so a short line never ends on one word."""
    if not text:
        return text
    head, space, tail = text.rstrip().rpartition(" ")
    return f"{head}{NBSP}{tail}" if space and head else text


def _day(value) -> str | None:
    """A date as the app says it: 3 May 2026."""
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError:
            return None
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        return None
    return f"{value.day} {value:%b} {value.year}"


def _ride_name(title: str | None, when) -> str:
    said = title or "a ride"
    day = _day(when)
    return f"{said}, {day}" if day else said


CHOICE_LABELS = {
    "save": "Mine: save to my rides",
    "chat_only": "Mine: just for this chat",
    "someone_else": "Someone else\u2019s",
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
    if f.decision == "someone_else" and f.ride is not None and t.get("matched") != "bytes":
        # Lined up with a ride of the rider's, and said to be another
        # person's: a ride they did together, not the rider's own.
        return f"Recorded alongside {_ride_name(f.ride.title, f.ride.ride_date)}."
    if f.state == "in_history":
        return f"Already in your rides as {_ride_name(f.ride.title, f.ride.ride_date)}."
    if f.state == "part_of_ride":
        name = _ride_name(f.ride.title, f.ride.ride_date)
        if f.can_keep_separately:
            return f"Most of it overlaps {name}, already in your rides."
        return f"Part of {name}, already in your rides."
    if t["state"] in ("in_history", "part_of_ride"):
        return "The ride it matched is no longer in your rides."
    hints = t["hints"]
    if "faster" in hints:
        h = hints["faster"]
        return (
            f"Its best 20 minutes is {h['best_20min_w']} W, well above your FTP "
            f"of {h['ftp']} W."
        )
    if "high_hr" in hints:
        h = hints["high_hr"]
        return (
            f"Its heart rate reaches {h['max_hr']} bpm, above your max of "
            f"{h['rider_max_hr']} bpm."
        )
    if "alongside" in hints:
        h = hints["alongside"]
        return (
            f"Recorded alongside {_ride_name(h.get('ride_title'), h.get('ride_date'))}, "
            "but the numbers differ."
        )
    if "route" in hints:
        return "No power or heart rate in it, so it reads like a route or a GPS trace."
    if "before_first_ride" in hints:
        day = _day((f.attachment.summary or {}).get("start_time"))
        return f"From {day}, older than anything in your rides." if day else "Older than anything in your rides."
    return "Not in your rides yet."


def _question(f: Filing) -> str | None:
    if f.view != "asking":
        return None
    hints = f.triage["hints"]
    if f.owner == "self":
        return "Should it join your rides?"
    if {"faster", "high_hr", "alongside"} & set(hints):
        return "Is it someone else\u2019s?"
    if "before_first_ride" in hints:
        return "An old ride of yours?"
    return "Is it yours, and should it join your rides?"


def _save_detail(f: Filing) -> str:
    day = _day((f.attachment.summary or {}).get("start_time"))
    if f.state == "part_of_ride":
        return "It joins Rides on its own, so the overlap counts twice in your fitness chart."
    if day:
        return f"It joins Rides and your fitness chart for {day}."
    return "It joins Rides and your fitness chart."


def _choices(f: Filing) -> tuple[list[dict], list[dict]]:
    """The card's buttons, and the quiet ways to change a choice."""
    view = f.view
    if view == "asking":
        order = ("save", "chat_only") if f.owner == "self" else DECISIONS
        return [
            _choice(c, detail=_save_detail(f) if c == "save" else None) for c in order
        ], []
    if view == "part_of_ride":
        keep = (
            [_choice("save", "Keep as a separate ride", _save_detail(f))]
            if f.can_keep_separately else []
        )
        return keep, [_choice("someone_else", "Not my ride")]
    if view == "in_history":
        # The same bytes are the saved ride itself; a time match could be a
        # riding partner's file from the same outing.
        mine_by_time = f.triage.get("matched") != "bytes"
        return [], [_choice("someone_else", "Not my ride")] if mine_by_time else []
    if view == "chat_only":
        return [], [
            _choice("save", "Save to my rides instead", _save_detail(f)),
            _choice("someone_else"),
        ]
    if view == "someone_else":
        return [], [_choice("chat_only", "It\u2019s mine")]
    return [], []


def _outcome(f: Filing) -> str | None:
    if f.view == "saved":
        return "Saved to Rides."
    if f.view == "chat_only":
        return "Just for this chat. Not in your rides."
    if f.view == "someone_else":
        return f"{f.label or CHOICE_LABELS['someone_else']}, for comparison only."
    return None


def _note(f: Filing) -> str | None:
    if f.view != "someone_else":
        return None
    if f.saved is not None:
        return "A saved copy is still in Rides. Delete it there if it isn\u2019t yours."
    if f.triage.get("matched") == "bytes" and f.ride is not None:
        return "A ride with the same data is in your rides. Delete it there if it isn\u2019t yours."
    return None


def _ride_ref(ride: Ride | None) -> dict | None:
    if ride is None:
        return None
    started = _naive_utc(ride.ride_date)
    return {
        "id": ride.id,
        "title": ride.title,
        "date": started.date().isoformat() if started else None,
    }


def card(f: Filing) -> dict:
    """The file card the app shows under the message that shared the file."""
    a = f.attachment
    summary = a.summary or {}
    started = summary.get("start_time")
    choices, changes = _choices(f)
    # The ride to link to: the one it was saved as, else the one it matched.
    linked = f.saved or (f.ride if f.view in ("in_history", "part_of_ride") else None)
    if f.view == "someone_else" and f.triage.get("matched") == "bytes":
        linked = f.ride
    return {
        "id": a.id,
        "filename": a.filename,
        "kind": a.kind,
        "date": started[:10] if isinstance(started, str) else None,
        "start_time": started,
        "duration": summary.get("duration"),
        "duration_s": summary.get("duration_s"),
        "distance_km": summary.get("distance_km"),
        "state": f.state,
        "view": f.view,
        "recognised": _hold(_recognised(f)),
        "question": _hold(_question(f)),
        "outcome": _hold(_outcome(f)),
        "note": _hold(_note(f)),
        "undo": _hold("To undo it, delete the ride from Rides.") if f.view == "saved" else None,
        "ride": _ride_ref(linked),
        "overlap": f.overlap,
        "hints": sorted(f.triage["hints"]),
        "decided": f.decision is not None,
        "decision": f.decision,
        "label": f.label,
        "decided_at": f.triage.get("decided_at") if f.triage.get("decision") else None,
        "choices": choices,
        "changes": changes,
    }


def cards(db: Session, user_id: str, attachments: list[ChatAttachment]) -> list[dict]:
    """Cards for several files, in the order given."""
    found = filings(db, user_id, attachments)
    return [card(found[a.id]) for a in attachments]
