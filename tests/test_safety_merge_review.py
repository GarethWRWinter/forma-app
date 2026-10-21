"""The joins the merge review found open, pinned.

Main's season keeper and goal tools build plans with no rider action, and
its booking flow confirms in a few words; the safety branch's record (an
injury hold, eating very little, words of hopelessness, an account held as
under 18) and its reply check were written before either existed. Each test
below is a case where the two disagreed.
"""

import asyncio
import json
import threading
from datetime import date, datetime, timedelta

import pytest

from app.api.v1 import goals as goals_api
from app.models.chat import ChatMessage, ChatRole, ChatSession
from app.models.onboarding import EventPriority, GoalStatus
from app.models.safety import SafetyEvent, SafetyHold
from app.models.training import TrainingPlan, Workout, WorkoutStatus, WorkoutType
from app.models.user import User
from app.schemas.onboarding import GoalAssessmentSubmit
from app.services import attachment_service, coach_service, file_triage_service, safety_screen
from app.services import safety_service as ss
from app.services.coach_service import _build_rider_context, _execute_tool
from app.services.safety_screen import ReplyGuard
from app.services.season_service import BLOCK_NAME, ensure_all, season_note, season_state
from tests.test_chat_file_triage import PB, PB_BYTES
from tests.test_coach_safety_flow import _FakeClassifier
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


BARE = ["Done.", "All set.", "Booked."]


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
@pytest.mark.parametrize("confirmation", BARE)
def test_a_bare_confirmation_after_a_booking_reaches_the_rider(
    db_session, rider, scripted_model, voice, confirmation
):
    """A whole reply of "Done." after add_workout said "Booked." was dropped
    as narration: nothing was left, and the rider was told the coach could
    not answer over a session that was booked. Main sends it as it is."""
    scripted_model.append(([], [_booking(scripted_model.tool_use, "endurance")]))
    scripted_model.append(([confirmation], ()))

    chunks, said, stored = _send(db_session, rider, "Book me an easy ride on Monday", voice)

    assert scripted_model.calls[1]["messages"][-1]["content"][0]["content"].startswith("Booked.")
    assert said.strip() == confirmation
    assert any(c["type"] == "plan_updated" for c in chunks)
    assert not (stored.context_snapshot or {}).get("fallback")
    assert db_session.query(Workout).count() == 1


@pytest.mark.parametrize("confirmation", BARE)
def test_a_bare_confirmation_after_a_refused_booking_is_dropped(
    db_session, rider, scripted_model, confirmation
):
    _injured(db_session, rider)
    scripted_model.append(([], [_booking(scripted_model.tool_use)]))
    scripted_model.append(([confirmation], ()))

    chunks, said, _ = _send(db_session, rider, "Can you add a threshold session on Tuesday?")

    assert scripted_model.calls[1]["messages"][-1]["content"][0]["content"].startswith(
        "Not changed"
    )
    assert confirmation not in said
    assert not any(c["type"] == "plan_updated" for c in chunks)
    assert db_session.query(Workout).count() == 0


@pytest.mark.parametrize("confirmation", BARE)
@pytest.mark.parametrize("lookup", [False, True], ids=["no tool", "a lookup only"])
def test_a_bare_confirmation_with_no_plan_tool_behind_it_is_dropped(
    db_session, rider, scripted_model, confirmation, lookup
):
    if lookup:
        scripted_model.append(([], [scripted_model.tool_use("find_ride", {"query": "yesterday"})]))
    scripted_model.append(([confirmation], ()))

    _, said, stored = _send(db_session, rider, "Book me an easy ride on Monday")

    assert confirmation not in said
    assert said.strip() == coach_service._NO_REPLY_FALLBACK
    assert stored.context_snapshot["fallback"] is True
    assert db_session.query(Workout).count() == 0


def test_a_bare_confirmation_after_a_change_announced_before_the_tool_is_dropped(
    db_session, rider, scripted_model
):
    announced = "I'm booking you an easy ride on Monday."
    scripted_model.append(([announced], [_booking(scripted_model.tool_use, "endurance")]))
    scripted_model.append((["Done."], ()))

    _, said, _ = _send(db_session, rider, "Book me an easy ride on Monday")

    assert said.strip() == announced
    assert db_session.query(Workout).count() == 1


def _plain_guard():
    return coach_service._CheckedReply(None, "GB", lambda: None)


BOOKED = ("add_workout", "Booked. 'Easy spin' (60 min) is on Monday 19 October.")


def test_a_bare_confirmation_is_the_last_word_or_none():
    """It goes out after a booking as the last word, or in front of the
    coach's next words; one written before the tool waits for the tool, and
    goes only if the booking went in. As the last word it is decided at the
    end (finish_tail), after the classifier's time has run out."""
    guard = _plain_guard()
    guard.feed("Easy it is.")
    guard.before_tools()
    guard.after_tools([BOOKED])
    assert guard.round_break() + guard.feed("Done.") + guard.finish_body() == ""
    assert guard.finish_tail() == "\n\nDone."

    guard = _plain_guard()
    guard.before_tools()
    guard.after_tools([BOOKED])
    out = guard.feed("Done. It's on Monday 19 October.") + guard.flush()
    assert out == "Done. It's on Monday 19 October."

    guard = _plain_guard()
    assert guard.feed("All set. ") + guard.before_tools() == ""
    assert guard.after_tools([BOOKED]) == ""
    assert guard.flush() == "All set. "

    guard = _plain_guard()
    guard.feed("All set. ")
    guard.before_tools()
    guard.after_tools([BOOKED, ("add_workout", "Not changed: an injury hold is open.")])
    assert guard.flush() == "" and "All set." in guard.dropped

    guard = _plain_guard()
    guard.before_tools()
    guard.after_tools([BOOKED, ("apply_safety_hold", "Hold applied now.")])
    assert guard.feed("Done.") + guard.flush() == ""

    # A red-flag turn keeps its whole fixed reply over a bare "Done.".
    guard = _plain_guard()
    guard.kinds = {"injury"}
    guard.before_tools()
    guard.after_tools([BOOKED])
    assert "Done." not in guard.feed("Done.") + guard.flush()
    assert "Done." in guard.dropped


