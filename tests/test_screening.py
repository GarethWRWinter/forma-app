"""The health screening: the tier each answer produces, what the rider is
told, the records it leaves (screening, hold, consent), re-screening, and
the two endpoints."""

import re
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.safety import ConsentEvent, HealthScreening, SafetyHold
from app.models.training import Workout
from app.models.user import User
from app.services import onboarding_service as ob
from app.services import plan_service
from app.services import safety_service as ss

QUESTIONS = [f"q{i}" for i in range(1, 9)]


def _answers(*yes: str) -> dict[str, bool]:
    return {q: q in yes for q in QUESTIONS}


def _user(db, email="rider@example.com") -> User:
    user = User(email=email, hashed_password="x", ftp=250, weekly_hours_available=6.0)
    db.add(user)
    db.commit()
    return user


class _Req:
    def __init__(self, headers: dict):
        self.headers = headers
        self.client = type("C", (), {"host": "10.0.0.1"})()


# === The tier ===


def test_all_no_is_tier_none():
    assert ob.screening_tier(_answers()) == "none"


@pytest.mark.parametrize("q", ["q2", "q3"])
def test_chest_pain_or_fainting_holds_everything(q):
    assert ob.screening_tier(_answers(q)) == "hold_all"


@pytest.mark.parametrize("q", ["q1", "q4", "q5", "q6", "q7", "q8"])
def test_any_other_yes_is_easy_only(q):
    assert ob.screening_tier(_answers(q)) == "easy_only"


def test_hold_all_wins_over_easy_only():
    assert ob.screening_tier(_answers("q1", "q5", "q3")) == "hold_all"


# === What the rider reads ===


# Section C of the safety plan, word for word.
AGREED = {
    "none": "Thanks. If anything changes, tell me or update this in Settings, then Health.",
    "hold_all": (
        "Thank you for telling me. Chest pain or fainting needs a doctor's view before you "
        "ride, even gently. Please see your GP soon, and if it's new or getting worse, call "
        "NHS 111 today. If you have chest pain now, or it lasts more than a few minutes, "
        "call 999. Your plan is built, but every session stays on hold until you tell me a "
        "doctor has cleared you."
    ),
    "easy_only": (
        "Thank you. Please check with your GP, or the doctor, physio or midwife who knows "
        "your situation, that structured training with some hard efforts is right for you. "
        "Until you tell me you've been cleared, I'll keep your plan to easy and steady "
        "riding, with no hard intervals and no fitness tests."
    ),
}
AGREED_EXTRA = [
    "If your medicine changes your heart rate, as beta blockers do, heart-rate zones won't "
    "be accurate for you, so I'll coach by power and feel. Never change or skip medicine "
    "to train.",
    "For an injury, a physiotherapist is the right first stop. In many parts of the UK you "
    "can refer yourself to NHS physiotherapy.",
    "Your midwife or obstetrician should decide how hard you train. Until then I'll keep "
    "things easy, and riding indoors is safer as pregnancy goes on.",
]


@pytest.mark.parametrize("country", [None, "GB", "gb"])
def test_messages_are_the_agreed_wording_in_the_uk(country):
    for tier, yes in (("none", ()), ("hold_all", ("q2",)), ("easy_only", ("q1",))):
        assert ob.screening_feedback(tier, _answers(*yes), country)[0] == AGREED[tier]
    _, extra = ob.screening_feedback("easy_only", _answers("q5", "q6", "q7"), country)
    assert extra == AGREED_EXTRA


def test_outside_the_uk_the_numbers_are_local():
    message, _ = ob.screening_feedback("hold_all", _answers("q2"), "IE")
    assert "call 112." in message and "999" not in message
    assert "NHS" not in message and "GP" not in message
    assert "out-of-hours service" in message
    _, extra = ob.screening_feedback("easy_only", _answers("q6"), "FR")
    assert extra == ["For an injury, a physiotherapist is the right first stop."]
    assert ob.emergency_number("US") == "911" and ob.emergency_number(None) == "999"


