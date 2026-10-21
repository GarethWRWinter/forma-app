"""Tests for training plan generation and workout management."""

from datetime import date, datetime, time, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.workout_templates import (
    PHASE_WORKOUT_MIX,
    WORKOUT_TEMPLATES,
    estimate_tss,
    get_template,
)
from app.models.base import Base
from app.models.onboarding import GoalEvent
from app.models.ride import Ride, RideSource
from app.models.safety import HealthScreening
from app.models.training import (
    TrainingPlan,
    TrainingPhase,
    Workout,
    WorkoutStatus,
    WorkoutStep,
)
from app.models.user import User
from app.services import safety_service as ss
from app.services.plan_service import (
    ACCOUNT_HOLD_PREFIX,
    BREAK_PREFIX,
    EASY_PLAN_FOCUS,
    HOLD_LABELS,
    HOLD_PREFIX,
    generate_plan,
    get_plan,
    get_plan_workouts,
    get_plans,
    get_workout,
    get_workouts_by_date,
    is_held,
    link_ride_to_workout,
    sync_hold_marks,
    update_workout_status,
)


def _make_test_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _make_test_user(db, user_id="test-user-1") -> User:
    user = User(
        id=user_id,
        email=f"{user_id}@example.com",
        hashed_password="hashed",
        full_name="Test Rider",
        ftp=250,
        weight_kg=75.0,
        weekly_hours_available=8.0,
    )
    db.add(user)
    db.commit()
    return user


class TestWorkoutTemplates:
    def test_all_types_have_templates(self):
        """Every workout type should have at least one template."""
        for wtype in ["recovery", "endurance", "tempo", "sweet_spot", "threshold", "vo2max", "sprint"]:
            templates = WORKOUT_TEMPLATES.get(wtype, [])
            assert len(templates) >= 1, f"No templates for {wtype}"

    def test_get_template_by_type(self):
        """get_template should return a valid template."""
        template = get_template("sweet_spot")
        assert template["workout_type"] == "sweet_spot"
        assert "steps" in template
        assert len(template["steps"]) > 0

    def test_get_template_closest_duration(self):
        """get_template should pick closest duration match."""
        # Short endurance
        short = get_template("endurance", duration_hint=3600)
        assert short["duration_seconds"] == 3600

        # Long endurance
        long = get_template("endurance", duration_hint=10000)
        assert long["duration_seconds"] == 10800

    def test_estimate_tss(self):
        """TSS estimation should match formula."""
        template = {"duration_seconds": 3600, "planned_if": 1.0}
        tss = estimate_tss(template, 250)
        # 1 hour at IF=1.0 -> TSS = 100
        assert abs(tss - 100.0) < 0.1

    def test_estimate_tss_easy(self):
        """Easy ride should have low TSS."""
        template = {"duration_seconds": 2700, "planned_if": 0.55}
        tss = estimate_tss(template, 250)
        # 45 min at IF=0.55 -> TSS ~= 22.7
        assert tss < 30

    def test_template_steps_valid(self):
        """All template steps should have required fields."""
        for wtype, templates in WORKOUT_TEMPLATES.items():
            for template in templates:
                for step in template["steps"]:
                    assert "step_type" in step
                    assert "duration_seconds" in step
                    assert step["duration_seconds"] > 0


