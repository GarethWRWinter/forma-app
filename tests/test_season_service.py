"""Between goals: a next step, and a plan that keeps going.

16 Oct 2026: Gareth's goal (Sa Calobra sub 30) was dated 4 October and had
passed. The plan built for it ended that day, but nothing ever happened when
a plan reached its end date: it stayed "active", the plan page read "week 7 of
7" over a week of rest-day filler, and the only thing on offer was a banner
asking what he wanted to chase. His rule: if the goal has expired, settle into
a default plan or be prompted for the next goal straight away.
"""

import json
from datetime import date, datetime, timedelta

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import text

from app.api.v1 import goals as goals_api
from app.api.v1.training import list_workouts
from app.models.onboarding import EventPriority, EventType, GoalEvent, GoalStatus
from app.models.plan_proposal import PlanProposal
from app.models.training import (
    PeriodizationModel, PhaseType, PlanStatus, TrainingPhase, TrainingPlan, Workout,
    WorkoutType,
)
from app.models.user import User
from app.schemas.onboarding import GoalAssessmentSubmit, GoalEventCreate
from app.services import (
    activation_service, assessment_service, coach_service, onboarding_service,
    plan_compliance_service, plan_service, season_service,
)
from app.services.coach_service import _build_rider_context, _execute_tool
from app.services.plan_service import generate_plan, get_workouts_by_date
from app.services.season_service import (
    BLOCK_NAME, ensure_all, ensure_season, season_note, season_state,
)

FRIDAY = date(2026, 10, 16)
SA_CALOBRA = date(2026, 10, 4)
DASHES = (chr(0x2014), chr(0x2013))  # em and en: never in anything the coach reads


class _Clock(date):
    now = FRIDAY

    @classmethod
    def today(cls):
        return cls.now


@pytest.fixture
def clock(monkeypatch):
    """Every module that asks for today hears the same day, and the test can
    move it."""
    for module in (
        season_service, plan_service, coach_service, onboarding_service,
        plan_compliance_service, assessment_service, goals_api,
    ):
        monkeypatch.setattr(module, "date", _Clock)

    def set_day(d: date) -> None:
        _Clock.now = _Clock(d.year, d.month, d.day)

    set_day(FRIDAY)
    yield set_day
    set_day(FRIDAY)


def _gareth(db) -> User:
    user = User(
        email="gareth@example.com", hashed_password="x", full_name="Gareth Winter",
        ftp=300, weight_kg=68, weekly_hours_available=8,
    )
    db.add(user)
    db.commit()
    return user


def _goal(db, user, day, name="Sa Calobra Mallorca sub 30", status=GoalStatus.upcoming,
          priority=EventPriority.a_race) -> GoalEvent:
    goal = GoalEvent(
        user_id=user.id, event_name=name, event_date=day, event_type=EventType.hill_climb,
        priority=priority, status=status,
    )
    db.add(goal)
    db.commit()
    return goal


def _old_plan(db, user, goal, start=date(2026, 8, 24), end=SA_CALOBRA) -> TrainingPlan:
    """The Sa Calobra plan as it stood: active, its last day the race."""
    plan = TrainingPlan(
        user_id=user.id, name="Plan for Sa Calobra Mallorca sub 30",
        goal_event_id=goal.id if goal else None, start_date=start, end_date=end,
        status=PlanStatus.active, periodization_model=PeriodizationModel.traditional,
    )
    db.add(plan)
    db.flush()
    phase = TrainingPhase(plan_id=plan.id, start_date=start, end_date=end,
                          phase_type=PhaseType.peak, sort_order=0)
    db.add(phase)
    db.flush()
    for d in (date(2026, 10, 1), end):
        db.add(Workout(phase_id=phase.id, user_id=user.id, scheduled_date=d,
                       title="Plan session", workout_type=WorkoutType.endurance,
                       planned_duration_seconds=3600))
    db.commit()
    return plan


def _incident(db):
    """Gareth on Friday 16 October: goal passed, plan ended, nothing filed."""
    gareth = _gareth(db)
    goal = _goal(db, gareth, SA_CALOBRA)
    plan = _old_plan(db, gareth, goal)
    return gareth, goal, plan


def _active(db, user) -> list[TrainingPlan]:
    return db.query(TrainingPlan).filter(
        TrainingPlan.user_id == user.id, TrainingPlan.status == PlanStatus.active
    ).all()


