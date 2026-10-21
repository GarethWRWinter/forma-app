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
            ["Goal set: Mallorca 312, Sunday 30 May 2027. " + NO_PLAN_YET],
        )
    assert what == "goal changed"
    goal = _goal(db, user, date(2027, 5, 30), name="Mallorca 312")
    return (
        [tool("update_goal", {"goal_id": goal.id, "event_date": "2027-06-06"})],
        ["Goal changed: Mallorca 312, Sunday 6 June 2027. " + NO_PLAN_YET],
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


def test_the_apps_line_is_spoken_in_voice_mode(db_session, rider, scripted_model, clock, monkeypatch):
    spoken = []

    async def tts(text):
        spoken.append(text)
        return b"audio"

    monkeypatch.setattr("app.services.voice_service.is_voice_enabled", lambda: True)
    monkeypatch.setattr("app.services.voice_service.text_to_speech", tts)
    scripted_model.append(([], [_easy_spin(scripted_model.tool_use)]))
    scripted_model.append((["Done."], ()))

    chunks, said, _ = _send(db_session, rider, "Book me an easy ride on Monday", True)

    assert said == "Booked: Easy spin, Monday 19 October."
    assert spoken[-1] == "Booked: Easy spin, Monday 19 October."
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
    assert db_session.query(SafetyEvent).filter(SafetyEvent.kind == "reply_failed").count() == 1


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


# What follows the list when a plan or goal call this turn did not go in and
# nothing later in the turn put it right, so the list never reads as the
# whole of what was asked.
NOT_ALL_IN = "Not everything went in. Ask me what didn't, and why."
# After a goal line, when no plan was built for the goal this turn.
NO_PLAN_YET = "No plan has been built for it yet."
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
@pytest.mark.parametrize("fails", [False, True], ids=["done", "failed call"])
def test_a_session_booked_twice_shows_as_two(
    db_session, rider, scripted_model, clock, monkeypatch, voice, fails
):
    """Two bookings that read the same are two sessions on the calendar. One
    line for both hid the very double booking this ending is there to stop."""
    tool = scripted_model.tool_use
    scripted_model.append(([], [_easy_spin(tool), _easy_spin(tool)]))
    if fails:
        _fails_after(monkeypatch, scripted_model)
    else:
        scripted_model.append((["Done."], ()))

    _, said, _ = _send(db_session, rider, "Book me an easy ride on Monday", voice)

    assert db_session.query(Workout).count() == 2
    line = "Booked: Easy spin, Monday 19 October (two sessions)."
    assert said == ("\n\n".join([coach_service.REPLY_FAILED_CHANGES, line]) if fails else line)


@VOICE
def test_two_sessions_that_read_the_same_are_both_counted(
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

    assert said == "Skipped: Endurance, Monday 19 October (two sessions)."


def _too_hard(tool, day=WEDNESDAY):
    """An endurance session whose steps ask for 120% of FTP: refused for any
    rider, hold or none."""
    return tool("add_workout", {
        "scheduled_date": day.isoformat(), "title": "Hill reps", "workout_type": "endurance",
        "steps": _HARD_STEPS,
    })


@VOICE
@pytest.mark.parametrize("fails", [False, True], ids=["done", "failed call"])
@pytest.mark.parametrize("later", [False, True], ids=["same round", "later round"])
def test_a_change_that_did_not_go_in_is_never_left_off_the_list(
    db_session, rider, scripted_model, clock, monkeypatch, voice, later, fails
):
    """Monday's session went and Wednesday's was refused: the list alone read
    as the whole of the swap."""
    w = _session(db_session, rider)
    tool = scripted_model.tool_use
    skip = tool("skip_workout", {"workout_id": w.id})
    if later:
        scripted_model.extend([([], [skip]), ([], [_too_hard(tool)])])
    else:
        scripted_model.append(([], [skip, _too_hard(tool)]))
    if fails:
        _fails_after(monkeypatch, scripted_model, rounds=len(scripted_model))
    else:
        scripted_model.append((["Done."], ()))

    _, said, stored = _send(
        db_session, rider, "Swap Monday's threshold for hill reps on Wednesday", voice
    )

    results = [r for call in scripted_model.calls[1:] for r in _results(call)]
    assert any(r.startswith("Not changed. This session's steps ask for 120%") for r in results)
    lines = ["Skipped: Threshold reps, Monday 19 October.", NOT_ALL_IN]
    assert said == "\n\n".join(([coach_service.REPLY_FAILED_CHANGES] if fails else []) + lines)
    if not fails:
        # The next turn can answer "what didn't?" from the tool's own words.
        note = coach_service._build_messages(stored.session)[-1]["content"]
        assert "This session's steps ask for 120% of FTP" in note


@VOICE
@pytest.mark.parametrize("first", ["bad date", "wrong id", "too hard"])
def test_a_call_put_right_in_a_later_round_is_not_said_to_have_failed(
    db_session, rider, scripted_model, clock, voice, first
):
    tool = scripted_model.tool_use
    if first == "bad date":
        bad = tool("add_workout", {
            "scheduled_date": "19/10/2026", "title": "Easy spin", "workout_type": "endurance",
        })
        scripted_model.extend([([], [bad]), ([], [_easy_spin(tool)])])
        line = "Booked: Easy spin, Monday 19 October."
    elif first == "wrong id":
        w = _session(db_session, rider)
        scripted_model.extend([
            ([], [tool("skip_workout", {"workout_id": "not-a-session"})]),
            ([], [tool("skip_workout", {"workout_id": w.id})]),
        ])
        line = "Skipped: Threshold reps, Monday 19 October."
    else:
        eased = tool("add_workout", {
            "scheduled_date": WEDNESDAY.isoformat(), "title": "Hill reps",
            "workout_type": "endurance", "steps": STEADY_HOUR,
        })
        scripted_model.extend([([], [_too_hard(tool)]), ([], [eased])])
        line = "Booked: Hill reps, Wednesday 21 October."
    scripted_model.append((["Done."], ()))

    _, said, _ = _send(db_session, rider, "Can you sort that for me?", voice)

    assert _results(scripted_model.calls[1])[0].startswith(("Error", "Not changed"))
    assert said == line


@VOICE
@pytest.mark.parametrize("fails", [False, True], ids=["done", "failed call"])
@pytest.mark.parametrize("first", ["2025-10-19", "2026-10-12"], ids=["wrong year", "a Monday gone"])
def test_a_date_before_today_put_right_was_a_slip_not_a_change_that_failed(
    db_session, rider, scripted_model, clock, monkeypatch, voice, fails, first
):
    """A real date before today is a slip of the year or of the weekday sum
    (the 16 October incident was one), never a day the rider asked for, so
    the booking that followed on the right day put it right. The rider was
    told "Not everything went in" over one session that did."""
    tool = scripted_model.tool_use
    slip = tool("add_workout", {
        "scheduled_date": first, "title": "Easy spin", "workout_type": "endurance",
    })
    scripted_model.extend([([], [slip]), ([], [_easy_spin(tool)])])
    if fails:
        _fails_after(monkeypatch, scripted_model, rounds=2)
    else:
        scripted_model.append((["Done."], ()))

    _, said, stored = _send(db_session, rider, "Book me an easy ride on Monday", voice)

    assert _results(scripted_model.calls[1])[0].startswith(
        "Error: nothing was booked. scheduled_date must be"
    )
    assert db_session.query(Workout).count() == 1
    line = "Booked: Easy spin, Monday 19 October."
    assert said == ("\n\n".join([coach_service.REPLY_FAILED_CHANGES, line]) if fails else line)
    assert "not_in" not in (stored.context_snapshot or {})


@VOICE
def test_a_day_past_the_fortnight_is_the_day_that_was_asked_for(
    db_session, rider, scripted_model, clock, voice
):
    """A day the tool cannot book yet may be the one the rider named, so a
    booking on another day instead is not it going in."""
    tool = scripted_model.tool_use
    far = tool("add_workout", {
        "scheduled_date": (FRIDAY + timedelta(days=17)).isoformat(), "title": "Easy spin",
        "workout_type": "endurance",
    })
    scripted_model.extend([([], [far]), ([], [_easy_spin(tool)])])
    scripted_model.append((["Done."], ()))

    _, said, _ = _send(db_session, rider, "Book me an easy ride on the 2nd of November", voice)

    assert "cannot be booked yet" in _results(scripted_model.calls[1])[0]
    assert said == "Booked: Easy spin, Monday 19 October.\n\n" + NOT_ALL_IN


@VOICE
@pytest.mark.parametrize("tool_name", ["update_workout", "add_workout"])
@pytest.mark.parametrize("later", ["eased", "another change"])
def test_a_refused_change_is_put_right_only_by_the_same_change(
    db_session, rider, scripted_model, clock, voice, tool_name, later
):
    """Steps at 120% of FTP refused, then the session's title changed: the
    rider read "Changed: Hill reps" with no "Not everything went in", and
    the steps they asked for never went in. A later call puts a refused one
    right only when it carries what the refused call carried: steps for
    steps, a day for a day, a type for a type."""
    tool = scripted_model.tool_use
    eased = later == "eased"
    if tool_name == "update_workout":
        w = _session(db_session, rider, title="Easy spin", kind=WorkoutType.endurance)
        refused = tool("update_workout", {"workout_id": w.id, "steps": _HARD_STEPS})
        fix = tool("update_workout", {
            "workout_id": w.id, **({"steps": STEADY_HOUR} if eased else {"title": "Hill reps"}),
        })
        line = f"Changed: {'Easy spin' if eased else 'Hill reps'}, Monday 19 October."
    else:
        refused = _too_hard(tool)
        fix = tool("add_workout", {
            "scheduled_date": WEDNESDAY.isoformat(), "title": "Hill reps",
            "workout_type": "endurance", **({"steps": STEADY_HOUR} if eased else {}),
        })
        line = "Booked: Hill reps, Wednesday 21 October."
    scripted_model.extend([([], [refused]), ([], [fix])])
    scripted_model.append((["Done."], ()))

    _, said, stored = _send(db_session, rider, "Can you sort that for me?", voice)

    assert _results(scripted_model.calls[1])[0].startswith(
        "Not changed. This session's steps ask for 120%"
    )
    assert db_session.query(Workout).count() == 1
    assert db_session.query(WorkoutStep).count() == (1 if eased else 0)
    assert said == (line if eased else "\n\n".join([line, NOT_ALL_IN]))
    note = coach_service._build_messages(stored.session)[-1]["content"]
    assert ("steps ask for 120% of FTP" in note) == (not eased)


def _shared_ride(db, user):
    """A ride file shared in the chat, whose owner nobody has recorded: an
    easy hour, with nothing in it that would keep it on its card."""
    easy_hour = _gpx("2026-10-11T09:00:00", 3600, watts=150, hr=125, rpm=85)
    return attachment_service.ingest(db, user, easy_hour, "Sunday_spin.gpx")


@VOICE
@pytest.mark.parametrize("then", ["nothing", "saved after all"])
def test_a_file_save_that_did_not_happen_beside_a_booking_is_part_of_the_story(
    db_session, rider, scripted_model, clock, voice, then
):
    """The save the rider asked for was refused and the booking went in:
    "Booked: Easy spin" alone read as the whole outcome, with no card for
    the file. A save is never listed (its card says it), but one that did
    not happen is part of the story, and a later save of the same file puts
    it right."""
    attachment = _shared_ride(db_session, rider)
    tool = scripted_model.tool_use
    save = {"attachment_id": attachment.id, "confirmed": True}
    scripted_model.append(([], [tool("save_attachment_as_ride", save), _easy_spin(tool)]))
    saved = then == "saved after all"
    if saved:
        scripted_model.append(([], [
            tool("set_attachment_owner", {"attachment_id": attachment.id, "owner": "self"}),
            tool("save_attachment_as_ride", save),
        ]))
    scripted_model.append((["Done."], ()))

    chunks, said, stored = _send(
        db_session, rider, "Save that ride and book me an easy spin on Monday", voice
    )

    not_saved = _results(scripted_model.calls[1])[0]
    assert not_saved.startswith("Error: nothing was saved. Whose ride this file is")
    assert db_session.query(Ride).count() == (1 if saved else 0)
    assert any(c["type"] == "files_updated" and c["saved"] for c in chunks) == saved
    line = "Booked: Easy spin, Monday 19 October."
    assert said == (line if saved else "\n\n".join([line, NOT_ALL_IN]))
    note = coach_service._build_messages(stored.session)[-1]["content"]
    assert ("Whose ride this file is has not been recorded" in note) == (not saved)


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
        })]], ["Moved and changed: Easy spin, now on Wednesday 21 October."]
    if what in ("skipped, then put back", "skipped again"):
        w = _session(db, user)
        w.status = WorkoutStatus.skipped
        db.commit()
        if what == "skipped again":
            return [[tool("skip_workout", {"workout_id": w.id})]], []
        return [[tool("update_workout", {
            "workout_id": w.id, "scheduled_date": MONDAY.isoformat(),
        })]], ["Back on the plan: Threshold reps, Monday 19 October."]
    assert what == "goal given its own date"
    goal = _goal(db, user, date(2027, 5, 30), name="Mallorca 312")
    return [[tool("update_goal", {"goal_id": goal.id, "event_date": "2027-05-30"})]], []


