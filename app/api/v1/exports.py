"""Workout and ride export API endpoints."""

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user
from app.core.exceptions import BadRequestException, NotFoundException
from app.database import get_db
from app.models.user import User
from app.services import ride_service
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
                "Set your FTP before exporting. Workout files carry watts, not "
                "percentages, so I need your real number to write them."
            )
        )
    return user.ftp


@router.get("/workout/{workout_id}/zwo")
def export_workout_zwo(
    workout_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download workout as ZWO file (for Zwift)."""
    workout = get_workout(db, workout_id, current_user.id)
    if not workout:
        raise NotFoundException(detail="Workout not found")

    if not workout.steps:
        raise BadRequestException(detail="Workout has no steps to export")

    zwo_content = workout_to_zwo(workout, ftp=_require_ftp(current_user))

    filename = f"{workout.title.replace(' ', '_')}.zwo"
    return Response(
        content=zwo_content,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/workout/{workout_id}/erg")
def export_workout_erg(
    workout_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download workout as ERG file (absolute watts, for TrainerRoad/Wahoo)."""
    workout = get_workout(db, workout_id, current_user.id)
    if not workout:
        raise NotFoundException(detail="Workout not found")
    if not workout.steps:
        raise BadRequestException(detail="Workout has no steps to export")

    erg_content = workout_to_erg(workout, ftp=_require_ftp(current_user))
    filename = f"{workout.title.replace(' ', '_')}.erg"
    return Response(
        content=erg_content,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/workout/{workout_id}/mrc")
def export_workout_mrc(
    workout_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download workout as MRC file (% FTP, for TrainerRoad/Wahoo)."""
    workout = get_workout(db, workout_id, current_user.id)
    if not workout:
        raise NotFoundException(detail="Workout not found")
    if not workout.steps:
        raise BadRequestException(detail="Workout has no steps to export")

    mrc_content = workout_to_mrc(workout, ftp=_require_ftp(current_user))
    filename = f"{workout.title.replace(' ', '_')}.mrc"
    return Response(
        content=mrc_content,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/workout/{workout_id}/fit")
def export_workout_fit(
    workout_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download workout as FIT file (for Garmin/Wahoo/Hammerhead)."""
    workout = get_workout(db, workout_id, current_user.id)
    if not workout:
        raise NotFoundException(detail="Workout not found")
    if not workout.steps:
        raise BadRequestException(detail="Workout has no steps to export")

    fit_content = workout_to_fit(workout, ftp=_require_ftp(current_user))
    filename = f"{workout.title.replace(' ', '_')}.fit"
    return Response(
        content=fit_content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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

    filename = f"{ride.title.replace(' ', '_')}.gpx"
    return Response(
        content=gpx_content,
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
