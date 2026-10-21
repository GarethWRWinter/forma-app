"""The joins the merge review found open, pinned.

Main's season keeper and goal tools build plans with no rider action, and
its booking flow confirms in a few words; the safety branch's record (an
injury hold, eating very little, words of hopelessness, an account held as
under 18) and its reply check were written before either existed. Each test
below is a case where the two disagreed.
"""

import asyncio
import json
import re
from datetime import date, datetime, timedelta

import pytest

from app.api.v1 import goals as goals_api
from app.models.chat import ChatMessage, ChatRole, ChatSession
from app.models.onboarding import EventPriority, GoalEvent, GoalStatus
from app.models.ride import Ride
from app.models.safety import SafetyEvent, SafetyHold
from app.models.training import TrainingPlan, Workout, WorkoutStatus, WorkoutStep, WorkoutType
from app.models.user import User
from app.schemas.onboarding import GoalAssessmentSubmit
from app.services import attachment_service, coach_service, file_triage_service, safety_screen
from app.services import safety_service as ss
from app.services.coach_service import _build_rider_context, _execute_tool
from app.services.safety_screen import ReplyGuard
from app.services.season_service import BLOCK_NAME, ensure_all, season_note, season_state
from tests.test_chat_file_triage import PB, PB_BYTES, _gpx
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


# ── No words left after a change: the app says what went in ─────────────────
#
# The reply check drops a bare "Done." after a tool, as it must (the model
# has confirmed changes twice, holds the code never placed, and changes that
# were refused). With nothing else said, the rider got the no-reply apology
# over a session that was booked, and a failed call after a booking asked
# them to send it again. The lines come from the rows, never the model.

VOICE = pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
# What the reply check drops after any tool. A bare "Booked." it lets
# through, so it is the reply (test_a_bare_booked_still_goes_out_as_written).
BARE = pytest.mark.parametrize("bare", ["Done.", "All set."])
MONDAY, WEDNESDAY = FRIDAY + timedelta(days=3), FRIDAY + timedelta(days=5)


def _session(db, user, day=MONDAY, title="Threshold reps", kind=WorkoutType.threshold):
    workout = Workout(
        user_id=user.id, scheduled_date=day, title=title, workout_type=kind,
        status=WorkoutStatus.planned, planned_duration_seconds=3600,
    )
    db.add(workout)
    db.commit()
    return workout


def _easy_spin(tool, day=MONDAY):
    return tool("add_workout", {
        "scheduled_date": day.isoformat(), "title": "Easy spin", "workout_type": "endurance",
    })


def _change(what, db, user, tool):
    """The tool calls for one change, and the lines the rider should get."""
    if what == "booking":
        return [_easy_spin(tool)], ["Booked: Easy spin, Monday 19 October."]
    if what == "move":
        w = _session(db, user)
        return (
            [tool("update_workout", {"workout_id": w.id, "scheduled_date": WEDNESDAY.isoformat()})],
            ["Moved: Threshold reps to Wednesday 21 October."],
        )
    if what == "swap":
        a = _session(db, user)
        b = _session(db, user, WEDNESDAY, "Easy spin", WorkoutType.endurance)
        return (
            [tool("swap_workout_date", {"workout_id_a": a.id, "workout_id_b": b.id})],
            ["Moved: Threshold reps to Wednesday 21 October.", "Moved: Easy spin to Monday 19 October."],
        )
    if what == "skip":
        w = _session(db, user)
        return (
            [tool("skip_workout", {"workout_id": w.id})],
            ["Skipped: Threshold reps, Monday 19 October."],
        )
    if what == "update":
        w = _session(db, user)
        return (
            [tool("update_workout", {"workout_id": w.id, "title": "Easy spin", "workout_type": "endurance"})],
            ["Changed: Easy spin, Monday 19 October."],
        )
    if what == "goal set":
        return (
            [tool("create_goal", {
                "event_name": "Mallorca 312", "event_date": "2027-05-30",
                "event_type": "gran_fondo", "priority": "a_race",
            })],
            ["Goal set: Mallorca 312, Sunday 30 May 2027."],
        )
    assert what == "goal changed"
    goal = _goal(db, user, date(2027, 5, 30), name="Mallorca 312")
    return (
        [tool("update_goal", {"goal_id": goal.id, "event_date": "2027-06-06"})],
        ["Goal changed: Mallorca 312, Sunday 6 June 2027."],
    )


CHANGES = ["booking", "move", "swap", "skip", "update", "goal set", "goal changed"]


@VOICE
@BARE
@pytest.mark.parametrize("what", CHANGES)
def test_a_bare_confirmation_after_a_change_gets_the_apps_line(
    db_session, rider, scripted_model, clock, voice, bare, what
):
    tools, lines = _change(what, db_session, rider, scripted_model.tool_use)
    scripted_model.append(([], tools))
    scripted_model.append(([bare], ()))

    chunks, said, stored = _send(db_session, rider, "Can you sort that for me?", voice)

    assert said == "\n\n".join(lines)
    assert any(c["type"] == "plan_updated" for c in chunks)
    assert stored.content == INTRO + said
    assert stored.context_snapshot["code_written"] is True
    assert stored.context_snapshot["went_in"] == lines
    assert not stored.context_snapshot.get("fallback")
    # The next turn reads them as the app's words, not an answer it gave:
    # the lines alone, never the AI disclosure the first reply opens with.
    history = coach_service._build_messages(stored.session)
    assert history[-1]["content"] == (
        "[No answer was written to the message above. The app showed the rider "
        "what went in, from the plan as saved:\n" + "\n".join(lines) + "]"
    )


@VOICE
@pytest.mark.parametrize("what", ["booking", "skip", "refused booking"])
def test_a_bare_booked_still_goes_out_as_written(
    db_session, rider, scripted_model, clock, voice, what
):
    """The reply check is the reviewed merge's: it lets a bare "Booked."
    through after any plan tool, whether its change went in (a booking, a
    skip) or was refused and nothing changed at all, so the turn has words
    and the app adds none. Live main sends the same "Booked.". Pinned so a
    change to it is a decision for Gareth, not an accident: dropping a bare
    "Booked." when no booking went in would let the apology, or the lines,
    take over."""
    if what == "refused booking":
        tools = [_too_hard(scripted_model.tool_use)]
    else:
        tools, _ = _change(what, db_session, rider, scripted_model.tool_use)
    scripted_model.append(([], tools))
    scripted_model.append((["Booked."], ()))

    _, said, stored = _send(db_session, rider, "Can you sort that for me?", voice)

    if what == "refused booking":
        assert _results(scripted_model.calls[1])[0].startswith("Not changed")
    assert db_session.query(Workout).count() == (0 if what == "refused booking" else 1)
    assert said == "Booked."
    assert "code_written" not in stored.context_snapshot


