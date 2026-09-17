"""Every ride, from any door, measured against the plan.

Before this, a planned session only counted as done when a ride was linked to
it, and linking happened on the Strava path and a manual upload but never on
the Wahoo path. Every Wahoo ride since the plan began was invisible to the
plan, so the coach read "zero of 39 completed" against a rider who had ridden
twelve of the last seventeen days (17 Sep 2026).

Three honest answers for any day inside the active plan:
  as prescribed   a ride linked to that day's session, close to the prescription
  deviated        linked, but a different type, intensity or length
  off-plan        a ride on a day with nothing planned, or a second ride
  missed          a planned session in the past with no ride against it

Source does not matter: Wahoo, a file upload, a Strava archive all land here.
"""

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.ride import Ride, RideData
from app.models.training import (
    PlanStatus,
    TrainingPhase,
    TrainingPlan,
    Workout,
    WorkoutStatus,
    WorkoutType,
)
from app.services.workout_assessment_service import WORKOUT_TYPE_IF_BAND, score_execution

logger = logging.getLogger(__name__)

# Below this the ride is recorded as done but not as prescribed.
AS_PRESCRIBED_SCORE = 7.0

_TITLE_TYPES = (
    ("vo2", WorkoutType.vo2max.value),
    ("sprint", WorkoutType.sprint.value),
    ("threshold", WorkoutType.threshold.value),
    ("sweet spot", WorkoutType.sweet_spot.value),
    ("sweetspot", WorkoutType.sweet_spot.value),
    ("tempo", WorkoutType.tempo.value),
    ("recovery", WorkoutType.recovery.value),
    ("easy", WorkoutType.recovery.value),
    ("spin", WorkoutType.recovery.value),
    ("endurance", WorkoutType.endurance.value),
    ("group", WorkoutType.endurance.value),
)


def infer_ride_type(ride: Ride) -> str | None:
    """What kind of session this actually was.

    The classifier's title leads: it read the file's structure, so a VO2
    session with long recoveries is still VO2. Whole-ride intensity factor is
    the fallback, because intervals average down over their recoveries (two
    VO2 rides read as tempo by IF alone, 17 Sep 2026)."""
    title = (ride.title or "").lower()
    for needle, wtype in _TITLE_TYPES:
        if needle in title:
            return wtype
    if ride.intensity_factor:
        bands = {k: v for k, v in WORKOUT_TYPE_IF_BAND.items() if k != WorkoutType.rest.value}
        return min(bands, key=lambda k: abs(ride.intensity_factor - bands[k][0]))
    return None


def is_indoor(db: Session, ride: Ride) -> bool:
    """No GPS in the file means the turbo. Used so the coach can say where."""
    has_fix = (
        db.query(RideData.id)
        .filter(RideData.ride_id == ride.id, RideData.latitude.isnot(None))
        .first()
    )
    return has_fix is None


def _active_plan(db: Session, user_id: str) -> TrainingPlan | None:
    return (
        db.query(TrainingPlan)
        .filter(TrainingPlan.user_id == user_id, TrainingPlan.status == PlanStatus.active)
        .order_by(TrainingPlan.start_date.desc())
        .first()
    )


def _planned_on(db: Session, plan: TrainingPlan, day: date) -> list[Workout]:
    return (
        db.query(Workout)
        .join(TrainingPhase, Workout.phase_id == TrainingPhase.id)
        .filter(TrainingPhase.plan_id == plan.id, Workout.scheduled_date == day)
        .order_by(Workout.sort_order)
        .all()
    )