SKIPPED = ("skip_workout", "Skipped 'PB reps' on Tuesday 20 October.")
UPDATED = ("update_workout", "Updated 'PB reps': now on Wednesday 21 October.")
# The same update with the call that made it: given a date, it moved.
MOVED = UPDATED + ({"workout_id": "w1", "scheduled_date": "2026-10-21"},)


@pytest.mark.parametrize("results, confirmation", [
    ([BOOKED], "Booked."),
    ([SKIPPED], "Skipped."),
    ([MOVED], "Moved."),
    ([UPDATED], "All done."),
])
def test_a_bare_confirmation_names_the_change_that_went_in(results, confirmation):
    guard = _plain_guard()
    guard.before_tools()
    guard.after_tools(results)
    assert guard.feed(confirmation) + guard.flush() == confirmation


def _booked_then(*rounds, after=None):
    """The reply after a booking went in: each entry in `rounds` is one
    round's words, with a lookup between them."""
    guard = _plain_guard()
    guard.before_tools()
    out = guard.after_tools([BOOKED])
    for i, words in enumerate(rounds):
        if i:
            out += guard.before_tools()
            out += guard.after_tools([("find_ride", "Yesterday's ride: 60 min, 180 W.")])
        out += guard.round_break() + guard.feed(words)
    if after:
        after(guard)
    return out + guard.flush(), guard


@pytest.mark.parametrize("rounds, after, shown", [
    # A hold is only ever stated in the code's words: none is on record.
    (["Hold applied."], None, ""),
    (["That's applied."], None, ""),
    (["Applied."], None, ""),
    # The verb names a change that did not go in.
    (["Skipped."], None, ""),
    (["Moved."], None, ""),
    # Tacked on after the coach's own words, in its round or since the change.
    (["It's on Monday 19 October. Done."], None, "It's on Monday 19 October. "),
    (["It's on Monday 19 October. ", "Done."], None, "It's on Monday 19 October. "),
    # The model call failed after it: the coach never finished.
    (["Done. "], lambda g: setattr(g, "cut_off", True), ""),
    # Written in a round the code took the tools away for.
    (["Done."], lambda g: setattr(g, "tools_withheld", True), ""),
], ids=[
    "hold applied", "that's applied", "applied", "skipped", "moved",
    "after words", "after an earlier round's words", "cut off", "tools withheld",
])
def test_a_bare_confirmation_that_would_claim_more_is_dropped(rounds, after, shown):
    out, guard = _booked_then(*rounds, after=after)
    assert out == shown
    assert guard.dropped[-1] in rounds[-1] and guard.dropped[-1] not in out


@pytest.mark.parametrize("before", [
    "Easy spin on Monday, booked in. Done. ",
    "I've booked it. Done. ",
])
def test_a_bare_confirmation_beside_words_before_the_tool_is_dropped(before):
    guard = _plain_guard()
    out = guard.feed(before) + guard.before_tools() + guard.after_tools([BOOKED])
    assert out + guard.flush() == before.removesuffix("Done. ")
    assert "Done." in guard.dropped


def test_a_bare_confirmation_cut_off_by_a_failed_call_is_dropped():
    """"Done." and then "I couldn't finish a reply" contradicted each other.
    Nor is the rider told to send it again: the booking went in."""
    guard = _plain_guard()
    guard.before_tools()
    guard.after_tools([BOOKED])
    guard.feed("Done. ")
    out = coach_service._reply_after_failure(
        guard, coach_service.StreamHumanizer(), "", safety_screen.ScreenResult()
    )
    assert out == coach_service.REPLY_FAILED_PLAN_CHANGED


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
def test_a_bare_confirmation_after_the_tools_were_taken_away_is_dropped(
    db_session, rider, scripted_model, voice
):
    """Monday went in; the third ride lookup was refused, so the last round
    ran without tools and Tuesday's booking never did. "Done." would say it
    had."""
    tool = scripted_model.tool_use
    scripted_model.append(([], [
        tool("find_ride", {"query": "yesterday"}), tool("find_ride", {"query": "Sunday"}),
        tool("find_ride", {"query": "Saturday"}), _booking(tool, "endurance"),
    ]))
    scripted_model.append((["Done."], [_booking(tool, "endurance")]))

    _, said, _ = _send(db_session, rider, "Book Monday and Tuesday like last week", voice)

    assert scripted_model.calls[1]["tool_choice"] == {"type": "none"}
    assert "Done." not in said
    assert db_session.query(Workout).count() == 1


LATE = {
    "chest": (
        ("chest_pain", "very wrong in my ribs on the climb", "emergency"),
        "Something felt very wrong in my ribs on the climb and I had to sit on the verge.",
    ),
    "crisis": (
        ("crisis", "gave the dog to my sister", "crisis"),
        "Sorted my will out and gave the dog to my sister, so the plan won't matter after Sunday.",
    ),
    "minor": (("minor", "my dad set this account up", "minor", 15), "My dad set this account up."),
}


