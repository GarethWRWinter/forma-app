"""A session the coach books lands where the rider can see it.

16 Oct 2026, a Friday: Gareth asked Forma for a structured session and heard
"Done. It's booked for Sunday the 19th". Nothing reached his plan page or
Today. add_workout wrote the session with no phase, and the calendar inner
joined through phases to the active plan, so every session the coach had
ever booked was invisible. His plan (Sa Calobra, 4 Oct) had also ended. The
prompt carried only an ISO date, and the 19th was a Monday. The session had
no steps, so nothing could run it in Ride mode or load it onto a head unit.
"""

import asyncio
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api.v1.training import get_workout_detail, list_workouts
from app.core import forma_core
from app.models.chat import ChatSession
from app.models.onboarding import GoalEvent  # noqa: F401  (registers FK targets)
from app.models.plan_proposal import PlanProposal
from app.models.ride import Ride, RideSource
from app.models.training import (
    PeriodizationModel, PhaseType, PlanStatus, TrainingPhase, TrainingPlan, Workout,
    WorkoutStatus, WorkoutStep, WorkoutType,
)
from app.models.user import User
from app.services import (
    coach_service, onboarding_service, plan_compliance_service, plan_service, season_service,
)
from app.services.coach_service import _build_rider_context, _calendar_block, _execute_tool, _run_tool
from app.services.plan_review_service import apply_proposal
from app.services.plan_service import build_steps, get_workouts_by_date, steps_load

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def _spoken(d: date) -> str:
    """The day in words, worked out independently of the code under test."""
    return f"{DAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"


class _Friday(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 16)


@pytest.fixture
def friday(monkeypatch):
    """Friday 16 October 2026, the day of the incident."""
    for module in (coach_service, plan_service, plan_compliance_service, season_service, onboarding_service):
        monkeypatch.setattr(module, "date", _Friday)
    return date(2026, 10, 16)


def _gareth(db, ftp=300) -> User:
    user = User(email="gareth@example.com", hashed_password="x", full_name="Gareth Winter", ftp=ftp)
    db.add(user)
    db.commit()
    return user


def _plan(db, user, start, end, status=PlanStatus.active) -> tuple[TrainingPlan, TrainingPhase]:
    plan = TrainingPlan(
        user_id=user.id, name="Plan for Sa Calobra Mallorca sub 30", start_date=start,
        end_date=end, status=status, periodization_model=PeriodizationModel.traditional,
    )
    db.add(plan)
    db.flush()
    phase = TrainingPhase(plan_id=plan.id, start_date=start, end_date=end,
                          phase_type=PhaseType.peak, sort_order=0)
    db.add(phase)
    db.commit()
    return plan, phase


def _session(db, user, phase, day, wtype=WorkoutType.endurance, status=WorkoutStatus.planned) -> Workout:
    w = Workout(
        phase_id=phase.id if phase else None, user_id=user.id, scheduled_date=day,
        title="Plan session", workout_type=wtype, planned_duration_seconds=3600,
        status=status,
    )
    db.add(w)
    db.commit()
    return w


def _ride(db, user, day, title="Ride", if_=0.95, tss=80) -> Ride:
    r = Ride(
        user_id=user.id, source=RideSource.wahoo, title=title,
        ride_date=datetime.combine(day, datetime.min.time()) + timedelta(hours=8),
        duration_seconds=3600, moving_time_seconds=3600, intensity_factor=if_, tss=tss,
    )
    db.add(r)
    db.commit()
    return r


# Two 12 minute reps on a climb, shaped like his PB file: a 299W opener, a
# 295W hold and a 309W finish, cadence in the low 90s. A rep with structure
# inside it is written out in full.
PB_SHAPE = [
    {"step_type": "warmup", "duration_seconds": 900, "power_low_watts": 150, "power_high_watts": 230},
    {"step_type": "steady_state", "duration_seconds": 120, "power_watts": 299, "cadence": 92},
    {"step_type": "steady_state", "duration_seconds": 480, "power_watts": 295, "cadence": 92},
    {"step_type": "steady_state", "duration_seconds": 120, "power_watts": 309, "cadence": 92},
    {"step_type": "interval_off", "duration_seconds": 600, "power_watts": 150},
    {"step_type": "steady_state", "duration_seconds": 120, "power_watts": 299, "cadence": 92},
    {"step_type": "steady_state", "duration_seconds": 480, "power_watts": 295, "cadence": 92},
    {"step_type": "steady_state", "duration_seconds": 120, "power_watts": 309, "cadence": 92},
    {"step_type": "cooldown", "duration_seconds": 600, "power_watts": 140},
]


