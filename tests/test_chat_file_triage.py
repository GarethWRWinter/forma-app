"""A ride file in the chat is recognised before anyone is asked anything.

Gareth asked for this after his Sa Calobra chat of 16 Oct 2026. His 29:53
GPX was already in his saved history from the Strava archive import, yet
the coach asked whether to save it. His 30:47 GPX was not in history at all.
A pro's KOM file, shared to compare against, carries a 20-minute power no
amateur on 290 W holds.

So at upload Forma checks a file against Rides, with no model call: the same
ride is linked (no question), a stretch of a saved ride counts as part of it
(never saved again), and anything else gets a card under the message with
three choices. The rider files a file with a button, never with a chat
"yes", and the coach is told what was chosen on its next turn.
"""

import asyncio
import io
import json
import zipfile
from datetime import datetime, timedelta

import pytest

from app.models.chat import ChatSession
from app.models.chat_attachment import ChatAttachment
from app.models.ride import Ride
from app.models.user import User
from app.services import attachment_service, coach_service, file_triage_service
from app.services.ride_service import create_ride_from_fit

FILES_HEADING = "## Files shared in this chat"
DASHES = (chr(0x2014), chr(0x2013))
NBSP = "\u00a0"

PB = "Sa_Calobra_29_53_sub_30_mission_complete_Failure_is_the_path_to_success_.gpx"
NEAR = "Sa_Calobra_30_47_Can_I_find_47_seconds_.gpx"
KOM = "Sa_Calobra_KOM_pro.gpx"