@pytest.fixture
def late_verdict(monkeypatch):
    """The classifier's verdict lands after the model's last words, while the
    turn waits out its time (_TurnSafety.settle), as it can in production
    with no head start."""
    fake = _FakeClassifier()
    fake.gate = threading.Event()
    monkeypatch.setattr(
        coach_service, "_load_classifier", lambda: (fake.classify_safety, fake.merge)
    )
    monkeypatch.setattr(coach_service, "CLASSIFIER_HEAD_START_SECONDS", 0.0)
    settle = coach_service._TurnSafety.settle

    async def land_then_settle(self):
        fake.gate.set()
        return await settle(self)

    monkeypatch.setattr(coach_service._TurnSafety, "settle", land_then_settle)
    yield fake
    fake.gate.set()


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
@pytest.mark.parametrize("kind", sorted(LATE))
@pytest.mark.parametrize("last", [
    "Done.", "Done, it's booked for Monday.", "Done, you're all set.",
], ids=["bare", "booked for monday", "you're all set"])
def test_a_late_red_flag_gets_the_same_fixed_reply_over_a_bare_confirmation(
    db_session, rider, scripted_model, late_verdict, voice, kind, last
):
    """The "Done." went out as the model stopped, before the verdict landed:
    the rider then got "Done." and the missing lines, not the whole fixed
    reply (a crisis lost its question and means safety, chest pain "Don't
    ride", and an under-18 account was told its booking was done). What a
    "Done, ..." said after its "Done" did the same: chest pain lost "Don't
    ride, not even an easy spin" and "Don't drive yourself"."""
    hit, words = LATE[kind]
    late_verdict.adds = [hit]
    other = User(email="other@example.com", hashed_password="x", full_name="Alex Rider", ftp=250)
    db_session.add(other)
    db_session.commit()
    replies = []
    for user, last_words in ((rider, [last]), (other, [])):
        late_verdict.gate.clear()
        scripted_model.append(([], [_booking(scripted_model.tool_use, "endurance")]))
        scripted_model.append((last_words, ()))
        _, said, _ = _send(db_session, user, words + " Book me an easy ride on Monday.", voice)
        replies.append(said)

    done, silent = replies
    said_after = last.split(", ", 1)[-1]
    assert "Done." not in done and said_after[:1].upper() + said_after[1:] not in done
    assert done == silent
    assert len(late_verdict.calls) == 2


# ── Confirmations, second review ────────────────────────────────────────────

CRISIS = "I feel worthless and I keep thinking everyone would be better off without me."
BOOK_MONDAY = " Book me an easy ride on Monday."


def _against_silence(db, rider, scripted_model, message, rounds, voice=False):
    """What the rider sees with `rounds` scripted, and what a second rider
    sees for the same message when the coach's words are left out of the
    same rounds."""
    other = User(email="other@example.com", hashed_password="x", full_name="Alex Rider", ftp=250)
    db.add(other)
    db.commit()
    replies = []
    for user, keep in ((rider, True), (other, False)):
        for texts, blocks in rounds:
            scripted_model.append((texts if keep else [], blocks(scripted_model.tool_use)))
        _, said, _ = _send(db, user, message, voice)
        replies.append(said)
    return replies


def _book(tool):
    return [_booking(tool, "endurance")]


def _nothing(tool):
    return []


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
@pytest.mark.parametrize("last", ["Done, you're all set.", "Done, it's booked for Monday."])
def test_a_confirmation_in_a_crisis_turn_never_stands_in_for_the_fixed_reply(
    db_session, rider, scripted_model, voice, last
):
    """"Done, you're all set." kept "You're all set." once the booking went
    in, which counted as the reply: the rider lost the question, means
    safety and "No one at Forma reads chats" for a few guaranteed lines."""
    done, silent = _against_silence(
        db_session, rider, scripted_model, CRISIS + BOOK_MONDAY,
        [([], _book), ([last], _nothing)], voice,
    )
    assert "Are you having thoughts of ending your life" in silent
    assert done == silent


def test_what_a_confirmation_says_still_goes_out_with_the_coachs_words():
    guard = _plain_guard()
    guard.before_tools()
    guard.after_tools([BOOKED])
    out = guard.feed("Done, it's booked for Monday 19 October. Keep it easy.") + guard.flush()
    assert out == "It's booked for Monday 19 October. Keep it easy."

    # Beside a red flag too: the words after it go out whatever happens.
    guard = _plain_guard()
    guard.kinds = {"injury"}
    guard.before_tools()
    guard.after_tools([BOOKED])
    out = guard.feed("Done, it's booked for Monday 19 October. Keep it easy.") + guard.flush()
    assert out.startswith("It's booked for Monday 19 October. Keep it easy.")


@pytest.mark.parametrize("word", ["Hold applied.", "Applied.", "That's applied.", "Paused."])
def test_a_hold_is_never_confirmed_in_the_coachs_words_before_the_tool(
    db_session, rider, scripted_model, word
):
    """Written before apply_safety_hold, which then refused the level: the
    rider was told a hold was on, and none was."""
    scripted_model.append(([word], [scripted_model.tool_use(
        "apply_safety_hold", {"level": "pause", "reason": "knee"},
    )]))
    scripted_model.append(([], ()))

    _, said, _ = _send(db_session, rider, "My knee aches a bit.")

    assert word not in said
    assert ss.current_hold(db_session, rider.id) is None


def test_a_hold_confirmed_before_a_failed_tool_never_cuts_a_crisis_reply(
    db_session, rider, scripted_model
):
    def failed_hold(tool):
        return [tool("apply_safety_hold", {"level": "pause", "reason": "crisis"})]

    done, silent = _against_silence(
        db_session, rider, scripted_model, CRISIS + BOOK_MONDAY,
        [(["Applied."], failed_hold), ([], _nothing)],
    )
    assert "Applied." not in done
    assert done == silent


