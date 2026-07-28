"""FIT file parsing pipeline and ride creation."""

from datetime import datetime, timezone
from io import BytesIO

import fitparse
from sqlalchemy import func, insert
from sqlalchemy.orm import Session

from app.core.formulas import (
    best_efforts,
    intensity_factor,
    normalized_power,
    training_stress_score,
    variability_index,
)
from app.models.ride import Ride, RideData, RideSource
from app.models.training import Workout, WorkoutStatus
from app.models.user import User


def parse_fit_file(file_bytes: bytes) -> dict:
    """
    Parse a FIT file into structured data.

    Returns:
        {
            "summary": { ride-level summary stats },
            "records": [ { per-second data points } ],
            "laps": [ { lap summaries } ]
        }
    """
    fit_file = fitparse.FitFile(BytesIO(file_bytes))

    records = []
    summary = {}
    laps = []
    start_time = None

    for message in fit_file.get_messages():
        msg_type = message.name

        if msg_type == "record":
            record = {}
            for field in message.fields:
                record[field.name] = field.value

            # Track start time
            if start_time is None and "timestamp" in record:
                start_time = record["timestamp"]

            # Calculate elapsed seconds
            if start_time and "timestamp" in record:
                ts = record["timestamp"]
                if isinstance(ts, datetime):
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if start_time.tzinfo is None:
                        start_time = start_time.replace(tzinfo=timezone.utc)
                    record["elapsed_seconds"] = int((ts - start_time).total_seconds())

            records.append(record)

        elif msg_type == "session":
            for field in message.fields:
                summary[field.name] = field.value

        elif msg_type == "lap":
            lap = {}
            for field in message.fields:
                lap[field.name] = field.value
            laps.append(lap)

    return {
        "summary": summary,
        "records": records,
        "laps": laps,
        "start_time": start_time,
    }


def _extract_summary_from_records(records: list[dict]) -> dict:
    """Compute summary stats from record data when session message is incomplete."""
    if not records:
        return {}

    powers = [r.get("power") for r in records if r.get("power") is not None and r.get("power") >= 0]
    heart_rates = [r.get("heart_rate") for r in records if r.get("heart_rate") is not None]
    cadences = [r.get("cadence") for r in records if r.get("cadence") is not None]
    speeds = [r.get("speed") or r.get("enhanced_speed") for r in records
              if (r.get("speed") or r.get("enhanced_speed")) is not None]

    summary = {}
    if powers:
        summary["avg_power"] = sum(powers) / len(powers)
        summary["max_power"] = max(powers)
    if heart_rates:
        summary["avg_heart_rate"] = sum(heart_rates) / len(heart_rates)
        summary["max_heart_rate"] = max(heart_rates)
    if cadences:
        summary["avg_cadence"] = sum(cadences) / len(cadences)
    if speeds:
        summary["avg_speed"] = sum(speeds) / len(speeds)

    # Duration from elapsed seconds
    elapsed_times = [r.get("elapsed_seconds", 0) for r in records if r.get("elapsed_seconds") is not None]
    if elapsed_times:
        summary["total_elapsed_time"] = max(elapsed_times)

    # Distance
    distances = [r.get("distance") for r in records if r.get("distance") is not None]
    if distances:
        summary["total_distance"] = max(distances)

    # Elevation
    altitudes = [r.get("altitude") or r.get("enhanced_altitude") for r in records
                 if (r.get("altitude") or r.get("enhanced_altitude")) is not None]
    if altitudes:
        gain = 0
        for i in range(1, len(altitudes)):
            diff = altitudes[i] - altitudes[i - 1]
            if diff > 0:
                gain += diff
        summary["total_ascent"] = gain

    return summary


def parse_ride_file(file_bytes: bytes, filename: str | None = None) -> dict:
    """Parse any supported ride file (.fit/.gpx/.tcx, optionally .gz) into
    the common {summary, records, laps, start_time} shape."""
    import gzip

    from app.services.file_parsers import parse_gpx, parse_tcx

    name = (filename or "").lower()
    if name.endswith(".gz") or file_bytes[:2] == b"\x1f\x8b":
        file_bytes = gzip.decompress(file_bytes)
        name = name[:-3] if name.endswith(".gz") else name

    if name.endswith(".gpx"):
        return parse_gpx(file_bytes)
    if name.endswith(".tcx"):
        return parse_tcx(file_bytes)
    # FIT by extension or by default (Strava archives ship bare .fit.gz)
    return parse_fit_file(file_bytes)


