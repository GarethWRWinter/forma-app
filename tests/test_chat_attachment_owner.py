"""Whose ride a shared file is: recorded once, guarding the save and memory.

16 Oct 2026, Gareth's Sa Calobra chat. A file shared in a chat now stays for
the whole chat, so far more turns talk about it, and every turn ends in a
memory pass. A mate's file shared on turn 1 can still be under discussion on
turn 9, and each of those passes is a chance to file the mate's numbers as
Gareth's. So each file records whose ride it is (self, someone_else or
unknown, unknown until the rider says), the coach sees it, a file recorded
as someone else's is never saved into the rider's history, and the memory
pass after a turn in a chat holding a file not known to be the rider's own
is told so in plain words.

Also here: an upload made inside an open chat is tied to it at once, and is
deleted with it, yet the coach only sees it once a message has carried it.
"""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.models.chat import ChatSession
from app.models.chat_attachment import ChatAttachment
from app.models.ride import Ride
from app.models.user import User
from app.services import attachment_service, coach_service, memory_service
from tests.test_chat_attachment_working_set import NEAR, PB, T0, _gpx, _upload

FILES_HEADING = "## Files shared in this chat"
DASHES = (chr(0x2014), chr(0x2013))  # em and en dash


@pytest.fixture
def gareth(db_session):
    user = User(id="u-gareth", email="g@example.com", hashed_password="x",
                full_name="Gareth Winter", ftp=290)
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def mate(db_session):
    user = User(id="u-mate", email="m@example.com", hashed_password="x",
                full_name="Mate", ftp=250)
    db_session.add(user)
    db_session.commit()
    return user


def _chat(db, user, sid="s-calobra") -> ChatSession:
    chat = ChatSession(id=sid, user_id=user.id, title="Chat")
    db.add(chat)
    db.commit()
    return chat


def _send(db, user, chat, text, ids=None, voice=False) -> list[dict]:
    attachment_service.bind_to_session(db, user.id, chat.id, ids or [])
    stream = coach_service.stream_voice_response if voice else coach_service.stream_response

    async def run():
        return [c async for c in stream(db, user, chat, text, attachment_ids=ids or [])]

    return [json.loads(c[len("data: "):]) for c in asyncio.run(run())]


def _files(call) -> dict:
    found = [b["text"] for b in call["system"] if b["text"].startswith(FILES_HEADING)]
    if not found:
        return {}
    text = found[0]
    start = text.index("```json\n") + len("```json\n")
    payload = json.loads(text[start:text.index("\n```", start)])
    return {f["attachment_id"]: f for f in payload["files"]}


def _owner(db, attachment) -> str:
    db.expire_all()
    return attachment_service.file_owner(db.get(ChatAttachment, attachment.id))


@pytest.fixture
def extractions(monkeypatch):
    """Every memory pass a turn asked for, with its arguments."""
    seen = []
    monkeypatch.setattr(
        "app.services.memory_service.extract_memories",
        lambda *args, **kwargs: seen.append(kwargs),
    )
    return seen


# ── The owner tool ──────────────────────────────────────────────────────────


def test_the_coach_records_whose_ride_it_is_and_sees_it_after(db_session, gareth, scripted_model):
    chat = _chat(db_session, gareth)
    mine = _upload(db_session, gareth, PB, "2026-05-01T08:00:00", 330, 93, T0)
    _send(db_session, gareth, chat, "Look at this.", [mine.id])
    assert _files(scripted_model.calls[0])[mine.id]["owner"] == "unknown"

    scripted_model.append(([], [scripted_model.tool_use(
        "set_attachment_owner", {"attachment_id": mine.id, "owner": "self"})]))
    scripted_model.append((["Noted, your PB."], []))
    chunks = _send(db_session, gareth, chat, "It's my PB from May.")

    result = scripted_model.calls[2]["messages"][-1]["content"][0]["content"]
    assert result == (
        f"Recorded. {PB} is the rider's own ride. Nothing in the rider's "
        "history, plan or training load changed."
    )
    assert {"type": "status", "content": "Noting whose ride it is"} in chunks
    # It writes only to the file, so the calendar is never told to refetch.
    assert not [c for c in chunks if c["type"] == "plan_updated"]
    assert _owner(db_session, mine) == "self"

    _send(db_session, gareth, chat, "Cadence?")
    shown = _files(scripted_model.calls[-1])[mine.id]
    assert shown["owner"] == "self"
    assert "owner" not in shown["summary"]


