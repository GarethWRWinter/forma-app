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

The analysis helpers come from ride_analysis_service on purpose. Copying them
would let the attachment's numbers drift from the saved ride's numbers, and
the coach would then say two different things about one file.
"""

import gzip
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.chat_attachment import ChatAttachment
from app.models.ride import Ride
from app.models.user import User
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


class AttachmentError(ValueError):
    """Something the rider can act on: wrong file, too big, unreadable."""


def _kind_for(filename: str | None) -> str:
    """The file's type by extension, ignoring a trailing .gz."""
    name = (filename or "").lower()
    if name.endswith(".gz"):
        name = name[:-3]
    for kind in SUPPORTED_KINDS:
        if name.endswith("." + kind):
            return kind
    raise AttachmentError(
        "That file type will not open. Send a .fit, .gpx or .tcx file, "
        "zipped or not."
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

    duration = value("total_elapsed_time") or value("total_timer_time") or len(records)
    distance_m = value("total_distance")
    elevation_m = value("total_ascent")
    avg_power = value("avg_power")
    max_power = value("max_power")
    avg_hr = value("avg_heart_rate")
    max_hr = value("max_heart_rate")
    avg_cadence = value("avg_cadence")
    started = _as_utc(start_time) or _as_utc(session_summary.get("start_time"))

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
    # preference as the import path uses.
    device_ftp = session_summary.get("threshold_power")
    ftp = int(device_ftp) if device_ftp and device_ftp > 0 else (user.ftp or None)

    result: dict = {
        "available": True,
        "samples": len(records),
        "ftp_used": ftp,
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

    try:
        parsed = parse_ride_file(file_bytes, filename)
    except Exception:
        logger.warning("Chat attachment: could not parse %s", filename)
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

    attachment = ChatAttachment(
        user_id=user.id,
        session_id=session_id,
        filename=filename.rsplit("/", 1)[-1][:255],
        kind=kind,
        summary=summary,
        analysis=analysis,
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
    return (
        db.query(ChatAttachment)
        .filter(
            ChatAttachment.user_id == user_id,
            ChatAttachment.id.in_(attachment_ids),
        )
        .all()
    )


def save_as_ride(db: Session, user: User, attachment: ChatAttachment) -> Ride:
    """Promote an attachment into a real Ride plus its per-second data.

    Returns the existing ride when this outing is already on record, so
    saying yes twice, or saving a file the rider had already uploaded, never
    double-counts the training load.
    """
    if attachment.user_id != user.id:
        raise AttachmentError("That file is not on your account.")

    if attachment.imported_ride_id:
        existing = (
            db.query(Ride)
            .filter(Ride.id == attachment.imported_ride_id, Ride.user_id == user.id)
            .first()
        )
        if existing:
            return existing

    if not attachment.raw_file:
        raise AttachmentError(
            "The original file is no longer held. Upload it again and it will "
            "save straight away."
        )
    file_bytes = gzip.decompress(attachment.raw_file)

    summary = attachment.summary or {}
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
            db.commit()
            return duplicate

    ride = create_ride_from_fit(
        db, user, file_bytes, filename=attachment.filename, source="fit_upload"
    )
    attachment.imported_ride_id = ride.id
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

    return ride
