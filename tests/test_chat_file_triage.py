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


def _gpx(start: str, seconds: int, watts=None, hr=None, rpm=None, times=True) -> bytes:
    """A climb file, one point a second. Leave watts, hr and rpm out for a
    trace with none of them; watts and hr may be a function of the second.
    times=False writes a route: points with no times at all."""
    t0 = datetime.fromisoformat(start)
    points = []
    for i in range(seconds + 1):
        ts = (t0 + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ext = ""
        w = watts(i) if callable(watts) else watts
        h = hr(i) if callable(hr) else hr
        if w is not None:
            ext += f"<power>{w}</power>"
        tpx = ""
        if h is not None:
            tpx += f"<gpxtpx:hr>{h}</gpxtpx:hr>"
        if rpm is not None:
            tpx += f"<gpxtpx:cad>{rpm}</gpxtpx:cad>"
        if tpx:
            ext += f"<gpxtpx:TrackPointExtension>{tpx}</gpxtpx:TrackPointExtension>"
        points.append(
            f'<trkpt lat="{39.80 + i * 0.00003:.6f}" lon="2.80"><ele>{5 + 0.25 * i:.2f}</ele>'
            + (f"<time>{ts}</time>" if times else "")
            + (f"<extensions>{ext}</extensions>" if ext else "") + "</trkpt>"
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
    assert _words(card["recognised"]) == "Already in your rides: Sprint Training, 1 May 2026."
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
    # Each choice says what it does, so none carries more weight.
    assert [_words(c["detail"]) for c in card["choices"]] == [
        "It joins Rides and your fitness chart for 3 May 2026.",
        "It stays out of Rides and your fitness chart.",
        "For comparison only, and only in this chat.",
    ]
    assert _words(card["recognised"]) == "Nothing in your rides matches it."
    assert _words(card["question"]) == "Whose ride is this?"
    assert near.imported_ride_id is None
    assert attachment_service.file_owner(near) == "unknown"

    kom = _attach(db_session, gareth, KOM, KOM_BYTES)
    card = _card(db_session, gareth, kom)
    assert card["view"] == "asking" and "faster" in card["hints"]
    assert _words(card["recognised"]) == (
        "Its best 20 minutes is 452 W, well above your FTP of 290 W."
    )
    assert _words(card["question"]) == "Yours, or someone else\u2019s?"
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
    assert _choices(card, "changes") == ["someone_else"]
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
    # Nothing is asked: keeping it apart is a quiet option, not a question.
    assert (card["question"], card["choices"]) == (None, [])
    assert _choices(card, "changes") == ["save", "someone_else"]
    assert _words(card["changes"][0]["label"]) == "Keep as a separate ride"
    assert _words(card["changes"][0]["detail"]) == (
        "It joins Rides on its own, so the minutes it shares with that ride count "
        "twice in your fitness chart."
    )
    assert _words(card["recognised"]) == "67% of it overlaps Sprint Training, 1 May 2026, already in your rides."

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
        "Recorded at the same time as Sprint Training, 1 May 2026, but the numbers differ."
    )
    assert _words(card["question"]) == "Yours, or someone else\u2019s?"
    # The time it covers is in Rides already, so there is no save to offer.
    assert _choices(card) == ["chat_only", "someone_else"]
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
     "Its heart rate holds 199 bpm for 30 seconds, above your max of 186 bpm."),
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
        assert r.json()["detail"] == attachment_service.A_ZIP
        assert "Ride archive in Settings, then Data in" in r.json()["detail"]

    sid = api.post("/api/v1/chat/sessions", json={}).json()["id"]
    ids = [api.upload(f"r{i}.gpx", _gpx(f"2026-04-0{i + 1}T08:00:00", 300, watts=200)).json()["id"]
           for i in range(4)]
    r = api.post(f"/api/v1/chat/sessions/{sid}/messages",
                 json={"content": "All of these.", "attachment_ids": ids})
    assert r.status_code == 400
    assert r.json()["detail"].startswith(
        "Three files is the limit for one message. If these are your own rides"
    )


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