def _week(db, user, monday: date) -> list:
    return list_workouts(target_date=None, week=monday, current_user=user, db=db)


def _new_goal(db, user, day=date(2027, 5, 30), name="Mallorca 312"):
    body = GoalEventCreate(event_name=name, event_date=day, event_type="gran_fondo",
                           priority="a_race")
    return goals_api.create_goal_event(body, BackgroundTasks(), current_user=user, db=db)


# ── The incident ─────────────────────────────────────────────────────────────


def test_an_ended_goal_and_plan_become_a_base_block_exactly_once(db_session, clock):
    gareth, goal, old = _incident(db_session)

    # Before: between goals, nothing current, the race report owed.
    before = season_state(db_session, gareth)
    assert before["state"] == "between_goals"
    assert before["current_plan"] is None
    assert before["last_goal"]["goal_id"] == goal.id
    assert before["last_goal"]["days_ago"] == 12
    assert before["last_goal"]["result_recorded"] is False
    assert _week(db_session, gareth, date(2026, 10, 19)) == []

    assert ensure_season(db_session, gareth) == "block"

    # The old plan ended on its own last day, and its weeks are still there.
    db_session.refresh(old)
    assert old.status == PlanStatus.completed and old.end_date == SA_CALOBRA
    assert {w.scheduled_date for w in _week(db_session, gareth, date(2026, 9, 28))} >= {
        date(2026, 10, 1), SA_CALOBRA,
    }

    # One block, aimed at no goal, from today for 12 weeks.
    [block] = _active(db_session, gareth)
    assert block.name == BLOCK_NAME
    assert block.goal_event_id is None
    assert block.start_date == FRIDAY
    assert block.end_date == FRIDAY + timedelta(weeks=12)

    # Every week has sessions: this one from today, next week, and the last.
    this_week = _week(db_session, gareth, date(2026, 10, 12))
    assert this_week and all(w.scheduled_date >= FRIDAY for w in this_week)
    assert _week(db_session, gareth, date(2026, 10, 19))
    assert _week(db_session, gareth, date(2027, 1, 4))
    first = min(this_week, key=lambda w: w.scheduled_date)
    on_the_day = list_workouts(target_date=first.scheduled_date, week=None,
                               current_user=gareth, db=db_session)
    assert first.id in {w.id for w in on_the_day}

    # Idempotent: again, and from the hourly pass, nothing more is built.
    plans = db_session.query(TrainingPlan).count()
    sessions = db_session.query(Workout).count()
    assert ensure_season(db_session, gareth) == "unchanged"
    assert ensure_all(db_session) == {"unchanged": 1}
    assert db_session.query(TrainingPlan).count() == plans
    assert db_session.query(Workout).count() == sessions

    after = season_state(db_session, gareth)
    assert after["state"] == "between_goals"
    assert after["current_plan"]["name"] == BLOCK_NAME
    assert after["current_plan"]["goal_less"] is True
    assert after["current_plan"]["weeks"] == 12


def test_the_hourly_pass_does_it_and_a_dry_run_writes_nothing(db_session, clock):
    gareth, _, old = _incident(db_session)

    assert ensure_all(db_session, dry_run=True) == {"block": 1}
    db_session.expire_all()
    assert old.status == PlanStatus.active
    assert db_session.query(TrainingPlan).count() == 1

    assert ensure_all(db_session) == {"block": 1}
    assert [p.name for p in _active(db_session, gareth)] == [BLOCK_NAME]


def test_a_new_goal_replaces_the_block_started_today(db_session, clock):
    gareth, _, _ = _incident(db_session)
    ensure_season(db_session, gareth)
    [block] = _active(db_session, gareth)

    created = _new_goal(db_session, gareth)

    assert created.plan_rebuilt is True
    db_session.refresh(block)
    assert block.status == PlanStatus.cancelled  # it had no past to keep
    [plan] = _active(db_session, gareth)
    assert plan.goal_event_id == created.id
    assert plan.end_date == date(2027, 5, 30)
    assert season_state(db_session, gareth)["state"] == "chasing"
    # Not rebuilt twice: the next pass leaves it be.
    assert ensure_season(db_session, gareth) == "unchanged"


