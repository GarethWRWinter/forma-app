"""Tests for workout and ride export services (ZWO, GPX)."""

import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.ride import Ride, RideData, RideSource
from app.models.training import Workout, WorkoutStep, WorkoutType, WorkoutStatus, StepType
from app.models.user import User
from app.services.export_service import ride_to_gpx, workout_to_zwo


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


# === Review finding 1: files ride the way ride mode does, never above the ERG cap ===
#
# The ERG and MRC builders used to loop each step's own repeat_count, so the
# VO2max 5x5 came out as 25 unbroken minutes at 112% and the Sprint as 60
# seconds held at 200% in ERG. These render the real templates and read the
# files back.

import struct

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.core.workout_templates import (
    ENDURANCE_Z2_SHORT,
    OVER_UNDER_3x12,
    SPRINT_NEUROMUSCULAR,
    VO2MAX_5x5,
    VO2MAX_MICRO_INTERVALS,
    WORKOUT_TEMPLATES,
)
from app.services import export_service
from app.services import safety_service as ss
from app.services.export_service import (
    MAX_EFFORT_COURSE_TEXT,
    MAX_EFFORT_WARNING,
    flatten_steps,
    workout_to_erg,
    workout_to_fit,
    workout_to_mrc,
)
from app.services.plan_service import _create_workout_steps

FTP = 250
CAP = ss.ERG_CAP  # 1.30
FIT_TARGET_OPEN = 2
FIT_TARGET_POWER = 4


def _from_template(db, user, template, days_ahead=2, title=None) -> Workout:
    """A session built the way the plan builds it, from a real template."""
    w = Workout(
        user_id=user.id,
        scheduled_date=date.today() + timedelta(days=days_ahead),
        title=title or template["name"],
        description=template["description"],
        workout_type=template["workout_type"],
        planned_duration_seconds=template["duration_seconds"],
        status=WorkoutStatus.planned,
    )
    db.add(w)
    db.flush()
    _create_workout_steps(db, w, template)
    db.commit()
    db.refresh(w)
    return w


def _ridden(template) -> list[tuple[int, float]]:
    """(seconds, fraction of FTP) for each step as ride mode rides it,
    worked out here from the template by hand, independently of the code."""
    out, steps, i = [], template["steps"], 0
    while i < len(steps):
        s = steps[i]
        if s["step_type"] == "interval_on":
            off = steps[i + 1] if i + 1 < len(steps) and steps[i + 1]["step_type"] == "interval_off" else None
            for _ in range(s.get("repeat_count") or 1):
                out.append((s["duration_seconds"], s["power_target_pct"]))
                if off:
                    out.append((off["duration_seconds"], off["power_target_pct"]))
            i += 2 if off else 1
        else:
            out.append((s["duration_seconds"], s.get("power_target_pct")))
            i += 1
    return out


def _course(text: str) -> tuple[list[tuple[float, float]], list[tuple[int, str]]]:
    """The [COURSE DATA] points and [COURSE TEXT] lines of an ERG or MRC file."""
    data = text.split("[COURSE DATA]")[1].split("[END COURSE DATA]")[0].strip().splitlines()
    labels = text.split("[COURSE TEXT]")[1].split("[END COURSE TEXT]")[0].strip().splitlines()
    points = [(float(m), float(v)) for m, v in (line.split("\t") for line in data)]
    texts = [(int(t), msg) for t, msg, _ in (line.split("\t") for line in labels)]
    return points, texts


def _segments(points) -> list[tuple[float, float, float]]:
    """(minutes long, start value, end value) for each pair of points."""
    return [(b[0] - a[0], a[1], b[1]) for a, b in zip(points[::2], points[1::2])]


def _longest_run_above(points, threshold: float) -> float:
    """The longest unbroken stretch, in minutes, held above `threshold`."""
    longest = run = 0.0
    for minutes, start, end in _segments(points):
        if min(start, end) > threshold:
            run += minutes
            longest = max(longest, run)
        else:
            run = 0.0
    return longest