def _book(db, user, day, **extra) -> str:
    return _execute_tool(db, user, "add_workout", {
        "scheduled_date": day.isoformat(),
        "title": "PB-shape climb reps",
        "description": "Two 12 minute reps on the local climb.",
        "workout_type": "threshold",
        **extra,
    })


def _ids(rows) -> set[str]:
    return {r.id for r in rows}


# ── The incident ─────────────────────────────────────────────────────────────


def test_a_session_booked_after_the_plan_has_ended_shows_everywhere(db_session, friday):
    gareth = _gareth(db_session)
    # The Sa Calobra plan: still active, its last phase ended on 4 Oct.
    _, phase = _plan(db_session, gareth, date(2026, 8, 24), date(2026, 10, 4))
    _session(db_session, gareth, phase, date(2026, 10, 1))
    monday = date(2026, 10, 19)

    result = _book(db_session, gareth, monday, steps=PB_SHAPE)

    # The coach is told the real day, in words, and what the rider can do with it.
    assert result.startswith("Booked.")
    assert "Monday 19 October" in result and "2026-10-19" in result
    assert "Sunday" not in result
    assert "Ride mode" in result and ".FIT" in result
    assert "main set: 24 min of work at 295 to 309W" in result

    booked = db_session.query(Workout).filter(Workout.title == "PB-shape climb reps").one()
    assert booked.phase_id is None
    assert booked.id in result

    # The plan page's week, Today on that morning, and the session page.
    week = list_workouts(target_date=None, week=monday, current_user=gareth, db=db_session)
    assert booked.id in {w.id for w in week}
    that_day = list_workouts(target_date=monday, week=None, current_user=gareth, db=db_session)
    assert [w.id for w in that_day] == [booked.id]
    assert that_day[0].title in result  # the name the app shows is the name the coach uses
    detail = get_workout_detail(booked.id, current_user=gareth, db=db_session)
    assert len(detail.steps) == len(PB_SHAPE)

    # The coach's own picture of the fortnight.
    context = json.loads(_build_rider_context(db_session, gareth))
    entry = next(e for e in context["this_week"] if e["id"] == booked.id)
    assert entry["day"] == "Monday 19 October"
    assert entry["structured"] is True


def test_steps_are_stored_as_fractions_of_ftp_and_set_the_load(db_session, friday):
    gareth = _gareth(db_session, ftp=300)

    _book(db_session, gareth, date(2026, 10, 19), steps=PB_SHAPE, planned_duration_seconds=5400)

    booked = db_session.query(Workout).one()
    steps = sorted(booked.steps, key=lambda s: s.step_order)
    assert [s.step_order for s in steps] == list(range(9))
    assert steps[1].power_target_pct == pytest.approx(299 / 300, abs=1e-4)
    assert steps[3].power_target_pct == pytest.approx(309 / 300, abs=1e-4)
    assert steps[1].cadence_target == 92
    # The warmup range becomes a ramp from 50% to 77% with its middle as target.
    assert (steps[0].power_low_pct, steps[0].power_high_pct) == (0.5, pytest.approx(0.7667, abs=1e-4))
    assert steps[0].power_target_pct == pytest.approx(190 / 300, abs=1e-4)
    # The steps total 59 minutes, so the 90 the coach guessed gives way.
    assert booked.planned_duration_seconds == 3540
    seconds, tss, intensity = steps_load([
        {k: getattr(s, k) for k in ("step_type", "duration_seconds", "power_target_pct",
                                    "power_low_pct", "power_high_pct", "repeat_count")}
        for s in steps
    ])
    assert booked.planned_tss == tss and booked.planned_if == intensity
    assert 50 < tss < 80 and 0.75 < intensity < 0.9


