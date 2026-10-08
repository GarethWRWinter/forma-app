"""The coach flow after the adversarial review: what the chat tools, plan
proposals, the coach writing first and the memory do with a rider's safety
state, what happens when the model fails, and the AI disclosure.

Review finding 5: a chat edit rewrote a completed session into a VO2max one
under a full hold. Finding 7: check-in emails ignored holds, under-18 accounts
and the quiet window after crisis words. Finding 8: proposals skipped the
injury and under-eating rules. Red-team 17: a failed reply on an ordinary
turn left no record and sent no alert, and nothing was retried.

Round 4: the safety classifier wired into every chat path (its verdict merged
with the regex's and acted on the same way), and new problem E, an under-18
admission inside the quiet window after an admin lift.

No real model calls: every model is a fake, the classifier included.
"""

import asyncio
import json
import re
import sys
import threading
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.core.workout_templates import ENDURANCE_Z2_SHORT, RECOVERY_SPIN, VO2MAX_5x5
from app.models.briefing import Briefing
from app.models.chat import ChatMessage, ChatRole, ChatSession
from app.models.coach_initiative import CoachInitiative
from app.models.memory import MemoryEntity
from app.models.plan_proposal import PlanProposal
from app.models.ride import Ride, RideSource
from app.models.safety import SafetyEvent
from app.models.training import Workout, WorkoutStatus
from app.models.user import User
from app.services import (
    briefing_service,
    coach_service,
    initiative_service,
    memory_service,
    outreach_service,
    plan_review_service,
    safety_screen,
)
from app.services import safety_service as ss
from app.services.plan_service import _create_workout_steps
from app.services.safety_screen import REPLY_FAILED_MESSAGE

TODAY = datetime.utcnow().date()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _user(db, email="rider@example.com", **kw) -> User:
    kw.setdefault("ftp", 250)
    kw.setdefault("is_active", True)
    kw.setdefault("email_verified", True)
    user = User(email=email, hashed_password="x", full_name="Sam Rider", **kw)
    db.add(user)
    db.commit()
    return user


def _workout(db, user, template=VO2MAX_5x5, day=None, status=WorkoutStatus.planned) -> Workout:
    w = Workout(
        user_id=user.id,
        scheduled_date=day or TODAY + timedelta(days=2),
        title=template["name"],
        description=template["description"],
        workout_type=template["workout_type"],
        planned_duration_seconds=template["duration_seconds"],
        planned_tss=40,
        status=status,
    )
    db.add(w)
    db.flush()
    _create_workout_steps(db, w, template)
    db.commit()
    db.refresh(w)
    return w


def _session(db, user) -> ChatSession:
    s = ChatSession(user_id=user.id, title="Chat - 8 Oct 2026")
    db.add(s)
    db.commit()
    return s


def _hold_all(db, user, red_flag="chest_pain", source="detector"):
    return ss.open_hold(db, user, "hold_all", "Chest pain on the climb", source, red_flag=red_flag)


def _minor(db, user):
    return ss.open_hold(db, user, "hold_all", "Said they are 15", "detector", red_flag="minor")


def _crisis(db, user):
    db.add(SafetyEvent(user_id=user.id, kind="crisis", source="chat", matched="worthless"))
    db.commit()


def _layoff_rider(db, user):
    """Off the bike for 40 days: the layoff gate keeps them to easy riding."""
    db.add(Ride(
        user_id=user.id, source=RideSource.manual,
        ride_date=datetime.combine(TODAY - timedelta(days=40), time(9)),
    ))
    db.commit()


def _run(agen) -> list[dict]:
    async def collect():
        return [chunk async for chunk in agen]

    return [json.loads(c[len("data: "):]) for c in asyncio.run(collect())]


def _text(chunks) -> str:
    return "".join(c["content"] for c in chunks if c["type"] == "text")


class _Usage:
    input_tokens = 10
    output_tokens = 5


class _Final:
    content = []
    stop_reason = "end_turn"
    usage = _Usage()


class _Stream:
    def __init__(self, texts, fail_after=False):
        self.texts, self.fail_after = texts, fail_after

    def __iter__(self):
        for t in self.texts:
            yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(text=t))
        if self.fail_after:
            raise RuntimeError("connection reset")

    def get_final_message(self):
        return _Final()


def _credit_error() -> Exception:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    import anthropic

    return anthropic.BadRequestError(
        "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
        "'message': 'Your credit balance is too low to access the Anthropic API. Please go "
        "to Plans & Billing to upgrade or purchase credits.'}}",
        response=httpx.Response(400, request=request),
        body=None,
    )