def _fit_steps(blob: bytes) -> list[dict]:
    """Read back the workout_step messages of a FIT file this module wrote."""
    header_size = blob[0]
    data_size = struct.unpack("<I", blob[4:8])[0]
    body = blob[header_size:header_size + data_size]
    layouts, steps, pos = {}, [], 0
    while pos < len(body):
        head = body[pos]
        local = head & 0x0F
        if head & 0x40:
            global_mesg, n = struct.unpack("<HB", body[pos + 3:pos + 6])
            sizes = [body[pos + 6 + 3 * k + 1] for k in range(n)]
            layouts[local] = (global_mesg, sum(sizes))
            pos += 6 + 3 * n
            continue
        global_mesg, size = layouts[local]
        record = body[pos + 1:pos + 1 + size]
        pos += 1 + size
        if global_mesg == 27:
            name = record[:16].split(b"\x00")[0].decode("utf-8")
            dur_type, dur_ms, target_type, _, low, high, intensity, idx = struct.unpack(
                "<BIBIIIBH", record[16:]
            )
            steps.append({
                "name": name, "seconds": dur_ms // 1000, "target": target_type,
                "low": low, "high": high, "index": idx,
            })
    assert pos == len(body)
    return steps


@pytest.fixture
def rider():
    db = _make_test_db()
    return db, _make_test_user(db)


def test_flattening_interleaves_on_and_off_like_ride_mode(rider):
    db, user = rider
    for template in (VO2MAX_5x5, SPRINT_NEUROMUSCULAR, OVER_UNDER_3x12, VO2MAX_MICRO_INTERVALS):
        w = _from_template(db, user, template)
        flat = [(s.duration_seconds, s.power_target_pct) for s in flatten_steps(w)]
        assert flat == _ridden(template), template["name"]


def test_vo2max_5x5_erg_alternates_five_hard_minutes_with_five_easy(rider):
    db, user = rider
    w = _from_template(db, user, VO2MAX_5x5)
    points, texts = _course(workout_to_erg(w, ftp=FTP))
    segs = _segments(points)

    hard = round(1.12 * FTP)
    easy = round(0.50 * FTP)
    middle = [(round(m, 2), a) for m, a, b in segs[1:-1]]
    assert middle == [(5.0, hard), (5.0, easy)] * 5
    # Never more than one rep above FTP without a break: 5 minutes, not 25.
    assert _longest_run_above(points, FTP) == pytest.approx(5.0, abs=0.01)
    total = sum(sec for sec, _ in _ridden(VO2MAX_5x5))
    assert points[-1][0] == pytest.approx(total / 60, abs=0.01)
    # The course text follows the same order, at the right second.
    assert [t for t, _ in texts] == [0, 900, 1200, 1500, 1800, 2100, 2400, 2700, 3000, 3300, 3600, 3900]


def test_vo2max_5x5_mrc_alternates_the_same_way(rider):
    db, user = rider
    w = _from_template(db, user, VO2MAX_5x5)
    points, _ = _course(workout_to_mrc(w, ftp=FTP))
    middle = [(round(m, 2), a) for m, a, b in _segments(points)[1:-1]]
    assert middle == [(5.0, 112), (5.0, 50)] * 5
    assert _longest_run_above(points, 100) == pytest.approx(5.0, abs=0.01)


def test_vo2max_5x5_fit_and_zwo_alternate_too(rider):
    db, user = rider
    w = _from_template(db, user, VO2MAX_5x5)
    fit = _fit_steps(workout_to_fit(w, ftp=FTP))
    assert [s["seconds"] for s in fit] == [sec for sec, _ in _ridden(VO2MAX_5x5)]
    assert [s["index"] for s in fit] == list(range(len(fit)))
    zwo = ET.fromstring(workout_to_zwo(w, ftp=FTP)).find("workout")
    intervals = zwo.find("IntervalsT")
    assert (intervals.get("Repeat"), intervals.get("OnPower"), intervals.get("OffPower")) == ("5", "1.12", "0.50")


def test_sprint_erg_holds_the_cap_never_200_percent_and_says_erg_off(rider):
    db, user = rider
    w = _from_template(db, user, SPRINT_NEUROMUSCULAR)
    points, texts = _course(workout_to_erg(w, ftp=FTP))

    assert max(v for _, v in points) == round(CAP * FTP)  # 325 W, not 500
    sprints = [(round(m * 60), a) for m, a, b in _segments(points) if a == round(CAP * FTP)]
    assert sprints == [(10, round(CAP * FTP))] * 6
    # Each sprint is followed by its recovery, not by the next sprint.
    segs = _segments(points)
    for k, (minutes, start, _) in enumerate(segs):
        if start == round(CAP * FTP):
            assert round(segs[k + 1][0] * 60) == 290 and segs[k + 1][1] == round(0.45 * FTP)
    # Marked: a heads-up before each sprint, then the max-effort line on it.
    efforts = [t for t, msg in texts if msg == MAX_EFFORT_COURSE_TEXT]
    warnings = [t for t, msg in texts if msg == MAX_EFFORT_WARNING]
    assert efforts == [1200, 1500, 1800, 2100, 2400, 2700]
    assert warnings == [t - 30 for t in efforts]
    assert [t for t, _ in texts] == sorted(t for t, _ in texts)