def test_a_rider_with_no_plan_at_all_still_gets_the_session(db_session, friday):
    gareth = _gareth(db_session)
    saturday = date(2026, 10, 17)

    result = _book(db_session, gareth, saturday)

    assert "Saturday 17 October" in result
    booked = db_session.query(Workout).one()
    assert _ids(list_workouts(target_date=saturday, week=None, current_user=gareth, db=db_session)) == {booked.id}
    assert booked.id in _ids(list_workouts(target_date=None, week=date(2026, 10, 12), current_user=gareth, db=db_session))
    context = json.loads(_build_rider_context(db_session, gareth))
    assert booked.id in {e["id"] for e in context["this_week"]}


def test_a_session_booked_today_says_it_is_on_today_now(db_session, friday):
    gareth = _gareth(db_session)
    result = _book(db_session, gareth, friday)
    assert "Friday 16 October" in result and "on Today now" in result


def test_a_second_session_that_day_is_not_promised_on_today(db_session, friday):
    """Today shows the first session of a day, so the coach must not say the
    second will be there."""
    gareth = _gareth(db_session)
    _, phase = _plan(db_session, gareth, date(2026, 10, 1), date(2026, 12, 1))
    _session(db_session, gareth, phase, date(2026, 10, 19))

    result = _book(db_session, gareth, date(2026, 10, 19))

    assert "second session that day" in result and "will be on Today" not in result


def test_without_an_ftp_ride_mode_is_not_offered(db_session, friday):
    rider = _gareth(db_session, ftp=None)
    result = _book(db_session, rider, date(2026, 10, 20), steps=[
        {"step_type": "interval_on", "duration_seconds": 900, "power_pct": 90, "repeat_count": 3},
        {"step_type": "interval_off", "duration_seconds": 300, "power_pct": 55},
    ])
    assert "3 × 15 min at 90% of FTP" in result
    assert "FTP, which is not set yet" in result and "can start it" not in result


def test_a_session_with_no_steps_is_never_offered_to_ride_mode(db_session, friday):
    gareth = _gareth(db_session)
    result = _book(db_session, gareth, date(2026, 10, 19))
    assert "no structured steps" in result and "Do not say it can be" in result
    booked = db_session.query(Workout).one()
    # It still carries an intensity and a load, so a ride can be scored against it.
    assert booked.planned_if and booked.planned_if > 0.8


def test_the_calendar_shows_booked_and_active_sessions_and_hides_cancelled_ones(db_session):
    rider = _gareth(db_session)
    day = date(2026, 10, 19)
    _, live = _plan(db_session, rider, date(2026, 10, 1), date(2026, 12, 1))
    _, dead = _plan(db_session, rider, date(2026, 9, 1), date(2026, 12, 1), status=PlanStatus.cancelled)
    active = _session(db_session, rider, live, day)
    booked = _session(db_session, rider, None, day)
    _session(db_session, rider, dead, day)
    someone_else = User(email="other@example.com", hashed_password="x")
    db_session.add(someone_else)
    db_session.commit()
    _session(db_session, someone_else, None, day)

    assert _ids(get_workouts_by_date(db_session, rider.id, target_date=day)) == {active.id, booked.id}


# ── Nothing booked, nothing claimed ──────────────────────────────────────────


@pytest.mark.parametrize("asked", ["2026-10-15", "2026-10-31", "next Monday", "2026-02-30"])
def test_a_date_outside_the_fortnight_books_nothing(db_session, friday, asked):
    gareth = _gareth(db_session)

    result = _execute_tool(db_session, gareth, "add_workout", {
        "scheduled_date": asked, "title": "Reps", "workout_type": "threshold",
    })

    assert result.startswith("Error: nothing was booked.")
    assert "2026-10-16 to 2026-10-30" in result
    assert db_session.query(Workout).count() == 0


