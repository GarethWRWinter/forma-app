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


# ── A cancellation that hasn't taken effect yet ─────────────────────────────
# From the dahlia API versions a rider who cancels in the customer portal gets
# cancel_at (a timestamp) while cancel_at_period_end stays false and the
# status stays active until then (found live on 13 Oct 2026). Older payloads,
# and Forma's own renewal stop, still use cancel_at_period_end.

END = 1_800_000_000
CANCEL_AT = END  # the portal cancels at the end of the paid period


def _naive(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def _sub(**kw) -> dict:
    sub = {
        "id": "sub_1", "object": "subscription", "customer": "cus_123", "status": "active",
        "cancel_at": None, "cancel_at_period_end": False,
        "items": {"object": "list", "data": [{"id": "si_1", "current_period_end": END}]},
    }
    sub.update(kw)
    return sub


def _send(kind: str, sub: dict) -> None:
    body, sig = _signed(_event(kind, sub))
    billing_service.handle_webhook(body, sig)


def test_a_portal_cancellation_with_cancel_at_is_stored_and_access_carries_on(db_session, wired):
    _send("customer.subscription.updated", _sub(cancel_at=CANCEL_AT))

    db_session.refresh(wired)
    assert wired.subscription_status == "active"
    assert wired.subscription_cancel_at == _naive(CANCEL_AT)
    assert wired.subscription_cancel_at.tzinfo is None  # naive UTC, like period_end
    assert wired.subscription_period_end == _naive(END)


def test_an_earlier_cancel_at_wins_over_the_period_end(db_session, wired):
    earlier = END - 86_400 * 10
    _send("customer.subscription.updated", _sub(cancel_at=earlier))

    db_session.refresh(wired)
    assert wired.subscription_cancel_at == _naive(earlier)


def test_a_cancel_date_in_a_later_period_still_renews_so_nothing_is_stored(db_session, wired):
    """A custom cancel date after the period end (set in the dashboard or the
    API): Stripe renews at the period end and charges for the days up to it.
    Settings must not say no more will be taken, so nothing is stored until
    that renewal makes it the current period's end."""
    later = END + 86_400 * 30
    _send("customer.subscription.updated", _sub(cancel_at=later))

    db_session.refresh(wired)
    assert wired.subscription_status == "active"
    assert wired.subscription_cancel_at is None
    assert wired.subscription_period_end == _naive(END)

    # Renewed: the new period runs to the cancel date, and it ends there.
    renewed = _sub(cancel_at=later, items={"object": "list", "data": [{"id": "si_1", "current_period_end": later}]})
    _send("customer.subscription.updated", renewed)
    db_session.refresh(wired)
    assert wired.subscription_cancel_at == _naive(later)
    assert wired.subscription_period_end == _naive(later)


def test_the_older_cancel_at_period_end_shape_is_stored_as_the_period_end(db_session, wired):
    _send("customer.subscription.updated", _sub(cancel_at_period_end=True))

    db_session.refresh(wired)
    assert wired.subscription_status == "active"
    assert wired.subscription_cancel_at == _naive(END)


def test_renewing_again_clears_the_end_date(db_session, wired):
    _send("customer.subscription.updated", _sub(cancel_at=CANCEL_AT))
    db_session.refresh(wired)
    assert wired.subscription_cancel_at is not None

    # The rider pressed Renew in the portal: Stripe clears cancel_at.
    _send("customer.subscription.updated", _sub(cancel_at=None))
    db_session.refresh(wired)
    assert wired.subscription_status == "active"
    assert wired.subscription_cancel_at is None
    assert wired.subscription_period_end == _naive(END)


def test_the_cancellation_taking_effect_clears_the_end_date(db_session, wired):
    _send("customer.subscription.updated", _sub(cancel_at=CANCEL_AT))
    db_session.refresh(wired)
    assert wired.subscription_cancel_at is not None

    _send("customer.subscription.deleted", _sub(status="canceled", cancel_at=CANCEL_AT, ended_at=CANCEL_AT))
    db_session.refresh(wired)
    assert wired.subscription_status == "canceled"
    assert wired.subscription_cancel_at is None


def test_the_renewal_helpers_read_both_shapes():
    renewing = _sub()
    portal = _sub(cancel_at=CANCEL_AT)
    legacy = _sub(cancel_at_period_end=True)
    cleared = _sub(cancel_at="")  # how Stripe is told to clear it
    gone = _sub(status="canceled")

    assert billing_service._renews(renewing) and billing_service._ends_at_ts(renewing) is None
    assert billing_service._renews(cleared) and billing_service._ends_at_ts(cleared) is None
    assert not billing_service._renews(portal) and billing_service._ends_at_ts(portal) == CANCEL_AT
    assert not billing_service._renews(legacy) and billing_service._ends_at_ts(legacy) == END
    assert not billing_service._renews(gone)

    # A cancel date in a later period renews once more first.
    later = _sub(cancel_at=END + 86_400 * 30)
    assert billing_service._renews(later) and billing_service._ends_at_ts(later) is None
    assert billing_service._later_cancel_at_ts(later) == END + 86_400 * 30
    # On the period end is the portal's own cancellation: no more payments.
    assert not billing_service._renews(portal) and billing_service._later_cancel_at_ts(portal) is None
    # Both shapes, disagreeing: it ends at whichever comes first.
    mixed = _sub(cancel_at=END + 86_400 * 30, cancel_at_period_end=True)
    assert not billing_service._renews(mixed) and billing_service._ends_at_ts(mixed) == END
    # No period end to compare against: cancel_at is taken at its word.
    bare = _sub(cancel_at=CANCEL_AT, items={"object": "list", "data": []})
    assert not billing_service._renews(bare) and billing_service._ends_at_ts(bare) == CANCEL_AT


# ── GET /billing/status ─────────────────────────────────────────────────────


@pytest.fixture
def status_client():
    """The real app, signed in as a rider held in memory: the status reads
    nothing but the rider's own row."""
    from fastapi.testclient import TestClient

    from app.api.v1.deps import get_current_user
    from app.main import app

    user = User(email="member@example.com", hashed_password="x", stripe_customer_id="cus_9",
                subscription_status="active", subscription_period_end=_naive(END))
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        yield TestClient(app), user
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_status_says_when_a_cancelled_membership_ends(status_client):
    client, user = status_client
    body = client.get("/api/v1/billing/status").json()
    assert body["status"] == "active"
    assert body["ends_at"] is None
    assert body["period_end"] == _naive(END).isoformat()

    user.subscription_cancel_at = _naive(CANCEL_AT)
    body = client.get("/api/v1/billing/status").json()
    assert body["status"] == "active"
    assert body["ends_at"] == _naive(CANCEL_AT).isoformat()
    assert body["has_access"] is True