@VOICE
def test_a_change_the_model_said_nothing_about_gets_the_apps_line(
    db_session, rider, scripted_model, clock, voice
):
    """Cut off at its output allowance after the booking, before a word."""
    scripted_model.append(([], [_easy_spin(scripted_model.tool_use)]))
    scripted_model.append(([], (), "max_tokens"))

    _, said, _ = _send(db_session, rider, "Book me an easy ride on Monday", voice)

    assert said == "Booked: Easy spin, Monday 19 October."


def test_the_turn_log_marks_the_apps_line(db_session, rider, scripted_model, clock, caplog):
    scripted_model.append(([], [_easy_spin(scripted_model.tool_use)]))
    scripted_model.append((["Done."], ()))

    with caplog.at_level("INFO", logger="forma.coach_turn"):
        _send(db_session, rider, "Book me an easy ride on Monday")

    [line] = [r.getMessage() for r in caplog.records if r.name == "forma.coach_turn"]
    logged = json.loads(line.removeprefix("coach_turn "))
    assert logged["ended"] == "changes" and logged["fallback"] is False


def _tts(monkeypatch, on=True):
    """Voice with speech on: every piece spoken, in order."""
    spoken = []

    async def tts(text):
        spoken.append(text)
        return b"audio"

    monkeypatch.setattr("app.services.voice_service.is_voice_enabled", lambda: on)
    monkeypatch.setattr("app.services.voice_service.text_to_speech", tts)
    return spoken


def _audio_indexes(chunks) -> list[int]:
    return [c["sentence_index"] for c in chunks if c["type"] == "audio"]


def test_the_apps_line_is_spoken_in_voice_mode(db_session, rider, scripted_model, clock, monkeypatch):
    spoken = _tts(monkeypatch)
    scripted_model.append(([], [_easy_spin(scripted_model.tool_use)]))
    scripted_model.append((["Done."], ()))

    chunks, said, _ = _send(db_session, rider, "Book me an easy ride on Monday", True)

    assert said == "Booked: Easy spin, Monday 19 October."
    # The opening line, then the app's line: never one index for both.
    assert spoken == [INTRO.strip(), "Booked: Easy spin, Monday 19 October."]
    assert _audio_indexes(chunks) == [0, 1]
    assert chunks[-3]["type"] == "audio"


def _restricted(db, user):
    """Eating too little flagged: every booking is refused, a skip goes in,
    and no hold is open."""
    db.add(SafetyEvent(user_id=user.id, kind="restriction", source="detector", matched="restriction"))
    db.commit()


@VOICE
@BARE
@pytest.mark.parametrize("why", ["injury hold", "restriction"])
def test_a_bare_confirmation_after_a_refused_change_keeps_the_apology(
    db_session, rider, scripted_model, clock, voice, bare, why
):
    (_injured if why == "injury hold" else _restricted)(db_session, rider)
    scripted_model.append(([], [_easy_spin(scripted_model.tool_use)]))
    scripted_model.append(([bare], ()))

    chunks, said, stored = _send(db_session, rider, "Book me an easy ride on Monday", voice)

    assert _results(scripted_model.calls[1])[0].startswith("Not changed")
    assert said == coach_service._NO_REPLY_FALLBACK
    assert stored.context_snapshot["fallback"] is True
    assert "code_written" not in stored.context_snapshot
    assert not any(c["type"] == "plan_updated" for c in chunks)
    assert db_session.query(Workout).count() == 0


@VOICE
def test_of_two_changes_only_the_one_that_went_in_is_listed(
    db_session, rider, scripted_model, clock, voice
):
    _restricted(db_session, rider)
    w = _session(db_session, rider)
    tool = scripted_model.tool_use
    scripted_model.append(([], [_easy_spin(tool, WEDNESDAY), tool("skip_workout", {"workout_id": w.id})]))
    scripted_model.append((["Done."], ()))

    _, said, _ = _send(db_session, rider, "Swap Monday for an easy ride on Wednesday", voice)

    refused, skipped = _results(scripted_model.calls[1])
    assert refused.startswith("Not changed") and skipped.startswith("Skipped")
    assert said == "Skipped: Threshold reps, Monday 19 October.\n\n" + NOT_ALL_IN


def _results(call) -> list[str]:
    """The tool results a model call was sent."""
    last = call["messages"][-1]["content"]
    return [r["content"] for r in last if isinstance(r, dict) and r.get("type") == "tool_result"]


def _fails_after(monkeypatch, scripted_model, rounds=1, words=()):
    """The script runs for `rounds` model calls; every call after them fails,
    at once or after `words`."""
    from contextlib import contextmanager

    from app.core import forma_core
    from tests.conftest import _Event

    scripted = forma_core.stream

    class Breaks:
        def __iter__(self):
            yield _Event("message_start")
            for text in words:
                yield _Event("content_block_delta", text=text)
            raise RuntimeError("the stream broke")

    @contextmanager
    def stream(**kwargs):
        if len(scripted_model.calls) < rounds:
            with scripted(**kwargs) as s:
                yield s
            return
        scripted_model.calls.append({**kwargs, "messages": list(kwargs.get("messages") or [])})
        if not words:
            raise RuntimeError("provider down")
        yield Breaks()

    monkeypatch.setattr(forma_core, "stream", stream)
    monkeypatch.setattr(coach_service, "MODEL_RETRY_BACKOFF_SECONDS", 0)