def test_sprint_mrc_is_capped_at_130_percent(rider):
    db, user = rider
    w = _from_template(db, user, SPRINT_NEUROMUSCULAR)
    points, texts = _course(workout_to_mrc(w, ftp=FTP))
    assert max(v for _, v in points) == 130
    assert sum(msg == MAX_EFFORT_COURSE_TEXT for _, msg in texts) == 6


def test_sprint_zwo_is_free_ride_blocks_with_no_erg_target(rider):
    db, user = rider
    w = _from_template(db, user, SPRINT_NEUROMUSCULAR)
    wo = ET.fromstring(workout_to_zwo(w, ftp=FTP)).find("workout")
    tags = [e.tag for e in wo]
    assert tags == ["Warmup", "SteadyState"] + ["FreeRide", "SteadyState"] * 6 + ["Cooldown"]
    assert wo.find("IntervalsT") is None
    for e in wo.iter():
        for attr in ("Power", "OnPower", "OffPower", "PowerLow", "PowerHigh"):
            if e.get(attr) is not None:
                assert float(e.get(attr)) <= CAP, (e.tag, attr, e.get(attr))
    sprints = [e for e in wo if e.tag == "FreeRide"]
    assert all(e.get("Duration") == "10" for e in sprints)
    assert all(e.find("textevent").get("message") == export_service.MAX_EFFORT_ZWO for e in sprints)


def test_sprint_fit_steps_have_an_open_target(rider):
    db, user = rider
    w = _from_template(db, user, SPRINT_NEUROMUSCULAR)
    fit = _fit_steps(workout_to_fit(w, ftp=FTP))
    sprints = [s for s in fit if s["seconds"] == 10]
    assert len(sprints) == 6
    assert all((s["target"], s["low"], s["high"]) == (FIT_TARGET_OPEN, 0, 0) for s in sprints)
    assert all(s["name"] == export_service.MAX_EFFORT_FIT_NAME for s in sprints)
    powered = [s for s in fit if s["target"] == FIT_TARGET_POWER]
    assert max(s["high"] - 1000 for s in powered) <= round(CAP * FTP) + 5


@pytest.mark.parametrize(
    "template", [t for ts in WORKOUT_TEMPLATES.values() for t in ts], ids=lambda t: t["name"]
)
def test_no_template_in_any_format_asks_erg_for_more_than_the_cap(rider, template):
    db, user = rider
    w = _from_template(db, user, template)
    points, _ = _course(workout_to_erg(w, ftp=FTP))
    assert max(v for _, v in points) <= round(CAP * FTP)
    points, _ = _course(workout_to_mrc(w, ftp=FTP))
    assert max(v for _, v in points) <= CAP * 100
    for s in _fit_steps(workout_to_fit(w, ftp=FTP)):
        if s["target"] == FIT_TARGET_POWER:
            assert s["high"] - 1000 <= round(CAP * FTP) + 5
    wo = ET.fromstring(workout_to_zwo(w, ftp=FTP)).find("workout")
    for e in wo.iter():
        for attr in ("Power", "OnPower", "OffPower", "PowerLow", "PowerHigh"):
            if e.get(attr) is not None:
                assert float(e.get(attr)) <= CAP + 1e-9


def test_exactly_the_cap_stays_in_erg(rider):
    """VO2max micro intervals sit at 130%: held in ERG, not released."""
    db, user = rider
    w = _from_template(db, user, VO2MAX_MICRO_INTERVALS)
    wo = ET.fromstring(workout_to_zwo(w, ftp=FTP)).find("workout")
    assert wo.find("FreeRide") is None
    assert wo.find("IntervalsT").get("OnPower") == "1.30"
    assert all(s["target"] == FIT_TARGET_POWER for s in _fit_steps(workout_to_fit(w, ftp=FTP)))


def _custom(db, user, steps, workout_type=WorkoutType.sprint) -> Workout:
    w = Workout(user_id=user.id, scheduled_date=date(2026, 10, 10), title="Custom",
                workout_type=workout_type, status=WorkoutStatus.planned)
    db.add(w)
    db.flush()
    for k, s in enumerate(steps):
        db.add(WorkoutStep(workout_id=w.id, step_order=k, **s))
    db.commit()
    db.refresh(w)
    return w


