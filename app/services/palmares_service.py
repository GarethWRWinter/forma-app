"""Palmarès — the rider's trophy cabinet.

Tiered per the PRD: the Cabinet holds conquered goals (attempts honoured)
and true records; the Log holds segment PRs and a small set of
coach-voiced milestones. Everything aggregates from existing tables.
"""

import logging
from datetime import date, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.onboarding import GoalEvent
from app.models.ride import Ride
from app.models.segment import SegmentEffort, StravaSegment
from app.models.user import User

logger = logging.getLogger(__name__)

# Coach-voiced milestones: a SMALL set, said once, in Forma's register.
_KM_MILESTONES = [1000, 2500, 5000, 10000, 25000, 50000]
_RIDE_MILESTONES = [50, 100, 250, 500, 1000]


def _km_line(km: int) -> str:
    return f"{km:,} kilometres logged together. Every one of them remembered."


def _ride_line(n: int) -> str:
    return f"Ride number {n:,}. The habit is the achievement."


def get_palmares(db: Session, user: User) -> dict:
    today = date.today()

    # ── The Cabinet: goals raced (attempts honoured) ──
    goals = (
        db.query(GoalEvent)
        .filter(GoalEvent.user_id == user.id)
        .order_by(GoalEvent.event_date.desc())
        .all()
    )
    raced = []
    for g in goals:
        is_past = g.event_date is not None and g.event_date <= today
        if not is_past and g.status == "upcoming":
            continue
        raced.append({
            "id": g.id,
            "name": g.event_name,
            "date": str(g.event_date),
            "year": g.event_date.year if g.event_date else None,
            "priority": str(g.priority).split(".")[-1],
            "event_type": str(g.event_type).split(".")[-1],
            "status": str(g.status).split(".")[-1],
            "achieved": str(g.status).split(".")[-1] == "completed",
            "satisfaction": g.overall_satisfaction,
            "assessed": g.assessment_completed_at is not None,
        })

    # ── Records ──
    from app.services.metrics_service import get_all_time_power_profile, get_ftp_history

    records: list[dict] = []
    try:
        ftp_hist = get_ftp_history(db, user.id)
        if user.ftp:
            first = ftp_hist[0]["ftp"] if ftp_hist else None
            delta = user.ftp - first if first else None
            records.append({
                "key": "ftp",
                "label": "FTP",
                "value": f"{user.ftp}",
                "unit": "w",
                "detail": (f"+{delta}w since the start" if delta and delta > 0 else "current"),
            })
    except Exception:
        logger.exception("FTP record failed")

    try:
        profile = get_all_time_power_profile(db, user.id)
        names = {5: "5 second", 60: "1 minute", 300: "5 minute", 1200: "20 minute"}
        for secs, label in names.items():
            best = profile.get(secs)
            if best and best.get("best_power"):
                records.append({
                    "key": f"p{secs}",
                    "label": f"Best {label} power",
                    "value": f"{round(best['best_power'])}",
                    "unit": "w",
                    "detail": str(best.get("ride_date", ""))[:10],
                })
    except Exception:
        logger.exception("Power records failed")

    try:
        longest = (
            db.query(Ride)
            .filter(Ride.user_id == user.id, Ride.distance_meters.isnot(None))
            .order_by(Ride.distance_meters.desc())
            .first()
        )
        if longest and longest.distance_meters:
            records.append({
                "key": "longest",
                "label": "Longest ride",
                "value": f"{longest.distance_meters / 1000:.0f}",
                "unit": "km",
                "detail": longest.forma_title or longest.title or str(longest.ride_date)[:10],
            })
        biggest = (
            db.query(Ride)
            .filter(Ride.user_id == user.id, Ride.tss.isnot(None))
            .order_by(Ride.tss.desc())
            .first()
        )
        if biggest and biggest.tss:
            records.append({
                "key": "biggest",
                "label": "Biggest day",
                "value": f"{round(biggest.tss)}",
                "unit": "TSS",
                "detail": biggest.forma_title or biggest.title or str(biggest.ride_date)[:10],
            })
    except Exception:
        logger.exception("Ride records failed")

    # ── Totals + milestones ──
    totals_row = (
        db.query(
            func.count(Ride.id),
            func.coalesce(func.sum(Ride.distance_meters), 0),
            func.coalesce(func.sum(Ride.duration_seconds), 0),
        )
        .filter(Ride.user_id == user.id)
        .first()
    )
    ride_count = int(totals_row[0] or 0)
    total_km = int((totals_row[1] or 0) / 1000)
    total_hours = int((totals_row[2] or 0) / 3600)
    totals = {"rides": ride_count, "km": total_km, "hours": total_hours}

    milestones = []
    for km in _KM_MILESTONES:
        if total_km >= km:
            milestones.append({"key": f"km{km}", "text": _km_line(km)})
    for n in _RIDE_MILESTONES:
        if ride_count >= n:
            milestones.append({"key": f"r{n}", "text": _ride_line(n)})
    milestones = milestones[-3:]  # only the most recent few — restraint

    # ── The Log: segment PRs (efforts link to the user through the ride) ──
    prs = []
    try:
        rows = (
            db.query(SegmentEffort, StravaSegment, Ride.ride_date)
            .join(StravaSegment, SegmentEffort.segment_id == StravaSegment.id)
            .join(Ride, SegmentEffort.ride_id == Ride.id)
            .filter(Ride.user_id == user.id, SegmentEffort.pr_rank == 1)
            .order_by(Ride.ride_date.desc())
            .limit(12)
            .all()
        )
        for eff, seg, ride_date in rows:
            prs.append({
                "name": seg.name,
                "time_seconds": eff.elapsed_time_seconds,
                "date": str(ride_date)[:10] if ride_date else None,
                "distance_m": seg.distance_meters,
            })
    except Exception:
        logger.exception("Segment PRs failed")

    return {
        "goals": raced,
        "records": records,
        "totals": totals,
        "milestones": milestones,
        "segment_prs": prs,
        "generated_at": datetime.utcnow().isoformat(),
    }
