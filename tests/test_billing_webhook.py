"""A signed Stripe event, through the real handler, into the rider's row."""

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import pytest

from app.config import settings
from app.models.user import User
from app.services import billing_service

SECRET = "whsec_test_forma"


def _signed(payload: dict) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    t = int(time.time())
    sig = hmac.new(SECRET.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={t},v1={sig}"


@pytest.fixture
def wired(db_session, monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x")
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    db_session.close = lambda: None  # handler closes its session; keep ours open
    monkeypatch.setattr("app.database.SessionLocal", lambda: db_session)

    # Offline by default: the handler asks Stripe for the live subscription
    # and falls back to the event's copy when it can't reach it.
    def _offline(*a, **k):
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(billing_service.stripe.Subscription, "retrieve", _offline)
    user = User(email="founder@example.com", hashed_password="x", stripe_customer_id="cus_123")
    db_session.add(user)
    db_session.commit()
    return user


def _event(kind: str, sub: dict) -> dict:
    return {"id": "evt_1", "object": "event", "type": kind, "data": {"object": sub}}


def test_basil_shaped_subscription_sets_status_and_period_end(db_session, wired):
    """2025 API versions carry the period end on the item, not the subscription."""
    end = 1_800_000_000
    sub = {
        "id": "sub_1", "object": "subscription", "customer": "cus_123", "status": "active",
        "items": {"object": "list", "data": [{"id": "si_1", "object": "subscription_item", "current_period_end": end}]},
        "metadata": {"forma_user_id": wired.id},
    }
    body, sig = _signed(_event("customer.subscription.created", sub))
    billing_service.handle_webhook(body, sig)

    db_session.refresh(wired)
    assert wired.subscription_status == "active"
    assert wired.subscription_period_end is not None
    assert wired.subscription_period_end == datetime.fromtimestamp(end, tz=timezone.utc).replace(tzinfo=None)


def test_legacy_shaped_subscription_still_reads_period_end(db_session, wired):
    sub = {"id": "sub_1", "object": "subscription", "customer": "cus_123", "status": "active",
           "current_period_end": 1_800_000_000}
    body, sig = _signed(_event("customer.subscription.updated", sub))
    billing_service.handle_webhook(body, sig)

    db_session.refresh(wired)
    assert wired.subscription_period_end is not None


def test_cancellation_is_recorded(db_session, wired):
    sub = {"id": "sub_1", "object": "subscription", "customer": "cus_123", "status": "canceled",
           "ended_at": 1_800_000_000, "items": {"object": "list", "data": []}}
    body, sig = _signed(_event("customer.subscription.deleted", sub))
    billing_service.handle_webhook(body, sig)

    db_session.refresh(wired)
    assert wired.subscription_status == "canceled"


def test_bad_signature_is_rejected(wired):
    body, _ = _signed(_event("customer.subscription.created", {"customer": "cus_123", "status": "active"}))
    with pytest.raises(ValueError):
        billing_service.handle_webhook(body, "t=1,v1=deadbeef")


def test_unknown_customer_found_by_metadata(db_session, wired):
    sub = {"id": "sub_2", "object": "subscription", "customer": "cus_new", "status": "active",
           "items": {"object": "list", "data": [{"current_period_end": 1_800_000_000}]},
           "metadata": {"forma_user_id": wired.id}}
    body, sig = _signed(_event("customer.subscription.created", sub))
    billing_service.handle_webhook(body, sig)

    db_session.refresh(wired)
    assert wired.subscription_status == "active"


class _Live:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


def test_late_active_event_cannot_revive_a_cancelled_subscription(db_session, wired, monkeypatch):
    """Stripe doesn't promise order. A retried updated(active) arriving after
    the cancellation must not hand back access: the live state wins."""
    live = {"id": "sub_1", "object": "subscription", "customer": "cus_123", "status": "canceled",
            "ended_at": 1_800_000_000, "items": {"object": "list", "data": []}}
    monkeypatch.setattr(billing_service.stripe.Subscription, "retrieve", lambda *a, **k: _Live(live))
    stale = {"id": "sub_1", "object": "subscription", "customer": "cus_123", "status": "active",
             "items": {"object": "list", "data": []}}
    body, sig = _signed(_event("customer.subscription.updated", stale))
    billing_service.handle_webhook(body, sig)

    db_session.refresh(wired)
    assert wired.subscription_status == "canceled"


def test_admin_email_passes_the_paywall_whatever_the_case(wired, monkeypatch):
    monkeypatch.setattr(settings, "require_subscription", True)
    monkeypatch.setattr(settings, "admin_emails", ["Founder@Example.com"])
    assert billing_service.has_access(wired)
