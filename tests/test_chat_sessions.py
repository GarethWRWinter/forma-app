"""The coach page's chat list: ordered like a messenger, labelled in plain words.

16 Oct 2026: Gareth's live conversation (52 messages, the last one minutes
old) sat third in the list, under a September "product knowledge probe", and
the page opened the probe. The list was ordered by when each chat was opened,
and writing in a chat never touched it. The titles beside it read
"# Sa Calobra Sub-30 Preparati..." and "That sounds brilliant, Gareth" with
an em dash after the name.
"""

from datetime import datetime
from types import SimpleNamespace

from app.api.v1.chat import get_chat_session, list_chat_sessions
from app.core import forma_core
from app.models.chat import ChatMessage, ChatRole, ChatSession
from app.models.user import User
from app.services.coach_service import (
    _clean_title,
    _looks_like_title,
    add_assistant_message,
    add_user_message,
    get_sessions,
    maybe_autotitle_session,
)


def _user(db, email="gareth@example.com") -> User:
    user = User(email=email, hashed_password="x", full_name="Gareth Winter")
    db.add(user)
    db.commit()
    return user


def _chat(db, user, title, opened, updated=None, pinned=False, archived=None) -> ChatSession:
    session = ChatSession(
        user_id=user.id, title=title, created_at=opened, updated_at=updated,
        pinned=pinned, archived_at=archived,
    )
    db.add(session)
    db.commit()
    return session


def _say(db, session, at, role=ChatRole.user, content="hello") -> None:
    db.add(ChatMessage(session_id=session.id, role=role, content=content, created_at=at))
    db.commit()


def _incident(db):
    """Gareth's list on the morning of 16 Oct, plus another rider's chat."""
    gareth = _user(db)
    # Opened first, in September, and still the conversation he is having.
    current = _chat(db, gareth, "Sa Calobra Sub-30 Preparation", datetime(2026, 9, 1, 8))
    _say(db, current, datetime(2026, 9, 1, 8, 1))
    _say(db, current, datetime(2026, 9, 1, 8, 2), ChatRole.assistant)
    _say(db, current, datetime(2026, 10, 16, 4, 30))
    # Opened later in September, written in once, named after the fact: its
    # updated_at is newer than the live chat's, which is what the page used
    # to pick the chat to open.
    probe = _chat(db, gareth, "Product knowledge probe", datetime(2026, 9, 20, 10),
                  updated=datetime(2026, 9, 20, 10, 5))
    _say(db, probe, datetime(2026, 9, 20, 10, 1))
    # A "New Chat" opened on 1 Oct and never written in.
    empty = _chat(db, gareth, "Chat - 01 Oct 2026", datetime(2026, 10, 1, 7))

    # Someone else wrote later than anyone; it must change nothing for Gareth.
    other = _user(db, "someone@example.com")
    theirs = _chat(db, other, "Not Gareth's", datetime(2026, 9, 15))
    _say(db, theirs, datetime(2026, 10, 16, 4, 50))
    return gareth, current, probe, empty


def test_the_chat_with_the_latest_message_leads_even_when_opened_first(db_session):
    gareth, current, probe, empty = _incident(db_session)

    rows = get_sessions(db_session, gareth.id)

    # The live chat first; an empty chat counts from when it was opened, so
    # it sits above a probe last written in on 20 Sep.
    assert [s.title for s, _, _ in rows] == [
        "Sa Calobra Sub-30 Preparation", "Chat - 01 Oct 2026", "Product knowledge probe",
    ]
    by_id = {s.id: (at, n) for s, at, n in rows}
    assert by_id[current.id] == (datetime(2026, 10, 16, 4, 30), 3)
    assert by_id[probe.id] == (datetime(2026, 9, 20, 10, 1), 1)
    assert by_id[empty.id] == (datetime(2026, 10, 1, 7), 0)


def test_a_pinned_chat_still_leads(db_session):
    gareth, current, probe, empty = _incident(db_session)
    probe.pinned = True
    db_session.commit()

    rows = get_sessions(db_session, gareth.id)

    assert [s.id for s, _, _ in rows] == [probe.id, current.id, empty.id]