@pytest.fixture
def model(monkeypatch):
    """A scripted model. `plan` is a list, one entry per call: an exception
    to raise as the call opens, or a list of text chunks to stream. Calls
    past the end of the plan stream "Fine." Memory, titles and alerts are
    recorded, never run."""
    state = {"plan": [], "calls": [], "alerts": [], "memory": 0}

    @contextmanager
    def fake_stream(**kwargs):
        state["calls"].append(kwargs)
        step = state["plan"].pop(0) if state["plan"] else ["Fine."]
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, tuple):  # (texts, fail_after)
            yield _Stream(*step)
            return
        yield _Stream(step)

    def fake_call(**kwargs):
        state["calls"].append(kwargs)
        step = state["plan"].pop(0) if state["plan"] else ["Fine."]
        if isinstance(step, BaseException):
            raise step
        return SimpleNamespace(text="".join(step), usage=_Usage())

    def memory(*a, **k):
        state["memory"] += 1

    monkeypatch.setattr(coach_service.forma_core, "stream", fake_stream)
    monkeypatch.setattr(coach_service.forma_core, "call", fake_call)
    monkeypatch.setattr(coach_service, "response_text", lambda r: r.text)
    monkeypatch.setattr(coach_service, "maybe_autotitle_session", lambda *a, **k: None)
    monkeypatch.setattr(coach_service, "_relevant_memories", lambda *a, **k: None)
    monkeypatch.setattr(coach_service, "_dossier_block", lambda *a, **k: "")
    monkeypatch.setattr(coach_service, "MODEL_RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(coach_service, "_ops_alerted_at", {})
    # The regex check alone, unless a test brings in the `classifier` fake.
    monkeypatch.setattr(coach_service, "_load_classifier", lambda: None)
    monkeypatch.setattr("app.services.memory_service.extract_memories", memory)
    monkeypatch.setattr("app.services.voice_service.is_voice_enabled", lambda: False)
    monkeypatch.setattr(
        safety_screen, "alert_founder",
        lambda kind, user, excerpt, ids=None: state["alerts"].append((kind, excerpt)),
    )
    return state


def _failed(db) -> list[SafetyEvent]:
    return db.query(SafetyEvent).filter(SafetyEvent.kind == "reply_failed").all()


# ── Finding 5: a completed session is the record, never rewritten ───────────


def test_chat_never_rewrites_a_completed_session_into_a_vo2_one(db_session):
    """The reviewer's probe: under hold_all, yesterday's completed recovery
    ride became tomorrow's VO2max session and its status became modified."""
    user = _user(db_session)
    _hold_all(db_session, user)
    ridden = _workout(
        db_session, user, RECOVERY_SPIN, day=TODAY - timedelta(days=1),
        status=WorkoutStatus.completed,
    )

    result = coach_service._execute_tool(db_session, user, "update_workout", {
        "workout_id": ridden.id, "workout_type": "vo2max",
        "scheduled_date": (TODAY + timedelta(days=1)).isoformat(),
    })

    assert result.startswith("Not changed:") and "already ridden" in result
    db_session.expire_all()
    ridden = db_session.get(Workout, ridden.id)
    assert ridden.status == WorkoutStatus.completed
    assert ridden.scheduled_date == TODAY - timedelta(days=1)
    assert coach_service._type_value(ridden.workout_type) == "recovery"
    assert ss.max_step_pct(ridden) <= ss.INTENSITY_CEILING["recovery"]


def test_no_chat_tool_touches_a_completed_session_even_without_a_hold(db_session):
    user = _user(db_session)
    ridden = _workout(
        db_session, user, RECOVERY_SPIN, day=TODAY - timedelta(days=1),
        status=WorkoutStatus.completed,
    )
    planned = _workout(db_session, user, RECOVERY_SPIN, day=TODAY + timedelta(days=1))

    for name, tool_input in [
        ("update_workout", {"workout_id": ridden.id, "title": "Something else"}),
        ("skip_workout", {"workout_id": ridden.id}),
        ("swap_workout_date", {"workout_id_a": ridden.id, "workout_id_b": planned.id}),
    ]:
        result = coach_service._execute_tool(db_session, user, name, tool_input)
        assert "already ridden" in result, name

    db_session.expire_all()
    ridden = db_session.get(Workout, ridden.id)
    assert ridden.status == WorkoutStatus.completed
    assert ridden.title == RECOVERY_SPIN["name"]
    assert ridden.scheduled_date == TODAY - timedelta(days=1)
    assert db_session.get(Workout, planned.id).scheduled_date == TODAY + timedelta(days=1)


def test_the_gate_runs_on_a_skipped_session_put_back_in_the_plan(db_session):
    user = _user(db_session)
    ss.open_hold(db_session, user, "easy_only", "Asthma medicine", "screening")
    skipped = _workout(db_session, user, ENDURANCE_Z2_SHORT, status=WorkoutStatus.skipped)

    refused = coach_service._execute_tool(db_session, user, "update_workout", {
        "workout_id": skipped.id, "workout_type": "vo2max",
    })
    assert refused.startswith("Not changed:")
    db_session.expire_all()
    assert db_session.get(Workout, skipped.id).status == WorkoutStatus.skipped

    allowed = coach_service._execute_tool(db_session, user, "update_workout", {
        "workout_id": skipped.id, "workout_type": "recovery",
    })
    assert allowed.startswith("Updated workout")


def test_a_lower_hold_added_under_a_full_hold_never_tells_the_coach_easy_riding_is_fine(
    db_session,
):
    user = _user(db_session)
    full = _hold_all(db_session, user)
    result = coach_service._execute_tool(db_session, user, "apply_safety_hold", {
        "level": "easy_only", "reason": "Sore knee", "red_flag": "injury",
    })
    assert result.startswith("A hold_all hold was already open (Chest pain on the climb)")
    assert "No riding of any kind until then." in result
    assert "Easy riding by feel is fine" not in result
    assert ss.current_hold(db_session, user.id).id == full.id


# ── Finding 8: proposals follow the injury and under-eating rules ───────────


def _injury(db, user):
    return ss.open_hold(db, user, "easy_only", "Knee pain", "detector", red_flag="injury")


def _restriction(db, user):
    db.add(SafetyEvent(user_id=user.id, kind="restriction", source="chat", matched="900 calories"))
    db.commit()


def test_the_reviewers_probe_an_injury_and_a_900_calorie_flag_hold_an_added_ride(db_session):
    user = _user(db_session)
    _injury(db_session, user)
    _restriction(db_session, user)
    add = {
        "action": "add_workout", "workout_id": None,
        "scheduled_date": (TODAY + timedelta(days=3)).isoformat(),
        "workout_type": "endurance", "title": "Steady hour",
        "planned_duration_seconds": 3600, "planned_tss": 40,
    }
    chat = coach_service._execute_tool(db_session, user, "add_workout", {
        "scheduled_date": add["scheduled_date"], "title": "Steady hour",
        "workout_type": "endurance",
    })
    kept, held = plan_review_service.gate_changes(db_session, user, [add])
    assert chat.startswith("Not changed")
    assert (kept, held) == ([], [add])


def test_an_injury_hold_keeps_rides_out_of_proposals_but_lets_skips_and_rest_through(db_session):
    user = _user(db_session)
    _injury(db_session, user)
    workout = _workout(db_session, user, ENDURANCE_Z2_SHORT)
    add = {
        "action": "add_workout", "scheduled_date": (TODAY + timedelta(days=3)).isoformat(),
        "workout_type": "recovery",
    }
    retype = {"action": "update_workout", "workout_id": workout.id, "workout_type": "recovery"}
    move = {
        "action": "update_workout", "workout_id": workout.id,
        "scheduled_date": (TODAY + timedelta(days=4)).isoformat(),
    }
    rest = {"action": "update_workout", "workout_id": workout.id, "workout_type": "rest"}
    skip = {"action": "skip_workout", "workout_id": workout.id}

    kept, held = plan_review_service.gate_changes(
        db_session, user, [add, retype, move, rest, skip]
    )
    assert held == [add, retype]
    assert kept == [move, rest, skip]


def test_an_under_eating_flag_holds_any_proposal_that_adds_load(db_session):
    user = _user(db_session)
    _restriction(db_session, user)
    workout = _workout(db_session, user, ENDURANCE_Z2_SHORT)
    longer = {
        "action": "update_workout", "workout_id": workout.id,
        "planned_duration_seconds": workout.planned_duration_seconds + 1800,
    }
    harder = {"action": "update_workout", "workout_id": workout.id, "workout_type": "tempo"}
    added = {
        "action": "add_workout", "scheduled_date": (TODAY + timedelta(days=3)).isoformat(),
        "workout_type": "recovery",
    }
    easier = {"action": "update_workout", "workout_id": workout.id, "workout_type": "recovery"}

    kept, held = plan_review_service.gate_changes(
        db_session, user, [longer, harder, added, easier]
    )
    assert held == [longer, harder, added]
    assert kept == [easier]


def test_accepting_a_proposal_after_an_injury_hold_opens_changes_nothing(db_session):
    user = _user(db_session)
    workout = _workout(db_session, user, RECOVERY_SPIN)
    proposal = PlanProposal(
        user_id=user.id, trigger="manual", observation="o", rationale="r", status="pending",
        changes=[{"action": "update_workout", "workout_id": workout.id, "workout_type": "endurance"}],
    )
    db_session.add(proposal)
    db_session.commit()
    _injury(db_session, user)  # opened after the proposal was written

    assert plan_review_service.apply_proposal(db_session, user, proposal) == 0
    db_session.expire_all()
    assert coach_service._type_value(db_session.get(Workout, workout.id).workout_type) == "recovery"


def test_a_coach_filed_proposal_under_an_injury_hold_files_nothing(db_session):
    user = _user(db_session)
    _injury(db_session, user)
    result = coach_service._execute_tool(db_session, user, "propose_plan_change", {
        "observation": "You've had no endurance this week.",
        "rationale": "The base needs it.",
        "changes": [{
            "action": "add_workout", "scheduled_date": (TODAY + timedelta(days=2)).isoformat(),
            "workout_type": "endurance", "title": "Steady hour",
        }],
    })
    assert result.startswith("Nothing was filed.") and "injury hold" in result
    assert db_session.query(PlanProposal).count() == 0


# ── Finding 7: the coach writing first ──────────────────────────────────────


def _activation(monkeypatch, stage="first_ride", quiet_days=3):
    monkeypatch.setattr(outreach_service, "activation_state", lambda db, u, now=None: {
        "stage": stage, "quiet_days": quiet_days, "last_activity": "x", "stage_index": 3,
        "facts": {}, "next_action": {
            "title": "Get one ride in", "instruction": "Go and ride.", "link": "/dashboard/rides",
        },
    })


def test_outreach_skips_a_rider_a_hold_a_child_or_crisis_words_rule_out(db_session, monkeypatch):
    _activation(monkeypatch)
    clear = _user(db_session, "clear@example.com")
    held = _user(db_session, "held@example.com")
    ss.open_hold(db_session, held, "hold_all", "Chest pain on screening", "screening")
    easy = _user(db_session, "easy@example.com")
    ss.open_hold(db_session, easy, "easy_only", "Asthma medicine", "screening")
    child = _user(db_session, "child@example.com")
    _minor(db_session, child)
    low = _user(db_session, "low@example.com")
    _crisis(db_session, low)

    due = {u.email for u, _, _ in outreach_service.due_riders(db_session)}

    assert due == {"clear@example.com"}


def test_outreach_still_writes_after_a_break_with_the_safety_state_in_the_brief(
    db_session, monkeypatch
):
    """The layoff gate alone prescribes easy riding, which is what the
    check-in asks for, so it goes out, and the model reads the gate."""
    _activation(monkeypatch)
    user = _user(db_session)
    _layoff_rider(db_session, user)
    assert ss.allowed_intensity(db_session, user) == "easy"

    due = outreach_service.due_riders(db_session)
    assert [u.id for u, _, _ in due] == [user.id]

    seen = {}

    def fake_call(**kw):
        seen.update(kw)
        return SimpleNamespace()

    monkeypatch.setattr(outreach_service.forma_core, "call", fake_call)
    monkeypatch.setattr(
        outreach_service, "response_text",
        lambda r: "Subject: One easy ride this week\n\nSam, ride easy.\n\nForma",
    )
    outreach_service.compose(db_session, user, due[0][1])
    brief = json.loads(seen["messages"][0]["content"])
    assert brief["safety"]["allowed_intensity"] == "easy"
    assert "layoff_gate" in brief["safety"]
    assert "Read `safety`" in seen["system"]


def test_outreach_fails_closed_when_the_safety_state_cannot_be_read(db_session, monkeypatch):
    _activation(monkeypatch)
    _user(db_session)

    def broken(*a, **k):
        raise RuntimeError("database gone")

    monkeypatch.setattr(ss, "allowed_intensity", broken)
    assert outreach_service.due_riders(db_session) == []


def _initiative_model(monkeypatch, insight=True):
    calls = {"model": 0, "insight": 0}

    def insight_finder(db, user):
        calls["insight"] += 1
        return {
            "kind": "ride_insight", "trigger": "easy_ride_was_not_easy",
            "subject_type": "ride", "subject_id": "ride-1", "numbers": {"if": 0.78},
        } if insight else None

    def fake_call(**kw):
        calls["model"] += 1
        return SimpleNamespace()

    monkeypatch.setattr(initiative_service, "find_open_loop", lambda db, u: None)
    monkeypatch.setattr(initiative_service, "find_ride_insight", insight_finder)
    monkeypatch.setattr(initiative_service, "weekly_checkin_due", lambda db, u: False)
    monkeypatch.setattr(initiative_service.forma_core, "call", fake_call)
    monkeypatch.setattr(
        initiative_service, "response_text",
        lambda r: '{"headline": "Your easy ride ran warm", "body": "It was tempo.", '
                  '"question": "What was going on?"}',
    )
    return calls


def test_an_initiative_is_raised_for_a_rider_with_nothing_holding_them(db_session, monkeypatch):
    calls = _initiative_model(monkeypatch)
    user = _user(db_session)
    assert initiative_service.generate(db_session, user) is not None
    assert calls == {"model": 1, "insight": 1}


@pytest.mark.parametrize("state", ["minor", "crisis", "hold"])
def test_no_training_initiative_for_a_child_a_rider_in_crisis_or_on_hold(
    db_session, monkeypatch, state
):
    calls = _initiative_model(monkeypatch)
    user = _user(db_session)
    {"minor": _minor, "crisis": _crisis, "hold": _hold_all}[state](db_session, user)

    assert initiative_service.generate(db_session, user) is None
    assert initiative_service.generate(db_session, user, force_kind="ride_insight") is None
    assert calls == {"model": 0, "insight": 0}
    assert db_session.query(CoachInitiative).count() == 0


def test_a_weekly_check_in_under_a_hold_carries_no_session_count(db_session, monkeypatch):
    calls = _initiative_model(monkeypatch, insight=False)
    monkeypatch.setattr(initiative_service, "weekly_checkin_due", lambda db, u: True)
    monkeypatch.setattr(initiative_service, "_weekly_checkin_context", lambda db, u: {
        "kind": "weekly_checkin", "week": {"rides": 2, "sessions_completed": "1/4"},
    })
    seen = {}

    def fake_call(**kw):
        seen.update(kw)
        return SimpleNamespace()

    monkeypatch.setattr(initiative_service.forma_core, "call", fake_call)
    user = _user(db_session)
    _hold_all(db_session, user)

    assert initiative_service.generate(db_session, user) is not None
    assert "sessions_completed" not in seen["messages"][0]["content"]
    assert calls["insight"] == 0


@pytest.mark.parametrize("state, left", [
    ("minor", set()), ("crisis", set()), ("hold", {"weekly_checkin"}),
])
def test_a_card_raised_before_the_block_is_retired(db_session, state, left):
    user = _user(db_session)
    for kind in ("ride_insight", "weekly_checkin"):
        db_session.add(CoachInitiative(
            user_id=user.id, kind=kind, headline="h", question="q?", status="pending",
        ))
    db_session.commit()
    {"minor": _minor, "crisis": _crisis, "hold": _hold_all}[state](db_session, user)

    initiative_service.pending_initiative(db_session, user.id)

    pending = {
        i.kind for i in db_session.query(CoachInitiative).filter(CoachInitiative.status == "pending")
    }
    assert pending == left


def _briefing_model(monkeypatch):
    calls = []

    def fake_call(**kw):
        calls.append(kw)
        return SimpleNamespace()

    monkeypatch.setattr(briefing_service.forma_core, "call", fake_call)
    monkeypatch.setattr(briefing_service, "response_text", lambda r: "Easy hour, wind from the west.")
    monkeypatch.setattr(
        "app.services.metrics_service.get_current_fitness",
        lambda db, uid: {"ctl": 0, "atl": 0, "tsb": 0},
    )
    return calls


@pytest.mark.parametrize("state, expected", [
    ("minor", "Forma is for adults, 18 and over, so I can't coach you or build you a plan."),
    ("crisis", "No briefing today. There's nothing you need to do, and your plan will wait."),
    ("hold", "No briefing today. All your training is on hold. Once a doctor has checked your heart"),
])
def test_the_briefing_is_fixed_words_with_no_model_call_when_blocked(
    db_session, monkeypatch, state, expected
):
    calls = _briefing_model(monkeypatch)
    user = _user(db_session)
    db_session.add(Briefing(
        user_id=user.id, date=date.today(), kind="daily", content="Smash the VO2s today.",
    ))
    db_session.commit()
    {"minor": _minor, "crisis": _crisis, "hold": _hold_all}[state](db_session, user)

    briefing = asyncio.run(briefing_service.get_or_create_briefing(db_session, user))

    assert briefing.content.startswith(expected)
    assert "VO2" not in briefing.content
    assert calls == []
    assert not any(ch in briefing.content for ch in ("\u2014", "\u2013", "!"))


def _screened(db, user, tier):
    from app.models.safety import HealthScreening

    db.add(HealthScreening(
        user_id=user.id, version="screen-v1", answers={"q2": tier == "hold_all", "q5": True},
        long_break=False, any_yes=True, tier=tier,
    ))
    db.commit()


def test_the_held_briefing_names_what_governs_not_the_lesser_hold(db_session, monkeypatch):
    calls = _briefing_model(monkeypatch)
    full = _user(db_session, "full@example.com")
    _screened(db_session, full, "hold_all")
    _injury(db_session, full)
    text = asyncio.run(briefing_service.get_or_create_briefing(db_session, full)).content
    assert text.startswith("No briefing today. All your training is on hold. Once a doctor has cleared you")
    assert "physio" not in text

    easy = _user(db_session, "easy@example.com")
    _screened(db_session, easy, "easy_only")
    ss.open_hold(db_session, easy, "easy_only", "Six months off", "layoff", red_flag="layoff")
    text = asyncio.run(briefing_service.get_or_create_briefing(db_session, easy)).content
    assert text.startswith("No briefing today. Your plan is on easy riding only. Once a doctor has cleared you")
    assert "ease back in" not in text
    assert calls == []


def test_the_briefing_after_a_break_is_written_and_reads_the_gate(db_session, monkeypatch):
    calls = _briefing_model(monkeypatch)
    user = _user(db_session)
    _layoff_rider(db_session, user)
    briefing = asyncio.run(briefing_service.get_or_create_briefing(db_session, user))
    assert briefing.content == "Easy hour, wind from the west."
    assert '"allowed_intensity": "easy"' in calls[0]["messages"][0]["content"]


# ── Minor accounts: nothing written, generated or sent ──────────────────────


def test_no_memory_is_written_for_an_account_held_as_under_18(db_session, monkeypatch):
    calls = []
    monkeypatch.setattr(
        memory_service.forma_core, "call", lambda **kw: calls.append(kw) or SimpleNamespace()
    )
    user = _user(db_session)
    _minor(db_session, user)

    out = memory_service.extract_memories(
        db_session, user, "Rider: I'm 15 and I race for my school team every Saturday.",
        source="chat",
    )

    assert out == {"created": 0, "linked": 0}
    assert calls == []
    assert db_session.query(MemoryEntity).count() == 0


def test_plan_review_never_reads_an_account_held_as_under_18(db_session, monkeypatch):
    reached = []
    monkeypatch.setattr(plan_review_service, "_active_plan", lambda db, u: reached.append(1))
    user = _user(db_session)
    _minor(db_session, user)
    assert plan_review_service.review_plan(db_session, user) is None
    assert reached == []


def test_a_child_gets_only_the_adults_only_words_on_every_chat_path(db_session, model):
    user = _user(db_session)
    _minor(db_session, user)
    streamed = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "Can you build me a race plan?"
    )))
    voiced = _text(_run(coach_service.stream_voice_response(
        db_session, user, _session(db_session, user), "What intervals should I do?"
    )))
    synced = coach_service.get_non_streaming_response(
        db_session, user, _session(db_session, user), "How hard should I go?"
    )
    for reply in (streamed, voiced, synced):
        assert "Forma is for adults, 18 and over" in reply
    assert model["calls"] == [] and model["memory"] == 0