def _quota_after(monkeypatch, scripted_model, rounds=1):
    """The script runs for `rounds` model calls; the rider's monthly quota is
    reached on the call after them."""
    from contextlib import contextmanager

    from app.core import forma_core

    scripted = forma_core.stream

    @contextmanager
    def stream(**kwargs):
        if len(scripted_model.calls) < rounds:
            with scripted(**kwargs) as s:
                yield s
            return
        scripted_model.calls.append({**kwargs, "messages": list(kwargs.get("messages") or [])})
        raise forma_core.BudgetExceededError(900.0, 800)

    monkeypatch.setattr(forma_core, "stream", stream)


# How a turn with a change in it can end with no words of the model's: the
# reply check drops a bare "Done.", the model call fails, or the quota is
# reached.
ENDING = pytest.mark.parametrize("ending", ["done", "failed call", "quota"])


def _end(ending, monkeypatch, scripted_model):
    if ending == "done":
        scripted_model.append((["Done."], ()))
    elif ending == "failed call":
        _fails_after(monkeypatch, scripted_model, rounds=len(scripted_model))
    else:
        _quota_after(monkeypatch, scripted_model, rounds=len(scripted_model))


def _ended(ending, lines) -> str:
    """What the rider is shown, for the lines and the way the turn ended."""
    from app.core import forma_core

    if ending == "failed call":
        return "\n\n".join([coach_service.REPLY_FAILED_CHANGES, *lines])
    if ending == "quota":
        return "\n\n".join([forma_core.QUOTA_MESSAGE, coach_service.CHANGES_HEADER, *lines])
    return "\n\n".join(lines)


@VOICE
@pytest.mark.parametrize("what", CHANGES)
def test_a_failed_call_after_a_change_says_what_went_in_and_never_asks_again(
    db_session, rider, scripted_model, clock, monkeypatch, voice, what
):
    tools, lines = _change(what, db_session, rider, scripted_model.tool_use)
    scripted_model.append(([], tools))
    _fails_after(monkeypatch, scripted_model)

    chunks, said, stored = _send(db_session, rider, "Can you sort that for me?", voice)

    assert said == "\n\n".join([coach_service.REPLY_FAILED_CHANGES, *lines])
    assert "again" not in said
    assert any(c["type"] == "plan_updated" for c in chunks)
    assert stored.content == INTRO + said
    assert stored.context_snapshot["went_in"] == lines
    assert "code_written" not in stored.context_snapshot
    assert "not_in" not in stored.context_snapshot
    # The next turn reads the words as the rider saw them.
    assert coach_service._build_messages(stored.session)[-1]["content"] == stored.content
    assert db_session.query(SafetyEvent).filter(SafetyEvent.kind == "reply_failed").count() == 1


@VOICE
@pytest.mark.parametrize("what", CHANGES)
def test_the_quota_after_a_change_says_what_went_in(
    db_session, rider, scripted_model, clock, monkeypatch, voice, what
):
    from app.core import forma_core

    tools, lines = _change(what, db_session, rider, scripted_model.tool_use)
    scripted_model.append(([], tools))
    _quota_after(monkeypatch, scripted_model)

    chunks, said, stored = _send(db_session, rider, "Can you sort that for me?", voice)

    assert said == "\n\n".join([forma_core.QUOTA_MESSAGE, "Here's what went in:", *lines])
    assert any(c["type"] == "plan_updated" for c in chunks)
    assert stored.content == INTRO + said
    assert stored.context_snapshot["went_in"] == lines
    assert "code_written" not in stored.context_snapshot


@VOICE
@pytest.mark.parametrize("why", ["injury hold", "restriction", "no change"])
def test_the_quota_with_nothing_gone_in_keeps_its_message(
    db_session, rider, scripted_model, clock, monkeypatch, voice, why
):
    """A hold's ending is the safety system's; a booking refused, or a turn
    that changed nothing, has nothing to list."""
    from app.core import forma_core

    if why == "injury hold":
        _injured(db_session, rider)
    elif why == "restriction":
        _restricted(db_session, rider)
    if why != "no change":
        scripted_model.append(([], [_easy_spin(scripted_model.tool_use)]))
    _quota_after(monkeypatch, scripted_model, rounds=len(scripted_model))

    _, said, stored = _send(db_session, rider, "Book me an easy ride on Monday", voice)

    assert said == forma_core.QUOTA_MESSAGE
    assert "went_in" not in (stored.context_snapshot or {})


@VOICE
def test_the_quota_after_words_keeps_the_words(
    db_session, rider, scripted_model, clock, monkeypatch, voice
):
    """A turn with words in it ends as it always has."""
    scripted_model.append((["Good plan. "], [_easy_spin(scripted_model.tool_use)]))
    _quota_after(monkeypatch, scripted_model)

    _, said, stored = _send(db_session, rider, "Book me an easy ride on Monday", voice)

    assert db_session.query(Workout).count() == 1
    assert said == "Good plan. "
    assert "went_in" not in (stored.context_snapshot or {})


@pytest.mark.parametrize("what", ["booking", "no change"])
def test_the_quota_ending_in_voice_speaks_the_opening_line_first(
    db_session, rider, scripted_model, clock, monkeypatch, what
):
    """The opening line was shown above the quota message and the lines, and
    never spoken, where every other ending that lists what went in speaks
    it first. With nothing listed the quota message is not spoken at all, as
    in the reviewed merge."""
    from app.core import forma_core

    spoken = _tts(monkeypatch)
    if what == "booking":
        scripted_model.append(([], [_easy_spin(scripted_model.tool_use)]))
    _quota_after(monkeypatch, scripted_model, rounds=len(scripted_model))

    chunks, said, _ = _send(db_session, rider, "Book me an easy ride on Monday", True)

    if what == "no change":
        assert said == forma_core.QUOTA_MESSAGE and spoken == []
        return
    line = "Booked: Easy spin, Monday 19 October."
    assert said == "\n\n".join([forma_core.QUOTA_MESSAGE, coach_service.CHANGES_HEADER, line])
    assert spoken == [INTRO.strip(), forma_core.QUOTA_MESSAGE, coach_service.CHANGES_HEADER, line]
    assert _audio_indexes(chunks) == [0, 1, 2, 3]