CHEST = "I had chest pain on the climb this morning."
KNEE = "Sharp pain on the outside of my knee when I climb."


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
@pytest.mark.parametrize("message, word", [
    (CHEST, "You're all set."),
    (KNEE, "You're booked in."),
    (KNEE, "Booked in for Monday."),
    (CRISIS, "Scheduled."),
    (CRISIS, "Locked in."),
    (CRISIS, "Pencilled in for Monday."),
])
def test_a_confirmation_in_other_words_never_stands_in_for_the_fixed_reply(
    db_session, rider, scripted_model, voice, message, word
):
    """Worded outside the confirmation words, it was never held or checked:
    after the gate refused the booking the rider was told it was booked, and
    beside a red flag it cut the fixed reply to a few lines."""
    done, silent = _against_silence(
        db_session, rider, scripted_model, message + BOOK_MONDAY,
        [([], _book), ([word], _nothing)], voice,
    )
    assert word not in done
    assert done == silent


@pytest.mark.parametrize("word", [
    "You're all set.", "You're booked in.", "Booked in for Monday.", "Scheduled.",
    "Locked in.", "It's in.",
])
def test_a_confirmation_in_other_words_after_a_refused_booking_is_dropped(word):
    guard = _plain_guard()
    guard.before_tools()
    guard.after_tools([("add_workout", "Not changed: an injury hold is open.")])
    assert guard.feed(word) + guard.flush() == ""
    assert word in guard.dropped

    guard = _plain_guard()
    guard.before_tools()
    guard.after_tools([BOOKED])
    assert guard.feed(word) + guard.flush() == word


def _tuesday(tool):
    return tool("add_workout", {
        "scheduled_date": (date.today() + timedelta(days=4)).isoformat(),
        "title": "Easy spin", "workout_type": "endurance",
    })


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
@pytest.mark.parametrize("before_the_cut", [False, True], ids=["in the cut round", "before"])
def test_a_confirmation_in_a_call_cut_at_its_output_allowance_is_dropped(
    db_session, rider, scripted_model, voice, before_the_cut
):
    """A round that ran out of output with Tuesday's booking unrun ended the
    loop as if the coach had finished: "Done." went out over one booking of
    two."""
    tool = scripted_model.tool_use
    if before_the_cut:
        scripted_model.append((["Done."], [_booking(tool, "endurance")]))
        scripted_model.append(([], [_tuesday(tool)], "max_tokens"))
    else:
        scripted_model.append(([], [_booking(tool, "endurance")]))
        scripted_model.append((["Done."], [_tuesday(tool)], "max_tokens"))

    _, said, _ = _send(db_session, rider, "Book me an easy ride on Monday and Tuesday.", voice)

    assert "Done." not in said
    assert db_session.query(Workout).count() == 1


def _planned(db, user, days=2, wtype=WorkoutType.threshold) -> Workout:
    workout = Workout(
        user_id=user.id, scheduled_date=date.today() + timedelta(days=days),
        title="Threshold reps", workout_type=wtype, planned_duration_seconds=3600,
        status=WorkoutStatus.planned,
    )
    db.add(workout)
    db.commit()
    return workout


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
@pytest.mark.parametrize("word", ["Moved.", "All moved."])
def test_moved_never_goes_out_over_a_session_left_on_its_day(
    db_session, rider, scripted_model, voice, word
):
    """update_workout changed only the type: the session stayed on its day,
    and "Moved." said it had gone to Thursday."""
    workout = _planned(db_session, rider)
    day = workout.scheduled_date
    scripted_model.append(([], [scripted_model.tool_use(
        "update_workout", {"workout_id": workout.id, "workout_type": "endurance"},
    )]))
    scripted_model.append(([word], ()))

    _, said, _ = _send(
        db_session, rider, "Move Tuesday's session to Thursday and make it easy.", voice
    )

    assert word not in said
    db_session.refresh(workout)
    assert workout.scheduled_date == day and _type(workout) == "endurance"


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
@pytest.mark.parametrize("change, word", [
    ("skip and book", "Swapped."),
    ("skip and book", "Moved."),
    ("new date", "Booked."),
    ("new date", "Moved."),
    ("new type", "Swapped."),
])
def test_a_bare_verb_names_what_the_change_did(
    db_session, rider, scripted_model, voice, change, word
):
    """A swap the coach made by skipping one session and booking another,
    and a session moved to a new day (booked there), got the no-reply
    apology: the verb named a tool that had not run."""
    workout = _planned(db_session, rider, days=1)
    tool = scripted_model.tool_use
    if change == "skip and book":
        blocks = [tool("skip_workout", {"workout_id": workout.id}), tool("add_workout", {
            "scheduled_date": workout.scheduled_date.isoformat(), "title": "Easy spin",
            "workout_type": "endurance",
        })]
    elif change == "new date":
        blocks = [tool("update_workout", {
            "workout_id": workout.id,
            "scheduled_date": (date.today() + timedelta(days=3)).isoformat(),
        })]
    else:
        blocks = [tool("update_workout", {"workout_id": workout.id, "workout_type": "endurance"})]
    scripted_model.append(([], blocks))
    scripted_model.append(([word], ()))

    _, said, stored = _send(db_session, rider, "Swap tomorrow's session for an easy ride", voice)

    assert said.strip() == word
    assert not (stored.context_snapshot or {}).get("fallback")


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
@pytest.mark.parametrize("message", [
    "Banged my head on the cupboard door, ouch, anyway.",
    "What should I do if I get chest pain on a ride?",
], ids=["head knock", "safety question"])
def test_a_quiet_red_flag_keeps_a_true_confirmation_and_never_asks_again(
    db_session, rider, scripted_model, voice, message
):
    """Their fixed reply opens with the no-reply apology: over a session
    that was booked, the rider was told to send it again, and booking it
    twice was one message away."""
    scripted_model.append(([], [_booking(scripted_model.tool_use, "endurance")]))
    scripted_model.append((["Done."], ()))

    chunks, said, _ = _send(db_session, rider, message + BOOK_MONDAY, voice)

    assert said.startswith("Done.\n\n") and len(said) > len("Done.") + 40
    assert safety_screen.REPLY_FAILED_MESSAGE not in said
    assert any(c["type"] == "plan_updated" for c in chunks)
    assert db_session.query(Workout).count() == 1