def test_extra_lines_follow_q5_q6_q7_in_order():
    _, extra = ob.screening_feedback("easy_only", _answers("q7", "q5", "q6", "q1"))
    assert [line.split(" ")[0] for line in extra] == ["If", "For", "Your"]
    assert "beta blockers" in extra[0] and "Never change or skip medicine" in extra[0]
    assert "NHS physiotherapy" in extra[1]
    assert "midwife or obstetrician" in extra[2]
    assert ob.screening_feedback("easy_only", _answers("q1", "q4"))[1] == []


def test_extra_lines_stay_off_a_full_hold():
    # Under hold_all the one thing that matters is seeing a doctor, and the
    # Q7 line ("I'll keep things easy") would contradict the hold.
    assert ob.screening_feedback("hold_all", _answers("q2", "q5", "q7"))[1] == []


def test_customer_text_has_no_dashes_or_exclamations():
    text = " ".join(
        [ob.SCREENING_TEXT, ob.LONG_BREAK_QUESTION, plan_service.EASY_PLAN_FOCUS]
        + list(plan_service.HOLD_LABELS)
        + list(ob.SCREENING_MESSAGES.values())
        + list(ob.SCREENING_EXTRA_LINES.values())
    )
    assert not set(text) & {"–", "—", "!"}
    assert "kicker" not in text.lower()
    assert not re.search(r"\bsafe\b|injury-proof|prevents? injur", text.lower())


def test_consent_text_holds_every_question_verbatim():
    assert ob.SCREENING_TEXT.startswith("A few health questions first\nEight yes or no questions.")
    assert "such as chest pain, call 999." in ob.SCREENING_TEXT
    for i, q in enumerate(QUESTIONS, start=1):
        assert f"{i}. {ob.SCREENING_QUESTIONS[q]}" in ob.SCREENING_TEXT


def test_consent_text_matches_what_an_eu_rider_was_shown(db_session):
    user = _user(db_session)
    user.country = "IE"
    db_session.commit()
    result = ob.submit_screening(db_session, user, _answers("q3"), long_break=False)
    assert "call 112." in result["message"]
    row = db_session.query(ConsentEvent).one()
    assert "such as chest pain, call 112." in row.text_shown
    assert ob.get_screening(db_session, user)["intro"].endswith("call 112.")


# === The records ===


def test_all_no_stores_the_answers_and_opens_no_hold(db_session):
    user = _user(db_session)
    result = ob.submit_screening(db_session, user, _answers(), long_break=False)
    assert result["tier"] == "none" and result["extra_lines"] == []
    assert result["safety"]["allowed"] == "all"
    row = db_session.query(HealthScreening).one()
    assert (row.version, row.tier, row.any_yes, row.long_break) == (
        "screen-v1", "none", False, False
    )
    assert row.answers == _answers()
    assert db_session.query(SafetyHold).count() == 0


def test_easy_only_opens_a_screening_hold(db_session):
    user = _user(db_session)
    result = ob.submit_screening(db_session, user, _answers("q1", "q6"), long_break=False)
    assert result["tier"] == "easy_only" and result["safety"]["allowed"] == "easy"
    assert len(result["extra_lines"]) == 1
    hold = db_session.query(SafetyHold).one()
    assert (hold.level, hold.source, hold.red_flag) == ("easy_only", "screening", "heart_condition")
    assert hold.reason == (
        "Health answers: heart condition or high blood pressure; injury, surgery or concussion"
    )
    assert "q1, q6" in hold.note
    assert result["safety"]["hold"]["id"] == hold.id


def test_hold_all_opens_a_full_hold(db_session):
    user = _user(db_session)
    result = ob.submit_screening(db_session, user, _answers("q3", "q1"), long_break=False)
    assert result["tier"] == "hold_all" and result["safety"]["allowed"] == "none"
    hold = ss.current_hold(db_session, user.id)
    assert (hold.level, hold.red_flag, hold.reason) == (
        "hold_all", "fainting", "Health answers: fainting or dizziness"
    )


def test_every_reason_fits_the_column(db_session):
    reason, _ = ob._hold_reason("easy_only", _answers(*QUESTIONS))
    assert len(reason) <= 200


