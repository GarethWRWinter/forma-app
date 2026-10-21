"""Ride files handed to the coach in conversation, read but not filed.

Two jobs, deliberately separated.

ingest() reads the file properly and keeps the numbers, not the samples. The
coach gets a compact summary plus the same deep analysis it would get from a
saved ride, so it can talk about a 4:12 climb inside the file rather than
describing the whole thing by its averages. Nothing enters the ride log, the
PMC or the weekly compliance figures.

save_as_ride() is the moment the rider agrees it belongs to them. It runs the
ordinary upload path from ride_service rather than a second one of its own,
so a promoted attachment and an uploaded file produce byte-identical rides.

Between the two, ingest() also recognises the file against the rider's saved
rides (file_triage_service), and decide() records what the rider chose on the
file's card: save it, keep it for this chat, or someone else's ride.

The analysis helpers come from ride_analysis_service on purpose. Copying them
would let the attachment's numbers drift from the saved ride's numbers, and
the coach would then say two different things about one file.
"""

import gzip
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session, defer, load_only

from app.models.chat import ChatMessage, ChatRole, ChatSession
from app.models.chat_attachment import ChatAttachment
from app.models.ride import Ride
from app.models.user import User
from app.services import file_triage_service
from app.services.ride_analysis_service import _climbs, _fade, _peak_efforts
from app.services.ride_service import (
    _extract_summary_from_records,
    create_ride_from_fit,
    find_duplicate_ride,
    parse_ride_file,
)

logger = logging.getLogger(__name__)

# A ride file is a few hundred kilobytes. Anything approaching this is either
# a whole archive or a mistake, and parsing it would tie up a worker.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

SUPPORTED_KINDS = ("fit", "gpx", "tcx")

# In a file's summary while it is tied to the chat it was attached in but no
# message has carried it yet (see ingest). Gone once it is sent.
UNSENT = "unsent"

# Whose ride a file is, in its summary as "owner": the rider's own, another
# person's, or not yet said. A file read before owners were kept has none
# and counts as unknown.
OWNERS = ("self", "someone_else", "unknown")

# What the rider chose on a file's card (see decide), kept in the summary's
# triage beside what Forma recognised at upload.
DECISIONS = file_triage_service.DECISIONS
LABEL_MAX = 40

# Files one message carries. A batch of rides belongs in the archive import,
# which de-duplicates and rebuilds fitness once, not in a conversation.
MAX_FILES_PER_MESSAGE = 3
# The archive import saves every ride in a zip to Rides, so it is offered
# for the rider's own rides only: files to compare stay in the chat.
A_ZIP = (
    "That file is a zip. Unzip it and attach the ride file inside, or for a "
    "batch of your own rides, choose the zip under Ride archive in Settings, "
    "then Data in, which skips any you already have."
)
TOO_MANY_FILES = (
    "Three files is the limit for one message. If these are your own rides, "
    "zip them and choose the zip under Ride archive in Settings, then Data in, "
    "which skips any you already have. To compare them here, send up to three "
    "at a time."
)


class AttachmentError(ValueError):
    """Something the rider can act on: wrong file, too big, unreadable."""


class DecisionRefused(AttachmentError):
    """A card choice that cannot apply to this file as it stands. code says
    why: someone_else, saved, part_of_ride, alongside, no_times, use_card
    (the coach's save of a file only its card can file), or held (an account
    held as possibly under 18)."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def _kind_for(filename: str | None) -> str:
    """The file's type by extension, ignoring a trailing .gz."""
    name = (filename or "").lower()
    if name.endswith(".zip"):
        raise AttachmentError(A_ZIP)
    if name.endswith(".gz"):
        name = name[:-3]
    for kind in SUPPORTED_KINDS:
        if name.endswith("." + kind):
            return kind
    raise AttachmentError(
        "That file type will not open. Send a .fit, .gpx or .tcx file, or a "
        ".gz of one."
    )


def _clock(seconds: int | float | None) -> str | None:
    """Seconds as h:mm:ss, or m:ss under the hour."""
    if not seconds:
        return None
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _as_utc(value) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _has_gps(records: list[dict]) -> bool:
    """GPX and TCX carry degrees, FIT carries semicircles under other keys."""
    return any(
        r.get("latitude") is not None or r.get("position_lat") is not None
        for r in records
    )