# ── Red-team 17: when the model fails ───────────────────────────────────────


def test_a_failed_call_is_tried_once_more_and_the_rider_gets_the_real_reply(db_session, model):
    model["plan"] = [RuntimeError("overloaded"), ["Your threshold work is coming on well."]]
    user = _user(db_session)
    text = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "How's my threshold work going?"
    )))
    assert len(model["calls"]) == 2
    assert text == "I'm Forma, your AI coach. Your threshold work is coming on well."
    assert _failed(db_session) == []


def test_an_ordinary_turn_that_fails_twice_is_recorded(db_session, model):
    model["plan"] = [RuntimeError("overloaded"), RuntimeError("overloaded")]
    user = _user(db_session)
    session = _session(db_session, user)
    text = _text(_run(coach_service.stream_response(
        db_session, user, session, "How's my threshold work going?"
    )))
    assert len(model["calls"]) == 2
    assert text.endswith(REPLY_FAILED_MESSAGE)
    [event] = _failed(db_session)
    assert (event.matched, event.source, event.user_id) == ("none", "chat", user.id)
    user_msg = db_session.query(ChatMessage).filter(ChatMessage.role == ChatRole.user).one()
    assert event.message_id == user_msg.id
    assert model["alerts"] == [] and model["memory"] == 0


