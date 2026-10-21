"""The joins the merge review found open, pinned.

Main's season keeper and goal tools build plans with no rider action, and
its booking flow confirms in a few words; the safety branch's record (an
injury hold, eating very little, words of hopelessness, an account held as
under 18) and its reply check were written before either existed. Each test
below is a case where the two disagreed.
"""

import asyncio
import json
from datetime import date, datetime, timedelta

import pytest

from app.api.v1 import goals as goals_api
from app.models.chat import ChatMessage, ChatRole, ChatSession
from app.models.onboarding import EventPriority, GoalStatus
from app.models.safety import SafetyEvent, SafetyHold
from app.models.training import TrainingPlan, Workout
from app.models.user import User
from app.schemas.onboarding import GoalAssessmentSubmit
from app.services import attachment_service, coach_service, file_triage_service, safety_screen
from app.services import safety_service as ss
from app.services.coach_service import _build_rider_context, _execute_tool
from app.services.safety_screen import ReplyGuard
from app.services.season_service import BLOCK_NAME, ensure_all, season_note, season_state
from tests.test_chat_file_triage import PB, PB_BYTES
from tests.test_season_service import (  # noqa: F401  (clock is a fixture)
    FRIDAY, _active, _gareth, _goal, _incident, _old_plan, clock,
)

HARD = {"tempo", "sweet_spot", "threshold", "vo2max", "sprint", "anaerobic"}


def _type(w: Workout) -> str:
    return getattr(w.workout_type, "value", w.workout_type)


# ── What the safety record holds back ───────────────────────────────────────


def _injured(db, user):
    ss.open_hold(db, user, "easy_only", "Knee pain", "detector", red_flag="injury")


def _event(kind):
    def record(db, user):
        db.add(SafetyEvent(user_id=user.id, kind=kind, source="detector", matched=kind))
        db.commit()
    return record


WAITS = {"injury": _injured, "restriction": _event("restriction"), "wellbeing": _event("crisis")}


def _clear(db, user, why):
    """The wait is over: the hold lifted, or the words long enough ago."""
    if why == "injury":
        for hold in db.query(SafetyHold).filter(SafetyHold.user_id == user.id):
            hold.lifted_at = datetime.utcnow()
    else:
        for event in db.query(SafetyEvent).filter(SafetyEvent.user_id == user.id):
            event.created_at = datetime.utcnow() - timedelta(days=40)
    db.commit()


def _ahead(db, user) -> int:
    return db.query(Workout).filter(
        Workout.user_id == user.id, Workout.scheduled_date > FRIDAY
    ).count()


@pytest.mark.parametrize("why", sorted(WAITS))
@pytest.mark.parametrize("tool", ["create_goal", "update_goal"])
def test_a_goal_filed_in_chat_builds_no_plan_while_the_safety_record_says_wait(
    db_session, clock, why, tool
):
    """Sa Calobra on 16 Oct with the plan ended. add_workout was refused under
    the same record, yet create_goal rebuilt 20 weeks of training around a
    hill climb (and, under an injury hold, 162 rides)."""
    gareth, goal, _ = _incident(db_session)
    WAITS[why](db_session, gareth)

    if tool == "create_goal":
        result = _execute_tool(db_session, gareth, "create_goal", {
            "event_name": "Mallorca 312", "event_date": "2027-05-30",
            "event_type": "gran_fondo", "priority": "a_race",
        })
    else:
        result = _execute_tool(db_session, gareth, "update_goal", {
            "goal_id": goal.id, "event_date": "2026-11-29",
        })

    assert "The plan was NOT rebuilt" in result
    assert "The plan has been rebuilt" not in result and "base block" not in result
    assert ("Build my season" in result) == (why == "wellbeing")
    assert _active(db_session, gareth) == [] and _ahead(db_session, gareth) == 0

    # Once the wait is over, the keeper builds the goal's plan on its own.
    _clear(db_session, gareth, why)
    assert ensure_all(db_session) == {"goal_plan": 1}


@pytest.mark.parametrize("why", sorted(WAITS))
def test_the_keeper_builds_no_block_while_the_safety_record_says_wait(db_session, clock, why):
    gareth, _, _ = _incident(db_session)
    WAITS[why](db_session, gareth)

    assert ensure_all(db_session, dry_run=True) == {"held": 1}
    assert ensure_all(db_session) == {"held": 1}
    assert _active(db_session, gareth) == [] and _ahead(db_session, gareth) == 0
    state = season_state(db_session, gareth)
    assert state["held_back"] == why and state["rebuilds"] is False

    _clear(db_session, gareth, why)
    assert ensure_all(db_session) == {"block": 1}
    assert [p.name for p in _active(db_session, gareth)] == [BLOCK_NAME]