@ENDING
def test_spoken_indexes_never_repeat(db_session, rider, scripted_model, clock, monkeypatch, ending):
    """In voice mode each spoken piece takes the next index, the app's lines
    and the quota message after them included."""
    spoken = _tts(monkeypatch)
    w = _session(db_session, rider)
    tool = scripted_model.tool_use
    scripted_model.append(([], [_easy_spin(tool), tool("skip_workout", {"workout_id": w.id}), _too_hard(tool)]))
    _end(ending, monkeypatch, scripted_model)

    chunks, said, _ = _send(db_session, rider, "Can you sort my week?", True)

    lines = ["Booked: Easy spin, Monday 19 October.", "Skipped: Threshold reps, Monday 19 October.", NOT_ALL_IN]
    assert said == _ended(ending, lines)
    indexes = _audio_indexes(chunks)
    assert indexes == list(range(len(indexes))) and len(indexes) >= 3
    assert spoken[-1].endswith("Ask me about it if anything looks missing.")
    assert "Booked: Easy spin, Monday 19 October." in "\n".join(spoken)


@VOICE
def test_a_call_that_breaks_after_its_words_says_what_went_in_after_them(
    db_session, rider, scripted_model, clock, monkeypatch, voice
):
    scripted_model.append(([], [_easy_spin(scripted_model.tool_use)]))
    _fails_after(monkeypatch, scripted_model, words=["Keep it conversational. "])

    _, said, _ = _send(db_session, rider, "Book me an easy ride on Monday", voice)

    assert said == (
        "Keep it conversational. \n\n" + coach_service.REPLY_FAILED_CHANGES
        + "\n\nBooked: Easy spin, Monday 19 October."
    )


@VOICE
@pytest.mark.parametrize("why", ["injury hold", "restriction"])
def test_a_failed_call_after_a_refused_change_keeps_its_line(
    db_session, rider, scripted_model, clock, monkeypatch, voice, why
):
    (_injured if why == "injury hold" else _restricted)(db_session, rider)
    scripted_model.append(([], [_easy_spin(scripted_model.tool_use)]))
    _fails_after(monkeypatch, scripted_model)

    _, said, _ = _send(db_session, rider, "Book me an easy ride on Monday", voice)

    assert said == safety_screen.REPLY_FAILED_MESSAGE


# What follows the list when a plan or goal call this turn was refused or
# errored, whatever any later call did.
NOT_ALL_IN = "Not everything I tried went in. Ask me about it if anything looks missing."
# What the next turn's history says before the calls that did not go in.
NOT_IN_INTRO = (
    "The plan and goal calls that did not go in, in the order they ran, each with "
    "what its tool said then. A later call may have made the same change; what went "
    "in, above, is the plan as saved."
)


def _not_in(call, result: str) -> str:
    """A call that did not go in, as the next turn's history lists it: the
    call as it was sent, then what its tool said."""
    return f"{call.name} {json.dumps(call.input, ensure_ascii=False)}: {result.strip()}"
# An hour at 65% of FTP: steps any endurance session may carry.
STEADY_HOUR = [{"step_type": "steady_state", "duration_seconds": 3600, "power_pct": 65}]
# Steps that ask for 120% of FTP: refused on an endurance session for any
# rider, hold or none.
_HARD_STEPS = [
    {"step_type": "warmup", "duration_seconds": 600, "power_pct": 55},
    {"step_type": "interval_on", "duration_seconds": 300, "power_pct": 120, "repeat_count": 4},
    {"step_type": "interval_off", "duration_seconds": 300, "power_pct": 50},
    {"step_type": "cooldown", "duration_seconds": 600, "power_pct": 50},
]


@VOICE
@ENDING
def test_a_session_booked_twice_gets_a_line_each(
    db_session, rider, scripted_model, clock, monkeypatch, voice, ending
):
    """Two bookings that read the same are two sessions on the calendar: a
    line each, so the double booking shows."""
    tool = scripted_model.tool_use
    scripted_model.append(([], [_easy_spin(tool), _easy_spin(tool)]))
    _end(ending, monkeypatch, scripted_model)

    _, said, _ = _send(db_session, rider, "Book me an easy ride on Monday", voice)

    assert db_session.query(Workout).count() == 2
    assert said == _ended(ending, ["Booked: Easy spin, Monday 19 October."] * 2)


@VOICE
def test_two_sessions_that_read_the_same_get_a_line_each(
    db_session, rider, scripted_model, clock, voice
):
    a, b = (
        _session(db_session, rider, title="Endurance", kind=WorkoutType.endurance) for _ in "ab"
    )
    tool = scripted_model.tool_use
    scripted_model.append(([], [
        tool("skip_workout", {"workout_id": a.id}), tool("skip_workout", {"workout_id": b.id}),
    ]))
    scripted_model.append((["Done."], ()))

    _, said, _ = _send(db_session, rider, "Skip both of Monday's rides", voice)

    assert said == "Skipped: Endurance, Monday 19 October.\n\nSkipped: Endurance, Monday 19 October."


def _too_hard(tool, day=WEDNESDAY):
    """An endurance session whose steps ask for 120% of FTP: refused for any
    rider, hold or none."""
    return tool("add_workout", {
        "scheduled_date": day.isoformat(), "title": "Hill reps", "workout_type": "endurance",
        "steps": _HARD_STEPS,
    })


@VOICE
@ENDING
@pytest.mark.parametrize("later", [False, True], ids=["same round", "later round"])
def test_a_change_that_did_not_go_in_is_never_left_off_the_list(
    db_session, rider, scripted_model, clock, monkeypatch, voice, later, ending
):
    """Monday's session went and Wednesday's was refused: the list alone read
    as the whole of the swap. Every ending says so, and the next turn has
    the tool's own words, so the coach can answer "what didn't?"."""
    w = _session(db_session, rider)
    tool = scripted_model.tool_use
    skip, hard = tool("skip_workout", {"workout_id": w.id}), _too_hard(tool)
    if later:
        scripted_model.extend([([], [skip]), ([], [hard])])
    else:
        scripted_model.append(([], [skip, hard]))
    _end(ending, monkeypatch, scripted_model)

    _, said, stored = _send(
        db_session, rider, "Swap Monday's threshold for hill reps on Wednesday", voice
    )

    results = [r for call in scripted_model.calls[1:] for r in _results(call)]
    refused = next(r for r in results if r.startswith("Not changed"))
    assert refused.startswith("Not changed. This session's steps ask for 120%")
    line = "Skipped: Threshold reps, Monday 19 October."
    assert said == _ended(ending, [line, NOT_ALL_IN])
    assert stored.context_snapshot["went_in"] == [line]
    assert stored.context_snapshot["not_in"] == [_not_in(hard, refused)]
    assert ("code_written" in stored.context_snapshot) == (ending == "done")
    note = coach_service._build_messages(stored.session)[-1]["content"]
    assert note.endswith(NOT_IN_INTRO + "\n- " + _not_in(hard, refused) + "]")
    if ending != "done":
        assert note.startswith(stored.content + "\n\n[")