def test_a_call_that_fails_after_words_reached_the_rider_is_not_repeated(db_session, model):
    model["plan"] = [(["Your threshold work is coming on. "], True)]
    user = _user(db_session)
    text = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "How's my threshold work going?"
    )))
    assert len(model["calls"]) == 1
    assert text.count("Your threshold work is coming on.") == 1
    assert len(_failed(db_session)) == 1


def test_the_quota_is_never_retried_or_counted_as_a_failure(db_session, model):
    model["plan"] = [coach_service.forma_core.BudgetExceededError(800.0, 800)]
    user = _user(db_session)
    text = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "How's my week?"
    )))
    assert len(model["calls"]) == 1
    assert coach_service.forma_core.QUOTA_MESSAGE in text
    assert _failed(db_session) == []


def test_more_than_three_failed_turns_in_ten_minutes_email_gareth_once_an_hour(db_session, model):
    riders = [_user(db_session, f"r{i}@example.com") for i in range(6)]
    for i, rider in enumerate(riders):
        model["plan"] = [RuntimeError("overloaded"), RuntimeError("overloaded")]
        _run(coach_service.stream_response(
            db_session, rider, _session(db_session, rider), "How's my week?"
        ))
        sent = [kind for kind, _ in model["alerts"]]
        assert sent == ([] if i < 3 else ["reply_failed_outage"]), i

    kind, excerpt = model["alerts"][0]
    assert "4 coach replies failed in the last 10 minutes" in excerpt
    assert "How's my week" not in excerpt
    assert len(_failed(db_session)) == 6


def test_failed_turns_outside_the_window_do_not_count(db_session, model):
    old = _user(db_session, "old@example.com")
    for _ in range(5):
        db_session.add(SafetyEvent(
            user_id=old.id, kind="reply_failed", source="chat", matched="none",
            created_at=datetime.utcnow() - timedelta(minutes=30),
        ))
    db_session.commit()
    model["plan"] = [RuntimeError("overloaded"), RuntimeError("overloaded")]
    user = _user(db_session)
    _run(coach_service.stream_response(db_session, user, _session(db_session, user), "Hi"))
    assert model["alerts"] == []


def test_an_empty_credit_balance_emails_gareth_on_the_first_failure(db_session, model):
    model["plan"] = [_credit_error()]
    user = _user(db_session)
    text = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "How's my week?"
    )))
    assert len(model["calls"]) == 1  # a second try can't top the account up
    assert text.endswith(REPLY_FAILED_MESSAGE)
    assert [kind for kind, _ in model["alerts"]] == ["reply_failed_credit"]
    assert "credit balance is too low" in model["alerts"][0][1]

    # Rate-limited: the next failure inside the hour sends nothing more.
    model["plan"] = [_credit_error()]
    other = _user(db_session, "b@example.com")
    _run(coach_service.stream_response(db_session, other, _session(db_session, other), "Hi"))
    assert len(model["alerts"]) == 1
    assert len(_failed(db_session)) == 2


def test_a_red_flag_failure_still_emails_after_an_ordinary_failure(db_session, model):
    """An ordinary failed turn must not use up the rider's red-flag alert."""
    user = _user(db_session)
    model["plan"] = [RuntimeError("down"), RuntimeError("down")]
    _run(coach_service.stream_response(db_session, user, _session(db_session, user), "Hi"))
    model["plan"] = [RuntimeError("down"), RuntimeError("down")]
    text = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "I had chest pain on the climb today"
    )))
    assert "999" in text
    assert [kind for kind, _ in model["alerts"]] == ["reply_failed"]
    assert sorted(e.matched for e in _failed(db_session)) == ["chest_pain", "none"]


def test_the_voice_path_tries_again_and_records_a_failure(db_session, model):
    model["plan"] = [RuntimeError("overloaded"), ["Ride easy today."]]
    user = _user(db_session)
    text = _text(_run(coach_service.stream_voice_response(
        db_session, user, _session(db_session, user), "What should I ride today?"
    )))
    assert text.endswith("Ride easy today.") and len(model["calls"]) == 2

    model["plan"] = [RuntimeError("overloaded"), RuntimeError("overloaded")]
    _run(coach_service.stream_voice_response(
        db_session, user, _session(db_session, user), "And tomorrow?"
    ))
    assert [(e.source, e.matched) for e in _failed(db_session)] == [("chat_voice", "none")]