def test_a_consent_row_records_the_question_set(db_session):
    user = _user(db_session)
    req = _Req({"x-forwarded-for": "1.1.1.1, 81.2.69.160", "user-agent": "Forma/1"})
    ob.submit_screening(db_session, user, _answers(), long_break=False, request=req)
    ob.submit_screening(db_session, user, _answers("q5"), long_break=False, request=req)
    rows = db_session.query(ConsentEvent).order_by(ConsentEvent.accepted_at).all()
    assert [(r.kind, r.doc_version) for r in rows] == [("screening", "screen-v1")] * 2
    assert rows[0].text_shown == ob.SCREENING_TEXT
    assert (rows[0].ip, rows[0].user_agent) == ("81.2.69.160", "Forma/1")
    # The first answers come from onboarding; later ones from Settings.
    assert [r.source for r in rows] == ["onboarding", "app"]


def test_missing_answers_are_refused(db_session):
    user = _user(db_session)
    answers = _answers()
    del answers["q4"]
    with pytest.raises(ValueError, match="q4"):
        ob.submit_screening(db_session, user, answers, long_break=False)
    assert db_session.query(HealthScreening).count() == 0


# === Re-screening ===


def test_new_answers_supersede_the_old_ones(db_session):
    user = _user(db_session)
    ob.submit_screening(db_session, user, _answers("q1"), long_break=False)
    ob.submit_screening(db_session, user, _answers("q6"), long_break=False)
    rows = db_session.query(HealthScreening).order_by(HealthScreening.created_at).all()
    assert len(rows) == 2
    assert rows[0].superseded_at is not None and rows[1].superseded_at is None
    assert ss.latest_screening(db_session, user.id).answers["q6"] is True


def test_new_answers_replace_the_screening_hold(db_session):
    user = _user(db_session)
    ob.submit_screening(db_session, user, _answers("q2"), long_break=False)
    first = ss.current_hold(db_session, user.id)
    result = ob.submit_screening(db_session, user, _answers(), long_break=False)
    db_session.refresh(first)
    assert first.lifted_how == "superseded"
    assert result["safety"]["allowed"] == "all" and result["safety"]["hold"] is None


def test_a_red_flag_hold_survives_new_answers(db_session):
    user = _user(db_session)
    detector = ss.open_hold(db_session, user, "hold_all", "Chest pain in chat", "detector",
                            red_flag="chest_pain")
    result = ob.submit_screening(db_session, user, _answers(), long_break=False)
    db_session.refresh(detector)
    assert detector.lifted_at is None
    assert result["safety"]["allowed"] == "none"


def test_a_clearance_does_not_carry_over_to_new_answers(db_session):
    user = _user(db_session)
    ob.submit_screening(db_session, user, _answers("q1"), long_break=False)
    ss.confirm_clearance(db_session, user, "My GP", None)
    assert ss.allowed_intensity(db_session, user) == "all"
    result = ob.submit_screening(db_session, user, _answers("q1"), long_break=False)
    assert result["safety"]["allowed"] == "easy"
    assert result["safety"]["screening"]["clearance_confirmed"] is False


def test_a_long_break_gates_hard_sessions_for_two_weeks(db_session):
    user = _user(db_session)
    result = ob.submit_screening(db_session, user, _answers(), long_break=True)
    assert result["safety"]["allowed"] == "easy"
    assert result["safety"]["layoff_gate_until"] == (
        datetime.utcnow().date() + timedelta(days=14)
    ).isoformat()


def test_answers_put_the_plan_on_hold_and_take_it_off(db_session):
    user = _user(db_session)
    plan_service.generate_plan(db_session, user)
    future = lambda: [  # noqa: E731
        w for w in db_session.query(Workout).all() if w.scheduled_date >= date.today()
    ]
    assert not any(plan_service.is_held(w) for w in future())

    ob.submit_screening(db_session, user, _answers("q2"), long_break=False)
    assert future() and all(plan_service.is_held(w) for w in future())

    ob.submit_screening(db_session, user, _answers(), long_break=False)
    assert not any(plan_service.is_held(w) for w in db_session.query(Workout).all())


