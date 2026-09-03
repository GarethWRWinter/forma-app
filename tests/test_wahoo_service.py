"""Wahoo token handling.

Wahoo caps an app at ten unrevoked tokens per rider, and only revokes the
previous token when the new one is first used for an API call (a CDN file
download does not count). These tests pin the behaviours that keep Forma
under that cap and stop a full cap being mistaken for a dead credential.
"""

import asyncio
from datetime import datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.integration import WahooToken
from app.models.user import User
from app.services import email_service, wahoo_service

CAP_BODY = {
    "error": (
        "Too many unrevoked access tokens exist for this app and user. You can "
        "only create a new token if you revoke an old one first."
    ),
    "error_description": "The authorization server refused the request.",
}
DEAD_BODY = {"error": "invalid_grant", "error_description": "The grant is invalid."}
MINTED = {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 7200}


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setattr(settings, "token_encryption_key", Fernet.generate_key().decode())


class FakeWahoo:
    """Scripted Wahoo API: records every request, answers by path."""

    def __init__(self, token_status=200, token_body=None):
        self.requests: list[httpx.Request] = []
        self.token_status = token_status
        self.token_body = MINTED if token_body is None else token_body

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/oauth/token":
            return httpx.Response(self.token_status, json=self.token_body)
        if path == "/v1/user":
            return httpx.Response(200, json={"id": 4242})
        if path == "/v1/workouts":
            return httpx.Response(200, json={"workouts": []})
        return httpx.Response(404)

    def install(self, monkeypatch):
        real = httpx.AsyncClient

        def factory(**kwargs):
            kwargs.pop("timeout", None)
            return real(transport=httpx.MockTransport(self.handler), **kwargs)

        monkeypatch.setattr(wahoo_service.httpx, "AsyncClient", factory)
        return self


@pytest.fixture
def notified(monkeypatch):
    calls = []

    async def record(to, name=None, reason=None):
        calls.append({"to": to, "name": name, "reason": reason})
        return True

    monkeypatch.setattr(email_service, "send_wahoo_disconnected", record)
    return calls


def _rider(db) -> User:
    user = User(email="rider@example.com", hashed_password="x", full_name="Test Rider")
    db.add(user)
    db.commit()
    return user


def _token(db, user, *, expired=True, needs_reauth=False, reason=None) -> WahooToken:
    delta = -timedelta(minutes=1) if expired else timedelta(hours=1)
    token = WahooToken(
        user_id=user.id,
        access_token="old-access",
        refresh_token="old-refresh",
        expires_at=datetime.utcnow() + delta,
        needs_reauth=needs_reauth,
        reauth_reason=reason,
        wahoo_user_id=4242,
    )
    db.add(token)
    db.commit()
    return token


def test_fresh_token_is_reused_without_touching_wahoo(db_session, monkeypatch):
    wahoo = FakeWahoo().install(monkeypatch)
    token = _token(db_session, _rider(db_session), expired=False)

    assert asyncio.run(wahoo_service._access_token(db_session, token)) == "old-access"
    assert wahoo.requests == []


def test_refresh_is_followed_by_an_api_call_with_the_new_token(db_session, monkeypatch):
    """Wahoo revokes the previous token only when the new one is used. Without
    this call the webhook path (refresh, then a CDN download) left one orphan
    per ride and hit the cap in a fortnight."""
    wahoo = FakeWahoo().install(monkeypatch)
    token = _token(db_session, _rider(db_session), needs_reauth=True, reason="refresh_rejected")

    access = asyncio.run(wahoo_service._access_token(db_session, token))

    assert access == "new-access"
    assert [(r.method, r.url.path) for r in wahoo.requests] == [
        ("POST", "/oauth/token"),
        ("GET", "/v1/user"),
    ]
    assert b"refresh_token=old-refresh" in wahoo.requests[0].content
    assert wahoo.requests[1].headers["Authorization"] == "Bearer new-access"
    assert token.refresh_token == "new-refresh"
    # A refresh that worked is proof the credential is alive.
    assert token.needs_reauth is False
    assert token.reauth_reason is None


def test_dead_refresh_token_is_recorded_and_the_rider_told_once(
    db_session, monkeypatch, notified
):
    FakeWahoo(400, DEAD_BODY).install(monkeypatch)
    token = _token(db_session, _rider(db_session))

    with pytest.raises(wahoo_service.WahooReauthRequired):
        asyncio.run(wahoo_service._access_token(db_session, token))
    with pytest.raises(wahoo_service.WahooReauthRequired):
        asyncio.run(wahoo_service._access_token(db_session, token))

    assert token.needs_reauth is True
    assert token.reauth_reason == "refresh_rejected"
    assert len(notified) == 1
    assert notified[0]["to"] == "rider@example.com"
    assert notified[0]["reason"] == "refresh_rejected"


