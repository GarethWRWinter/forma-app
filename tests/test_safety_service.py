"""The safety gate: holds, screening, the layoff rule, consent records, the
type ceilings ride mode enforces, and the endpoints the app calls."""

import asyncio
from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.workout_templates import WORKOUT_TEMPLATES
from app.models.base import Base
from app.models.ride import Ride, RideSource
from app.models.safety import (
    SAFETY_RETAINED_TABLES,
    ClearanceLimit,
    ConsentEvent,
    HealthScreening,
    RideSessionStart,
    SafetyEvent,
    SafetyHold,
)
from app.models.training import WorkoutType
from app.models.user import User
from app.services import email_service, safety_service as ss

TODAY = datetime.utcnow().date()


def _user(db, email="rider@example.com", **kw) -> User:
    user = User(email=email, hashed_password="x", **kw)
    db.add(user)
    db.commit()
    return user


def _ride(db, user, days_ago: int) -> None:
    db.add(Ride(
        user_id=user.id, source=RideSource.manual,
        ride_date=datetime.combine(TODAY - timedelta(days=days_ago), time(9, 0)),
    ))
    db.commit()


def _screening(db, user, tier="none", long_break=False, days_ago=0, **kw) -> HealthScreening:
    s = HealthScreening(
        user_id=user.id, version=ss.SCREENING_VERSION, answers={"q1": tier != "none"},
        long_break=long_break, any_yes=tier != "none", tier=tier,
        created_at=datetime.combine(TODAY - timedelta(days=days_ago), time(8, 0)), **kw,
    )
    db.add(s)
    db.commit()
    return s


class _Req:
    def __init__(self, headers: dict, host: str = "10.0.0.1"):
        self.headers = headers
        self.client = type("C", (), {"host": host})()


# === Holds ===


def test_hold_all_beats_easy_only(db_session):
    user = _user(db_session)
    ss.open_hold(db_session, user, "easy_only", "Heart condition on screening", "screening")
    assert ss.current_hold(db_session, user.id).level == "easy_only"
    ss.open_hold(db_session, user, "hold_all", "Chest pain in chat", "detector", red_flag="chest_pain")
    assert ss.current_hold(db_session, user.id).level == "hold_all"


def test_open_hold_is_idempotent_for_the_same_concern(db_session):
    user = _user(db_session)
    first = ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
    again = ss.open_hold(db_session, user, "hold_all", "Chest pain again", "detector", red_flag="chest_pain")
    lower = ss.open_hold(db_session, user, "easy_only", "Chest, milder", "detector", red_flag="chest_pain")
    assert again.id == first.id and lower.id == first.id
    assert db_session.query(SafetyHold).count() == 1


def test_a_different_concern_opens_its_own_hold_under_a_higher_one(db_session):
    # Finding 9: "hit my head and my wrist is swollen". The wrist's easy_only
    # hold used to be dropped because the head's hold_all was already open,
    # so marking the head a mistake left the rider on "all".
    user = _user(db_session)
    head = ss.open_hold(db_session, user, "hold_all", "Head", "detector", red_flag="head_injury")
    wrist = ss.open_hold(db_session, user, "easy_only", "Wrist", "detector", red_flag="injury")
    assert wrist.id != head.id
    assert {h.id for h in ss.open_holds(db_session, user)} == {head.id, wrist.id}
    assert ss.current_hold(db_session, user.id).id == head.id
    ss.lift_hold(db_session, user, head.id, "mistake")
    assert ss.current_hold(db_session, user.id).id == wrist.id
    assert ss.allowed_intensity(db_session, user) == "easy"


def test_the_same_red_flag_from_another_source_is_its_own_hold(db_session):
    user = _user(db_session)
    chat = ss.open_hold(db_session, user, "easy_only", "Knee", "detector", red_flag="injury")
    tool = ss.open_hold(db_session, user, "easy_only", "Knee", "coach_tool", red_flag="injury")
    assert chat.id != tool.id
    assert len(ss.open_holds(db_session, user)) == 2


def test_a_higher_hold_supersedes_a_lower_one_for_the_same_concern(db_session):
    user = _user(db_session)
    low = ss.open_hold(db_session, user, "easy_only", "Knee", "coach_tool", red_flag="injury")
    high = ss.open_hold(db_session, user, "hold_all", "Knee, worse", "coach_tool", red_flag="injury")
    db_session.refresh(low)
    assert low.lifted_how == "superseded" and low.lifted_at is not None
    assert high.lifted_at is None
    assert [h.id for h in ss._open_holds(db_session, user.id)] == [high.id]


def test_a_higher_hold_leaves_other_concerns_open_underneath(db_session):
    # The coach's knee restriction must survive a fainting hold being lifted
    # as a mistake (or a fresh all-no screening lifting a screening hold).
    user = _user(db_session)
    low = ss.open_hold(db_session, user, "easy_only", "Knee", "coach_tool", red_flag="injury")
    high = ss.open_hold(db_session, user, "hold_all", "Fainted", "detector", red_flag="fainting")
    db_session.refresh(low)
    assert low.lifted_at is None
    assert ss.current_hold(db_session, user.id).id == high.id
    ss.lift_hold(db_session, user, high.id, "mistake")
    assert ss.current_hold(db_session, user.id).id == low.id
    assert ss.allowed_intensity(db_session, user) == "easy"


def test_open_hold_rejects_unknown_values(db_session):
    user = _user(db_session)
    with pytest.raises(ValueError):
        ss.open_hold(db_session, user, "maybe", "x", "detector")
    with pytest.raises(ValueError):
        ss.open_hold(db_session, user, "hold_all", "x", "the_model")


def test_lift_holds_counts_and_clears(db_session):
    user = _user(db_session)
    ss.open_hold(db_session, user, "hold_all", "Fever", "detector", red_flag="fever")
    assert ss.lift_holds(db_session, user, "admin", note="Checked") == 1
    assert ss.current_hold(db_session, user.id) is None
    assert ss.lift_holds(db_session, user, "admin") == 0


def test_lift_hold_only_touches_the_riders_own_hold(db_session):
    a = _user(db_session, "a@example.com")
    b = _user(db_session, "b@example.com")
    hold = ss.open_hold(db_session, a, "hold_all", "Chest pain", "detector")
    assert ss.lift_hold(db_session, b, hold.id, "mistake") is None
    assert ss.current_hold(db_session, a.id) is not None
    assert ss.lift_hold(db_session, a, hold.id, "mistake").lifted_how == "mistake"


# === Screening ===


def test_latest_screening_ignores_superseded(db_session):
    user = _user(db_session)
    _screening(db_session, user, tier="easy_only", days_ago=1)
    _screening(db_session, user, tier="hold_all", superseded_at=datetime.utcnow())
    assert ss.latest_screening(db_session, user.id).tier == "easy_only"


# === Layoff gate ===


def test_no_rides_and_no_long_break_is_not_gated(db_session):
    user = _user(db_session)
    assert ss.layoff_gate_until(db_session, user, today=TODAY) is None


def test_long_break_answer_gates_fourteen_days_from_the_answer(db_session):
    user = _user(db_session)
    _screening(db_session, user, long_break=True, days_ago=3)
    assert ss.layoff_gate_until(db_session, user, today=TODAY) == TODAY + timedelta(days=11)


def test_long_break_answer_expires_after_fourteen_days(db_session):
    user = _user(db_session)
    _screening(db_session, user, long_break=True, days_ago=14)
    assert ss.layoff_gate_until(db_session, user, today=TODAY) is None


def test_regular_rider_is_not_gated(db_session):
    user = _user(db_session)
    for d in (40, 33, 26, 19, 12, 5, 1):
        _ride(db_session, user, d)
    assert ss.layoff_gate_until(db_session, user, today=TODAY) is None


def test_no_ride_in_28_days_gates_fourteen_days_ahead(db_session):
    user = _user(db_session)
    _ride(db_session, user, 30)
    assert ss.layoff_gate_until(db_session, user, today=TODAY) == TODAY + timedelta(days=14)


def test_no_ride_in_90_days_gates_28_days_ahead(db_session):
    user = _user(db_session)
    _ride(db_session, user, 120)
    assert ss.layoff_gate_until(db_session, user, today=TODAY) == TODAY + timedelta(days=28)


def test_return_after_a_gap_gates_from_the_first_ride_back(db_session):
    """Riding again after the break doesn't end the gate: it runs 14 days from
    the first ride back, however many rides follow."""
    user = _user(db_session)
    _ride(db_session, user, 60)
    for d in (10, 7, 3, 1):
        _ride(db_session, user, d)
    assert ss.layoff_gate_until(db_session, user, today=TODAY) == TODAY + timedelta(days=4)


def test_return_after_three_months_gates_28_days(db_session):
    user = _user(db_session)
    _ride(db_session, user, 150)
    _ride(db_session, user, 20)
    _ride(db_session, user, 2)
    assert ss.layoff_gate_until(db_session, user, today=TODAY) == TODAY + timedelta(days=8)


def test_return_gate_expires(db_session):
    user = _user(db_session)
    _ride(db_session, user, 60)
    _ride(db_session, user, 20)
    _ride(db_session, user, 1)
    assert ss.layoff_gate_until(db_session, user, today=TODAY) is None


def test_the_later_of_screening_and_ride_gates_wins(db_session):
    user = _user(db_session)
    _screening(db_session, user, long_break=True, days_ago=2)
    _ride(db_session, user, 100)
    assert ss.layoff_gate_until(db_session, user, today=TODAY) == TODAY + timedelta(days=28)


# === allowed_intensity ===


def test_allowed_is_all_by_default(db_session):
    user = _user(db_session)
    assert ss.allowed_intensity(db_session, user) == "all"


def test_hold_levels_map_to_allowed(db_session):
    user = _user(db_session)
    ss.open_hold(db_session, user, "easy_only", "Knee", "coach_tool")
    assert ss.allowed_intensity(db_session, user) == "easy"
    ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector")
    assert ss.allowed_intensity(db_session, user) == "none"


def test_uncleared_screening_tier_limits_even_without_a_hold(db_session):
    user = _user(db_session)
    s = _screening(db_session, user, tier="easy_only")
    assert ss.allowed_intensity(db_session, user) == "easy"
    s.clearance_confirmed_at = datetime.utcnow()
    db_session.commit()
    assert ss.allowed_intensity(db_session, user) == "all"


def test_hold_all_tier_means_none(db_session):
    user = _user(db_session)
    _screening(db_session, user, tier="hold_all")
    assert ss.allowed_intensity(db_session, user) == "none"


def test_layoff_gate_is_easy_until_the_gate_date(db_session):
    user = _user(db_session)
    _ride(db_session, user, 35)
    assert ss.allowed_intensity(db_session, user) == "easy"
    assert ss.allowed_intensity(db_session, user, on_date=TODAY + timedelta(days=13)) == "easy"
    assert ss.allowed_intensity(db_session, user, on_date=TODAY + timedelta(days=14)) == "all"


# === Clearance and consent ===


def test_clearance_stamps_screening_lifts_holds_and_records_consent(db_session):
    user = _user(db_session)
    _screening(db_session, user, tier="hold_all")
    ss.open_hold(db_session, user, "hold_all", "Chest pain on screening", "screening")
    ss.confirm_clearance(db_session, user, "My GP, Dr Shah", "No max efforts till June")

    s = ss.latest_screening(db_session, user.id)
    assert s.clearance_confirmed_at is not None
    assert s.clearance_by == "My GP, Dr Shah"
    assert s.clearance_limits == "No max efforts till June"
    assert ss.current_hold(db_session, user.id) is None
    lifted = db_session.query(SafetyHold).one()
    assert lifted.lifted_how == "clearance" and "No max efforts" in lifted.note
    event = db_session.query(ConsentEvent).one()
    assert event.kind == "clearance" and event.text_shown == ss.CLEARANCE_TEXT
    assert event.doc_version == ss.SCREENING_VERSION
    assert ss.allowed_intensity(db_session, user) == "all"


def test_clearance_needs_a_name(db_session):
    user = _user(db_session)
    with pytest.raises(ValueError):
        ss.confirm_clearance(db_session, user, "  ", None)