def _streams(records: list[dict]) -> dict[str, list]:
    """Pull the per-second channels out of parsed records.

    Built the same way ride_analysis_service builds them from RideData rows,
    so the analysis of an attachment matches the analysis of the same file
    once it is saved.
    """
    power = [int(r["power"]) for r in records if r.get("power") is not None]
    elapsed = [int(r.get("elapsed_seconds") or 0) for r in records]
    altitude = [
        float(r.get("altitude") or r.get("enhanced_altitude"))
        for r in records
        if (r.get("altitude") or r.get("enhanced_altitude")) is not None
    ]
    distance = [float(r["distance"]) for r in records if r.get("distance") is not None]
    hr = [int(r["heart_rate"]) for r in records if r.get("heart_rate") is not None]
    cadence = [int(r["cadence"]) for r in records if r.get("cadence") is not None]
    return {
        "power": power,
        "elapsed": elapsed,
        "altitude": altitude,
        "distance": distance,
        "hr": hr,
        "cadence": cadence,
    }


def _build_summary(
    filename: str,
    kind: str,
    records: list[dict],
    session_summary: dict,
    start_time: datetime | None,
) -> dict:
    """The ride at a glance: a few dozen numbers, no sample arrays."""
    computed = _extract_summary_from_records(records)

    def value(key: str):
        # The head unit's own figure beats ours: it knows about pauses,
        # coasting and zero-offset in a way a record stream does not.
        found = session_summary.get(key)
        return found if found is not None else computed.get(key)

    # No times, no length: a route's points are not seconds.
    duration = value("total_elapsed_time") or value("total_timer_time")
    distance_m = value("total_distance")
    elevation_m = value("total_ascent")
    avg_power = value("avg_power")
    max_power = value("max_power")
    avg_hr = value("avg_heart_rate")
    max_hr = value("max_heart_rate")
    avg_cadence = value("avg_cadence")
    started = _as_utc(start_time) or _as_utc(session_summary.get("start_time"))
    # In UTC, like the rides table and the fitness chart, whatever offset
    # the file wrote its times in.
    started = started.astimezone(timezone.utc) if started else None

    summary = {
        "name": filename.rsplit("/", 1)[-1],
        "kind": kind,
        "start_time": started.isoformat() if started else None,
        "duration": _clock(duration),
        "duration_s": int(duration) if duration else None,
        "distance_km": round(distance_m / 1000, 2) if distance_m else None,
        "elevation_gain_m": round(elevation_m) if elevation_m else None,
        "avg_power": round(avg_power) if avg_power else None,
        "max_power": round(max_power) if max_power else None,
        "avg_hr": round(avg_hr) if avg_hr else None,
        "max_hr": round(max_hr) if max_hr else None,
        "avg_cadence": round(avg_cadence) if avg_cadence else None,
        "samples": len(records),
        "has_power": any(r.get("power") is not None for r in records),
        "has_gps": _has_gps(records),
    }
    return {k: v for k, v in summary.items() if v is not None}


def _build_analysis(
    records: list[dict], session_summary: dict, user: User
) -> dict:
    """Power curve, climbs, fade and time in zone, without creating a Ride.

    Mirrors ride_analysis_service.analyse_ride's output shape so the coach
    reads an attachment with exactly the tools it reads a saved ride with.
    """
    from app.core.formulas import time_in_zones

    streams = _streams(records)
    power = streams["power"]
    elapsed = streams["elapsed"]

    # The device's own threshold beats the profile's, same order of
    # preference as the import path uses. Which one it was is kept, because
    # the profile figure is today's FTP, not the FTP on the ride date.
    device_ftp = session_summary.get("threshold_power")
    from_file = bool(device_ftp and device_ftp > 0)
    ftp = int(device_ftp) if from_file else (user.ftp or None)

    result: dict = {
        "available": True,
        "samples": len(records),
        "ftp_used": ftp,
        "ftp_source": "file" if from_file else "profile",
        "has_power": bool(power),
        "has_gps": _has_gps(records),
    }

    if power:
        result["power_curve"] = _peak_efforts(power, elapsed)
        result["fade"] = _fade(power, ftp)
        if ftp:
            zones = time_in_zones(power, ftp)
            result["time_in_zones_min"] = {
                z: round(secs / 60, 1) for z, secs in zones.items() if secs
            }
            for effort in result["power_curve"]:
                effort["pct_ftp"] = round((effort["watts"] / ftp) * 100)

    if streams["altitude"] and streams["distance"]:
        result["climbs"] = _climbs(
            streams["altitude"], streams["distance"], power, elapsed
        )

    if streams["hr"]:
        result["hr"] = {
            "avg": round(sum(streams["hr"]) / len(streams["hr"])),
            "max": max(streams["hr"]),
        }
    if streams["cadence"]:
        spinning = [c for c in streams["cadence"] if c > 0]
        if spinning:
            result["cadence"] = {"avg": round(sum(spinning) / len(spinning))}

    result["computed_at"] = datetime.utcnow().isoformat() + "Z"
    return result


