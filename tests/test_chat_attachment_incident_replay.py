"""Gareth's Sa Calobra chat of 16 Oct 2026, replayed end to end.

Through the real HTTP routes (upload, create chat, send, voice send, delete),
with a stand-in model that hunts ride history every round it is allowed to,
as the incident's model did, and answers only from what it is actually sent.
So this proves the data path and the loop, not how the real model behaves.

Turn 1 attached two GPX files (29:53 and 30:47). Turn 2 asked about cadence
and got "I got cut off" after five rounds of hunting. Turn 3 asked about gear
changes and could only find the 29:53 in saved history. Turn 4 attached both
again. Each scenario below is one a review replay found a gap in.
"""

import asyncio
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core import forma_core
from app.models.chat import ChatMessage, ChatRole, ChatSession
from app.models.chat_attachment import ChatAttachment
from app.models.ride import Ride, RideSource
from app.models.user import User
from app.services import attachment_service, coach_service

PB = "Sa_Calobra_29_53_sub_30_mission_complete_Failure_is_the_path_to_success_.gpx"
NEAR = "Sa_Calobra_30_47_Can_I_find_47_seconds_.gpx"
FILES_HEADING = "## Files shared in this chat"
CONTEXT_HEADING = "## Current Rider Context"
SEARCHING = "Searching your rides"

T1 = "Compare these two Sa Calobra efforts. Where did the 54 seconds go?"
T2 = "What about cadence? Was I spinning less on the slower one?"
T3 = "And gear changes? Did I shift more on the 30:47?"
T4 = "Here they are again. Cadence and gears please."


def _climb(day: str, seconds: int, watts: int, rpm: int, descent: int = 0) -> bytes:
    """A steady climb from the sea, 0.25 m a second at about 7%, ending at
    the top unless `descent` seconds of downhill follow it."""
    t0 = datetime.fromisoformat(f"{day}T08:00:00")
    points = []
    for i in range(seconds + descent):
        ele = 5 + 0.25 * min(i, seconds) - 0.5 * max(i - seconds, 0)
        ts = (t0 + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        points.append(
            f'<trkpt lat="{39.80 + i * 0.00003:.6f}" lon="2.80"><ele>{ele:.2f}</ele>'
            f"<time>{ts}</time><extensions><power>{watts}</power>"
            f"<gpxtpx:TrackPointExtension><gpxtpx:cad>{rpm}</gpxtpx:cad>"
            f"</gpxtpx:TrackPointExtension></extensions></trkpt>"
        )
    return (
        '<?xml version="1.0"?><gpx version="1.1" '
        'xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">'
        "<trk><trkseg>" + "".join(points) + "</trkseg></trk></gpx>"
    ).encode()


PB_BYTES = _climb("2026-05-01", 400, 330, 84)
NEAR_BYTES = _climb("2026-05-03", 410, 316, 79)


def _files(system: list) -> dict | None:
    found = [b["text"] for b in system if b["text"].startswith(FILES_HEADING)]
    assert len(found) <= 1
    if not found:
        return None
    body = found[0].split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    return json.loads(body)


def _files_text(system: list) -> str | None:
    found = [b["text"] for b in system if b["text"].startswith(FILES_HEADING)]
    return found[0] if found else None


def _describe(files: dict | None) -> str:
    if not files:
        return "I cannot see any shared file."
    return "; ".join(
        f'{f["file"]} averaged {(f.get("cadence") or {}).get("avg_rpm")} rpm'
        for f in files["files"]
    )


# ── A model that hunts like the incident's ─────────────────────────────────


class _Hunter:
    """On the follow-up turns it calls ride lookups every round it may, with
    thinking and no words. Sent tool_choice none, it has to answer, and it
    answers only from the files block it can see."""

    def __init__(self):
        self.calls: list[dict] = []
        self.turn: str | None = None
        self.saved_ride_id: str | None = None
        self.plans: dict = {}
        self._n = 0

    def tool(self, name: str, tool_input: dict):
        self._n += 1
        return SimpleNamespace(type="tool_use", id=f"toolu_{self._n:04d}", name=name, input=tool_input)

    def rounds(self) -> int:
        return sum(1 for c in self.calls if c["turn"] == self.turn)

    def plan(self, kw) -> tuple[list[str], list]:
        files = _files(kw["system"])
        forced = kw.get("tool_choice") == {"type": "none"}
        r = self.rounds()
        if self.turn in self.plans:
            return self.plans[self.turn](self, kw, r, forced)
        if self.turn == "t2":
            if forced:
                return [f"Cadence: {_describe(files)}."], []
            hunts = [
                [self.tool("find_ride", {"query": "Sa Calobra"})],
                [self.tool("find_ride", {"date_from": "2026-05-01", "date_to": "2026-05-31"}),
                 self.tool("analyse_ride", {"ride_id": self.saved_ride_id})],
                [self.tool("find_ride", {"query": "30:47"})],
                [self.tool("find_ride", {"query": "Can I find 47 seconds"})],
            ]
            return [], hunts[min(r, 3)]
        if self.turn == "t3":
            if forced:
                gears = (
                    "Neither file holds gear data."
                    if files and "no gear data" in _files_text(kw["system"])
                    else "I do not know about gears."
                )
                return [f"{gears} {_describe(files)}."], []
            hunts = [
                [self.tool("find_ride", {"query": "Sa Calobra"})],
                [self.tool("analyse_ride", {"ride_id": self.saved_ride_id})],
                [self.tool("find_ride", {"query": "30 47"})],
                [self.tool("analyse_ride", {"ride_id": self.saved_ride_id})],
            ]
            return [], hunts[min(r, 3)]
        return [f"Read them. {_describe(files)}."], []

    @contextmanager
    def stream(self, **kw):
        texts, tools = self.plan(kw)
        if kw.get("tool_choice") == {"type": "none"}:
            assert not tools, "a forced round can never call a tool"
        self.calls.append({**kw, "messages": list(kw["messages"]), "turn": self.turn})
        thinking = [SimpleNamespace(type="thinking", thinking="")]
        content = thinking + ([SimpleNamespace(type="text", text="".join(texts))] if texts else []) + tools
        final = SimpleNamespace(
            content=content,
            stop_reason="tool_use" if tools else "end_turn",
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )

        class _Stream:
            def __iter__(self_inner):
                yield SimpleNamespace(type="message_start")
                yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(thinking=""))
                for t in texts:
                    yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(text=t))

            def get_final_message(self_inner):
                return final

        yield _Stream()

    def for_turn(self, turn: str) -> list[dict]:
        return [c for c in self.calls if c["turn"] == turn]