def test_new_answers_that_only_ease_the_plan_keep_hard_sessions_on_hold(db_session):
    """Review finding 17: a plan built under a full hold keeps its hard
    sessions. When new answers drop the full hold to easy riding only, those
    sessions keep the label and the easy ones lose it."""
    user = _user(db_session)
    ob.submit_screening(db_session, user, _answers("q2"), long_break=False)
    plan_service.generate_plan(db_session, user)
    future = [w for w in db_session.query(Workout).all() if w.scheduled_date >= date.today()]
    assert future and all(plan_service.is_held(w) for w in future)

    result = ob.submit_screening(db_session, user, _answers("q6"), long_break=False)
    assert result["safety"]["allowed"] == "easy"
    hard = [w for w in future if not ss.workout_allowed(w, "easy")]
    assert hard
    for w in future:
        assert plan_service.is_held(w) == (w in hard)
        if w in hard:
            assert w.description.startswith(plan_service.HOLD_PREFIX)


# === Reading the answers back ===


def test_get_screening_before_any_answers(db_session):
    user = _user(db_session)
    record = ob.get_screening(db_session, user)
    assert record["answers"] is None and record["rescreen_due"] is True
    assert [q["id"] for q in record["questions"]] == QUESTIONS
    assert record["clearance_text"] == ss.CLEARANCE_TEXT


def test_get_screening_returns_the_latest_answers(db_session):
    user = _user(db_session)
    ob.submit_screening(db_session, user, _answers("q7"), long_break=True)
    record = ob.get_screening(db_session, user)
    assert record["answers"] == _answers("q7")
    assert (record["tier"], record["long_break"], record["rescreen_due"]) == (
        "easy_only", True, False
    )


def test_rescreen_is_due_after_a_year_or_a_red_flag(db_session):
    user = _user(db_session)
    ob.submit_screening(db_session, user, _answers(), long_break=False)
    row = ss.latest_screening(db_session, user.id)
    row.created_at = datetime.utcnow() - timedelta(days=366)
    db_session.commit()
    assert ob.get_screening(db_session, user)["rescreen_due"] is True

    row.created_at = datetime.utcnow() - timedelta(days=10)
    db_session.commit()
    assert ob.get_screening(db_session, user)["rescreen_due"] is False
    mistake = ss.open_hold(db_session, user, "hold_all", "My chest strap died", "detector")
    ss.lift_hold(db_session, user, mistake.id, "mistake")
    assert ob.get_screening(db_session, user)["rescreen_due"] is False
    ss.open_hold(db_session, user, "hold_all", "Fainted on a climb", "detector")
    assert ob.get_screening(db_session, user)["rescreen_due"] is True


# === An account held as under 18 ===


def _minor(db, user):
    """The under-18 hold the detector opens, as it opens it."""
    return ss.open_hold(
        db, user, "hold_all", "The rider said they are under 18", "detector",
        red_flag="minor",
    )


def _counts(db, user) -> tuple[int, int, int]:
    return (
        db.query(HealthScreening).filter(HealthScreening.user_id == user.id).count(),
        db.query(ConsentEvent).filter(ConsentEvent.user_id == user.id).count(),
        db.query(SafetyHold).filter(SafetyHold.user_id == user.id).count(),
    )


def test_a_minor_hold_is_never_asked_the_health_questions(db_session):
    """New problem 3: the minor hold counted as a red flag in chat, so the
    blocking re-screen prompt asked a child the health questions."""
    never = _user(db_session, "never@example.com")
    _minor(db_session, never)
    assert ob.get_screening(db_session, never)["rescreen_due"] is False

    answered = _user(db_session, "answered@example.com")
    ob.submit_screening(db_session, answered, _answers(), long_break=False)
    _minor(db_session, answered)
    assert ob.get_screening(db_session, answered)["rescreen_due"] is False
    # Not even when the answers are a year old.
    row = ss.latest_screening(db_session, answered.id)
    row.created_at = datetime.utcnow() - timedelta(days=400)
    db_session.commit()
    assert ob.get_screening(db_session, answered)["rescreen_due"] is False


def test_a_minor_hold_refuses_the_answers_and_stores_nothing(db_session):
    user = _user(db_session)
    _minor(db_session, user)
    before = _counts(db_session, user)
    with pytest.raises(ob.ScreeningRefused) as refused:
        ob.submit_screening(db_session, user, _answers("q7"), long_break=True)
    assert str(refused.value) == ob.MINOR_SCREENING_REFUSAL
    db_session.rollback()
    assert _counts(db_session, user) == before == (0, 0, 1)
    assert ss.minor_hold(db_session, user) is not None