def test_bad_steps_book_nothing(db_session, friday):
    gareth = _gareth(db_session)
    result = _book(db_session, gareth, date(2026, 10, 19), steps=[
        {"step_type": "interval_on", "duration_seconds": 300, "power_pct": 110, "repeat_count": 5},
    ])
    assert result.startswith("Error: nothing was booked.") and "interval_off" in result
    assert db_session.query(Workout).count() == 0


def test_a_session_the_calendar_would_not_show_is_rolled_back(db_session, friday, monkeypatch):
    gareth = _gareth(db_session)
    monkeypatch.setattr(coach_service, "get_workouts_by_date", lambda *a, **k: [])

    result = _book(db_session, gareth, date(2026, 10, 19), steps=PB_SHAPE)

    assert result.startswith("Error: nothing was booked.") and "do not say it is booked" in result
    assert db_session.query(Workout).count() == 0
    assert db_session.query(WorkoutStep).count() == 0


def test_a_tool_that_crashes_saves_nothing_and_says_so(db_session, friday, monkeypatch):
    gareth = _gareth(db_session)

    def half_done(db, user, name, tool_input):
        db.add(Workout(user_id=user.id, scheduled_date=date(2026, 10, 19), title="Half",
                       workout_type=WorkoutType.threshold))
        db.flush()
        raise RuntimeError("database went away")

    monkeypatch.setattr(coach_service, "_execute_tool", half_done)

    result = _run_tool(db_session, gareth, "add_workout", {})

    assert result.startswith("Error: that change failed and nothing was saved.")
    assert db_session.query(Workout).count() == 0


def test_the_prompt_calendar_names_every_weekday():
    block = _calendar_block(date(2026, 10, 16))

    assert "Today is Friday 16 October 2026 (2026-10-16)." in block
    assert "- 2026-10-18: Sunday 18 October" in block
    assert "- 2026-10-19: Monday 19 October" in block
    assert "- 2026-10-30: Friday 30 October" in block
    assert "2026-10-31" not in block


# ── The steps ────────────────────────────────────────────────────────────────


def test_build_steps_reads_repeats_the_way_ride_mode_does():
    steps = build_steps([
        {"step_type": "warmup", "duration_seconds": 600, "power_pct": 60},
        {"step_type": "interval_on", "duration_seconds": 720, "power_pct": 100, "repeat_count": 2},
        {"step_type": "interval_off", "duration_seconds": 300, "power_pct": 50},
        {"step_type": "cooldown", "duration_seconds": 300, "power_pct": 50},
    ], ftp=None)

    assert steps[1]["repeat_count"] == 2 and steps[2]["repeat_count"] is None
    seconds, tss, intensity = steps_load(steps)
    # 10 min warmup, then on, off, on, off, then 5 min cooldown.
    assert seconds == 600 + 2 * (720 + 300) + 300
    assert tss == pytest.approx(6 + 2 * (20 + 300 / 3600 * 25) + 300 / 3600 * 25, abs=0.1)
    assert intensity == pytest.approx((tss / (seconds / 3600 * 100)) ** 0.5, abs=0.01)


@pytest.mark.parametrize("steps, ftp, says", [
    ([{"step_type": "steady_state", "duration_seconds": 600, "power_watts": 280}], None, "no FTP"),
    ([{"step_type": "steady_state", "duration_seconds": 600, "power_pct": 0.95}], 300, "95 means 95%"),
    ([{"step_type": "steady_state", "duration_seconds": 600, "power_pct": 90, "power_watts": 270}], 300, "not both"),
    ([{"step_type": "interval_on", "duration_seconds": 60, "power_pct": 120, "repeat_count": 4},
      {"step_type": "steady_state", "duration_seconds": 60, "power_pct": 60}], 300, "interval_off"),
    ([{"step_type": "steady_state", "duration_seconds": 5}], 300, "duration_seconds"),
    ([{"step_type": "steady_state", "duration_seconds": 600}], 300, "needs a power target"),
    ([{"step_type": "ramp", "duration_seconds": 600, "power_pct": 80}], 300, "low and a high"),
    ([{"step_type": "steady_state", "duration_seconds": 600, "power_pct": 90, "cadence": 200}], 300, "cadence"),
    ([{"step_type": "steady_state", "duration_seconds": 14400, "power_pct": 60}] * 3, 300, "8 hour"),
    ([], 300, "1 to 40"),
])
def test_build_steps_refuses_what_ride_mode_would_get_wrong(steps, ftp, says):
    with pytest.raises(ValueError, match=says):
        build_steps(steps, ftp)