def find_duplicate_ride(
    db: Session, user_id: str, ride_date: datetime, duration_seconds: int | None
) -> Ride | None:
    """A ride already on record that is almost certainly the same outing:
    starts within 2 minutes and (when both know it) runs within 5% or 60s
    of the same duration. Used so archive re-imports and multi-source
    overlap (Wahoo + upload) never double-count a ride."""
    from datetime import timedelta

    if ride_date.tzinfo is None:
        ride_date = ride_date.replace(tzinfo=timezone.utc)
    window = timedelta(minutes=2)
    candidates = (
        db.query(Ride)
        .filter(
            Ride.user_id == user_id,
            Ride.ride_date >= ride_date - window,
            Ride.ride_date <= ride_date + window,
        )
        .all()
    )
    for cand in candidates:
        if duration_seconds and cand.duration_seconds:
            tolerance = max(60, int(0.05 * max(duration_seconds, cand.duration_seconds)))
            if abs(cand.duration_seconds - duration_seconds) > tolerance:
                continue
        return cand
    return None


def create_ride_from_fit(
    db: Session, user: User, file_bytes: bytes, filename: str | None = None,
    source: str = "fit_upload",
) -> Ride:
    """
    Parse a ride file (FIT/GPX/TCX), create Ride and RideData rows,
    calculate metrics.

    Prioritises device-calculated metrics (NP, TSS, IF, FTP) from the
    FIT session summary over our own re-calculations, because the head
    unit has access to real-time data we may not (e.g. zero-offset,
    coasting detection, pause handling).
    """
    from app.services.ride_classifier import classify_ride

    parsed = parse_ride_file(file_bytes, filename)
    records = parsed["records"]
    session_summary = parsed["summary"]
    start_time = parsed.get("start_time")

    # Compute summary from records if session data is sparse
    record_summary = _extract_summary_from_records(records)

    # Prefer session summary, fall back to record-computed values
    def get_val(session_key, record_key=None):
        val = session_summary.get(session_key)
        if val is not None:
            return val
        return record_summary.get(record_key or session_key)

    # --- FTP: prefer device threshold_power, then user profile ---
    device_ftp = session_summary.get("threshold_power")
    ride_ftp = int(device_ftp) if device_ftp and device_ftp > 0 else (user.ftp or 0)

    # Only update user.ftp if the ride is the most recent one with FTP data.
    # This prevents older rides (during batch import) from overwriting a
    # newer FTP value.
    if device_ftp and device_ftp > 0 and int(device_ftp) != user.ftp:
        # Determine this ride's date early for the FTP check
        _ride_ts = (
            start_time
            or session_summary.get("start_time")
            or session_summary.get("timestamp")
        )
        latest_ride = (
            db.query(Ride.ride_date)
            .filter(Ride.user_id == user.id, Ride.ftp_at_time.isnot(None))
            .order_by(Ride.ride_date.desc())
            .first()
        )
        is_newest = latest_ride is None or (
            _ride_ts and isinstance(_ride_ts, datetime) and (
                _ride_ts.replace(tzinfo=timezone.utc) if _ride_ts.tzinfo is None else _ride_ts
            ) >= latest_ride.ride_date.replace(tzinfo=timezone.utc)
            if latest_ride.ride_date.tzinfo is None
            else latest_ride.ride_date
        )
        if is_newest:
            user.ftp = int(device_ftp)

    # --- Power data for our own NP calc (fallback only) ---
    power_data = [r.get("power") for r in records if r.get("power") is not None]

    # --- Prefer device-calculated NP, TSS, IF over our calculations ---
    device_np = session_summary.get("normalized_power")
    device_tss = session_summary.get("training_stress_score")
    device_if = session_summary.get("intensity_factor")

    # Use device values if available, otherwise calculate ourselves
    if device_np and device_np > 0:
        np_val = float(device_np)
    else:
        np_val = normalized_power([p or 0 for p in power_data])

    avg_power = get_val("avg_power") or (
        sum(p for p in power_data if p) / len(power_data) if power_data else 0
    )
    duration = get_val("total_elapsed_time") or len(records)

    if device_if and device_if > 0:
        if_val = float(device_if)
    elif ride_ftp > 0 and np_val > 0:
        if_val = intensity_factor(np_val, ride_ftp)
    else:
        if_val = None

    vi_val = variability_index(np_val, avg_power) if avg_power > 0 and np_val > 0 else None

    if device_tss and device_tss > 0:
        tss_val = float(device_tss)
    elif ride_ftp > 0 and if_val and np_val > 0:
        tss_val = training_stress_score(int(duration), np_val, if_val, ride_ftp)
    else:
        tss_val = None

    # Determine ride date
    ride_date = start_time
    if ride_date is None:
        ts = session_summary.get("start_time") or session_summary.get("timestamp")
        if ts and isinstance(ts, datetime):
            ride_date = ts
    if ride_date is None:
        ride_date = datetime.now(timezone.utc)
    if ride_date.tzinfo is None:
        ride_date = ride_date.replace(tzinfo=timezone.utc)

    # --- Smart ride title from classifier ---
    title = classify_ride(
        power_samples=power_data if power_data else None,
        ftp=ride_ftp,
        duration_seconds=int(get_val("total_timer_time") or duration or 0),
        normalized_power=np_val,
        average_power=avg_power,
        intensity_factor=if_val or 0,
        variability_index=vi_val or 0,
        ride_date=ride_date,
    )

    ride = Ride(
        user_id=user.id,
        source=RideSource(source),
        title=title,
        fit_file_path=filename,
        ride_date=ride_date,
        duration_seconds=int(duration) if duration else None,
        moving_time_seconds=int(get_val("total_timer_time") or duration or 0),
        distance_meters=get_val("total_distance"),
        elevation_gain_meters=get_val("total_ascent"),
        average_power=round(avg_power, 1) if avg_power else None,
        normalized_power=round(np_val, 1) if np_val else None,
        max_power=int(get_val("max_power") or 0) or None,
        average_hr=int(get_val("avg_heart_rate") or 0) or None,
        max_hr=int(get_val("max_heart_rate") or 0) or None,
        average_cadence=int(get_val("avg_cadence") or 0) or None,
        average_speed=get_val("avg_speed") or get_val("enhanced_avg_speed"),
        intensity_factor=round(if_val, 3) if if_val else None,
        variability_index=round(vi_val, 3) if vi_val else None,
        tss=round(tss_val, 1) if tss_val else None,
        ftp_at_time=ride_ftp or None,
        calories=int(get_val("total_calories") or 0) or None,
    )

    db.add(ride)
    db.flush()  # Get ride.id for FK

    # Bulk insert ride_data
    if records:
        data_rows = []
        for rec in records:
            ts = rec.get("timestamp")
            if ts and isinstance(ts, datetime) and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            data_rows.append({
                "ride_id": ride.id,
                "timestamp": ts,
                "elapsed_seconds": _safe_int(rec.get("elapsed_seconds")),
                "power": _safe_int(rec.get("power")),
                "heart_rate": _safe_int(rec.get("heart_rate")),
                "cadence": _safe_int(rec.get("cadence")),
                "speed": _safe_float(rec.get("speed") or rec.get("enhanced_speed")),
                "distance": _safe_float(rec.get("distance")),
                "altitude": _safe_float(rec.get("altitude") or rec.get("enhanced_altitude")),
                "latitude": (
                    rec["latitude"] if rec.get("latitude") is not None
                    else _semicircles_to_degrees(rec.get("position_lat"))
                ),
                "longitude": (
                    rec["longitude"] if rec.get("longitude") is not None
                    else _semicircles_to_degrees(rec.get("position_long"))
                ),
                "temperature": _safe_int(rec.get("temperature")),
                "left_right_balance": _safe_int(rec.get("left_right_balance")),
                "torque": _safe_float(rec.get("torque_effectiveness")),
            })

        # Batch insert for performance
        db.execute(insert(RideData), data_rows)

    db.commit()
    db.refresh(ride)
    return ride


