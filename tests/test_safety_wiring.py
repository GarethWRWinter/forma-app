"""The joins between the six safety fixes (integration, 8 Oct 2026): what one
fixer's change needed from another's code, each pinned so it can't come
apart again.

- A break the rider mentions ends by itself, as its lift sentence promises
  (a clearance no longer lifts it), and the plan treats any hold that ends
  by itself as an easy window, not the whole plan.
- The coach reads every limit a clinician set, screening or none.
- A physio or a midwife clears only their own concern (red team #8).
- Nothing about a possible child reaches the model on any surface, and the
  nudge is fixed words under a hold or in the quiet window.
- The initiative read retires a card a new hold rules out.
- Outage and credit alerts have their own words.
- Chat edits and accepted proposals put the on-hold labels back.
- The privacy policy says what the purge keeps.

No real model calls: the model is a fake throughout.
"""

import asyncio
import json
from datetime import date, datetime, time, timedelta

import pytest

from app.core import forma_core
from app.core.workout_templates import ENDURANCE_Z2_SHORT, VO2MAX_5x5
from app.config import settings
from app.models.coach_initiative import CoachInitiative
from app.models.onboarding import EventPriority, EventType, GoalEvent
from app.models.plan_proposal import PlanProposal
from app.models.ride import Ride, RideSource
from app.models.safety import ConsentEvent, HealthScreening, SafetyEvent, SafetyHold
from app.models.training import Workout, WorkoutStatus
from app.models.user import User
from app.services import (
    briefing_service,
    coach_insights_service,
    coach_service,
    email_service,
    goal_read_service,
    plan_review_service,
    plan_service,
    safety_screen,
    workout_assessment_service,
)
from app.services import safety_service as ss
from app.services.plan_service import BREAK_PREFIX, _create_workout_steps, is_held, sync_hold_marks

TODAY = datetime.utcnow().date()


def _user(db, email="rider@example.com", **kw) -> User:
    kw.setdefault("ftp", 250)
    user = User(email=email, hashed_password="x", full_name="Sam Rider", **kw)
    db.add(user)
    db.commit()
    return user


def _workout(db, user, template=VO2MAX_5x5, day=None) -> Workout:
    w = Workout(
        user_id=user.id,
        scheduled_date=day or TODAY + timedelta(days=2),
        title=template["name"],
        description=template["description"],
        workout_type=template["workout_type"],
        planned_duration_seconds=template["duration_seconds"],
        planned_tss=40,
        status=WorkoutStatus.planned,
    )
    db.add(w)
    db.flush()
    _create_workout_steps(db, w, template)
    db.commit()
    db.refresh(w)
    return w


def _days(hold) -> int:
    return round((hold.expires_at - hold.opened_at).total_seconds() / 86400)


def _minor(db, user):
    return ss.open_hold(db, user, "hold_all", "Said they are 15", "detector", red_flag="minor")


def _break_hold(db, user, days=14):
    return ss.open_hold(
        db, user, "easy_only", "Easing back in after a break from riding", "layoff",
        red_flag="layoff", expires_at=datetime.utcnow() + timedelta(days=days),
    )


@pytest.fixture
def model(monkeypatch):
    """A fake model that records every call and answers with `reply`."""
    state = {"calls": [], "reply": "Fine."}

    class _Block:
        type = "text"

        def __init__(self, text):
            self.text = text

    class _Response:
        def __init__(self, text):
            self.content = [_Block(text)]
            self.stop_reason = "end_turn"

    def call(**kw):
        state["calls"].append(kw)
        return _Response(state["reply"])

    monkeypatch.setattr(forma_core, "call", call)
    return state


# ── A break ends by itself ─────────────────────────────────────────────────


