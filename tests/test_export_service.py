"""Tests for workout and ride export services (ZWO, GPX)."""

import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.ride import Ride, RideData, RideSource
from app.models.training import Workout, WorkoutStep, WorkoutType, WorkoutStatus, StepType
from app.models.user import User
from app.services.export_service import ride_to_gpx, workout_to_erg, workout_to_mrc, workout_to_zwo


def _make_test_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _make_test_user(db) -> User:
    user = User(
        id="test-user-1",
        email="test@example.com",
        hashed_password="hashed",
        full_name="Test Rider",
        ftp=250,
    )
    db.add(user)
    db.commit()
    return user


def _make_workout_with_steps(db, user) -> Workout:
    """Create a workout with warmup, intervals, and cooldown."""
    workout = Workout(
        user_id=user.id,
        scheduled_date=date(2025, 1, 15),
        title="Test Intervals",
        description="Threshold workout",
        workout_type=WorkoutType.threshold,
        planned_duration_seconds=3600,
        status=WorkoutStatus.planned,
    )
    db.add(workout)
    db.flush()

    steps = [
        WorkoutStep(workout_id=workout.id, step_order=0, step_type=StepType.warmup,
                     duration_seconds=600, power_target_pct=0.55, power_low_pct=0.45, power_high_pct=0.65),
        WorkoutStep(workout_id=workout.id, step_order=1, step_type=StepType.interval_on,
                     duration_seconds=600, power_target_pct=0.97, repeat_count=3, cadence_target=90),
        WorkoutStep(workout_id=workout.id, step_order=2, step_type=StepType.interval_off,
                     duration_seconds=300, power_target_pct=0.55),
        WorkoutStep(workout_id=workout.id, step_order=3, step_type=StepType.cooldown,
                     duration_seconds=300, power_target_pct=0.50, power_low_pct=0.40, power_high_pct=0.55),
    ]
    for s in steps:
        db.add(s)
    db.commit()
    db.refresh(workout)
    return workout


class TestZWOExport:
    def test_basic_zwo_structure(self):
        """ZWO should have valid XML structure."""
        db = _make_test_db()
        user = _make_test_user(db)
        workout = _make_workout_with_steps(db, user)

        zwo = workout_to_zwo(workout, ftp=250)
        root = ET.fromstring(zwo)

        assert root.tag == "workout_file"
        assert root.find("name").text == "Test Intervals"
        assert root.find("sportType").text == "bike"
        assert root.find("workout") is not None

    def test_warmup_element(self):
        """Warmup step should produce <Warmup> element."""
        db = _make_test_db()
        user = _make_test_user(db)
        workout = _make_workout_with_steps(db, user)

        zwo = workout_to_zwo(workout, ftp=250)
        root = ET.fromstring(zwo)
        wo = root.find("workout")

        warmup = wo.find("Warmup")
        assert warmup is not None
        assert warmup.get("Duration") == "600"

    def test_intervals_element(self):
        """Interval on/off pair should produce <IntervalsT> element."""
        db = _make_test_db()
        user = _make_test_user(db)
        workout = _make_workout_with_steps(db, user)

        zwo = workout_to_zwo(workout, ftp=250)
        root = ET.fromstring(zwo)
        wo = root.find("workout")

        intervals = wo.find("IntervalsT")
        assert intervals is not None
        assert intervals.get("Repeat") == "3"
        assert intervals.get("OnDuration") == "600"
        assert intervals.get("OffDuration") == "300"

    def test_cooldown_element(self):
        """Cooldown step should produce <Cooldown> element."""
        db = _make_test_db()
        user = _make_test_user(db)
        workout = _make_workout_with_steps(db, user)

        zwo = workout_to_zwo(workout, ftp=250)
        root = ET.fromstring(zwo)
        wo = root.find("workout")

        cooldown = wo.find("Cooldown")
        assert cooldown is not None


def _course(text: str) -> list[tuple[float, float]]:
    """The (minute, value) points between COURSE DATA markers."""
    body = text.split("[COURSE DATA]")[1].split("[END COURSE DATA]")[0]
    return [tuple(float(x) for x in line.split("\t")) for line in body.strip().splitlines()]


class TestTrainerFileOrder:
    """3 x 10 min on, 5 min off is ridden on, off, on, off, on, off. The ERG and
    MRC files repeated each step on its own: 30 minutes straight at threshold,
    then a single recovery (found 16 Oct 2026)."""

    def test_erg_alternates_work_and_recovery(self):
        db = _make_test_db()
        workout = _make_workout_with_steps(db, _make_test_user(db))

        points = _course(workout_to_erg(workout, ftp=250))
        # Each step is a pair of points: (start, watts), (end, watts).
        watts = [points[i][1] for i in range(0, len(points), 2)]

        assert watts[1:7] == [242, 138, 242, 138, 242, 138]
        assert points[-1][0] == 60.0  # 10 + 3 x 15 + 5 minutes; the old file came to 50

    def test_mrc_alternates_work_and_recovery(self):
        db = _make_test_db()
        workout = _make_workout_with_steps(db, _make_test_user(db))

        points = _course(workout_to_mrc(workout, ftp=250))
        pct = [points[i][1] for i in range(0, len(points), 2)]

        assert pct[1:7] == [97, 55, 97, 55, 97, 55]
        assert points[-1][0] == 60.0


class TestGPXExport:
    def test_gpx_with_gps_data(self):
        """GPX should include GPS trackpoints."""
        db = _make_test_db()
        user = _make_test_user(db)

        ride = Ride(
            user_id=user.id,
            source=RideSource.fit_upload,
            title="GPS Ride",
            ride_date=datetime.now(timezone.utc),
            duration_seconds=300,
        )
        db.add(ride)
        db.flush()

        # Add data with GPS coordinates
        for i in range(5):
            dp = RideData(
                ride_id=ride.id,
                elapsed_seconds=i * 60,
                latitude=51.5 + (i * 0.001),
                longitude=-0.1 + (i * 0.001),
                altitude=100 + i,
                power=200,
                heart_rate=145,
            )
            db.add(dp)
        db.commit()

        gpx = ride_to_gpx(db, ride.id, ride_title="GPS Ride")
        root = ET.fromstring(gpx)

        ns = {"gpx": "http://www.topografix.com/GPX/1/1"}
        trkpts = root.findall(".//gpx:trkpt", ns)
        assert len(trkpts) == 5

        # Check first trackpoint
        first = trkpts[0]
        assert float(first.get("lat")) > 51.0
        assert float(first.get("lon")) < 0.0

    def test_gpx_empty_ride(self):
        """GPX with no GPS data should produce empty but valid file."""
        db = _make_test_db()
        user = _make_test_user(db)

        ride = Ride(
            user_id=user.id,
            source=RideSource.in_app,
            title="Indoor Ride",
            ride_date=datetime.now(timezone.utc),
            duration_seconds=300,
        )
        db.add(ride)
        db.commit()

        gpx = ride_to_gpx(db, ride.id, ride_title="Indoor Ride")
        root = ET.fromstring(gpx)
        assert root.tag.endswith("gpx")