def _refused_then(first, db, user, tool):
    """A refused or failed plan call, then one in a later round that went
    in: the rounds, and the line for what went in."""
    easy = "Booked: Easy spin, Monday 19 October."
    if first in ("bad date", "wrong year", "a Monday gone", "past the fortnight"):
        day = {
            "bad date": "19/10/2026", "wrong year": "2025-10-19", "a Monday gone": "2026-10-12",
            "past the fortnight": (FRIDAY + timedelta(days=17)).isoformat(),
        }[first]
        refused = tool("add_workout", {"scheduled_date": day, "title": "Easy spin", "workout_type": "endurance"})
        return [[refused], [_easy_spin(tool)]], easy
    if first == "wrong id":
        w = _session(db, user)
        return [
            [tool("skip_workout", {"workout_id": "not-a-session"})],
            [tool("skip_workout", {"workout_id": w.id})],
        ], "Skipped: Threshold reps, Monday 19 October."
    if first.startswith("booking too hard"):
        fix = tool("add_workout", {
            "scheduled_date": WEDNESDAY.isoformat(), "title": "Hill reps", "workout_type": "endurance",
            **({"steps": STEADY_HOUR} if first.endswith("eased") else {}),
        })
        return [[_too_hard(tool)], [fix]], "Booked: Hill reps, Wednesday 21 October."
    assert first.startswith("steps too hard")
    w = _session(db, user, title="Easy spin", kind=WorkoutType.endurance)
    eased = first.endswith("eased")
    fix = tool("update_workout", {
        "workout_id": w.id, **({"steps": STEADY_HOUR} if eased else {"title": "Hill reps"}),
    })
    return [
        [tool("update_workout", {"workout_id": w.id, "steps": _HARD_STEPS})], [fix],
    ], f"Changed: {'Easy spin' if eased else 'Hill reps'}, Monday 19 October."


REFUSED_FIRST = [
    "bad date", "wrong year", "a Monday gone", "past the fortnight", "wrong id",
    "booking too hard, eased", "booking too hard, retitled", "steps too hard, eased",
    "steps too hard, retitled",
]


@VOICE
@ENDING
@pytest.mark.parametrize("first", REFUSED_FIRST)
def test_a_refusal_is_never_taken_as_put_right(
    db_session, rider, scripted_model, clock, monkeypatch, voice, ending, first
):
    """Whether a later call put a refused one right is never worked out (a
    retitle once passed for steps that never went in): a refusal is always
    said, and the next turn has the tool's words to answer from."""
    rounds, line = _refused_then(first, db_session, rider, scripted_model.tool_use)
    scripted_model.extend(([], calls) for calls in rounds)
    _end(ending, monkeypatch, scripted_model)

    _, said, stored = _send(db_session, rider, "Can you sort that for me?", voice)

    refused = _results(scripted_model.calls[1])[0]
    assert refused.startswith(("Error", "Not changed"))
    assert said == _ended(ending, [line, NOT_ALL_IN])
    assert stored.context_snapshot["not_in"] == [_not_in(rounds[0][0], refused)]
    assert _not_in(rounds[0][0], refused) in coach_service._build_messages(stored.session)[-1]["content"]


def _shared_ride(db, user):
    """A ride file shared in the chat, whose owner nobody has recorded: an
    easy hour, with nothing in it that would keep it on its card."""
    easy_hour = _gpx("2026-10-11T09:00:00", 3600, watts=150, hr=125, rpm=85)
    return attachment_service.ingest(db, user, easy_hour, "Sunday_spin.gpx")


@VOICE
@ENDING
def test_a_file_save_lookup_or_proposal_that_did_not_go_in_is_not_a_plan_change(
    db_session, rider, scripted_model, clock, monkeypatch, voice, ending
):
    """Beside a booking, a file save that did not happen (its card says so),
    a ride lookup past the limit and a proposal that was not filed are not
    plan or goal calls: the booking is the whole of the list."""
    attachment = _shared_ride(db_session, rider)
    tool = scripted_model.tool_use
    scripted_model.append(([], [
        tool("save_attachment_as_ride", {"attachment_id": attachment.id, "confirmed": True}),
        *(tool("find_ride", {"query": "yesterday"}) for _ in range(3)),
        tool("propose_plan_change", {"observation": "", "rationale": "", "changes": []}),
        _easy_spin(tool),
    ]))
    _end(ending, monkeypatch, scripted_model)

    _, said, stored = _send(
        db_session, rider, "Save that ride and book me an easy spin on Monday", voice
    )

    results = _results(scripted_model.calls[1])
    assert results[0].startswith("Error: nothing was saved. Whose ride this file is")
    assert results[3].startswith("Not run: this rider message has already had its two")
    assert results[4].startswith("Nothing was filed")
    assert db_session.query(Ride).count() == 0
    assert said == _ended(ending, ["Booked: Easy spin, Monday 19 October."])
    assert "not_in" not in stored.context_snapshot


@VOICE
def test_a_file_save_that_did_not_happen_on_its_own_keeps_the_apology(
    db_session, rider, scripted_model, clock, voice
):
    """Nothing went in, so there is nothing to list: the ending is the
    reviewed merge's."""
    attachment = _shared_ride(db_session, rider)
    scripted_model.append(([], [scripted_model.tool_use("save_attachment_as_ride", {
        "attachment_id": attachment.id, "confirmed": True,
    })]))
    scripted_model.append((["Done."], ()))

    _, said, stored = _send(db_session, rider, "Save that ride for me", voice)

    assert said == coach_service._NO_REPLY_FALLBACK
    assert stored.context_snapshot["fallback"] is True


