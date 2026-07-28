from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, UploadFile
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user
from app.core.exceptions import BadRequestException, NotFoundException
from app.database import get_db
from app.models.user import User
from app.models.segment import SegmentEffort, StravaSegment
from app.schemas.ride import (
    PowerCurveResponse,
    RideDataResponse,
    RideListResponse,
    RideRecordRequest,
    RideResponse,
)
from app.schemas.segment import RideSegmentsResponse, SegmentEffortResponse
from app.services import ride_service
from app.services.metrics_service import recalculate_from_date

router = APIRouter(prefix="/rides", tags=["rides"])

import logging
_logger = logging.getLogger(__name__)


async def _generate_debrief_bg(db: Session, user: User, ride):
    """Background task to generate a Coach Forma debrief for a new ride."""
    try:
        from app.services.coach_insights_service import generate_ride_debrief
        # NB: generate_ride_debrief is synchronous — awaiting it was the bug
        # that silently killed every debrief (audit finding #4).
        generate_ride_debrief(db, user, ride)
    except Exception:
        _logger.exception("Failed to generate debrief for ride %s", ride.id)
        return

    # Memory extraction — the debrief is a rich source of durable facts
    # (gaps observed, insights given, ride memories). Pillar 2 write path.
    try:
        if getattr(ride, "debrief_text", None):
            from app.services.memory_service import extract_memories

            extract_memories(
                db,
                user,
                f"Ride: {ride.title}\n\nForma's debrief:\n{ride.debrief_text}",
                source="debrief",
                source_ref=str(ride.id),
            )
    except Exception:
        _logger.exception("Memory extraction after debrief failed (ride %s)", ride.id)


ALLOWED_RIDE_EXTENSIONS = (".fit", ".gpx", ".tcx", ".fit.gz", ".gpx.gz", ".tcx.gz")


def _is_supported_ride_file(filename: str | None) -> bool:
    return bool(filename) and filename.lower().endswith(ALLOWED_RIDE_EXTENSIONS)


@router.post("/upload", response_model=RideResponse, status_code=201)
def upload_fit_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload a ride file (FIT, GPX or TCX), parse it, create ride with
    calculated metrics."""
    if not _is_supported_ride_file(file.filename):
        raise BadRequestException(detail="File must be a .fit, .gpx or .tcx file")

    file_bytes = file.file.read()
    if len(file_bytes) == 0:
        raise BadRequestException(detail="File is empty")

    if len(file_bytes) > 50 * 1024 * 1024:  # 50MB limit
        raise BadRequestException(detail="File too large (max 50MB)")

    ride = ride_service.create_ride_from_fit(
        db, current_user, file_bytes, filename=file.filename
    )

    # Recalculate PMC from ride date
    if ride.tss and ride.ride_date:
        recalculate_from_date(db, current_user.id, ride.ride_date.date())

    # Auto-generate Coach Forma debrief in background
    background_tasks.add_task(_generate_debrief_bg, db, current_user, ride)

    return ride


@router.post("/import-file")
def import_ride_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """One file from a bulk archive import (Strava/Garmin account export).

    Unlike /upload this never aborts a batch: parse failures come back as
    {"status": "failed"}, rides already on record come back as
    {"status": "duplicate"}. No per-ride debrief and no PMC recalculation —
    the client calls /rides/import-finalize once at the end instead.
    """
    if not _is_supported_ride_file(file.filename):
        return {"status": "unsupported", "filename": file.filename}

    file_bytes = file.file.read()
    if not file_bytes or len(file_bytes) > 50 * 1024 * 1024:
        return {"status": "failed", "filename": file.filename, "error": "empty or oversized file"}

    try:
        parsed = ride_service.parse_ride_file(file_bytes, file.filename)
    except Exception:
        _logger.warning("Archive import: could not parse %s", file.filename)
        return {"status": "failed", "filename": file.filename, "error": "could not read the file"}

    start_time = parsed.get("start_time")
    records = parsed.get("records") or []
    if start_time is None or not records:
        return {"status": "failed", "filename": file.filename, "error": "no ride data in file"}

    duration = None
    elapsed = [r.get("elapsed_seconds") for r in records if r.get("elapsed_seconds") is not None]
    if elapsed:
        duration = max(elapsed)

    existing = ride_service.find_duplicate_ride(db, current_user.id, start_time, duration)
    if existing:
        return {"status": "duplicate", "filename": file.filename, "ride_id": existing.id}

    try:
        ride = ride_service.create_ride_from_fit(
            db, current_user, file_bytes, filename=file.filename
        )
    except Exception:
        db.rollback()
        _logger.exception("Archive import: failed to create ride from %s", file.filename)
        return {"status": "failed", "filename": file.filename, "error": "could not import the ride"}

    return {
        "status": "imported",
        "filename": file.filename,
        "ride_id": ride.id,
        "title": ride.title,
        "date": ride.ride_date.isoformat() if ride.ride_date else None,
    }


@router.post("/import-finalize")
def finalize_import(
    earliest_date: datetime | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """After a bulk import: rebuild the training-load history once from the
    earliest imported ride, and link recent rides to planned workouts."""
    from app.models.ride import Ride

    if earliest_date is None:
        earliest = (
            db.query(Ride.ride_date)
            .filter(Ride.user_id == current_user.id)
            .order_by(Ride.ride_date.asc())
            .first()
        )
        earliest_date = earliest[0] if earliest else None

    if earliest_date is not None:
        recalculate_from_date(db, current_user.id, earliest_date.date())

    from app.services.workout_assessment_service import backfill_auto_links
    try:
        backfill_auto_links(db, current_user.id, days=14)
    except Exception:
        _logger.exception("import-finalize: auto-link backfill failed")

    return {"status": "ok", "recalculated_from": earliest_date.isoformat() if earliest_date else None}


@router.post("/record", response_model=RideResponse, status_code=201)
def record_ride(
    background_tasks: BackgroundTasks,
    body: RideRecordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save an in-app workout recording (from workout player)."""
    if not body.data_points:
        raise BadRequestException(detail="No data points provided")

    data_dicts = [dp.model_dump() for dp in body.data_points]
    ride = ride_service.create_ride_from_recording(
        db, current_user, body.title, body.ride_date,
        data_dicts, body.workout_id
    )

    if ride.tss and ride.ride_date:
        recalculate_from_date(db, current_user.id, ride.ride_date.date())

    # Auto-generate Coach Forma debrief in background
    background_tasks.add_task(_generate_debrief_bg, db, current_user, ride)

    return ride