def ingest(
    db: Session,
    user: User,
    file_bytes: bytes,
    filename: str,
    session_id: str | None = None,
) -> ChatAttachment:
    """Read a ride file for the conversation. Creates no Ride and no RideData."""
    if not file_bytes:
        raise AttachmentError("That file is empty. Try exporting it again.")
    if len(file_bytes) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(
            f"That file is larger than {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB. "
            "Send a single ride rather than an archive."
        )

    kind = _kind_for(filename)
    if file_bytes[:4] == b"PK\x03\x04":
        # A zip under a ride file's name: the archive import reads those.
        raise AttachmentError(A_ZIP)

    try:
        parsed = parse_ride_file(file_bytes, filename)
    except Exception:
        # Escaped and cut short: the name is the rider's text, and a line
        # break in it would otherwise write a log line of its own.
        logger.warning("Chat attachment: could not parse %r", (filename or "")[:80])
        raise AttachmentError(
            "That file would not open. It may be corrupted or only part of a "
            "download."
        ) from None

    records = parsed.get("records") or []
    if not records:
        raise AttachmentError("There is no ride data in that file, only a header.")

    session_summary = parsed.get("summary") or {}
    summary = _build_summary(
        filename, kind, records, session_summary, parsed.get("start_time")
    )
    analysis = _build_analysis(records, session_summary, user)
    # The bytes' own fingerprint: the same file under another name is still
    # the same file (see same_file_key).
    summary["sha256"] = hashlib.sha256(file_bytes).hexdigest()

    # What Forma recognises before anyone asks: already in Rides, part of a
    # saved ride, or new. A file sent again keeps what the rider chose for
    # the copy before it, so nobody is asked twice.
    triage = file_triage_service.recognise(db, user, summary, analysis, records)
    copy = _decided_copy(db, user.id, summary, filename)
    owner = file_owner(copy) if copy is not None else None
    imported_ride_id = None
    if copy is not None:
        earlier = file_triage_service.triage_of(copy)
        if earlier["decision"]:
            triage.update({
                k: earlier[k]
                for k in ("decided", "decision", "label", "decided_at", "decided_by")
            })
            if earlier["decision"] == "save":
                imported_ride_id = file_triage_service.own_ride_id(copy, earlier)
    if triage["decision"] == "someone_else":
        owner = "someone_else"
    elif triage["decision"] in ("save", "chat_only") or triage["state"] != "new":
        # A ride already in Rides, or a stretch of one, is the rider's own.
        owner = "self"
    if triage["state"] == "in_history" and owner == "self":
        imported_ride_id = triage["ride_id"]
    summary["triage"] = triage
    if owner:
        summary["owner"] = owner
    if session_id:
        # Tied to the chat it was attached in from the start, so deleting
        # that chat deletes it even if it is never sent. Until a message
        # carries it the coach does not see it, and a send into another chat
        # moves it there (bind_to_session).
        summary[UNSENT] = True

    attachment = ChatAttachment(
        user_id=user.id,
        session_id=session_id,
        filename=filename.rsplit("/", 1)[-1][:255],
        kind=kind,
        summary=summary,
        analysis=analysis,
        imported_ride_id=imported_ride_id,
        # Compressed both ways, so decompressing always returns the exact
        # bytes uploaded, including a file that arrived gzipped already.
        raw_file=gzip.compress(file_bytes),
    )
    db.add(attachment)
    db.commit()
    db.refresh(attachment)
    return attachment