def create_ride_from_recording(
    db: Session, user: User, title: str, ride_date: datetime,
    data_points: list[dict], workout_id: str | None = None
) -> Ride:
    """
    Create a ride from in-app workout recording (raw JSON time-series).
    """
    power_data = [dp.get("power", 0) or 0 for dp in data_points]

    np = normalized_power(power_data)
    avg_power = sum(power_data) / len(power_data) if power_data else 0
    duration = len(data_points)  # 1Hz recording
    user_ftp = user.ftp or 0

    if_val = intensity_factor(np, user_ftp) if user_ftp > 0 else None
    vi_val = variability_index(np, avg_power) if avg_power > 0 else None
    tss_val = training_stress_score(duration, np, if_val or 0, user_ftp) if user_ftp > 0 else None

    heart_rates = [dp.get("heart_rate") for dp in data_points if dp.get("heart_rate")]
    cadences = [dp.get("cadence") for dp in data_points if dp.get("cadence")]

    ride = Ride(
        user_id=user.id,
        source=RideSource.in_app,
        title=title,
        ride_date=ride_date,
        duration_seconds=duration,
        moving_time_seconds=duration,
        distance_meters=data_points[-1].get("distance") if data_points else None,
        average_power=round(avg_power, 1) if avg_power else None,
        normalized_power=round(np, 1) if np else None,
        max_power=max(power_data) if power_data else None,
        average_hr=round(sum(heart_rates) / len(heart_rates)) if heart_rates else None,
        max_hr=max(heart_rates) if heart_rates else None,
        average_cadence=round(sum(cadences) / len(cadences)) if cadences else None,
        intensity_factor=round(if_val, 3) if if_val else None,
        variability_index=round(vi_val, 3) if vi_val else None,
        tss=round(tss_val, 1) if tss_val else None,
        ftp_at_time=user_ftp or None,
        workout_id=workout_id,
    )

    db.add(ride)
    db.flush()

    if data_points:
        data_rows = []
        for i, dp in enumerate(data_points):
            data_rows.append({
                "ride_id": ride.id,
                "elapsed_seconds": dp.get("elapsed_seconds", i),
                "power": dp.get("power"),
                "heart_rate": dp.get("heart_rate"),
                "cadence": dp.get("cadence"),
                "speed": dp.get("speed"),
                "distance": dp.get("distance"),
            })
        db.execute(insert(RideData), data_rows)

    # Riding a planned session IS completing it — flip the workout so the
    # calendar and weekly compliance reflect the ride without a manual PATCH.
    if workout_id:
        workout = (
            db.query(Workout)
            .filter(Workout.id == workout_id, Workout.user_id == user.id)
            .first()
        )
        if workout:
            workout.status = WorkoutStatus.completed
            workout.actual_ride_id = ride.id

    db.commit()
    db.refresh(ride)
    return ride