@pytest.mark.parametrize("why", sorted(WAITS))
def test_the_season_note_pitches_no_goal_while_the_safety_record_says_wait(db_session, clock, why):
    gareth, goal, _ = _incident(db_session)
    goal.status = GoalStatus.completed
    db_session.commit()
    WAITS[why](db_session, gareth)

    state = season_state(db_session, gareth)
    in_chat, morning = season_note(state), season_note(state, in_chat=False)
    for note in (in_chat, morning):
        assert "Propose the next goal" not in note
        assert "Do not propose a new goal." in note
    assert "rebuilt around it at once" not in in_chat
    assert "how they are" in morning

    context = json.loads(_build_rider_context(db_session, gareth))
    assert "Propose the next goal" not in context["season"]["note"]
    if why == "wellbeing":
        assert "wellbeing" in context["safety"]


# ── A DNF or DNS is followed by an easy week ────────────────────────────────


@pytest.mark.parametrize("status, days, priority, reason", [
    ("dns", 20, EventPriority.a_race, "injury"),   # filed ahead of the day
    ("dns", -2, EventPriority.a_race, "illness"),
    ("dnf", -2, EventPriority.c_race, "crash"),    # a C race gets no recovery week of its own
], ids=["dns-injury-ahead", "dns-illness", "dnf-crash-c-race"])
def test_the_block_after_a_dnf_or_dns_the_body_caused_opens_with_an_easy_week(
    db_session, clock, status, days, priority, reason
):
    """Filed as DNS for injury, the block once started at once with tempo,
    under a screen saying the next step was one easy ride, not a new plan.
    A DNS for any other reason was never ridden, and main's rule (no
    recovery week) stands: test_season_service pins it."""
    gareth = _gareth(db_session)
    race_day = FRIDAY + timedelta(days=days)
    goal = _goal(db_session, gareth, race_day, name="Race A", priority=priority)
    _old_plan(db_session, gareth, goal, start=FRIDAY - timedelta(days=60), end=race_day)

    filed = goals_api.submit_goal_assessment(
        goal.id,
        GoalAssessmentSubmit(status=status, overall_satisfaction=4, assessment_data={
            "dns_reason" if status == "dns" else "dnf_reason": reason,
            "check_in": True,
        }),
        current_user=gareth, db=db_session,
    )

    assert filed.plan_rebuilt is True
    [block] = _active(db_session, gareth)
    assert block.name == BLOCK_NAME
    first_week = db_session.query(Workout).filter(
        Workout.user_id == gareth.id,
        Workout.scheduled_date >= FRIDAY,
        Workout.scheduled_date < FRIDAY + timedelta(days=7),
    ).all()
    assert first_week and not {_type(w) for w in first_week} & HARD


# ── The reply check and a goal that rebuilt the plan ────────────────────────


def _layoff_guard() -> ReplyGuard:
    hold = SafetyHold(level="easy_only", red_flag="layoff", reason="Back from a break")
    return ReplyGuard(None, None, lambda: hold, since=datetime.utcnow() - timedelta(minutes=1))


def test_a_rebuild_claim_goes_out_once_a_goal_tool_rebuilt_the_plan():
    guard = _layoff_guard()
    assert guard.safety_turn
    claim = "I've rebuilt your plan around the Mallorca 312. "
    assert guard.feed(claim) + guard.before_tools() == ""
    out = guard.after_tools([("create_goal", (
        "Filed the goal 'Mallorca 312' for Sunday 30 May 2027 (ID: g1). The plan has "
        "been rebuilt around it: 'Plan for Mallorca 312', Friday 16 October to Sunday "
        "30 May 2027."
    ))])
    assert out == claim

    # A goal filed with the plan held back makes the same words untrue.
    guard = _layoff_guard()
    guard.feed(claim)
    guard.before_tools()
    out = guard.after_tools([("create_goal", "Filed the goal. " + coach_service._HELD_PLAN["injury"])])
    assert out == "" and claim.strip() in guard.dropped


# ── Words about a booking ───────────────────────────────────────────────────


INTRO = "I'm Forma, your AI coach. "


@pytest.fixture
def rider(db_session):
    user = User(email="rider@example.com", hashed_password="x", full_name="Sam Rider", ftp=250)
    db_session.add(user)
    db_session.commit()
    return user


def _send(db, user, text, voice=False) -> tuple[list[dict], str, ChatMessage]:
    chat = ChatSession(user_id=user.id, title="Chat")
    db.add(chat)
    db.commit()
    stream = coach_service.stream_voice_response if voice else coach_service.stream_response

    async def run():
        return [c async for c in stream(db, user, chat, text)]

    chunks = [json.loads(c[len("data: "):]) for c in asyncio.run(run())]
    said = "".join(c["content"] for c in chunks if c["type"] == "text")
    stored = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == chat.id, ChatMessage.role == ChatRole.assistant)
        .one()
    )
    return chunks, said.removeprefix(INTRO), stored