def get_attachments(
    db: Session, user_id: str, attachment_ids: list[str]
) -> list[ChatAttachment]:
    """Fetch the rider's own attachments by id. Never anyone else's."""
    if not attachment_ids:
        return []
    # The raw bytes (up to 25MB gzipped) load only if a caller reads them;
    # only saving a file needs them.
    return (
        db.query(ChatAttachment)
        .options(defer(ChatAttachment.raw_file))
        .filter(
            ChatAttachment.user_id == user_id,
            ChatAttachment.id.in_(attachment_ids),
        )
        .all()
    )


# How many of a chat's files the coach holds open in full. Older ones are
# named only, so the coach can say a file has dropped off rather than guess.
CHAT_FILES_IN_FULL = 6


def file_owner(attachment: ChatAttachment) -> str:
    """self, someone_else or unknown. A file saved into the rider's history
    before owners were kept counts as their own: saving one now marks it so."""
    owner = (attachment.summary or {}).get("owner")
    if owner in OWNERS:
        return owner
    return "self" if attachment.imported_ride_id else "unknown"


def _with_summary(attachment: ChatAttachment, **changes) -> None:
    """Change summary keys (None removes one). A new dict each time, because
    the JSON column does not notice a dict changed in place."""
    summary = {**(attachment.summary or {}), **changes}
    attachment.summary = {k: v for k, v in summary.items() if v is not None}


def clean_label(label: str | None) -> str | None:
    """A name for someone else's file as the rider typed it, on one line."""
    words = " ".join((label or "").split())
    text = " ".join("".join(ch for ch in words if ch.isprintable()).split())
    return text or None


def _filed(
    attachment: ChatAttachment, owner: str, decision: str | None,
    label: str | None = None, by: str | None = None, ride_id: str | None = None,
) -> None:
    """Write whose a file is and what was chosen for it, as one change, so
    owner and decision never disagree: someone_else goes with someone_else,
    save and chat_only with self."""
    triage = file_triage_service.triage_of(attachment)
    if decision != triage["decision"] or label != triage["label"]:
        triage.update(
            decided=decision is not None,
            decision=decision,
            label=label if decision == "someone_else" else None,
            decided_at=datetime.now(timezone.utc).isoformat() if decision else None,
            decided_by=by if decision else None,
        )
    recognised = triage["state"] == "in_history" and triage["ride_id"]
    if ride_id:
        attachment.imported_ride_id = ride_id
    elif recognised and owner == "someone_else" and attachment.imported_ride_id == triage["ride_id"]:
        # Matched by time to the rider's own ride: a riding partner's file
        # from the same outing is not that ride, so the link goes.
        attachment.imported_ride_id = None
    elif recognised and owner == "self" and not attachment.imported_ride_id:
        attachment.imported_ride_id = triage["ride_id"]
    _with_summary(attachment, owner=owner, triage=triage)


def _owner_change(attachment: ChatAttachment, owner: str, label: str | None, by: str) -> None:
    """Whose a file is, keeping the choice in step: someone else's is a
    choice in itself, and self or unknown undoes one."""
    decision = file_triage_service.triage_of(attachment)["decision"]
    if owner == "someone_else":
        keep = decision == "someone_else"
        label = label or (file_triage_service.triage_of(attachment)["label"] if keep else None)
        _filed(attachment, owner, "someone_else", label, by)
    elif owner == "self":
        _filed(attachment, owner, None if decision == "someone_else" else decision, by=by)
    else:
        _filed(attachment, owner, None)


def set_owner(db: Session, attachment: ChatAttachment, owner: str) -> None:
    """Record whose ride a file is, on this one upload. Touches nothing in
    the rider's history."""
    if owner not in OWNERS:
        raise ValueError(f"owner must be one of {OWNERS}")
    _owner_change(attachment, owner, None, "coach")
    db.commit()


def record_owner(
    db: Session, user: User, attachment: ChatAttachment, owner: str,
    label: str | None = None,
) -> None:
    """The coach's record of whose ride a file is, from the rider's words, on
    every upload of the file so each card agrees. Touches nothing in the
    rider's history."""
    if attachment.user_id != user.id:
        raise AttachmentError("That file is not on your account.")
    if owner not in OWNERS:
        raise ValueError(f"owner must be one of {OWNERS}")
    label = (clean_label(label) or "")[:LABEL_MAX].strip() or None
    for copy in _copies_of(db, attachment):
        _owner_change(copy, owner, label, "coach")
    db.commit()