@pytest.mark.parametrize("text, days", [
    ("I haven't ridden for six weeks", 14),
    ("I haven't ridden for six months", 28),
])
def test_a_break_the_rider_mentions_ends_by_itself(db_session, text, days):
    user = _user(db_session)
    safety_screen.screen_message(db_session, user, text)
    hold = ss.current_hold(db_session, user.id)
    assert (hold.level, hold.source, hold.red_flag) == ("easy_only", "layoff", "layoff")
    assert hold.expires_at is not None and _days(hold) == days
    # Still a break: the rider can call it a mistake, and a clearance never
    # lifts it (it would have stayed for ever before).
    assert ss.lift_kind(hold) == "layoff"
    assert ss.can_lift(hold, "mistake") and not ss.can_lift(hold, "clearance")
    ends = hold.expires_at
    sentence = safety_screen.lift_sentence(hold)
    assert sentence.startswith(
        f"This hold keeps your first {'four' if days == 28 else 'two'} weeks back to easy "
        f"riding by feel, until {ends.day} {ends:%B}."
    )
    assert ss.allowed_intensity(db_session, user, on_date=ends.date()) == "easy"
    assert ss.allowed_intensity(db_session, user, on_date=ends.date() + timedelta(days=1)) == "all"


def test_the_coach_tool_gives_a_break_an_end_date(db_session):
    said = _user(db_session)
    coach_service._execute_tool(db_session, said, "apply_safety_hold", {
        "level": "easy_only", "reason": "Back after some time off", "red_flag": "layoff",
        "days_off": 35,
    })
    assert _days(ss.current_hold(db_session, said.id)) == ss.LAYOFF_GATE_DAYS

    # Not said: the longer easy start, never cut short on a guess.
    unsaid = _user(db_session, email="unsaid@example.com")
    result = coach_service._execute_tool(db_session, unsaid, "apply_safety_hold", {
        "level": "easy_only", "reason": "Easing back in", "red_flag": "layoff",
    })
    hold = ss.current_hold(db_session, unsaid.id)
    assert _days(hold) == ss.LONG_LAYOFF_GATE_DAYS
    assert "This hold keeps your first four weeks" in result


def test_a_clearance_leaves_a_break_alone_and_a_mistake_lifts_it(db_session):
    user = _user(db_session)
    hold = _break_hold(db_session, user)
    # A break has nothing a clearance can lift, so the clearance is refused
    # before anything is written and the break stays.
    with pytest.raises(ss.NothingToClear):
        ss.confirm_clearance(db_session, user, "My GP", None)
    db_session.rollback()
    db_session.refresh(hold)
    assert hold.lifted_at is None
    ss.lift_hold(db_session, user, hold.id, "mistake")
    db_session.refresh(hold)
    assert hold.lifted_how == "mistake"


def test_the_plan_keeps_only_the_days_before_a_self_ending_hold_easy(db_session):
    user = _user(db_session)
    ends = datetime.utcnow() + timedelta(days=7)
    ss.open_hold(
        db_session, user, "easy_only", ss.FEVER_EASY_REASON, "detector",
        red_flag="fever", expires_at=ends,
    )
    standing, until = plan_service._intensity_gate(db_session, user)
    # Before the fix the easy week after a fever made the whole plan easy.
    assert standing == "all" and until == ends.date() + timedelta(days=1)

    inside = _workout(db_session, user, day=TODAY + timedelta(days=2))
    after = _workout(db_session, user, day=TODAY + timedelta(days=12))
    sync_hold_marks(db_session, user.id)
    assert is_held(inside) and not is_held(after)

    # A hold that doesn't end by itself still governs the whole plan.
    ss.open_hold(db_session, user, "easy_only", "On a beta blocker", "detector", red_flag="medication")
    standing, _ = plan_service._intensity_gate(db_session, user)
    assert standing == "easy"