def _booking(tool_use, workout_type="threshold"):
    return tool_use("add_workout", {
        "scheduled_date": (date.today() + timedelta(days=3)).isoformat(),
        "title": "PB reps", "workout_type": workout_type,
    })


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
@pytest.mark.parametrize("confirmation, shown", [
    ("Done, it's booked for Monday 19 October.", "It's booked for Monday 19 October."),
    ("All set, PB reps is on Monday 19 October.", "PB reps is on Monday 19 October."),
])
def test_a_short_confirmation_after_a_booking_reaches_the_rider(
    db_session, rider, scripted_model, voice, confirmation, shown
):
    """The guard dropped any "Done, ..." of 12 words or fewer after a tool: the
    session was booked, and the rider was told the coach could not answer."""
    scripted_model.append(([], [_booking(scripted_model.tool_use, "endurance")]))
    scripted_model.append(([confirmation], ()))

    chunks, said, stored = _send(db_session, rider, "Book me an easy ride on Monday", voice)

    assert said.strip() == shown
    assert any(c["type"] == "plan_updated" for c in chunks)
    assert not (stored.context_snapshot or {}).get("fallback")
    assert db_session.query(Workout).count() == 1


@pytest.mark.parametrize("claim", [
    "I'm booking you a threshold session on Tuesday.",
    "I'm putting a threshold session in on Tuesday.",
    "I've put a threshold session in on Tuesday.",
])
def test_a_booking_the_gate_refused_is_never_announced(db_session, rider, scripted_model, claim):
    ss.open_hold(db_session, rider, "easy_only", "Knee pain", "detector", red_flag="injury")
    scripted_model.append(([claim], [_booking(scripted_model.tool_use)]))
    scripted_model.append((["It did not go in, because of the knee."], ()))

    _, said, _ = _send(db_session, rider, "Can you add a threshold session on Tuesday?")

    assert claim not in said and "It did not go in" in said
    assert db_session.query(Workout).count() == 0


def test_putting_it_in_names_no_session_and_still_goes_out():
    assert not coach_service._PLAN_NOW_RX.search("i'm putting it in now.")
    assert not coach_service._PLAN_NOW_RX.search("i'm putting the emphasis on tempo this week.")


# ── A red-flag turn keeps its safety tools ──────────────────────────────────


def test_a_red_flag_turn_keeps_its_tools_after_a_refused_lookup(db_session, rider, scripted_model):
    tool = scripted_model.tool_use
    scripted_model.append(([], [tool("find_ride", {"query": "yesterday"}) for _ in range(3)]))
    scripted_model.append(([], [tool("flag_for_review", {"reason": "chest pain on a ride"})]))
    scripted_model.append((["Stop riding and call 999 now."], ()))

    _send(db_session, rider, "I had chest pain on the climb yesterday, can you check my ride?")

    choices = [c.get("tool_choice") for c in scripted_model.calls]
    assert choices[:2] == [None, None]
    assert coach_service._last_round_choice(1, 5, 1) == {"type": "none"}
    assert coach_service._last_round_choice(1, 5, 1, red_flag=True) is None
    assert coach_service._last_round_choice(4, 5, 0, red_flag=True) == {"type": "none"}


# ── An account held as under 18 files nothing ───────────────────────────────


def test_a_held_account_saves_no_file_and_its_card_asks_nothing(db_session, rider):
    attachment = attachment_service.ingest(db_session, rider, PB_BYTES, PB)
    assert file_triage_service.cards(db_session, rider.id, [attachment])[0]["choices"]

    ss.open_hold(db_session, rider, "hold_all", "Says they are 15", "coach_tool", red_flag="minor")
    [card] = file_triage_service.cards(db_session, rider.id, [attachment])
    assert card["choices"] == [] and card["changes"] == [] and card["question"] is None
    for choice in ("save", "chat_only", "someone_else"):
        with pytest.raises(attachment_service.DecisionRefused) as refused:
            attachment_service.decide(db_session, rider, attachment, choice)
        assert refused.value.code == "held"
    from app.models.ride import Ride
    assert db_session.query(Ride).count() == 0


def test_the_coach_is_never_told_to_ask_the_filing_question(db_session, rider):
    result = _execute_tool(db_session, rider, "save_attachment_as_ride", {
        "attachment_id": "a-1", "confirmed": False,
    })
    assert "Do not ask them" in result and "card under their message" in result
    assert "Ask them first" not in result