def test_a_new_goal_ends_an_older_block_and_keeps_its_weeks(db_session, clock):
    gareth, _, _ = _incident(db_session)
    clock(date(2026, 10, 5))  # the keeper's first pass, the day after the race
    assert ensure_season(db_session, gareth) == "block"
    [block] = _active(db_session, gareth)
    ridden = get_workouts_by_date(db_session, gareth.id, week_start=date(2026, 10, 5))
    assert ridden

    clock(FRIDAY)
    created = _new_goal(db_session, gareth)

    db_session.refresh(block)
    assert block.status == PlanStatus.completed
    assert block.end_date == date(2026, 10, 15)
    # The block's week of 5 October is still on the calendar; from today the
    # sessions belong to the plan for the new goal.
    assert {w.id for w in _week(db_session, gareth, date(2026, 10, 5))} == {w.id for w in ridden}
    ahead = get_workouts_by_date(db_session, gareth.id, week_start=date(2026, 10, 19))
    assert ahead and all(w.phase.plan.goal_event_id == created.id for w in ahead)


def test_a_goal_the_plan_already_aims_past_leaves_it_alone(db_session, clock):
    """A rider chasing a goal keeps their plan when another goal is added
    outside the goals page: the keeper only replaces a plan aimed at nothing."""
    gareth = _gareth(db_session)
    a_race = _goal(db_session, gareth, date(2027, 6, 1), name="Etape du Tour")
    generate_plan(db_session, gareth)
    [plan] = _active(db_session, gareth)

    created = _new_goal(db_session, gareth, day=date(2027, 3, 1), name="Spring sportive")

    assert created.plan_rebuilt is False
    assert [p.id for p in _active(db_session, gareth)] == [plan.id]
    assert plan.goal_event_id == a_race.id


def test_a_new_rider_in_setup_is_left_alone(db_session, clock):
    rider = _gareth(db_session)
    assert ensure_season(db_session, rider) == "unchanged"
    # Even one whose only goal is in the past: setup builds the first plan,
    # and the coach is not told otherwise.
    _goal(db_session, rider, SA_CALOBRA)
    assert ensure_season(db_session, rider) == "unchanged"
    note = season_note(season_state(db_session, rider))
    assert "Build my season" in note and "rebuilt around it at once" not in note
    _new_goal(db_session, rider)
    assert db_session.query(TrainingPlan).count() == 0


def test_a_rider_without_access_is_not_built_for(db_session, clock, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "require_subscription", True)
    gareth, _, old = _incident(db_session)

    assert ensure_season(db_session, gareth) == "retired"
    db_session.refresh(old)
    assert old.status == PlanStatus.completed
    assert _active(db_session, gareth) == []


def test_race_day_never_starts_a_block(db_session, clock):
    gareth = _gareth(db_session)
    race = _goal(db_session, gareth, FRIDAY)
    _old_plan(db_session, gareth, race, end=FRIDAY)
    # Filed on the day itself: still race day.
    race.status = GoalStatus.completed
    db_session.commit()
    assert season_state(db_session, gareth)["state"] == "chasing"
    assert ensure_season(db_session, gareth) == "unchanged"

    clock(FRIDAY + timedelta(days=1))
    assert ensure_season(db_session, gareth) == "block"
    state = season_state(db_session, gareth)
    assert state["last_goal"]["result_recorded"] is True
    assert state["last_goal"]["days_ago"] == 1


# ── The calendar keeps an ended plan's weeks ─────────────────────────────────


def test_an_ended_plan_shows_its_sessions_up_to_its_end_and_none_after(db_session, clock):
    gareth = _gareth(db_session)
    plan = _old_plan(db_session, gareth, None, start=date(2026, 9, 1), end=date(2026, 10, 10))
    phase = plan.phases[0]
    after = Workout(phase_id=phase.id, user_id=gareth.id, scheduled_date=date(2026, 10, 12),
                    title="Past the end", workout_type=WorkoutType.endurance)
    db_session.add(after)
    plan.status = PlanStatus.completed
    db_session.commit()

    shown = {w.scheduled_date for w in get_workouts_by_date(
        db_session, gareth.id, week_start=date(2026, 10, 5))}
    assert date(2026, 10, 10) in shown
    assert date(2026, 10, 12) not in shown