def test_a_lone_interval_repeats_and_a_ramp_over_the_cap_is_held_at_it(rider):
    db, user = rider
    w = _custom(db, user, [
        dict(step_type=StepType.ramp, duration_seconds=60, power_low_pct=1.10, power_high_pct=1.60),
        dict(step_type=StepType.interval_on, duration_seconds=20, power_target_pct=1.50, repeat_count=3),
        dict(step_type=StepType.steady_state, duration_seconds=120, power_target_pct=0.50),
    ])
    assert [s.duration_seconds for s in flatten_steps(w)] == [60, 20, 20, 20, 120]
    points, texts = _course(workout_to_erg(w, ftp=FTP))
    assert _segments(points)[0][1:] == (round(1.10 * FTP), round(CAP * FTP))
    assert sum(msg == MAX_EFFORT_COURSE_TEXT for _, msg in texts) == 3
    wo = ET.fromstring(workout_to_zwo(w, ftp=FTP)).find("workout")
    assert [e.tag for e in wo] == ["Warmup", "FreeRide", "FreeRide", "FreeRide", "SteadyState"]
    assert wo.find("Warmup").get("PowerHigh") == "1.30"


def test_fit_names_with_accents_cannot_overflow_the_field(rider):
    db, user = rider
    w = _custom(db, user, [
        dict(step_type=StepType.steady_state, duration_seconds=600, power_target_pct=0.6,
             notes="Échauffement très très long"),
    ], workout_type=WorkoutType.recovery)
    w.title = "Séance de récupération"
    fit = _fit_steps(workout_to_fit(w, ftp=FTP))
    assert fit[0]["name"].startswith("Échauffement")


# === The endpoint: the gate, then the file ===


@pytest.fixture
def api(monkeypatch):
    from app.api.v1.deps import get_current_user
    from app.database import get_db
    from app.main import app

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = _make_test_user(db)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, user.id)
    try:
        yield TestClient(app), db, user
    finally:
        for dep in (get_db, get_current_user):
            app.dependency_overrides.pop(dep, None)
        db.close()


FORMATS = ("zwo", "erg", "mrc", "fit")


def test_with_nothing_held_the_sprint_downloads_capped_in_every_format(api):
    client, db, user = api
    w = _from_template(db, user, SPRINT_NEUROMUSCULAR)
    for fmt in FORMATS:
        r = client.get(f"/api/v1/exports/workout/{w.id}/{fmt}")
        assert r.status_code == 200, (fmt, r.text)
        assert r.headers["content-disposition"] == f'attachment; filename="Sprint_Power.{fmt}"'
    points, _ = _course(client.get(f"/api/v1/exports/workout/{w.id}/erg").text)
    assert max(v for _, v in points) == round(CAP * FTP)


@pytest.mark.parametrize("template", [VO2MAX_5x5, SPRINT_NEUROMUSCULAR], ids=lambda t: t["name"])
def test_easy_only_refuses_vo2max_and_sprint_in_every_format(api, template):
    client, db, user = api
    w = _from_template(db, user, template)
    easy = _from_template(db, user, ENDURANCE_Z2_SHORT, days_ahead=3)
    ss.open_hold(db, user, "easy_only", "Sore knee", "coach_tool", red_flag="injury")
    for fmt in FORMATS:
        r = client.get(f"/api/v1/exports/workout/{w.id}/{fmt}")
        assert r.status_code == 403, fmt
        assert "easy version" in r.json()["detail"]
        assert client.get(f"/api/v1/exports/workout/{easy.id}/{fmt}").status_code == 200, fmt


@pytest.mark.parametrize("template", [VO2MAX_5x5, SPRINT_NEUROMUSCULAR, ENDURANCE_Z2_SHORT],
                         ids=lambda t: t["name"])
def test_a_full_hold_refuses_everything_in_every_format(api, template):
    client, db, user = api
    w = _from_template(db, user, template)
    ss.open_hold(db, user, "hold_all", "Fainted", "detector", red_flag="fainting")
    for fmt in FORMATS:
        r = client.get(f"/api/v1/exports/workout/{w.id}/{fmt}")
        assert r.status_code == 403, fmt
        assert r.json()["detail"].startswith("Riding is on hold until you tell me a doctor")


def test_a_title_with_accents_and_quotes_still_downloads(api):
    client, db, user = api
    w = _from_template(db, user, ENDURANCE_Z2_SHORT, title='Café "ride" 🚴')
    r = client.get(f"/api/v1/exports/workout/{w.id}/zwo")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="Caf_ride.zwo"'