def test_consent_reads_the_address_the_way_the_rate_limiter_does(db_session, monkeypatch):
    # R16: one rule for which forwarded hop is the rider, kept in
    # app.core.ratelimit. A consent row must never record a different hop
    # from the one the limiter keys on.
    from app.core import ratelimit

    monkeypatch.setattr(ss.settings, "app_build", "abc123")
    user = _user(db_session)
    shapes = [
        {"x-forwarded-for": "6.6.6.6, 81.2.69.160", "user-agent": "Mozilla/5.0"},
        {"x-forwarded-for": "81.2.69.160, 76.76.21.21", "x-vercel-id": "lhr1::abc"},
        {},
    ]
    for headers in shapes:
        req = _Req(headers)
        ev = ss.record_consent(
            db_session, user.id, "terms", "terms-2026-10-draft", "I agree.",
            request=req, source="register",
        )
        assert ev.ip == ratelimit.client_ip(req), headers
    assert ev.ip == "10.0.0.1"
    first = db_session.query(ConsentEvent).order_by(ConsentEvent.accepted_at).first()
    assert first.user_agent == "Mozilla/5.0"
    assert first.app_build == "abc123" and first.source == "register"

    # And it really is the limiter's function, not a copy of its rule.
    monkeypatch.setattr(ratelimit, "client_ip", lambda request: "  203.0.113.9  ")
    ev = ss.record_consent(db_session, user.id, "terms", "v", "I agree.", request=_Req({}))
    assert ev.ip == "203.0.113.9"
    monkeypatch.setattr(ratelimit, "client_ip", lambda request: None)
    assert ss.record_consent(db_session, user.id, "terms", "v", "I agree.", request=_Req({})).ip is None


def test_consent_rejects_unknown_kind_and_empty_text(db_session):
    user = _user(db_session)
    with pytest.raises(ValueError):
        ss.record_consent(db_session, user.id, "marketing", "v", "x")
    with pytest.raises(ValueError):
        ss.record_consent(db_session, user.id, "terms", "v", "   ")


def test_consent_rows_are_append_only(db_session):
    user = _user(db_session)
    ev = ss.record_consent(db_session, user.id, "terms", "v1", "I agree.")
    ev.text_shown = "Something else"
    with pytest.raises(ValueError):
        db_session.commit()
    db_session.rollback()
    ev.subject_deleted_at = datetime.utcnow()
    db_session.commit()
    db_session.delete(ev)
    with pytest.raises(ValueError):
        db_session.commit()
    db_session.rollback()


# === Type ceilings ===


def test_no_template_exceeds_its_own_ceiling():
    for wtype, templates in WORKOUT_TEMPLATES.items():
        for tpl in templates:
            assert not ss.workout_exceeds_ceiling(tpl), tpl["name"]


def test_mislabelled_workout_exceeds_ceiling():
    vo2_as_recovery = {"workout_type": "recovery", "steps": [
        {"power_target_pct": 0.5}, {"power_target_pct": 1.2},
    ]}
    assert ss.workout_exceeds_ceiling(vo2_as_recovery)


def test_ceiling_reads_power_high_and_enum_types():
    class Step:
        def __init__(self, target, high=None):
            self.power_target_pct, self.power_high_pct = target, high

    class W:
        workout_type = WorkoutType.endurance
        steps = [Step(0.65, 0.85)]

    assert ss.workout_exceeds_ceiling(W())
    W.steps = [Step(0.65, 0.75)]
    assert not ss.workout_exceeds_ceiling(W())


def test_rest_and_unknown_types_fail_closed():
    assert not ss.workout_exceeds_ceiling({"workout_type": "rest", "steps": []})
    assert ss.workout_exceeds_ceiling({"workout_type": "mystery", "steps": [{"power_target_pct": 0.9}]})


def test_workout_allowed_by_level():
    endurance = WORKOUT_TEMPLATES["endurance"][0]
    threshold = WORKOUT_TEMPLATES["threshold"][0]
    rest = {"workout_type": "rest", "steps": []}
    assert ss.workout_allowed(threshold, "all")
    assert ss.workout_allowed(endurance, "easy")
    assert not ss.workout_allowed(threshold, "easy")
    assert not ss.workout_allowed(endurance, "none")
    assert ss.workout_allowed(rest, "none")
    hot_endurance = {"workout_type": "endurance", "steps": [{"power_target_pct": 0.78}]}
    assert ss.workout_allowed(hot_endurance, "all")
    assert not ss.workout_allowed(hot_endurance, "easy")


# === safety_state ===


def test_safety_state_shape(db_session):
    user = _user(db_session, ftp=250)
    _screening(db_session, user, tier="easy_only")
    hold = ss.open_hold(db_session, user, "easy_only", "Heart condition", "screening", red_flag="screen_q1")
    _ride(db_session, user, 40)

    state = ss.safety_state(db_session, user)

    assert state["allowed"] == "easy"
    assert state["hold"] == {
        "id": hold.id, "level": "easy_only", "reason": "Heart condition",
        "red_flag": "screen_q1", "source": "screening", "opened_at": hold.opened_at.isoformat(),
        "expires_at": None, "lift_kind": "doctor",
    }
    assert state["limits"] == []
    assert state["screening"] == {
        "tier": "easy_only", "version": ss.SCREENING_VERSION,
        "clearance_confirmed": False, "limits": None,
    }
    assert state["layoff_gate_until"] == (TODAY + timedelta(days=14)).isoformat()
    assert state["ride_mode_ack"] is False
    assert state["ftp"] == 250
    assert state["erg_cap"] == 1.30
    assert state["ceilings"]["vo2max"] == 1.30


# === Under-18 holds (finding 4) ===


@pytest.mark.parametrize("source", ["detector", "coach_tool"])
def test_an_under_18_hold_lifts_only_by_admin(db_session, source):
    user = _user(db_session)
    minor = ss.open_hold(db_session, user, "hold_all", "Under 18", source, red_flag="minor")

    with pytest.raises(ss.HoldNotLiftable):
        ss.lift_hold(db_session, user, minor.id, "mistake")
    with pytest.raises(ss.HoldNotLiftable):
        ss.lift_hold(db_session, user, minor.id, "clearance")
    with pytest.raises(ss.HoldNotLiftable):
        ss.lift_hold(db_session, user, minor.id, "superseded")
    assert ss.lift_holds(db_session, user, "mistake") == 0
    assert ss.lift_holds(db_session, user, "clearance") == 0
    with pytest.raises(ss.HoldNotLiftable):
        ss.confirm_clearance(db_session, user, "My GP", None)
    db_session.rollback()

    db_session.refresh(minor)
    assert minor.lifted_at is None
    assert ss.allowed_intensity(db_session, user) == "none"
    assert db_session.query(ConsentEvent).count() == 0

    assert ss.lift_holds(db_session, user, "admin", note="Account closed and refunded") == 1
    assert ss.current_hold(db_session, user.id) is None


def test_clearance_while_held_as_under_18_lifts_nothing_else_either(db_session):
    user = _user(db_session)
    _screening(db_session, user, tier="easy_only")
    ss.open_hold(db_session, user, "hold_all", "Under 18", "detector", red_flag="minor")
    chest = ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
    with pytest.raises(ss.HoldNotLiftable):
        ss.confirm_clearance(db_session, user, "My GP", "No sprints")
    db_session.rollback()
    db_session.refresh(chest)
    assert chest.lifted_at is None
    assert ss.latest_screening(db_session, user.id).clearance_confirmed_at is None
    assert ss.active_limits(db_session, user) == []


def test_safety_state_shows_the_under_18_hold_over_a_newer_one(db_session):
    user = _user(db_session)
    minor = ss.open_hold(db_session, user, "hold_all", "Under 18", "detector", red_flag="minor")
    ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
    state = ss.safety_state(db_session, user)
    assert state["hold"]["id"] == minor.id
    assert state["hold"]["lift_kind"] == "admin_only"
    assert state["allowed"] == "none"


def test_lift_kinds():
    def kind(**kw):
        return ss.lift_kind({"red_flag": None, "source": "detector", "expires_at": None, **kw})

    assert kind(red_flag="minor") == "admin_only"
    assert kind(red_flag="minor", source="coach_tool") == "admin_only"
    assert kind(source="admin") == "admin_only"
    assert kind(red_flag="fever") == "fever_self"
    assert kind(red_flag="fever", expires_at=datetime.utcnow()) == "expires"
    assert kind(red_flag="layoff", source="layoff") == "layoff"
    assert kind(red_flag="chest_pain") == "doctor"
    assert kind(red_flag="head_injury") == "head_injury"
    assert kind(red_flag="head_injury", source="coach_tool") == "head_injury"
    # The easy riding that follows a head injury ends by itself.
    assert kind(red_flag="head_injury", expires_at=datetime.utcnow()) == "expires"
    assert kind(red_flag="screen_q1", source="screening") == "doctor"
    assert kind() == "doctor"


# === The doctor's limits (finding 10) ===


def _rescreen(db, user, tier="none"):
    """What onboarding_service.save_screening does: supersede, then add."""
    for old in db.query(HealthScreening).filter_by(user_id=user.id, superseded_at=None):
        old.superseded_at = datetime.utcnow()
    db.commit()
    return _screening(db, user, tier=tier)


def test_limits_survive_a_rescreen_and_a_second_clearance(db_session):
    user = _user(db_session)
    _screening(db_session, user, tier="easy_only")
    ss.open_hold(db_session, user, "easy_only", "Heart condition", "screening", red_flag="screen_q1")
    ss.confirm_clearance(db_session, user, "My GP", "No efforts above threshold for six weeks")
    assert [l.limits for l in ss.active_limits(db_session, user)] == [
        "No efforts above threshold for six weeks"
    ]

    _rescreen(db_session, user, tier="easy_only")
    assert ss.latest_screening(db_session, user.id).clearance_limits is None
    assert len(ss.active_limits(db_session, user)) == 1

    ss.open_hold(db_session, user, "easy_only", "Knee", "detector", red_flag="injury")
    ss.confirm_clearance(db_session, user, "My physio", None)
    assert [l.limits for l in ss.active_limits(db_session, user)] == [
        "No efforts above threshold for six weeks"
    ]

    # A physio clears only an injury, so the second clearance is for a new one.
    ss.open_hold(db_session, user, "easy_only", "Calf", "detector", red_flag="injury")
    ss.confirm_clearance(db_session, user, "My physio", "No standing climbs")
    limits = ss.active_limits(db_session, user)
    assert [l.limits for l in limits] == [
        "No efforts above threshold for six weeks", "No standing climbs",
    ]
    assert limits[1].cleared_by == "My physio"
    consent = db_session.get(ConsentEvent, limits[1].consent_event_id)
    assert consent.kind == "clearance" and consent.text_shown == ss.CLEARANCE_TEXT
    state = ss.safety_state(db_session, user)
    assert [l["text"] for l in state["limits"]] == [
        "No efforts above threshold for six weeks", "No standing climbs",
    ]
    lines = ss.limit_lines(db_session, user)
    assert lines[0].startswith("No efforts above threshold for six weeks (from My GP, ")


def test_a_second_clearance_without_limits_never_wipes_the_screenings_limits(db_session):
    user = _user(db_session)
    _screening(db_session, user, tier="easy_only")
    ss.confirm_clearance(db_session, user, "My GP", "No sprints")
    # Nothing is left to lift, so the second one is refused before it writes.
    with pytest.raises(ss.NothingToClear):
        ss.confirm_clearance(db_session, user, "My GP", None)
    db_session.rollback()
    assert ss.latest_screening(db_session, user.id).clearance_limits == "No sprints"
    assert db_session.query(ConsentEvent).count() == 1


def test_limits_are_kept_for_a_rider_who_never_screened(db_session):
    user = _user(db_session)
    hold = ss.open_hold(db_session, user, "hold_all", "Fainted", "detector", red_flag="fainting")
    assert ss.confirm_clearance(db_session, user, "A&E doctor", "Nothing above tempo") is None
    (limit,) = ss.active_limits(db_session, user)
    assert limit.limits == "Nothing above tempo"
    assert limit.cleared_for == hold.reason
    assert ss.safety_state(db_session, user)["limits"][0]["by"] == "A&E doctor"