def test_rebuilding_keeps_the_old_plans_past_weeks(db_session, clock):
    """Building used to cancel the old plan, which blanked every week of it."""
    gareth = _gareth(db_session)
    _goal(db_session, gareth, date(2027, 6, 1), name="Etape du Tour")
    old = _old_plan(db_session, gareth, None, start=date(2026, 9, 1), end=date(2026, 12, 1))
    proposal = PlanProposal(user_id=gareth.id, plan_id=old.id, trigger="review",
                            observation="o", rationale="r", changes=[], status="pending")
    db_session.add(proposal)
    db_session.commit()

    generate_plan(db_session, gareth)

    db_session.refresh(old)
    db_session.refresh(proposal)
    assert old.status == PlanStatus.completed and old.end_date == date(2026, 10, 15)
    assert proposal.status == "superseded"
    shown = get_workouts_by_date(db_session, gareth.id, week_start=date(2026, 9, 28))
    assert date(2026, 10, 1) in {w.scheduled_date for w in shown}
    # Nothing of the old plan from today on.
    for w in get_workouts_by_date(db_session, gareth.id, week_start=date(2026, 10, 12)):
        assert w.scheduled_date < FRIDAY or w.phase.plan.status == PlanStatus.active


# ── What the coach is told ───────────────────────────────────────────────────


def test_the_coach_knows_the_rider_is_between_goals(db_session, clock):
    gareth, goal, _ = _incident(db_session)
    ensure_season(db_session, gareth)

    context = json.loads(_build_rider_context(db_session, gareth))

    season = context["season"]
    assert season["last_goal"]["result_recorded"] is False
    note = season["note"]
    assert "Sa Calobra Mallorca sub 30, was 12 days ago" in note
    assert "File the race report" in note
    assert f"'{BLOCK_NAME}', a 12-week block" in note
    assert "do not ask whether they want one" in note
    assert "the plan is rebuilt around it at once" in note
    assert not any(d in note for d in DASHES)

    entry = next(g for g in context["goal_events"] if g["goal_id"] == goal.id)
    assert entry["date_passed"] is True and entry["result_recorded"] is False
    assert context["training_plan"]["name"] == BLOCK_NAME
    assert "aimed at no goal" in context["training_plan"]["kind"]

    # The report filed: no more asking how it went.
    goals_api.submit_goal_assessment(
        goal.id, GoalAssessmentSubmit(status="completed", overall_satisfaction=8),
        current_user=gareth, db=db_session,
    )
    context = json.loads(_build_rider_context(db_session, gareth))
    assert context["season"]["last_goal"]["result_recorded"] is True
    assert "File the race report" not in context["season"]["note"]
    entry = next(g for g in context["goal_events"] if g["goal_id"] == goal.id)
    assert "date_passed" not in entry and entry["status"] == "completed"


def test_an_ended_plan_the_keeper_has_not_reached_is_said_plainly(db_session, clock):
    gareth, _, _ = _incident(db_session)

    context = json.loads(_build_rider_context(db_session, gareth))

    assert "Sunday 4 October 2026" in context["training_plan"]["ended"]
    assert "no current plan" in context["season"]["note"]


def test_a_rider_with_a_goal_ahead_hears_nothing_about_seasons(db_session, clock):
    gareth = _gareth(db_session)
    _goal(db_session, gareth, date(2027, 6, 1), name="Etape du Tour")
    generate_plan(db_session, gareth)

    context = json.loads(_build_rider_context(db_session, gareth))

    assert "season" not in context
    assert "kind" not in context["training_plan"]


def test_the_coach_filing_a_goal_rebuilds_the_plan_and_names_it(db_session, clock):
    gareth, _, _ = _incident(db_session)
    ensure_season(db_session, gareth)

    result = _execute_tool(db_session, gareth, "create_goal", {
        "event_name": "Mallorca 312", "event_date": "2027-05-30",
        "event_type": "gran_fondo", "priority": "a_race",
    })

    [plan] = _active(db_session, gareth)
    assert plan.name == "Plan for Mallorca 312"
    assert "Sunday 30 May 2027" in result
    assert "The plan has been rebuilt around it: 'Plan for Mallorca 312'" in result
    assert "Friday 16 October to Sunday 30 May 2027" in result
    assert not any(d in result for d in DASHES)