@VOICE
def test_a_session_saved_before_its_tool_broke_is_listed(
    db_session, rider, scripted_model, clock, monkeypatch, voice
):
    """The rows decide, not the result: the tool broke after saving, said
    nothing was saved, and the session is on the calendar."""
    def broken(*args, **kwargs):
        raise RuntimeError("the note broke")

    monkeypatch.setattr(coach_service, "_structure_note", broken)
    scripted_model.append(([], [_easy_spin(scripted_model.tool_use)]))
    scripted_model.append((["Done."], ()))

    _, said, _ = _send(db_session, rider, "Book me an easy ride on Monday", voice)

    assert _results(scripted_model.calls[1]) == [coach_service._TOOL_CRASHED]
    assert db_session.query(Workout).count() == 1
    assert said == "Booked: Easy spin, Monday 19 October."


@VOICE
@ENDING
def test_a_change_whose_tool_broke_before_saving_is_not_all_in(
    db_session, rider, scripted_model, clock, monkeypatch, voice, ending
):
    """An errored call is one that did not go in, as a refused one is."""
    real = coach_service._execute_tool

    def run(db, user, name, args):
        if name == "skip_workout":
            raise RuntimeError("the skip broke")
        return real(db, user, name, args)

    monkeypatch.setattr(coach_service, "_execute_tool", run)
    w = _session(db_session, rider)
    tool = scripted_model.tool_use
    skip = tool("skip_workout", {"workout_id": w.id})
    scripted_model.append(([], [_easy_spin(tool), skip]))
    _end(ending, monkeypatch, scripted_model)

    _, said, stored = _send(db_session, rider, "Swap Monday for an easy spin", voice)

    assert _results(scripted_model.calls[1])[1] == coach_service._TOOL_CRASHED
    assert said == _ended(ending, ["Booked: Easy spin, Monday 19 October.", NOT_ALL_IN])
    assert stored.context_snapshot["not_in"] == [_not_in(skip, coach_service._TOOL_CRASHED)]


@VOICE
@ENDING
def test_the_next_turn_knows_which_call_did_not_go_in(
    db_session, rider, scripted_model, clock, monkeypatch, voice, ending
):
    """The skip broke, and the same skip in the next round went in. The next
    turn had only "Error: that change failed", naming no call, listed under
    what went in beside the rider's "Not everything I tried went in": asked
    what did not, the coach had nothing to answer from and was told to say
    the skip failed. It now has each call as it was sent, in the order they
    ran, and knows a later call may have made the same change."""
    real = coach_service._execute_tool
    broke = []

    def run(db, user, name, args):
        if name == "skip_workout" and not broke:
            broke.append(name)
            raise RuntimeError("the skip broke")
        return real(db, user, name, args)

    monkeypatch.setattr(coach_service, "_execute_tool", run)
    w = _session(db_session, rider)
    tool = scripted_model.tool_use
    skip = tool("skip_workout", {"workout_id": w.id})
    scripted_model.extend([([], [skip]), ([], [tool("skip_workout", {"workout_id": w.id})])])
    _end(ending, monkeypatch, scripted_model)

    _, said, stored = _send(db_session, rider, "Skip Monday for me", voice)

    line = "Skipped: Threshold reps, Monday 19 October."
    assert said == _ended(ending, [line, NOT_ALL_IN])
    assert stored.context_snapshot["went_in"] == [line]
    entry = _not_in(skip, coach_service._TOOL_CRASHED)
    assert entry == f'skip_workout {{"workout_id": "{w.id}"}}: ' + coach_service._TOOL_CRASHED
    assert stored.context_snapshot["not_in"] == [entry]
    not_in = f"{NOT_IN_INTRO}\n- {entry}"
    note = coach_service._build_messages(stored.session)[-1]["content"]
    if ending == "done":
        assert note == (
            "[No answer was written to the message above. The app showed the rider "
            f"what went in, from the plan as saved:\n{line}\n{not_in}]"
        )
    else:
        assert note == f"{stored.content}\n\n[{not_in}]"


def _booked_id_into(monkeypatch, tool_input: dict) -> None:
    """A later round's call gets the id of the session booked before it, as
    the model reads it from the booking's result."""
    real = coach_service._execute_tool

    def run(db, user, name, args):
        result = real(db, user, name, args)
        booked = name == "add_workout" and result.startswith("Booked")
        if booked and not tool_input.get("workout_id"):
            tool_input["workout_id"] = re.search(r"workout_id ([\w-]+)", result).group(1)
        return result

    monkeypatch.setattr(coach_service, "_execute_tool", run)