def decide(
    db: Session, user: User, attachment: ChatAttachment, choice: str,
    label: str | None = None, by: str = "card",
) -> Ride | None:
    """What the rider chose on the file's card, for every upload of the file.

    save runs save_as_ride, which checks for the ride in history again and
    rebuilds fitness, and returns the ride: the one already in Rides when the
    file is in history. chat_only keeps it the rider's own and files nothing.
    someone_else marks it another person's ride, used only in this chat and
    refused a save from then on. Choosing what is already chosen changes
    nothing. A save is undone by deleting the ride from Rides, never here.
    Nothing is filed for an account held as possibly under 18.
    """
    if attachment.user_id != user.id:
        raise AttachmentError("That file is not on your account.")
    if file_triage_service.account_held(db, user.id):
        raise DecisionRefused("This account is on hold, so nothing can be saved to it.", "held")
    if choice not in DECISIONS:
        raise AttachmentError("Choose save, chat_only or someone_else.")
    label = clean_label(label) if choice == "someone_else" else None
    if label and len(label) > LABEL_MAX:
        raise AttachmentError(f"Keep the name to {LABEL_MAX} characters or fewer.")

    f = file_triage_service.filing(db, user.id, attachment)
    if choice == "save":
        return _save(db, user, attachment, f, by)

    if f.saved is not None:
        if f.decision == "someone_else" and choice == "chat_only":
            # Marked someone else's after a save it still has: theirs again,
            # and in Rides as it was.
            for copy in _copies_of(db, attachment):
                _filed(copy, "self", "save", by=by)
            db.commit()
            return None
        raise DecisionRefused(
            "It is saved in your rides. To take it out, delete the ride from Rides.",
            "saved",
        )
    if choice == "chat_only" and f.state == "in_history":
        # Already in Rides, so nothing to keep apart; from someone else's,
        # it is the rider's ride in Rides again.
        if f.decision != "someone_else":
            return None
        decision, owner = None, "self"
    elif choice == "chat_only":
        decision, owner = "chat_only", "self"
    else:
        decision, owner = "someone_else", "someone_else"
    if f.decision == decision and f.label == label and f.owner == owner:
        return None
    for copy in _copies_of(db, attachment):
        _filed(copy, owner, decision, label, by)
    db.commit()
    return None


# Hints that mean only the rider's own button files the file: a save the
# coach heard in the chat is refused and pointed at the card.
CARD_ONLY_HINTS = ("faster", "high_hr", "holds")


def _save(
    db: Session, user: User, attachment: ChatAttachment,
    f: "file_triage_service.Filing", by: str,
) -> Ride:
    """decide's save. The rides may have moved since the file arrived (an
    archive import, a Wahoo sync, another file saved from chat), so it looks
    again before filing anything, and files nothing a second time."""
    if f.decision == "someone_else":
        raise DecisionRefused(
            "This file is marked as someone else\u2019s ride, so it stays out "
            "of your rides. If it is yours, change that first.",
            "someone_else",
        )
    if f.saved is not None:
        return f.saved

    copies = _copies_of(db, attachment)
    file_triage_service.refresh(db, user, attachment, copies)
    f = file_triage_service.filing(db, user.id, attachment)
    if f.state == "in_history":
        # In Rides already: linked by the look just taken, and nothing saved.
        return f.ride
    hints = f.hints
    if not (attachment.summary or {}).get("start_time"):
        raise DecisionRefused(
            "This file has no times in it, so it can\u2019t sit on a date in "
            "your rides. It reads as a route, and stays in this chat.",
            "no_times",
        )
    if "alongside" in hints:
        raise DecisionRefused(
            "This file covers the same time as a ride already in your rides, "
            "with different numbers, so it can\u2019t be saved as well. If it is "
            "someone else\u2019s, mark it so.",
            "alongside",
        )
    if f.state == "part_of_ride" and (by == "coach" or not f.can_keep_separately):
        raise DecisionRefused(
            "This file is part of a ride already in your rides, so saving it "
            "would count that ride twice.",
            "part_of_ride",
        )
    if by == "coach" and set(CARD_ONLY_HINTS) & set(hints):
        raise DecisionRefused(
            "This file is saved from its card only, where what it does is "
            "written beside the button.",
            "use_card",
        )

    ride, created = _promote(db, user, attachment)
    if not created:
        # The duplicate rule found it on record after all: in Rides
        # already, so filed as that ride, with no save to undo.
        for copy in copies:
            file_triage_service.link_in_history(copy, ride)
        db.commit()
        return ride
    for copy in copies:
        _filed(copy, "self", "save", by=by, ride_id=ride.id)
    db.commit()
    return ride


