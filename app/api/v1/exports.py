"""Workout and ride export API endpoints."""

import re

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user
from app.core.exceptions import BadRequestException, ForbiddenException, NotFoundException
from app.database import get_db
from app.models.training import Workout
from app.models.user import User
from app.services import ride_service, safety_service
from app.services.export_service import (
    ride_to_gpx,
    workout_to_erg,
    workout_to_fit,
    workout_to_mrc,
    workout_to_zwo,
)
from app.services.plan_service import get_workout

router = APIRouter(prefix="/exports", tags=["exports"])


def _require_ftp(user: User) -> int:
    """An exported workout file bakes FTP into absolute watts, and once it is
    open in Zwift or on a head unit nobody can tell the number was a guess.
    So we refuse the export rather than ship a fabricated FTP off-platform."""
    if not user.ftp:
        raise BadRequestException(
            detail=(
                "Add your FTP before exporting (Settings, then Profile). Workout "
                "files carry watts, not percentages, so I need your real number "
                "to write them."
            )
        )
    return user.ftp


def _require_allowed(db: Session, user: User, workout) -> None:
    """A file in Zwift or on a head unit is out of reach of every safety
    check in the app, so a session the rider's safety state rules out today
    (a hold, an uncleared health answer, the layoff gate) isn't exported.
    Today, not the session's date: the file can be ridden any day."""
    if safety_service.workout_exceeds_ceiling(workout):
        raise ForbiddenException(
            detail=(
                "This session asks for more than its type allows, so I can't export it. "
                "Ask the coach to rebuild it."
            )
        )
    allowed = safety_service.allowed_intensity(db, user)
    if safety_service.workout_allowed(workout, allowed):
        return
    # Worded for what holds the rider back: a fever never mentions a doctor,
    # and an under-18 account says where to write instead.
    raise ForbiddenException(detail=safety_service.export_refusal(db, user))


def _exportable(db: Session, user: User, workout_id: str) -> tuple[Workout, int]:
    """The one way into a workout file, for every format: the rider's own
    session, with steps, that the safety gate allows today, and a real FTP.
    The file builders then hold every step to the ERG cap themselves."""
    workout = get_workout(db, workout_id, user.id)
    if not workout:
        raise NotFoundException(detail="Workout not found")
    if not workout.steps:
        raise BadRequestException(detail="This session has no structured steps, so there's nothing to export.")
    _require_allowed(db, user, workout)
    return workout, _require_ftp(user)


def _download(content, media_type: str, title: str, ext: str) -> Response:
    # Headers are Latin-1: a title with an accent, an emoji or a quote mark
    # must not turn a download into a server error.
    stem = re.sub(r"[^A-Za-z0-9.-]+", "_", title or "").strip("_.") or "Forma"
    filename = f"{stem}.{ext}"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/workout/{workout_id}/zwo")
def export_workout_zwo(
    workout_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download workout as ZWO file (for Zwift)."""
    workout, ftp = _exportable(db, current_user, workout_id)
    return _download(workout_to_zwo(workout, ftp=ftp), "application/xml", workout.title, "zwo")


@router.get("/workout/{workout_id}/erg")
def export_workout_erg(
    workout_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download workout as ERG file (absolute watts, for TrainerRoad/Wahoo)."""
    workout, ftp = _exportable(db, current_user, workout_id)
    return _download(workout_to_erg(workout, ftp=ftp), "text/plain", workout.title, "erg")


@router.get("/workout/{workout_id}/mrc")
def export_workout_mrc(
    workout_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download workout as MRC file (% FTP, for TrainerRoad/Wahoo)."""
    workout, ftp = _exportable(db, current_user, workout_id)
    return _download(workout_to_mrc(workout, ftp=ftp), "text/plain", workout.title, "mrc")


@router.get("/workout/{workout_id}/fit")
def export_workout_fit(
    workout_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download workout as FIT file (for Garmin/Wahoo/Hammerhead)."""
    workout, ftp = _exportable(db, current_user, workout_id)
    return _download(workout_to_fit(workout, ftp=ftp), "application/octet-stream", workout.title, "fit")


@router.get("/ride/{ride_id}/gpx")
def export_ride_gpx(
    ride_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download ride GPS track as GPX file."""
    ride = ride_service.get_ride(db, ride_id, current_user.id)
    if not ride:
        raise NotFoundException(detail="Ride not found")

    gpx_content = ride_to_gpx(db, ride_id, ride_title=ride.title)
    return _download(gpx_content, "application/gpx+xml", ride.title, "gpx")