def test_the_owner_tool_reaches_only_the_riders_own_files(db_session, gareth, mate):
    theirs = _upload(db_session, mate, NEAR, "2026-05-03T08:00:00", 316, 89, T0)
    mine = _upload(db_session, gareth, PB, "2026-05-01T08:00:00", 330, 93, T0)

    assert coach_service._execute_tool(
        db_session, gareth, "set_attachment_owner",
        {"attachment_id": theirs.id, "owner": "self"},
    ) == "Error: Attachment not found."
    assert coach_service._execute_tool(
        db_session, gareth, "set_attachment_owner",
        {"attachment_id": mine.id, "owner": "mine"},
    ).startswith("Error: nothing was recorded.")
    assert _owner(db_session, theirs) == "unknown"
    assert _owner(db_session, mine) == "unknown"


def test_recording_a_saved_file_as_someone_elses_says_the_ride_stays(db_session, gareth):
    a = _upload(db_session, gareth, PB, "2026-05-01T08:00:00", 330, 93, T0)
    a.imported_ride_id = "r-already"
    db_session.commit()

    said = coach_service._execute_tool(
        db_session, gareth, "set_attachment_owner",
        {"attachment_id": a.id, "owner": "someone_else"},
    )
    assert "does not take that ride out: tell the rider so" in said
    assert "cannot be saved" not in said


# ── The save ────────────────────────────────────────────────────────────────


def _save(db, user, attachment) -> str:
    return coach_service._execute_tool(
        db, user, "save_attachment_as_ride",
        {"attachment_id": attachment.id, "confirmed": True},
    )


def test_a_file_recorded_as_someone_elses_is_never_saved(db_session, gareth):
    theirs = _upload(db_session, gareth, NEAR, "2026-05-03T08:00:00", 316, 89, T0)
    attachment_service.set_owner(db_session, theirs, "someone_else")

    said = _save(db_session, gareth, theirs)
    assert said.startswith("Error: nothing was saved. This file is recorded as someone else's ride")
    assert "set_attachment_owner" in said
    assert db_session.query(Ride).count() == 0
    db_session.expire_all()
    assert db_session.get(ChatAttachment, theirs.id).imported_ride_id is None

    # The rider says it is theirs after all: recorded, then it saves.
    attachment_service.set_owner(db_session, theirs, "self")
    assert _save(db_session, gareth, theirs).startswith("Saved ")
    assert db_session.query(Ride).count() == 1


def test_saving_a_file_marks_it_as_the_riders_own_once_and_for_a_copy_too(db_session, gareth):
    first = _upload(db_session, gareth, PB, "2026-05-01T08:00:00", 330, 93, T0)
    assert _save(db_session, gareth, first).startswith("Saved ")
    assert _owner(db_session, first) == "self"

    # The same ride attached again joins the ride already saved, and is the
    # rider's own too.
    again = _upload(db_session, gareth, PB, "2026-05-01T08:00:00", 330, 93, T0)
    assert _save(db_session, gareth, again).startswith("Saved ")
    assert db_session.query(Ride).count() == 1
    assert _owner(db_session, again) == "self"