def test_limits_are_append_only_and_only_retired(db_session):
    user = _user(db_session)
    ss.open_hold(db_session, user, "hold_all", "Fainted", "detector", red_flag="fainting")
    ss.confirm_clearance(db_session, user, "My GP", "No sprints")
    (limit,) = ss.active_limits(db_session, user)
    limit.limits = "Anything goes"
    with pytest.raises(ValueError):
        db_session.commit()
    db_session.rollback()
    db_session.delete(limit)
    with pytest.raises(ValueError):
        db_session.commit()
    db_session.rollback()
    with pytest.raises(ValueError):
        ss.retire_limit(db_session, limit.id, " ")
    ss.retire_limit(db_session, limit.id, "GP lifted it on review")
    assert ss.active_limits(db_session, user) == []
    assert db_session.query(ClearanceLimit).count() == 1
    assert "clearance_limits" in SAFETY_RETAINED_TABLES


# === Fever: the rider's own word, then an easy week ===


def _fever(db, user, source="detector"):
    return ss.open_hold(db, user, "hold_all", "Fever in chat", source, red_flag="fever")


def test_fever_lift_turns_the_hold_into_an_easy_week(db_session):
    user = _user(db_session)
    fever = _fever(db_session, user)
    easy = ss.lift_fever(db_session, user, fever.id)

    db_session.refresh(fever)
    assert fever.lifted_how == "fever_self" and fever.lifted_at is not None
    assert easy.id != fever.id
    assert (easy.level, easy.red_flag, easy.source) == ("easy_only", "fever", "detector")
    week = easy.expires_at - easy.opened_at
    assert abs(week - timedelta(days=ss.FEVER_EASY_DAYS)) < timedelta(seconds=1)
    assert ss.lift_kind(easy) == "expires"
    (consent,) = db_session.query(ConsentEvent).all()
    assert consent.kind == "clearance"
    assert consent.text_shown == ss.FEVER_LIFT_TEXT == (
        "My fever has been gone for 24 hours without paracetamol or ibuprofen, "
        "and my chest has cleared."
    )
    assert consent.doc_version == ss.FEVER_LIFT_VERSION
    assert "doctor" not in consent.text_shown and "hard training" not in consent.text_shown
    assert ss.allowed_intensity(db_session, user) == "easy"


def test_a_fever_lift_never_allows_all_within_seven_days(db_session, monkeypatch):
    # Red-team legal judge #5: the lift used to go straight to "all".
    user = _user(db_session)
    lifted_at = datetime(2026, 10, 8, 10, 0)
    monkeypatch.setattr(ss, "_now", lambda: lifted_at)
    easy = ss.lift_fever(db_session, user, _fever(db_session, user).id)

    # Asked on the day of the lift, about each of the next seven days.
    for d in range(0, 8):
        on = lifted_at.date() + timedelta(days=d)
        assert ss.allowed_intensity(db_session, user, on_date=on) == "easy", d
    # Asked as the week goes by.
    for hours in (1, 24, 72, 7 * 24 - 1):
        monkeypatch.setattr(ss, "_now", lambda h=hours: lifted_at + timedelta(hours=h))
        assert ss.allowed_intensity(db_session, user) == "easy", hours
        assert ss.current_hold(db_session, user.id).id == easy.id
    # And once it is over, it is over without anyone touching it.
    monkeypatch.setattr(ss, "_now", lambda: lifted_at + timedelta(days=7, minutes=1))
    assert ss.allowed_intensity(db_session, user) == "all"
    assert ss.current_hold(db_session, user.id) is None
    assert ss.safety_state(db_session, user)["hold"] is None


def test_an_expired_hold_is_stamped_by_the_next_write(db_session, monkeypatch):
    user = _user(db_session)
    lifted_at = datetime(2026, 10, 8, 10, 0)
    monkeypatch.setattr(ss, "_now", lambda: lifted_at)
    easy = ss.lift_fever(db_session, user, _fever(db_session, user).id)
    monkeypatch.setattr(ss, "_now", lambda: lifted_at + timedelta(days=8))
    ss.open_hold(db_session, user, "easy_only", "Knee", "detector", red_flag="injury")
    db_session.refresh(easy)
    assert easy.lifted_how == "expired" and easy.lifted_at == easy.expires_at


def test_a_doctors_clearance_skips_neither_the_fever_nor_the_easy_week(db_session):
    user = _user(db_session)
    fever = _fever(db_session, user)
    chest = ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
    ss.confirm_clearance(db_session, user, "My GP", None)
    db_session.refresh(fever)
    db_session.refresh(chest)
    assert chest.lifted_how == "clearance"
    assert fever.lifted_at is None
    assert ss.allowed_intensity(db_session, user) == "none"

    easy = ss.lift_fever(db_session, user, fever.id)
    with pytest.raises(ss.NothingToClear) as refused:
        ss.confirm_clearance(db_session, user, "My GP", None)
    db_session.rollback()
    assert "Your easy riding ends by itself on" in str(refused.value)
    db_session.refresh(easy)
    assert easy.lifted_at is None
    assert ss.allowed_intensity(db_session, user) == "easy"


def test_the_easy_week_is_not_a_mistake_and_not_a_fever(db_session):
    user = _user(db_session)
    easy = ss.lift_fever(db_session, user, _fever(db_session, user).id)
    with pytest.raises(ss.HoldNotLiftable):
        ss.lift_hold(db_session, user, easy.id, "mistake")
    with pytest.raises(ss.HoldNotLiftable):
        ss.lift_fever(db_session, user, easy.id)
    assert ss.lift_holds(db_session, user, "mistake") == 0


def test_fever_lift_refuses_other_holds_and_under_18_accounts(db_session):
    user = _user(db_session)
    chest = ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
    with pytest.raises(ss.HoldNotLiftable):
        ss.lift_fever(db_session, user, chest.id)
    fever = _fever(db_session, user)
    ss.open_hold(db_session, user, "hold_all", "Under 18", "detector", red_flag="minor")
    with pytest.raises(ss.HoldNotLiftable):
        ss.lift_fever(db_session, user, fever.id)
    assert ss.lift_fever(db_session, user, "not-a-hold") is None
    assert db_session.query(ConsentEvent).count() == 0


def test_fever_lift_lifts_every_fever_hold_and_a_second_tap_changes_nothing(db_session):
    user = _user(db_session)
    chat = _fever(db_session, user)
    tool = _fever(db_session, user, source="coach_tool")
    easy = ss.lift_fever(db_session, user, chat.id)
    db_session.refresh(tool)
    assert tool.lifted_how == "fever_self"
    assert [h.id for h in ss.open_holds(db_session, user)] == [easy.id]
    assert ss.lift_fever(db_session, user, chat.id).id == chat.id
    assert len(ss.open_holds(db_session, user)) == 1
    assert db_session.query(ConsentEvent).count() == 1


def test_a_fever_mentioned_again_in_the_easy_week_holds_everything_again(db_session):
    user = _user(db_session)
    easy = ss.lift_fever(db_session, user, _fever(db_session, user).id)
    again = _fever(db_session, user)
    db_session.refresh(easy)
    assert again.id != easy.id and again.expires_at is None
    # Beside the easy week, never in its place (reverify round 3, problem 1).
    assert easy.lifted_at is None
    assert ss.allowed_intensity(db_session, user) == "none"


# === The exchange kept with a red flag ===


def test_safety_events_keep_the_exchange(db_session):
    user = _user(db_session)
    db_session.add(SafetyEvent(
        user_id=user.id, kind="chest_pain", source="chat", matched="chest pain",
        rider_message="Had chest pain on the climb, can I still race Sunday?",
        coach_reply="No. Stop riding and get seen today.",
    ))
    db_session.commit()
    event = db_session.query(SafetyEvent).one()
    assert event.rider_message.startswith("Had chest pain")
    assert event.coach_reply == "No. Stop riding and get seen today."


# === Founder alert ===


def test_safety_alert_goes_to_founder_with_a_capped_excerpt(monkeypatch):
    sent = {}

    async def fake_send(to, subject, text_body, from_address=None):
        sent.update(to=to, subject=subject, body=text_body)
        return True

    monkeypatch.setattr(email_service, "send", fake_send)
    ok = asyncio.run(email_service.send_safety_alert("crisis", "r@example.com", "u-1", "x" * 2000))

    assert ok is True
    assert sent["to"] == email_service.settings.founder_alert_email
    assert sent["subject"] == "Forma safety alert: crisis"
    assert "u-1" in sent["body"] and "r@example.com" in sent["body"]
    assert "x" * 497 + "..." in sent["body"] and "x" * 498 not in sent["body"]
    assert "within 24 hours (safeguarding protocol)" in sent["body"]


# === Endpoints ===


def _reset_safety_rate_limits() -> None:
    """Each safety endpoint allows 20 an hour per address, and every test
    client shares one address, so start each test with empty windows."""
    from app.api.v1 import safety as api_safety

    for route in api_safety.router.routes:
        for dep in getattr(route, "dependencies", []):
            window = getattr(dep.dependency, "window", None)
            if window is not None:
                window.reset()


@pytest.fixture
def api():
    """The safety endpoints over a private SQLite database, signed in as one
    rider."""
    from app.api.v1.deps import get_current_user
    from app.database import get_db
    from app.main import app

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = _user(db, "api@example.com", ftp=240)

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, user.id)
    _reset_safety_rate_limits()
    try:
        yield TestClient(app), db, user
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        db.close()


def test_get_safety_state_endpoint(api):
    client, db, user = api
    r = client.get("/api/v1/users/me/safety-state")
    assert r.status_code == 200
    assert r.json()["allowed"] == "all" and r.json()["ftp"] == 240


def test_clearance_endpoint_lifts_the_hold(api):
    client, db, user = api
    ss.open_hold(db, user.id, "hold_all", "Chest pain", "detector")
    r = client.post("/api/v1/users/me/safety/clearance", json={"by": "My GP", "limits": None})
    assert r.status_code == 200
    assert r.json()["allowed"] == "all" and r.json()["hold"] is None
    assert db.query(ConsentEvent).filter_by(kind="clearance").count() == 1


def test_clearance_endpoint_needs_by(api):
    client, db, user = api
    assert client.post("/api/v1/users/me/safety/clearance", json={"by": ""}).status_code == 422


def test_mistake_endpoint_lifts_a_detector_hold(api):
    client, db, user = api
    hold = ss.open_hold(db, user.id, "hold_all", "Chest strap", "detector", red_flag="chest_pain")
    r = client.post("/api/v1/users/me/safety/mistake", json={"hold_id": hold.id})
    assert r.status_code == 200 and r.json()["hold"] is None
    db.refresh(hold)
    assert hold.lifted_how == "mistake"


def test_mistake_endpoint_refuses_screening_holds_and_strangers(api):
    client, db, user = api
    hold = ss.open_hold(db, user.id, "easy_only", "Screening yes", "screening")
    r = client.post("/api/v1/users/me/safety/mistake", json={"hold_id": hold.id})
    assert r.status_code == 400 and "Settings, then Health" in r.json()["detail"]
    other = ss.open_hold(db, "someone-else", "hold_all", "x", "detector")
    assert client.post("/api/v1/users/me/safety/mistake", json={"hold_id": other.id}).status_code == 404


def test_ride_mode_acknowledgement(api):
    client, db, user = api
    r = client.post(
        "/api/v1/users/me/acknowledgements",
        json={"kind": "ride_mode", "text_shown": "Before your first ride with Forma."},
        headers={"x-forwarded-for": "1.1.1.1, 81.2.69.160"},
    )
    assert r.status_code == 200 and r.json() == {"ok": True}
    db.expire_all()
    assert db.get(User, user.id).ride_mode_ack_at is not None
    ev = db.query(ConsentEvent).one()
    assert (ev.kind, ev.source, ev.doc_version) == ("ride_mode", "ride_mode", ss.RIDE_MODE_VERSION)
    # No x-vercel-id: a direct call, so the last hop (app.core.ratelimit).
    assert ev.ip == "81.2.69.160"
    assert client.post(
        "/api/v1/users/me/acknowledgements", json={"kind": "terms", "text_shown": "x"}
    ).status_code == 422


