"""The coach's tool loop always ends in an answer, and never goes hunting.

16 Oct 2026, turn 2 of Gareth's Sa Calobra chat: he asked about cadence in
the two GPX files he had shared on turn 1. The coach spent all five tool
rounds searching his ride history for them, wrote no words at all, and the
rider got "I got cut off before I could answer that properly. Send it again
and I'll get straight to the point." Sending it again could not have helped,
and the next turn read that line back as if it were an answer.

- The last round sends tool_choice {"type": "none"}, so it has to be words.
- find_ride and analyse_ride run at most twice per rider message, parallel
  calls counted; past that the coach is told to answer from what it has.
- A turn that still ends with no words (a max_tokens cut-off) promises
  nothing, is marked in its snapshot, and shows the next turn a plain note
  that no answer was given.
- One forma.coach_turn log line per turn says what each round did.
"""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from app.core import forma_core
from app.models.chat import ChatMessage, ChatRole, ChatSession
from app.models.user import User
from app.services import attachment_service, coach_service
from tests.test_chat_attachment_working_set import _gpx

NONE = {"type": "none"}
DASHES = (chr(0x2014), chr(0x2013))  # em and en dash


@pytest.fixture
def gareth(db_session):
    user = User(id="u-gareth", email="g@example.com", hashed_password="x",
                full_name="Gareth Winter", ftp=290)
    db_session.add(user)
    db_session.commit()
    return user


def _chat(db, user, sid="s-calobra") -> ChatSession:
    chat = ChatSession(id=sid, user_id=user.id, title="Chat")
    db.add(chat)
    db.commit()
    return chat


def _send(db, user, chat, text, voice=False, ids=None) -> list[dict]:
    attachment_service.bind_to_session(db, user.id, chat.id, ids or [])
    stream = coach_service.stream_voice_response if voice else coach_service.stream_response

    async def run():
        return [c async for c in stream(db, user, chat, text, attachment_ids=ids or [])]

    return [json.loads(c[len("data: "):]) for c in asyncio.run(run())]


def _said(chunks) -> str:
    return "".join(c["content"] for c in chunks if c["type"] == "text")


def _replies(db, chat) -> list[ChatMessage]:
    return (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == chat.id, ChatMessage.role == ChatRole.assistant)
        .order_by(ChatMessage.created_at, ChatMessage.id)
        .all()
    )


def _tool_results(call) -> list[str]:
    return [r["content"] for r in call["messages"][-1]["content"]]


@pytest.fixture
def ran(monkeypatch):
    """The tools that actually ran, in order."""
    names = []
    real = coach_service._execute_tool

    def spy(db, user, name, tool_input):
        names.append(name)
        return real(db, user, name, tool_input)

    monkeypatch.setattr(coach_service, "_execute_tool", spy)
    return names


# ── The forced final round ──────────────────────────────────────────────────


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
def test_five_rounds_of_searching_end_in_words_on_the_last(db_session, gareth, scripted_model, ran, voice):
    chat = _chat(db_session, gareth)
    search = {"query": "Sa Calobra"}
    for _ in range(4):
        scripted_model.append(([], [scripted_model.tool_use("find_ride", search)]))
    # The model would search a fifth time, but the last round cannot.
    answer = "Your cadence held at 88 rpm on the 29:53 and 91 on the 30:47."
    scripted_model.append(([answer], [scripted_model.tool_use("find_ride", search)]))

    chunks = _send(db_session, gareth, chat, "Now analyse my cadence.", voice=voice)

    calls = scripted_model.calls
    assert [c.get("tool_choice") for c in calls] == [None, None, None, None, NONE]
    assert all(c["tools"] is coach_service.COACH_TOOLS for c in calls)
    # The last round is told why the tools have gone; the round before is not.
    note = {"type": "text", "text": coach_service._LAST_ROUND_NOTE}
    assert calls[4]["messages"][-1]["content"][-1] == note
    assert note not in calls[3]["messages"][-1]["content"]
    assert [b["type"] for b in calls[4]["messages"][-1]["content"]] == ["tool_result", "text"]
    assert {c["task"] for c in calls} == {"chat_voice" if voice else "chat"}
    assert _said(chunks).strip() == answer
    (stored,) = _replies(db_session, chat)
    assert stored.content.strip() == answer
    assert "fallback" not in (stored.context_snapshot or {})
    # Two searches ran; the third and fourth were refused.
    assert ran == ["find_ride", "find_ride"]