def bind_to_session(
    db: Session, user_id: str, session_id: str, attachment_ids: list[str] | None
) -> int:
    """Tie files to the chat they were sent in. Returns how many were tied.

    Runs when the message carrying them is sent, because the client uploads
    before a new chat exists. The first chat a file is sent in keeps it. A
    later send of the same file into another chat still reaches the coach
    there, through that message's record of what it carried. A file tied by
    its upload to a chat it was never sent in goes with its first send, so a
    rider who attaches in one chat and sends in another has it where they
    sent it. Only the rider's own files, and only into the rider's own chat.
    """
    ids = list(dict.fromkeys(i for i in attachment_ids or [] if i))
    if not ids:
        return 0
    own_chat = (
        db.query(ChatSession.id)
        .filter(ChatSession.id == session_id, ChatSession.user_id == user_id)
        .first()
    )
    if not own_chat:
        return 0
    tied = 0
    for attachment in (
        db.query(ChatAttachment)
        .options(defer(ChatAttachment.raw_file))
        .filter(ChatAttachment.user_id == user_id, ChatAttachment.id.in_(ids))
    ):
        unsent = bool((attachment.summary or {}).get(UNSENT))
        if attachment.session_id is not None and not unsent:
            continue
        attachment.session_id = session_id
        if unsent:
            _with_summary(attachment, **{UNSENT: None})
        tied += 1
    db.commit()
    return tied


def _file_key(summary: dict, filename: str | None) -> tuple:
    """What makes two uploads one file. A ride's type, start and sample
    count, and the numbers read from it, whatever it is called: a browser
    saves a second download as "name (1)", and a .gz of the file reads the
    same. Two riders' files can start and stop on the same second; their
    numbers part them. The type keeps a FIT and the GPX made from it apart,
    since the FIT can hold more. Without a start (a route, say), the bytes'
    fingerprint, then the name."""
    if summary.get("start_time"):
        return (
            "ride", summary.get("kind"), summary["start_time"], summary.get("samples"),
            summary.get("duration_s"), summary.get("distance_km"),
            summary.get("avg_power"), summary.get("max_power"),
            summary.get("avg_hr"), summary.get("max_hr"),
        )
    if summary.get("sha256"):
        return ("bytes", summary["sha256"])
    return ("name", filename, summary.get("samples"))


def same_file_key(attachment: ChatAttachment) -> tuple:
    """Two uploads of one file share this key, so a file attached again
    shows once."""
    return _file_key(attachment.summary or {}, attachment.filename)


def _copies_query(db: Session, user_id: str, key: tuple):
    """The rider's uploads that could share key, newest first. Narrowed in
    the database by the key's first part, so an upload never reads every
    file the rider has shared; the caller compares the whole key."""
    query = (
        db.query(ChatAttachment)
        .options(load_only(
            ChatAttachment.id, ChatAttachment.user_id, ChatAttachment.filename,
            ChatAttachment.kind, ChatAttachment.summary,
            ChatAttachment.imported_ride_id, ChatAttachment.created_at,
        ))
        .filter(ChatAttachment.user_id == user_id)
    )
    if key[0] == "ride":
        query = query.filter(
            ChatAttachment.kind == key[1],
            ChatAttachment.summary["start_time"].as_string() == key[2],
        )
    elif key[0] == "bytes":
        query = query.filter(ChatAttachment.summary["sha256"].as_string() == key[1])
    else:
        query = query.filter(ChatAttachment.filename == key[1])
    return query.order_by(ChatAttachment.created_at.desc(), ChatAttachment.id.desc())