def test_ride_session_start_is_recorded(api):
    client, db, user = api
    r = client.post("/api/v1/users/me/ride-session-starts", json={
        "workout_id": "w-1", "steps_hash": "a" * 64, "ftp": 240, "erg": True,
        "max_target_watts": 312,
    })
    assert r.status_code == 200 and r.json() == {"ok": True}
    row = db.query(RideSessionStart).one()
    assert (row.user_id, row.ftp, row.erg, row.max_target_watts) == (user.id, 240, True, 312)


# === Endpoints: under-18 holds and the fever lift ===


@pytest.mark.parametrize("source", ["detector", "coach_tool"])
def test_mistake_endpoint_refuses_an_under_18_hold(api, source):
    client, db, user = api
    minor = ss.open_hold(db, user.id, "hold_all", "Under 18", source, red_flag="minor")
    r = client.post("/api/v1/users/me/safety/mistake", json={"hold_id": minor.id})
    assert r.status_code == 400
    assert "Forma is for adults, 18 and over" in r.json()["detail"]
    db.refresh(minor)
    assert minor.lifted_at is None
    assert client.get("/api/v1/users/me/safety-state").json()["allowed"] == "none"


def test_clearance_endpoint_refuses_an_under_18_account(api):
    client, db, user = api
    ss.open_hold(db, user.id, "hold_all", "Under 18", "detector", red_flag="minor")
    r = client.post("/api/v1/users/me/safety/clearance", json={"by": "My GP", "limits": None})
    assert r.status_code == 400
    assert "Forma is for adults, 18 and over" in r.json()["detail"]
    assert db.query(ConsentEvent).count() == 0
    state = client.get("/api/v1/users/me/safety-state").json()
    assert state["allowed"] == "none" and state["hold"]["lift_kind"] == "admin_only"


def test_fever_lift_endpoint(api):
    client, db, user = api
    fever = ss.open_hold(db, user.id, "hold_all", "Fever", "detector", red_flag="fever")
    state = client.get("/api/v1/users/me/safety-state").json()
    assert state["hold"]["lift_kind"] == "fever_self"

    r = client.post("/api/v1/users/me/safety/fever-lift", json={"hold_id": fever.id})
    assert r.status_code == 200
    body = r.json()
    assert body["allowed"] == "easy"
    assert body["hold"]["lift_kind"] == "expires" and body["hold"]["expires_at"]
    consent = db.query(ConsentEvent).one()
    assert consent.text_shown == ss.FEVER_LIFT_TEXT

    # A second tap is harmless.
    again = client.post("/api/v1/users/me/safety/fever-lift", json={"hold_id": fever.id})
    assert again.status_code == 200 and again.json()["allowed"] == "easy"
    assert db.query(ConsentEvent).count() == 1

    # The easy week can't be waved away.
    easy_id = body["hold"]["id"]
    r = client.post("/api/v1/users/me/safety/mistake", json={"hold_id": easy_id})
    assert r.status_code == 400 and "ends by itself on" in r.json()["detail"]
    r = client.post("/api/v1/users/me/safety/clearance", json={"by": "My GP"})
    assert r.status_code == 409 and "ends by itself" in r.json()["detail"]
    assert client.get("/api/v1/users/me/safety-state").json()["allowed"] == "easy"
    assert db.query(ConsentEvent).count() == 1


def test_fever_lift_endpoint_refuses_other_holds_and_strangers(api):
    client, db, user = api
    chest = ss.open_hold(db, user.id, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
    r = client.post("/api/v1/users/me/safety/fever-lift", json={"hold_id": chest.id})
    assert r.status_code == 400 and "isn't for a fever" in r.json()["detail"]
    other = ss.open_hold(db, "someone-else", "hold_all", "Fever", "detector", red_flag="fever")
    r = client.post("/api/v1/users/me/safety/fever-lift", json={"hold_id": other.id})
    assert r.status_code == 404
    assert db.query(ConsentEvent).count() == 0


def test_refusals_read_cleanly():
    from app.api.v1 import safety as api_safety

    texts = [api_safety.MINOR_REFUSAL, *api_safety._NOT_A_MISTAKE.values()]
    for text in texts:
        assert "\u2014" not in text and "\u2013" not in text and "!" not in text


# === Re-verification S3: a head injury has its own way back, and it is graded ===


def _head(db, user, source="detector"):
    return ss.open_hold(db, user, "hold_all", "Hit their head in a crash", source, red_flag="head_injury")


def test_the_generic_clearance_never_lifts_a_head_injury(db_session):
    user = _user(db_session)
    head = _head(db_session, user)
    with pytest.raises(ss.NothingToClear) as refused:
        ss.confirm_clearance(db_session, user, "My GP", None)
    db_session.rollback()
    assert "head injury" in str(refused.value) and "its own check" in str(refused.value)
    db_session.refresh(head)
    assert head.lifted_at is None
    assert db_session.query(ConsentEvent).count() == 0

    # With a chest hold too, the clearance lifts the chest and leaves the head.
    chest = ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
    ss.confirm_clearance(db_session, user, "My GP", None)
    db_session.refresh(chest)
    db_session.refresh(head)
    assert chest.lifted_how == "clearance" and head.lifted_at is None
    assert ss.allowed_intensity(db_session, user) == "none"
    assert db_session.query(ConsentEvent).one().text_shown == ss.CLEARANCE_TEXT


def test_a_physio_is_told_a_head_injury_needs_a_doctor(db_session):
    user = _user(db_session)
    head = _head(db_session, user)
    with pytest.raises(ss.ClearanceOutOfScope) as refused:
        ss.confirm_clearance(db_session, user, "My physio", None)
    assert "needs a doctor" in str(refused.value)
    with pytest.raises(ss.ClearanceOutOfScope):
        ss.lift_head_injury(db_session, user, head.id, "My physio")
    db_session.rollback()
    db_session.refresh(head)
    assert head.lifted_at is None
    assert db_session.query(ConsentEvent).count() == 0


def test_a_head_injury_lift_is_a_graded_return(db_session, monkeypatch):
    # The reverify probe: after a GP clearance, allowed was "all" on days 1,
    # 7 and 20, though the reply promised no racing or group riding before
    # day 21.
    user = _user(db_session)
    injured = datetime(2026, 10, 1, 18, 0)
    monkeypatch.setattr(ss, "_now", lambda: injured)
    head = _head(db_session, user)
    day21 = injured.date() + timedelta(days=21)
    assert ss.no_racing_or_group_until(db_session, user) == day21
    assert ss.safety_state(db_session, user)["no_racing_or_group_until"] == day21.isoformat()

    checked = injured + timedelta(days=1, hours=2)
    monkeypatch.setattr(ss, "_now", lambda: checked)
    easy = ss.lift_head_injury(db_session, user, head.id, "My GP", "No sprints for a month")

    db_session.refresh(head)
    assert head.lifted_how == "head_clearance" and "Checked by: My GP" in head.note
    assert (easy.level, easy.red_flag, easy.source) == ("easy_only", "head_injury", "detector")
    assert easy.expires_at == injured + timedelta(days=ss.HEAD_EASY_DAYS)
    assert ss.lift_kind(easy) == "expires"
    consent = db_session.query(ConsentEvent).one()
    assert consent.text_shown == ss.HEAD_LIFT_TEXT == (
        "A doctor has checked me since I hit my head, and I've had no symptoms for "
        "at least 24 hours."
    )
    assert consent.doc_version == ss.HEAD_LIFT_VERSION and consent.kind == "clearance"
    assert "hard training" not in consent.text_shown
    (limit,) = ss.active_limits(db_session, user)
    assert limit.limits == "No sprints for a month" and limit.consent_event_id == consent.id

    # Days 1 and 7: easy riding only. Day 14 is the last easy day.
    for day in (1, 7, 14):
        on = injured.date() + timedelta(days=day)
        assert ss.allowed_intensity(db_session, user, on_date=on) == "easy", day
    # Day 20: training is open again, but racing and group riding are not.
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=20))
    state = ss.safety_state(db_session, user)
    assert state["allowed"] == "all" and state["hold"] is None
    assert state["no_racing_or_group_until"] == day21.isoformat()
    # Day 21: nothing holds them back.
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=21))
    assert ss.safety_state(db_session, user)["no_racing_or_group_until"] is None


def test_the_coach_is_told_the_head_injury_return_in_its_own_words(db_session):
    """After the head check, the coach's lift sentence names the easy days
    and day 21, and its safety context carries the no-racing date."""
    from app.services import safety_screen

    user = _user(db_session)
    head = _head(db_session, user)
    easy = ss.lift_head_injury(db_session, user, head.id, "My GP")
    sentence = safety_screen.lift_sentence(easy)
    ends = easy.expires_at
    day21 = head.opened_at + timedelta(days=ss.HEAD_NO_RACING_DAYS)
    assert sentence == (
        f"Easy riding only until {ends.day} {ends:%B} after your head injury, then build "
        f"back gradually. No racing or group riding before {day21.day} {day21:%B}."
    )
    assert "ends by itself" not in sentence
    context = safety_screen.coach_safety_context(db_session, user)
    assert day21.date().isoformat() in context["no_racing_or_group_until"]
    assert "no racing" in context["no_racing_or_group_until"].lower()


def test_a_head_injury_checked_after_two_weeks_still_keeps_racing_off_until_day_21(
    db_session, monkeypatch
):
    user = _user(db_session)
    injured = datetime(2026, 10, 1, 9, 0)
    monkeypatch.setattr(ss, "_now", lambda: injured)
    head = _head(db_session, user)
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=16))
    assert ss.lift_head_injury(db_session, user, head.id, "Another doctor").id == head.id
    assert ss.open_holds(db_session, user) == []
    assert ss.allowed_intensity(db_session, user) == "all"
    assert ss.no_racing_or_group_until(db_session, user) == injured.date() + timedelta(days=21)


def test_the_head_injury_lift_lifts_every_head_hold_and_a_second_tap_changes_nothing(db_session):
    user = _user(db_session)
    chat = _head(db_session, user)
    tool = _head(db_session, user, source="coach_tool")
    easy = ss.lift_head_injury(db_session, user, chat.id, "My GP")
    db_session.refresh(tool)
    assert tool.lifted_how == "head_clearance"
    assert [h.id for h in ss.open_holds(db_session, user)] == [easy.id]
    assert ss.lift_head_injury(db_session, user, chat.id, "My GP").id == chat.id
    assert db_session.query(ConsentEvent).count() == 1
    # The easy riding that follows can't be waved away or cleared.
    with pytest.raises(ss.HoldNotLiftable):
        ss.lift_hold(db_session, user, easy.id, "mistake")
    with pytest.raises(ss.NothingToClear):
        ss.confirm_clearance(db_session, user, "My GP", None)


def test_the_head_injury_lift_refuses_other_holds_strangers_and_under_18s(db_session):
    user = _user(db_session)
    chest = ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
    with pytest.raises(ss.HoldNotLiftable):
        ss.lift_head_injury(db_session, user, chest.id, "My GP")
    with pytest.raises(ValueError):
        ss.lift_head_injury(db_session, user, chest.id, "  ")
    assert ss.lift_head_injury(db_session, user, "not-a-hold", "My GP") is None
    head = _head(db_session, user)
    ss.open_hold(db_session, user, "hold_all", "Under 18", "detector", red_flag="minor")
    with pytest.raises(ss.HoldNotLiftable):
        ss.lift_head_injury(db_session, user, head.id, "My GP")
    assert db_session.query(ConsentEvent).count() == 0


def test_a_head_injury_marked_a_mistake_leaves_no_racing_date(db_session):
    user = _user(db_session)
    head = _head(db_session, user)
    ss.lift_hold(db_session, user, head.id, "mistake")
    assert ss.no_racing_or_group_until(db_session, user) is None
    assert ss.allowed_intensity(db_session, user) == "all"