class TestPlanGeneration:
    def test_generates_plan_with_phases(self):
        """Should create a plan with phases."""
        db = _make_test_db()
        user = _make_test_user(db)

        plan = generate_plan(db, user)

        assert plan.id is not None
        assert plan.user_id == user.id
        assert plan.status == "active"
        assert len(plan.phases) >= 2
        assert plan.start_date == date.today()

    def test_plan_phases_are_contiguous(self):
        """Phase dates should be contiguous (no gaps)."""
        db = _make_test_db()
        user = _make_test_user(db)

        plan = generate_plan(db, user)
        phases = sorted(plan.phases, key=lambda p: p.sort_order)

        for i in range(1, len(phases)):
            prev_end = phases[i - 1].end_date
            curr_start = phases[i].start_date
            gap = (curr_start - prev_end).days
            assert gap <= 1, f"Gap of {gap} days between phases {i-1} and {i}"

    def test_generates_workouts(self):
        """Plan should include workouts for each phase."""
        db = _make_test_db()
        user = _make_test_user(db)

        plan = generate_plan(db, user)

        total_workouts = 0
        for phase in plan.phases:
            assert len(phase.workouts) > 0, f"Phase {phase.phase_type} has no workouts"
            total_workouts += len(phase.workouts)

        assert total_workouts >= 10  # At least 10 workouts in a 12-week plan

    def test_workouts_have_steps(self):
        """Each workout should have steps."""
        db = _make_test_db()
        user = _make_test_user(db)

        plan = generate_plan(db, user)

        for phase in plan.phases:
            for workout in phase.workouts:
                if workout.workout_type != "rest":
                    assert len(workout.steps) > 0, f"Workout {workout.title} has no steps"

    def test_plan_with_goal_event(self):
        """Plan should end on the goal event date."""
        db = _make_test_db()
        user = _make_test_user(db)

        event_date = date.today() + timedelta(weeks=16)
        goal = GoalEvent(
            user_id=user.id,
            event_name="A Race",
            event_date=event_date,
            event_type="road_race",
            priority="a_race",
        )
        db.add(goal)
        db.commit()

        plan = generate_plan(db, user, goal_event_id=goal.id)

        assert plan.goal_event_id == goal.id
        assert plan.end_date == event_date
        assert "A Race" in plan.name

    def test_short_plan_4_weeks(self):
        """Short 4-week plan should have build + peak phases."""
        db = _make_test_db()
        user = _make_test_user(db)

        event_date = date.today() + timedelta(weeks=4)
        goal = GoalEvent(
            user_id=user.id,
            event_name="Quick Race",
            event_date=event_date,
            event_type="crit",
            priority="a_race",
        )
        db.add(goal)
        db.commit()

        plan = generate_plan(db, user, goal_event_id=goal.id)

        phase_types = [p.phase_type for p in plan.phases]
        assert "build" in phase_types
        assert "peak" in phase_types

    def test_periodization_models(self):
        """Different periodization models should produce different phase distributions."""
        db = _make_test_db()
        user = _make_test_user(db)

        plan_trad = generate_plan(db, user, periodization_model="traditional", name="Trad")
        plan_pol = generate_plan(db, user, periodization_model="polarized", name="Polarized")

        trad_phases = {p.phase_type: p for p in plan_trad.phases}
        pol_phases = {p.phase_type: p for p in plan_pol.phases}

        # Both should have base phase
        assert "base" in trad_phases
        assert "base" in pol_phases


class TestPlanQueries:
    def test_get_plans(self):
        """Should list user's plans."""
        db = _make_test_db()
        user = _make_test_user(db)

        generate_plan(db, user, name="Plan 1")
        generate_plan(db, user, name="Plan 2")

        plans = get_plans(db, user.id)
        assert len(plans) == 2

    def test_get_plan_by_id(self):
        """Should retrieve a specific plan."""
        db = _make_test_db()
        user = _make_test_user(db)

        plan = generate_plan(db, user)
        fetched = get_plan(db, plan.id, user.id)

        assert fetched is not None
        assert fetched.id == plan.id

    def test_get_plan_wrong_user(self):
        """Should not return another user's plan."""
        db = _make_test_db()
        user1 = _make_test_user(db, "user-1")
        user2 = _make_test_user(db, "user-2")

        plan = generate_plan(db, user1)
        fetched = get_plan(db, plan.id, user2.id)

        assert fetched is None

    def test_get_plan_workouts(self):
        """Should return all workouts for a plan."""
        db = _make_test_db()
        user = _make_test_user(db)

        plan = generate_plan(db, user)
        workouts = get_plan_workouts(db, plan.id, user.id)

        assert len(workouts) > 0
        # Verify they belong to the plan's phases
        phase_ids = {p.id for p in plan.phases}
        for w in workouts:
            assert w.phase_id in phase_ids