def test_plan_labels_say_what_holds_the_rider_back(db_session):
    """Re-verification new problem 9: a fever's sessions never mention a
    doctor, and the easy week after it reads as illness, not a break."""
    user = _user(db_session)
    session = _workout(db_session, user, day=TODAY + timedelta(days=2))
    fever = ss.open_hold(db_session, user, "hold_all", "Has a fever", "detector", red_flag="fever")
    sync_hold_marks(db_session, user.id)
    db_session.refresh(session)
    assert session.description.startswith(ss.FEVER_HOLD_LABEL)
    assert "doctor" not in session.description.split(". ")[0]

    ss.lift_fever(db_session, user, fever.id)
    sync_hold_marks(db_session, user.id)
    db_session.refresh(session)
    assert session.description.startswith(ss.ILLNESS_EASY_LABEL)
    assert "break" not in session.description.split(". ")[1]
    # A new plan says the same in its "Easy for now" line.
    _, until = plan_service._intensity_gate(db_session, user)
    label, because = plan_service._easy_reason(ss.easy_windows(db_session, user), session.scheduled_date)
    assert label == ss.ILLNESS_EASY_LABEL
    assert plan_service._gated_description("Ride.", None, until, because).startswith(
        "Easy for now, because this is your easy week after illness."
    )
    # An under-18 account's sessions say only that they are on hold.
    _minor(db_session, user)
    sync_hold_marks(db_session, user.id)
    db_session.refresh(session)
    assert session.description.startswith(ss.ACCOUNT_HOLD_LABEL)
    assert plan_service.HOLD_LABELS == ss.HOLD_LABELS


def test_an_ended_hold_counts_as_a_safety_change(db_session):
    user = _user(db_session)
    hold = _break_hold(db_session, user)
    hold.opened_at = datetime.utcnow() - timedelta(days=15)
    hold.expires_at = datetime.utcnow() - timedelta(days=1)
    db_session.commit()
    assert safety_screen.safety_changed_since(db_session, user.id, datetime.utcnow() - timedelta(days=2))
    assert not safety_screen.safety_changed_since(db_session, user.id, datetime.utcnow() - timedelta(hours=1))


# ── The doctor's limits reach the coach ────────────────────────────────────


def test_the_coach_reads_every_limit_even_for_a_rider_who_never_screened(db_session):
    user = _user(db_session)
    ss.open_hold(db_session, user, "easy_only", "Knee", "detector", red_flag="injury")
    ss.confirm_clearance(db_session, user, "My physio", "No standing climbs")
    assert ss.latest_screening(db_session, user.id) is None
    context = safety_screen.coach_safety_context(db_session, user)
    assert context["doctor_limits"].startswith("No standing climbs (from My physio, ")

    # A re-screen afterwards loses nothing.
    db_session.add(HealthScreening(
        user_id=user.id, version=ss.SCREENING_VERSION, answers={}, any_yes=False, tier="none",
    ))
    db_session.commit()
    assert "No standing climbs" in safety_screen.coach_safety_context(db_session, user)["doctor_limits"]


# ── Each clinician clears their own concern (red team #8) ──────────────────


def test_a_physio_clears_the_knee_and_never_the_medicine(db_session):
    user = _user(db_session)
    knee = ss.open_hold(db_session, user, "easy_only", "Knee", "detector", red_flag="injury")
    medicine = ss.open_hold(db_session, user, "easy_only", "Beta blocker", "detector", red_flag="medication")
    ss.confirm_clearance(db_session, user, "My physio", None)
    db_session.refresh(knee)
    db_session.refresh(medicine)
    assert knee.lifted_how == "clearance"
    # So the medicine is never counted as cleared for a year either.
    assert medicine.lifted_at is None
    assert ss.current_hold(db_session, user.id).id == medicine.id


@pytest.mark.parametrize("red_flag, needs", [
    ("chest_pain", "a doctor. Once one has cleared you, pick My GP or Another doctor."),
    ("head_injury", "a doctor. Once one has cleared you, pick My GP or Another doctor."),
    ("medication", "a doctor. Once one has cleared you, pick My GP or Another doctor."),
    ("pregnancy", "a midwife or a doctor. Once one has cleared you, pick My midwife, "
                  "My GP or Another doctor."),
])
def test_a_physio_alone_clears_nothing_outside_an_injury_and_records_nothing(
    db_session, red_flag, needs
):
    user = _user(db_session)
    hold = ss.open_hold(db_session, user, "hold_all", "Held", "detector", red_flag=red_flag)
    with pytest.raises(ss.ClearanceOutOfScope) as refused:
        ss.confirm_clearance(db_session, user, "My physio", "No sprints")
    assert str(refused.value) == (
        f"A physio can clear an injury, but what's holding you needs {needs}"
    )
    db_session.rollback()
    db_session.refresh(hold)
    assert hold.lifted_at is None
    assert db_session.query(ConsentEvent).count() == 0
    assert ss.active_limits(db_session, user) == []