def test_a_quiet_red_flag_never_asks_again_over_a_change_that_went_in():
    guard = _plain_guard()
    guard.kinds = {"head_knock"}
    guard.before_tools()
    guard.after_tools([BOOKED, ("add_workout", "Not changed: there is a session that day.")])
    out = guard.feed("Done.") + guard.flush()
    assert out.startswith(coach_service.REPLY_FAILED_PLAN_CHANGED)
    assert safety_screen.REPLY_FAILED_MESSAGE not in out


class _BreaksAfterItsWords:
    """A model stream that sends its words, then fails."""

    def __init__(self, stream):
        self._stream = stream

    def __iter__(self):
        yield from self._stream
        raise RuntimeError("the stream broke")

    def get_final_message(self):
        return self._stream.get_final_message()


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
def test_a_failed_call_after_a_booking_never_asks_for_the_message_again(
    db_session, rider, scripted_model, monkeypatch, voice
):
    from contextlib import contextmanager

    from app.core import forma_core

    scripted = forma_core.stream

    @contextmanager
    def second_call_breaks(**kwargs):
        with scripted(**kwargs) as stream:
            yield _BreaksAfterItsWords(stream) if len(scripted_model.calls) == 2 else stream

    monkeypatch.setattr(forma_core, "stream", second_call_breaks)
    scripted_model.append(([], [_booking(scripted_model.tool_use, "endurance")]))
    scripted_model.append((["Done. "], ()))

    chunks, said, _ = _send(db_session, rider, "Book me an easy ride on Monday", voice)

    assert said.strip() == coach_service.REPLY_FAILED_PLAN_CHANGED
    assert any(c["type"] == "plan_updated" for c in chunks)
    assert db_session.query(Workout).count() == 1


@pytest.mark.parametrize("results, text, shown", [
    ([BOOKED], "Booked in. Enjoy it.", "Booked in. Enjoy it."),
    ([BOOKED], "Booked. Keep the effort conversational.", "Booked. Keep the effort conversational."),
    ([BOOKED], "Done. Enjoy the ride!", "Done. Enjoy the ride!"),
    ([BOOKED], "That's in. Keep it easy.", "That's in. Keep it easy."),
    ([BOOKED], "Done! Anything else you want changed?", "Done! Anything else you want changed?"),
    ([SKIPPED], "Skipped. Get some sleep.", "Skipped. Get some sleep."),
    ([MOVED], "Moved. Rest up tomorrow.", "Moved. Rest up tomorrow."),
    # Untrue, it still goes: the verb names a change that did not go in,
    # or the booking was refused.
    ([BOOKED], "Moved. Rest up tomorrow.", "Rest up tomorrow."),
    ([("add_workout", "Not changed: an injury hold is open.")], "Done. Enjoy the ride!",
     "Enjoy the ride!"),
], ids=[
    "booked in", "booked", "done", "that's in", "done!", "skipped", "moved", "moved over a booking",
    "refused",
])
def test_a_true_confirmation_goes_in_front_of_the_coachs_words(results, text, shown):
    """A sign-off after it took its place, and the rider lost the only
    words saying the change went in."""
    guard = _plain_guard()
    guard.before_tools()
    guard.after_tools(results)
    assert guard.feed(text) + guard.flush() == shown


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
def test_a_true_confirmation_before_a_sign_off_reaches_the_rider(
    db_session, rider, scripted_model, voice
):
    scripted_model.append(([], [_booking(scripted_model.tool_use, "endurance")]))
    scripted_model.append((["Booked. Enjoy it."], ()))

    _, said, stored = _send(db_session, rider, "Book me an easy ride on Monday", voice)

    assert said.strip() == "Booked. Enjoy it."
    assert stored.content.endswith("Booked. Enjoy it.")


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


def test_a_released_confirmation_is_spoken_in_voice_mode(db_session, rider, scripted_model, monkeypatch):
    """Only sentences over five characters were spoken: the rider saw "Done."
    and heard nothing about a booking that went in."""
    spoken = []

    async def tts(text):
        spoken.append(text)
        return b"audio"

    monkeypatch.setattr("app.services.voice_service.is_voice_enabled", lambda: True)
    monkeypatch.setattr("app.services.voice_service.text_to_speech", tts)
    scripted_model.append(([], [_booking(scripted_model.tool_use, "endurance")]))
    scripted_model.append((["Done."], ()))

    chunks, said, _ = _send(db_session, rider, "Book me an easy ride on Monday", voice=True)

    assert said.strip() == "Done."
    assert spoken[-1] == "Done."
    assert sum(c["type"] == "audio" for c in chunks) == len(spoken)