@router.get("", response_model=RideListResponse)
def list_rides(
    background_tasks: BackgroundTasks,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List rides with pagination and optional date filtering."""
    rides, total = ride_service.get_rides(
        db, current_user.id, page, per_page, start_date, end_date
    )
    _warm_zone_summaries(db, current_user, rides)

    # Forma names and narrates rides that don't have it yet — in the
    # background, a few per view, so the list is never blocked. The client
    # shows a deterministic line until the coach's own words land.
    missing = [r for r in rides if not r.story or not r.forma_title][:8]
    if missing:
        background_tasks.add_task(_generate_stories_bg, db, current_user, missing)

    return RideListResponse(
        rides=[RideResponse.model_validate(r) for r in rides],
        total=total, page=page, per_page=per_page,
    )


def _generate_stories_bg(db: Session, user: User, rides: list) -> None:
    """Background: Forma writes title + story for rides missing them."""
    from app.services.coach_insights_service import generate_ride_story

    for ride in rides:
        try:
            generate_ride_story(db, user, ride)
        except Exception:
            _logger.exception("Story generation failed for ride %s", ride.id)


_SHAPE_BUCKETS = 48


def _warm_zone_summaries(db: Session, user: User, rides: list) -> None:
    """Lazily cache a compact time-in-zone summary + shape on each ride.

    One pass over the power stream produces both the zone totals and the
    "shape": the ride's power over time in ~48 buckets, each [height 0-100,
    zone 1-7] — the thumbnail fingerprint that makes every ride look like
    itself. Cached on the row forever; {"none": true} when no power data.
    """
    from app.core.formulas import power_zones
    from app.models.ride import RideData

    dirty = False
    for ride in rides:
        zs = ride.zone_summary
        if zs is not None and (zs.get("none") or zs.get("v") == 2):
            continue  # cached in current format (v2 = FTP-anchored heights)
        try:
            ftp = ride.ftp_at_time or user.ftp or 0
            powers = [
                p for (p,) in (
                    db.query(RideData.power)
                    .filter(RideData.ride_id == ride.id, RideData.power.isnot(None))
                    .order_by(RideData.elapsed_seconds)
                    .all()
                )
                if p is not None and p >= 0
            ]
            if not powers or ftp <= 0:
                ride.zone_summary = {"none": True}
                dirty = True
                continue

            zones = power_zones(ftp)
            bounds = [(zones[k]["low"], zones[k]["high"]) for k in sorted(zones.keys())]

            def zone_of(p: float) -> int:
                for i, (low, high) in enumerate(bounds):
                    if low <= p <= high:
                        return i
                return len(bounds) - 1  # above Z7

            secs = [0] * len(bounds)
            for p in powers:
                secs[zone_of(p)] += 1

            # Shape: average power per bucket, coloured by that bucket's zone.
            # Heights share ONE ruler across every ride — FTP. Full height is
            # 130% of FTP (sprint efforts cap there), so threshold sits at
            # ~77% and a recovery spin sits honestly low. A ride never looks
            # harder than it was just because it was its own hardest moment.
            n = min(_SHAPE_BUCKETS, len(powers))
            size = len(powers) / n
            buckets = []
            for i in range(n):
                seg = powers[int(i * size): max(int((i + 1) * size), int(i * size) + 1)]
                buckets.append(sum(seg) / len(seg))
            ceiling = 1.3 * ftp
            shape = [
                [max(4, min(100, round(b / ceiling * 100))), zone_of(b) + 1]
                for b in buckets
            ]

            dom_idx = max(range(len(secs)), key=lambda i: secs[i])
            ride.zone_summary = {
                "z": secs, "dom": f"z{dom_idx + 1}", "shape": shape, "v": 2,
            }
            dirty = True
        except Exception:
            _logger.exception("Zone summary failed for ride %s", ride.id)
    if dirty:
        db.commit()


@router.get("/{ride_id}", response_model=RideResponse)
def get_ride(
    ride_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a single ride summary."""
    ride = ride_service.get_ride(db, ride_id, current_user.id)
    if not ride:
        raise NotFoundException(detail="Ride not found")
    return ride