def link_ride(db: Session, ride: Ride) -> Workout | None:
    """Attach a ride to the session it most plausibly was, and score it.

    Same day, active plan only, not already satisfied. With more than one
    candidate the type wins, then the closest intensity band. A recovery spin
    and a VO2 session on the same day no longer get confused. The score is
    the deterministic one (no model call), so this is safe to run on every
    import, including a thousand-ride backfill.
    """
    if not ride.ride_date or ride.workout_id:
        return None
    plan = _active_plan(db, ride.user_id)
    if plan is None:
        return None
    day = ride.ride_date.date() if hasattr(ride.ride_date, "date") else ride.ride_date
    if not (plan.start_date <= day <= plan.end_date):
        return None
    candidates = [
        w for w in _planned_on(db, plan, day)
        if w.actual_ride_id is None
        and w.status in (WorkoutStatus.planned, WorkoutStatus.modified)
        and w.workout_type != WorkoutType.rest.value
    ]
    if not candidates:
        return None

    actual_type = infer_ride_type(ride)
    if len(candidates) > 1 and actual_type:
        typed = [w for w in candidates if str(w.workout_type).split(".")[-1] == actual_type]
        if typed:
            candidates = typed
        elif ride.intensity_factor:
            candidates.sort(
                key=lambda w: abs(
                    ride.intensity_factor
                    - WORKOUT_TYPE_IF_BAND.get(str(w.workout_type).split(".")[-1], (0.7, 0))[0]
                )
            )
    workout = candidates[0]

    workout.actual_ride_id = ride.id
    workout.status = WorkoutStatus.completed
    workout.execution_score = score_execution(workout, ride)["score"]
    ride.workout_id = workout.id
    db.commit()
    logger.info(
        "Linked ride %s to workout %s (%s on %s, score %s)",
        ride.id, workout.id, workout.title, day, workout.execution_score,
    )
    return workout


def classify_unlinked_rides(db: Session, user_id: str, since: date | None = None) -> dict:
    """Run link_ride over every ride in the active plan's window that has not
    been matched yet. Idempotent. Returns counts."""
    plan = _active_plan(db, user_id)
    if plan is None:
        return {"linked": 0, "checked": 0, "reason": "no active plan"}
    start = max(plan.start_date, since) if since else plan.start_date
    rides = (
        db.query(Ride)
        .filter(
            Ride.user_id == user_id,
            Ride.workout_id.is_(None),
            Ride.ride_date >= datetime.combine(start, datetime.min.time()),
            Ride.ride_date <= datetime.combine(plan.end_date, datetime.max.time()),
        )
        .order_by(Ride.ride_date)
        .all()
    )
    linked = sum(1 for r in rides if link_ride(db, r) is not None)
    return {"linked": linked, "checked": len(rides)}


def _deviation(workout: Workout, ride: Ride) -> str:
    """One plain sentence on how the ride differed from the prescription."""
    parts = []
    planned_type = str(workout.workout_type).split(".")[-1]
    actual_type = infer_ride_type(ride)
    if actual_type and actual_type != planned_type:
        parts.append(f"{planned_type.replace('_', ' ')} planned, rode {actual_type.replace('_', ' ')}")
    if workout.planned_if and ride.intensity_factor:
        d = ride.intensity_factor - workout.planned_if
        if abs(d) >= 0.05:
            parts.append(f"IF {ride.intensity_factor:.2f} vs {workout.planned_if:.2f} planned")
    if workout.planned_duration_seconds and (ride.moving_time_seconds or ride.duration_seconds):
        actual = ride.moving_time_seconds or ride.duration_seconds
        pct = (actual - workout.planned_duration_seconds) / workout.planned_duration_seconds * 100
        if abs(pct) >= 20:
            parts.append(f"{round(actual / 60)} min vs {round(workout.planned_duration_seconds / 60)} planned")
    if workout.planned_tss and ride.tss:
        pct = (ride.tss - workout.planned_tss) / workout.planned_tss * 100
        if abs(pct) >= 25:
            parts.append(f"TSS {round(ride.tss)} vs {round(workout.planned_tss)} planned")
    return "; ".join(parts) or "close to the prescription"