def test_the_coach_is_told_when_the_plan_did_not_move(db_session, clock):
    gareth = _gareth(db_session)
    etape = _goal(db_session, gareth, date(2027, 6, 1), name="Etape du Tour")
    generate_plan(db_session, gareth)

    filed = _execute_tool(db_session, gareth, "create_goal", {
        "event_name": "Spring sportive", "event_date": "2027-03-07",
        "event_type": "sportive", "priority": "c_race",
    })
    assert "still aims at Etape du Tour; its sessions are unchanged" in filed
    assert "rebuilt" not in filed

    moved = _execute_tool(db_session, gareth, "update_goal", {
        "goal_id": etape.id, "event_date": "2027-06-08",
    })
    assert "built for the old date" in moved and "Build my season" in moved


# ── Around the edges ─────────────────────────────────────────────────────────


def test_deleting_a_goal_a_plan_was_built_for_works_and_replaces_the_plan(db_session, clock):
    """Postgres enforces the plan's foreign key to its goal, which has no ON
    DELETE: deleting any goal a plan had ever been built for failed."""
    db_session.execute(text("PRAGMA foreign_keys=ON"))
    gareth = _gareth(db_session)
    etape = _goal(db_session, gareth, date(2027, 6, 1), name="Etape du Tour")
    clock(date(2026, 10, 1))
    generate_plan(db_session, gareth)
    [plan] = _active(db_session, gareth)
    clock(FRIDAY)

    goals_api.delete_goal_event(etape.id, current_user=gareth, db=db_session)

    assert db_session.get(GoalEvent, etape.id) is None
    db_session.refresh(plan)
    assert plan.goal_event_id is None
    assert plan.status == PlanStatus.completed and plan.end_date == date(2026, 10, 15)
    # It was the rider's only goal; they have been on a plan, so the weeks
    # ahead are not left empty.
    assert [p.name for p in _active(db_session, gareth)] == [BLOCK_NAME]


def test_a_rider_between_goals_is_not_sent_back_to_set_up(db_session, clock):
    gareth, goal, _ = _incident(db_session)
    goal.status = GoalStatus.completed
    db_session.commit()

    facts = activation_service.collect_facts(db_session, gareth, datetime(2026, 10, 16, 9))

    assert facts.has_goal is True
    assert activation_service.derive_stage(facts) != "goal"


def test_needs_assessment_is_not_swallowed_by_the_goal_id_route():
    paths = [r.path for r in goals_api.router.routes if "GET" in getattr(r, "methods", set())]
    assert paths.index("/goals/needs-assessment") < paths.index("/goals/{goal_id}")


# ── Review findings, 16 Oct 2026 ─────────────────────────────────────────────


def _ride_on(db, user, day, title="Ride", if_=0.7, tss=60):
    from app.models.ride import Ride, RideSource

    ride = Ride(
        user_id=user.id, source=RideSource.wahoo, title=title,
        ride_date=datetime.combine(day, datetime.min.time()) + timedelta(hours=8),
        duration_seconds=3600, moving_time_seconds=3600, intensity_factor=if_, tss=tss,
    )
    db.add(ride)
    db.commit()
    return ride


def _block_after_the_race(db, clock):
    """Sa Calobra raced on Sunday 4 October; the keeper runs on the Monday."""
    gareth, goal, old = _incident(db)
    clock(date(2026, 10, 5))
    assert ensure_season(db, gareth) == "block"
    return gareth, goal, old


def test_a_ride_that_syncs_after_the_plan_ended_still_links(db_session, clock):
    """Race-week rides that sync late found nothing once the plan had ended,
    because only the active plan's window was searched: the session stayed
    planned and read as missed."""
    gareth, _, old = _block_after_the_race(db_session, clock)
    [race_day] = [w for w in get_workouts_by_date(db_session, gareth.id, target_date=SA_CALOBRA)]

    ride = _ride_on(db_session, gareth, SA_CALOBRA, title="Sa Calobra")
    linked = plan_compliance_service.link_ride(db_session, ride)

    assert linked is not None and linked.id == race_day.id
    assert ride.workout_id == race_day.id

    # The backfill finds the same session for a ride that missed the import.
    thursday = date(2026, 10, 1)
    late = _ride_on(db_session, gareth, thursday)
    assert plan_compliance_service.classify_unlinked_rides(db_session, gareth.id)["linked"] == 1
    db_session.refresh(late)
    assert late.workout_id is not None