def test_head_injury_lift_endpoint(api):
    client, db, user = api
    head = _head(db, user)
    state = client.get("/api/v1/users/me/safety-state").json()
    assert state["hold"]["lift_kind"] == "head_injury"
    assert state["no_racing_or_group_until"]

    # The generic clearance is refused, and records nothing.
    r = client.post("/api/v1/users/me/safety/clearance", json={"by": "My GP"})
    assert r.status_code == 409 and "head injury" in r.json()["detail"]
    assert db.query(ConsentEvent).count() == 0

    r = client.post("/api/v1/users/me/safety/head-injury-lift",
                    json={"hold_id": head.id, "by": "My physio"})
    assert r.status_code == 400 and "needs a doctor" in r.json()["detail"]

    r = client.post("/api/v1/users/me/safety/head-injury-lift",
                    json={"hold_id": head.id, "by": "My GP", "limits": None})
    assert r.status_code == 200
    body = r.json()
    assert body["allowed"] == "easy"
    assert body["hold"]["lift_kind"] == "expires" and body["hold"]["red_flag"] == "head_injury"
    assert body["no_racing_or_group_until"] == (
        head.opened_at.date() + timedelta(days=21)
    ).isoformat()
    assert db.query(ConsentEvent).one().text_shown == ss.HEAD_LIFT_TEXT

    r = client.post("/api/v1/users/me/safety/mistake", json={"hold_id": body["hold"]["id"]})
    assert r.status_code == 400 and "follows your head injury" in r.json()["detail"]

    chest = ss.open_hold(db, user.id, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
    r = client.post("/api/v1/users/me/safety/head-injury-lift", json={"hold_id": chest.id, "by": "My GP"})
    assert r.status_code == 400 and "isn't for a head injury" in r.json()["detail"]
    other = _head(db, "someone-else")
    r = client.post("/api/v1/users/me/safety/head-injury-lift", json={"hold_id": other.id, "by": "My GP"})
    assert r.status_code == 404
    assert db.query(ConsentEvent).count() == 1


# === New problem 8: a clearance with nothing to lift records nothing ===


@pytest.mark.parametrize("setup, says", [
    (lambda db, u: None, "Nothing is on hold"),
    (lambda db, u: ss.open_hold(db, u, "hold_all", "Fever", "detector", red_flag="fever"),
     "My fever has gone"),
    (lambda db, u: ss.open_hold(db, u, "easy_only", "Six weeks off", "layoff", red_flag="layoff",
                                expires_at=datetime.utcnow() + timedelta(days=14)),
     "ends by itself"),
    (lambda db, u: _screening(db, u, tier="easy_only", clearance_confirmed_at=datetime.utcnow()),
     "Nothing is on hold"),
    (lambda db, u: ss.open_hold(db, u, "hold_all", "Set by hand", "admin"), "set by Forma"),
])
def test_clearance_endpoint_with_nothing_to_lift_is_409_and_records_nothing(api, setup, says):
    client, db, user = api
    setup(db, user)
    db.commit()
    before = client.get("/api/v1/users/me/safety-state").json()
    for by in ("My GP", "My physio"):
        r = client.post("/api/v1/users/me/safety/clearance", json={"by": by, "limits": "No sprints"})
        assert r.status_code == 409, (by, r.json())
        assert says in r.json()["detail"]
    assert db.query(ConsentEvent).count() == 0
    assert db.query(ClearanceLimit).count() == 0
    assert client.get("/api/v1/users/me/safety-state").json() == before


# === New problem 9: each hold in its own words ===


_DASHES = ("—", "–")


def _clean(text: str) -> None:
    assert not any(d in text for d in _DASHES) and "!" not in text, text


def test_a_fever_never_mentions_a_doctor(db_session):
    user = _user(db_session)
    fever = ss.open_hold(db_session, user, "hold_all", "Fever", "detector", red_flag="fever")
    assert ss.hold_label(fever) == "On hold until your fever has been gone for 24 hours. "
    assert ss.standing_label(db_session, user) == ss.FEVER_HOLD_LABEL
    assert ss.hold_explanation(fever) == "Riding is on hold until your fever has been gone for 24 hours."
    refusal = ss.export_refusal(db_session, user)
    assert refusal == (
        "Riding is on hold until your fever has been gone for 24 hours, so I can't export "
        "this session yet."
    )
    for text in (ss.hold_label(fever), ss.hold_explanation(fever), refusal):
        assert "doctor" not in text

    # A fever with a heart answer still waiting: the doctor is needed too.
    _screening(db_session, user, tier="hold_all")
    assert ss.standing_label(db_session, user) == ss.DOCTOR_HOLD_LABEL
    assert "doctor" in ss.export_refusal(db_session, user)


def test_the_easy_week_after_a_fever_reads_as_illness_not_a_break(db_session, monkeypatch):
    user = _user(db_session)
    now = datetime(2026, 10, 8, 10, 0)
    monkeypatch.setattr(ss, "_now", lambda: now)
    easy = ss.lift_fever(db_session, user, ss.open_hold(
        db_session, user, "hold_all", "Fever", "detector", red_flag="fever").id)
    assert ss.hold_label(easy) == ss.ILLNESS_EASY_LABEL
    assert "break" not in ss.hold_label(easy) and "illness" in ss.hold_label(easy)
    assert ss.hold_explanation(easy) == (
        "Riding is easy only until 15 October, your first week back after illness."
    )
    windows = ss.easy_windows(db_session, user)
    assert windows == [(date(2026, 10, 16), ss.ILLNESS_EASY_LABEL,
                        "this is your easy week after illness")]
    assert ss.easy_window_on(windows, date(2026, 10, 12)) == (
        ss.ILLNESS_EASY_LABEL, "this is your easy week after illness"
    )
    assert ss.easy_window_on(windows, date(2026, 10, 16)) is None
    assert ss.export_refusal(db_session, user) == ss.EXPORT_EASY_REFUSAL

    # A break that runs longer still reads as illness on the days they share.
    _ride(db_session, user, 40)
    monkeypatch.setattr(ss, "_today", lambda: TODAY)
    windows = ss.easy_windows(db_session, user)
    assert windows[0][1] == ss.ILLNESS_EASY_LABEL and windows[-1][1] == ss.BREAK_LABEL
    assert ss.easy_window_on(windows, date(2026, 10, 12))[0] == ss.ILLNESS_EASY_LABEL


def test_an_under_18_account_has_its_own_words(db_session):
    user = _user(db_session)
    minor = ss.open_hold(db_session, user, "hold_all", "Under 18", "detector", red_flag="minor")
    ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
    assert ss.hold_label(minor) == ss.ACCOUNT_HOLD_LABEL
    assert ss.standing_label(db_session, user) == ss.ACCOUNT_HOLD_LABEL
    refusal = ss.export_refusal(db_session, user)
    assert refusal.startswith("This account is on hold because Forma is for adults, 18 and over")
    assert "doctor" not in refusal and ss.FORMA_EMAIL in refusal
    assert "doctor" not in ss.hold_explanation(minor)


def test_hold_words_by_kind(db_session):
    user = _user(db_session)
    chest = ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
    knee = ss.open_hold(db_session, user, "easy_only", "Knee", "coach_tool", red_flag="injury")
    head = _head(db_session, user)
    brk = {"level": "easy_only", "source": "layoff", "red_flag": "layoff", "expires_at": None}
    assert ss.hold_label(chest) == ss.DOCTOR_HOLD_LABEL
    assert ss.hold_explanation(knee) == (
        "Hard sessions are on hold until you tell me a doctor has cleared you."
    )
    assert ss.hold_label(head) == ss.HEAD_HOLD_LABEL
    assert ss.hold_label(brk) == ss.BREAK_LABEL
    assert ss.hold_label({**brk, "level": "hold_all"}) == ss.ACCOUNT_HOLD_LABEL
    # A doctor's hold outranks a head injury's on the plan.
    assert ss.standing_label(db_session, user) == ss.DOCTOR_HOLD_LABEL
    ss.lift_hold(db_session, user, chest.id, "mistake")
    ss.lift_hold(db_session, user, knee.id, "mistake")
    assert ss.standing_label(db_session, user) == ss.HEAD_HOLD_LABEL
    assert "hit your head" in ss.export_refusal(db_session, user)
    by_hand = ss.open_hold(db_session, user, "hold_all", "Set by hand", "admin")
    assert ss.hold_explanation(by_hand).endswith(f"email {ss.FORMA_EMAIL}.")


def test_hold_labels_are_recognisable_and_read_cleanly():
    labels = ss.HOLD_LABELS
    assert len(set(labels)) == len(labels)
    for a in labels:
        _clean(a)
        assert a.endswith(". ")
        for b in labels:
            assert a == b or not b.startswith(a), (a, b)
    holds = [
        {"red_flag": flag, "source": source, "level": level, "expires_at": expires}
        for flag in (None, "minor", "fever", "head_injury", "layoff", "chest_pain")
        for source in ("detector", "admin", "layoff", "screening")
        for level in ("easy_only", "hold_all")
        for expires in (None, datetime(2026, 10, 15))
    ]
    for hold in holds:
        assert ss.hold_label(hold) in labels
        _clean(ss.hold_explanation(hold))
    _clean(ss.EXPORT_EASY_REFUSAL)
    _clean(ss.HEAD_LIFT_TEXT)


def test_refusals_for_nothing_to_clear_read_cleanly():
    open_now = [
        SafetyHold(red_flag="head_injury", source="detector", level="hold_all"),
        SafetyHold(red_flag="fever", source="detector", level="hold_all"),
        SafetyHold(red_flag=None, source="admin", level="hold_all"),
        SafetyHold(red_flag="fever", source="detector", level="easy_only",
                   expires_at=datetime(2026, 10, 15)),
        SafetyHold(red_flag="layoff", source="layoff", level="easy_only"),
    ]
    for hold in open_now:
        _clean(ss._nothing_to_clear([hold]))
    _clean(ss._nothing_to_clear([]))


# === Forma's own tools: lifting by hand, reviews, retention ===


def test_admin_lift_frees_an_adult_misread_as_a_minor(db_session, monkeypatch):
    from app.services import plan_service

    synced = []
    monkeypatch.setattr(plan_service, "sync_hold_marks",
                        lambda db, user_id, commit=True: synced.append(user_id))
    user = _user(db_session)
    chat = ss.open_hold(db_session, user, "hold_all", "Said 16", "detector", red_flag="minor")
    tool = ss.open_hold(db_session, user, "hold_all", "Said 16", "coach_tool", red_flag="minor")
    chest = ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")

    with pytest.raises(ValueError):
        ss.admin_lift(db_session, chat.id, "  ")
    assert ss.admin_lift(db_session, "not-a-hold", "x") is None

    lifted = ss.admin_lift(db_session, chat.id, "16 and a half stone, not 16 years old")
    assert lifted.id == chat.id
    for hold in (chat, tool):
        db_session.refresh(hold)
        assert hold.lifted_how == "admin"
        assert "Lifted by Forma: 16 and a half stone" in hold.note
    db_session.refresh(chest)
    assert chest.lifted_at is None  # only the age, nothing medical
    assert ss.minor_hold(db_session, user) is None
    assert synced == [user.id]

    # A hold set by hand, and anything else, lifts the same way.
    ss.admin_lift(db_session, chest.id, "Cardiologist letter seen")
    assert ss.allowed_intensity(db_session, user) == "all"
    # Lifting again changes nothing.
    ss.admin_lift(db_session, chest.id, "Again", sync_plan=False)
    db_session.refresh(chest)
    assert "Again" not in chest.note


def test_mark_event_reviewed(db_session, monkeypatch):
    user = _user(db_session)
    event = SafetyEvent(user_id=user.id, kind="minor", source="chat", matched="i'm 16")
    db_session.add(event)
    db_session.commit()
    with pytest.raises(ValueError):
        ss.mark_event_reviewed(db_session, event.id, "")
    assert ss.mark_event_reviewed(db_session, "nope", "x") is None

    first = datetime(2026, 10, 8, 9, 30)
    monkeypatch.setattr(ss, "_now", lambda: first)
    ss.mark_event_reviewed(db_session, event.id, "Weight in stone, not an age. Hold lifted.")
    monkeypatch.setattr(ss, "_now", lambda: first + timedelta(days=2))
    ss.mark_event_reviewed(db_session, event.id, "Rider emailed to confirm.")
    db_session.refresh(event)
    assert event.reviewed_at == first
    assert event.review_note == (
        "2026-10-08 09:30 Weight in stone, not an age. Hold lifted.\n"
        "2026-10-10 09:30 Rider emailed to confirm."
    )


def test_safety_events_keep_the_stated_age(db_session):
    user = _user(db_session)
    db_session.add(SafetyEvent(user_id=user.id, kind="minor", source="chat", matched="i'm 15",
                               stated_age=15))
    db_session.commit()
    assert db_session.query(SafetyEvent).one().stated_age == 15


def test_minor_retention_until():
    said = datetime(2026, 10, 8, 12, 0)
    # A 15-year-old is 21 within six years of saying so: that outlasts three
    # years after an account deleted now.
    # The 21st birthday is kept whole: the records go from the start of the
    # day after, as with a date of birth on file.
    event = SafetyEvent(kind="minor", created_at=said, stated_age=15)
    assert ss.minor_retention_until(event, deleted_at=said) == datetime(2032, 10, 9)
    # A 17-year-old whose account went late: three years from deletion wins.
    late = datetime(2031, 1, 1)
    older = SafetyEvent(kind="minor", created_at=said, stated_age=17, subject_deleted_at=late)
    assert ss.minor_retention_until(older) == datetime(2034, 1, 1)
    # No age stated: taken as 13, the youngest plausible age, so kept to the
    # 21st birthday that implies, eight years on (reverify round 3, problem 9).
    assert ss.minor_retention_until(
        SafetyEvent(kind="minor", created_at=said), deleted_at=said
    ) == datetime(2034, 10, 9)
    # A leap day never comes back a day early, and dicts work too.
    leap = {"created_at": datetime(2028, 2, 29), "stated_age": 16,
            "subject_deleted_at": datetime(2028, 2, 29)}
    assert ss.minor_retention_until(leap) == datetime(2033, 3, 2)


def test_minor_retention_reads_the_age_from_an_older_record():
    """A minor record written before stated_age was kept: the age comes from
    the rider's own words, then from what the detector matched."""
    said = datetime(2026, 10, 8, 12, 0)
    old = SafetyEvent(kind="minor", created_at=said, rider_message="I'm 15 and love racing")
    assert ss.minor_retention_until(old, deleted_at=said) == datetime(2032, 10, 9)
    matched_only = SafetyEvent(kind="minor", created_at=said, matched="I'm 16")
    assert ss.minor_retention_until(matched_only, deleted_at=said) == datetime(2031, 10, 9)
    # Words that give no under-18 age (or an adult's weight): still a minor
    # flag, so the youngest plausible age, 13.
    adult = SafetyEvent(kind="minor", created_at=said, rider_message="I'm 16 and a half stone")
    assert ss.minor_retention_until(adult, deleted_at=said) == datetime(2034, 10, 9)


# === Reverify round 3, problem 1: a re-mention never skips the easy days ===
#
# The same fever or head injury mentioned again during the easy days that
# follow its lift opened a full hold that superseded the easy one, so "This
# was a mistake" on it left nothing: allowed went straight to "all". It also
# moved the 21-day racing clock (27 October to 29 October).


def _lift(db, user, flag, hold):
    if flag == "fever":
        return ss.lift_fever(db, user, hold.id)
    return ss.lift_head_injury(db, user, hold.id, "My GP")


@pytest.mark.parametrize("flag", ["fever", "head_injury"])
def test_a_remention_in_the_easy_days_sits_beside_them(db_session, monkeypatch, flag):
    user = _user(db_session)
    told = datetime(2026, 10, 6, 9, 0)
    monkeypatch.setattr(ss, "_now", lambda: told)
    first = ss.open_hold(db_session, user, "hold_all", "First mention", "detector", red_flag=flag)
    day21 = told.date() + timedelta(days=ss.HEAD_NO_RACING_DAYS)
    monkeypatch.setattr(ss, "_now", lambda: told + timedelta(days=1))
    easy = _lift(db_session, user, flag, first)
    easy_ends = easy.expires_at

    # Two days into the easy days, the rider mentions it again.
    again_at = told + timedelta(days=3)
    monkeypatch.setattr(ss, "_now", lambda: again_at)
    again = ss.open_hold(db_session, user, "hold_all", "Mentioned again", "detector",
                         red_flag=flag, note='Matched "again"')
    db_session.refresh(easy)
    assert again.id != easy.id
    assert easy.lifted_at is None and easy.lifted_how is None
    assert ss.remention_of(db_session, again).id == easy.id
    assert again.note.startswith('Matched "again"\nMentioned again during the easy riding')
    assert ss.current_hold(db_session, user.id).id == again.id
    assert ss.allowed_intensity(db_session, user) == "none"
    if flag == "head_injury":
        # The racing clock still runs from the day the rider first told us.
        assert ss.no_racing_or_group_until(db_session, user) == day21

    # "This was a mistake" lifts the re-mention alone. The easy days run on.
    ss.lift_hold(db_session, user, again.id, "mistake", note="Rider marked it a mistake.")
    db_session.refresh(easy)
    assert easy.lifted_at is None and easy.expires_at == easy_ends
    assert [h.id for h in ss.open_holds(db_session, user)] == [easy.id]
    assert ss.allowed_intensity(db_session, user) == "easy"
    for day in range((easy_ends.date() - again_at.date()).days + 1):
        on = again_at.date() + timedelta(days=day)
        assert ss.allowed_intensity(db_session, user, on_date=on) == "easy", day
    if flag == "head_injury":
        assert ss.no_racing_or_group_until(db_session, user) == day21
    # And the easy days themselves can't be waved away.
    with pytest.raises(ss.HoldNotLiftable):
        ss.lift_hold(db_session, user, easy.id, "mistake")
    assert ss.lift_holds(db_session, user, "mistake") == 0
    assert ss.allowed_intensity(db_session, user) == "easy"


@pytest.mark.parametrize("flag", ["fever", "head_injury"])
def test_a_second_remention_returns_the_first_and_nothing_supersedes_the_easy_days(
    db_session, flag
):
    user = _user(db_session)
    easy = _lift(db_session, user, flag, ss.open_hold(
        db_session, user, "hold_all", "First", "detector", red_flag=flag))
    again = ss.open_hold(db_session, user, "hold_all", "Again", "detector", red_flag=flag)
    third = ss.open_hold(db_session, user, "hold_all", "Again", "detector", red_flag=flag)
    milder = ss.open_hold(db_session, user, "easy_only", "Milder", "detector", red_flag=flag)
    assert third.id == again.id and milder.id == again.id
    db_session.refresh(easy)
    assert easy.lifted_at is None
    assert {h.id for h in ss.open_holds(db_session, user)} == {easy.id, again.id}


def test_a_head_injury_remention_checked_again_keeps_the_original_two_weeks(
    db_session, monkeypatch
):
    """The re-mention lifted by the head check again: the easy riding still
    ends two weeks after the injury, not two weeks after the re-mention, and
    day 21 stays where it was."""
    user = _user(db_session)
    injured = datetime(2026, 10, 6, 9, 0)
    monkeypatch.setattr(ss, "_now", lambda: injured)
    head = _head(db_session, user)
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=1))
    easy = ss.lift_head_injury(db_session, user, head.id, "My GP")
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=5))
    again = _head(db_session, user)
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=6))
    after = ss.lift_head_injury(db_session, user, again.id, "My GP")
    assert after.id == easy.id
    assert after.expires_at == injured + timedelta(days=ss.HEAD_EASY_DAYS)
    assert ss.no_racing_or_group_until(db_session, user) == injured.date() + timedelta(days=21)
    assert ss.allowed_intensity(db_session, user) == "easy"

    # Mentioned again after the two weeks, then checked: straight back to
    # training, and racing still waits for day 21 of the first injury.
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=13))
    late = _head(db_session, user)
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=15))
    assert ss.lift_head_injury(db_session, user, late.id, "Another doctor").id == late.id
    assert ss.open_holds(db_session, user) == []
    assert ss.allowed_intensity(db_session, user) == "all"
    assert ss.no_racing_or_group_until(db_session, user) == injured.date() + timedelta(days=21)


