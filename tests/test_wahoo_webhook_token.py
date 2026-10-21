"""The Wahoo webhook token check ignores stray whitespace on either copy.

A copied line break once made the two copies differ and blocked the live
webhook for four days (11 Oct 2026)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import wahoo
from app.config import settings


@pytest.fixture
def client(monkeypatch):
    async def _no_import(payload):
        return None

    monkeypatch.setattr(wahoo.wahoo_service, "handle_webhook_event", _no_import)
    app = FastAPI()
    app.include_router(wahoo.router)
    return TestClient(app)


@pytest.mark.parametrize("stored, sent, status", [
    ("tok-123", "tok-123", 200),
    ("tok-123\n", "tok-123", 200),     # line break pasted into Railway
    ("tok-123", " tok-123\n", 200),    # whitespace from Wahoo's side
    ("tok-123", "tok-124", 403),
    ("tok-123", "", 403),
])
def test_token_check(client, monkeypatch, stored, sent, status):
    monkeypatch.setattr(settings, "wahoo_webhook_token", stored)
    resp = client.post(
        "/integrations/wahoo/webhook",
        json={"event_type": "workout_summary", "webhook_token": sent},
    )
    assert resp.status_code == status