def test_the_non_streaming_path_tries_again_and_records_a_failure(db_session, model):
    model["plan"] = [RuntimeError("overloaded"), ["Ride easy today."]]
    user = _user(db_session)
    reply = coach_service.get_non_streaming_response(
        db_session, user, _session(db_session, user), "What should I ride today?"
    )
    assert reply.endswith("Ride easy today.") and len(model["calls"]) == 2

    model["plan"] = [RuntimeError("overloaded"), RuntimeError("overloaded")]
    reply = coach_service.get_non_streaming_response(
        db_session, user, _session(db_session, user), "And tomorrow?"
    )
    assert reply.endswith(REPLY_FAILED_MESSAGE)
    assert [(e.source, e.matched) for e in _failed(db_session)] == [("chat_sync", "none")]


# ── The exchange kept with the safety record ────────────────────────────────

_HAS_EXCHANGE = hasattr(SafetyEvent, "rider_message") and hasattr(SafetyEvent, "coach_reply")


@pytest.mark.skipif(not _HAS_EXCHANGE, reason="safety_events has no rider_message/coach_reply yet")
def test_a_red_flag_keeps_the_riders_words_and_the_coachs_final_reply(db_session, model):
    model["plan"] = [["Stop riding for now. ", "Is it happening right now?"]]
    user = _user(db_session)
    message = "I had chest pain on the climb today"
    _run(coach_service.stream_response(db_session, user, _session(db_session, user), message))

    event = db_session.query(SafetyEvent).filter(SafetyEvent.kind == "chest_pain").one()
    saved = db_session.query(ChatMessage).filter(ChatMessage.role == ChatRole.assistant).one()
    assert event.rider_message == message
    assert event.coach_reply == saved.content
    assert "Is it happening right now?" in event.coach_reply


@pytest.mark.skipif(not _HAS_EXCHANGE, reason="safety_events has no rider_message/coach_reply yet")
def test_a_failed_red_flag_turn_keeps_the_fixed_reply_and_an_ordinary_one_keeps_nothing(
    db_session, model
):
    user = _user(db_session)
    model["plan"] = [RuntimeError("down"), RuntimeError("down")]
    _run(coach_service.stream_response(db_session, user, _session(db_session, user), "Hi there"))
    [plain] = _failed(db_session)
    assert plain.rider_message is None and plain.coach_reply is None

    model["plan"] = [RuntimeError("down"), RuntimeError("down")]
    sync = coach_service.get_non_streaming_response(
        db_session, user, _session(db_session, user), "I fainted after the ride"
    )
    events = db_session.query(SafetyEvent).filter(
        SafetyEvent.kind.in_(["fainting", "reply_failed"]), SafetyEvent.matched != "none"
    ).all()
    assert {e.kind for e in events} == {"fainting", "reply_failed"}
    assert all(e.rider_message == "I fainted after the ride" and e.coach_reply == sync for e in events)


@pytest.mark.skipif(not _HAS_EXCHANGE, reason="safety_events has no rider_message/coach_reply yet")
def test_a_hold_the_coach_applies_keeps_the_exchange_too(db_session, monkeypatch, model):
    class _ToolFinal:
        stop_reason = "tool_use"
        usage = _Usage()
        content = [SimpleNamespace(
            type="tool_use", id="t1", name="apply_safety_hold",
            input={"level": "hold_all", "reason": "Breathless at rest", "red_flag": "other"},
        )]

    class _ToolStream(_Stream):
        def get_final_message(self):
            return _ToolFinal()

    plan = [_ToolStream(["I'm putting your training on hold. "]), _Stream(["Please rest."])]

    @contextmanager
    def fake_stream(**kwargs):
        model["calls"].append(kwargs)
        yield plan.pop(0)

    monkeypatch.setattr(coach_service.forma_core, "stream", fake_stream)
    user = _user(db_session)
    message = "I get breathless just sitting on the sofa now"
    _run(coach_service.stream_response(db_session, user, _session(db_session, user), message))

    event = db_session.query(SafetyEvent).filter(SafetyEvent.source == "coach_tool").one()
    assert event.rider_message == message and "Please rest." in event.coach_reply


# ── The AI disclosure at the start of every chat ────────────────────────────


def test_every_new_chat_opens_with_the_ai_disclosure(db_session, model):
    model["plan"] = [["Morning."], ["Sure."], ["Hello again."]]
    user = _user(db_session)
    first = _session(db_session, user)
    one = _text(_run(coach_service.stream_response(db_session, user, first, "Hi coach")))
    two = _text(_run(coach_service.stream_response(db_session, user, first, "And another")))
    later = _session(db_session, user)
    three = _text(_run(coach_service.stream_response(db_session, user, later, "Back again")))

    assert one == "I'm Forma, your AI coach. Morning."
    assert "AI coach" not in two
    assert three == "I'm Forma, your AI coach. Hello again."
    notes = model["calls"][2]["system"][-1]["text"]
    assert "first reply in a new chat with a rider you have coached before" in notes
    saved = (
        db_session.query(ChatMessage)
        .filter(ChatMessage.session_id == later.id, ChatMessage.role == ChatRole.assistant)
        .one()
    )
    assert saved.content == "I'm Forma, your AI coach. Hello again."


def test_a_new_chat_on_the_voice_and_non_streaming_paths_says_it_is_ai(db_session, model):
    user = _user(db_session, coach_name="Marco")
    _run(coach_service.stream_response(db_session, user, _session(db_session, user), "Hi"))
    voice = _text(_run(coach_service.stream_voice_response(
        db_session, user, _session(db_session, user), "Hi"
    )))
    sync = coach_service.get_non_streaming_response(
        db_session, user, _session(db_session, user), "Hi"
    )
    assert voice.startswith("I'm Marco, your AI coach in Forma. ")
    assert sync.startswith("I'm Marco, your AI coach in Forma. ")


# ── Round 4: the safety classifier, wired into every chat path ──────────────

_REAL_LOAD_CLASSIFIER = coach_service._load_classifier


class _FakeClassifier:
    """A scripted classifier. `adds` are (kind, matched, severity[, age])
    hits it finds that the regex may have missed; `drops` are kinds it sets
    aside. `error` makes the call fail; `gate` makes it wait until set."""

    def __init__(self):
        self.calls: list[dict] = []
        self.merged_with: list[list] = []
        self.adds: list[tuple] = []
        self.drops: set[str] = set()
        self.error: BaseException | None = None
        self.gate: threading.Event | None = None
        self.verdict_none = False

    def classify_safety(self, text, recent=None, country=None, *, user_id=None, surface="coach"):
        self.calls.append({
            "text": text, "context": list(recent or []), "country": country,
            "user_id": user_id, "surface": surface,
        })
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        if self.verdict_none:
            return None
        return {"adds": list(self.adds), "drops": set(self.drops)}

    def merge(self, regex_hits, verdict):
        self.merged_with.append(list(regex_hits))
        kept = [h for h in regex_hits if h.kind not in verdict["drops"]]
        found = {h.kind for h in kept}
        for kind, matched, severity, *age in verdict["adds"]:
            if kind not in found:
                kept.append(safety_screen.Hit(kind, matched, severity, age[0] if age else None))
        return kept


@pytest.fixture
def classifier(monkeypatch, model):
    fake = _FakeClassifier()
    monkeypatch.setattr(
        coach_service, "_load_classifier", lambda: (fake.classify_safety, fake.merge)
    )
    yield fake
    if fake.gate is not None:
        fake.gate.set()  # let any thread still waiting finish


def _cards(chunks) -> list[str]:
    return [c.get("card") for c in chunks if c["type"] == "safety"]


def _first_text_at(chunks) -> int:
    return next(i for i, c in enumerate(chunks) if c["type"] == "text")


_DASHES = re.compile("[\u2013\u2014]")


def _events(db, kind=None) -> list[SafetyEvent]:
    q = db.query(SafetyEvent)
    if kind is not None:
        q = q.filter(SafetyEvent.kind == kind)
    return q.all()


# Words the regex is built never to read as a red flag, so anything the turn
# does about them came from the classifier.
_OBLIQUE_CRISIS = "Sorted my will out and gave the dog to my sister, so the plan won't matter after Sunday"
_OBLIQUE_CHEST = "Something felt very wrong in my ribs on the climb and I had to sit on the verge"