class TestWorkoutManagement:
    def test_get_workouts_by_date(self):
        """Should find workouts for a specific date."""
        db = _make_test_db()
        user = _make_test_user(db)

        plan = generate_plan(db, user)
        # Get the first workout's date
        all_workouts = get_plan_workouts(db, plan.id, user.id)
        if all_workouts:
            target_date = all_workouts[0].scheduled_date
            found = get_workouts_by_date(db, user.id, target_date=target_date)
            assert len(found) >= 1

    def test_get_workouts_by_week(self):
        """Should find workouts for a week range."""
        db = _make_test_db()
        user = _make_test_user(db)

        plan = generate_plan(db, user)
        week_start = date.today()
        found = get_workouts_by_date(db, user.id, week_start=week_start)

        assert len(found) >= 1

    def test_update_workout_status(self):
        """Should update workout status."""
        db = _make_test_db()
        user = _make_test_user(db)

        plan = generate_plan(db, user)
        workouts = get_plan_workouts(db, plan.id, user.id)
        workout = workouts[0]

        assert workout.status == "planned"
        updated = update_workout_status(db, workout, "completed")
        assert updated.status == "completed"

    def test_link_ride_to_workout(self):
        """Should link a ride ID to a workout."""
        db = _make_test_db()
        user = _make_test_user(db)

        plan = generate_plan(db, user)
        workouts = get_plan_workouts(db, plan.id, user.id)
        workout = workouts[0]

        updated = link_ride_to_workout(db, workout, "ride-123")
        assert updated.actual_ride_id == "ride-123"
        assert updated.status == "completed"

    def test_get_workout_with_steps(self):
        """Should return a workout with its steps."""
        db = _make_test_db()
        user = _make_test_user(db)

        plan = generate_plan(db, user)
        workouts = get_plan_workouts(db, plan.id, user.id)
        workout = workouts[0]

        fetched = get_workout(db, workout.id, user.id)
        assert fetched is not None
        assert len(fetched.steps) > 0
        # Steps should be ordered
        orders = [s.step_order for s in fetched.steps]
        assert orders == sorted(orders)


class TestLongRideAndTaper:
    """Found in the launch audit (4 Oct 2026): an 8-hour sportive plan never
    rode longer than 2 hours, a 31-week plan tapered for 6 weeks, and a rider
    with no goal was tapered for a race that did not exist."""

    def _sportive_plan(self, weeks_out=31, minutes=480, hours=6.0):
        db = _make_test_db()
        user = _make_test_user(db)
        user.weekly_hours_available = hours
        user.preferred_hard_days = [5, 6]
        user.rest_days = []
        db.commit()
        event_date = date.today() + timedelta(weeks=weeks_out)
        goal = GoalEvent(
            user_id=user.id, event_name="Fred Whitton", event_type="sportive",
            priority="a_race", event_date=event_date, target_duration_minutes=minutes,
        )
        db.add(goal)
        db.commit()
        plan = generate_plan(db, user, goal_event_id=goal.id)
        return db, user, plan, event_date

    def test_long_ride_builds_towards_the_event(self):
        db, user, plan, _ = self._sportive_plan()
        workouts = get_plan_workouts(db, plan.id, user.id)
        longest = max(w.planned_duration_seconds for w in workouts)
        # 55% of a 6-hour week, rounded to the quarter hour.
        assert longest >= 3 * 3600
        first_month = [w for w in workouts if w.scheduled_date < date.today() + timedelta(weeks=4)]
        assert max(w.planned_duration_seconds for w in first_month) < longest

    def test_long_ride_lands_on_the_weekend(self):
        db, user, plan, _ = self._sportive_plan()
        long_rides = [
            w for w in get_plan_workouts(db, plan.id, user.id)
            if w.description.startswith("The long ride")
        ]
        assert long_rides
        assert all(w.scheduled_date.weekday() in (5, 6) for w in long_rides)

    def test_week_stays_close_to_the_riders_hours(self):
        db, user, plan, _ = self._sportive_plan(hours=6.0)
        by_week: dict[date, int] = {}
        for w in get_plan_workouts(db, plan.id, user.id):
            monday = w.scheduled_date - timedelta(days=w.scheduled_date.weekday())
            by_week[monday] = by_week.get(monday, 0) + w.planned_duration_seconds
        assert max(by_week.values()) <= 6.0 * 3600 * 1.15

    def test_taper_is_at_most_two_weeks(self):
        db, user, plan, _ = self._sportive_plan(weeks_out=31)
        race = [p for p in plan.phases if str(getattr(p.phase_type, "value", p.phase_type)) == "race"]
        assert len(race) == 1
        assert (race[0].end_date - race[0].start_date).days <= 20

    def test_race_week_is_short_and_easy(self):
        db, user, plan, event_date = self._sportive_plan()
        race_week = [
            w for w in get_plan_workouts(db, plan.id, user.id)
            if 0 < (event_date - w.scheduled_date).days <= 6
        ]
        assert all(w.planned_duration_seconds <= 3600 for w in race_week)
        last_two = [w for w in race_week if (event_date - w.scheduled_date).days <= 2]
        assert all(
            str(getattr(w.workout_type, "value", w.workout_type)) in ("recovery", "endurance")
            for w in last_two
        )

    def test_no_goal_means_no_taper(self):
        db = _make_test_db()
        user = _make_test_user(db)
        plan = generate_plan(db, user)
        kinds = {str(getattr(p.phase_type, "value", p.phase_type)) for p in plan.phases}
        assert kinds == {"base", "build"}

    def test_crit_long_ride_stops_at_two_hours(self):
        db = _make_test_db()
        user = _make_test_user(db)
        goal = GoalEvent(
            user_id=user.id, event_name="Crit series", event_type="crit",
            priority="a_race", event_date=date.today() + timedelta(weeks=12),
        )
        db.add(goal)
        db.commit()
        plan = generate_plan(db, user, goal_event_id=goal.id)
        longest = max(w.planned_duration_seconds for w in get_plan_workouts(db, plan.id, user.id))
        assert longest <= 2 * 3600