def test_the_morning_after_the_race_the_coach_still_sees_race_week(db_session, clock):
    """The block starts the morning after the race, and the 28 days the coach
    reads were clipped to the block: build-up and race week vanished on the
    very day the coach is told to debrief."""
    gareth, _, _ = _block_after_the_race(db_session, clock)
    plan_compliance_service.link_ride(db_session, _ride_on(db_session, gareth, SA_CALOBRA))

    summary = plan_compliance_service.compliance_summary(
        db_session, gareth.id, date(2026, 10, 5) - timedelta(days=28), date(2026, 10, 5)
    )

    assert summary is not None
    # From the ended plan's first session (1 October), not from the block's
    # first day.
    assert summary["window"]["start"] == "2026-10-01"
    assert summary["planned_sessions"] >= 2  # 1 and 4 October, from the ended plan
    assert summary["as_prescribed"] + summary["deviated"] == 1
    assert summary["missed"] == 1  # 1 October, with no ride against it
    assert summary["plan"] == BLOCK_NAME
    # Nothing before the rider's first session counts as riding off-plan.
    early = plan_compliance_service.compliance_summary(
        db_session, gareth.id, date(2026, 7, 1), date(2026, 8, 1)
    )
    assert early is None


def test_the_block_after_an_a_race_opens_with_a_recovery_week(db_session, clock):
    """The block once opened with 75 minutes of tempo the morning after the
    A race. Its first seven days are now recovery and easy endurance."""
    gareth, _, _ = _block_after_the_race(db_session, clock)

    first_week = [
        w for w in get_workouts_by_date(db_session, gareth.id, week_start=date(2026, 10, 5))
        if w.phase_id is not None and w.phase.plan.name == BLOCK_NAME
    ]
    assert first_week
    assert {str(getattr(w.workout_type, "value", w.workout_type)) for w in first_week} <= {
        "recovery", "endurance",
    }
    week_two = [
        w for w in get_workouts_by_date(db_session, gareth.id, week_start=date(2026, 10, 12))
        if w.phase_id is not None
    ]
    assert sum(w.planned_tss or 0 for w in first_week) < sum(w.planned_tss or 0 for w in week_two)


@pytest.mark.parametrize("built_on, priority, status, recovers", [
    (date(2026, 10, 5), EventPriority.b_race, GoalStatus.upcoming, True),
    (date(2026, 10, 12), EventPriority.a_race, GoalStatus.completed, False),  # 8 days on
    (date(2026, 10, 5), EventPriority.c_race, GoalStatus.completed, False),  # a training race
    (date(2026, 10, 5), EventPriority.a_race, GoalStatus.dns, False),  # never ridden
])
def test_only_a_plan_straight_after_an_a_or_b_race_starts_with_recovery(
    db_session, clock, built_on, priority, status, recovers,
):
    gareth = _gareth(db_session)
    goal = _goal(db_session, gareth, SA_CALOBRA, priority=priority, status=status)
    _old_plan(db_session, gareth, goal)
    clock(built_on)

    assert ensure_season(db_session, gareth) == "block"

    first_seven = [
        w for w in get_workouts_by_date(db_session, gareth.id, week_start=built_on)
        if w.phase_id is not None and w.phase.plan.name == BLOCK_NAME
    ]
    types = {str(getattr(w.workout_type, "value", w.workout_type)) for w in first_seven}
    assert first_seven
    assert (types <= {"recovery", "endurance"}) is recovers


def test_a_plan_aimed_at_a_goal_filed_as_not_started_gives_way(db_session, clock):
    """The plan aimed at Race A (25 Oct); A was filed as DNS early. The coach
    was told a new goal would rebuild the plan at once, and then the keeper
    left the rider tapering for a race they are not riding."""
    gareth = _gareth(db_session)
    race_a = _goal(db_session, gareth, date(2026, 10, 25), name="Race A")
    clock(date(2026, 9, 1))
    generate_plan(db_session, gareth)
    clock(FRIDAY)
    race_a.status = GoalStatus.dns
    db_session.commit()

    state = season_state(db_session, gareth)
    assert state["state"] == "between_goals" and state["rebuilds"] is True
    assert "rebuilt around it at once" in season_note(state)

    filed = _execute_tool(db_session, gareth, "create_goal", {
        "event_name": "Race B", "event_date": "2027-03-01",
        "event_type": "road_race", "priority": "a_race",
    })

    assert "The plan has been rebuilt around it: 'Plan for Race B'" in filed
    [plan] = _active(db_session, gareth)
    assert plan.name == "Plan for Race B"