# ── The world: one rider, the PB already in saved history, a clock ─────────


@pytest.fixture
def threaded_db():
    """The routes run in TestClient's worker thread, so the in-memory
    database must be shareable across threads."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.models.base import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


@pytest.fixture
def no_real_database(monkeypatch):
    """Nothing here may open the app's own database session."""
    import sys

    import app.main  # noqa: F401  (loads every module that holds SessionLocal)

    asked = []

    def refuse(*args, **kwargs):
        asked.append(1)
        raise RuntimeError("the replay asked for the app's real database")

    for name, module in list(sys.modules.items()):
        if name.startswith("app.") and getattr(module, "SessionLocal", None) is not None:
            monkeypatch.setattr(module, "SessionLocal", refuse)
    yield
    assert not asked


class _World:
    pass


@pytest.fixture
def world(threaded_db, no_real_database, monkeypatch, caplog):
    from fastapi.testclient import TestClient

    from app.api.v1 import chat as chat_api
    from app.api.v1.deps import get_current_user, require_paid_access
    from app.database import get_db
    from app.main import app

    db = threaded_db
    w = _World()
    w.db = db
    w.now = datetime(2026, 10, 16, 4, 11)
    w.gareth = User(id="u-gareth", email="g@example.com", hashed_password="x",
                    full_name="Gareth Winter", ftp=290, weight_kg=68)
    db.add(w.gareth)
    db.commit()
    # The 29:53 is in saved history already, under a title the system wrote.
    w.saved = Ride(user_id=w.gareth.id, source=RideSource.strava, external_id="strava-1",
                   title="Sprint Training", location_name="Escorca, Spain",
                   ride_date=datetime(2026, 5, 1, 8, 0), duration_seconds=1793,
                   distance_meters=9400, elevation_gain_meters=670, average_power=330)
    db.add(w.saved)
    db.commit()

    w.model = _Hunter()
    w.model.saved_ride_id = w.saved.id
    monkeypatch.setattr(forma_core, "stream", w.model.stream)
    monkeypatch.setattr(forma_core, "call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(coach_service, "maybe_autotitle_session", lambda *a, **k: None)
    monkeypatch.setattr(coach_service, "_relevant_memories", lambda *a, **k: None)
    monkeypatch.setattr(coach_service, "_dossier_block", lambda *a, **k: "")
    w.memory = []
    monkeypatch.setattr(
        "app.services.memory_service.extract_memories",
        lambda db_, user, text, **kw: w.memory.append(kw),
    )

    # SQLite stamps whole seconds and the turns were minutes apart, so each
    # row is stamped with its turn's own minute.
    real_user, real_reply = coach_service.add_user_message, coach_service.add_assistant_message
    real_ingest = attachment_service.ingest

    def add_user(db_, session, content, attachment_ids=None):
        m = real_user(db_, session, content, attachment_ids)
        m.created_at = w.now
        db_.commit()
        return m

    def add_reply(db_, session, content, context_snapshot=None, tokens_used=None):
        m = real_reply(db_, session, content, context_snapshot, tokens_used)
        m.created_at = w.now + timedelta(seconds=40)
        db_.commit()
        return m

    def ingest(db_, user, data, filename, session_id=None):
        a = real_ingest(db_, user, data, filename, session_id)
        a.created_at = w.now - timedelta(seconds=20)
        db_.commit()
        return a

    monkeypatch.setattr(coach_service, "add_user_message", add_user)
    monkeypatch.setattr(coach_service, "add_assistant_message", add_reply)
    monkeypatch.setattr(attachment_service, "ingest", ingest)

    w.ran = []
    real_exec = coach_service._execute_tool

    def spy(db_, user, name, tool_input):
        w.ran.append((w.model.turn, name))
        return real_exec(db_, user, name, tool_input)

    monkeypatch.setattr(coach_service, "_execute_tool", spy)

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_paid_access] = lambda: w.gareth
    app.dependency_overrides[get_current_user] = lambda: w.gareth
    app.dependency_overrides[chat_api._attachment_limit] = lambda: None
    # Never as a context manager: no lifespan, so no background loops.
    w.client = TestClient(app)
    caplog.set_level(logging.INFO, logger="forma.coach_turn")
    w.caplog = caplog

    def at(hh, mm):
        w.now = datetime(2026, 10, 16, hh, mm)

    def new_chat():
        r = w.client.post("/api/v1/chat/sessions", json={})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def upload(name, data, session_id=None):
        r = w.client.post(
            "/api/v1/chat/attachments",
            files={"file": (name, data, "application/gpx+xml")},
            data={"session_id": session_id} if session_id else {},
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def send(sid, text, ids=(), voice=False, turn=None):
        w.model.turn = turn
        path = "voice-message" if voice else "messages"
        r = w.client.post(f"/api/v1/chat/sessions/{sid}/{path}",
                          json={"content": text, "attachment_ids": list(ids)})
        assert r.status_code == 200, r.text
        return [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]

    def turn_lines():
        return [json.loads(rec.getMessage().split(" ", 1)[1])
                for rec in caplog.records if rec.name == "forma.coach_turn"]

    def replies(sid):
        return (db.query(ChatMessage)
                .filter(ChatMessage.session_id == sid, ChatMessage.role == ChatRole.assistant)
                .order_by(ChatMessage.created_at, ChatMessage.id).all())

    w.at, w.new_chat, w.upload, w.send = at, new_chat, upload, send
    w.turn_lines, w.replies = turn_lines, replies
    yield w
    app.dependency_overrides.clear()


def _users_in(call) -> list[str]:
    return [m["content"] for m in call["messages"] if m["role"] == ChatRole.user and isinstance(m["content"], str)]


# ── The four turns ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
def test_the_incident_replayed(world, voice):
    w, m = world, world.model

    # Turn 1: the page uploads before the chat exists, then creates it.
    w.at(4, 11)
    first = (w.upload(PB, PB_BYTES), w.upload(NEAR, NEAR_BYTES))
    sid = w.new_chat()
    out = {"t1": w.send(sid, T1, first, voice=voice, turn="t1")}
    w.at(4, 16)
    out["t2"] = w.send(sid, T2, voice=voice, turn="t2")
    w.at(4, 20)
    out["t3"] = w.send(sid, T3, voice=voice, turn="t3")
    # Turn 4: attached again inside the open chat, as the page now does.
    w.at(4, 24)
    again = (w.upload(PB, PB_BYTES, sid), w.upload(NEAR, NEAR_BYTES, sid))
    out["t4"] = w.send(sid, T4, again, voice=voice, turn="t4")
    w.at(4, 30)
    out["t5"] = w.send(sid, "One more on cadence.", voice=voice, turn="t5")

    # Turns 2 and 3 see both files on every round, without a re-attach.
    for turn in ("t2", "t3"):
        for call in m.for_turn(turn):
            files = _files(call["system"])
            assert files is not None, f"{turn}: no files block"
            assert sorted(f["attachment_id"] for f in files["files"]) == sorted(first)
            assert sorted(f["cadence"]["avg_rpm"] for f in files["files"]) == [79, 84]
        context = next(b["text"] for b in m.for_turn(turn)[0]["system"] if CONTEXT_HEADING in b["text"])
        assert first[0] not in context and first[1] not in context

    # One set of bytes on every call of turns 1 to 3, in its own cached block
    # between the education and the rider context.
    assert len({_files_text(c["system"]) for t in ("t1", "t2", "t3") for c in m.for_turn(t)}) == 1
    system = m.for_turn("t2")[0]["system"]
    assert system[1]["text"].startswith(FILES_HEADING) and CONTEXT_HEADING in system[2]["text"]
    assert all(system[i].get("cache_control") == {"type": "ephemeral"} for i in (0, 1, 2))

    # The hunt: two lookups ran, the third was refused, and the round after
    # the refusal answered. Turn 2 took three model calls, not five.
    assert [len(m.for_turn(t)) for t in ("t1", "t2", "t3", "t4", "t5")] == [1, 3, 4, 1, 1]
    for turn in ("t2", "t3"):
        lookups = [n for t, n in w.ran if t == turn and n in ("find_ride", "analyse_ride")]
        assert len(lookups) == 2, (turn, lookups)
        calls = m.for_turn(turn)
        assert calls[-1]["tool_choice"] == {"type": "none"}
        assert all(c.get("tool_choice") is None for c in calls[:-1])
        assert calls[-1]["messages"][-1]["content"][-1] == {
            "type": "text", "text": coach_service._LAST_ROUND_NOTE,
        }
        assert len([e for e in out[turn] if e.get("content") == SEARCHING]) <= 2

    # Every reply is words, never the fallback; turn 2 quotes both cadences,
    # turn 3 says the files hold no gear data.
    replies = w.replies(sid)
    assert len(replies) == 5
    for r in replies:
        assert not (r.context_snapshot or {}).get("fallback"), r.content
    assert "84 rpm" in replies[1].content and "79 rpm" in replies[1].content
    assert replies[2].content.startswith("Neither file holds gear data.")
    assert "Sa_Calobra_29_53" in replies[2].content and "Sa_Calobra_30_47" in replies[2].content

    # Turn 4's copies show once, as the newest; four rows exist.
    for turn in ("t4", "t5"):
        files = _files(m.for_turn(turn)[0]["system"])
        assert sorted(f["attachment_id"] for f in files["files"]) == sorted(again)
    assert w.db.query(ChatAttachment).count() == 4

    # History: only turns 1 and 4 open with the note of what they carried.
    users = _users_in(m.for_turn("t5")[0])
    note = "[Files shared with this message, by file name: "
    assert [u.startswith(note) for u in users] == [True, False, False, True, False]
    assert f"“{PB}”, “{NEAR}”]" in users[0]

    # The coach_turn lines tell the turns apart.
    lines = w.turn_lines()
    assert [ln["carried"] for ln in lines] == [2, 0, 0, 2, 0]
    assert [ln["rounds"] for ln in lines] == [1, 3, 4, 1, 1]
    assert [ln["lookups_refused"] for ln in lines] == [0, 1, 1, 0, 0]
    assert all(ln["files"] == 2 and ln["files_open"] == 2 for ln in lines)
    assert all(ln["ended"] == "reply" for ln in lines)
    assert {ln["surface"] for ln in lines} == {"voice" if voice else "chat"}
    t2_results = [r for rnd in lines[1]["per_round"] for r in rnd["results"]]
    # As on the day: "Sa Calobra" matched nothing (the PB is saved as
    # "Sprint Training" in "Escorca"), the date search found it, and the
    # third lookup was refused.
    assert [(r["tool"], r["outcome"], r.get("matches")) for r in t2_results] == [
        ("find_ride", "empty", 0), ("find_ride", "ok", 1), ("analyse_ride", "refused", None),
    ]

    # Nobody has said whose rides these are, so every memory pass is warned.
    assert len(w.memory) == 5 and all(mm.get("caution") for mm in w.memory)


def test_turn_four_under_browser_renamed_names_shows_each_ride_once(world):
    """Downloaded again, the browser saves "name (1).gpx". Same rides, so
    the coach sees two files, not four."""
    w = world
    first = (w.upload(PB, PB_BYTES), w.upload(NEAR, NEAR_BYTES))
    sid = w.new_chat()
    w.send(sid, T1, first, turn="t1")
    w.at(4, 24)
    again = (w.upload(PB.replace(".gpx", " (1).gpx"), PB_BYTES, sid),
             w.upload(NEAR.replace(".gpx", " (1).gpx"), NEAR_BYTES, sid))
    w.send(sid, T4, again, turn="t4")

    files = _files(w.model.for_turn("t4")[0]["system"])
    assert sorted(f["attachment_id"] for f in files["files"]) == sorted(again)
    assert "older_files_named_only" not in files


def test_what_the_rider_said_about_a_file_survives_turn_fours_re_upload(world):
    w, m = world, world.model
    first = (w.upload(PB, PB_BYTES), w.upload(NEAR, NEAR_BYTES))
    sid = w.new_chat()

    def records_owners(model, kw, r, forced):
        if r == 0:
            return [], [
                model.tool("set_attachment_owner", {"attachment_id": first[0], "owner": "self"}),
                model.tool("set_attachment_owner", {"attachment_id": first[1], "owner": "someone_else"}),
            ]
        return ["Noted: the 29:53 is yours, the 30:47 your mate's."], []

    m.plans["owners"] = records_owners
    w.send(sid, "The 29:53 is mine. The 30:47 is my mate Tom's.", first, turn="owners")
    w.at(4, 24)
    again = (w.upload(PB, PB_BYTES, sid), w.upload(NEAR, NEAR_BYTES, sid))
    w.send(sid, T4, again, turn="t4")

    shown = {f["attachment_id"]: f for f in _files(m.for_turn("t4")[0]["system"])["files"]}
    assert (shown[again[0]]["owner"], shown[again[1]]["owner"]) == ("self", "someone_else")
    # Tom's numbers are measured against Gareth's FTP, and the coach is told.
    assert "measure their power against the rider's FTP, not their own" in (
        shown[again[1]]["ftp_used"]["source"]
    )
    assert w.memory[-1]["caution"].count("“") == 1  # only Tom's file
    said = coach_service._execute_tool(
        w.db, w.gareth, "save_attachment_as_ride", {"attachment_id": again[1], "confirmed": True}
    )
    assert said.startswith("Error: nothing was saved. This file is recorded as someone else's ride")


# ── A model that saves a file so it can open it ────────────────────────────


def test_a_hunting_model_cannot_save_a_file_nobody_said_was_the_riders(world):
    """The replay's worst case: with no gear data in the file, the model
    saved the 30:47 into history to open it, unasked. Now it is refused."""
    w, m = world, world.model
    first = (w.upload(PB, PB_BYTES), w.upload(NEAR, NEAR_BYTES))
    sid = w.new_chat()
    w.send(sid, T1, first, turn="t1")

    def saves_to_open(model, kw, r, forced):
        if r == 0:
            return [], [model.tool("save_attachment_as_ride", {"attachment_id": first[1], "confirmed": True})]
        return ["Neither file holds gear data."], []

    m.plans["t3save"] = saves_to_open
    w.at(4, 20)
    events = w.send(sid, T3, turn="t3save")

    result = m.for_turn("t3save")[1]["messages"][-1]["content"][0]["content"]
    assert result.startswith("Error: nothing was saved. Whose ride this file is has not been recorded")
    assert w.db.query(Ride).count() == 1
    assert not [e for e in events if e["type"] == "plan_updated"]
    w.db.expire_all()
    assert attachment_service.file_owner(w.db.get(ChatAttachment, first[1])) == "unknown"
    assert w.memory[-1]["caution"]


# ── What the logs can tell apart ────────────────────────────────────────────


def test_a_rider_who_leaves_mid_turn_still_leaves_a_coach_turn_line(world):
    w = world
    first = (w.upload(PB, PB_BYTES), w.upload(NEAR, NEAR_BYTES))
    sid = w.new_chat()
    w.send(sid, T1, first, turn="t1")
    w.at(4, 16)
    w.model.turn = "t2"
    session = w.db.get(ChatSession, sid)

    async def hang_up_at_the_first_status():
        chunks = coach_service.stream_response(w.db, w.gareth, session, T2)
        async for chunk in chunks:
            if '"status"' in chunk:  # "Searching your rides", and the rider leaves
                break
        await chunks.aclose()

    asyncio.run(hang_up_at_the_first_status())

    lines = w.turn_lines()
    assert len(lines) == 2
    assert lines[1]["ended"] == "disconnected" and lines[1]["rounds"] == 1
    assert lines[1]["per_round"][0]["tools"] == ["find_ride"]


# ── Gareth's own chat, as the old code stored it ───────────────────────────


def test_gareths_stored_chat_after_the_backfill(world):
    from scripts import backfill_attachment_sessions as backfill

    w, db = world, world.db
    first = (w.upload(PB, PB_BYTES), w.upload(NEAR, NEAR_BYTES))
    w.at(4, 24)
    again = (w.upload(PB, PB_BYTES), w.upload(NEAR, NEAR_BYTES))
    chat = ChatSession(id="s-26246584", user_id=w.gareth.id, title="Sa Calobra Sub-30 Preparation")
    db.add(chat)
    db.commit()

    def row(role, content, minute, snapshot=None):
        db.add(ChatMessage(session_id=chat.id, role=role, content=content,
                           context_snapshot=snapshot, created_at=datetime(2026, 10, 16, 4, minute)))

    def old_files(ids):
        return {"recent_rides": [], "attachments": [{"attachment_id": i} for i in ids]}

    row(ChatRole.user, T1, 11)
    row(ChatRole.assistant, "Analysis of both.", 12, old_files(first))
    row(ChatRole.user, T2, 16)
    row(ChatRole.assistant, coach_service._OLD_NO_REPLY_FALLBACK, 17, {"recent_rides": []})
    row(ChatRole.user, T3, 20)
    row(ChatRole.assistant, "I can only find the 29:53 in your saved rides.", 21, {"recent_rides": []})
    row(ChatRole.user, T4, 24)
    row(ChatRole.assistant, "Both again, analysed.", 25, old_files(again))
    db.commit()

    found = backfill.plan(db)
    assert backfill.apply(db, found.bindings, found.unbound) == 4

    w.at(9, 0)
    w.send(chat.id, "Cadence again?", turn="t5")
    call = w.model.for_turn("t5")[0]
    files = _files(call["system"])
    assert sorted(f["attachment_id"] for f in files["files"]) == sorted(again)
    coach = [msg["content"] for msg in call["messages"] if msg["role"] == ChatRole.assistant]
    assert coach[1] == coach_service._NO_REPLY_NOTE


# ── The climb list ──────────────────────────────────────────────────────────


def test_a_climb_that_ends_where_the_file_ends_is_never_called_no_climb(world):
    """The finder closes a climb only after 30 s off the climb, so a file that
    ends at the top lists none. An empty list now stays out of the coach's
    view, and the note says what a missing one means."""
    w = world
    to_the_top = w.upload(PB, PB_BYTES)
    over_the_top = w.upload("with_descent.gpx", _climb("2026-05-05", 400, 330, 84, descent=60))
    assert w.db.get(ChatAttachment, to_the_top).analysis["climbs"] == []
    assert w.db.get(ChatAttachment, over_the_top).analysis["climbs"]

    sid = w.new_chat()
    w.send(sid, T1, [to_the_top, over_the_top], turn="t1")
    system = w.model.for_turn("t1")[0]["system"]
    shown = {f["attachment_id"]: f for f in _files(system)["files"]}
    assert "climbs" not in shown[to_the_top]["analysis"]
    assert shown[over_the_top]["analysis"]["climbs"][0]["gain_m"] >= 25
    assert "which does not mean there was none" in _files_text(system)
    assert "Never tell the rider a ride had no climb for that reason" in _files_text(system)


# ── Deleting a chat ─────────────────────────────────────────────────────────


def test_deleting_chat_a_leaves_a_file_also_sent_into_chat_b_readable_there(world):
    w = world
    pb = w.upload(PB, PB_BYTES)
    chat_a = w.new_chat()
    w.send(chat_a, T1, [pb], turn="t1")
    chat_b = w.new_chat()
    w.send(chat_b, "Compare with this one.", [pb], turn="t4")
    only_a = w.upload(NEAR, NEAR_BYTES, chat_a)
    w.send(chat_a, "And this.", [only_a], turn="t4")

    assert w.client.delete(f"/api/v1/chat/sessions/{chat_a}").status_code == 204

    w.db.expire_all()
    assert w.db.get(ChatAttachment, pb).session_id == chat_b
    assert w.db.get(ChatAttachment, only_a) is None
    w.send(chat_b, "Still there?", turn="t5")
    call = w.model.for_turn("t5")[0]
    assert [f["attachment_id"] for f in _files(call["system"])["files"]] == [pb]
    assert _users_in(call)[0].startswith("[Files shared with this message, by file name: ")