def test_a_fever_remention_lifted_again_restarts_only_the_easy_week(db_session, monkeypatch):
    user = _user(db_session)
    start = datetime(2026, 10, 6, 9, 0)
    monkeypatch.setattr(ss, "_now", lambda: start)
    easy = ss.lift_fever(db_session, user, _fever(db_session, user).id)
    monkeypatch.setattr(ss, "_now", lambda: start + timedelta(days=3))
    again = _fever(db_session, user)
    monkeypatch.setattr(ss, "_now", lambda: start + timedelta(days=4))
    week = ss.lift_fever(db_session, user, again.id)
    db_session.refresh(easy)
    # A fever back is a new illness: a fresh easy week from today, never a
    # shorter one.
    assert easy.lifted_how == "superseded"
    assert week.expires_at == start + timedelta(days=4 + ss.FEVER_EASY_DAYS)
    assert [h.id for h in ss.open_holds(db_session, user)] == [week.id]
    assert ss.allowed_intensity(db_session, user) == "easy"


def test_a_head_injury_the_coach_holds_on_purpose_is_a_new_injury(db_session, monkeypatch):
    """The coach's apply_safety_hold is its own judgement that this is new
    ("I crashed again today"): its own hold beside the easy riding, and the
    racing clock runs from it."""
    user = _user(db_session)
    injured = datetime(2026, 10, 6, 9, 0)
    monkeypatch.setattr(ss, "_now", lambda: injured)
    easy = ss.lift_head_injury(db_session, user, _head(db_session, user).id, "My GP")
    crashed = injured + timedelta(days=4)
    monkeypatch.setattr(ss, "_now", lambda: crashed)
    tool = _head(db_session, user, source="coach_tool")
    db_session.refresh(easy)
    assert easy.lifted_at is None and ss.remention_of(db_session, tool) is None
    assert ss.no_racing_or_group_until(db_session, user) == crashed.date() + timedelta(days=21)


def test_a_higher_hold_still_supersedes_a_lower_one_it_shares_every_way_out_with(db_session):
    user = _user(db_session)
    low = ss.open_hold(db_session, user, "easy_only", "Knee", "detector", red_flag="injury")
    high = ss.open_hold(db_session, user, "hold_all", "Knee, worse", "detector", red_flag="injury")
    db_session.refresh(low)
    assert low.lifted_how == "superseded" and high.lifted_at is None
    fever = _fever(db_session, user)
    assert fever.note is None  # no easy days running, so no re-mention line


@pytest.mark.parametrize("flag, text", [
    ("fever", "I had a fever last week, feeling much better now"),
    ("fever", "had a temperature on monday, fine now, can I do intervals?"),
    ("head_injury", "how long after hitting my head can I do intervals?"),
    ("head_injury", "I hit my head in the crash last week, all good now"),
])
def test_the_reverify_phrasings_leave_the_easy_days_after_a_mistake(api, flag, text):
    """End to end: the detector reads the re-mention, the banner's "This was
    a mistake" goes through, and the easy days are still there."""
    from app.services import safety_screen

    client, db, user = api
    first = ss.open_hold(db, user.id, "hold_all", "First", "detector", red_flag=flag)
    first.opened_at = datetime.utcnow() - timedelta(days=3)
    db.commit()
    easy = _lift(db, user, flag, first)
    racing = ss.no_racing_or_group_until(db, user)

    result = safety_screen.screen_message(db, db.get(User, user.id), text)
    assert flag in result.kinds
    state = client.get("/api/v1/users/me/safety-state").json()
    assert state["allowed"] == "none" and state["hold"]["id"] != easy.id
    assert state["no_racing_or_group_until"] == (racing.isoformat() if racing else None)

    r = client.post("/api/v1/users/me/safety/mistake", json={"hold_id": state["hold"]["id"]})
    assert r.status_code == 200
    body = r.json()
    assert body["allowed"] == "easy"
    assert body["hold"]["id"] == easy.id and body["hold"]["lift_kind"] == "expires"
    assert body["no_racing_or_group_until"] == (racing.isoformat() if racing else None)
    r = client.post("/api/v1/users/me/safety/mistake", json={"hold_id": easy.id})
    assert r.status_code == 400 and "ends by itself on" in r.json()["detail"]