def test_update_with_steps_replaces_the_old_ones(db_session, friday):
    gareth = _gareth(db_session)
    _, phase = _plan(db_session, gareth, date(2026, 10, 1), date(2026, 12, 1))
    w = _session(db_session, gareth, phase, date(2026, 10, 20), wtype=WorkoutType.threshold)
    plan_service._create_workout_steps(db_session, w, plan_service.get_template("threshold"))
    db_session.commit()
    old_ids = {s.id for s in w.steps}

    result = _execute_tool(db_session, gareth, "update_workout", {
        "workout_id": w.id, "scheduled_date": "2026-10-19", "steps": PB_SHAPE,
    })

    assert result.startswith("Updated.") and "Monday 19 October" in result
    db_session.expire_all()
    assert len(w.steps) == len(PB_SHAPE) and not old_ids & {s.id for s in w.steps}
    assert db_session.query(WorkoutStep).count() == len(PB_SHAPE)
    assert w.scheduled_date == date(2026, 10, 19) and w.status == WorkoutStatus.modified


def test_changing_the_type_swaps_in_steps_for_the_new_session(db_session, friday):
    gareth = _gareth(db_session)
    _, phase = _plan(db_session, gareth, date(2026, 10, 1), date(2026, 12, 1))
    w = _session(db_session, gareth, phase, date(2026, 10, 20), wtype=WorkoutType.threshold)
    plan_service._create_workout_steps(db_session, w, plan_service.get_template("threshold"))
    db_session.commit()

    result = _execute_tool(db_session, gareth, "update_workout", {
        "workout_id": w.id, "workout_type": "endurance",
    })

    db_session.expire_all()
    assert "standard endurance session" in result
    assert not any(str(getattr(s.step_type, "value", s.step_type)) == "interval_on" for s in w.steps)
    assert w.planned_duration_seconds == sum(s.duration_seconds for s in w.steps)


def test_a_ridden_session_cannot_be_rewritten_or_skipped(db_session, friday):
    gareth = _gareth(db_session)
    w = _session(db_session, gareth, None, date(2026, 10, 16), status=WorkoutStatus.completed)

    for tool, extra in (("update_workout", {"steps": PB_SHAPE}), ("skip_workout", {})):
        result = _execute_tool(db_session, gareth, tool, {"workout_id": w.id, **extra})
        assert result.startswith("Error: nothing was") and "already been ridden" in result

    db_session.expire_all()
    assert w.status == WorkoutStatus.completed and not w.steps


def test_swap_and_skip_name_the_days(db_session, friday):
    gareth = _gareth(db_session)
    a = _session(db_session, gareth, None, date(2026, 10, 17))
    b = _session(db_session, gareth, None, date(2026, 10, 18))

    swapped = _execute_tool(db_session, gareth, "swap_workout_date", {"workout_id_a": a.id, "workout_id_b": b.id})
    skipped = _execute_tool(db_session, gareth, "skip_workout", {"workout_id": a.id})

    assert "Sunday 18 October" in swapped and "Saturday 17 October" in swapped
    assert skipped.startswith("Skipped.") and "Sunday 18 October" in skipped


# ── What happens to a booked session afterwards ──────────────────────────────