def test_the_refusal_names_who_can_clear_it(db_session):
    user = _user(db_session)
    ss.open_hold(db_session, user, "easy_only", "Knee", "detector", red_flag="injury")
    with pytest.raises(ss.ClearanceOutOfScope) as refused:
        ss.confirm_clearance(db_session, user, "My midwife", None)
    assert str(refused.value) == (
        "A midwife can clear a pregnancy, but what's holding you needs a physio or a "
        "doctor. Once one has cleared you, pick My physio, My GP or Another doctor."
    )


def test_the_screening_is_stamped_only_when_every_yes_is_the_clinicians(db_session):
    knee_only = _user(db_session)
    db_session.add(HealthScreening(
        user_id=knee_only.id, version=ss.SCREENING_VERSION, answers={"q6": True},
        any_yes=True, tier="easy_only",
    ))
    db_session.commit()
    ss.open_hold(db_session, knee_only, "easy_only", "Health answers: injury", "screening", red_flag="injury")
    ss.confirm_clearance(db_session, knee_only, "My physio", None)
    assert ss.latest_screening(db_session, knee_only.id).clearance_confirmed_at is not None
    assert ss.allowed_intensity(db_session, knee_only) == "all"

    heart_too = _user(db_session, email="heart@example.com")
    db_session.add(HealthScreening(
        user_id=heart_too.id, version=ss.SCREENING_VERSION, answers={"q1": True, "q6": True},
        any_yes=True, tier="easy_only",
    ))
    db_session.commit()
    with pytest.raises(ss.ClearanceOutOfScope):
        ss.confirm_clearance(db_session, heart_too, "My physio", None)
    db_session.rollback()
    assert ss.latest_screening(db_session, heart_too.id).clearance_confirmed_at is None
    # A midwife clears a pregnancy, a doctor anything.
    ss.open_hold(db_session, heart_too, "easy_only", "Pregnant", "detector", red_flag="pregnancy")
    ss.confirm_clearance(db_session, heart_too, "My midwife", None)
    assert ss.latest_screening(db_session, heart_too.id).clearance_confirmed_at is None
    ss.confirm_clearance(db_session, heart_too, "My GP", None)
    assert ss.allowed_intensity(db_session, heart_too) == "all"


