"""Every ride meets the plan, wherever it came from."""

from datetime import date, datetime, timedelta

from app.models.onboarding import GoalEvent  # noqa: F401  (registers FK targets)
from app.models.ride import Ride, RideSource
from app.models.training import (
    PeriodizationModel, PhaseType, PlanStatus, TrainingPhase, TrainingPlan, Workout,
    WorkoutStatus, WorkoutType,
)
from app.models.user import User
from app.services import plan_compliance_service as pcs
from app.services.memory_service import _age_label

TODAY = date.today()


def _rider(db):
    u = User(email="r@example.com", hashed_password="x", full_name="Rider", ftp=290)
    db.add(u); db.commit(); return u


def _plan(db, user, start, end):
    plan = TrainingPlan(user_id=user.id, name="Test plan", start_date=start, end_date=end,
                        status=PlanStatus.active, periodization_model=PeriodizationModel.traditional)
    db.add(plan); db.flush()
    phase = TrainingPhase(plan_id=plan.id, start_date=start, end_date=end,
                          phase_type=PhaseType.build, sort_order=1)
    db.add(phase); db.commit()
    return plan, phase


def _workout(db, user, phase, day, wtype, title, dur=3600, pif=None, tss=None, sort=0):
    w = Workout(
        phase_id=phase.id, user_id=user.id, scheduled_date=day, title=title,
        workout_type=wtype, planned_duration_seconds=dur, planned_if=pif, planned_tss=tss,
        status=WorkoutStatus.planned, sort_order=sort,
    )
    db.add(w); db.commit(); return w


def _ride(db, user, day, title, if_, tss, dur=3600):
    r = Ride(
        user_id=user.id, source=RideSource.wahoo, title=title,
        ride_date=datetime.combine(day, datetime.min.time()) + timedelta(hours=7),
        duration_seconds=dur, moving_time_seconds=dur, intensity_factor=if_, tss=tss,
    )
    db.add(r); db.commit(); return r


def test_infer_type_title_first_then_intensity():
    # Intervals average down over recoveries: the structural title must win.
    assert pcs.infer_ride_type(Ride(intensity_factor=0.80, title="VO2 Max Intervals")) == "vo2max"
    assert pcs.infer_ride_type(Ride(intensity_factor=1.04, title="Ride")) == "vo2max"
    assert pcs.infer_ride_type(Ride(intensity_factor=0.56, title=None)) == "recovery"
    assert pcs.infer_ride_type(Ride(intensity_factor=None, title="Easy Spin")) == "recovery"
    assert pcs.infer_ride_type(Ride(intensity_factor=None, title="Tempo Endurance")) == "tempo"
    assert pcs.infer_ride_type(Ride(intensity_factor=None, title="Ride")) is None


def test_ride_links_to_same_day_session_and_scores(db_session):
    user = _rider(db_session)
    plan, phase = _plan(db_session, user, TODAY - timedelta(days=10), TODAY + timedelta(days=20))
    w = _workout(db_session, user, phase, TODAY - timedelta(days=1), WorkoutType.tempo.value, "Tempo Blocks", pif=0.82, tss=80)
    r = _ride(db_session, user, TODAY - timedelta(days=1), "Tempo Ride", 0.83, 78)

    linked = pcs.link_ride(db_session, r)

    assert linked is not None and linked.id == w.id
    assert w.status == WorkoutStatus.completed and w.actual_ride_id == r.id
    assert r.workout_id == w.id
    assert w.execution_score >= pcs.AS_PRESCRIBED_SCORE


def test_type_wins_when_two_sessions_share_a_day(db_session):
    """A recovery spin and a VO2 session planned on one day: the hard ride goes
    to the hard session, not to whichever comes first."""
    user = _rider(db_session)
    plan, phase = _plan(db_session, user, TODAY - timedelta(days=10), TODAY + timedelta(days=20))
    spin = _workout(db_session, user, phase, TODAY, WorkoutType.recovery.value, "Recovery Spin", sort=0)
    vo2 = _workout(db_session, user, phase, TODAY, WorkoutType.vo2max.value, "VO2 5x4", sort=1)
    r = _ride(db_session, user, TODAY, "VO2 Max Intervals", 1.05, 74)

    linked = pcs.link_ride(db_session, r)

    assert linked.id == vo2.id
    assert spin.status == WorkoutStatus.planned


def test_rides_outside_the_plan_or_on_rest_days_do_not_link(db_session):
    user = _rider(db_session)
    plan, phase = _plan(db_session, user, TODAY - timedelta(days=10), TODAY + timedelta(days=20))
    rest = _workout(db_session, user, phase, TODAY, WorkoutType.rest.value, "Rest")
    r_rest = _ride(db_session, user, TODAY, "Easy Spin", 0.55, 12)
    r_before = _ride(db_session, user, TODAY - timedelta(days=30), "Old ride", 0.7, 50)

    assert pcs.link_ride(db_session, r_rest) is None
    assert pcs.link_ride(db_session, r_before) is None
    assert rest.status == WorkoutStatus.planned


def test_three_way_summary(db_session):
    user = _rider(db_session)
    start = TODAY - timedelta(days=6)
    plan, phase = _plan(db_session, user, start, TODAY + timedelta(days=20))
    # Day 1: prescribed and ridden close.
    _workout(db_session, user, phase, start, WorkoutType.endurance.value, "Endurance Z2", pif=0.68, tss=60)
    _ride(db_session, user, start, "Endurance Ride", 0.69, 58)
    # Day 2: recovery planned, VO2 ridden -> deviated.
    _workout(db_session, user, phase, start + timedelta(days=1), WorkoutType.recovery.value, "Recovery Spin", pif=0.55, tss=20)
    _ride(db_session, user, start + timedelta(days=1), "VO2 Max Intervals", 1.05, 74)
    # Day 3: nothing planned, rode anyway -> off-plan.
    _ride(db_session, user, start + timedelta(days=2), "Tempo Endurance", 0.85, 100)
    # Day 4: planned, no ride -> missed.
    _workout(db_session, user, phase, start + timedelta(days=3), WorkoutType.threshold.value, "Threshold 2x20", pif=0.96, tss=90)
    # Day 5: rest day with a ride -> off-plan on rest day.
    _workout(db_session, user, phase, start + timedelta(days=4), WorkoutType.rest.value, "Rest")
    _ride(db_session, user, start + timedelta(days=4), "Easy Spin", 0.55, 12)

    assert pcs.classify_unlinked_rides(db_session, user.id)["linked"] == 2
    cs = pcs.compliance_summary(db_session, user.id, start, TODAY)

    assert cs["planned_sessions"] == 3
    assert cs["as_prescribed"] == 1
    assert cs["deviated"] == 1 and "recovery planned, rode vo2max" in cs["deviations"][0]["how"]
    assert cs["missed"] == 1
    assert cs["off_plan_rides"] == 2 and cs["off_plan_on_rest_days"] == 1
    assert cs["off_plan"][0]["type"] == "tempo"


def test_no_active_plan_means_nothing_to_comply_with(db_session):
    user = _rider(db_session)
    _ride(db_session, user, TODAY, "Ride", 0.7, 50)
    assert pcs.compliance_summary(db_session, user.id, TODAY - timedelta(days=7), TODAY) is None


def test_memory_age_labels():
    assert _age_label(0.5) == "today"
    assert _age_label(3) == "3 days ago"
    assert _age_label(46) == "6 weeks ago"
    assert _age_label(95) == "3 months ago"
