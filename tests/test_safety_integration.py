"""The safety pieces working together: holds from different sources, the
"On hold" label following every way a hold opens or lifts, exports obeying
the gate, the data export carrying the safety records, the accept message
naming what the gate left out, and the waitlist keeping a country."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.training import _accept_message
from app.core.workout_templates import ENDURANCE_Z2_SHORT, VO2MAX_5x5
from app.models.base import Base
from app.models.training import Workout, WorkoutStatus
from app.models.user import User
from app.models.waitlist import WaitlistEntry
from app.schemas.user import UserResponse
from app.services import gdpr_service, plan_service, safety_screen
from app.services import onboarding_service as ob
from app.services import safety_service as ss
from app.services.plan_service import HOLD_PREFIX, _create_workout_steps

QUESTIONS = [f"q{i}" for i in range(1, 9)]


def _answers(*yes: str) -> dict[str, bool]:
    return {q: q in yes for q in QUESTIONS}


def _user(db, email="rider@example.com", **kw) -> User:
    kw.setdefault("ftp", 250)
    user = User(email=email, hashed_password="x", full_name="Sam Rider", **kw)
    db.add(user)
    db.commit()
    return user


def _workout(db, user, template=VO2MAX_5x5, days_ahead=2) -> Workout:
    w = Workout(
        user_id=user.id,
        scheduled_date=date.today() + timedelta(days=days_ahead),
        title=template["name"],
        description=template["description"],
        workout_type=template["workout_type"],
        planned_duration_seconds=template["duration_seconds"],
        planned_tss=70,
        status=WorkoutStatus.planned,
    )
    db.add(w)
    db.flush()
    _create_workout_steps(db, w, template)
    db.commit()
    db.refresh(w)
    return w


def _held(db, workout) -> bool:
    db.refresh(workout)
    return (workout.description or "").startswith(HOLD_PREFIX)


# === Holds from different sources ===


def test_a_fresh_all_no_screening_keeps_the_coachs_restriction(db_session):
    user = _user(db_session)
    coach = ss.open_hold(db_session, user, "easy_only", "Sore knee", "coach_tool", red_flag="injury")
    ob.submit_screening(db_session, user, _answers("q2"), long_break=False)
    assert ss.current_hold(db_session, user.id).level == "hold_all"
    result = ob.submit_screening(db_session, user, _answers(), long_break=False)
    db_session.refresh(coach)
    assert coach.lifted_at is None
    assert result["safety"]["allowed"] == "easy"
    assert result["safety"]["hold"]["id"] == coach.id


# === The "On hold" label follows the hold ===


@pytest.fixture
def api():
    """The app over a private SQLite database, signed in as one rider."""
    from app.api.v1 import waitlist
    from app.api.v1.deps import get_current_user
    from app.database import get_db
    from app.main import app

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = _user(db, "api@example.com")

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, user.id)
    app.dependency_overrides[waitlist._join_limit] = lambda: None
    try:
        yield TestClient(app), db, user
    finally:
        for dep in (get_db, get_current_user, waitlist._join_limit):
            app.dependency_overrides.pop(dep, None)
        db.close()


def test_clearance_takes_the_on_hold_label_off(api):
    client, db, user = api
    workout = _workout(db, user)
    ob.submit_screening(db, user, _answers("q3"), long_break=False)
    assert _held(db, workout)
    r = client.post("/api/v1/users/me/safety/clearance", json={"by": "My GP", "limits": None})
    assert r.status_code == 200 and r.json()["allowed"] == "all"
    assert not _held(db, workout)


def test_this_was_a_mistake_takes_the_on_hold_label_off(api):
    client, db, user = api
    workout = _workout(db, user)
    hold = ss.open_hold(db, user, "hold_all", "Chest pain in chat", "detector", red_flag="chest_pain")
    plan_service.sync_hold_marks(db, user.id)
    assert _held(db, workout)
    r = client.post("/api/v1/users/me/safety/mistake", json={"hold_id": hold.id})
    assert r.status_code == 200 and r.json()["hold"] is None
    assert not _held(db, workout)


def test_a_red_flag_in_chat_puts_the_on_hold_label_on(db_session):
    user = _user(db_session)
    workout = _workout(db_session, user)
    result = safety_screen.screen_message(db_session, user, "I had chest pain on the climb today")
    assert result.hits and ss.current_hold(db_session, user.id).level == "hold_all"
    assert _held(db_session, workout)


def test_the_coachs_hold_tool_puts_the_on_hold_label_on(db_session):
    from app.services import coach_service

    user = _user(db_session)
    workout = _workout(db_session, user)
    out = coach_service._execute_tool(
        db_session, user, "apply_safety_hold",
        {"level": "hold_all", "reason": "Fainted after the ride", "red_flag": "fainting"},
    )
    assert out.startswith("Hold applied now: hold_all.")
    assert _held(db_session, workout)


# === Exports obey the gate ===


def test_exports_refuse_a_session_a_full_hold_rules_out(api):
    client, db, user = api
    workout = _workout(db, user)
    ss.open_hold(db, user, "hold_all", "Fainted", "detector", red_flag="fainting")
    r = client.get(f"/api/v1/exports/workout/{workout.id}/zwo")
    assert r.status_code == 403
    assert r.json()["detail"].startswith("Riding is on hold until you tell me a doctor")


def test_export_refusals_are_worded_for_the_hold(api):
    """Re-verification new problem 9: a fever and an under-18 account each
    get their own words, and neither mentions a doctor."""
    client, db, user = api
    workout = _workout(db, user)
    fever = ss.open_hold(db, user, "hold_all", "Has a fever", "detector", red_flag="fever")
    r = client.get(f"/api/v1/exports/workout/{workout.id}/zwo")
    assert r.status_code == 403
    assert r.json()["detail"].startswith("Riding is on hold until your fever has been gone")
    assert "doctor" not in r.json()["detail"]
    ss.lift_hold(db, user, fever.id, "mistake")
    ss.open_hold(db, user, "hold_all", "Said they are 15", "detector", red_flag="minor")
    r = client.get(f"/api/v1/exports/workout/{workout.id}/zwo")
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "18 and over" in detail and "doctor" not in detail


def test_exports_refuse_a_hard_session_under_easy_but_allow_endurance(api):
    client, db, user = api
    hard = _workout(db, user, VO2MAX_5x5)
    easy = _workout(db, user, ENDURANCE_Z2_SHORT, days_ahead=3)
    ss.open_hold(db, user, "easy_only", "Knee", "coach_tool", red_flag="injury")
    for fmt in ("zwo", "erg", "mrc", "fit"):
        r = client.get(f"/api/v1/exports/workout/{hard.id}/{fmt}")
        assert r.status_code == 403, fmt
        assert "easy version" in r.json()["detail"]
    assert client.get(f"/api/v1/exports/workout/{easy.id}/zwo").status_code == 200


def test_exports_work_normally_with_no_hold(api):
    client, db, user = api
    workout = _workout(db, user)
    assert client.get(f"/api/v1/exports/workout/{workout.id}/erg").status_code == 200


# === The rider's data export ===


def test_data_export_carries_the_safety_records(db_session):
    user = _user(db_session)
    ob.submit_screening(db_session, user, _answers("q1"), long_break=False)
    ss.record_consent(db_session, user, "terms", "terms-x", "I agree.", source="register")
    archive = gdpr_service.export_user_data(db_session, user)
    for key in ("health_screenings", "consent_events", "safety_holds", "safety_events",
                "ride_session_starts"):
        assert key in archive
    assert len(archive["health_screenings"]) == 1
    assert {row["kind"] for row in archive["consent_events"]} == {"screening", "terms"}
    assert archive["safety_holds"][0]["source"] == "screening"


# === Accepting a proposal ===


def test_accept_message_names_what_the_gate_left_out():
    assert _accept_message(2, 0) == "2 sessions updated."
    assert _accept_message(0, 0) == "Nothing needed changing on the calendar."
    assert _accept_message(1, 1) == (
        "1 session updated. 1 change was left out, because hard sessions are on hold for now."
    )
    assert _accept_message(0, 2) == (
        "Nothing changed. 2 changes were left out, because hard sessions are on hold for now."
    )


# === Profile ===


def test_profile_carries_date_of_birth():
    born = date(1990, 5, 1)
    assert UserResponse(id="u", email="a@b.com", date_of_birth=born).date_of_birth == born


# === Waitlist ===


def test_waitlist_keeps_the_country_from_the_register_page(api):
    client, db, user = api
    r = client.post("/api/v1/waitlist", json={"email": "us@example.com", "name": "Sam", "country": "us"})
    assert r.status_code == 200
    assert db.query(WaitlistEntry).filter_by(email="us@example.com").one().country == "US"

    client.post("/api/v1/waitlist", json={"email": "later@example.com"})
    client.post("/api/v1/waitlist", json={"email": "later@example.com", "country": "UK"})
    assert db.query(WaitlistEntry).filter_by(email="later@example.com").one().country == "GB"

    client.post("/api/v1/waitlist", json={"email": "odd@example.com", "country": "elsewhere"})
    assert db.query(WaitlistEntry).filter_by(email="odd@example.com").one().country is None


# === An account held as under 18 can't pay ===


def test_checkout_and_portal_refuse_an_account_held_as_under_18(api, monkeypatch):
    """The billing API answers the hold in plain words (403), not with the
    generic "Couldn't open checkout" it gave before."""
    from app.api.v1 import billing
    from app.services import billing_service

    monkeypatch.setattr(billing_service, "is_configured", lambda: True)
    client, db, user = api
    ss.open_hold(db, user, "hold_all", "Said they are 15", "detector", red_flag="minor")
    for path in ("/api/v1/billing/checkout", "/api/v1/billing/portal"):
        r = client.post(path)
        assert r.status_code == 403, path
        assert r.json()["detail"] == billing.MINOR_BILLING_REFUSAL
    assert "18 and over" in billing.MINOR_BILLING_REFUSAL
    assert not any(ch in billing.MINOR_BILLING_REFUSAL for ch in "\u2013\u2014!")