def _before_triage(db, a) -> ChatAttachment:
    """An upload as it was stored before triage was kept."""
    a.summary = {k: v for k, v in a.summary.items() if k not in ("triage", "owner")}
    a.imported_ride_id = None
    db.commit()
    return a


def test_a_file_read_before_triage_was_kept_is_recognised_on_first_read(
    db_session, gareth, scripted_model
):
    """Gareth's Sa Calobra chat of 16 Oct was shared before this shipped:
    on its first read the 29:53, already in his rides, is linked and asks
    nothing, and the 30:47 gets its card."""
    saved = _archive(db_session, gareth, PB_BYTES)
    chat = ChatSession(id="s-old", user_id=gareth.id, title="Chat")
    db_session.add(chat)
    db_session.commit()
    pb = _before_triage(db_session, _attach(db_session, gareth, PB, PB_BYTES))
    near = _before_triage(db_session, _attach(db_session, gareth, NEAR, NEAR_BYTES))
    _send(db_session, gareth, chat, "Remember these?", [pb.id, near.id])

    shown = _files(scripted_model.calls[-1])
    assert shown[pb.id]["triage"]["state"] == "in_history"
    assert shown[pb.id]["triage"]["saved_ride"]["ride_id"] == saved.id
    assert shown[near.id]["triage"] == {"state": "new", "card": "asking"}
    assert _card(db_session, gareth, pb)["view"] == "in_history"
    assert _card(db_session, gareth, near)["view"] == "asking"


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
        "record none of its numbers and nothing about the person who rode it. "
        "What the rider says about their own riding or goals still counts as usual."
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
        coach_service._SOMEONE_ELSES_CAUTION, coach_service._CHAT_ONLY_CAUTION,
        svc.A_ZIP, svc.TOO_MANY_FILES, *coach_service._SAVE_REFUSED.values(),
        json.dumps(next(t for t in coach_service.COACH_TOOLS
                        if t["name"] == "save_attachment_as_ride")),
    ]
    for text in words:
        assert not any(d in text for d in DASHES), text
        assert "kicker" not in text.lower()


# ── Review scenarios, 16 Oct 2026 ───────────────────────────────────────────
# Each of these failed before the review fixes: the state a file was given
# at upload was never checked again, a save trusted it, and the coach's save
# skipped what the card guards.


def _fit(start: str, seconds: int, watts: int, hr: int, threshold: int | None = None) -> bytes:
    """A FIT activity, one record a second, with the head unit's threshold."""
    from datetime import timezone

    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.record_message import RecordMessage
    from fit_tool.profile.messages.session_message import SessionMessage
    from fit_tool.profile.profile_type import FileType, Manufacturer

    t0 = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)

    def ms(t):
        return round(t.timestamp() * 1000)

    builder = FitFileBuilder(auto_define=True, min_string_size=50)
    file_id = FileIdMessage()
    file_id.type = FileType.ACTIVITY
    file_id.manufacturer = Manufacturer.DEVELOPMENT.value
    file_id.product = 0
    file_id.time_created = ms(t0)
    file_id.serial_number = 1234
    builder.add(file_id)
    for i in range(seconds + 1):
        record = RecordMessage()
        record.timestamp = ms(t0 + timedelta(seconds=i))
        record.power = watts
        record.heart_rate = hr
        record.position_lat = 39.8 + i * 0.00003
        record.position_long = 2.8
        record.altitude = 5 + 0.25 * i
        record.distance = i * 3.3
        builder.add(record)
    session = SessionMessage()
    session.start_time = ms(t0)
    session.timestamp = ms(t0 + timedelta(seconds=seconds))
    session.total_elapsed_time = seconds
    session.total_timer_time = seconds
    if threshold:
        session.threshold_power = threshold
    builder.add(session)
    return builder.build().to_bytes()


def _chunks(raw: list[str]) -> list[dict]:
    return [json.loads(c[len("data: "):]) for c in raw]


def _chat(db, user, sid="s-review") -> ChatSession:
    chat = ChatSession(id=sid, user_id=user.id, title="Chat")
    db.add(chat)
    db.commit()
    return chat


def _coach_save(db, user, a) -> str:
    return coach_service._execute_tool(
        db, user, "save_attachment_as_ride", {"attachment_id": a.id, "confirmed": True}
    )