def _said_owner(copy: ChatAttachment) -> bool:
    """Whether a copy's owner was said rather than guessed. A file linked
    to a ride in Rides is taken as the rider's own, which is Forma's guess,
    not the rider's word, so a later upload does not inherit it."""
    if file_owner(copy) == "unknown":
        return False
    t = file_triage_service.triage_of(copy)
    return t["decision"] is not None or t["state"] not in file_triage_service.LINKED


def _decided_copy(
    db: Session, user_id: str, summary: dict, filename: str
) -> ChatAttachment | None:
    """The newest earlier copy of this file with a choice or an owner said
    on record, or None when no copy has one."""
    key = _file_key(summary, filename.rsplit("/", 1)[-1][:255])
    for copy in _copies_query(db, user_id, key):
        if same_file_key(copy) == key and (
            file_triage_service.triage_of(copy)["decision"] or _said_owner(copy)
        ):
            return copy
    return None


def _copies_of(db: Session, attachment: ChatAttachment) -> list[ChatAttachment]:
    """Every upload of this file by its rider, itself included. A choice is
    about the file, not one upload of it, so each copy's card agrees."""
    key = same_file_key(attachment)
    copies = [
        c for c in _copies_query(db, attachment.user_id, key) if same_file_key(c) == key
    ]
    return copies if any(c.id == attachment.id for c in copies) else [attachment, *copies]


@dataclass
class ChatFiles:
    """The files a chat holds, as the coach is shown them."""

    # The newest CHAT_FILES_IN_FULL files, oldest first.
    in_full: list[ChatAttachment] = field(default_factory=list)
    # Older files, oldest first, which the coach knows by name only.
    named_only: list[ChatAttachment] = field(default_factory=list)
    # attachment id -> when the rider's message that carried it was sent.
    shared_at: dict[str, datetime] = field(default_factory=dict)
    # Every file the chat's messages carried, repeats included: id -> filename.
    names: dict[str, str] = field(default_factory=dict)


def chat_files(db: Session, user_id: str, session_id: str) -> ChatFiles:
    """Every file shared in one chat: the ones tied to it, plus every id its
    rider messages carried, and only ever the rider's own.

    The order is fixed (upload time, then id) so the coach's view is the same
    bytes from turn to turn. A file attached twice shows once, as the copy
    shared most recently, so a later "save it" reaches that copy.
    """
    sent_with: dict[str, datetime] = {}
    for snapshot, created_at in (
        db.query(ChatMessage.context_snapshot, ChatMessage.created_at)
        .filter(ChatMessage.session_id == session_id, ChatMessage.role == ChatRole.user)
        .order_by(ChatMessage.created_at, ChatMessage.id)
    ):
        ids = snapshot.get("attachment_ids") if isinstance(snapshot, dict) else None
        for aid in ids if isinstance(ids, list) else []:
            if isinstance(aid, str):
                sent_with[aid] = created_at

    in_this_chat = ChatAttachment.session_id == session_id
    if sent_with:
        in_this_chat = or_(in_this_chat, ChatAttachment.id.in_(list(sent_with)))
    rows = (
        db.query(ChatAttachment)
        .options(defer(ChatAttachment.raw_file))
        .filter(ChatAttachment.user_id == user_id, in_this_chat)
        .order_by(ChatAttachment.created_at, ChatAttachment.id)
        .all()
    )

    # Attached here but not sent yet: still in the composer, or taken off it.
    rows = [a for a in rows if a.id in sent_with or not (a.summary or {}).get(UNSENT)]

    newest: dict[tuple, ChatAttachment] = {}
    for a in rows:  # oldest first, so a later copy replaces an earlier one
        newest[same_file_key(a)] = a
    kept = sorted(newest.values(), key=lambda a: (a.created_at, a.id))
    cut = max(len(kept) - CHAT_FILES_IN_FULL, 0)

    return ChatFiles(
        in_full=kept[cut:],
        named_only=kept[:cut],
        shared_at={a.id: sent_with[a.id] for a in rows if a.id in sent_with},
        names={a.id: a.filename for a in rows},
    )