@router.get("/{ride_id}/data", response_model=RideDataResponse)
def get_ride_data(
    ride_id: str,
    resolution: str = Query("5s", pattern="^(full|5s|30s)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get time-series data for a ride (supports downsampling)."""
    ride = ride_service.get_ride(db, ride_id, current_user.id)
    if not ride:
        raise NotFoundException(detail="Ride not found")

    data_points = ride_service.get_ride_data(db, ride_id, resolution)
    return RideDataResponse(
        ride_id=ride_id,
        resolution=resolution,
        data_points=data_points,
        total_points=len(data_points),
    )


@router.get("/{ride_id}/power-curve", response_model=PowerCurveResponse)
def get_power_curve(
    ride_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get best-effort power curve for a single ride."""
    ride = ride_service.get_ride(db, ride_id, current_user.id)
    if not ride:
        raise NotFoundException(detail="Ride not found")

    points = ride_service.get_ride_power_curve(db, ride_id)
    return PowerCurveResponse(ride_id=ride_id, points=points)


@router.get("/{ride_id}/segments", response_model=RideSegmentsResponse)
def get_ride_segments(
    ride_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get segment efforts, achievements, and social data for a ride."""
    ride = ride_service.get_ride(db, ride_id, current_user.id)
    if not ride:
        raise NotFoundException(detail="Ride not found")

    efforts = (
        db.query(SegmentEffort, StravaSegment)
        .join(StravaSegment, SegmentEffort.segment_id == StravaSegment.id)
        .filter(SegmentEffort.ride_id == ride_id)
        .order_by(SegmentEffort.elapsed_time_seconds.desc())
        .all()
    )

    segment_efforts = [
        SegmentEffortResponse(
            id=effort.id,
            segment_name=segment.name,
            distance_meters=segment.distance_meters,
            average_grade=segment.average_grade,
            climb_category=segment.climb_category,
            elapsed_time_seconds=effort.elapsed_time_seconds,
            moving_time_seconds=effort.moving_time_seconds,
            average_watts=effort.average_watts,
            max_watts=effort.max_watts,
            average_hr=effort.average_hr,
            max_hr=effort.max_hr,
            pr_rank=effort.pr_rank,
            kom_rank=effort.kom_rank,
            achievement_type=effort.achievement_type,
        )
        for effort, segment in efforts
    ]

    return RideSegmentsResponse(
        ride_id=ride_id,
        achievement_count=ride.achievement_count,
        pr_count=ride.pr_count,
        kudos_count=ride.kudos_count,
        segment_efforts=segment_efforts,
    )