@pytest.mark.parametrize("sentence", [
    "You're booked in for an easy spin on Monday, so keep it conversational.",
    "It's in your plan for Monday, an hour of easy spinning.",
])
def test_a_booking_claim_in_the_riders_words_after_a_refusal_is_dropped(sentence):
    """"I've booked" waits for the tool; "You're booked in" went out over a
    booking the gate refused."""
    guard = _plain_guard()
    guard.before_tools()
    guard.after_tools([("add_workout", "Not changed: an injury hold is open.")])
    out = guard.feed("That one couldn't go in. " + sentence) + guard.flush()
    assert out == "That one couldn't go in. "
    assert sentence in guard.dropped

    guard = _plain_guard()
    guard.before_tools()
    guard.after_tools([BOOKED])
    assert guard.feed(sentence) + guard.flush() == sentence


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
@pytest.mark.parametrize("message", [CHEST, CRISIS], ids=["chest", "crisis"])
@pytest.mark.parametrize("words", ["Done, you're all set.", "All set, enjoy the spin."])
def test_a_confirmation_written_before_the_tool_never_stands_in_for_the_fixed_reply(
    db_session, rider, scripted_model, voice, message, words
):
    """Written before any tool, a "Done, ..." went out as written: chest pain
    lost "Don't ride" over a booking the gate refused, and a crisis lost its
    question and means safety."""
    done, silent = _against_silence(
        db_session, rider, scripted_model, message + BOOK_MONDAY,
        [([words], _book), ([], _nothing)], voice,
    )
    assert done == silent


def test_a_confirmation_written_before_the_tool_goes_out_only_if_the_change_did():
    def reply(results, words="All set, enjoy the spin. "):
        guard = _plain_guard()
        out = guard.feed(words) + guard.before_tools() + guard.after_tools(results)
        return out + guard.flush()

    assert reply([("add_workout", "Not changed: an injury hold is open.")]) == ""
    assert reply([BOOKED]) == "All set, enjoy the spin. "
    # With no tool after it, it is the coach's answer, as before.
    guard = _plain_guard()
    words = "Sorted, you've got three rides this week."
    assert guard.feed(words) + guard.flush() == words


# ── Confirmations, third review ─────────────────────────────────────────────

REFUSED = ("add_workout", "Not changed: an injury hold is open.")
EATING = "I've been eating about 800 calories a day to get my weight down fast."


def _refused_then(words, results=(REFUSED,)):
    guard = _plain_guard()
    guard.before_tools()
    guard.after_tools(list(results))
    return guard.feed(words) + guard.flush(), guard


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
@pytest.mark.parametrize("message", [CHEST, CRISIS, KNEE, EATING], ids=[
    "chest", "crisis", "knee", "eating",
])
@pytest.mark.parametrize("word", [
    "It's done.", "Yep, done.", "Monday's booked.", "Easy spin booked for Monday.",
    "Booked it.", "Added to your plan.",
])
def test_a_confirmation_in_any_words_never_stands_in_for_the_fixed_reply(
    db_session, rider, scripted_model, voice, message, word
):
    """Only the listed words were held and checked: after the gate refused
    the booking the rider read "Monday's booked.", and as the coach's only
    words it cut the fixed reply to the guaranteed lines (a crisis lost its
    question and means safety, chest pain "Don't ride" and "Don't drive
    yourself")."""
    done, silent = _against_silence(
        db_session, rider, scripted_model, message + BOOK_MONDAY,
        [([], _book), ([word], _nothing)], voice,
    )
    if not db_session.query(Workout).filter(Workout.user_id == rider.id).count():
        assert word not in done
    assert silent.strip() in done


@pytest.mark.parametrize("word", [
    "It's done.", "All sorted.", "Yep, done.", "Sure, done.", "Ok, booked.", "Brilliant, done.",
    "Consider it done.", "Monday's booked.", "Booked it.", "Booked for Monday morning.",
    "Booked for the 19th.", "Easy spin booked for Monday.", "Added to your plan.",
    "Moved to Thursday.",
])
def test_a_confirmation_in_any_words_after_a_refused_booking_is_dropped(word):
    out, guard = _refused_then(word)
    assert out == "" and word in guard.dropped


@pytest.mark.parametrize("word", [
    "It's done.", "All sorted.", "Yep, done.", "Monday's booked.", "Booked it.",
    "Easy spin booked for Monday.",
])
def test_a_confirmation_in_any_words_after_a_booking_reaches_the_rider(word):
    out, _ = _refused_then(word, [BOOKED])
    assert out == word


@pytest.mark.parametrize("word", ["Monday's booked.", "It's done.", "Easy spin booked for Monday."])
def test_a_held_account_never_hears_a_refused_booking_confirmed(
    db_session, rider, scripted_model, word
):
    ss.open_hold(db_session, rider, "hold_all", "Chest pain", "detector", red_flag="chest_pain")
    scripted_model.append(([], _book(scripted_model.tool_use)))
    scripted_model.append(([word], ()))

    _, said, _ = _send(db_session, rider, "Book me an easy ride on Monday.")

    assert word not in said
    assert db_session.query(Workout).count() == 0


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
@pytest.mark.parametrize("kind", sorted(LATE))
@pytest.mark.parametrize("last", [
    "Monday's booked.", "All sorted.", "Easy spin booked for Monday.",
])
def test_a_late_red_flag_keeps_its_fixed_reply_over_a_confirmation_in_any_words(
    db_session, rider, scripted_model, late_verdict, voice, kind, last
):
    hit, words = LATE[kind]
    late_verdict.adds = [hit]
    other = User(email="other@example.com", hashed_password="x", full_name="Alex Rider", ftp=250)
    db_session.add(other)
    db_session.commit()
    replies = []
    for user, last_words in ((rider, [last]), (other, [])):
        late_verdict.gate.clear()
        scripted_model.append(([], [_booking(scripted_model.tool_use, "endurance")]))
        scripted_model.append((last_words, ()))
        _, said, _ = _send(db_session, user, words + BOOK_MONDAY, voice)
        replies.append(said)

    done, silent = replies
    assert done == silent