def _coach_owner(db, user, a, owner="self") -> str:
    return coach_service._execute_tool(
        db, user, "set_attachment_owner", {"attachment_id": a.id, "owner": owner}
    )


WHOLE_MORNING = _gpx("2026-05-01T07:30:00", 3 * 3600, watts=330, hr=168)


def test_saving_the_whole_ride_turns_its_climb_into_part_of_it(db_session, gareth):
    """Both arrive new. Once the whole morning is saved, the climb's card
    stops asking, and a save of it is refused: the climb's load never
    counts twice."""
    whole = _attach(db_session, gareth, "whole_morning.gpx", WHOLE_MORNING)
    climb = _attach(db_session, gareth, PB, PB_BYTES)
    assert _card(db_session, gareth, climb)["view"] == "asking"

    ride = attachment_service.decide(db_session, gareth, whole, "save")
    card = _card(db_session, gareth, climb)
    assert (card["view"], card["ride"]["id"], card["choices"]) == ("part_of_ride", ride.id, [])
    with pytest.raises(attachment_service.DecisionRefused) as refused:
        attachment_service.decide(db_session, gareth, db_session.get(ChatAttachment, climb.id), "save")
    assert refused.value.code == "part_of_ride"
    assert db_session.query(Ride).count() == 1


def test_a_ride_arriving_after_the_file_is_noticed(db_session, gareth, scripted_model):
    """The archive import, or a Wahoo sync, brings the ride in after the file
    was dropped into chat: undecided or kept for the chat, the card now says
    it is in Rides, and so does the coach."""
    asking = _attach(db_session, gareth, NEAR, NEAR_BYTES)
    kept = _attach(db_session, gareth, PB, PB_BYTES)
    attachment_service.decide(db_session, gareth, kept, "chat_only")
    near_ride = _archive(db_session, gareth, NEAR_BYTES)
    pb_ride = _archive(db_session, gareth, PB_BYTES)

    for a, ride in ((asking, near_ride), (kept, pb_ride)):
        card = _card(db_session, gareth, a)
        assert (card["view"], card["ride"]["id"], card["outcome"]) == ("in_history", ride.id, None)
        assert _words(card["recognised"]).startswith("Already in your rides: Sprint Training")

    chat = _chat(db_session, gareth)
    _send(db_session, gareth, chat, "How do they compare?", [asking.id, kept.id])
    shown = _files(scripted_model.calls[-1])
    assert shown[asking.id]["triage"]["saved_ride"]["ride_id"] == near_ride.id
    assert shown[kept.id]["triage"]["state"] == "in_history"
    assert db_session.query(Ride).count() == 2


def test_a_saved_stretch_inside_a_new_file_is_noticed(db_session, gareth):
    """A 30 minute stretch is in Rides; the whole 3 hour outing arrives.
    The card says so, and what a save would do to the fitness chart, and
    the coach cannot save it from a word in the chat."""
    stretch = _archive(db_session, gareth, PB_BYTES)
    whole = _attach(db_session, gareth, "whole_morning.gpx", WHOLE_MORNING)

    card = _card(db_session, gareth, whole)
    assert card["view"] == "asking" and "holds" in card["hints"]
    assert _words(card["recognised"]) == "It includes Sprint Training, 1 May 2026, already in your rides."
    assert _words(card["choices"][0]["detail"]) == (
        "It joins Rides on its own, so the minutes it shares with that ride count "
        "twice in your fitness chart."
    )
    assert whole.summary["triage"]["hints"]["holds"]["ride_id"] == stretch.id

    _coach_owner(db_session, gareth, whole)
    said = _coach_save(db_session, gareth, whole)
    assert said.startswith("Error: nothing was saved. This file is filed on its card only")
    assert db_session.query(Ride).count() == 1


def test_a_time_match_stays_a_time_match_on_re_upload(db_session, gareth):
    """A different recording of the outing, linked by time, is not the saved
    ride's own bytes: sent again, it is still a time match the rider can
    say is not theirs, and the coach is never told the data is identical."""
    _archive(db_session, gareth, PB_BYTES)
    trace = _gpx("2026-05-01T08:00:00", 1793)
    _attach(db_session, gareth, "trace.gpx", trace)
    again = _attach(db_session, gareth, "trace (1).gpx", trace)
    assert again.summary["triage"]["matched"] == "time"
    assert _choices(_card(db_session, gareth, again), "changes") == ["someone_else"]

    said = _coach_owner(db_session, gareth, again, "someone_else")
    assert "lined up in time" in said and "exactly this data" not in said


