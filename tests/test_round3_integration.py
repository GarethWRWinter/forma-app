"""Reverify round 3: the pieces each fixer built, joined up.

- Problem 8: after Forma lifts an under-18 hold by hand, the detector's
  under-18 rule stays quiet on that account (safety_service.minor_quiet_until
  read by safety_screen.screen_message), so the same adult isn't held again.
- Problem 10: after a clearance, the health questions aren't asked again
  until the yearly re-screen (safety_service.rescreen_quiet_until read by
  onboarding_service.get_screening), so a truthful yes doesn't reopen a hold
  a doctor has just cleared. Round 4 moved this from 30 days to a year,
  counted from a clinician's clearance.

In-memory SQLite, no model, no email, no Stripe.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.models.safety import SafetyEvent
from app.models.user import User
from app.services import onboarding_service as ob
from app.services import plan_service, safety_screen
from app.services import safety_service as ss

QUESTIONS = [f"q{i}" for i in range(1, 9)]


def _user(db, email="rider@example.com", **kw) -> User:
    user = User(email=email, hashed_password="x", ftp=250, **kw)
    db.add(user)
    db.commit()
    return user


@pytest.fixture(autouse=True)
def _no_plan_marks(monkeypatch):
    monkeypatch.setattr(plan_service, "sync_hold_marks", lambda db, user_id, commit=True: None)


# === Problem 8: an adult Forma has lifted stays lifted ===


def _lifted_adult(db, **kw) -> User:
    user = _user(db, **kw)
    first = safety_screen.screen_message(db, user, "I'm 16 and I want to race")
    assert "minor" in first.kinds
    hold = ss.minor_hold(db, user)
    assert hold is not None
    ss.admin_lift(db, hold.id, "Emailed me: 46, not 16")
    return user


def test_the_detector_reads_no_age_into_a_lifted_adults_words(db_session):
    user = _lifted_adult(db_session, date_of_birth=date(1980, 1, 1))
    events = db_session.query(SafetyEvent).filter(SafetyEvent.kind == "minor").count()

    result = safety_screen.screen_message(db_session, user, "I'm 16 and I want to race again")
    assert result.kinds == set() and result.cards == [] and result.alerts == []
    assert result.context_line is None
    assert ss.minor_hold(db_session, user) is None
    assert db_session.query(SafetyEvent).filter(SafetyEvent.kind == "minor").count() == events

    # Anything else in the same message is still caught.
    result = safety_screen.screen_message(
        db_session, user, "I'm 16 and I had chest pain on the climb"
    )
    assert result.kinds == {"chest_pain"}
    assert ss.minor_hold(db_session, user) is None


def test_the_quiet_ends_after_180_days(db_session, monkeypatch):
    user = _lifted_adult(db_session)
    later = datetime.utcnow() + timedelta(days=ss.MINOR_QUIET_DAYS, minutes=1)
    monkeypatch.setattr(ss, "_now", lambda: later)
    assert ss.minor_quiet_until(db_session, user) is None
    result = safety_screen.screen_message(db_session, user, "I'm 16 and I want to race")
    assert "minor" in result.kinds and ss.minor_hold(db_session, user) is not None


def test_the_quiet_never_covers_a_child_on_the_record(db_session):
    today = datetime.utcnow().date()
    user = _lifted_adult(db_session, date_of_birth=date(today.year - 16, 1, 1))
    result = safety_screen.screen_message(db_session, user, "I'm 16 and I want to race")
    assert "minor" in result.kinds and ss.minor_hold(db_session, user) is not None


def test_a_failed_quiet_lookup_keeps_the_hit(db_session, monkeypatch):
    user = _lifted_adult(db_session)

    def broken(db, u):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(ss, "minor_quiet_until", broken)
    result = safety_screen.screen_message(db_session, user, "I'm 16 and I want to race")
    assert "minor" in result.kinds


def test_the_coach_gives_the_lifted_adult_an_ordinary_turn(db_session):
    from app.services import coach_service

    user = _lifted_adult(db_session)
    screen = safety_screen.screen_message(db_session, user, "I'm 16 and I want to race")
    assert coach_service._child_account(db_session, user, screen) is False


# === Problem 10: no re-screen straight after a clearance ===


def _answers(*yes: str) -> dict[str, bool]:
    return {q: q in yes for q in QUESTIONS}


def _answered(db, user, days_ago: int) -> None:
    ob.submit_screening(db, user, _answers(), long_break=False)
    row = ss.latest_screening(db, user.id)
    row.created_at = datetime.utcnow() - timedelta(days=days_ago)
    db.commit()


@pytest.mark.parametrize("how", ["doctor", "head", "fever"])
def test_no_rescreen_until_a_year_after_a_clearance(db_session, monkeypatch, how):
    """Round 3: a doctor cleared a chest-pain hold, the re-screen opened, and a
    truthful answer put a full hold back on until a second clearance. A
    fever or head-injury lift opened it too. Round 4: the cleared red flag
    never reopens the questions; only the yearly re-screen does."""
    user = _user(db_session)
    _answered(db_session, user, days_ago=20)
    if how == "doctor":
        ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
        ss.confirm_clearance(db_session, user, "My GP", None)
    elif how == "head":
        head = ss.open_hold(db_session, user, "hold_all", "Head injury", "detector",
                            red_flag="head_injury")
        ss.lift_head_injury(db_session, user, head.id, "My GP")
    else:
        fever = ss.open_hold(db_session, user, "hold_all", "Fever", "detector", red_flag="fever")
        ss.lift_fever(db_session, user, fever.id)
    assert ob.get_screening(db_session, user)["rescreen_due"] is False

    # Day 31, when round 3's 30-day quiet ran out: still not asked again.
    day_31 = datetime.utcnow() + timedelta(days=31)
    monkeypatch.setattr(ss, "_now", lambda: day_31)
    assert ob.get_screening(db_session, user)["rescreen_due"] is False

    # A year on, the yearly re-screen asks them again.
    later = datetime.utcnow() + timedelta(days=ss.RESCREEN_AFTER_DAYS, minutes=1)
    monkeypatch.setattr(ss, "_now", lambda: later)
    assert ob.get_screening(db_session, user)["rescreen_due"] is True


def test_new_questions_are_asked_even_straight_after_a_clearance(db_session, monkeypatch):
    user = _user(db_session)
    _answered(db_session, user, days_ago=20)
    ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
    ss.confirm_clearance(db_session, user, "My GP", None)
    assert ob.get_screening(db_session, user)["rescreen_due"] is False
    monkeypatch.setattr(ss, "SCREENING_VERSION", "screen-v2")
    assert ob.get_screening(db_session, user)["rescreen_due"] is True


def test_without_a_clearance_a_red_flag_still_asks_again(db_session):
    user = _user(db_session)
    _answered(db_session, user, days_ago=20)
    ss.open_hold(db_session, user, "hold_all", "Fainted on a climb", "detector",
                 red_flag="faint")
    assert ob.get_screening(db_session, user)["rescreen_due"] is True