def test_a_crisis_the_classifier_finds_gets_the_card_first_a_record_and_an_alert(
    db_session, model, classifier
):
    assert safety_screen._detect(_OBLIQUE_CRISIS) == []
    classifier.adds = [("crisis", "gave the dog to my sister", "crisis")]
    model["plan"] = [["I'm really glad you told me. ", "Are you thinking of ending your life?"]]
    user = _user(db_session)

    chunks = _run(coach_service.stream_response(
        db_session, user, _session(db_session, user), _OBLIQUE_CRISIS
    ))

    assert _cards(chunks) == ["crisis"]
    assert chunks[0]["type"] == "safety" and chunks[0]["kind"] == "crisis"
    assert chunks[0]["text"] == safety_screen.card_text("crisis")
    [event] = _events(db_session, "crisis")
    assert (event.source, event.card_shown, event.matched) == (
        "classifier", "crisis", "gave the dog to my sister"
    )
    assert model["alerts"] == [("crisis", _OBLIQUE_CRISIS)]
    notes = model["calls"][0]["system"][-1]["text"]
    assert "SAFETY CONTEXT" in notes and '- crisis, matched "gave the dog to my sister"' in notes
    assert classifier.calls[0]["text"] == _OBLIQUE_CRISIS
    assert classifier.calls[0]["user_id"] == user.id


def test_a_red_flag_the_classifier_adds_holds_the_plan_like_a_regex_one(
    db_session, model, classifier
):
    assert safety_screen._detect(_OBLIQUE_CHEST) == []
    classifier.adds = [("chest_pain", "very wrong in my ribs on the climb", "emergency")]
    model["plan"] = [["No. Is it happening now?"]]
    user = _user(db_session)
    planned = _workout(db_session, user, VO2MAX_5x5)

    chunks = _run(coach_service.stream_response(
        db_session, user, _session(db_session, user), _OBLIQUE_CHEST
    ))

    assert _cards(chunks) == ["chest"]
    assert chunks[0]["type"] == "safety" and chunks[0]["kind"] == "emergency"
    hold = ss.current_hold(db_session, user.id)
    assert (hold.level, hold.red_flag, hold.source) == ("hold_all", "chest_pain", "detector")
    [event] = _events(db_session, "chest_pain")
    assert (event.source, event.hold_id, event.card_shown) == ("classifier", hold.id, "emergency")
    assert "hold_all hold opened" in model["calls"][0]["system"][-1]["text"]
    assert ss.allowed_intensity(db_session, user) == "none"
    refused = coach_service._execute_tool(db_session, user, "update_workout", {
        "workout_id": planned.id, "workout_type": "vo2max",
    })
    assert refused.startswith("Not changed:")


def test_a_card_the_classifier_adds_comes_before_any_text_on_voice_and_sync(
    db_session, model, classifier
):
    classifier.adds = [("crisis", "gave the dog to my sister", "crisis")]
    user = _user(db_session)
    voice = _run(coach_service.stream_voice_response(
        db_session, user, _session(db_session, user), _OBLIQUE_CRISIS
    ))
    assert _cards(voice) == ["crisis"] and voice[0]["type"] == "safety"
    assert voice.index(next(c for c in voice if c["type"] == "safety")) < _first_text_at(voice)

    sync = coach_service.get_non_streaming_response(
        db_session, user, _session(db_session, user), _OBLIQUE_CRISIS
    )
    assert sync.startswith(safety_screen.card_text("crisis") + "\n\n")
    assert {e.source for e in _events(db_session, "crisis")} == {"classifier"}
    assert [c["surface"] for c in classifier.calls] == ["coach_voice", "coach"]


def test_the_regex_emergency_card_goes_out_before_the_classifier_is_asked(
    db_session, model, classifier
):
    classifier.adds = [("crisis", "no point going on", "crisis")]
    user = _user(db_session)
    session = _session(db_session, user)
    message = "I had chest pain on the climb today"

    async def first_then_rest():
        agen = coach_service.stream_response(db_session, user, session, message)
        first = json.loads((await agen.__anext__())[len("data: "):])
        asked_before_first = len(classifier.calls)
        rest = [json.loads(c[len("data: "):]) async for c in agen]
        return first, asked_before_first, rest

    first, asked_before_first, rest = asyncio.run(first_then_rest())

    assert (first["type"], first["kind"], first["card"]) == ("safety", "emergency", "chest")
    assert asked_before_first == 0  # the card never waited on the classifier
    assert len(classifier.calls) == 1
    chunks = [first, *rest]
    # The crisis card the classifier added follows, still before any text,
    # and the chest card is never sent twice.
    assert _cards(chunks) == ["chest", "crisis"]
    assert max(i for i, c in enumerate(chunks) if c["type"] == "safety") < _first_text_at(chunks)
    assert {(e.kind, e.source) for e in _events(db_session)} == {
        ("chest_pain", "chat"), ("crisis", "classifier"),
    }


def test_a_later_emergency_card_keeps_the_one_already_shown_on_the_record(
    db_session, model, classifier
):
    """The regex showed the head card; the classifier adds chest pain, whose
    card ranks higher. Both were shown, so both stay on the record."""
    classifier.adds = [("chest_pain", "tight across the chest", "emergency")]
    user = _user(db_session)
    session = _session(db_session, user)
    chunks = _run(coach_service.stream_response(
        db_session, user, session, "Crashed and hit my head this morning"
    ))
    assert _cards(chunks) == ["head", "chest"]
    notes = model["calls"][0]["system"][-1]["text"]
    assert notes.count("The app has already shown the rider this emergency card") == 2
    saved = db_session.query(ChatMessage).filter(ChatMessage.role == ChatRole.assistant).one()
    assert saved.context_snapshot["safety_cards"] == ["emergency", "emergency"]


def _regex_misreads_hay_fever(monkeypatch):
    """Pin the regex to the misreading the round 3 sweep found ("hay fever"
    opened a fever hold), so the test shows the classifier setting a regex hit
    aside however the regex's own rules change."""
    real = safety_screen._detect

    def detect(text):
        hits = real(text)
        if "fever" in safety_screen._normalise(text) and "fever" not in {h.kind for h in hits}:
            hits = [*hits, safety_screen.Hit("fever", "fever", "urgent")]
        return hits

    monkeypatch.setattr(safety_screen, "_detect", detect)


def test_hay_fever_the_classifier_sets_aside_opens_no_hold_and_shows_no_card(
    db_session, model, classifier, monkeypatch
):
    _regex_misreads_hay_fever(monkeypatch)
    message = "My hay fever is playing up, eyes streaming on the ride"
    regex = [h.kind for h in safety_screen._detect(message)]
    assert regex == ["fever"]
    classifier.drops = {"fever"}
    model["plan"] = [["Antihistamines are fine to ride on."]]
    user = _user(db_session)

    chunks = _run(coach_service.stream_response(
        db_session, user, _session(db_session, user), message
    ))

    assert [[h.kind for h in hits] for hits in classifier.merged_with] == [regex]
    assert _cards(chunks) == []
    assert ss.current_hold(db_session, user.id) is None
    assert _events(db_session) == []
    assert model["alerts"] == []
    assert "SAFETY CONTEXT" not in (model["calls"][0]["system"][-1]["text"] or "")
    assert _text(chunks).endswith("Antihistamines are fine to ride on.")


def test_the_classifier_never_takes_back_an_emergency_card_already_shown(
    db_session, model, classifier
):
    classifier.drops = {"chest_pain"}
    user = _user(db_session)
    chunks = _run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "I had chest pain on the climb today"
    ))
    assert _cards(chunks) == ["chest"]
    assert ss.current_hold(db_session, user.id).red_flag == "chest_pain"
    [event] = _events(db_session, "chest_pain")
    assert event.source == "chat"