def test_a_rebuild_keeps_the_booked_session_and_leaves_its_day_clear(db_session, friday):
    gareth = _gareth(db_session)
    _plan(db_session, gareth, date(2026, 8, 24), date(2026, 10, 4))
    saturday = date(2026, 10, 17)  # where the new plan's long ride would go
    _book(db_session, gareth, saturday, steps=PB_SHAPE)
    booked = db_session.query(Workout).filter(Workout.phase_id.is_(None)).one()

    for _ in range(2):
        plan_service.generate_plan(db_session, gareth)

    on_the_day = get_workouts_by_date(db_session, gareth.id, target_date=saturday)
    assert [w.id for w in on_the_day] == [booked.id]
    later = get_workouts_by_date(db_session, gareth.id, week_start=date(2026, 10, 19))
    assert later and all(w.phase_id for w in later)  # the new plan is there too


def test_a_ride_matches_a_booked_session_with_no_plan(db_session, friday):
    gareth = _gareth(db_session)
    booked = _session(db_session, gareth, None, date(2026, 10, 16), wtype=WorkoutType.threshold)
    ride = _ride(db_session, gareth, date(2026, 10, 16))

    assert plan_compliance_service.link_ride(db_session, ride).id == booked.id
    assert booked.status == WorkoutStatus.completed and ride.workout_id == booked.id


def test_a_ride_matches_a_booked_session_past_the_plans_end(db_session, friday):
    gareth = _gareth(db_session)
    _plan(db_session, gareth, date(2026, 8, 24), date(2026, 10, 4))
    booked = _session(db_session, gareth, None, date(2026, 10, 16), wtype=WorkoutType.threshold)
    ride = _ride(db_session, gareth, date(2026, 10, 16))

    assert plan_compliance_service.link_ride(db_session, ride).id == booked.id


def test_inside_the_plan_a_ride_still_prefers_the_matching_type(db_session, friday):
    gareth = _gareth(db_session)
    _, phase = _plan(db_session, gareth, date(2026, 10, 1), date(2026, 12, 1))
    _session(db_session, gareth, phase, date(2026, 10, 16), wtype=WorkoutType.recovery)
    booked = _session(db_session, gareth, None, date(2026, 10, 16), wtype=WorkoutType.threshold)
    ride = _ride(db_session, gareth, date(2026, 10, 16), title="Threshold Intervals")

    assert plan_compliance_service.link_ride(db_session, ride).id == booked.id


def test_an_accepted_proposal_lands_on_the_plan_the_rider_has_now(db_session, friday):
    gareth = _gareth(db_session)
    old, _ = _plan(db_session, gareth, date(2026, 8, 24), date(2026, 10, 4), status=PlanStatus.cancelled)
    _plan(db_session, gareth, date(2026, 8, 24), date(2026, 10, 4))  # active, but ended
    proposal = PlanProposal(
        user_id=gareth.id, plan_id=old.id, trigger="conversation", status="pending",
        observation="o", rationale="r",
        changes=[{"action": "add_workout", "scheduled_date": "2026-10-19",
                  "workout_type": "threshold", "title": "Reps"}],
    )
    db_session.add(proposal)
    db_session.commit()

    assert apply_proposal(db_session, gareth, proposal) == 1

    added = get_workouts_by_date(db_session, gareth.id, target_date=date(2026, 10, 19))
    assert len(added) == 1 and added[0].phase_id is None


# ── The chat loop ────────────────────────────────────────────────────────────


class _FakeStream:
    def __init__(self, final):
        self._final = final

    def __iter__(self):
        for block in self._final.content:
            if block.type == "text":
                yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(text=block.text))

    def get_final_message(self):
        return self._final


def _chat(db, monkeypatch, user, tool_input):
    """One coach turn: the model asks for add_workout, then answers."""
    rounds = iter([
        SimpleNamespace(stop_reason="tool_use", usage=None, content=[
            SimpleNamespace(type="text", text="Putting it in now."),
            SimpleNamespace(type="tool_use", id="t1", name="add_workout", input=tool_input),
        ]),
        SimpleNamespace(stop_reason="end_turn", usage=None, content=[
            SimpleNamespace(type="text", text="All set."),
        ]),
    ])
    calls = []

    @contextmanager
    def fake_stream(**kwargs):
        calls.append(kwargs)
        yield _FakeStream(next(rounds))

    def offline(*args, **kwargs):
        raise RuntimeError("no model in tests")

    monkeypatch.setattr(forma_core, "stream", fake_stream)
    monkeypatch.setattr(forma_core, "call", offline)
    monkeypatch.setattr(coach_service, "_relevant_memories", lambda *a: None)
    monkeypatch.setattr(coach_service, "_dossier_block", lambda *a: "")
    session = ChatSession(user_id=user.id, title="Sa Calobra Sub-30 Preparation")
    db.add(session)
    db.commit()

    async def run():
        return [chunk async for chunk in coach_service.stream_response(db, user, session, "Book it in")]

    chunks = asyncio.run(run())
    tool_result = calls[1]["messages"][-1]["content"][0]["content"]
    return chunks, calls, tool_result


