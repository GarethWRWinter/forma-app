from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user
from app.core.constants import POWER_PROFILE_DURATIONS
from app.core.exceptions import BadRequestException
from app.core.formulas import ftp_from_20min_test, w_per_kg
from app.database import get_db
from app.models.ride import Ride
from app.models.user import User
from app.core.constants import RIDER_PROFILE_CATEGORIES
from app.schemas.metrics import (
    FitnessSummaryResponse,
    FTPTestRequest,
    PMCDataPoint,
    PMCResponse,
    PowerProfilePoint,
    PowerProfileResponse,
    RiderProfileScore,
    RideZonesResponse,
    RideZoneTime,
    WeeklyLoadPoint,
    WeeklyLoadResponse,
    ZoneBounds,
    ZonesResponse,
)
from app.services.metrics_service import (
    get_all_time_power_profile,
    get_current_fitness,
    get_ftp_history,
    get_pmc_data,
    get_recent_power_profile,
    get_ride_zone_distribution,
    get_weekly_training_load,
)
from app.services.profile_service import get_fitness_summary
from app.services.zone_service import get_zones

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/zones", response_model=ZonesResponse)
def get_user_zones(
    current_user: User = Depends(get_current_user),
):
    """Get current power and HR zones based on FTP."""
    if not current_user.ftp:
        raise BadRequestException(detail="Add your FTP first: Settings, then Profile.")

    zone_data = get_zones(current_user)
    power_zones = None
    hr_zones = None

    if zone_data["power_zones"]:
        power_zones = {
            k: ZoneBounds(**v) for k, v in zone_data["power_zones"].items()
        }
    if zone_data["hr_zones"]:
        hr_zones = {
            k: ZoneBounds(**v) for k, v in zone_data["hr_zones"].items()
        }

    return ZonesResponse(
        ftp=current_user.ftp,
        power_zones=power_zones,
        hr_zones=hr_zones,
    )


@router.get("/pmc", response_model=PMCResponse)
def get_pmc(
    start_date: date | None = None,
    end_date: date | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get Performance Management Chart data (CTL/ATL/TSB over time)."""
    metrics = get_pmc_data(db, current_user.id, start_date, end_date)
    fitness = get_current_fitness(db, current_user.id)

    data = [
        PMCDataPoint(
            date=m.date,
            tss=m.tss,
            ctl=m.ctl,
            atl=m.atl,
            tsb=m.tsb,
            ramp_rate=m.ramp_rate,
        )
        for m in metrics
    ]

    return PMCResponse(
        data=data,
        current_ctl=fitness["ctl"],
        current_atl=fitness["atl"],
        current_tsb=fitness["tsb"],
    )


@router.get("/power-profile", response_model=PowerProfileResponse)
def get_power_profile(
    days: int = Query(90, description="Number of days for current form (default 90). Use 0 for all-time only."),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get best efforts at standard durations. Returns current form (last N days) and all-time PBs."""
    ftp = current_user.ftp or 0
    weight = current_user.weight_kg
    labels = {v: k for k, v in POWER_PROFILE_DURATIONS.items()}

    # Always compute all-time
    all_time = get_all_time_power_profile(db, current_user.id)

    # Compute recent/current form if days > 0
    recent = get_recent_power_profile(db, current_user.id, days) if days > 0 else None

    points = []
    for duration in sorted(POWER_PROFILE_DURATIONS.values()):
        # Use recent as primary, fall back to all-time if no recent data
        if recent:
            entry = recent.get(duration, {"best_power": 0.0, "ride_id": None, "ride_date": None})
        else:
            entry = all_time.get(duration, {"best_power": 0.0, "ride_id": None, "ride_date": None})

        at_entry = all_time.get(duration, {"best_power": 0.0, "ride_id": None, "ride_date": None})

        power = entry["best_power"]
        wpkg = None
        if weight and weight > 0 and power > 0:
            wpkg = round(power / weight, 2)

        at_power = at_entry["best_power"]
        at_wpkg = None
        if weight and weight > 0 and at_power > 0:
            at_wpkg = round(at_power / weight, 2)

        points.append(PowerProfilePoint(
            duration_seconds=duration,
            duration_label=labels.get(duration, f"{duration}s"),
            best_power=power,
            watts_per_kg=wpkg,
            ride_id=str(entry["ride_id"]) if entry.get("ride_id") else None,
            ride_date=entry.get("ride_date"),
            all_time_power=at_power if at_power > 0 else None,
            all_time_watts_per_kg=at_wpkg,
            all_time_ride_date=at_entry.get("ride_date"),
        ))

    return PowerProfileResponse(
        points=points,
        ftp=ftp,
        w_per_kg=w_per_kg(ftp, weight) if ftp and weight else None,
        days=days if days > 0 else None,
    )


@router.get("/fitness-summary", response_model=FitnessSummaryResponse)
def get_fitness(
    include_power_profile: bool = True,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get comprehensive fitness summary: CTL/ATL/TSB, W/kg, rider type, profile scores."""
    summary = get_fitness_summary(db, current_user, include_power_profile=include_power_profile)

    # Convert profile_scores dict to list of RiderProfileScore for the response
    raw_scores = summary.pop("profile_scores", {})
    weight = current_user.weight_kg or 0
    scores_list = []
    for cat, info in RIDER_PROFILE_CATEGORIES.items():
        score_val = raw_scores.get(cat, 0.0)
        scores_list.append(RiderProfileScore(
            category=cat,
            label=info["label"],
            score=score_val,
        ))
    summary["profile_scores"] = scores_list

    return FitnessSummaryResponse(**summary)


@router.get("/weekly-load", response_model=WeeklyLoadResponse)
def get_weekly_load(
    weeks: int = Query(default=12, ge=4, le=52),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get weekly training load (TSS, ride count, duration) for the last N weeks."""
    data = get_weekly_training_load(db, current_user.id, weeks)
    return WeeklyLoadResponse(
        weeks=[WeeklyLoadPoint(**w) for w in data]
    )


@router.get("/rides/{ride_id}/zones", response_model=RideZonesResponse)
def get_ride_zones(
    ride_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get power zone distribution for a specific ride."""
    # Verify ride belongs to user
    ride = db.query(Ride).filter(
        Ride.id == ride_id, Ride.user_id == current_user.id
    ).first()
    if not ride:
        raise BadRequestException(detail="Ride not found")

    ftp = ride.ftp_at_time or current_user.ftp or 0
    if ftp <= 0:
        raise BadRequestException(detail="Add your FTP in Settings, then Profile, to see zones for this ride.")

    result = get_ride_zone_distribution(db, ride_id, ftp)
    return RideZonesResponse(
        ftp=result["ftp"],
        total_seconds=result["total_seconds"],
        zones=[RideZoneTime(**z) for z in result["zones"]],
    )


@router.get("/ftp-history")
def get_ftp_history_endpoint(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get FTP history over time from ride device data."""
    history = get_ftp_history(db, current_user.id)
    return {
        "history": history,
        "current_ftp": current_user.ftp,
    }


@router.post("/ftp-test")
def submit_ftp_test(
    body: FTPTestRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Submit a 20-minute FTP test result. Updates user FTP."""
    if body.twenty_min_avg_power <= 0:
        raise BadRequestException(detail="Enter your 20-minute average power in watts.")

    new_ftp = ftp_from_20min_test(body.twenty_min_avg_power)
    current_user.ftp = new_ftp
    db.commit()

    return {
        "new_ftp": new_ftp,
        "twenty_min_avg_power": body.twenty_min_avg_power,
        "message": f"FTP updated to {new_ftp}W",
    }