def test_the_hourly_pass_replaces_a_plan_aimed_at_a_race_not_ridden(db_session, clock):
    gareth = _gareth(db_session)
    race_a = _goal(db_session, gareth, date(2026, 10, 25), name="Race A")
    clock(date(2026, 9, 1))
    generate_plan(db_session, gareth)
    clock(FRIDAY)
    race_a.status = GoalStatus.dns
    db_session.commit()

    assert ensure_season(db_session, gareth) == "block"
    assert [p.name for p in _active(db_session, gareth)] == [BLOCK_NAME]
    assert ensure_season(db_session, gareth) == "unchanged"


def test_a_goal_moved_into_the_past_does_not_leave_its_plan_running(db_session, clock):
    """Moved from 1 November to 4 October: on the 16th the plan still ran to
    1 November, peaking for a race already ridden."""
    gareth = _gareth(db_session)
    goal = _goal(db_session, gareth, date(2026, 11, 1), name="Sa Calobra")
    clock(date(2026, 9, 1))
    generate_plan(db_session, gareth)
    goal.event_date = SA_CALOBRA
    db_session.commit()
    clock(FRIDAY)

    assert ensure_season(db_session, gareth) == "block"
    [block] = _active(db_session, gareth)
    assert block.name == BLOCK_NAME


def test_the_day_after_an_a_race_a_later_goal_gets_its_own_plan(db_session, clock):
    """A plan aimed at the A race runs on to a C race after it. On race day
    nothing moves, even once the result is filed; the next morning the plan
    is rebuilt for the C race, starting with a recovery week."""
    gareth = _gareth(db_session)
    a_race = _goal(db_session, gareth, date(2026, 10, 18), name="Race A")
    _goal(db_session, gareth, date(2026, 11, 15), name="Club hill climb",
          priority=EventPriority.c_race)
    clock(date(2026, 9, 1))
    generate_plan(db_session, gareth)
    [plan] = _active(db_session, gareth)
    assert plan.goal_event_id == a_race.id and plan.end_date == date(2026, 11, 15)

    clock(date(2026, 10, 18))
    a_race.status = GoalStatus.completed
    db_session.commit()
    assert ensure_season(db_session, gareth) == "unchanged"

    clock(date(2026, 10, 19))
    assert ensure_season(db_session, gareth) == "goal_plan"
    [rebuilt] = _active(db_session, gareth)
    assert rebuilt.name == "Plan for Club hill climb"
    first_week = [
        w for w in get_workouts_by_date(db_session, gareth.id, week_start=date(2026, 10, 19))
        if w.phase_id is not None and w.phase.plan_id == rebuilt.id
    ]
    assert first_week and {
        str(getattr(w.workout_type, "value", w.workout_type)) for w in first_week
    } <= {"recovery", "endurance"}
    assert ensure_season(db_session, gareth) == "unchanged"


def test_a_session_ridden_today_survives_an_evening_goal(db_session, clock):
    """The block's Friday session was ridden and matched. Filing a goal that
    evening hid it and laid a fresh planned session on today, which would
    count as missed tomorrow."""
    gareth, _, _ = _incident(db_session)
    ensure_season(db_session, gareth)
    [todays] = get_workouts_by_date(db_session, gareth.id, target_date=FRIDAY)
    ride = _ride_on(db_session, gareth, FRIDAY)
    assert plan_compliance_service.link_ride(db_session, ride).id == todays.id

    _new_goal(db_session, gareth)

    shown = list_workouts(target_date=FRIDAY, week=None, current_user=gareth, db=db_session)
    assert [w.id for w in shown] == [todays.id]
    assert str(getattr(shown[0].status, "value", shown[0].status)) == "completed"
    context = json.loads(_build_rider_context(db_session, gareth))
    today_entries = [e for e in context["this_week"] if e["date"] == FRIDAY.isoformat()]
    assert [e["id"] for e in today_entries] == [todays.id]


