"""Tests for training plan generation and workout management."""

from datetime import date, timedelta

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
from app.models.training import (
    TrainingPlan,
    TrainingPhase,
    Workout,
    WorkoutStep,
)
from app.models.user import User
from app.services.plan_service import (
    generate_plan,
    get_plan,
    get_plan_workouts,
    get_plans,
    get_workout,
    get_workouts_by_date,
    link_ride_to_workout,
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