def _gpx(start: str, seconds: int, watts=None, hr=None, rpm=None) -> bytes:
    """A climb file, one point a second. Leave watts, hr and rpm out for a
    trace with none of them."""
    t0 = datetime.fromisoformat(start)
    points = []
    for i in range(seconds + 1):
        ts = (t0 + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ext = ""
        if watts is not None:
            ext += f"<power>{watts}</power>"
        tpx = ""
        if hr is not None:
            tpx += f"<gpxtpx:hr>{hr}</gpxtpx:hr>"
        if rpm is not None:
            tpx += f"<gpxtpx:cad>{rpm}</gpxtpx:cad>"
        if tpx:
            ext += f"<gpxtpx:TrackPointExtension>{tpx}</gpxtpx:TrackPointExtension>"
        points.append(
            f'<trkpt lat="{39.80 + i * 0.00003:.6f}" lon="2.80"><ele>{5 + 0.25 * i:.2f}</ele>'
            f"<time>{ts}</time>" + (f"<extensions>{ext}</extensions>" if ext else "") + "</trkpt>"
        )
    return (
        '<?xml version="1.0"?><gpx version="1.1" '
        'xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">'
        "<trk><trkseg>" + "".join(points) + "</trkseg></trk></gpx>"
    ).encode()


# Gareth's two efforts and a pro's: 29:53 and 30:47 are 1793 s and 1847 s.
PB_BYTES = _gpx("2026-05-01T08:00:00", 1793, watts=330, hr=168, rpm=84)
NEAR_BYTES = _gpx("2026-05-03T08:00:00", 1847, watts=316, hr=165, rpm=79)
KOM_BYTES = _gpx("2026-04-12T10:00:00", 1500, watts=452, hr=178, rpm=92)


@pytest.fixture(autouse=True)
def _offline_ride_import(monkeypatch):
    """Saving a ride names its town and looks up its weather: neither here."""
    monkeypatch.setattr("app.services.geo.locale_for", lambda lat, lon: "Escorca, Spain")
    monkeypatch.setattr("app.services.weather_service.ride_conditions", lambda *a, **k: None)


@pytest.fixture
def gareth(db_session):
    user = User(id="u-gareth", email="g@example.com", hashed_password="x",
                full_name="Gareth Winter", ftp=290, max_hr=186)
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


def _archive(db, user, data: bytes, name="activities/123.gpx", source="strava") -> Ride:
    """A ride already in history with its per-second data, the way the
    Strava archive import leaves one."""
    ride = create_ride_from_fit(db, user, data, filename=name, source=source)
    ride.title = "Sprint Training"
    db.commit()
    return ride


def _attach(db, user, name: str, data: bytes) -> ChatAttachment:
    return attachment_service.ingest(db, user, data, name)


def _card(db, user, attachment) -> dict:
    db.expire_all()
    a = db.get(ChatAttachment, attachment.id)
    return file_triage_service.cards(db, user.id, [a])[0]


def _choices(card: dict, key="choices") -> list[str]:
    return [c["choice"] for c in card[key]]


def _words(text: str | None) -> str | None:
    return text.replace(NBSP, " ") if text else text


# ── The three motivating files ──────────────────────────────────────────────


def test_gareths_three_files(db_session, gareth):
    """The 29:53 is in history from the archive: linked, no question. The
    30:47 is not: the card asks. The pro's KOM: the card wonders aloud."""
    saved = _archive(db_session, gareth, PB_BYTES)

    pb = _attach(db_session, gareth, PB, PB_BYTES)
    card = _card(db_session, gareth, pb)
    assert (card["state"], card["view"], card["question"]) == ("in_history", "in_history", None)
    assert card["choices"] == [] and card["ride"]["id"] == saved.id
    assert _words(card["recognised"]) == "Already in your rides as Sprint Training, 1 May 2026."
    assert pb.imported_ride_id == saved.id
    assert attachment_service.file_owner(pb) == "self"
    assert pb.summary["triage"]["matched"] == "time"

    near = _attach(db_session, gareth, NEAR, NEAR_BYTES)
    card = _card(db_session, gareth, near)
    assert (card["state"], card["view"]) == ("new", "asking")
    assert _choices(card) == ["save", "chat_only", "someone_else"]
    assert [_words(c["label"]) for c in card["choices"]] == [
        "Mine: save to my rides", "Mine: just for this chat", "Someone else\u2019s",
    ]
    assert _words(card["choices"][0]["detail"]) == "It joins Rides and your fitness chart for 3 May 2026."
    assert _words(card["recognised"]) == "Not in your rides yet."
    assert near.imported_ride_id is None
    assert attachment_service.file_owner(near) == "unknown"

    kom = _attach(db_session, gareth, KOM, KOM_BYTES)
    card = _card(db_session, gareth, kom)
    assert card["view"] == "asking" and "faster" in card["hints"]
    assert _words(card["recognised"]) == (
        "Its best 20 minutes is 452 W, well above your FTP of 290 W."
    )
    assert _words(card["question"]) == "Is it someone else\u2019s?"
    # A hint phrases the question and decides nothing.
    assert attachment_service.file_owner(kom) == "unknown"
    assert db_session.query(Ride).count() == 1


# ── Each state ──────────────────────────────────────────────────────────────


def test_the_same_bytes_as_a_file_saved_from_chat_are_in_history(db_session, gareth):
    first = _attach(db_session, gareth, NEAR, NEAR_BYTES)
    ride = attachment_service.decide(db_session, gareth, first, "save")

    again = _attach(db_session, gareth, "renamed.gpx", NEAR_BYTES)
    triage = again.summary["triage"]
    assert (triage["state"], triage["matched"], triage["ride_id"]) == ("in_history", "bytes", ride.id)
    card = _card(db_session, gareth, again)
    # The same bytes are the saved ride itself, so there is nothing to undo here.
    assert (card["view"], card["choices"], card["changes"]) == ("in_history", [], [])


def test_a_stretch_of_a_saved_ride_is_part_of_it_and_never_saved_again(db_session, gareth):
    whole = _archive(db_session, gareth, _gpx("2026-05-01T07:30:00", 3 * 3600, watts=330, hr=168))
    climb = _attach(db_session, gareth, "climb_only.gpx", PB_BYTES)

    card = _card(db_session, gareth, climb)
    assert (card["state"], card["view"], card["overlap"]) == ("part_of_ride", "part_of_ride", 1.0)
    assert card["choices"] == [] and card["ride"]["id"] == whole.id
    assert _words(card["recognised"]) == "Part of Sprint Training, 1 May 2026, already in your rides."
    assert attachment_service.file_owner(climb) == "self"
    # Part of a ride is not that ride: the file keeps no link of its own.
    assert climb.imported_ride_id is None

    with pytest.raises(attachment_service.DecisionRefused) as refused:
        attachment_service.decide(db_session, gareth, climb, "save")
    assert refused.value.code == "part_of_ride"
    assert db_session.query(Ride).count() == 1


def test_a_file_mostly_inside_a_saved_ride_may_be_kept_separately(db_session, gareth):
    _archive(db_session, gareth, _gpx("2026-05-01T08:10:00", 1800, watts=330, hr=168))
    # Starts ten minutes early: two thirds of it sits inside the saved ride.
    early = _attach(db_session, gareth, "early.gpx", PB_BYTES)

    card = _card(db_session, gareth, early)
    assert card["view"] == "part_of_ride" and 0.5 <= card["overlap"] < 0.9
    assert _choices(card) == ["save"]
    assert _words(card["choices"][0]["label"]) == "Keep as a separate ride"
    assert _words(card["recognised"]).startswith("Most of it overlaps Sprint Training")

    ride = attachment_service.decide(db_session, gareth, early, "save")
    assert db_session.query(Ride).count() == 2
    card = _card(db_session, gareth, early)
    assert (card["view"], card["ride"]["id"]) == ("saved", ride.id)


def test_a_riding_partners_file_from_the_same_outing_is_not_called_yours(db_session, gareth):
    """Same start, same length, very different watts and heart rate: the
    time match alone would have linked Tom's file to Gareth's own ride."""
    _archive(db_session, gareth, _gpx("2026-05-01T08:00:00", 1793, watts=250, hr=150))
    tom = _attach(db_session, gareth, "tom.gpx", PB_BYTES)

    card = _card(db_session, gareth, tom)
    assert (card["state"], card["view"]) == ("new", "asking")
    assert "alongside" in card["hints"]
    assert _words(card["recognised"]) == (
        "Recorded alongside Sprint Training, 1 May 2026, but the numbers differ."
    )
    assert _words(card["question"]) == "Is it someone else\u2019s?"
    assert tom.imported_ride_id is None


def test_a_file_matched_by_time_then_said_to_be_someone_elses_lets_go(
    db_session, gareth, scripted_model
):
    """Where the numbers cannot tell (no power or heart rate in the file),
    the time match links it. Tom's file from the same ride, said to be his,
    lets go of Gareth's ride and the coach is told whose ride it lines up
    with."""
    saved = _archive(db_session, gareth, PB_BYTES)
    tom = _attach(db_session, gareth, "tom.gpx", _gpx("2026-05-01T08:00:00", 1793))
    assert tom.imported_ride_id == saved.id
    card = _card(db_session, gareth, tom)
    assert card["view"] == "in_history" and _choices(card, "changes") == ["someone_else"]

    said = coach_service._execute_tool(
        db_session, gareth, "set_attachment_owner",
        {"attachment_id": tom.id, "owner": "someone_else", "label": "Tom"},
    )
    assert "lined up in time with a ride in the rider's history" in said
    db_session.expire_all()
    assert db_session.get(ChatAttachment, tom.id).imported_ride_id is None
    assert _card(db_session, gareth, tom)["view"] == "someone_else"

    chat = ChatSession(id="s-together", user_id=gareth.id, title="Chat")
    db_session.add(chat)
    db_session.commit()
    _send(db_session, gareth, chat, "How did Tom pace it?", [tom.id])
    triage = _files(scripted_model.calls[-1])[tom.id]["triage"]
    assert triage["state"] == "new" and "saved_ride" not in triage
    assert triage["lines_up_with"]["ride_id"] == saved.id

    # His after all was a mistake: the link comes back.
    attachment_service.decide(db_session, gareth, tom, "chat_only")
    db_session.expire_all()
    assert db_session.get(ChatAttachment, tom.id).imported_ride_id == saved.id
    assert _card(db_session, gareth, tom)["view"] == "in_history"


def test_a_second_device_on_the_same_outing_agrees_and_is_linked(db_session, gareth):
    saved = _archive(db_session, gareth, PB_BYTES)
    # The head unit's own file: same ride, power a touch different.
    other = _attach(db_session, gareth, "wahoo.gpx", _gpx("2026-05-01T08:00:00", 1793, watts=318, hr=168))
    assert other.summary["triage"]["state"] == "in_history"
    assert other.imported_ride_id == saved.id


@pytest.mark.parametrize("hint,data,line", [
    ("high_hr", _gpx("2026-05-05T08:00:00", 300, watts=250, hr=199),
     "Its heart rate reaches 199 bpm, above your max of 186 bpm."),
    ("route", _gpx("2026-05-05T08:00:00", 300),
     "No power or heart rate in it, so it reads like a route or a GPS trace."),
    ("before_first_ride", _gpx("2024-03-02T08:00:00", 300, watts=250, hr=150),
     "From 2 Mar 2024, older than anything in your rides."),
])
def test_the_quiet_hints(db_session, gareth, hint, data, line):
    _archive(db_session, gareth, _gpx("2025-01-10T08:00:00", 600, watts=200, hr=140))
    a = _attach(db_session, gareth, f"{hint}.gpx", data)
    card = _card(db_session, gareth, a)
    assert hint in card["hints"] and card["view"] == "asking"
    assert _words(card["recognised"]) == line
    assert attachment_service.file_owner(a) == "unknown"


def test_no_hint_without_the_riders_numbers(db_session):
    """No FTP, no max HR and no rides: nothing to compare against."""
    blank = User(id="u-blank", email="b@example.com", hashed_password="x")
    db_session.add(blank)
    db_session.commit()
    a = _attach(db_session, blank, KOM, KOM_BYTES)
    assert a.summary["triage"]["hints"] == {}


# ── Re-uploads ──────────────────────────────────────────────────────────────


def test_a_re_upload_inherits_the_choice_and_a_choice_reaches_every_copy(db_session, gareth):
    first = _attach(db_session, gareth, KOM, KOM_BYTES)
    second = _attach(db_session, gareth, KOM.replace(".gpx", " (1).gpx"), KOM_BYTES)
    attachment_service.decide(db_session, gareth, second, "someone_else", "  Tom Pidcock\u2019s\nKOM ")

    # Chosen on the newer card, the older card under an earlier message agrees.
    for a in (first, second):
        card = _card(db_session, gareth, a)
        assert (card["view"], card["label"]) == ("someone_else", "Tom Pidcock\u2019s KOM")
        assert _words(card["outcome"]) == "Tom Pidcock\u2019s KOM, for comparison only."
        assert _choices(card, "changes") == ["chat_only"]

    third = _attach(db_session, gareth, KOM, KOM_BYTES)
    triage = third.summary["triage"]
    assert (triage["decision"], triage["label"]) == ("someone_else", "Tom Pidcock\u2019s KOM")
    assert attachment_service.file_owner(third) == "someone_else"

    # Mine after all: the change reaches every copy, and so does a save.
    attachment_service.decide(db_session, gareth, first, "chat_only")
    assert _card(db_session, gareth, third)["view"] == "chat_only"
    ride = attachment_service.decide(db_session, gareth, third, "save")
    assert {_card(db_session, gareth, a)["ride"]["id"] for a in (first, second, third)} == {ride.id}
    fourth = _attach(db_session, gareth, KOM, KOM_BYTES)
    assert _card(db_session, gareth, fourth)["view"] == "in_history"
    assert db_session.query(Ride).count() == 1


# ── The decision API ────────────────────────────────────────────────────────


@pytest.fixture
def api(gareth, monkeypatch, request):
    """The real routes over a database the worker thread can share."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.api.v1 import chat as chat_api
    from app.api.v1.deps import get_current_user, require_paid_access
    from app.database import get_db
    from app.main import app
    from app.models.base import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    rider = User(id=gareth.id, email=gareth.email, hashed_password="x",
                 full_name=gareth.full_name, ftp=290, max_hr=186)
    db.add(rider)
    db.add(User(id="u-mate", email="m@example.com", hashed_password="x", full_name="Mate"))
    db.commit()

    who = {"user": rider}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_paid_access] = lambda: who["user"]
    app.dependency_overrides[get_current_user] = lambda: who["user"]
    app.dependency_overrides[chat_api._attachment_limit] = lambda: None
    client = TestClient(app)
    client.db, client.rider, client.who = db, rider, who

    def upload(name, data, session_id=None):
        r = client.post(
            "/api/v1/chat/attachments",
            files={"file": (name, data, "application/octet-stream")},
            data={"session_id": session_id} if session_id else {},
        )
        return r

    def decide(attachment_id, choice, label=None):
        body = {"choice": choice, **({"label": label} if label is not None else {})}
        return client.post(f"/api/v1/chat/attachments/{attachment_id}/decision", json=body)

    client.upload, client.decide = upload, decide
    yield client
    app.dependency_overrides.clear()
    db.close()


def test_the_upload_carries_the_card(api):
    r = api.upload(NEAR, NEAR_BYTES)
    assert r.status_code == 201, r.text
    card = r.json()["card"]
    assert (card["id"], card["view"], card["date"], card["duration"], card["distance_km"]) == (
        r.json()["id"], "asking", "2026-05-03", "30:47", r.json()["summary"]["distance_km"],
    )


def test_deciding_reaches_only_the_riders_own_files(api):
    mine = api.upload(NEAR, NEAR_BYTES).json()["id"]
    api.who["user"] = api.db.get(User, "u-mate")
    assert api.decide(mine, "save").status_code == 404
    assert api.decide(mine, "someone_else", "Gareth").status_code == 404
    api.who["user"] = api.rider
    assert api.decide(mine, "chat_only").json()["view"] == "chat_only"
    assert api.db.query(Ride).count() == 0


def test_someone_elses_file_is_refused_a_save(api):
    kom = api.upload(KOM, KOM_BYTES).json()["id"]
    assert api.decide(kom, "someone_else", "x" * 41).status_code == 400
    r = api.decide(kom, "someone_else", "Tom Pidcock\u2019s KOM")
    assert (r.status_code, r.json()["view"], r.json()["decision"]) == (200, "someone_else", "someone_else")

    r = api.decide(kom, "save")
    assert r.status_code == 409
    assert "someone else\u2019s ride" in r.json()["detail"]
    assert api.db.query(Ride).count() == 0
    said = coach_service._execute_tool(
        api.db, api.rider, "save_attachment_as_ride", {"attachment_id": kom, "confirmed": True}
    )
    assert said.startswith("Error: nothing was saved. This file is recorded as someone else's ride")


def test_the_same_choice_twice_changes_nothing(api):
    near = api.upload(NEAR, NEAR_BYTES).json()["id"]
    first = api.decide(near, "save").json()
    second = api.decide(near, "save").json()
    assert first == second and first["view"] == "saved"
    assert api.db.query(Ride).count() == 1

    kom = api.upload(KOM, KOM_BYTES).json()["id"]
    once = api.decide(kom, "chat_only").json()
    assert api.decide(kom, "chat_only").json()["decided_at"] == once["decided_at"]


def test_a_save_of_a_file_already_in_history_returns_that_ride(api):
    saved = _archive(api.db, api.rider, PB_BYTES)
    pb = api.upload(PB, PB_BYTES).json()
    assert pb["card"]["view"] == "in_history"
    r = api.decide(pb["id"], "save")
    assert (r.status_code, r.json()["ride"]["id"], r.json()["view"]) == (200, saved.id, "in_history")
    assert api.db.query(Ride).count() == 1


def test_a_save_is_undone_from_rides_not_from_the_card(api):
    near = api.upload(NEAR, NEAR_BYTES).json()["id"]
    card = api.decide(near, "save").json()
    assert _words(card["undo"]) == "To undo it, delete the ride from Rides."
    assert card["changes"] == []
    assert api.decide(near, "chat_only").status_code == 409
    assert api.decide(near, "someone_else").status_code == 409

    # Deleted from Rides: the card says it is just for this chat, and a
    # second save is a fresh one.
    api.db.query(Ride).filter(Ride.id == card["ride"]["id"]).delete()
    api.db.commit()
    api.db.expire_all()
    again = api.decide(near, "chat_only").json()
    assert again["view"] == "chat_only"
    assert api.decide(near, "save").json()["view"] == "saved"
    assert api.db.query(Ride).count() == 1


def test_cards_survive_a_reload(api):
    sid = api.post("/api/v1/chat/sessions", json={}).json()["id"]
    pb_saved = _archive(api.db, api.rider, PB_BYTES)
    pb = api.upload(PB, PB_BYTES, sid).json()["id"]
    near = api.upload(NEAR, NEAR_BYTES, sid).json()["id"]
    session = api.db.get(ChatSession, sid)
    attachment_service.bind_to_session(api.db, api.rider.id, sid, [pb, near])
    coach_service.add_user_message(api.db, session, "Compare these two.", [pb, near])
    coach_service.add_assistant_message(api.db, session, "Read both.")
    api.decide(near, "chat_only")

    detail = api.get(f"/api/v1/chat/sessions/{sid}").json()
    users = [m for m in detail["messages"] if m["role"] == "user"]
    assert users[0]["attachment_ids"] == [pb, near]
    assert all(m["attachment_ids"] == [] for m in detail["messages"] if m["role"] != "user")
    cards = {c["id"]: c for c in detail["attachments"]}
    assert list(cards) == [pb, near]
    assert (cards[pb]["view"], cards[pb]["ride"]["id"]) == ("in_history", pb_saved.id)
    assert cards[near]["view"] == "chat_only"


def test_a_batch_is_pointed_to_data_in(api):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("a.gpx", PB_BYTES)
        z.writestr("b.gpx", NEAR_BYTES)
    for name in ("export_123.zip", "misnamed.gpx"):
        r = api.upload(name, archive.getvalue())
        assert r.status_code == 400
        assert "Settings, then Data in, then Ride archive" in r.json()["detail"]

    sid = api.post("/api/v1/chat/sessions", json={}).json()["id"]
    ids = [api.upload(f"r{i}.gpx", _gpx(f"2026-04-0{i + 1}T08:00:00", 300, watts=200)).json()["id"]
           for i in range(4)]
    r = api.post(f"/api/v1/chat/sessions/{sid}/messages",
                 json={"content": "All of these.", "attachment_ids": ids})
    assert r.status_code == 400
    assert r.json()["detail"].startswith("Three files is the limit for one message. For a batch")


# ── What the coach is told ──────────────────────────────────────────────────


def _send(db, user, chat, text, ids=None):
    attachment_service.bind_to_session(db, user.id, chat.id, ids or [])

    async def run():
        return [c async for c in coach_service.stream_response(db, user, chat, text, attachment_ids=ids or [])]

    return asyncio.run(run())


def _files(call) -> dict:
    text = next(b["text"] for b in call["system"] if b["text"].startswith(FILES_HEADING))
    start = text.index("```json\n") + len("```json\n")
    return {f["attachment_id"]: f for f in json.loads(text[start:text.index("\n```", start)])["files"]}


def test_the_files_block_carries_triage_decision_and_label(db_session, gareth, scripted_model):
    saved = _archive(db_session, gareth, PB_BYTES)
    chat = ChatSession(id="s-calobra", user_id=gareth.id, title="Chat")
    db_session.add(chat)
    db_session.commit()
    pb = _attach(db_session, gareth, PB, PB_BYTES)
    near = _attach(db_session, gareth, NEAR, NEAR_BYTES)
    kom = _attach(db_session, gareth, KOM, KOM_BYTES)
    _send(db_session, gareth, chat, "Compare all three.", [pb.id, near.id, kom.id])

    shown = _files(scripted_model.calls[-1])
    assert shown[pb.id]["triage"] == {
        "state": "in_history", "card": "nothing to ask",
        "saved_ride": {"ride_id": saved.id, "title": "Sprint Training", "date": "1 May 2026"},
    }
    # Read from the saved ride, which holds the per-second data.
    assert shown[pb.id]["analysis_source"].startswith("the saved ride")
    assert shown[pb.id]["analysis"]["samples"] == db_session.get(Ride, saved.id).analysis["samples"]
    assert shown[pb.id]["ftp_used"]["source"] == "the FTP saved with the ride in Rides"
    assert shown[near.id]["triage"] == {"state": "new", "card": "asking"}
    assert "triage" not in shown[near.id]["summary"]

    attachment_service.decide(db_session, gareth, near, "chat_only")
    coach_service._execute_tool(
        db_session, gareth, "set_attachment_owner",
        {"attachment_id": kom.id, "owner": "someone_else", "label": "Tom Pidcock\u2019s KOM"},
    )
    _send(db_session, gareth, chat, "And now?")
    shown = _files(scripted_model.calls[-1])
    assert shown[near.id]["triage"] == {"state": "new", "card": "answered", "decision": "chat_only"}
    assert shown[kom.id]["triage"] == {
        "state": "new", "card": "answered", "decision": "someone_else", "label": "Tom Pidcock's KOM",
    }
    assert _card(db_session, gareth, kom)["label"] == "Tom Pidcock\u2019s KOM"

    note = next(b["text"] for b in scripted_model.calls[-1]["system"] if b["text"].startswith(FILES_HEADING))
    for rule in (
        "answer what the rider asked first, then add at most one plain sentence pointing at the card",
        "card answered: decision says what they chose; never ask about it again",
        "read it from analysis, which is that saved ride's own",
        "Compare in absolute terms only",
        "A yes to a question is not that",
    ):
        assert rule in note


def test_a_file_read_before_triage_was_kept_has_no_card_to_point_at(db_session, gareth, scripted_model):
    chat = ChatSession(id="s-old", user_id=gareth.id, title="Chat")
    db_session.add(chat)
    db_session.commit()
    old = _attach(db_session, gareth, NEAR, NEAR_BYTES)
    old.summary = {k: v for k, v in old.summary.items() if k != "triage"}
    db_session.commit()
    _send(db_session, gareth, chat, "Remember this one?", [old.id])
    shown = _files(scripted_model.calls[-1])[old.id]
    assert "triage" not in shown and shown["owner"] == "unknown"
    # Its card, should a message carry it, reads as new and asks.
    assert _card(db_session, gareth, old)["view"] == "asking"


def test_the_memory_pass_keeps_nothing_from_someone_elses_file(db_session, gareth, scripted_model, monkeypatch):
    seen = []
    monkeypatch.setattr("app.services.memory_service.extract_memories", lambda *a, **k: seen.append(k))
    chat = ChatSession(id="s-kom", user_id=gareth.id, title="Chat")
    db_session.add(chat)
    db_session.commit()
    kom = _attach(db_session, gareth, KOM, KOM_BYTES)
    attachment_service.decide(db_session, gareth, kom, "someone_else", "Tom")
    _send(db_session, gareth, chat, "How far off am I?", [kom.id])
    assert seen[-1]["caution"].endswith(
        "Someone else's file is that person's data, used only inside this chat: "
        "record nothing from it at all, about the rider or about the person who rode it."
    )


def test_the_coach_cannot_save_a_stretch_of_a_saved_ride(db_session, gareth):
    _archive(db_session, gareth, _gpx("2026-05-01T07:30:00", 3 * 3600, watts=330, hr=168))
    climb = _attach(db_session, gareth, "climb_only.gpx", PB_BYTES)
    said = coach_service._execute_tool(
        db_session, gareth, "save_attachment_as_ride", {"attachment_id": climb.id, "confirmed": True}
    )
    assert said.startswith("Error: nothing was saved. This file is a stretch of a ride")
    assert db_session.query(Ride).count() == 1


# ── The words ───────────────────────────────────────────────────────────────


def test_every_card_line_is_house_style(db_session, gareth):
    _archive(db_session, gareth, _gpx("2026-05-01T08:10:00", 1800, watts=330, hr=168))
    files = [
        _attach(db_session, gareth, "early.gpx", PB_BYTES),
        _attach(db_session, gareth, NEAR, NEAR_BYTES),
        _attach(db_session, gareth, KOM, KOM_BYTES),
        _attach(db_session, gareth, "route.gpx", _gpx("2026-05-07T08:00:00", 120)),
    ]
    attachment_service.decide(db_session, gareth, files[2], "someone_else")
    attachment_service.decide(db_session, gareth, files[3], "chat_only")
    lines = []
    for card in file_triage_service.cards(db_session, gareth.id, files):
        lines += [card[k] for k in ("recognised", "question", "outcome", "note", "undo") if card[k]]
        lines += [c[k] for c in card["choices"] + card["changes"] for k in ("label", "detail") if c[k]]
    assert len(lines) > 10
    for line in lines:
        assert not any(d in line for d in DASHES), line
        assert "'" not in line, line  # curly apostrophes only
        assert "kicker" not in line.lower()
        # A short line never ends on a lone word.
        assert NBSP in line or " " not in line, line


def test_the_new_words_for_the_coach_and_the_rider_carry_no_dashes():
    from app.services import attachment_service as svc

    words = [
        coach_service._SOMEONE_ELSES_CAUTION, svc.A_ZIP, svc.TOO_MANY_FILES,
        json.dumps(next(t for t in coach_service.COACH_TOOLS
                        if t["name"] == "save_attachment_as_ride")),
    ]
    for text in words:
        assert not any(d in text for d in DASHES), text
        assert "kicker" not in text.lower()