def test_the_classifier_never_takes_back_an_under_18_reading(db_session, model, classifier):
    classifier.drops = {"minor"}
    user = _user(db_session)
    reply = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "I'm 15 and want to race"
    )))
    assert "Forma is for adults, 18 and over" in reply
    assert ss.minor_hold(db_session, user) is not None
    assert classifier.calls == []  # a child's words never reach a model


_FEVER = "I've got a fever, can I still do the intervals tomorrow?"


@pytest.mark.parametrize("failure", [
    "no_module", "no_functions", "raises", "times_out", "no_verdict", "merge_raises",
])
def test_an_unavailable_classifier_leaves_the_regex_check_as_it_was(
    db_session, model, classifier, monkeypatch, failure
):
    import app.services as services

    if failure == "no_module":
        monkeypatch.setattr(coach_service, "_load_classifier", _REAL_LOAD_CLASSIFIER)
        monkeypatch.delattr(services, "safety_classifier", raising=False)
        monkeypatch.setitem(sys.modules, "app.services.safety_classifier", None)
    elif failure == "no_functions":
        import types as _types

        empty = _types.ModuleType("app.services.safety_classifier")
        monkeypatch.setattr(coach_service, "_load_classifier", _REAL_LOAD_CLASSIFIER)
        monkeypatch.setattr(services, "safety_classifier", empty, raising=False)
        monkeypatch.setitem(sys.modules, "app.services.safety_classifier", empty)
    elif failure == "raises":
        classifier.error = RuntimeError("provider down")
    elif failure == "times_out":
        classifier.gate = threading.Event()
        monkeypatch.setattr(coach_service, "CLASSIFIER_WAIT_SECONDS", 0.05)
    elif failure == "no_verdict":
        classifier.verdict_none = True
    elif failure == "merge_raises":
        def broken_merge(regex_hits, verdict):
            raise ValueError("bad verdict")
        monkeypatch.setattr(
            coach_service, "_load_classifier",
            lambda: (classifier.classify_safety, broken_merge),
        )
    model["plan"] = [["No. Rest until the fever has gone."]]
    user = _user(db_session)

    chunks = _run(coach_service.stream_response(
        db_session, user, _session(db_session, user), _FEVER
    ))

    assert _cards(chunks) == ["fever"]
    assert ss.current_hold(db_session, user.id).red_flag == "fever"
    assert [(e.kind, e.source) for e in _events(db_session)] == [("fever", "chat")]
    assert "I'm Forma, your AI coach. No. Rest until the fever has gone." in _text(chunks)
    assert "SAFETY CONTEXT" in model["calls"][0]["system"][-1]["text"]


def test_a_classifier_that_times_out_never_holds_up_the_sync_reply(
    db_session, model, classifier, monkeypatch
):
    classifier.gate = threading.Event()
    monkeypatch.setattr(coach_service, "CLASSIFIER_WAIT_SECONDS", 0.05)
    user = _user(db_session)
    reply = coach_service.get_non_streaming_response(
        db_session, user, _session(db_session, user), "How hard should Saturday be?"
    )
    assert reply.endswith("Fine.") and len(classifier.calls) == 1


def test_the_classifier_reads_the_riders_last_two_messages_for_context(
    db_session, model, classifier
):
    user = _user(db_session, country="GB")
    session = _session(db_session, user)
    then = datetime.utcnow() - timedelta(minutes=10)
    for i, (role, content) in enumerate([
        (ChatRole.user, "First message"),
        (ChatRole.assistant, "Coach reply one"),
        (ChatRole.user, "Second message"),
        (ChatRole.assistant, "Coach reply two"),
        (ChatRole.user, "Third message"),
        (ChatRole.assistant, "Coach reply three"),
    ]):
        db_session.add(ChatMessage(
            session_id=session.id, role=role, content=content,
            created_at=then + timedelta(minutes=i),
        ))
    other = _session(db_session, user)
    db_session.add(ChatMessage(
        session_id=other.id, role=ChatRole.user, content="Another chat",
        created_at=then + timedelta(minutes=8),
    ))
    db_session.commit()

    _run(coach_service.stream_response(db_session, user, session, "Fourth message"))

    [call] = classifier.calls
    assert call["text"] == "Fourth message"
    assert call["context"] == ["Second message", "Third message"]
    assert (call["country"], call["user_id"], call["surface"]) == ("GB", user.id, "coach")


def test_an_account_held_as_under_18_never_reaches_the_classifier_on_any_path(
    db_session, model, classifier
):
    classifier.adds = [("crisis", "anything", "crisis")]
    user = _user(db_session)
    _minor(db_session, user)
    for reply in (
        _text(_run(coach_service.stream_response(
            db_session, user, _session(db_session, user), "Can you build me a race plan?"
        ))),
        _text(_run(coach_service.stream_voice_response(
            db_session, user, _session(db_session, user), "What intervals should I do?"
        ))),
        coach_service.get_non_streaming_response(
            db_session, user, _session(db_session, user), "How hard should I go?"
        ),
    ):
        assert "Forma is for adults, 18 and over" in reply
    assert classifier.calls == []
    assert model["calls"] == [] and model["memory"] == 0
    assert _events(db_session, "crisis") == []


def test_an_under_18_reading_the_classifier_adds_holds_the_account(
    db_session, model, classifier
):
    classifier.adds = [("minor", "my dad set this account up", "minor")]
    user = _user(db_session)
    reply = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "My dad set this account up for me"
    )))
    assert "Forma is for adults, 18 and over" in reply
    hold = ss.minor_hold(db_session, user)
    assert hold is not None
    [event] = _events(db_session, "minor")
    assert (event.source, event.hold_id) == ("classifier", hold.id)
    assert model["calls"] == []
    assert [kind for kind, _ in model["alerts"]] == ["minor"]


# ── New problem E: an admission inside the quiet window after an admin lift ─


def _lifted_adult(db, user):
    """Forma reviewed an under-18 hold by hand and lifted it: an adult."""
    hold = _minor(db, user)
    ss.admin_lift(db, hold.id, "Adult, the check misread them", sync_plan=False)
    assert ss.minor_quiet_until(db, user) is not None
    return hold


def test_an_admission_the_regex_reads_after_a_lift_is_recorded_and_sent_to_gareth(
    db_session, model, classifier
):
    user = _user(db_session)
    _lifted_adult(db_session, user)
    message = "I'm in year 10 at school and want to get faster"
    model["plan"] = [["Let's start with your threshold."]]

    reply = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), message
    )))

    [event] = _events(db_session, "minor")
    assert (event.source, event.hold_id, event.card_shown) == ("chat", None, None)
    assert event.stated_age == 14
    assert event.rider_message == message
    assert ss.minor_hold(db_session, user) is None  # no automatic hold: Gareth decides
    [(kind, excerpt)] = model["alerts"]
    assert kind == "minor"
    assert excerpt.startswith("No hold opened: you lifted an under-18 hold on this account")
    assert excerpt.endswith(f"They wrote: {message}")
    assert not _DASHES.search(excerpt)
    assert "Forma is for adults" not in reply
    assert reply.endswith("Let's start with your threshold.")


@pytest.mark.parametrize("message,matched,age", [
    ("honestly I'm actually 15, my dad set this account up with his details", "I'm actually 15", 15),
    ("I'm actually 15", "I'm actually 15", 15),
    ("my dad set this account up", "my dad set this account up", None),
])
def test_an_admission_the_classifier_reads_after_a_lift_is_recorded_and_sent_to_gareth(
    db_session, model, classifier, message, matched, age
):
    classifier.adds = [("minor", matched, "minor", age)]
    regex_reads_it = "minor" in {h.kind for h in safety_screen._detect(message)}
    user = _user(db_session)
    _lifted_adult(db_session, user)

    reply = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), message
    )))

    [event] = _events(db_session, "minor")
    assert (event.hold_id, event.card_shown, event.rider_message) == (None, None, message)
    if regex_reads_it:  # the regex may learn these words; then it is the regex's hit
        assert event.source == "chat"
    else:
        assert (event.source, event.matched, event.stated_age) == ("classifier", matched, age)
    assert ss.minor_hold(db_session, user) is None
    assert [kind for kind, _ in model["alerts"]] == ["minor"]
    assert "Forma is for adults" not in reply
    assert len(classifier.calls) == 1