def test_a_mates_file_from_the_same_ride_cannot_be_saved_or_called_saved(api):
    """Tom's file, recorded beside Gareth's ride: no save on the card, a
    save refused with the reason, nothing in Rides changed, and no undo line
    that would delete Gareth's own ride."""
    _archive(api.db, api.rider, _gpx("2026-05-01T08:00:00", 1793, watts=250, hr=150))
    tom = api.upload("tom.gpx", PB_BYTES).json()
    assert _choices(tom["card"]) == ["chat_only", "someone_else"]

    r = api.decide(tom["id"], "save")
    assert r.status_code == 409
    assert "covers the same time as a ride already in your rides" in r.json()["detail"]
    a = api.db.get(ChatAttachment, tom["id"])
    assert a.imported_ride_id is None and attachment_service.file_owner(a) == "unknown"
    assert api.db.query(Ride).count() == 1

    _coach_owner(api.db, api.rider, a)
    said = _coach_save(api.db, api.rider, a)
    assert said.startswith("Error: nothing was saved. This file was recorded at the same time")
    assert api.db.query(Ride).count() == 1


def test_a_mate_on_the_same_ride_with_close_numbers_is_not_linked(db_session, gareth):
    """Tom at 300 W and 160 bpm, starting 30 s after Gareth's 330 W and 168
    bpm: close on average, apart second by second."""
    _archive(db_session, gareth, PB_BYTES)
    tom = _attach(db_session, gareth, "tom.gpx", _gpx("2026-05-01T08:00:30", 1793, watts=300, hr=160))
    triage = tom.summary["triage"]
    assert triage["state"] == "new" and "alongside" in triage["hints"]
    assert tom.imported_ride_id is None


def test_saving_a_pros_fit_file_never_sets_the_riders_ftp(db_session, gareth):
    kom = _attach(db_session, gareth, "kom.fit", _fit("2026-04-12T10:00:00", 1500, 452, 178, threshold=430))
    assert "faster" in kom.summary["triage"]["hints"]
    ride = attachment_service.decide(db_session, gareth, kom, "save")
    db_session.refresh(gareth)
    assert gareth.ftp == 290
    # The ride keeps the file's own threshold for its training stress.
    assert ride.ftp_at_time == 430


def test_the_coachs_save_holds_to_the_cards_guards(db_session, gareth):
    """A spoken save of a file that looks like a pro's, or of one two thirds
    inside a saved ride, is refused and pointed at the card."""
    kom = _attach(db_session, gareth, KOM, KOM_BYTES)
    _coach_owner(db_session, gareth, kom)
    assert _coach_save(db_session, gareth, kom).startswith(
        "Error: nothing was saved. This file is filed on its card only"
    )
    assert db_session.query(Ride).count() == 0

    _archive(db_session, gareth, _gpx("2026-05-01T08:10:00", 1800, watts=330, hr=168))
    early = _attach(db_session, gareth, "early.gpx", PB_BYTES)
    assert _coach_save(db_session, gareth, early).startswith(
        "Error: nothing was saved. This file is a stretch of a ride"
    )
    assert db_session.query(Ride).count() == 1


def test_a_linked_file_whose_ride_was_deleted_saves_as_a_fresh_ride(db_session, gareth, scripted_model):
    saved = _archive(db_session, gareth, PB_BYTES)
    pb = _attach(db_session, gareth, PB, PB_BYTES)
    db_session.delete(saved)
    db_session.commit()

    card = _card(db_session, gareth, pb)
    assert card["view"] == "chat_only"
    assert _words(card["outcome"]) == (
        "The ride it matched is no longer in your rides, so it’s just for this chat."
    )
    assert _choices(card, "changes") == ["save", "someone_else"]

    ride = attachment_service.decide(db_session, gareth, db_session.get(ChatAttachment, pb.id), "save")
    card = _card(db_session, gareth, pb)
    assert (card["view"], card["ride"]["id"], _words(card["outcome"])) == ("saved", ride.id, "Saved to Rides.")

    chat = _chat(db_session, gareth)
    _send(db_session, gareth, chat, "Saved?", [pb.id])
    triage = _files(scripted_model.calls[-1])[pb.id]["triage"]
    assert triage["decision"] == "save" and triage["saved_as"]["ride_id"] == ride.id