def delete_session_files(db: Session, user_id: str, session_id: str) -> int:
    """Delete the files tied to one of the rider's chats, raw bytes and all,
    and return how many went. A file the rider also sent into another of
    their chats moves to the first of those instead, so it is still there to
    read. A ride already saved from one keeps its ride. The caller commits."""
    tied = {
        aid
        for (aid,) in db.query(ChatAttachment.id).filter(
            ChatAttachment.user_id == user_id,
            ChatAttachment.session_id == session_id,
        )
    }
    if not tied:
        return 0

    sent_elsewhere: dict[str, str] = {}
    for other_chat, snapshot in (
        db.query(ChatMessage.session_id, ChatMessage.context_snapshot)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .filter(
            ChatSession.user_id == user_id,
            ChatMessage.session_id != session_id,
            ChatMessage.role == ChatRole.user,
            ChatMessage.context_snapshot.isnot(None),
        )
        .order_by(ChatMessage.created_at, ChatMessage.id)
    ):
        ids = snapshot.get("attachment_ids") if isinstance(snapshot, dict) else None
        for aid in ids if isinstance(ids, list) else []:
            if aid in tied:
                sent_elsewhere.setdefault(aid, other_chat)

    for aid, other_chat in sent_elsewhere.items():
        db.query(ChatAttachment).filter(
            ChatAttachment.user_id == user_id, ChatAttachment.id == aid
        ).update({ChatAttachment.session_id: other_chat}, synchronize_session=False)

    gone = tied - set(sent_elsewhere)
    if not gone:
        return 0
    return (
        db.query(ChatAttachment)
        .filter(ChatAttachment.user_id == user_id, ChatAttachment.id.in_(gone))
        .delete(synchronize_session=False)
    )


def save_as_ride(db: Session, user: User, attachment: ChatAttachment) -> Ride:
    """Promote an attachment into a real Ride plus its per-second data.

    Returns the existing ride when this outing is already on record, so
    saying yes twice, or saving a file the rider had already uploaded, never
    double-counts the training load.
    """
    return _promote(db, user, attachment)[0]


def _promote(db: Session, user: User, attachment: ChatAttachment) -> tuple[Ride, bool]:
    """save_as_ride, saying whether it created the ride (True) or found the
    outing on record already (False)."""
    if attachment.user_id != user.id:
        raise AttachmentError("That file is not on your account.")

    if attachment.imported_ride_id:
        existing = (
            db.query(Ride)
            .filter(Ride.id == attachment.imported_ride_id, Ride.user_id == user.id)
            .first()
        )
        if existing:
            return existing, False

    summary = attachment.summary or {}
    if not summary.get("start_time"):
        # A ride with no times would be dated today, with its points counted
        # as seconds.
        raise AttachmentError(
            "That file has no times in it, so it reads as a route rather than "
            "a ride, and a route never goes into your rides."
        )

    if not attachment.raw_file:
        raise AttachmentError(
            "The original file is no longer held. Upload it again and it will "
            "save straight away."
        )
    file_bytes = gzip.decompress(attachment.raw_file)

    started = None
    if summary.get("start_time"):
        try:
            started = _as_utc(datetime.fromisoformat(summary["start_time"]))
        except ValueError:
            started = None

    if started:
        duplicate = find_duplicate_ride(
            db, user.id, started, summary.get("duration_s")
        )
        if duplicate:
            attachment.imported_ride_id = duplicate.id
            _with_summary(attachment, owner="self")
            db.commit()
            return duplicate, False

    # A file shared in chat can come from anyone's head unit, so its own
    # threshold never becomes the rider's FTP: in review (16 Oct 2026) a
    # pro's file saved by mistake set it from 290 W to 430 W.
    ride = create_ride_from_fit(
        db, user, file_bytes, filename=attachment.filename, source="fit_upload",
        adopt_ftp=False,
    )
    attachment.imported_ride_id = ride.id
    # Saved into the rider's own history, so it is the rider's own ride.
    _with_summary(attachment, owner="self")
    db.commit()

    # A saved ride carries training load. Leaving the PMC unrebuilt would
    # show the ride in the list but not in the form curve, which is the sort
    # of quiet inconsistency the rider notices and stops trusting.
    if ride.tss and ride.ride_date:
        try:
            from app.services.metrics_service import recalculate_from_date

            recalculate_from_date(db, user.id, ride.ride_date.date())
        except Exception:
            logger.exception("Attachment promotion: PMC rebuild failed (ride %s)", ride.id)

    return ride, True