def test_a_new_plan_leaves_today_clear_after_an_unplanned_ride(db_session, clock):
    gareth, _, _ = _incident(db_session)
    _ride_on(db_session, gareth, FRIDAY, title="Coffee ride")

    ensure_season(db_session, gareth)

    assert get_workouts_by_date(db_session, gareth.id, target_date=FRIDAY) == []
    assert get_workouts_by_date(db_session, gareth.id, week_start=date(2026, 10, 12))


def test_two_running_plans_are_put_right(db_session, clock):
    """Two builds that raced each other left two active plans; the keeper only
    ever ended expired ones, so both sets of sessions showed for 12 weeks."""
    gareth, _, _ = _incident(db_session)
    ensure_season(db_session, gareth)
    [first] = _active(db_session, gareth)
    second = TrainingPlan(
        user_id=gareth.id, name=BLOCK_NAME, start_date=FRIDAY,
        end_date=FRIDAY + timedelta(weeks=12), status=PlanStatus.active,
        periodization_model=PeriodizationModel.traditional,
        created_at=datetime.utcnow() + timedelta(seconds=5),
    )
    db_session.add(second)
    db_session.commit()

    assert ensure_season(db_session, gareth) == "retired"
    assert [p.id for p in _active(db_session, gareth)] == [second.id]
    db_session.refresh(first)
    assert first.status == PlanStatus.cancelled  # started today: no past to keep


def test_the_riders_periodisation_carries_into_the_block(db_session, clock):
    gareth, _, old = _incident(db_session)
    old.periodization_model = PeriodizationModel.polarized
    db_session.commit()

    ensure_season(db_session, gareth)

    [block] = _active(db_session, gareth)
    assert str(getattr(block.periodization_model, "value", block.periodization_model)) == "polarized"


def test_switched_off_the_keeper_does_nothing_on_a_goal_write_either(db_session, clock, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "season_keeper_enabled", False)
    gareth, goal, old = _incident(db_session)

    state = season_state(db_session, gareth)
    assert state["rebuilds"] is False
    assert "Build my season" in season_note(state)
    goals_api.submit_goal_assessment(
        goal.id, GoalAssessmentSubmit(status="completed", overall_satisfaction=8),
        current_user=gareth, db=db_session,
    )
    filed = _execute_tool(db_session, gareth, "create_goal", {
        "event_name": "Mallorca 312", "event_date": "2027-05-30",
        "event_type": "gran_fondo", "priority": "a_race",
    })

    db_session.refresh(old)
    assert old.status == PlanStatus.active  # untouched
    assert db_session.query(TrainingPlan).count() == 1
    assert "rebuilt" not in filed


def test_the_morning_note_spends_its_one_question_on_the_race(db_session, clock):
    gareth, goal, _ = _incident(db_session)
    ensure_season(db_session, gareth)

    owed = season_note(season_state(db_session, gareth), in_chat=False)
    assert "Spend the one question on how the race went" in owed
    assert "Propose the next goal" not in owed

    goal.status = GoalStatus.completed
    db_session.commit()
    filed = season_note(season_state(db_session, gareth), in_chat=False)
    assert "Propose the next goal yourself" in filed and "how the race went" not in filed
    assert not any(d in owed + filed for d in DASHES)


def test_dates_in_the_season_note_are_in_words(db_session, clock):
    gareth = _gareth(db_session)
    race = _goal(db_session, gareth, date(2026, 10, 25), name="Race A", status=GoalStatus.dns)
    plan = _old_plan(db_session, gareth, race, start=date(2026, 9, 1), end=date(2026, 12, 20))

    note = season_note(season_state(db_session, gareth))

    assert "set for Sunday 25 October 2026" in note
    assert "until Sunday 20 December 2026" in note
    assert "2026-" not in note
    assert plan.id


def test_a_goal_tool_whose_plan_read_fails_still_says_the_goal_is_filed(db_session, clock, monkeypatch):
    gareth, _, _ = _incident(db_session)

    def broken(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(season_service, "season_state", broken)

    filed = coach_service._run_tool(db_session, gareth, "create_goal", {
        "event_name": "Mallorca 312", "event_date": "2027-05-30",
        "event_type": "gran_fondo", "priority": "a_race",
    })

    assert filed.startswith("Filed the goal 'Mallorca 312'")
    assert "The goal is filed, but whether the plan changed could not be checked" in filed
    assert db_session.query(GoalEvent).filter(GoalEvent.event_name == "Mallorca 312").count() == 1