def test_a_poorer_saved_ride_never_replaces_the_files_own_read(db_session, gareth, scripted_model):
    """The phone's heart rate only recording is in Rides; the head unit's
    file, with power, is shared. The coach reads the file, power curve and
    all, and the summary and analysis describe one recording."""
    _archive(db_session, gareth, _gpx("2026-05-01T08:00:00", 1793, hr=168))
    head_unit = _attach(db_session, gareth, PB, PB_BYTES)
    assert head_unit.summary["triage"]["state"] == "in_history"

    chat = _chat(db_session, gareth)
    _send(db_session, gareth, chat, "Pacing?", [head_unit.id])
    shown = _files(scripted_model.calls[-1])[head_unit.id]
    assert shown["analysis_source"].startswith("this file")
    assert shown["analysis"]["power_curve"]
    assert shown["summary"]["avg_power"] == 330


def test_a_saved_ride_read_in_place_of_the_file_brings_its_own_summary(db_session, gareth, scripted_model):
    saved = _archive(db_session, gareth, PB_BYTES)
    second = _attach(db_session, gareth, "wahoo.gpx", _gpx("2026-05-01T08:00:00", 1793, watts=318, hr=168))
    chat = _chat(db_session, gareth)
    _send(db_session, gareth, chat, "Pacing?", [second.id])
    shown = _files(scripted_model.calls[-1])[second.id]
    assert shown["analysis_source"].startswith("the saved ride")
    assert shown["summary"]["avg_power"] == round(saved.average_power)


def test_a_route_with_no_times_is_never_dated_today(db_session, gareth):
    route = _attach(db_session, gareth, "route.gpx", _gpx("2026-05-01T08:00:00", 2000, times=False))
    assert "duration" not in route.summary
    card = _card(db_session, gareth, route)
    assert (card["duration"], card["duration_s"]) == (None, None)
    assert _choices(card) == ["chat_only", "someone_else"]
    assert _words(card["question"]) == "Yours, or someone else’s?"

    with pytest.raises(attachment_service.DecisionRefused) as refused:
        attachment_service.decide(db_session, gareth, route, "save")
    assert refused.value.code == "no_times"
    with pytest.raises(attachment_service.AttachmentError):
        attachment_service.save_as_ride(db_session, gareth, route)
    assert db_session.query(Ride).count() == 0


def test_two_riders_files_on_the_same_second_are_two_files(db_session, gareth):
    """Same start, same length, same samples: their numbers keep them apart,
    so marking Tom's file never touches Gareth's card."""
    mine = _attach(db_session, gareth, "mine.gpx", PB_BYTES)
    tom = _attach(db_session, gareth, "tom.gpx", _gpx("2026-05-01T08:00:00", 1793, watts=250, hr=150))
    assert attachment_service.same_file_key(mine) != attachment_service.same_file_key(tom)
    attachment_service.decide(db_session, gareth, tom, "someone_else", "Tom")
    assert _card(db_session, gareth, mine)["view"] == "asking"


def test_a_heart_rate_spike_is_not_someone_elses_heart(db_session, gareth):
    spiky = _gpx("2026-05-05T08:00:00", 3600, watts=220,
                 hr=lambda i: 225 if 1800 <= i < 1803 else 148)
    a = _attach(db_session, gareth, "spike.gpx", spiky)
    assert "high_hr" not in a.summary["triage"]["hints"]


def test_a_short_pro_effort_still_wonders_aloud(db_session, gareth):
    kom = _attach(db_session, gareth, "short_kom.gpx", _gpx("2026-04-12T10:00:00", 480, watts=560, hr=178))
    card = _card(db_session, gareth, kom)
    assert _words(card["recognised"]) == "Its best 8 minutes is 560 W, well above your FTP of 290 W."