def _net(what, db, user, tool, monkeypatch):
    """Rounds of plan calls, and the lines for where each session ended up:
    one per session, from its turn-start state to its end state."""
    if what.startswith("booked, then"):
        later = {
            "booked, then given steps": {
                "steps": [{"step_type": "steady_state", "duration_seconds": 3600, "power_pct": 65}],
            },
            "booked, then moved": {"scheduled_date": WEDNESDAY.isoformat()},
            "booked, then skipped": {"workout_id": ""},
        }[what]
        _booked_id_into(monkeypatch, later)
        name = "skip_workout" if what.endswith("skipped") else "update_workout"
        day = "Wednesday 21 October" if what.endswith("moved") else "Monday 19 October"
        lines = [] if what.endswith("skipped") else [f"Booked: Easy spin, {day}."]
        return [[_easy_spin(tool)], [tool(name, later)]], lines
    if what == "moved, then moved back":
        w = _session(db, user)
        return [
            [tool("update_workout", {"workout_id": w.id, "scheduled_date": WEDNESDAY.isoformat()})],
            [tool("update_workout", {"workout_id": w.id, "scheduled_date": MONDAY.isoformat()})],
        ], []
    if what == "swapped twice":
        a = _session(db, user)
        b = _session(db, user, WEDNESDAY, "Easy spin", WorkoutType.endurance)
        swap = {"workout_id_a": a.id, "workout_id_b": b.id}
        return [[tool("swap_workout_date", swap)], [tool("swap_workout_date", swap)]], []
    if what == "moved under its own name":
        w = _session(db, user)
        return [[tool("update_workout", {
            "workout_id": w.id, "title": "Threshold reps", "scheduled_date": WEDNESDAY.isoformat(),
        })]], ["Moved: Threshold reps to Wednesday 21 October."]
    if what == "moved and changed":
        w = _session(db, user)
        return [[tool("update_workout", {
            "workout_id": w.id, "title": "Easy spin", "workout_type": "endurance",
            "scheduled_date": WEDNESDAY.isoformat(),
        })]], ["Moved: Threshold reps to Wednesday 21 October.", "Changed: Easy spin, Wednesday 21 October."]
    if what == "moved and retyped":
        # The app gives a stock title the new type's name: the move still
        # names the session the rider knew, so they can tell which one went.
        w = _session(db, user, title="Threshold")
        return [[tool("update_workout", {
            "workout_id": w.id, "workout_type": "endurance", "scheduled_date": WEDNESDAY.isoformat(),
        })]], [
            "Moved: Threshold to Wednesday 21 October.",
            "Changed: Short endurance ride, Wednesday 21 October.",
        ]
    if what in ("moved, then skipped", "retitled, then skipped"):
        # Each change the session ended with has its line, the skip included.
        w = _session(db, user)
        moved = what.startswith("moved")
        edit = {"scheduled_date": WEDNESDAY.isoformat()} if moved else {"title": "Easy spin"}
        day = "Wednesday 21 October" if moved else "Monday 19 October"
        name = "Threshold reps" if moved else "Easy spin"
        return [
            [tool("update_workout", {"workout_id": w.id, **edit})],
            [tool("skip_workout", {"workout_id": w.id})],
        ], [
            f"Moved: Threshold reps to {day}." if moved else f"Changed: Easy spin, {day}.",
            f"Skipped: {name}, {day}.",
        ]
    if what in ("given its own type", "moved under its own type"):
        # The app works out a load estimate for a session that had none: a
        # number the call never sent, so not a change.
        w = _session(db, user)
        moved = what.startswith("moved")
        return [[tool("update_workout", {
            "workout_id": w.id, "workout_type": "threshold",
            **({"scheduled_date": WEDNESDAY.isoformat()} if moved else {}),
        })]], ["Moved: Threshold reps to Wednesday 21 October."] if moved else []
    if what in ("skipped, then put back", "skipped again"):
        w = _session(db, user)
        w.status = WorkoutStatus.skipped
        db.commit()
        if what == "skipped again":
            return [[tool("skip_workout", {"workout_id": w.id})]], []
        return [[tool("update_workout", {
            "workout_id": w.id, "scheduled_date": MONDAY.isoformat(),
        })]], ["Changed: Threshold reps, Monday 19 October."]
    assert what == "goal given its own date"
    goal = _goal(db, user, date(2027, 5, 30), name="Mallorca 312")
    return [[tool("update_goal", {"goal_id": goal.id, "event_date": "2027-05-30"})]], []


NET = [
    "booked, then given steps", "booked, then moved", "booked, then skipped",
    "moved, then moved back", "swapped twice", "moved under its own name", "moved and changed",
    "moved and retyped", "moved, then skipped", "retitled, then skipped",
    "given its own type", "moved under its own type",
    "skipped, then put back", "skipped again", "goal given its own date",
]


@VOICE
@pytest.mark.parametrize("what", NET)
def test_each_change_is_listed_for_where_it_ended_up(
    db_session, rider, scripted_model, clock, monkeypatch, voice, what
):
    """One line per session or goal, for the difference between how the turn
    found it and how it left it: never a line per call, and none for a call
    that left it as it was."""
    rounds, lines = _net(what, db_session, rider, scripted_model.tool_use, monkeypatch)
    scripted_model.extend(([], calls) for calls in rounds)
    scripted_model.append((["Done."], ()))

    _, said, _ = _send(db_session, rider, "Can you sort that for me?", voice)

    results = [r for call in scripted_model.calls[1:] for r in _results(call)]
    assert len(results) == sum(map(len, rounds))
    assert not any(r.startswith(("Error", "Not changed")) for r in results)
    assert said == ("\n\n".join(lines) if lines else coach_service._NO_REPLY_FALLBACK)
    if what.endswith("own type"):
        [w] = db_session.query(Workout).all()
        assert w.planned_tss is not None


@VOICE
def test_a_goal_that_rebuilt_the_plan_says_so(db_session, scripted_model, clock, voice):
    gareth, _, _ = _incident(db_session)
    scripted_model.append(([], [scripted_model.tool_use("create_goal", {
        "event_name": "Mallorca 312", "event_date": "2027-04-24",
        "event_type": "gran_fondo", "priority": "a_race",
    })]))
    scripted_model.append((["Done."], ()))

    _, said, _ = _send(db_session, gareth, "Let's aim at the 312 in April", voice)

    assert "The plan has been rebuilt around it" in _results(scripted_model.calls[1])[0]
    [plan] = _active(db_session, gareth)
    assert plan.name == "Plan for Mallorca 312"
    assert (plan.start_date, plan.end_date) == (FRIDAY, date(2027, 4, 24))
    # Named for its goal, never "New plan: Plan for".
    assert said == (
        "Goal set: Mallorca 312, Saturday 24 April 2027.\n\n"
        "New plan for Mallorca 312: Friday 16 October to Saturday 24 April 2027."
    )


@VOICE
@pytest.mark.parametrize("why", ["moved off its plan", "restriction", "never set up"])
def test_a_goal_line_says_the_goal_and_nothing_about_the_plan(
    db_session, rider, scripted_model, clock, voice, why
):
    """Where the plan stands was worked out from the rows, and read wrong:
    "Ask me when you want it rebuilt" when no coach tool rebuilds a plan.
    The goal line is the goal alone."""
    tool = scripted_model.tool_use
    if why == "moved off its plan":
        user, _, _ = _incident(db_session)
        filed = _execute_tool(db_session, user, "create_goal", {
            "event_name": "Mallorca 312", "event_date": "2027-04-24",
            "event_type": "gran_fondo", "priority": "a_race",
        })
        assert "The plan has been rebuilt around it" in filed
        goal = db_session.query(GoalEvent).filter(GoalEvent.event_name == "Mallorca 312").one()
        call = tool("update_goal", {"goal_id": goal.id, "event_date": "2027-05-29"})
        line = "Goal changed: Mallorca 312, Saturday 29 May 2027."
    else:
        if why == "restriction":
            user, _, _ = _incident(db_session)
            _restricted(db_session, user)
        else:
            user = rider
        call = tool("create_goal", {
            "event_name": "Mallorca 312", "event_date": "2027-04-24",
            "event_type": "gran_fondo", "priority": "a_race",
        })
        line = "Goal set: Mallorca 312, Saturday 24 April 2027."
    scripted_model.append(([], [call]))
    scripted_model.append((["Done."], ()))

    _, said, _ = _send(db_session, user, "The 312 is on", voice)

    result = _results(scripted_model.calls[1])[0]
    assert {
        "moved off its plan": "was built for the old date",
        "restriction": "The plan was NOT rebuilt",
        "never set up": "There is no current plan",
    }[why] in result
    assert said == line