def test_a_turn_that_answers_early_is_never_forced(db_session, gareth, scripted_model):
    chat = _chat(db_session, gareth)
    scripted_model.append((["Both files are here: 88 and 91 rpm."], ()))

    _send(db_session, gareth, chat, "Cadence?")

    assert [c.get("tool_choice") for c in scripted_model.calls] == [None]


# ── The ride lookup cap ─────────────────────────────────────────────────────


def test_the_third_ride_lookup_is_refused_parallel_calls_counted(db_session, gareth, scripted_model, ran):
    chat = _chat(db_session, gareth)
    tool = scripted_model.tool_use
    scripted_model.append(([], [
        tool("find_ride", {"query": "Sa Calobra"}),
        tool("analyse_ride", {"ride_id": "r-1"}),
        tool("find_ride", {"date_from": "2026-10-01"}),
    ]))
    scripted_model.append(([], [tool("analyse_ride", {"ride_id": "r-2"})]))
    scripted_model.append((["The two files are all I have to go on."], ()))

    chunks = _send(db_session, gareth, chat, "What about gear changes?")

    assert ran == ["find_ride", "analyse_ride"]
    first, second = _tool_results(scripted_model.calls[1]), _tool_results(scripted_model.calls[2])
    assert first[2] == coach_service._LOOKUP_LIMIT_REACHED
    assert first[0] != coach_service._LOOKUP_LIMIT_REACHED
    assert second == [coach_service._LOOKUP_LIMIT_REACHED]
    # The rider is never told a search is running when it is not.
    assert len([c for c in chunks if c["type"] == "status"]) == 2
    assert "say plainly what it is" in coach_service._LOOKUP_LIMIT_REACHED


def test_the_cap_is_per_rider_message(db_session, gareth, scripted_model, ran):
    chat = _chat(db_session, gareth)
    for _ in range(2):
        scripted_model.append(([], [scripted_model.tool_use("find_ride", {"query": "a"})]))
        scripted_model.append(([], [scripted_model.tool_use("find_ride", {"query": "b"})]))
        scripted_model.append((["Nothing saved under that name."], ()))
        _send(db_session, gareth, chat, "Find my Ditchling ride.")

    assert ran == ["find_ride"] * 4


def test_other_tools_still_run_after_the_cap(db_session, gareth, scripted_model, ran):
    chat = _chat(db_session, gareth)
    tool = scripted_model.tool_use
    scripted_model.append(([], [
        tool("find_ride", {"query": "a"}),
        tool("find_ride", {"query": "b"}),
        tool("find_ride", {"query": "c"}),
        tool("create_goal", {
            "event_name": "Sa Calobra sub 29", "event_date": "2027-04-01",
            "event_type": "hill_climb", "priority": "a_race",
        }),
    ]))
    scripted_model.append((["Filed."], ()))

    _send(db_session, gareth, chat, "Set me a goal.")

    assert ran == ["find_ride", "find_ride", "create_goal"]
    results = _tool_results(scripted_model.calls[1])
    assert results[2] == coach_service._LOOKUP_LIMIT_REACHED
    assert not results[3].startswith("Error") and "Sa Calobra sub 29" in results[3]