def test_once_the_rider_says_its_theirs_the_card_keeps_every_choice(db_session, gareth):
    """The coach heard "that's mine" about the KOM file: the card stops
    wondering aloud but keeps Someone else's, so a misread is corrected on
    the card."""
    kom = _attach(db_session, gareth, KOM, KOM_BYTES)
    _coach_owner(db_session, gareth, kom)
    card = _card(db_session, gareth, kom)
    assert _choices(card) == ["save", "chat_only", "someone_else"]
    assert _words(card["recognised"]) == "Nothing in your rides matches it."
    assert _words(card["question"]) == "Should it join your rides?"


def test_a_guessed_owner_is_not_handed_down(db_session, gareth):
    """Linked by time, the file was taken as Gareth's. Its ride deleted, the
    same file sent again is asked about afresh."""
    saved = _archive(db_session, gareth, PB_BYTES)
    _attach(db_session, gareth, PB, PB_BYTES)
    db_session.delete(saved)
    db_session.commit()
    again = _attach(db_session, gareth, PB, PB_BYTES)
    assert attachment_service.file_owner(again) == "unknown"
    assert _choices(_card(db_session, gareth, again)) == ["save", "chat_only", "someone_else"]


def test_identical_bytes_marked_someone_elses_say_the_saved_ride_holds_them(
    db_session, gareth, scripted_model
):
    first = _attach(db_session, gareth, NEAR, NEAR_BYTES)
    ride = attachment_service.decide(db_session, gareth, first, "save")
    again = _attach(db_session, gareth, "renamed.gpx", NEAR_BYTES)
    attachment_service.decide(db_session, gareth, again, "someone_else", "tom's KOM")

    card = _card(db_session, gareth, again)
    assert card["label"] == "Tom’s KOM"
    assert _words(card["outcome"]) == "Tom’s KOM, for comparison only."
    chat = _chat(db_session, gareth)
    _send(db_session, gareth, chat, "Whose is this?", [again.id])
    triage = _files(scripted_model.calls[-1])[again.id]["triage"]
    assert triage["same_data_as"]["ride_id"] == ride.id and "lines_up_with" not in triage


def test_card_dates_are_utc_whatever_offset_the_file_wrote(db_session, gareth):
    offset = _gpx("2026-05-02T01:00:00", 600, watts=250, hr=150).replace(b"Z</time>", b"+02:00</time>")
    a = _attach(db_session, gareth, "late.gpx", offset)
    assert a.summary["start_time"] == "2026-05-01T23:00:00+00:00"
    card = _card(db_session, gareth, a)
    assert card["date"] == "2026-05-01"
    assert _words(card["choices"][0]["detail"]) == "It joins Rides and your fitness chart for 1 May 2026."


def test_the_same_file_twice_in_one_message_shows_one_card(api):
    sid = api.post("/api/v1/chat/sessions", json={}).json()["id"]
    one = api.upload(NEAR, NEAR_BYTES, sid).json()["id"]
    two = api.upload(NEAR.replace(".gpx", " (1).gpx"), NEAR_BYTES, sid).json()["id"]
    session = api.db.get(ChatSession, sid)
    attachment_service.bind_to_session(api.db, api.rider.id, sid, [one, two])
    coach_service.add_user_message(api.db, session, "This one.", [one, two])

    detail = api.get(f"/api/v1/chat/sessions/{sid}").json()
    assert detail["messages"][0]["attachment_ids"] == [one]
    assert [c["id"] for c in detail["attachments"]] == [one]
    first, second = (_card(api.db, api.rider, api.db.get(ChatAttachment, i)) for i in (one, two))
    assert first["file_key"] == second["file_key"]