@VOICE
@pytest.mark.parametrize("day, line", [
    (MONDAY, "Booked: Easy spin, Monday 19 October."),
    (FRIDAY, "Booked: Easy spin, Friday 16 October."),
], ids=["Monday", "today"])
def test_a_booking_behind_another_session_is_its_line_alone(
    db_session, rider, scripted_model, clock, monkeypatch, voice, day, line
):
    """Notes on which page shows it read badly aloud and missed sessions
    that moved: the line says what went in and where it is, nothing more."""
    spoken = _tts(monkeypatch, on=voice)
    _session(db_session, rider, day=day, title="Rest day", kind=WorkoutType.rest)
    scripted_model.append(([], [_easy_spin(scripted_model.tool_use, day)]))
    scripted_model.append((["Done."], ()))

    _, said, _ = _send(db_session, rider, "Book me an easy ride", voice)

    assert "as a second session that day after Rest day" in _results(scripted_model.calls[1])[0]
    assert said == line
    assert spoken[-1:] == ([line] if voice else [])


def _safety_turn(why, db, user, tool):
    """A turn whose ending is the safety system's, with a change that goes
    in: the rider's message and the model's tool rounds."""
    if why == "head knock":
        return (
            "Hit my head on the cupboard door, ouch, anyway can you book me an easy ride on Monday?",
            [([], [_easy_spin(tool)])],
        )
    if why == "crisis":
        return "I don't want to be here anymore. Book me an easy ride on Monday.", [
            ([], [_easy_spin(tool)]),
        ]
    if why == "hold":
        _injured(db, user)
        w = _session(db, user)
        return "Skip Monday for me", [([], [tool("skip_workout", {"workout_id": w.id})])]
    # The coach's own safety tools, on a message the regex finds nothing in
    # (the classifier is offline in tests): the coach filed the turn as a
    # crisis, a possible minor or a safety matter, or tried to hold the plan
    # and was refused.
    if why.startswith("coach flags"):
        _, _, reason, *later = why.split()
        flag = tool("flag_for_review", {"reason": reason, "note": "Sounded hopeless"})
        if later:
            return QUIET_CRISIS, [([], [_easy_spin(tool)]), ([], [flag])]
        return QUIET_CRISIS, [([], [flag, _easy_spin(tool)])]
    if why == "coach hold refused":
        return "Felt a bit odd on the climb yesterday. Book me an easy spin Monday.", [
            ([], [tool("apply_safety_hold", {
                "level": "pause", "reason": "Odd on the climb", "red_flag": "chest_pain",
            }), _easy_spin(tool)]),
        ]
    assert why == "under 18"
    return "Book me an easy ride on Monday", [
        ([], [_easy_spin(tool)]),
        ([], [tool("apply_safety_hold", {
            "level": "hold_all", "reason": "Says they are 15", "red_flag": "minor",
        })]),
    ]


QUIET_CRISIS = (
    "Sorted my will out and gave the dog to my sister, so the plan won't matter "
    "after Sunday. Book me an easy spin Monday."
)


@pytest.mark.parametrize("message", [
    QUIET_CRISIS, "Felt a bit odd on the climb yesterday. Book me an easy spin Monday.",
])
def test_the_coachs_own_safety_turns_are_ones_the_regex_misses(message):
    """So in those turns the coach's tool is the only safety signal."""
    assert safety_screen._detect(message) == []


# What the stored reply's snapshot says about how it ended, and its cards.
MARKS = ("fallback", "code_written", "went_in", "not_in", "safety_cards", "safety_card")


@VOICE
@ENDING
@pytest.mark.parametrize("why", [
    "head knock", "crisis", "hold", "under 18",
    "coach flags crisis", "coach flags minor", "coach flags safety",
    "coach flags crisis later", "coach hold refused",
])
def test_a_safety_turn_with_a_change_ends_as_it_always_has(
    db_session, rider, scripted_model, clock, monkeypatch, voice, ending, why
):
    """A red flag's, a hold's or an under-18 account's ending is the safety
    system's, byte for byte, and so is the ending of a turn the coach itself
    flagged for review or tried to hold: the same turn with the app's lines
    switched off ends the same way, and neither lists the change."""
    twin = User(email="twin@example.com", hashed_password="x", full_name="Sam Rider", ftp=250)
    db_session.add(twin)
    db_session.commit()

    def run(user, lines_on):
        scripted_model.clear()
        scripted_model.calls.clear()
        message, rounds = _safety_turn(why, db_session, user, scripted_model.tool_use)
        scripted_model.extend(rounds)
        with monkeypatch.context() as m:
            _end(ending, m, scripted_model)
            if not lines_on:
                m.setattr(coach_service._TurnChanges, "ending", lambda *a, **k: [])
            _, said, stored = _send(db_session, user, message, voice)
        went_in = [r for call in scripted_model.calls[1:] for r in _results(call)]
        assert any(r.startswith(("Booked", "Skipped")) for r in went_in)
        marks = {k: (stored.context_snapshot or {}).get(k) for k in MARKS}
        return said, stored.content, marks

    said, content, snapshot = run(rider, lines_on=True)
    assert run(twin, lines_on=False) == (said, content, snapshot)
    for line in ("Booked:", "Skipped:", coach_service.CHANGES_HEADER):
        assert line not in said
    assert snapshot["code_written"] is None and snapshot["went_in"] is None


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