def test_the_clearance_endpoint_says_who_can_clear_what():
    from fastapi.testclient import TestClient

    from app.api.v1.deps import get_current_user
    from app.database import get_db
    from app.main import app

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.models.base import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = _user(db)
    ss.open_hold(db, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, user.id)
    try:
        r = TestClient(app).post(
            "/api/v1/users/me/safety/clearance", json={"by": "My physio", "limits": None}
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 400
    assert r.json()["detail"].startswith("A physio can clear an injury, but what's holding you")
    assert ss.current_hold(db, user.id) is not None
    assert db.query(ConsentEvent).count() == 0
    db.close()


# ── Nothing about a possible child reaches the model ───────────────────────


def test_no_surface_sends_a_possible_childs_data_to_the_model(db_session, model):
    user = _user(db_session)
    _minor(db_session, user)
    adults_only = briefing_service.ADULTS_ONLY_BRIEFING

    assert coach_insights_service.generate_daily_nudge(db_session, user)["nudge"] == adults_only

    ride = Ride(
        user_id=user.id, source=RideSource.manual, title="Morning ride",
        ride_date=datetime.combine(TODAY, time(9)), duration_seconds=3600,
    )
    db_session.add(ride)
    db_session.commit()
    debrief = coach_insights_service.generate_ride_debrief(db_session, user, ride)
    assert debrief["debrief"] == adults_only and debrief["cached"] is False
    assert ride.debrief_text is None
    coach_insights_service.generate_ride_story(db_session, user, ride)
    assert ride.story is None and ride.forma_title is None
    assert coach_insights_service.explain_metric(db_session, user, "FTP", 250) == {
        "explanation": adults_only
    }

    goal = GoalEvent(
        user_id=user.id, event_name="Fred Whitton", event_date=TODAY + timedelta(days=60),
        event_type=EventType.sportive, priority=EventPriority.a_race,
    )
    db_session.add(goal)
    db_session.commit()
    assert goal_read_service.generate_goal_read(db_session, user, goal).coach_read is None

    workout = _workout(db_session, user, ENDURANCE_Z2_SHORT, day=TODAY)
    workout.actual_ride_id = ride.id
    db_session.commit()
    assessed = workout_assessment_service.generate_assessment(db_session, user, workout)
    assert assessed.execution_feedback is None and assessed.execution_score is not None

    assert model["calls"] == []


def test_the_nudge_in_the_quiet_window_is_no_pressure_and_never_written(db_session, model):
    user = _user(db_session)
    db_session.add(SafetyEvent(user_id=user.id, kind="crisis", source="chat", matched="worthless"))
    db_session.commit()
    nudge = coach_insights_service.generate_daily_nudge(db_session, user)
    assert nudge["nudge"] == coach_insights_service.QUIET_NUDGE and nudge["cached"] is False
    assert model["calls"] == []


def test_the_metric_debrief_and_assessment_prompts_carry_the_safety_state(db_session, model):
    user = _user(db_session)
    ss.open_hold(db_session, user, "easy_only", "Sore knee", "detector", red_flag="injury")
    coach_insights_service.explain_metric(db_session, user, "TSB", -12)
    sent = model["calls"][-1]
    assert '"allowed_intensity": "easy"' in sent["messages"][0]["content"]
    assert "never suggest training above" in sent["system"]

    ride = Ride(
        user_id=user.id, source=RideSource.manual, title="Ride",
        ride_date=datetime.combine(TODAY, time(9)), duration_seconds=3600,
    )
    db_session.add(ride)
    db_session.commit()
    coach_insights_service.generate_ride_debrief(db_session, user, ride)
    sent = model["calls"][-1]
    assert '"allowed_intensity": "easy"' in sent["messages"][0]["content"]
    assert "Read `safety` first" in sent["system"]

    workout = _workout(db_session, user, ENDURANCE_Z2_SHORT, day=TODAY)
    workout.actual_ride_id = ride.id
    db_session.commit()
    model["reply"] = json.dumps({"feedback": "Good.", "adjustments": []})
    workout_assessment_service.generate_assessment(db_session, user, workout)
    sent = model["calls"][-1]
    assert '"allowed_intensity": "easy"' in sent["messages"][0]["content"]
    assert "Read `safety` first" in sent["system"]


# ── The initiative read ────────────────────────────────────────────────────


def test_the_initiative_read_retires_a_card_a_new_hold_rules_out(db_session):
    from app.api.v1.coach_insights import _pending_initiative

    user = _user(db_session)
    card = CoachInitiative(
        user_id=user.id, kind="ride_insight", headline="h", question="q?", status="pending",
    )
    db_session.add(card)
    db_session.commit()
    assert _pending_initiative(db_session, user.id).id == card.id

    ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
    assert _pending_initiative(db_session, user.id) is None
    db_session.refresh(card)
    assert card.status == "expired"


# ── Founder alerts ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind, subject", [
    ("reply_failed_outage", "Forma ops alert: coach replies are failing"),
    ("reply_failed_credit", "Forma ops alert: the Anthropic credit balance is empty"),
])
def test_ops_alerts_say_what_failed_and_never_quote_a_rider(monkeypatch, kind, subject):
    sent = []

    async def fake_send(to, subj, body, from_address=None):
        sent.append((to, subj, body))
        return True

    monkeypatch.setattr(email_service, "send", fake_send)
    assert asyncio.run(email_service.send_safety_alert(
        kind, "rider@example.com", "u-1", "4 coach replies failed in the last 10 minutes."
    ))
    to, subj, body = sent[0]
    assert to == settings.founder_alert_email and subj == subject
    assert "4 coach replies failed" in body and "What they wrote" not in body
    assert "rider@example.com" not in body
    assert not any(ch in subj + body for ch in "–—!")