NET = [
    "booked, then given steps", "booked, then moved", "booked, then skipped",
    "moved, then moved back", "swapped twice", "moved under its own name", "moved and changed",
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
    assert (plan.start_date, plan.end_date) == (FRIDAY, date(2027, 4, 24))
    assert said == (
        "Goal set: Mallorca 312, Saturday 24 April 2027.\n\n"
        f"New plan: {plan.name}, Friday 16 October to Saturday 24 April 2027."
    )


@VOICE
def test_a_goal_moved_off_the_end_of_its_plan_says_the_plan_still_ends_on_the_old_day(
    db_session, scripted_model, clock, voice
):
    """The goal moved and the plan built for its old date did not: "Goal
    changed" alone left the rider on a plan that peaks five weeks early,
    with the tool's "tell the rider to rebuild it" dropped with the words."""
    gareth, _, _ = _incident(db_session)
    filed = _execute_tool(db_session, gareth, "create_goal", {
        "event_name": "Mallorca 312", "event_date": "2027-04-24",
        "event_type": "gran_fondo", "priority": "a_race",
    })
    assert "The plan has been rebuilt around it" in filed
    goal = db_session.query(GoalEvent).filter(GoalEvent.event_name == "Mallorca 312").one()
    scripted_model.append(([], [scripted_model.tool_use("update_goal", {
        "goal_id": goal.id, "event_date": "2027-05-29",
    })]))
    scripted_model.append((["Done."], ()))

    _, said, _ = _send(db_session, gareth, "The 312 has moved to the 29th of May", voice)

    assert "was built for the old date" in _results(scripted_model.calls[1])[0]
    [plan] = _active(db_session, gareth)
    assert plan.end_date == date(2027, 4, 24)
    assert said == (
        "Goal changed: Mallorca 312, Saturday 29 May 2027. Your plan still ends on "
        "Saturday 24 April 2027. Ask me when you want it rebuilt."
    )


@VOICE
@pytest.mark.parametrize("why", ["restriction", "never set up"])
def test_a_goal_no_plan_was_built_for_says_so(
    db_session, rider, scripted_model, clock, voice, why
):
    """The tool told the coach the plan was not built and to say so; "Goal
    set" alone read as a goal with a plan behind it."""
    if why == "restriction":
        user, _, _ = _incident(db_session)
        _restricted(db_session, user)
    else:
        user = rider
    scripted_model.append(([], [scripted_model.tool_use("create_goal", {
        "event_name": "Mallorca 312", "event_date": "2027-04-24",
        "event_type": "gran_fondo", "priority": "a_race",
    })]))
    scripted_model.append((["Done."], ()))

    _, said, _ = _send(db_session, user, "Let's aim at the 312 in April", voice)

    result = _results(scripted_model.calls[1])[0]
    assert ("The plan was NOT rebuilt" in result) == (why == "restriction")
    assert ("There is no current plan" in result) == (why == "never set up")
    assert _active(db_session, user) == []
    assert said == "Goal set: Mallorca 312, Saturday 24 April 2027. " + NO_PLAN_YET


@VOICE
@pytest.mark.parametrize("day, line", [
    (MONDAY, (
        "Booked: Easy spin, Monday 19 October. It's on the Goal page, not the Today page: "
        "Rest day comes first that day."
    )),
    (FRIDAY, (
        "Booked: Easy spin, Friday 16 October. It's on the Goal page, not the Today page: "
        "Rest day comes first today."
    )),
], ids=["Monday", "today"])
def test_a_booking_behind_another_session_says_where_it_is(
    db_session, rider, scripted_model, clock, monkeypatch, voice, day, line
):
    """Today shows a day's first session only: the rider looking there for
    the booking would not find it. The pages are named as pages: read
    aloud, "It's on Goal, not Today" after a booking for today said the
    opposite of what it meant."""
    spoken = []

    async def tts(text):
        spoken.append(text)
        return b"audio"

    monkeypatch.setattr("app.services.voice_service.is_voice_enabled", lambda: voice)
    monkeypatch.setattr("app.services.voice_service.text_to_speech", tts)
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
MARKS = ("fallback", "code_written", "safety_cards", "safety_card")


@VOICE
@pytest.mark.parametrize("fails", [False, True], ids=["done", "failed call"])
@pytest.mark.parametrize("why", [
    "head knock", "crisis", "hold", "under 18",
    "coach flags crisis", "coach flags minor", "coach flags safety",
    "coach flags crisis later", "coach hold refused",
])
def test_a_safety_turn_with_a_change_ends_as_it_always_has(
    db_session, rider, scripted_model, clock, monkeypatch, voice, fails, why
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
        scripted_model.append((["Done."], ()))
        with monkeypatch.context() as m:
            if fails:
                _fails_after(m, scripted_model, rounds=len(rounds))
            if not lines_on:
                m.setattr(coach_service._TurnChanges, "ending", lambda *a, **k: [])
            _, said, stored = _send(db_session, user, message, voice)
        went_in = [r for call in scripted_model.calls[1:] for r in _results(call)]
        assert any(r.startswith(("Booked", "Skipped")) for r in went_in)
        marks = {k: (stored.context_snapshot or {}).get(k) for k in MARKS}
        return said, stored.content, marks

    said, content, snapshot = run(rider, lines_on=True)
    assert run(twin, lines_on=False) == (said, content, snapshot)
    for line in ("Booked:", "Skipped:", coach_service.REPLY_FAILED_CHANGES):
        assert line not in said
    assert snapshot["code_written"] is None


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