# === Reverify round 3, problem 8: an adult Forma has lifted stays lifted ===


def test_the_minor_rule_stays_quiet_for_180_days_after_an_admin_lift(db_session, monkeypatch):
    from app.services import plan_service

    monkeypatch.setattr(plan_service, "sync_hold_marks", lambda db, user_id, commit=True: None)
    user = _user(db_session, date_of_birth=date(1979, 5, 1))
    assert ss.minor_quiet_until(db_session, user) is None
    lifted_at = datetime(2026, 10, 8, 10, 0)
    monkeypatch.setattr(ss, "_now", lambda: lifted_at - timedelta(days=1))
    minor = ss.open_hold(db_session, user, "hold_all", "Said 16", "detector", red_flag="minor")
    # Held: nothing to be quiet about.
    assert ss.minor_quiet_until(db_session, user) is None
    monkeypatch.setattr(ss, "_now", lambda: lifted_at)
    ss.admin_lift(db_session, minor.id, "16 and 17 watts up, a 47-year-old")

    until = lifted_at + timedelta(days=ss.MINOR_QUIET_DAYS)
    assert ss.minor_quiet_until(db_session, user) == until
    assert ss.minor_quiet_until(db_session, user.id) == until
    monkeypatch.setattr(ss, "_now", lambda: until - timedelta(minutes=1))
    assert ss.minor_quiet_until(db_session, user) == until
    monkeypatch.setattr(ss, "_now", lambda: until)
    assert ss.minor_quiet_until(db_session, user) is None


def test_the_minor_quiet_never_covers_a_child_on_the_record(db_session, monkeypatch):
    from app.services import plan_service

    monkeypatch.setattr(plan_service, "sync_hold_marks", lambda db, user_id, commit=True: None)
    today = datetime.utcnow().date()
    child = _user(db_session, "child@example.com",
                  date_of_birth=date(today.year - 16, today.month, 1))
    minor = ss.open_hold(db_session, child, "hold_all", "Said 16", "detector", red_flag="minor")
    ss.admin_lift(db_session, minor.id, "Lifted in error")
    assert ss.minor_quiet_until(db_session, child) is None

    # No date of birth: the admin's decision stands.
    nobody = _user(db_session, "nodob@example.com")
    hold = ss.open_hold(db_session, nobody, "hold_all", "Said 16", "detector", red_flag="minor")
    ss.admin_lift(db_session, hold.id, "Adult, emailed in")
    assert ss.minor_quiet_until(db_session, nobody) is not None
    # Put back on hold by Forma: the account is held, not quiet.
    ss.open_hold(db_session, nobody, "hold_all", "Closed as under 18", "admin", red_flag="minor")
    assert ss.minor_quiet_until(db_session, nobody) is None
    # Only Forma's own lift starts the quiet.
    other = _user(db_session, "other@example.com")
    ss.open_hold(db_session, other, "hold_all", "Chest", "detector", red_flag="chest_pain")
    ss.admin_lift(db_session, ss.open_holds(db_session, other)[0].id, "Cardiologist letter")
    assert ss.minor_quiet_until(db_session, other) is None
    assert ss.minor_quiet_until(db_session, "no-such-user") is None


# === Reverify round 3, problem 9: no stated age means the youngest age ===


def test_a_minor_hold_with_no_event_behind_it_is_kept_as_a_13_year_olds(db_session):
    """--close-minor --force writes the hold alone: no event, no words, no
    age. It used to keep only the three years."""
    user = _user(db_session)
    closed = datetime(2026, 10, 8, 12, 0)
    db_session.add(SafetyHold(user_id=user.id, level="hold_all", reason="Closed as under 18",
                              red_flag="minor", source="admin", opened_at=closed))
    db_session.commit()
    (hold,) = ss.open_holds(db_session, user)
    assert ss.minor_retention_until(hold, deleted_at=closed) == datetime(2034, 10, 9)
    assert ss.minor_retention_end(db_session, user, deleted_at=closed) == datetime(2034, 10, 9)
    # Deleted late: three years from deletion wins.
    late = datetime(2033, 1, 1)
    assert ss.minor_retention_end(db_session, user, deleted_at=late) == datetime(2036, 1, 1)


def test_minor_retention_end_takes_the_latest_flag_and_counts_each_once(db_session):
    user = _user(db_session)
    said = datetime(2026, 10, 8, 12, 0)
    held = SafetyHold(user_id=user.id, level="hold_all", reason="Said 15", red_flag="minor",
                      source="detector", opened_at=said)
    db_session.add(held)
    db_session.commit()
    # The event says 15 and points at its hold, so the hold adds nothing.
    db_session.add(SafetyEvent(user_id=user.id, kind="minor", source="chat", matched="i'm 15",
                               stated_age=15, hold_id=held.id, created_at=said))
    db_session.commit()
    assert ss.minor_retention_end(db_session, user, deleted_at=said) == datetime(2032, 10, 9)
    # The coach's own judgement, no age: 13.
    db_session.add(SafetyEvent(user_id=user.id, kind="minor", source="coach_tool",
                               matched="Sounds like a school pupil", created_at=said))
    db_session.commit()
    assert ss.minor_retention_end(db_session, user, deleted_at=said) == datetime(2034, 10, 9)
    # Nothing under 18 on record: None, and other flags never count.
    adult = _user(db_session, "adult@example.com")
    db_session.add(SafetyEvent(user_id=adult.id, kind="chest_pain", source="chat",
                               matched="chest pain"))
    db_session.commit()
    assert ss.minor_retention_end(db_session, adult) is None
    assert ss.minor_retention_until(SafetyEvent(kind="chest_pain", created_at=said)) is None


def test_a_childs_date_of_birth_is_the_age_to_go_on_when_the_flag_gives_none(db_session):
    """No age in the flag, but the date of birth on file was a child's when
    it was raised: the records run through that 21st birthday, not the
    13-year-old's eight years. An adult date of birth is no age to go on
    (a minor who joined must have given a false one), so 13 still holds."""
    said = datetime(2026, 10, 8, 12, 0)
    child = _user(db_session, "dob16@example.com", date_of_birth=date(2010, 3, 1))
    db_session.add(SafetyEvent(user_id=child.id, kind="minor", source="coach_tool",
                               matched="Sounds like a school pupil", created_at=said))
    db_session.commit()
    assert ss.minor_retention_end(db_session, child, deleted_at=said) == datetime(2031, 3, 2)
    assert ss.minor_retention_end(db_session, child.id, deleted_at=said) == datetime(2031, 3, 2)
    # An age the rider gave still wins over the date of birth.
    db_session.add(SafetyEvent(user_id=child.id, kind="minor", source="chat", matched="i'm 14",
                               stated_age=14, created_at=said))
    db_session.commit()
    assert ss.minor_retention_end(db_session, child, deleted_at=said) == datetime(2033, 10, 9)

    adult = _user(db_session, "dob47@example.com", date_of_birth=date(1979, 5, 1))
    db_session.add(SafetyEvent(user_id=adult.id, kind="minor", source="coach_tool",
                               matched="Sounds like a school pupil", created_at=said))
    db_session.commit()
    assert ss.minor_retention_end(db_session, adult, deleted_at=said) == datetime(2034, 10, 9)


# === Reverify round 3, problem 10: an under-18 account, and the re-screen ===


def test_nothing_the_rider_does_lifts_a_hold_while_held_as_under_18(db_session):
    user = _user(db_session)
    head = _head(db_session, user)
    chest = ss.open_hold(db_session, user, "hold_all", "Chest", "detector", red_flag="chest_pain")
    fever = _fever(db_session, user)
    easy = ss.lift_fever(db_session, user, fever.id)
    minor = ss.open_hold(db_session, user, "hold_all", "Said 15", "detector", red_flag="minor")
    for hold in (head, chest, minor):
        with pytest.raises(ss.HoldNotLiftable) as refused:
            ss.lift_hold(db_session, user, hold.id, "mistake")
        assert refused.value.hold.id == minor.id
    for how in ("mistake", "clearance", "fever_self", "head_clearance"):
        assert ss.lift_holds(db_session, user, how) == 0
    # A second tap on the fever lift is refused too, not waved through.
    with pytest.raises(ss.HoldNotLiftable):
        ss.lift_fever(db_session, user, fever.id)
    assert {h.id for h in ss.open_holds(db_session, user)} == {head.id, chest.id, easy.id, minor.id}
    # Forma's own lifts still work.
    assert ss.lift_hold(db_session, user, chest.id, "admin").lifted_how == "admin"


def test_mistake_endpoint_refuses_everything_while_held_as_under_18(api):
    client, db, user = api
    head = _head(db, user)
    chest = ss.open_hold(db, user.id, "hold_all", "Chest", "detector", red_flag="chest_pain")
    ss.open_hold(db, user.id, "hold_all", "Said 15", "detector", red_flag="minor")
    for hold in (head, chest):
        r = client.post("/api/v1/users/me/safety/mistake", json={"hold_id": hold.id})
        assert r.status_code == 400
        assert "Forma is for adults, 18 and over" in r.json()["detail"]
        db.refresh(hold)
        assert hold.lifted_at is None
    # Even a hold that isn't theirs gets the same answer: nothing leaks.
    r = client.post("/api/v1/users/me/safety/mistake", json={"hold_id": "not-a-hold"})
    assert r.status_code == 400
    state = client.get("/api/v1/users/me/safety-state").json()
    assert state["allowed"] == "none" and state["hold"]["red_flag"] == "minor"


@pytest.mark.parametrize("how", ["doctor", "head", "fever"])
def test_after_a_clearance_the_rescreen_falls_due_on_the_yearly_cadence(
    db_session, monkeypatch, how
):
    """Reverify round 4: the re-screen stayed shut for 30 days after a GP
    cleared chest pain, then opened on day 31, and a truthful yes put a full
    hold back on. After a clinician's clearance or the head check, the
    yearly re-screen now counts from the clearance; a fever gone on the
    rider's own word leaves it a year after the answers."""
    user = _user(db_session)
    answered = _screening(db_session, user, days_ago=40).created_at
    assert ss.rescreen_quiet_until(db_session, user) is None
    cleared = datetime.combine(TODAY, time(10, 0))
    monkeypatch.setattr(ss, "_now", lambda: cleared)
    if how == "doctor":
        ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
        ss.confirm_clearance(db_session, user, "My GP", None)
    elif how == "head":
        ss.lift_head_injury(db_session, user, _head(db_session, user).id, "My GP")
    else:
        ss.lift_fever(db_session, user, _fever(db_session, user).id)

    assert ss.RESCREEN_AFTER_DAYS == ss.RESCREEN_QUIET_DAYS == 365
    assert ss.latest_clearance_at(db_session, user) == cleared
    # Red flags count from the clearance whichever kind it was.
    assert ss.rescreen_red_flags_after(
        db_session, user, ss.latest_screening(db_session, user.id)
    ) == cleared
    if how == "fever":
        assert ss.latest_clearance_at(db_session, user, clinician_only=True) is None
        assert ss.rescreen_quiet_until(db_session, user) is None
        return
    until = cleared + timedelta(days=365)
    assert until > answered + timedelta(days=365)
    assert ss.rescreen_quiet_until(db_session, user) == until
    for day in (31, 100, 364):
        monkeypatch.setattr(ss, "_now", lambda day=day: cleared + timedelta(days=day))
        assert ss.rescreen_quiet_until(db_session, user.id) == until, day
    monkeypatch.setattr(ss, "_now", lambda: until - timedelta(minutes=1))
    assert ss.rescreen_quiet_until(db_session, user) == until
    monkeypatch.setattr(ss, "_now", lambda: until)
    assert ss.rescreen_quiet_until(db_session, user) is None