def get_rides(
    db: Session, user_id: str, page: int = 1, per_page: int = 20,
    start_date: datetime | None = None, end_date: datetime | None = None
) -> tuple[list[Ride], int]:
    """List rides with pagination and optional date filtering.

    Excludes Strava rides that have a corresponding Dropbox ride
    (identified by matching strava_activity_id) to avoid duplicates.
    """
    from sqlalchemy import exists, and_

    # Sub-query: Strava rides whose external_id appears as
    # strava_activity_id on a Dropbox ride for the same user.
    dropbox_covers = (
        db.query(Ride.strava_activity_id)
        .filter(
            Ride.user_id == user_id,
            Ride.source == RideSource.dropbox,
            Ride.strava_activity_id.isnot(None),
        )
        .subquery()
    )

    query = db.query(Ride).filter(
        Ride.user_id == user_id,
        # Exclude Strava rides that are covered by a Dropbox ride
        ~and_(
            Ride.source == RideSource.strava,
            Ride.external_id.in_(db.query(dropbox_covers.c.strava_activity_id)),
        ),
    )

    if start_date:
        query = query.filter(Ride.ride_date >= start_date)
    if end_date:
        query = query.filter(Ride.ride_date <= end_date)

    total = query.count()
    rides = (
        query
        .order_by(Ride.ride_date.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return rides, total


def get_ride(db: Session, ride_id: str, user_id: str) -> Ride | None:
    """Get a single ride by ID."""
    return db.query(Ride).filter(Ride.id == ride_id, Ride.user_id == user_id).first()


def get_ride_data(
    db: Session, ride_id: str, resolution: str = "5s"
) -> list[dict]:
    """
    Get time-series data for a ride with optional downsampling.

    Resolutions:
        "full" - all data points
        "5s" - 5-second averages (default)
        "30s" - 30-second averages
    """
    if resolution == "full":
        rows = (
            db.query(RideData)
            .filter(RideData.ride_id == ride_id)
            .order_by(RideData.elapsed_seconds)
            .all()
        )
        return [
            {
                "elapsed_seconds": r.elapsed_seconds,
                "power": r.power,
                "heart_rate": r.heart_rate,
                "cadence": r.cadence,
                "speed": r.speed,
                "distance": r.distance,
                "altitude": r.altitude,
                "latitude": r.latitude,
                "longitude": r.longitude,
                "temperature": r.temperature,
                "left_right_balance": r.left_right_balance,
                "torque": r.torque,
            }
            for r in rows
        ]

    # Downsampled: GROUP BY interval
    interval = 5 if resolution == "5s" else 30
    rows = (
        db.query(
            (RideData.elapsed_seconds / interval * interval).label("bucket"),
            func.avg(RideData.power).label("power"),
            func.avg(RideData.heart_rate).label("heart_rate"),
            func.avg(RideData.cadence).label("cadence"),
            func.avg(RideData.speed).label("speed"),
            func.max(RideData.distance).label("distance"),
            func.avg(RideData.altitude).label("altitude"),
        )
        .filter(RideData.ride_id == ride_id)
        .group_by("bucket")
        .order_by("bucket")
        .all()
    )
    return [
        {
            "elapsed_seconds": int(r.bucket) if r.bucket else 0,
            "power": round(r.power) if r.power else None,
            "heart_rate": round(r.heart_rate) if r.heart_rate else None,
            "cadence": round(r.cadence) if r.cadence else None,
            "speed": round(r.speed, 2) if r.speed else None,
            "distance": round(r.distance, 1) if r.distance else None,
            "altitude": round(r.altitude, 1) if r.altitude else None,
        }
        for r in rows
    ]


def get_ride_power_curve(db: Session, ride_id: str) -> list[dict]:
    """Calculate best-effort power curve for a ride."""
    rows = (
        db.query(RideData.power)
        .filter(RideData.ride_id == ride_id)
        .order_by(RideData.elapsed_seconds)
        .all()
    )
    power_data = [r.power or 0 for r in rows]

    if not power_data:
        return []

    durations = [1, 5, 10, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600]
    # Only include durations shorter than the ride
    valid_durations = [d for d in durations if d <= len(power_data)]

    efforts = best_efforts(power_data, valid_durations)

    labels = {
        1: "1s", 5: "5s", 10: "10s", 15: "15s", 30: "30s",
        60: "1min", 120: "2min", 300: "5min", 600: "10min",
        1200: "20min", 1800: "30min", 3600: "60min",
    }

    return [
        {
            "duration_seconds": d,
            "duration_label": labels.get(d, f"{d}s"),
            "best_power": efforts[d],
        }
        for d in valid_durations
        if efforts.get(d, 0) > 0
    ]


def _safe_int(value) -> int | None:
    """Convert value to int, returning None if not possible."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _safe_float(value) -> float | None:
    """Convert value to float, returning None if not possible."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _semicircles_to_degrees(semicircles: int | None) -> float | None:
    """Convert FIT file semicircle coordinates to degrees."""
    if semicircles is None:
        return None
    return semicircles * (180.0 / 2**31)