def test_a_failed_red_flag_reply_is_not_labelled_as_the_riders_words(monkeypatch):
    sent = []

    async def fake_send(to, subj, body, from_address=None):
        sent.append((subj, body))
        return True

    monkeypatch.setattr(email_service, "send", fake_send)
    asyncio.run(email_service.send_safety_alert(
        "reply_failed", "rider@example.com", "u-1", "Red flags: chest_pain."
    ))
    subj, body = sent[0]
    assert subj == "Forma safety alert: a red-flag reply failed"
    assert "What happened:\nRed flags: chest_pain." in body and "What they wrote" not in body


# ── Labels follow chat edits and accepted proposals ────────────────────────


def test_a_chat_move_out_of_the_easy_start_takes_the_label_off(db_session):
    user = _user(db_session)
    session = _workout(db_session, user, day=TODAY + timedelta(days=2))
    _break_hold(db_session, user, days=14)
    sync_hold_marks(db_session, user.id)
    assert session.description.startswith(BREAK_PREFIX)

    result = coach_service._execute_tool(db_session, user, "update_workout", {
        "workout_id": session.id, "scheduled_date": (TODAY + timedelta(days=20)).isoformat(),
    })
    assert result.startswith("Updated")
    db_session.refresh(session)
    assert not is_held(session)


def test_an_accepted_proposal_puts_the_labels_back(db_session):
    user = _user(db_session)
    session = _workout(db_session, user, day=TODAY + timedelta(days=3))
    _break_hold(db_session, user, days=14)
    sync_hold_marks(db_session, user.id)
    assert session.description.startswith(BREAK_PREFIX)

    # Moved past the easy start: the label must come off with it.
    proposal = PlanProposal(
        user_id=user.id, trigger="manual", observation="o", rationale="r", status="pending",
        changes=[{
            "action": "update_workout", "workout_id": session.id,
            "scheduled_date": (TODAY + timedelta(days=20)).isoformat(),
        }],
    )
    db_session.add(proposal)
    db_session.commit()
    assert plan_review_service.apply_proposal(db_session, user, proposal) == 1
    db_session.refresh(session)
    assert session.scheduled_date == TODAY + timedelta(days=20)
    assert not is_held(session)


# ── The privacy policy says what the purge keeps ───────────────────────────


def test_the_privacy_policy_says_what_the_purge_keeps():
    text = " ".join(settings.privacy_doc().text.split())
    assert "until that person's 21st birthday if that is later" in text
    assert "your message and the coach's reply in that exchange" in text
    assert "or, for an account made before we asked, when the app first asked you" in text


# ── A re-mention in the easy days (reverify round 3, problem 1) ────────────


@pytest.mark.parametrize("flag, text, held_label, easy_label", [
    ("fever", "I had a fever last week, feeling much better now",
     ss.FEVER_HOLD_LABEL, ss.ILLNESS_EASY_LABEL),
    ("head_injury", "how long after hitting my head can I do intervals?",
     ss.HEAD_HOLD_LABEL, ss.HEAD_EASY_LABEL),
])
def test_a_remention_marked_a_mistake_puts_the_easy_label_back(
    db_session, flag, text, held_label, easy_label
):
    """The detector reads the re-mention, the plan goes on hold, the rider
    calls it a mistake, and the plan's hard sessions read as the easy days
    again, never as free to ride. The coach is told the same racing date
    throughout."""
    user = _user(db_session, country="GB")
    first = ss.open_hold(db_session, user, "hold_all", "First", "detector", red_flag=flag)
    first.opened_at = datetime.utcnow() - timedelta(days=3)
    db_session.commit()
    if flag == "fever":
        easy = ss.lift_fever(db_session, user, first.id)
    else:
        easy = ss.lift_head_injury(db_session, user, first.id, "My GP")
    racing = ss.no_racing_or_group_until(db_session, user)
    session = _workout(db_session, user, day=TODAY + timedelta(days=2))
    sync_hold_marks(db_session, user.id)
    db_session.refresh(session)
    assert session.description.startswith(easy_label)

    result = safety_screen.screen_message(db_session, user, text)
    assert flag in result.kinds
    db_session.refresh(session)
    assert session.description.startswith(held_label)
    assert ss.no_racing_or_group_until(db_session, user) == racing

    (again,) = [h for h in ss.open_holds(db_session, user) if h.id != easy.id]
    ss.lift_hold(db_session, user, again.id, "mistake", note="Rider marked it a mistake.")
    sync_hold_marks(db_session, user.id)
    db_session.refresh(session)
    assert session.description.startswith(easy_label)
    assert ss.allowed_intensity(db_session, user, on_date=session.scheduled_date) == "easy"
    assert ss.no_racing_or_group_until(db_session, user) == racing