# ── The fallback ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
def test_a_cut_off_turn_promises_nothing_and_shows_next_turn_as_no_answer(db_session, gareth, scripted_model, voice):
    chat = _chat(db_session, gareth)
    # The whole output allowance went on thinking: no words, no tool call.
    scripted_model.append(([], [scripted_model.thinking()], "max_tokens"))

    chunks = _send(db_session, gareth, chat, "Now analyse my cadence.", voice=voice)

    said = _said(chunks)
    assert said == coach_service._NO_REPLY_FALLBACK
    for broken_promise in ("send it again", "straight to the point", "cut off"):
        assert broken_promise not in said.lower()
    (stored,) = _replies(db_session, chat)
    assert stored.content == coach_service._NO_REPLY_FALLBACK
    assert stored.context_snapshot["fallback"] is True

    scripted_model.append((["The 30:47 averaged 91 rpm."], ()))
    _send(db_session, gareth, chat, "And the gear changes?", voice=voice)

    history = scripted_model.calls[-1]["messages"]
    assert [m["role"] for m in history] == ["user", "assistant", "user"]
    assert history[1]["content"] == coach_service._NO_REPLY_NOTE
    assert coach_service._NO_REPLY_FALLBACK not in json.dumps(history, default=str)
    (answer,) = [r for r in _replies(db_session, chat) if r.content.strip() == "The 30:47 averaged 91 rpm."]
    assert "fallback" not in (answer.context_snapshot or {})


def test_the_old_fallback_in_an_older_chat_shows_as_no_answer_too(db_session, gareth, scripted_model):
    chat = _chat(db_session, gareth)
    coach_service.add_user_message(db_session, chat, "Now analyse my cadence.")
    coach_service.add_assistant_message(db_session, chat, coach_service._OLD_NO_REPLY_FALLBACK)
    scripted_model.append((["Here it is."], ()))

    _send(db_session, gareth, chat, "And the gear changes?")

    history = scripted_model.calls[-1]["messages"]
    assert history[1]["content"] == coach_service._NO_REPLY_NOTE
    assert "straight to the point" not in json.dumps(history, default=str)


def test_an_answer_is_never_mistaken_for_the_fallback(db_session, gareth, scripted_model):
    chat = _chat(db_session, gareth)
    coach_service.add_user_message(db_session, chat, "Cadence?")
    coach_service.add_assistant_message(db_session, chat, "88 rpm.", {"fallback": False})
    scripted_model.append((["Yes."], ()))

    _send(db_session, gareth, chat, "And again?")

    assert scripted_model.calls[-1]["messages"][1]["content"] == "88 rpm."


# ── The coach_turn log line ─────────────────────────────────────────────────


def _turn_lines(caplog) -> list[dict]:
    return [
        json.loads(r.getMessage()[len("coach_turn "):])
        for r in caplog.records
        if r.name == "forma.coach_turn"
    ]


def test_one_coach_turn_line_says_what_each_round_did(db_session, gareth, scripted_model, caplog):
    caplog.set_level(logging.INFO, logger="forma.coach_turn")
    chat = _chat(db_session, gareth)
    shared = attachment_service.ingest(
        db_session, gareth, _gpx("2026-10-12T09:00:00", 120, 300, 88), "Sa_Calobra_29_53.gpx"
    )
    tool = scripted_model.tool_use
    scripted_model.append(([], [scripted_model.thinking(), tool("find_ride", {"query": "saddle sore"})]))
    scripted_model.append(([], [
        tool("find_ride", {"query": "b"}), tool("analyse_ride", {"ride_id": "r"}),
    ]))
    scripted_model.append((["Here it is."], [scripted_model.thinking()]))

    _send(db_session, gareth, chat, "My secret saddle sore question", ids=[shared.id])

    (line,) = _turn_lines(caplog)
    assert line["session"] == chat.id
    assert line["surface"] == "chat"
    assert line["rounds"] == 3
    assert line["per_round"] == [
        {"stop": "tool_use", "blocks": ["thinking", "tool_use"], "tools": ["find_ride"]},
        {"stop": "tool_use", "blocks": ["tool_use", "tool_use"], "tools": ["find_ride", "analyse_ride"]},
        {"stop": "end_turn", "blocks": ["thinking", "text"], "tools": []},
    ]
    assert line["files"] == 1
    assert line["lookups_refused"] == 1
    assert line["ended"] == "reply" and line["fallback"] is False
    assert 0 <= line["prep_ms"] <= line["first_byte_ms"] <= line["first_text_ms"] <= line["total_ms"]
    # Counts, names and timings only: none of the rider's words or inputs.
    raw = next(r.getMessage() for r in caplog.records if r.name == "forma.coach_turn")
    for private in ("secret", "saddle", "Sa_Calobra", "Here it is", gareth.id, gareth.email):
        assert private not in raw
    assert all(r.levelno == logging.INFO for r in caplog.records if r.name == "forma.coach_turn")


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
def test_the_coach_turn_line_marks_a_fallback(db_session, gareth, scripted_model, caplog, voice):
    caplog.set_level(logging.INFO, logger="forma.coach_turn")
    chat = _chat(db_session, gareth)
    scripted_model.append(([], [scripted_model.thinking()], "max_tokens"))

    _send(db_session, gareth, chat, "Cadence?", voice=voice)

    (line,) = _turn_lines(caplog)
    assert line["surface"] == ("voice" if voice else "chat")
    assert line["ended"] == "fallback" and line["fallback"] is True
    assert line["per_round"] == [{"stop": "max_tokens", "blocks": ["thinking"], "tools": []}]
    assert line["files"] == 0
    assert line["first_text_ms"] is None