# ── The memory pass ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("voice", [False, True], ids=["chat", "voice"])
def test_the_memory_pass_is_cautioned_until_every_file_is_the_riders_own(
    db_session, gareth, scripted_model, extractions, voice
):
    chat = _chat(db_session, gareth)
    pb = _upload(db_session, gareth, PB, "2026-05-01T08:00:00", 330, 93, T0)
    near = _upload(db_session, gareth, NEAR, "2026-05-03T08:00:00", 316, 89,
                   T0 + timedelta(seconds=1))

    _send(db_session, gareth, chat, "Overlay these.", [pb.id, near.id], voice=voice)
    assert extractions[-1]["caution"] == (
        "This chat holds ride files that are not known to be the rider's own. "
        f"Not known whose: {PB}, {NEAR}. Numbers from someone else's file "
        "describe that person's ride: never record them as the rider's "
        "figures, efforts, personal bests or fitness. Numbers from a file whose "
        "owner is not known count as the rider's only where the rider's own "
        "words in the text claim them."
    )

    # Recorded during the turn, and the caution after it already knows.
    scripted_model.append(([], [
        scripted_model.tool_use("set_attachment_owner", {"attachment_id": pb.id, "owner": "self"}),
        scripted_model.tool_use("set_attachment_owner", {"attachment_id": near.id, "owner": "someone_else"}),
    ]))
    scripted_model.append((["Got it: yours and your mate's."], []))
    _send(db_session, gareth, chat, "The 29:53 is mine, the 30:47 is my mate's.", voice=voice)
    caution = extractions[-1]["caution"]
    assert f"Someone else's: {NEAR}." in caution
    assert PB not in caution

    attachment_service.set_owner(db_session, db_session.get(ChatAttachment, near.id), "self")
    _send(db_session, gareth, chat, "Actually both are mine.", voice=voice)
    assert extractions[-1]["caution"] is None


def test_a_chat_without_files_gives_the_memory_pass_no_caution(
    db_session, gareth, scripted_model, extractions
):
    chat = _chat(db_session, gareth)
    _send(db_session, gareth, chat, "How was my week?")
    assert extractions[-1]["caution"] is None
    assert extractions[-1]["source_ref"] == chat.id


def test_the_caution_sits_beside_the_text_and_changes_nothing_without_one(
    db_session, gareth, monkeypatch
):
    sent = []

    def fake_call(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(
            type="text", text='{"entities": [], "edges": [], "dossier": []}')])

    monkeypatch.setattr(memory_service.forma_core, "call", fake_call)
    text = "Rider: my 29:53 up Sa Calobra.\n\nForma: That is a strong climb."

    memory_service.extract_memories(db_session, gareth, text, source="chat", source_ref="s-1")
    memory_service.extract_memories(
        db_session, gareth, text, source="chat", source_ref="s-1",
        caution="This chat holds a mate's file.",
    )

    plain, cautioned = (c["messages"][0]["content"] for c in sent)
    assert plain == (
        "KNOWN ENTITIES (dedup against these; you may create edges to them):\n"
        f"(none yet)\n\nSOURCE: chat\n\nTEXT:\n{text}"
    )
    assert cautioned == plain.replace(
        "SOURCE: chat\n\n", "SOURCE: chat\n\nCAUTION: This chat holds a mate's file.\n\n"
    )
    assert sent[0]["system"] == sent[1]["system"]


# ── An upload inside an open chat ───────────────────────────────────────────


@pytest.fixture
def threaded_db():
    """The routes run in TestClient's worker thread, so the in-memory
    database must be shareable across threads for these tests."""
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
def upload(threaded_db):
    """POST a file to the real upload route as one rider."""
    from fastapi.testclient import TestClient

    from app.api.v1.deps import get_current_user, require_paid_access
    from app.database import get_db
    from app.main import app

    def post(rider, name, body, session_id=None):
        app.dependency_overrides[get_db] = lambda: threaded_db
        app.dependency_overrides[require_paid_access] = lambda: rider
        app.dependency_overrides[get_current_user] = lambda: rider
        try:
            return TestClient(app).post(
                "/api/v1/chat/attachments",
                files={"file": (name, body, "application/gpx+xml")},
                data={"session_id": session_id} if session_id else {},
            )
        finally:
            app.dependency_overrides.clear()

    return post


def _rider(db, uid):
    user = User(id=uid, email=f"{uid}@example.com", hashed_password="x", ftp=290)
    db.add(user)
    db.commit()
    return user