HOLD_WORDS = [
    "Plan paused.", "Your plan is on hold.", "Training paused.", "Your training is paused.",
    "Hold's in place.", "Everything's on hold.",
]


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
@pytest.mark.parametrize("word", HOLD_WORDS)
def test_a_hold_in_other_words_waits_for_the_hold_tool(
    db_session, rider, scripted_model, voice, word
):
    """Only "I've put a hold on" and "your hold is on" waited for
    apply_safety_hold: "Plan paused." went out when the tool then refused
    the level, and no hold was on."""
    scripted_model.append(([word], [scripted_model.tool_use(
        "apply_safety_hold", {"level": "pause", "reason": "knee"},
    )]))
    scripted_model.append(([], ()))

    _, said, _ = _send(db_session, rider, "My knee aches a bit.", voice)

    assert word not in said
    assert ss.current_hold(db_session, rider.id) is None


@pytest.mark.parametrize("word", ["Plan paused.", "Your plan is on hold."])
def test_a_hold_in_other_words_before_a_failed_tool_never_cuts_a_crisis_reply(
    db_session, rider, scripted_model, word
):
    def failed_hold(tool):
        return [tool("apply_safety_hold", {"level": "pause", "reason": "crisis"})]

    done, silent = _against_silence(
        db_session, rider, scripted_model, CRISIS + BOOK_MONDAY,
        [([word], failed_hold), ([], _nothing)],
    )
    assert done == silent


@pytest.mark.parametrize("text", [
    "It's on hold. We'll see how your legs are.",
    "Paused. It picks up when you're back.",
])
def test_hold_words_in_an_ordinary_turn_still_go_out(db_session, rider, scripted_model, text):
    scripted_model.append(([text], ()))
    _, said, _ = _send(db_session, rider, "What happens to my club rides while I'm away?")
    assert said.strip() == text


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
@pytest.mark.parametrize("words", [
    "Done, you're all set. Enjoy the ride!", "Sorted, it's booked for Monday. Enjoy it.",
])
def test_a_confirmation_before_the_tool_waits_for_it_whatever_follows(
    db_session, rider, scripted_model, voice, words
):
    """The words after it released it before the tool ran: the gate then
    refused the booking, and the rider read that it was done."""
    _injured(db_session, rider)
    scripted_model.append(([words], [_booking(scripted_model.tool_use)]))
    scripted_model.append(([], ()))

    _, said, _ = _send(db_session, rider, "Book me a hard ride on Monday.", voice)

    lead, after = words.split(". ", 1)
    assert lead not in said and after in said
    assert db_session.query(Workout).count() == 0


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
def test_a_confirmation_before_a_refused_booking_never_cuts_a_chest_reply(
    db_session, rider, scripted_model, voice
):
    done, silent = _against_silence(
        db_session, rider, scripted_model, CHEST + BOOK_MONDAY,
        [(["Done, you're all set. Enjoy the ride!"], _book), ([], _nothing)], voice,
    )
    assert "all set" not in done
    assert silent.strip() in done


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
def test_a_booking_claimed_after_done_before_the_tool_never_stands_in_for_the_fixed_reply(
    db_session, rider, scripted_model, voice
):
    """As a plan claim it waited for the booking, then went out as the
    reply: the crisis lost its question and means safety."""
    done, silent = _against_silence(
        db_session, rider, scripted_model, CRISIS + BOOK_MONDAY,
        [(["Done, I've booked Monday for you."], _book), ([], _nothing)], voice,
    )
    assert done == silent


def _far(tool):
    return tool("add_workout", {
        "scheduled_date": (date.today() + timedelta(days=40)).isoformat(),
        "title": "Easy spin", "workout_type": "endurance",
    })


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
@pytest.mark.parametrize("case", ["errored", "cut", "tools withheld", "proposal"])
def test_what_done_says_is_checked_against_what_happened(
    db_session, rider, scripted_model, voice, case
):
    """What a "Done, ..." said after a change went out unchecked: over a
    booking that errored after it, one the cut left unrun, one the last
    round could no longer make, and a proposal that changed nothing."""
    tool = scripted_model.tool_use
    message = "Book Monday and Tuesday, both easy."
    if case == "errored":
        scripted_model.append(([], _book(tool)))
        scripted_model.append((["Done, Tuesday's in too."], [_far(tool)]))
        scripted_model.append(([], ()))
        claim = "Tuesday's in too."
    elif case == "cut":
        scripted_model.append(([], _book(tool)))
        scripted_model.append((["Done, both are booked."], [_tuesday(tool)], "max_tokens"))
        claim = "oth are booked"
    elif case == "tools withheld":
        scripted_model.append(([], [
            tool("find_ride", {"query": "yesterday"}), tool("find_ride", {"query": "Sunday"}),
            tool("find_ride", {"query": "Saturday"}), _booking(tool, "endurance"),
        ]))
        scripted_model.append((["Done, Tuesday's booked too."], [_tuesday(tool)]))
        claim = "Tuesday's booked too."
    else:
        workout = _planned(db_session, rider)
        scripted_model.append(([], [tool("propose_plan_change", {
            "observation": "Your legs are heavy", "rationale": "Fatigue is high",
            "changes": [{"action": "skip_workout", "workout_id": workout.id}],
        })]))
        scripted_model.append((["Done, your plan's updated."], ()))
        message = "Can you lighten this week?"
        claim = "plan's updated"

    _, said, _ = _send(db_session, rider, message, voice)

    assert claim not in said
    assert db_session.query(Workout).filter(Workout.status != WorkoutStatus.skipped).count() <= 1


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
def test_a_confirmation_in_front_of_a_claim_is_checked_against_its_round(
    db_session, rider, scripted_model, voice
):
    """after_tools let the pending claim out, and "Done." in front of it,
    before it took in the round's error."""
    tool = scripted_model.tool_use
    scripted_model.append((["Done. I've booked Monday's easy ride."], [*_book(tool), _far(tool)]))
    scripted_model.append((["Tuesday is too far out to book yet."], ()))

    _, said, _ = _send(
        db_session, rider, "Book me an easy ride Monday and another on Tuesday.", voice
    )

    assert "Done." not in said
    assert "I've booked Monday's easy ride." in said and "too far out" in said

    guard = _plain_guard()
    guard.feed("Done. I've booked Monday's easy ride. ")
    guard.before_tools()
    out = guard.after_tools([BOOKED, ("add_workout", "Error: nothing was booked.")])
    assert "Done." not in out + guard.flush()