def test_token_cap_is_not_mistaken_for_a_dead_credential(db_session, monkeypatch, notified):
    FakeWahoo(400, CAP_BODY).install(monkeypatch)
    token = _token(db_session, _rider(db_session))

    with pytest.raises(wahoo_service.WahooReauthRequired):
        asyncio.run(wahoo_service._access_token(db_session, token))

    assert token.reauth_reason == "token_cap"
    assert notified[0]["reason"] == "token_cap"


def test_reconnect_against_a_full_cap_marks_the_row_and_raises(db_session, monkeypatch):
    """The rider did what Reconnect asked and Wahoo refused. Settings has to
    say so, which means the existing row has to carry the reason."""
    FakeWahoo(400, CAP_BODY).install(monkeypatch)
    user = _rider(db_session)
    prior = _token(db_session, user, needs_reauth=True, reason="refresh_rejected")

    with pytest.raises(wahoo_service.WahooTokenCapReached):
        asyncio.run(wahoo_service.exchange_code(db_session, user.id, "auth-code"))

    assert prior.needs_reauth is True
    assert prior.reauth_reason == "token_cap"


def test_successful_reconnect_clears_the_reason(db_session, monkeypatch):
    FakeWahoo().install(monkeypatch)
    user = _rider(db_session)
    _token(db_session, user, needs_reauth=True, reason="token_cap")

    token = asyncio.run(wahoo_service.exchange_code(db_session, user.id, "auth-code"))

    assert token.access_token == "new-access"
    assert token.needs_reauth is False
    assert token.reauth_reason is None
    assert wahoo_service.get_connection_status(db_session, user.id)["reauth_reason"] is None


def test_status_carries_the_reason(db_session, monkeypatch):
    user = _rider(db_session)
    _token(db_session, user, needs_reauth=True, reason="token_cap")

    status = wahoo_service.get_connection_status(db_session, user.id)

    assert status["needs_reauth"] is True
    assert status["reauth_reason"] == "token_cap"


def test_cap_email_tells_the_rider_to_deauthorise_first(monkeypatch):
    sent = []

    async def capture(to, subject, text_body, from_address=None):
        sent.append(text_body)
        return True

    monkeypatch.setattr(email_service, "send", capture)

    asyncio.run(email_service.send_wahoo_disconnected("r@example.com", "Neil", reason="token_cap"))
    asyncio.run(email_service.send_wahoo_disconnected("r@example.com", "Neil"))

    assert "Authorized Apps" in sent[0] and "Deauthorize" in sent[0]
    assert "Authorized Apps" not in sent[1] and "Reconnect on the Wahoo card" in sent[1]
    for body in sent:
        assert "—" not in body and "–" not in body


def test_reconnect_refusal_is_a_warning_not_an_error(db_session, monkeypatch, caplog):
    """A rider hitting the cap on Reconnect is a handled state with its own
    instructions in Settings. It must not page anyone."""
    FakeWahoo(400, CAP_BODY).install(monkeypatch)
    user = _rider(db_session)
    _token(db_session, user, needs_reauth=True, reason="refresh_rejected")

    with caplog.at_level("WARNING", logger="app.services.wahoo_service"):
        with pytest.raises(wahoo_service.WahooTokenCapReached):
            asyncio.run(wahoo_service.exchange_code(db_session, user.id, "auth-code"))

    levels = {r.levelname for r in caplog.records if r.name == "app.services.wahoo_service"}
    assert "WARNING" in levels
    assert "ERROR" not in levels


def test_cap_during_refresh_stays_an_error(db_session, monkeypatch, caplog, notified):
    """On the refresh path the cap means our prevention failed. That one must
    reach Sentry."""
    FakeWahoo(400, CAP_BODY).install(monkeypatch)
    token = _token(db_session, _rider(db_session))

    with caplog.at_level("WARNING", logger="app.services.wahoo_service"):
        with pytest.raises(wahoo_service.WahooReauthRequired):
            asyncio.run(wahoo_service._access_token(db_session, token))

    assert any(
        r.levelname == "ERROR" and "refresh rejected" in r.getMessage()
        for r in caplog.records
    )