def test_an_upload_inside_an_open_chat_is_tied_to_it_at_once(threaded_db, upload, scripted_model):
    db = threaded_db
    rider = _rider(db, "u-up")
    chat = _chat(db, rider, "s-open")

    r = upload(rider, PB, _gpx("2026-05-01T08:00:00", 120, 330, 93), chat.id)
    assert r.status_code == 201, r.text
    row = db.get(ChatAttachment, r.json()["id"])
    assert row.session_id == chat.id

    # Not sent yet, so the coach does not see it.
    _send(db, rider, chat, "Before I send it.")
    assert _files(scripted_model.calls[-1]) == {}

    _send(db, rider, chat, "Look at this.", [row.id])
    shown = _files(scripted_model.calls[-1])
    assert list(shown) == [row.id]
    assert "unsent" not in shown[row.id]["summary"]
    db.expire_all()
    assert db.get(ChatAttachment, row.id).session_id == chat.id


def test_a_file_taken_off_the_composer_never_reaches_the_coach_and_goes_with_the_chat(
    threaded_db, upload, scripted_model
):
    db = threaded_db
    rider = _rider(db, "u-up")
    chat = _chat(db, rider, "s-open")
    sent_id = upload(rider, PB, _gpx("2026-05-01T08:00:00", 120, 330, 93), chat.id).json()["id"]
    removed_id = upload(rider, NEAR, _gpx("2026-05-03T08:00:00", 120, 316, 89), chat.id).json()["id"]

    _send(db, rider, chat, "Just the one.", [sent_id])
    _send(db, rider, chat, "Cadence?")
    assert list(_files(scripted_model.calls[-1])) == [sent_id]
    assert "Sa_Calobra_30_47" not in json.dumps(scripted_model.calls[-1]["system"])

    coach_service.delete_session(db, db.get(ChatSession, chat.id))
    assert db.query(ChatAttachment).filter(
        ChatAttachment.id.in_([sent_id, removed_id])).count() == 0


def test_a_file_attached_in_one_chat_and_sent_in_another_goes_where_it_was_sent(
    threaded_db, upload, scripted_model
):
    db = threaded_db
    rider = _rider(db, "u-up")
    attached_in = _chat(db, rider, "s-attached")
    sent_in = _chat(db, rider, "s-sent")
    aid = upload(rider, PB, _gpx("2026-05-01T08:00:00", 120, 330, 93), attached_in.id).json()["id"]

    _send(db, rider, sent_in, "Look at this.", [aid])
    db.expire_all()
    assert db.get(ChatAttachment, aid).session_id == sent_in.id

    # Sent now, it stays with the chat it was sent in.
    assert attachment_service.bind_to_session(db, rider.id, attached_in.id, [aid]) == 0
    _send(db, rider, attached_in, "Anything here?")
    assert _files(scripted_model.calls[-1]) == {}
    db.expire_all()
    assert db.get(ChatAttachment, aid).session_id == sent_in.id


def test_an_upload_naming_someone_elses_chat_is_refused(threaded_db, upload):
    db = threaded_db
    rider = _rider(db, "u-up")
    stranger = _rider(db, "u-stranger")
    theirs = _chat(db, stranger, "s-theirs")

    r = upload(rider, PB, _gpx("2026-05-01T08:00:00", 120, 330, 93), theirs.id)
    assert r.status_code == 404
    assert db.query(ChatAttachment).count() == 0


# ── The words ───────────────────────────────────────────────────────────────


def test_the_new_words_carry_no_dashes():
    tool = next(t for t in coach_service.COACH_TOOLS if t["name"] == "set_attachment_owner")
    words = [
        tool["description"],
        json.dumps(tool["input_schema"]),
        coach_service._FILES_NOTE,
        coach_service._NOT_OWN_FILES_CAUTION,
        coach_service._FILES_UNCHECKED_CAUTION,
        coach_service._TOOL_STATUS["set_attachment_owner"],
    ]
    for text in words:
        assert not any(d in text for d in DASHES), text
        assert "kicker" not in text.lower()
    assert "set_attachment_owner" in coach_service._NO_PLAN_CHANGE_TOOLS