def test_the_chat_loop_gives_the_coach_the_dated_calendar_and_the_real_day(db_session, friday, monkeypatch):
    gareth = _gareth(db_session)

    chunks, calls, tool_result = _chat(db_session, monkeypatch, gareth, {
        "scheduled_date": "2026-10-19", "title": "PB reps", "workout_type": "threshold", "steps": PB_SHAPE,
    })

    system = " ".join(block["text"] for block in calls[0]["system"])
    assert "Today is Friday 16 October 2026" in system
    assert "- 2026-10-19: Monday 19 October" in system
    assert "Nothing is booked until a tool says so" in system
    assert tool_result.startswith("Booked.") and "Monday 19 October" in tool_result
    assert any('"plan_updated"' in c for c in chunks)
    assert db_session.query(Workout).count() == 1


def test_a_tool_crash_in_the_chat_loop_reaches_the_coach_as_an_error(db_session, friday, monkeypatch):
    gareth = _gareth(db_session)

    def explode(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(coach_service, "_on_calendar", explode)

    chunks, _, tool_result = _chat(db_session, monkeypatch, gareth, {
        "scheduled_date": "2026-10-19", "title": "PB reps", "workout_type": "threshold",
    })

    assert tool_result.startswith("Error: that change failed and nothing was saved.")
    assert not any('"plan_updated"' in c for c in chunks)
    assert any("All set." in c for c in chunks)  # the turn carries on to an answer
    assert db_session.query(Workout).count() == 0


def test_the_last_round_cannot_call_a_tool_so_the_rider_hears_the_result(db_session, friday, monkeypatch):
    """Five model rounds a turn. A booking asked for in the fifth used to run
    with no round after it: the reply ended on "I'll put it straight into
    your training plan" and nothing said whether it had gone in. The last
    round now may not call a tool, so the coach speaks to the results it has."""
    gareth = _gareth(db_session)
    lookups = [
        SimpleNamespace(stop_reason="tool_use", usage=None, content=[
            SimpleNamespace(type="text", text="Putting it in."),
            SimpleNamespace(type="tool_use", id=f"t{n}", name="add_workout", input={
                "scheduled_date": "2026-10-31", "title": "PB reps", "workout_type": "threshold",
            }),
        ])
        for n in range(4)
    ]
    calls = []

    @contextmanager
    def fake_stream(**kwargs):
        calls.append(kwargs)
        if kwargs.get("tool_choice") == {"type": "none"}:
            # The model obeys: words only, about the refusal it was handed.
            yield _FakeStream(SimpleNamespace(stop_reason="end_turn", usage=None, content=[
                SimpleNamespace(type="text", text="The 31st is past the fortnight, so it is not booked yet."),
            ]))
        elif len(calls) <= len(lookups):
            yield _FakeStream(lookups[len(calls) - 1])
        else:
            yield _FakeStream(SimpleNamespace(stop_reason="tool_use", usage=None, content=[
                SimpleNamespace(type="text", text="I'll put it straight into your training plan."),
                SimpleNamespace(type="tool_use", id="t5", name="add_workout", input={
                    "scheduled_date": "2026-10-19", "title": "PB reps", "workout_type": "threshold",
                }),
            ]))

    monkeypatch.setattr(forma_core, "stream", fake_stream)
    monkeypatch.setattr(forma_core, "call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(coach_service, "_relevant_memories", lambda *a: None)
    monkeypatch.setattr(coach_service, "_dossier_block", lambda *a: "")
    session = ChatSession(user_id=gareth.id, title="Sa Calobra Sub-30 Preparation")
    db_session.add(session)
    db_session.commit()

    async def run():
        return [c async for c in coach_service.stream_response(db_session, gareth, session, "Book it in")]

    chunks = asyncio.run(run())

    assert len(calls) == coach_service.MAX_TOOL_ROUNDS
    assert [c.get("tool_choice") for c in calls] == [None] * 4 + [{"type": "none"}]
    assert all(c["tools"] for c in calls)  # the tools stay visible, only the call is off
    said = "".join(json.loads(c[6:])["content"] for c in chunks if '"type": "text"' in c)
    assert said.rstrip().endswith("so it is not booked yet.")
    assert db_session.query(Workout).count() == 0


def test_forma_core_passes_tool_choice_only_with_tools(monkeypatch):
    seen = []

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_final_message(self):
            return SimpleNamespace(usage=None)

    class _Messages:
        def stream(self, **kwargs):
            seen.append(kwargs)
            return _Stream()

    monkeypatch.setattr(forma_core, "_client", lambda: SimpleNamespace(messages=_Messages()))
    monkeypatch.setattr(forma_core, "_log", lambda *a, **k: None)
    for tools in ([{"name": "add_workout"}], None):
        with forma_core.stream(user_id="u", task="chat", system="s", messages=[],
                               tools=tools, tool_choice={"type": "none"}, enforce_budget=False):
            pass

    assert seen[0]["tool_choice"] == {"type": "none"} and seen[0]["tools"]
    assert "tool_choice" not in seen[1] and "tools" not in seen[1]


def test_a_day_past_the_fortnight_is_not_swapped_for_another(db_session, friday):
    """The refusal used to end "take the date from the dated calendar and
    call again", which invites booking the 30th when the rider asked for the
    31st."""
    gareth = _gareth(db_session)

    result = _execute_tool(db_session, gareth, "add_workout", {
        "scheduled_date": "2026-10-31", "title": "Reps", "workout_type": "threshold",
    })

    assert "after Friday 30 October, it cannot be booked yet" in result
    assert "do not book another day instead" in result


def test_a_session_the_coach_rewrote_survives_a_new_goal(db_session, friday, monkeypatch):
    """On the base block the coach rewrote Saturday as the PB session and said
    it was ready for Ride mode. Filing the next goal, as the season note asks,
    rebuilt the plan and put a generated long ride back on Saturday."""
    gareth = _gareth(db_session)
    _plan(db_session, gareth, date(2026, 8, 24), date(2026, 10, 4))
    plan_service.generate_plan(db_session, gareth, name="Building to the next goal")
    saturday = date(2026, 10, 17)
    [generated] = get_workouts_by_date(db_session, gareth.id, target_date=saturday)
    updated = _execute_tool(db_session, gareth, "update_workout", {
        "workout_id": generated.id, "title": "The Honest Hour", "workout_type": "threshold",
        "steps": PB_SHAPE,
    })
    assert updated.startswith("Updated.")

    filed = _execute_tool(db_session, gareth, "create_goal", {
        "event_name": "Mallorca 312", "event_date": "2027-04-24",
        "event_type": "gran_fondo", "priority": "a_race",
    })

    assert "rebuilt around it" in filed
    on_the_day = get_workouts_by_date(db_session, gareth.id, target_date=saturday)
    assert [w.id for w in on_the_day] == [generated.id]
    assert len(on_the_day[0].steps) == len(PB_SHAPE)
    assert on_the_day[0].phase_id is None  # its own now, like a booking

    # And when the rider presses Build my season the next day, it still stands.
    class _Saturday(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 17)

    for module in (coach_service, plan_service, plan_compliance_service, season_service):
        monkeypatch.setattr(module, "date", _Saturday)
    plan_service.generate_plan(db_session, gareth)
    assert [w.id for w in get_workouts_by_date(db_session, gareth.id, target_date=saturday)] == [generated.id]