# ── A second head injury in the build-back (reverify round 4, problem A) ───
#
# The detector marks a genuinely new event (Hit.new_event); safety_service
# reads it off the hit it is handed (open_hold's hit=, with getattr) and
# restarts the easy fortnight and the 21-day racing clock from that day.
# These pin the join: until the detector's Hit carries new_event they skip.


def _needs_new_event():
    fields = getattr(safety_screen.Hit, "__dataclass_fields__", {})
    if "new_event" not in fields:
        pytest.skip("Waiting for the detector's Hit.new_event")


def _in_the_build_back(db, user, days_ago: int = 12):
    """A head injury `days_ago` days back, checked by a GP the next day."""
    first = ss.open_hold(db, user, "hold_all", "First", "detector", red_flag="head_injury")
    first.opened_at = datetime.utcnow() - timedelta(days=days_ago)
    db.commit()
    easy = ss.lift_head_injury(db, user, first.id, "My GP")
    easy.opened_at = first.opened_at + timedelta(days=1)
    db.commit()
    return first, easy


def test_a_hit_marked_new_reaches_the_hold_and_restarts_both_clocks(db_session, monkeypatch):
    _needs_new_event()
    user = _user(db_session, country="GB")
    first, easy = _in_the_build_back(db_session, user)
    first_racing = ss.no_racing_or_group_until(db_session, user)
    assert first_racing == first.opened_at.date() + timedelta(days=21)

    new_hit = safety_screen.Hit(
        kind="head_injury", matched="crashed again today and hit my head",
        severity="emergency", new_event=True,
    )
    monkeypatch.setattr(safety_screen, "_detect", lambda text: [new_hit])
    result = safety_screen.screen_message(db_session, user, "crashed again today")
    assert "head_injury" in result.kinds

    (second,) = [h for h in ss.open_holds(db_session, user) if h.id != easy.id]
    assert ss.is_new_event(second) and ss.remention_of(db_session, second) is None
    assert ss.no_racing_or_group_until(db_session, user) == TODAY + timedelta(days=21)
    after = ss.lift_head_injury(db_session, user, second.id, "My GP")
    assert after.expires_at.date() == TODAY + timedelta(days=ss.HEAD_EASY_DAYS)


def test_a_hit_not_marked_new_keeps_the_first_clock(db_session, monkeypatch):
    _needs_new_event()
    user = _user(db_session, country="GB")
    first, easy = _in_the_build_back(db_session, user)
    racing = ss.no_racing_or_group_until(db_session, user)
    old_hit = safety_screen.Hit(
        kind="head_injury", matched="hit my head", severity="emergency", new_event=False,
    )
    monkeypatch.setattr(safety_screen, "_detect", lambda text: [old_hit])
    safety_screen.screen_message(db_session, user, "still thinking about hitting my head")
    (again,) = [h for h in ss.open_holds(db_session, user) if h.id != easy.id]
    assert ss.remention_of(db_session, again).id == easy.id
    assert ss.no_racing_or_group_until(db_session, user) == racing


@pytest.mark.parametrize("text", [
    "crashed again today and hit my head on the kerb",
    "came off on the ice this morning, smacked my head, cracked helmet",
])
def test_the_reverify_second_crashes_restart_the_clock_end_to_end(db_session, text):
    _needs_new_event()
    user = _user(db_session, country="GB")
    _in_the_build_back(db_session, user)
    result = safety_screen.screen_message(db_session, user, text)
    assert "head_injury" in result.kinds
    assert ss.no_racing_or_group_until(db_session, user) == TODAY + timedelta(days=21)