def test_the_coach_turn_line_survives_a_provider_failure(db_session, gareth, monkeypatch, caplog):
    from contextlib import contextmanager

    caplog.set_level(logging.INFO, logger="forma.coach_turn")
    chat = _chat(db_session, gareth)

    @contextmanager
    def down(**kwargs):
        raise RuntimeError("provider down")
        yield

    monkeypatch.setattr(forma_core, "stream", down)
    monkeypatch.setattr(forma_core, "call", lambda **k: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(coach_service, "maybe_autotitle_session", lambda *a, **k: None)
    monkeypatch.setattr(coach_service, "_relevant_memories", lambda *a, **k: None)
    monkeypatch.setattr(coach_service, "_dossier_block", lambda *a, **k: "")
    monkeypatch.setattr("app.services.memory_service.extract_memories", lambda *a, **k: None)

    chunks = _send(db_session, gareth, chat, "Cadence?")

    (line,) = _turn_lines(caplog)
    assert line["ended"] == "error" and line["fallback"] is False and line["rounds"] == 0
    assert "didn't reach me" in _said(chunks)


# ── The funnel and the words ────────────────────────────────────────────────


def test_forma_core_passes_tool_choice_through_only_when_given(monkeypatch):
    sent = []

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_final_message(self):
            return SimpleNamespace(usage=None)

    class _Messages:
        def stream(self, **kwargs):
            sent.append(kwargs)
            return _Stream()

    monkeypatch.setattr(forma_core, "_client", lambda: SimpleNamespace(messages=_Messages()))
    monkeypatch.setattr(forma_core, "_log", lambda *a, **k: None)
    for choice in (NONE, None):
        with forma_core.stream(
            user_id="u", task="chat", system="s", messages=[], tools=[{"name": "t"}],
            tool_choice=choice, enforce_budget=False,
        ):
            pass

    assert sent[0]["tool_choice"] == NONE
    assert "tool_choice" not in sent[1]


def _tool(name) -> dict:
    return next(t for t in coach_service.COACH_TOOLS if t["name"] == name)


def test_the_ride_tools_point_away_from_shared_files_and_name_no_ones_rides():
    find, analyse = _tool("find_ride"), _tool("analyse_ride")
    everything = json.dumps([find, analyse])
    for gareths_own in ("Sprint Training", "Escorca", "Sa Calobra"):
        assert gareths_own not in everything
    assert "saved rides only" in find["description"]
    assert "never use this to look for one" in find["description"]
    assert "WHENEVER" not in analyse["description"]
    assert "cannot open a file shared in this chat" in analyse["description"]
    for d in (find["description"], analyse["description"]):
        assert "at most twice per rider message" in d


def test_the_new_words_carry_no_dashes():
    for text in (
        _tool("find_ride")["description"],
        _tool("analyse_ride")["description"],
        coach_service._LOOKUP_LIMIT_REACHED,
        coach_service._NO_REPLY_FALLBACK,
        coach_service._NO_REPLY_NOTE,
        coach_service._LAST_ROUND_NOTE,
    ):
        assert not any(d in text for d in DASHES)