def test_new_questions_and_a_first_screening_are_never_held_back(db_session):
    user = _user(db_session)
    ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
    ss.confirm_clearance(db_session, user, "My GP", None)
    # Never answered the questions: they are asked.
    assert ss.rescreen_quiet_until(db_session, user) is None
    # Answered an older version of them: the new ones are asked.
    old = _screening(db_session, user)
    old.version = "screen-v0"
    db_session.commit()
    assert ss.rescreen_quiet_until(db_session, user) is None
    # The current version: held back.
    old.superseded_at = datetime.utcnow()
    db_session.commit()
    _screening(db_session, user)
    assert ss.rescreen_quiet_until(db_session, user) is not None


# === Reverify round 4, new problem A: a second head injury in the build-back ===
#
# A genuinely new crash during the easy fortnight ("crashed again today and
# hit my head on the kerb") was read as the first injury mentioned again: the
# easy riding still ended two weeks after the first injury and racing waited
# only for day 21 of it. A hit the detector marks as new (Hit.new_event) is a
# second injury: its own hold, and both clocks restart from its day.


class _NewHit:
    """Stands in for the detector's Hit with new_event set."""

    def __init__(self, new_event=True):
        self.kind = "head_injury"
        self.matched = "crashed again today and hit my head"
        self.severity = "emergency"
        self.new_event = new_event


class _OldHit:
    """A Hit from before the field existed: no new_event at all."""

    kind = "head_injury"
    matched = "hit my head"
    severity = "emergency"


def _new_head(db, user, **kw):
    kw.setdefault("new_event", True)
    return ss.open_hold(
        db, user, "hold_all", "Hit their head in a crash", "detector",
        red_flag="head_injury", note='Matched "crashed again"', **kw,
    )


def test_a_second_head_injury_in_the_build_back_restarts_both_clocks(db_session, monkeypatch):
    """The reverify's numbers: injured 8 October, checked the next day, so
    easy riding to 22 October and no racing before 29 October. A new crash
    on day 12 (20 October) gives easy riding to 3 November and no racing
    before 10 November."""
    user = _user(db_session)
    injured = datetime(2026, 10, 8, 9, 0)
    monkeypatch.setattr(ss, "_now", lambda: injured)
    first = _head(db_session, user)
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=1))
    easy = ss.lift_head_injury(db_session, user, first.id, "My GP")
    assert easy.expires_at == datetime(2026, 10, 22, 9, 0)
    assert ss.no_racing_or_group_until(db_session, user) == date(2026, 10, 29)

    crashed = injured + timedelta(days=12)
    monkeypatch.setattr(ss, "_now", lambda: crashed)
    second = _new_head(db_session, user)
    db_session.refresh(easy)
    # Its own hold, beside the first injury's easy riding, and never a
    # re-mention of it.
    assert second.id not in (first.id, easy.id)
    assert easy.lifted_at is None
    assert ss.is_new_event(second) and ss.remention_of(db_session, second) is None
    assert second.note.startswith('Matched "crashed again"\n' + ss.NEW_EVENT_MARK)
    assert "A second head injury" in second.note
    assert "The same injury" not in second.note
    assert ss.allowed_intensity(db_session, user) == "none"
    assert ss.no_racing_or_group_until(db_session, user) == date(2026, 11, 10)

    # Checked by a doctor the next day: easy riding runs two weeks from the
    # second injury, and the first injury's easy riding gives way to it.
    monkeypatch.setattr(ss, "_now", lambda: crashed + timedelta(days=1))
    after = ss.lift_head_injury(db_session, user, second.id, "My GP")
    db_session.refresh(easy)
    assert after.id != easy.id
    assert after.expires_at == datetime(2026, 11, 3, 9, 0)
    assert easy.lifted_how == "superseded"
    assert [h.id for h in ss.open_holds(db_session, user)] == [after.id]
    assert ss.no_racing_or_group_until(db_session, user) == date(2026, 11, 10)
    for on in (date(2026, 10, 22), date(2026, 10, 30), date(2026, 11, 2)):
        assert ss.allowed_intensity(db_session, user, on_date=on) == "easy", on
    state = ss.safety_state(db_session, user)
    assert state["no_racing_or_group_until"] == "2026-11-10"
    assert state["hold"]["id"] == after.id


def test_a_plain_remention_still_keeps_the_first_clock(db_session, monkeypatch):
    """Only a hit marked new restarts anything: a re-mention, and a Hit with
    no new_event field at all, keep day 21 of the first injury."""
    user = _user(db_session)
    injured = datetime(2026, 10, 8, 9, 0)
    monkeypatch.setattr(ss, "_now", lambda: injured)
    first = _head(db_session, user)
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=1))
    easy = ss.lift_head_injury(db_session, user, first.id, "My GP")
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=12))
    again = ss.open_hold(db_session, user, "hold_all", "Head", "detector",
                         red_flag="head_injury", hit=_OldHit())
    assert ss.remention_of(db_session, again).id == easy.id
    assert not ss.is_new_event(again)
    assert "The same injury, so racing waits for day 21" in again.note
    assert ss.no_racing_or_group_until(db_session, user) == date(2026, 10, 29)
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=13))
    after = ss.lift_head_injury(db_session, user, again.id, "My GP")
    assert after.id == easy.id and after.expires_at == datetime(2026, 10, 22, 9, 0)


@pytest.mark.parametrize("hit, new", [
    (_NewHit(), True),
    (_NewHit(new_event=False), False),
    (_OldHit(), False),
    # Read defensively: only a real True counts, so a stand-in object or a
    # string never restarts a clock by accident.
    (_NewHit(new_event="yes"), False),
    (None, False),
])
def test_new_event_is_read_from_the_hit_defensively(db_session, monkeypatch, hit, new):
    user = _user(db_session)
    injured = datetime(2026, 10, 8, 9, 0)
    monkeypatch.setattr(ss, "_now", lambda: injured)
    easy = ss.lift_head_injury(db_session, user, _head(db_session, user).id, "My GP")
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=5))
    hold = ss.open_hold(db_session, user, "hold_all", "Head", "detector",
                        red_flag="head_injury", hit=hit)
    assert ss.is_new_event(hold) is new
    assert (ss.remention_of(db_session, hold) is None) is new
    racing = (injured + timedelta(days=5 if new else 0)).date() + timedelta(days=21)
    assert ss.no_racing_or_group_until(db_session, user) == racing
    db_session.refresh(easy)
    assert easy.lifted_at is None


def test_the_second_injury_marked_a_mistake_leaves_the_first_clock(db_session, monkeypatch):
    user = _user(db_session)
    injured = datetime(2026, 10, 8, 9, 0)
    monkeypatch.setattr(ss, "_now", lambda: injured)
    first = _head(db_session, user)
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=1))
    easy = ss.lift_head_injury(db_session, user, first.id, "My GP")
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=12))
    second = _new_head(db_session, user)
    ss.lift_hold(db_session, user, second.id, "mistake", note="Rider marked it a mistake.")
    db_session.refresh(easy)
    assert easy.lifted_at is None and easy.expires_at == datetime(2026, 10, 22, 9, 0)
    assert [h.id for h in ss.open_holds(db_session, user)] == [easy.id]
    assert ss.allowed_intensity(db_session, user) == "easy"
    assert ss.no_racing_or_group_until(db_session, user) == date(2026, 10, 29)


def test_a_second_injury_never_folds_into_a_remention_or_supersedes_it(db_session, monkeypatch):
    """A re-mention on day 3 is still open when the rider crashes again on
    day 5: the crash gets its own hold, the re-mention stays, and marking
    the crash a mistake leaves the re-mention holding."""
    user = _user(db_session)
    injured = datetime(2026, 10, 8, 9, 0)
    monkeypatch.setattr(ss, "_now", lambda: injured)
    easy = ss.lift_head_injury(db_session, user, _head(db_session, user).id, "My GP")
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=3))
    again = _head(db_session, user)
    assert ss.remention_of(db_session, again).id == easy.id
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=5))
    second = _new_head(db_session, user)
    db_session.refresh(again)
    assert second.id != again.id and again.lifted_at is None
    assert ss.no_racing_or_group_until(db_session, user) == date(2026, 11, 3)

    # Later the same day, the rider says more about it: that is the same new
    # injury, not a third one.
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=5, hours=6))
    assert _new_head(db_session, user).id == second.id
    assert _head(db_session, user).id in (again.id, second.id)

    ss.lift_hold(db_session, user, second.id, "mistake")
    db_session.refresh(again)
    assert again.lifted_at is None
    assert ss.allowed_intensity(db_session, user) == "none"
    assert ss.no_racing_or_group_until(db_session, user) == date(2026, 10, 29)


def test_a_second_injury_before_the_first_is_checked_gets_its_own_hold(db_session, monkeypatch):
    """Crashed again on day 5 while the first injury is still on hold: the
    head check lifts both, with easy riding to day 19 and no racing before
    day 26. Marking the second a mistake never lifts the first."""
    user = _user(db_session)
    injured = datetime(2026, 10, 8, 9, 0)
    monkeypatch.setattr(ss, "_now", lambda: injured)
    first = _head(db_session, user)
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=5))
    second = _new_head(db_session, user)
    db_session.refresh(first)
    assert second.id != first.id and first.lifted_at is None
    assert "A second head injury" in second.note
    assert ss.no_racing_or_group_until(db_session, user) == date(2026, 11, 3)

    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=6))
    easy = ss.lift_head_injury(db_session, user, first.id, "My GP")
    db_session.refresh(second)
    assert second.lifted_how == "head_clearance"
    assert easy.expires_at == injured + timedelta(days=5 + ss.HEAD_EASY_DAYS)
    assert ss.no_racing_or_group_until(db_session, user) == date(2026, 11, 3)


def test_a_second_injury_marked_a_mistake_before_the_check_leaves_the_first_held(
    db_session, monkeypatch
):
    user = _user(db_session)
    injured = datetime(2026, 10, 8, 9, 0)
    monkeypatch.setattr(ss, "_now", lambda: injured)
    first = _head(db_session, user)
    monkeypatch.setattr(ss, "_now", lambda: injured + timedelta(days=5))
    second = _new_head(db_session, user)
    ss.lift_hold(db_session, user, second.id, "mistake")
    db_session.refresh(first)
    assert first.lifted_at is None
    assert [h.id for h in ss.open_holds(db_session, user)] == [first.id]
    assert ss.allowed_intensity(db_session, user) == "none"
    assert ss.no_racing_or_group_until(db_session, user) == date(2026, 10, 29)


def test_a_first_head_injury_read_as_new_is_not_called_a_second(db_session):
    user = _user(db_session)
    hold = _new_head(db_session, user)
    assert hold.note == 'Matched "crashed again"\n' + ss.NEW_EVENT_MARK
    assert ss.no_racing_or_group_until(db_session, user) == TODAY + timedelta(days=21)


def test_a_new_fever_in_the_easy_week_sits_beside_it(db_session, monkeypatch):
    user = _user(db_session)
    start = datetime(2026, 10, 8, 9, 0)
    monkeypatch.setattr(ss, "_now", lambda: start)
    easy = ss.lift_fever(db_session, user, _fever(db_session, user).id)
    monkeypatch.setattr(ss, "_now", lambda: start + timedelta(days=3))
    again = ss.open_hold(db_session, user, "hold_all", "Fever", "detector",
                         red_flag="fever", new_event=True)
    db_session.refresh(easy)
    assert easy.lifted_at is None and ss.remention_of(db_session, again) is None
    assert again.note.startswith(ss.NEW_EVENT_MARK)
    assert f"easy riding of hold {easy.id}" in again.note
    assert "head injury" not in again.note


def test_the_new_event_note_reads_cleanly():
    import re

    for text in (
        ss.NEW_EVENT_MARK,
        "A second head injury, so the two easy weeks and the 21 days before racing "
        "or group riding count from today.",
    ):
        assert not re.search("[–—!]", text)
        assert "kicker" not in text.lower()