def compliance_summary(
    db: Session, user_id: str, start: date, end: date, include_rides: bool = True
) -> dict | None:
    """The three-way read for a date window inside the active plan.

    Returns None when there is no active plan (nothing to comply with)."""
    plan = _active_plan(db, user_id)
    if plan is None:
        return None
    start = max(start, plan.start_date)
    end = min(end, plan.end_date)
    if start > end:
        return None

    workouts = (
        db.query(Workout)
        .join(TrainingPhase, Workout.phase_id == TrainingPhase.id)
        .filter(
            TrainingPhase.plan_id == plan.id,
            Workout.scheduled_date >= start,
            Workout.scheduled_date <= end,
        )
        .order_by(Workout.scheduled_date, Workout.sort_order)
        .all()
    )
    rides = (
        db.query(Ride)
        .filter(
            Ride.user_id == user_id,
            Ride.ride_date >= datetime.combine(start, datetime.min.time()),
            Ride.ride_date <= datetime.combine(end, datetime.max.time()),
        )
        .order_by(Ride.ride_date)
        .all()
    )
    rides_by_id = {r.id: r for r in rides}
    linked_ride_ids = {w.actual_ride_id for w in workouts if w.actual_ride_id}

    as_prescribed, deviated, missed, skipped = [], [], [], []
    for w in workouts:
        wtype = str(w.workout_type).split(".")[-1]
        if w.actual_ride_id and w.actual_ride_id in rides_by_id:
            r = rides_by_id[w.actual_ride_id]
            score = w.execution_score if w.execution_score is not None else score_execution(w, r)["score"]
            entry = {
                "date": str(w.scheduled_date), "planned": w.title, "type": wtype,
                "score": score, "ride_id": r.id,
            }
            if score >= AS_PRESCRIBED_SCORE:
                as_prescribed.append(entry)
            else:
                deviated.append({**entry, "how": _deviation(w, r)})
        elif w.status == WorkoutStatus.skipped:
            skipped.append({"date": str(w.scheduled_date), "planned": w.title, "type": wtype})
        elif wtype == WorkoutType.rest.value:
            continue
        elif w.scheduled_date < date.today():
            missed.append({"date": str(w.scheduled_date), "planned": w.title, "type": wtype})

    rest_days = {w.scheduled_date for w in workouts if str(w.workout_type).split(".")[-1] == WorkoutType.rest.value}
    off_plan = []
    for r in rides:
        if r.id in linked_ride_ids:
            continue
        day = r.ride_date.date() if hasattr(r.ride_date, "date") else r.ride_date
        off_plan.append({
            "date": str(day), "title": r.title, "type": infer_ride_type(r),
            "tss": round(r.tss) if r.tss else None,
            "if": round(r.intensity_factor, 2) if r.intensity_factor else None,
            "on_rest_day": day in rest_days,
            "ride_id": r.id,
        })

    total_planned = len([w for w in workouts if str(w.workout_type).split(".")[-1] != WorkoutType.rest.value])
    summary = {
        "plan": plan.name,
        "window": {"start": str(start), "end": str(end)},
        "planned_sessions": total_planned,
        "as_prescribed": len(as_prescribed),
        "deviated": len(deviated),
        "missed": len(missed),
        "skipped": len(skipped),
        "off_plan_rides": len(off_plan),
        "off_plan_on_rest_days": sum(1 for o in off_plan if o["on_rest_day"]),
        "note": (
            "as_prescribed and deviated are rides matched to a planned session; "
            "off_plan_rides happened on days with nothing planned or as a second ride. "
            "Rides count wherever they were done: Wahoo, upload or archive."
        ),
    }
    if include_rides:
        summary["deviations"] = deviated[-8:]
        summary["off_plan"] = off_plan[-8:]
        summary["missed_sessions"] = missed[-8:]
    return summary


def week_window(today: date) -> tuple[date, date]:
    start = today - timedelta(days=today.weekday())
    return start, today