@pytest.mark.parametrize("words", [
    "No. Stop riding and get seen today at A&E. That's in your best interest, so don't "
    "drive yourself.",
    "No. Stop riding and get seen today at A&E. There you go, and please don't drive yourself.",
])
def test_the_coachs_own_safety_words_are_never_taken_for_a_confirmation(
    db_session, rider, scripted_model, words
):
    scripted_model.append(([words], ()))
    _, said, _ = _send(db_session, rider, CHEST + " Should I ride tomorrow?")
    assert "don't drive yourself" in said


SATURDAY = ("add_workout", "Booked. 'Easy spin' (90 min) is on Saturday 24 October.")


@pytest.mark.parametrize("results, text, shown", [
    ([SATURDAY], "Great choice. You're booked in for Saturday.", None),
    ([SATURDAY], "Nice one. It's booked for Saturday 24 October.", None),
    ([SATURDAY], "Love it. Booked in for Saturday.", None),
    ([SATURDAY], "90 minutes in Zone 2, cadence around 90. You're booked in for Saturday.", None),
    ([SATURDAY], "Good call. Booked.", None),
    ([SATURDAY], "An easy 90 minutes on Saturday, Zone 2 throughout. Booked.", None),
    ([SKIPPED], "Good call. Skipped.", None),
    ([MOVED], "Good idea with the travel. Moved.", None),
    ([SATURDAY], "Booked in for Saturday. You're all set.", "Booked in for Saturday. "),
    ([BOOKED], "It's on Monday 19 October. Done.", "It's on Monday 19 October. "),
])
def test_a_true_confirmation_after_the_coachs_words_reaches_the_rider(results, text, shown):
    """Any words before it in its round dropped it ("Great choice." was
    enough), so the rider lost the only words saying the booking went in;
    said twice, the later one stayed and the one with the day went."""
    guard = _plain_guard()
    guard.before_tools()
    guard.after_tools(results)
    assert guard.feed(text) + guard.flush() == (text if shown is None else shown)


@pytest.mark.parametrize("message, text", [
    ("Is Saturday's ride still on?", "You're booked in for Saturday. Keep the effort easy."),
    ("Is Saturday's ride still on?", "Yes. You're booked in for Saturday."),
    ("Do I need to change anything for Saturday?",
     "Nothing to change. You're all set for Saturday."),
    ("Do I need to change anything for Saturday?", "Nope. You're all set for Saturday."),
    ("What's on this weekend?", "It's in. Saturday, three hours endurance."),
    ("When's my next ride?", "Scheduled. It's on Thursday."),
])
def test_a_recap_with_no_tool_behind_it_reaches_the_rider(
    db_session, rider, scripted_model, message, text
):
    scripted_model.append(([text], ()))
    _, said, stored = _send(db_session, rider, message)
    assert said.strip() == text
    assert not (stored.context_snapshot or {}).get("fallback")


@pytest.mark.parametrize("text", ["Done.", "You're all set.", "Booked for tomorrow."])
def test_a_confirmation_of_a_change_asked_for_with_no_tool_is_dropped(
    db_session, rider, scripted_model, text
):
    scripted_model.append(([text], ()))
    _, said, _ = _send(db_session, rider, "Skip tomorrow")
    assert text not in said


@pytest.mark.parametrize("text", [
    "Nothing changed, so you're booked in for Tuesday as before.",
    "That didn't go through. It's in your plan for Tuesday as it was. Want me to try again?",
])
def test_saying_nothing_changed_after_a_failed_tool_goes_out(text):
    out, _ = _refused_then(text, [("update_workout", "Error: Workout not found.")])
    assert out == text


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
def test_a_true_confirmation_after_a_retried_tool_reaches_the_rider(
    db_session, rider, scripted_model, voice
):
    workout = _planned(db_session, rider)
    day = (date.today() + timedelta(days=4)).isoformat()
    tool = scripted_model.tool_use
    for workout_id in ("nope", workout.id):
        scripted_model.append(([], [tool(
            "update_workout", {"workout_id": workout_id, "scheduled_date": day},
        )]))
    scripted_model.append((["Done."], ()))

    _, said, stored = _send(db_session, rider, "Move Tuesday's session to Thursday.", voice)

    assert said.strip() == "Done."
    assert not (stored.context_snapshot or {}).get("fallback")


NOTED = (
    "update_goal",
    "Updated the goal 'Mallorca 312' (notes). There is no current plan to rebuild.",
)


@pytest.mark.parametrize("word", ["Done.", "Updated."])
def test_a_goal_note_is_confirmed(word):
    out, _ = _refused_then(word, [NOTED + ({"goal_id": "g1", "notes": "with my brother"},)])
    assert out == word
    # A new date or priority may move the plan: nothing says it did.
    out, _ = _refused_then(word, [NOTED + ({"goal_id": "g1", "event_date": "2027-05-30"},)])
    assert out == ""