def test_a_second_admission_in_the_same_half_hour_is_recorded_but_not_sent_again(
    db_session, model, classifier
):
    user = _user(db_session)
    _lifted_adult(db_session, user)
    session = _session(db_session, user)
    _run(coach_service.stream_response(db_session, user, session, "I'm in year 10 at school"))
    _run(coach_service.stream_response(db_session, user, session, "I'm in year 10, honestly"))
    assert [e.hold_id for e in _events(db_session, "minor")] == [None, None]
    assert [kind for kind, _ in model["alerts"]] == ["minor"]


def test_after_the_quiet_window_an_admission_holds_the_account_again(
    db_session, model, classifier, monkeypatch
):
    user = _user(db_session)
    _lifted_adult(db_session, user)
    later = datetime.utcnow() + timedelta(days=ss.MINOR_QUIET_DAYS + 1)
    monkeypatch.setattr(ss, "_now", lambda: later)
    reply = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "I'm in year 10 at school"
    )))
    assert "Forma is for adults, 18 and over" in reply
    assert ss.minor_hold(db_session, user) is not None
    assert classifier.calls == []


# ── The real classifier and merge, with only the model faked ───────────────


@pytest.fixture
def haiku(monkeypatch, model):
    """The classifier module as it ships, with a fake model behind
    forma_core.call for its task: `reply` is the tool input it answers with,
    `delay` how long it takes. The real provider client is never reached."""
    sc = pytest.importorskip("app.services.safety_classifier")
    state = {
        "reply": {"flags": [], "stated_age": None, "benign_reasons": []},
        "calls": [], "delay": 0.0, "module": sc,
    }
    chat_call = coach_service.forma_core.call  # the `model` fixture's fake

    def fake_call(**kwargs):
        if kwargs.get("task") != sc.TASK:
            return chat_call(**kwargs)
        state["calls"].append(kwargs)
        if state["delay"]:
            threading.Event().wait(state["delay"])
        return SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(
            type="tool_use", name=sc.TOOL_NAME, input=state["reply"], id="t1",
        )])

    def refuse():
        raise AssertionError("a test tried to reach the real model provider")

    monkeypatch.setattr(coach_service.forma_core, "call", fake_call)
    monkeypatch.setattr(coach_service.forma_core, "_client", refuse)
    monkeypatch.setattr(coach_service, "_load_classifier", _REAL_LOAD_CLASSIFIER)
    return state


def _flag(kind, quote, severity):
    return {"kind": kind, "severity": severity, "about_rider": True, "current": True, "quote": quote}


def test_shipped_classifier_hay_fever_is_set_aside(db_session, model, haiku, monkeypatch):
    _regex_misreads_hay_fever(monkeypatch)
    haiku["reply"] = {"flags": [], "stated_age": None, "benign_reasons": [
        {"kind": "fever", "reason": "hay fever is an allergy, not a fever", "quote": "hay fever"},
    ]}
    model["plan"] = [["Antihistamines are fine to ride on."]]
    user = _user(db_session)
    chunks = _run(coach_service.stream_response(
        db_session, user, _session(db_session, user),
        "My hay fever is playing up, eyes streaming on the ride",
    ))
    assert len(haiku["calls"]) == 1
    assert _cards(chunks) == []
    assert ss.current_hold(db_session, user.id) is None
    assert _events(db_session) == []
    assert _text(chunks).endswith("Antihistamines are fine to ride on.")


def test_shipped_classifier_a_real_fever_still_holds(db_session, model, haiku):
    haiku["reply"] = {"flags": [_flag("fever", "I've got a fever", "urgent")],
                      "stated_age": None, "benign_reasons": []}
    user = _user(db_session)
    chunks = _run(coach_service.stream_response(
        db_session, user, _session(db_session, user), _FEVER
    ))
    assert _cards(chunks) == ["fever"]
    assert ss.current_hold(db_session, user.id).red_flag == "fever"
    assert [(e.kind, e.source) for e in _events(db_session)] == [("fever", "chat")]


def test_shipped_classifier_an_added_crisis_gets_the_card_first(db_session, model, haiku):
    haiku["reply"] = {"flags": [_flag("crisis", "gave the dog to my sister", "crisis")],
                      "stated_age": None, "benign_reasons": []}
    user = _user(db_session)
    chunks = _run(coach_service.stream_response(
        db_session, user, _session(db_session, user), _OBLIQUE_CRISIS
    ))
    assert _cards(chunks) == ["crisis"] and chunks[0]["type"] == "safety"
    [event] = _events(db_session, "crisis")
    assert (event.source, event.matched) == ("classifier", "gave the dog to my sister")
    assert model["alerts"] == [("crisis", _OBLIQUE_CRISIS)]
    assert haiku["calls"][0]["surface"] == "coach"


def test_shipped_classifier_that_times_out_leaves_the_regex_alone(
    db_session, model, haiku, monkeypatch
):
    monkeypatch.setattr(haiku["module"], "deadline_seconds", lambda: 0.05)
    haiku["delay"] = 0.5
    haiku["reply"] = {"flags": [], "stated_age": None, "benign_reasons": [
        {"kind": "fever", "reason": "not a fever", "quote": "fever"},
    ]}
    user = _user(db_session)
    chunks = _run(coach_service.stream_response(
        db_session, user, _session(db_session, user), _FEVER
    ))
    assert _cards(chunks) == ["fever"]
    assert ss.current_hold(db_session, user.id).red_flag == "fever"
    assert "Fine." in _text(chunks)


def test_shipped_classifier_is_never_called_for_an_account_held_as_under_18(
    db_session, model, haiku
):
    user = _user(db_session)
    _minor(db_session, user)
    _run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "Can you build me a race plan?"
    ))
    coach_service.get_non_streaming_response(
        db_session, user, _session(db_session, user), "How hard should I go?"
    )
    assert haiku["calls"] == [] and model["calls"] == []


@pytest.mark.parametrize("message,quote,age", [
    ("my dad set this account up", "my dad set this account up", None),
    ("honestly I'm actually 15, my dad set this account up with his details", "I'm actually 15", 15),
])
def test_shipped_classifier_an_admission_after_a_lift_goes_to_gareth(
    db_session, model, haiku, message, quote, age
):
    haiku["reply"] = {"flags": [_flag("minor", quote, "minor")], "stated_age": age,
                      "benign_reasons": []}
    regex_reads_it = "minor" in {h.kind for h in safety_screen._detect(message)}
    user = _user(db_session)
    _lifted_adult(db_session, user)
    model["plan"] = [["Let's look at your week."]]

    reply = _text(_run(coach_service.stream_response(
        db_session, user, _session(db_session, user), message
    )))

    [event] = _events(db_session, "minor")
    assert (event.hold_id, event.rider_message) == (None, message)
    if not regex_reads_it:
        assert (event.source, event.matched.lower(), event.stated_age) == (
            "classifier", quote.lower(), age
        )
    assert ss.minor_hold(db_session, user) is None
    [(kind, excerpt)] = model["alerts"]
    assert kind == "minor" and excerpt.startswith("No hold opened")
    assert reply.endswith("Let's look at your week.")


def test_shipped_classifier_an_ageless_admission_outside_the_window_opens_nothing(
    db_session, model, haiku
):
    """merge() needs a stated age under 18 before it opens an under-18 hold;
    outside the quiet window there is nothing else to record."""
    haiku["reply"] = {"flags": [_flag("minor", "my dad set this account up", "minor")],
                      "stated_age": None, "benign_reasons": []}
    user = _user(db_session)
    _run(coach_service.stream_response(
        db_session, user, _session(db_session, user), "my dad set this account up"
    ))
    assert _events(db_session, "minor") == [] and ss.minor_hold(db_session, user) is None