class TestSafetyGate:
    """Plan generation obeys safety_service.allowed_intensity: an open hold
    or an uncleared health answer keeps the whole plan easy (or on hold);
    the layoff gate keeps only its first weeks easy."""

    HARD = {"tempo", "sweet_spot", "threshold", "vo2max", "sprint"}

    @staticmethod
    def _type(w) -> str:
        return str(getattr(w.workout_type, "value", w.workout_type))

    def _plan(self, db, user, weeks_out=12, event_type="road_race"):
        goal = GoalEvent(
            user_id=user.id, event_name="Spring Classic", event_type=event_type,
            priority="a_race", event_date=date.today() + timedelta(weeks=weeks_out),
        )
        db.add(goal)
        db.commit()
        plan = generate_plan(db, user, goal_event_id=goal.id)
        return plan, get_plan_workouts(db, plan.id, user.id)

    def _screening(self, db, user, tier="none", long_break=False, cleared=False):
        db.add(HealthScreening(
            user_id=user.id, version=ss.SCREENING_VERSION, answers={"q1": tier != "none"},
            long_break=long_break, any_yes=tier != "none", tier=tier,
            clearance_confirmed_at=datetime.utcnow() if cleared else None,
        ))
        db.commit()

    def test_a_normal_plan_has_hard_sessions(self):
        db = _make_test_db()
        user = _make_test_user(db)
        _, workouts = self._plan(db, user)
        assert {self._type(w) for w in workouts} & self.HARD
        assert not any(is_held(w) for w in workouts)

    def test_an_easy_hold_keeps_the_whole_plan_easy(self):
        db = _make_test_db()
        user = _make_test_user(db)
        ss.open_hold(db, user, "easy_only", "Knee", "coach_tool")
        plan, workouts = self._plan(db, user)
        assert workouts
        assert {self._type(w) for w in workouts} <= ss.EASY_TYPES
        assert all(ss.max_step_pct(w) <= ss.EASY_CAP for w in workouts)
        assert all(ss.workout_allowed(w, "easy") for w in workouts)
        assert {p.focus for p in plan.phases} == {EASY_PLAN_FOCUS}
        assert not any(is_held(w) for w in workouts)

    def test_an_uncleared_health_answer_keeps_the_plan_easy(self):
        db = _make_test_db()
        user = _make_test_user(db)
        self._screening(db, user, tier="easy_only")
        _, workouts = self._plan(db, user)
        assert {self._type(w) for w in workouts} <= ss.EASY_TYPES

    def test_a_cleared_health_answer_does_not(self):
        db = _make_test_db()
        user = _make_test_user(db)
        self._screening(db, user, tier="easy_only", cleared=True)
        _, workouts = self._plan(db, user)
        assert {self._type(w) for w in workouts} & self.HARD

    def test_a_full_hold_builds_the_plan_with_every_session_on_hold(self):
        db = _make_test_db()
        user = _make_test_user(db)
        ss.open_hold(db, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
        plan, workouts = self._plan(db, user)
        assert workouts and all(is_held(w) for w in workouts)
        assert all(w.status == "planned" for w in workouts)
        assert all(not ss.workout_allowed(w, ss.allowed_intensity(db, user)) for w in workouts)
        # The sessions are the plan they ride once cleared.
        assert {self._type(w) for w in workouts} & self.HARD
        assert EASY_PLAN_FOCUS not in {p.focus for p in plan.phases}

    def test_the_layoff_gate_eases_only_its_window(self):
        db = _make_test_db()
        user = _make_test_user(db)
        self._screening(db, user, long_break=True)
        gate = ss.layoff_gate_until(db, user)
        assert gate is not None
        _, workouts = self._plan(db, user)
        inside = [w for w in workouts if w.scheduled_date < gate]
        after = [w for w in workouts if w.scheduled_date >= gate]
        assert inside and after
        assert all(ss.workout_allowed(w, "easy") for w in inside)
        assert {self._type(w) for w in after} & self.HARD
        line = f"Hard sessions start again on {gate.day} {gate:%B}."
        eased = [w for w in inside if line in (w.description or "")]
        assert eased, "a session made easy by the gate says when hard sessions return"
        assert not any(line in (w.description or "") for w in after)

    def test_a_long_gap_in_the_rides_gates_the_plan(self):
        db = _make_test_db()
        user = _make_test_user(db)
        db.add(Ride(
            user_id=user.id, source=RideSource.manual,
            ride_date=datetime.combine(date.today() - timedelta(days=40), time(9, 0)),
        ))
        db.commit()
        gate = ss.layoff_gate_until(db, user)
        assert gate is not None
        _, workouts = self._plan(db, user)
        assert all(ss.workout_allowed(w, "easy") for w in workouts if w.scheduled_date < gate)

    def test_easy_fails_closed_on_a_template_above_the_cap(self, monkeypatch):
        import app.services.plan_service as ps

        real = ps.get_template

        def hot_endurance(wtype, duration_hint=None):
            template = real(wtype, duration_hint)
            if wtype == "endurance":
                template = {**template, "steps": [
                    {**s, "power_target_pct": 0.85, "power_high_pct": 0.9}
                    for s in template["steps"]
                ]}
            return template

        monkeypatch.setattr(ps, "get_template", hot_endurance)
        db = _make_test_db()
        user = _make_test_user(db)
        ss.open_hold(db, user, "easy_only", "Pregnancy", "screening")
        _, workouts = self._plan(db, user)
        assert all(ss.workout_allowed(w, "easy") for w in workouts)
        assert "recovery" in {self._type(w) for w in workouts}

    def test_the_plan_never_schedules_an_ftp_test(self):
        db = _make_test_db()
        user = _make_test_user(db)
        _, workouts = self._plan(db, user)
        assert not any("ftp" in w.title.lower() or "test" in w.title.lower() for w in workouts)

    def test_sync_hold_marks_labels_and_releases(self):
        db = _make_test_db()
        user = _make_test_user(db)
        _, workouts = self._plan(db, user)
        before = {w.id: w.description for w in workouts}
        assert sync_hold_marks(db, user.id) == 0

        ss.open_hold(db, user, "hold_all", "Fainted", "detector", red_flag="fainting")
        labelled = sync_hold_marks(db, user.id)
        assert labelled == len([w for w in workouts if w.scheduled_date >= date.today()])
        assert sync_hold_marks(db, user.id) == 0  # idempotent
        assert all(is_held(w) for w in workouts if w.scheduled_date >= date.today())

        ss.confirm_clearance(db, user, "A&E doctor", None)
        assert sync_hold_marks(db, user.id) == labelled
        assert {w.id: w.description for w in workouts} == before


class TestHoldLabelsFollowTheGate:
    """Review finding 17: the on-hold label matches the gate session by
    session whenever a hold changes, for modified sessions as well as
    planned ones, and for a plan built under a full hold that keeps its
    hard sessions."""

    def _plan(self, db, user):
        goal = GoalEvent(
            user_id=user.id, event_name="Spring Classic", event_type="road_race",
            priority="a_race", event_date=date.today() + timedelta(weeks=12),
        )
        db.add(goal)
        db.commit()
        plan = generate_plan(db, user, goal_event_id=goal.id)
        return get_plan_workouts(db, plan.id, user.id)

    @staticmethod
    def _future(workouts):
        return [w for w in workouts if w.scheduled_date >= date.today()]

    @staticmethod
    def _hard(w) -> bool:
        return not ss.workout_allowed(w, "easy")

    @staticmethod
    def _label(w):
        return next((p for p in HOLD_LABELS if (w.description or "").startswith(p)), None)

    def test_lifting_a_full_hold_under_an_easy_answer_keeps_hard_sessions_labelled(self):
        db = _make_test_db()
        user = _make_test_user(db)
        db.add(HealthScreening(
            user_id=user.id, version=ss.SCREENING_VERSION, answers={"q6": True},
            any_yes=True, tier="easy_only",
        ))
        db.commit()
        hold = ss.open_hold(db, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
        workouts = self._plan(db, user)
        future = self._future(workouts)
        assert future and all(is_held(w) for w in future)
        hard = [w for w in future if self._hard(w)]
        assert hard, "a plan built under a full hold keeps its hard sessions"

        ss.lift_hold(db, user, hold.id, "mistake")
        assert ss.allowed_intensity(db, user) == "easy"
        sync_hold_marks(db, user.id)
        for w in future:
            assert is_held(w) == self._hard(w), (w.title, w.workout_type)
        assert all(self._label(w) == HOLD_PREFIX for w in hard)
        assert sync_hold_marks(db, user.id) == 0

    def test_modified_sessions_take_the_label_too(self):
        db = _make_test_db()
        user = _make_test_user(db)
        future = self._future(self._plan(db, user))
        for w in future[:3]:
            w.status = WorkoutStatus.modified
        db.commit()
        ss.open_hold(db, user, "hold_all", "Fainted", "detector", red_flag="fainting")
        sync_hold_marks(db, user.id)
        assert all(is_held(w) for w in future[:3])

        ss.confirm_clearance(db, user, "A&E doctor", None)
        sync_hold_marks(db, user.id)
        assert not any(is_held(w) for w in future)

    def test_an_easy_hold_on_an_existing_plan_labels_only_its_hard_sessions(self):
        db = _make_test_db()
        user = _make_test_user(db)
        workouts = self._plan(db, user)
        before = {w.id: w.description for w in workouts}
        future = self._future(workouts)
        ss.open_hold(db, user, "easy_only", "Sore knee", "coach_tool", red_flag="injury")
        sync_hold_marks(db, user.id)
        assert any(self._hard(w) for w in future)
        for w in future:
            assert self._label(w) == (HOLD_PREFIX if self._hard(w) else None)

        ss.confirm_clearance(db, user, "Physio", None)
        sync_hold_marks(db, user.id)
        assert {w.id: w.description for w in workouts} == before

    def test_after_clearance_hard_sessions_in_the_layoff_window_still_wait(self):
        db = _make_test_db()
        user = _make_test_user(db)
        workouts = self._plan(db, user)
        # Off the bike for six weeks before this: the first two weeks back
        # are easy, whatever the plan was built with.
        db.add(Ride(
            user_id=user.id, source=RideSource.manual,
            ride_date=datetime.combine(date.today() - timedelta(days=40), time(9, 0)),
        ))
        db.commit()
        gate = ss.layoff_gate_until(db, user)
        assert gate is not None
        ss.open_hold(db, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
        sync_hold_marks(db, user.id)
        ss.confirm_clearance(db, user, "Cardiologist", None)
        sync_hold_marks(db, user.id)

        future = self._future(workouts)
        inside = [w for w in future if w.scheduled_date < gate]
        assert any(self._hard(w) for w in inside)
        for w in future:
            wanted = BREAK_PREFIX if w.scheduled_date < gate and self._hard(w) else None
            assert self._label(w) == wanted, (w.scheduled_date, w.workout_type)

    def test_an_under_18_hold_never_mentions_a_doctor(self):
        db = _make_test_db()
        user = _make_test_user(db)
        ss.open_hold(db, user, "hold_all", "Said they are 15", "detector", red_flag="minor")
        future = self._future(self._plan(db, user))
        assert future and all(self._label(w) == ACCOUNT_HOLD_PREFIX for w in future)
        assert not any("doctor" in (w.description or "").split(".")[0] for w in future)

    def test_a_break_hold_from_the_coach_reads_as_a_break(self):
        db = _make_test_db()
        user = _make_test_user(db)
        future = self._future(self._plan(db, user))
        ss.open_hold(db, user, "easy_only", "Six weeks off", "layoff", red_flag="layoff")
        sync_hold_marks(db, user.id)
        hard = [w for w in future if self._hard(w)]
        assert hard and all(self._label(w) == BREAK_PREFIX for w in hard)

    def test_past_and_closed_sessions_never_gain_a_label_but_lose_a_stale_one(self):
        db = _make_test_db()
        user = _make_test_user(db)
        future = self._future(self._plan(db, user))
        done, skipped = future[0], future[1]
        done.status = WorkoutStatus.completed
        skipped.status = WorkoutStatus.skipped
        past = Workout(
            user_id=user.id, scheduled_date=date.today() - timedelta(days=3),
            title="Old VO2", description="Five by five.", workout_type="vo2max",
            planned_duration_seconds=3600, status=WorkoutStatus.planned,
        )
        db.add(past)
        db.commit()
        ss.open_hold(db, user, "hold_all", "Fainted", "detector", red_flag="fainting")
        sync_hold_marks(db, user.id)
        assert not is_held(done) and not is_held(skipped) and not is_held(past)

        # A label left on a session that has since closed comes off once
        # the gate no longer holds it.
        done.description = HOLD_PREFIX + done.description
        db.commit()
        ss.confirm_clearance(db, user, "GP", None)
        sync_hold_marks(db, user.id)
        assert not is_held(done)

    def test_a_fresh_plan_already_matches_the_gate(self):
        """Whatever the gate, sync_hold_marks has nothing to change on a plan
        that was just built: the two read the gate the same way."""
        cases = {
            "clear": lambda db, u: None,
            "easy hold": lambda db, u: ss.open_hold(db, u, "easy_only", "Knee", "coach_tool"),
            "full hold": lambda db, u: ss.open_hold(
                db, u, "hold_all", "Chest pain", "detector", red_flag="chest_pain"),
            "under 18": lambda db, u: ss.open_hold(
                db, u, "hold_all", "Said they are 15", "detector", red_flag="minor"),
            "break": lambda db, u: db.add(Ride(
                user_id=u.id, source=RideSource.manual,
                ride_date=datetime.combine(date.today() - timedelta(days=40), time(9, 0)),
            )),
        }
        for name, setup in cases.items():
            db = _make_test_db()
            user = _make_test_user(db)
            setup(db, user)
            db.commit()
            self._plan(db, user)
            assert sync_hold_marks(db, user.id) == 0, name


class TestExperienceDefault:
    def _first_long_ride(self, experience):
        db = _make_test_db()
        user = _make_test_user(db)
        user.experience_level = experience
        user.preferred_hard_days = []
        user.rest_days = []
        db.commit()
        goal = GoalEvent(
            user_id=user.id, event_name="Fred Whitton", event_type="sportive",
            priority="a_race", event_date=date.today() + timedelta(weeks=20),
            target_duration_minutes=480,
        )
        db.add(goal)
        db.commit()
        plan = generate_plan(db, user, goal_event_id=goal.id)
        long_rides = [
            w for w in get_plan_workouts(db, plan.id, user.id)
            if w.description.startswith("The long ride")
        ]
        return long_rides[0].planned_duration_seconds

    def test_no_experience_level_plans_as_a_beginner(self):
        # A beginner's long ride starts at 75 minutes; an intermediate's at 90.
        assert self._first_long_ride(None) == self._first_long_ride("beginner") == 75 * 60
        assert self._first_long_ride("intermediate") == 90 * 60