def test_minor_refusal_wording():
    text = ob.MINOR_SCREENING_REFUSAL
    assert "18 and over" in text and "gareth@ridewithforma.com" in text
    assert not re.search("[\u2013\u2014!]", text)
    assert "kicker" not in text.lower()


def test_lifting_a_wrong_minor_hold_does_not_force_a_rescreen(db_session):
    """An adult read as a minor, once Forma lifts the hold, keeps their
    answers: an age flag says nothing about their health."""
    user = _user(db_session)
    ob.submit_screening(db_session, user, _answers(), long_break=False)
    hold = _minor(db_session, user)
    ss.lift_hold(db_session, user, hold.id, "admin", note="Adult, misread.")
    assert ob.get_screening(db_session, user)["rescreen_due"] is False
    result = ob.submit_screening(db_session, user, _answers("q1"), long_break=False)
    assert result["tier"] == "easy_only"


# === Endpoints ===


@pytest.fixture
def api():
    """The onboarding endpoints over a private SQLite database, signed in as
    one rider."""
    from app.api.v1.deps import get_current_user
    from app.database import get_db
    from app.main import app

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = _user(db, "api@example.com")
    user.health_consent_at = datetime.utcnow()
    db.commit()

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, user.id)
    try:
        yield TestClient(app), db, user
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        db.close()