def test_the_coachs_file_tools_tell_the_app_the_card_changed(db_session, gareth, scripted_model):
    chat = _chat(db_session, gareth)
    near = _attach(db_session, gareth, NEAR, NEAR_BYTES)
    scripted_model.append(([], [
        scripted_model.tool_use("set_attachment_owner", {"attachment_id": near.id, "owner": "self"}),
        scripted_model.tool_use("save_attachment_as_ride", {"attachment_id": near.id, "confirmed": True}),
    ]))
    scripted_model.append((["In."], []))
    chunks = _chunks(_send(db_session, gareth, chat, "Mine, from Sunday. Save it to my rides.", [near.id]))
    events = [c for c in chunks if c["type"] == "files_updated"]
    assert events == [
        {"type": "files_updated", "attachment_ids": [near.id], "saved": False},
        {"type": "files_updated", "attachment_ids": [near.id], "saved": True},
    ]
    assert _card(db_session, gareth, near)["view"] == "saved"


def test_a_save_that_finds_the_ride_in_rides_says_so_to_the_coach(db_session, gareth):
    near = _attach(db_session, gareth, NEAR, NEAR_BYTES)
    _coach_owner(db_session, gareth, near)
    _archive(db_session, gareth, NEAR_BYTES)
    said = _coach_save(db_session, gareth, db_session.get(ChatAttachment, near.id))
    assert said.startswith("Already in the rider's history")
    assert db_session.query(Ride).count() == 1


def test_a_file_without_power_says_the_fitness_chart_will_not_move(db_session, gareth, scripted_model):
    hr_only = _attach(db_session, gareth, "hr_only.gpx", _gpx("2026-05-05T08:00:00", 600, hr=150))
    card = _card(db_session, gareth, hr_only)
    assert _words(card["choices"][0]["detail"]) == (
        "It joins Rides for 5 May 2026. With no power in it, your fitness chart won’t move."
    )
    _coach_owner(db_session, gareth, hr_only)
    said = _coach_save(db_session, gareth, hr_only)
    assert "it has no power, so their training load does not move" in said


def test_someone_elses_file_without_a_name(db_session, gareth):
    kom = _attach(db_session, gareth, KOM, KOM_BYTES)
    attachment_service.decide(db_session, gareth, kom, "someone_else")
    assert _words(_card(db_session, gareth, kom)["outcome"]) == "Someone else’s ride, for comparison only."


def test_just_for_this_chat_keeps_its_numbers_out_of_memory(db_session, gareth, scripted_model, monkeypatch):
    seen = []
    monkeypatch.setattr("app.services.memory_service.extract_memories", lambda *a, **k: seen.append(k))
    chat = _chat(db_session, gareth)
    near = _attach(db_session, gareth, NEAR, NEAR_BYTES)
    attachment_service.decide(db_session, gareth, near, "chat_only")
    _send(db_session, gareth, chat, "How did I pace it?", [near.id])
    assert seen[-1]["caution"] == (
        "These ride files are the rider's own and kept just for this chat, named here "
        f"by file name: “{NEAR}”. Record none of their numbers. What the rider "
        "says in their own words still counts as usual."
    )


def test_the_files_note_retires_the_attachment_laws_question(db_session, gareth, scripted_model):
    chat = _chat(db_session, gareth)
    near = _attach(db_session, gareth, NEAR, NEAR_BYTES)
    _send(db_session, gareth, chat, "Here’s a ride file.", [near.id])
    note = next(
        b["text"] for b in scripted_model.calls[-1]["system"] if b["text"].startswith(FILES_HEADING)
    )
    for rule in (
        "THE ATTACHMENT LAW applies to them with one change, which overrides it",
        "Its question, and its rule to save only after the rider has agreed in reply to you, are retired",
        "which sits under their message, above your reply (never say below)",
        "say in one sentence the one thing worth knowing about the file",
        "never as you or yours",
        "Heart rate is personal",
    ):
        assert rule in note
    assert not any(d in note for d in DASHES)


def test_a_file_saved_before_triage_was_kept_still_reads_as_saved(db_session, gareth):
    """Saved from chat before triage, its first read finds the ride it was
    saved as, which is its own save: Saved to Rides, not a match."""
    near = _attach(db_session, gareth, NEAR, NEAR_BYTES)
    ride = attachment_service.decide(db_session, gareth, near, "save")
    near = db_session.get(ChatAttachment, near.id)
    near.summary = {k: v for k, v in near.summary.items() if k not in ("triage", "sha256")}
    db_session.commit()
    card = _card(db_session, gareth, near)
    assert (card["view"], card["ride"]["id"]) == ("saved", ride.id)