def test_archived_chats_stay_hidden_unless_asked_for(db_session):
    gareth, current, probe, empty = _incident(db_session)
    old = _chat(db_session, gareth, "Old block", datetime(2026, 8, 1),
                archived=datetime(2026, 8, 30))
    _say(db_session, old, datetime(2026, 10, 16, 4, 40))

    assert old.id not in [s.id for s, _, _ in get_sessions(db_session, gareth.id)]
    everything = get_sessions(db_session, gareth.id, include_archived=True)
    assert [s.id for s, _, _ in everything][0] == old.id
    assert len(everything) == 4


def test_writing_in_a_chat_moves_it_to_the_top(db_session):
    gareth, current, probe, empty = _incident(db_session)
    probe.updated_at = datetime(2026, 9, 20, 10, 5)
    db_session.commit()

    message = add_user_message(db_session, probe, "Back to the probe for a moment")
    db_session.refresh(probe)

    # updated_at follows the message, on the database's own clock.
    assert probe.updated_at > datetime(2026, 9, 20, 10, 5)
    assert abs((probe.updated_at - message.created_at).total_seconds()) < 2
    assert get_sessions(db_session, gareth.id)[0][0].id == probe.id

    # The coach's replies move the thread too (voice, stream and sync paths).
    current.updated_at = datetime(2026, 9, 1, 8, 2)
    db_session.commit()
    reply = add_assistant_message(db_session, current, "And we're back on Sa Calobra.")
    db_session.refresh(current)
    assert abs((current.updated_at - reply.created_at).total_seconds()) < 2


def test_the_list_endpoint_carries_the_last_message_time(db_session):
    gareth, current, probe, empty = _incident(db_session)

    listed = list_chat_sessions(include_archived=False, current_user=gareth, db=db_session)

    assert [s.id for s in listed] == [current.id, empty.id, probe.id]
    assert listed[0].last_message_at == datetime(2026, 10, 16, 4, 30)
    assert listed[0].message_count == 3
    assert listed[1].last_message_at == datetime(2026, 10, 1, 7)
    assert listed[1].message_count == 0

    detail = get_chat_session(current.id, limit=60, before=None,
                              current_user=gareth, db=db_session)
    assert detail.last_message_at == datetime(2026, 10, 16, 4, 30)


def test_titles_are_cleaned_to_plain_words():
    assert _clean_title("# Sa Calobra Sub-30 Preparation") == "Sa Calobra Sub-30 Preparation"
    assert _clean_title("**Fuelling** for the `312`") == "Fuelling for the 312"
    assert _clean_title("Threshold \u2014 rebuild") == "Threshold, rebuild"
    assert _clean_title("3\u20134 sweet spot blocks") == "3-4 sweet spot blocks"
    assert _clean_title("\u201cSaddle pain fix\u201d") == "Saddle pain fix"
    assert _clean_title('"Tuesday intervals rethink"') == "Tuesday intervals rethink"
    assert _clean_title("\u2018Saddle pain fix\u2019") == "Saddle pain fix"
    # A quoted phrase inside the label keeps both of its marks.
    assert _clean_title("Sa Calobra 'sub 30'") == "Sa Calobra 'sub 30'"
    assert _clean_title("#1 priority race") == "#1 priority race"
    assert _clean_title("") == ""


def test_the_incident_titles_are_cleaned_or_turned_away():
    assert _looks_like_title(_clean_title("# Sa Calobra Sub-30 Preparation"))
    # Cleaned of its dash, this is still the model talking to the rider.
    assert not _looks_like_title(
        _clean_title("That sounds brilliant, Gareth\u2014let's get started")
    )


def test_auto_title_saves_the_clean_label(db_session, monkeypatch):
    gareth = _user(db_session)
    chat = _chat(db_session, gareth, "Chat - 16 Oct 2026", datetime(2026, 10, 16, 4))
    _say(db_session, chat, datetime(2026, 10, 16, 4, 1), content="Sa Calobra sub 30")
    _say(db_session, chat, datetime(2026, 10, 16, 4, 2), ChatRole.assistant, "Let's build it.")

    reply = SimpleNamespace(content=[SimpleNamespace(type="text", text="# Sa Calobra Sub-30 Preparation")])
    monkeypatch.setattr(forma_core, "call", lambda **kw: reply)

    maybe_autotitle_session(db_session, gareth, chat)

    assert chat.title == "Sa Calobra Sub-30 Preparation"