def test_post_screening_endpoint(api):
    client, db, user = api
    r = client.post(
        "/api/v1/onboarding/screening",
        json={"answers": _answers("q5"), "long_break": False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "easy_only" and body["message"].startswith("Thank you. Please check")
    assert body["extra_lines"] == [ob.SCREENING_EXTRA_LINES["q5"]]
    assert body["safety"]["allowed"] == "easy" and body["safety"]["hold"]["source"] == "screening"


def test_post_screening_needs_every_answer_and_the_break_question(api):
    client, db, user = api
    partial = _answers()
    del partial["q8"]
    assert client.post(
        "/api/v1/onboarding/screening", json={"answers": partial, "long_break": False}
    ).status_code == 422
    assert client.post(
        "/api/v1/onboarding/screening", json={"answers": _answers()}
    ).status_code == 422
    assert client.post(
        "/api/v1/onboarding/screening",
        json={"answers": {**_answers(), "q9": True}, "long_break": False},
    ).status_code == 422
    assert db.query(HealthScreening).count() == 0


def test_get_screening_endpoint(api):
    client, db, user = api
    r = client.get("/api/v1/onboarding/screening")
    assert r.status_code == 200 and r.json()["answers"] is None
    client.post(
        "/api/v1/onboarding/screening", json={"answers": _answers("q2"), "long_break": False}
    )
    body = client.get("/api/v1/onboarding/screening").json()
    assert body["answers"]["q2"] is True and body["tier"] == "hold_all"
    assert body["version"] == "screen-v1" and len(body["questions"]) == 8


def test_post_screening_refused_with_403_under_a_minor_hold(api):
    client, db, user = api
    _minor(db, user)
    before = _counts(db, user)
    r = client.post(
        "/api/v1/onboarding/screening",
        json={"answers": _answers("q2", "q7"), "long_break": True},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == ob.MINOR_SCREENING_REFUSAL
    assert _counts(db, user) == before
    assert db.query(ConsentEvent).filter(ConsentEvent.kind == "screening").count() == 0
    assert client.get("/api/v1/onboarding/screening").json()["rescreen_due"] is False


def test_post_screening_403_comes_before_the_health_consent_check(api):
    client, db, user = api
    user.health_consent_at = None
    db.commit()
    _minor(db, user)
    r = client.post(
        "/api/v1/onboarding/screening", json={"answers": _answers(), "long_break": False}
    )
    assert r.status_code == 403 and r.json()["detail"] == ob.MINOR_SCREENING_REFUSAL
    assert db.query(HealthScreening).count() == 0


def test_health_consent_refused_with_403_under_a_minor_hold(api):
    """Box 2 is health data too: an account held as under 18 can't give it,
    and nothing is recorded."""
    from app.api.v1 import auth

    client, db, user = api
    user.health_consent_at = None
    db.commit()
    _minor(db, user)
    r = client.post("/api/v1/auth/health-consent", json={"text_shown": "I agree"})
    assert r.status_code == 403
    assert r.json()["detail"] == auth.MINOR_HEALTH_CONSENT_REFUSAL
    db.refresh(user)
    assert user.health_consent_at is None
    assert db.query(ConsentEvent).filter(ConsentEvent.kind == "health_data").count() == 0


# === Reverify round 4: after a clearance, the yearly cadence, not 30 days ===
#
# The re-screen stayed shut for 30 days after a GP cleared chest pain, then
# opened on day 31, and answering q2 truthfully put a full hold back on
# until a second clearance. A clearance now closes the red flags before it,
# and after a clinician's clearance or the head check the yearly re-screen
# counts from the clearance.


def _answered_days_ago(db, user, days: int) -> datetime:
    ob.submit_screening(db, user, _answers(), long_break=False)
    row = ss.latest_screening(db, user.id)
    row.created_at = datetime.utcnow() - timedelta(days=days)
    db.commit()
    return row.created_at


def _clear(db, user, how: str) -> None:
    if how == "doctor":
        ss.open_hold(db, user, "hold_all", "Chest pain on the climb", "detector",
                     red_flag="chest_pain")
        ss.confirm_clearance(db, user, "My GP", None)
    elif how == "physio":
        ss.open_hold(db, user, "easy_only", "Sore knee", "detector", red_flag="injury")
        ss.confirm_clearance(db, user, "My physio", None)
    elif how == "head":
        head = ss.open_hold(db, user, "hold_all", "Hit their head", "detector",
                            red_flag="head_injury")
        ss.lift_head_injury(db, user, head.id, "My GP")
    else:
        fever = ss.open_hold(db, user, "hold_all", "Fever", "detector", red_flag="fever")
        ss.lift_fever(db, user, fever.id)


def _due_on(db, user, monkeypatch, when: datetime) -> bool:
    monkeypatch.setattr(ss, "_now", lambda: when)
    return ob.get_screening(db, user)["rescreen_due"]


@pytest.mark.parametrize("how", ["doctor", "physio", "head"])
def test_after_a_clinicians_clearance_the_rescreen_waits_a_year_from_it(
    db_session, monkeypatch, how
):
    user = _user(db_session)
    _answered_days_ago(db_session, user, 20)
    cleared = datetime.utcnow()
    monkeypatch.setattr(ss, "_now", lambda: cleared)
    _clear(db_session, user, how)
    for day in (0, 30, 31, 90, 345, 364):
        assert _due_on(db_session, user, monkeypatch, cleared + timedelta(days=day)) is False, day
    assert _due_on(
        db_session, user, monkeypatch, cleared + timedelta(days=365, minutes=1)
    ) is True


def test_after_a_fever_the_rescreen_stays_a_year_after_the_answers(db_session, monkeypatch):
    """A fever gone on the rider's own word isn't a clinician's look: the
    fever no longer reopens the questions, and the yearly date stays where
    the answers put it."""
    user = _user(db_session)
    answered = _answered_days_ago(db_session, user, 20)
    cleared = datetime.utcnow()
    monkeypatch.setattr(ss, "_now", lambda: cleared)
    _clear(db_session, user, "fever")
    for day in (0, 31, 90, 340):
        assert _due_on(db_session, user, monkeypatch, cleared + timedelta(days=day)) is False, day
    a_year = answered + timedelta(days=365)
    assert _due_on(db_session, user, monkeypatch, a_year - timedelta(minutes=1)) is False
    assert _due_on(db_session, user, monkeypatch, a_year + timedelta(minutes=1)) is True


def test_a_clearance_before_the_answers_never_pushes_their_year_back(db_session, monkeypatch):
    user = _user(db_session)
    cleared = datetime.utcnow() - timedelta(days=100)
    monkeypatch.setattr(ss, "_now", lambda: cleared)
    _clear(db_session, user, "doctor")
    answered = _answered_days_ago(db_session, user, 50)
    assert _due_on(db_session, user, monkeypatch, datetime.utcnow()) is False
    a_year = answered + timedelta(days=365)
    assert _due_on(db_session, user, monkeypatch, a_year - timedelta(minutes=1)) is False
    assert _due_on(db_session, user, monkeypatch, a_year + timedelta(minutes=1)) is True


@pytest.mark.parametrize("how", ["doctor", "head", "fever"])
def test_a_new_red_flag_after_a_clearance_still_asks_again(db_session, monkeypatch, how):
    user = _user(db_session)
    _answered_days_ago(db_session, user, 20)
    cleared = datetime.utcnow()
    monkeypatch.setattr(ss, "_now", lambda: cleared)
    _clear(db_session, user, how)
    assert _due_on(db_session, user, monkeypatch, cleared + timedelta(days=5)) is False
    # Something new, after the clearance: the answers may be out of date.
    faint = ss.open_hold(db_session, user, "hold_all", "Fainted on a climb", "detector",
                         red_flag="fainting")
    assert _due_on(db_session, user, monkeypatch, cleared + timedelta(days=5)) is True
    # Marked a mistake, it doesn't count.
    ss.lift_hold(db_session, user, faint.id, "mistake")
    assert _due_on(db_session, user, monkeypatch, cleared + timedelta(days=5)) is False
    # Cleared by a doctor in turn, it doesn't either.
    monkeypatch.setattr(ss, "_now", lambda: cleared + timedelta(days=6))
    ss.open_hold(db_session, user, "hold_all", "Fainted again", "detector",
                 red_flag="fainting")
    assert _due_on(db_session, user, monkeypatch, cleared + timedelta(days=6)) is True
    monkeypatch.setattr(ss, "_now", lambda: cleared + timedelta(days=7))
    ss.confirm_clearance(db_session, user, "My GP", None)
    assert _due_on(db_session, user, monkeypatch, cleared + timedelta(days=40)) is False


def test_the_easy_days_after_a_lift_are_not_a_red_flag(db_session, monkeypatch):
    """The easy week after a fever and the easy riding after a head injury
    follow a clearance and end by themselves; they never ask the questions
    again, even opened a moment after the clearance's record."""
    user = _user(db_session)
    _answered_days_ago(db_session, user, 20)
    cleared = datetime.utcnow()
    monkeypatch.setattr(ss, "_now", lambda: cleared)
    _clear(db_session, user, "head")
    easy = ss.open_holds(db_session, user)[0]
    assert easy.expires_at is not None
    easy.opened_at = cleared + timedelta(seconds=1)
    db_session.commit()
    assert _due_on(db_session, user, monkeypatch, cleared + timedelta(days=3)) is False


def test_the_gp_clears_chest_pain_and_no_hold_comes_back_on_day_31(api, monkeypatch):
    """The reverify's path, end to end: answers, chest pain in chat, a GP's
    clearance through the endpoint, then day 31 and day 100. The questions
    stay closed, so no truthful yes puts a full hold back on."""
    from app.api.v1 import safety as api_safety

    for route in api_safety.router.routes:
        for dep in getattr(route, "dependencies", []):
            window = getattr(dep.dependency, "window", None)
            if window is not None:
                window.reset()
    client, db, user = api
    assert client.post(
        "/api/v1/onboarding/screening", json={"answers": _answers(), "long_break": False}
    ).status_code == 200
    ss.open_hold(db, user.id, "hold_all", "Chest pain on the climb", "detector",
                 red_flag="chest_pain")
    assert client.get("/api/v1/onboarding/screening").json()["rescreen_due"] is True
    r = client.post("/api/v1/users/me/safety/clearance", json={"by": "My GP"})
    assert r.status_code == 200 and r.json()["allowed"] == "all"
    cleared = ss.latest_clearance_at(db, user.id)
    for day in (31, 100, 364):
        monkeypatch.setattr(ss, "_now", lambda day=day: cleared + timedelta(days=day))
        body = client.get("/api/v1/onboarding/screening").json()
        assert body["rescreen_due"] is False, day
        assert client.get("/api/v1/users/me/safety-state").json()["allowed"] == "all"
    monkeypatch.setattr(ss, "_now", lambda: cleared + timedelta(days=366))
    assert client.get("/api/v1/onboarding/screening").json()["rescreen_due"] is True
