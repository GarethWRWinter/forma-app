"""Registration rules: the launch audit (4 Oct 2026) and the safety work
(plan section B): who can join, the consent record, re-acceptance when the
terms change, and the sign-up email that is the rider's durable record."""

import hashlib
import logging
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import config as app_config
from app.api.v1 import auth
from app.config import (
    EU_MEMBER_STATES,
    LEGAL_PUBLISHED_DIR,
    MISSING_LEGAL_TEXT,
    Settings,
    published_legal_doc,
    settings,
)
from app.core.security import verify_email_token
from app.models.base import Base
from app.models.invite import InviteCode
from app.models.safety import ConsentEvent
from app.models.user import User
from app.schemas.user import UserCreate, UserLogin, UserResponse
from app.services import email_service

TODAY = date(2026, 10, 8)
TERMS_BOX = (
    "I agree to Forma's terms. I understand the coaching is written by AI and "
    "can be wrong, that it isn't medical advice, and that I decide what I ride "
    "and stop if something feels wrong."
)
HEALTH_BOX = (
    "Forma can use the health details I share with it, such as injuries, "
    "illness, medication, sleep and my answers to its health questions, to "
    "coach me. The privacy policy explains how to withdraw this."
)


def test_email_is_lower_cased_at_register_and_login():
    """A phone that capitalised the first letter used to lock a rider out of
    the account they registered in lower case."""
    reg = UserCreate(email="Rider.Name@Gmail.COM", password="x" * 8, health_consent=True)
    login = UserLogin(email=" rider.name@gmail.com ", password="x" * 8)
    assert reg.email == login.email == "rider.name@gmail.com"


def test_health_consent_defaults_to_not_given():
    assert UserCreate(email="a@b.com", password="x" * 8).health_consent is False


def test_terms_box_defaults_to_not_ticked():
    assert UserCreate(email="a@b.com", password="x" * 8).terms_accepted is False


def test_blank_date_of_birth_is_missing_not_malformed():
    assert UserCreate(email="a@b.com", password="x" * 8, date_of_birth="").date_of_birth is None


def test_bad_email_still_rejected():
    with pytest.raises(ValidationError):
        UserCreate(email="not-an-email", password="x" * 8)


# === Age ===


def test_age_turns_over_on_the_birthday_itself():
    assert auth.age_on(date(2008, 10, 9), TODAY) == 17
    assert auth.age_on(date(2008, 10, 8), TODAY) == 18
    assert auth.age_on(date(2008, 10, 7), TODAY) == 18


def test_leap_day_birthday_turns_over_on_the_first_of_march():
    born = date(2008, 2, 29)
    assert auth.age_on(born, date(2026, 2, 28)) == 17
    assert auth.age_on(born, date(2026, 3, 1)) == 18
    assert auth.age_on(born, date(2028, 2, 29)) == 20


def test_terms_current_follows_the_settings_version():
    base = dict(id="u", email="a@b.com")
    assert UserResponse(**base, terms_version=settings.terms_version).terms_current is True
    assert UserResponse(**base, terms_version="terms-2026-01-01").terms_current is False
    assert UserResponse(**base).terms_current is False


# === The endpoints ===


@pytest.fixture
def api(monkeypatch):
    """The auth and profile endpoints over a private SQLite database, with
    the rate limit off, the server's date fixed, the invite door open, and
    every email captured instead of sent."""
    from app.api.v1.deps import get_current_user
    from app.database import get_db
    from app.main import app

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    sent = []

    async def fake_send(to, subject, text_body, from_address=None):
        sent.append({"to": to, "subject": subject, "body": text_body})
        return True

    monkeypatch.setattr(email_service, "send", fake_send)
    monkeypatch.setattr(auth, "_today", lambda: TODAY)
    monkeypatch.setattr(settings, "require_invite", False)

    me = {}
    # The per-email and invite-guess limits live in the module, across tests.
    shared_limits = (auth._register_email_limit, auth._invite_failures, auth._invite_surge)
    for limit in shared_limits:
        limit.reset()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[auth._register_limit] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: db.get(User, me["id"])
    try:
        yield TestClient(app), db, sent, me
    finally:
        for dep in (get_db, auth._register_limit, get_current_user):
            app.dependency_overrides.pop(dep, None)
        for limit in shared_limits:
            limit.reset()
        db.close()


def _form(**kw) -> dict:
    form = {
        "email": "rider@example.com",
        "password": "a-long-password",
        "full_name": "Sam Rider",
        "date_of_birth": "1990-05-17",
        "country": "GB",
        "terms_accepted": True,
        "health_consent": True,
        "terms_text_shown": TERMS_BOX,
        "health_text_shown": HEALTH_BOX,
    }
    form.update(kw)
    return form


def _refused(client, db, sent, form, status=400) -> str:
    """Post a form that must be turned away, and prove nothing was written."""
    r = client.post("/api/v1/auth/register", json=form)
    assert r.status_code == status, r.text
    assert db.query(User).count() == 0
    assert db.query(ConsentEvent).count() == 0
    assert sent == []
    return r.json()["detail"]


def test_register_stores_eligibility_and_consent(api):
    client, db, sent, _ = api
    r = client.post(
        "/api/v1/auth/register",
        json=_form(country=" gb "),
        headers={"x-forwarded-for": "1.1.1.1, 81.2.69.160", "user-agent": "FormaTest/1.0"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["terms_current"] is True
    assert body["terms_version"] == settings.terms_version
    assert body["country"] == "GB"

    user = db.query(User).one()
    assert user.date_of_birth == date(1990, 5, 17)
    assert user.country == "GB"
    assert user.terms_version == settings.terms_version
    assert user.terms_accepted_at is not None
    assert user.health_consent_at == user.terms_accepted_at

    rows = {e.kind: e for e in db.query(ConsentEvent).all()}
    assert set(rows) == {"terms", "health_data", "age"}
    assert rows["terms"].text_shown == TERMS_BOX
    assert rows["health_data"].text_shown == HEALTH_BOX
    assert rows["age"].text_shown == "Date of birth given: 18 or over"
    for e in rows.values():
        assert e.user_id == user.id
        assert e.source == "register"
        assert e.ip == "81.2.69.160"
        assert e.user_agent == "FormaTest/1.0"
    # Each row names the exact published text: version, then the first 12
    # hex of the file's SHA-256. The health box is the privacy policy's.
    assert rows["terms"].doc_version == _stamp(settings.terms_version)
    assert rows["age"].doc_version == _stamp(settings.terms_version)
    assert rows["health_data"].doc_version == _stamp(settings.privacy_version)


# Review R16: through the Vercel rewrite the last hop is Vercel's own address,
# so a consent row has to take the first, read the way the rate limiter reads
# it (safety_service.client_ip delegates to app.core.ratelimit.client_ip).
def test_a_consent_row_through_vercel_records_the_riders_address(api):
    client, db, sent, _ = api
    r = client.post(
        "/api/v1/auth/register",
        json=_form(),
        headers={"x-vercel-id": "lhr1::abc-1", "x-forwarded-for": "81.2.69.160, 76.76.21.21"},
    )
    assert r.status_code == 201, r.text
    assert {e.ip for e in db.query(ConsentEvent).all()} == {"81.2.69.160"}


def test_eighteenth_birthday_is_old_enough(api):
    client, db, sent, _ = api
    r = client.post("/api/v1/auth/register", json=_form(date_of_birth="2008-10-08"))
    assert r.status_code == 201, r.text


def test_the_day_before_the_eighteenth_birthday_is_not(api):
    client, db, sent, _ = api
    detail = _refused(client, db, sent, _form(date_of_birth="2008-10-09"))
    assert detail == auth.UNDER_18
    assert "British Cycling club" in detail and "youth coach" in detail


@pytest.mark.parametrize("born", ["2026-10-09", "1890-01-01"])
def test_impossible_dates_of_birth_are_refused(api, born):
    client, db, sent, _ = api
    assert _refused(client, db, sent, _form(date_of_birth=born)) == auth.BAD_DATE_OF_BIRTH


@pytest.mark.parametrize("missing", [None, ""])
def test_date_of_birth_is_required(api, missing):
    client, db, sent, _ = api
    assert _refused(client, db, sent, _form(date_of_birth=missing)) == auth.NO_DATE_OF_BIRTH


@pytest.mark.parametrize("code", ["US", "CA", "us", " ca "])
def test_us_and_canada_are_blocked(api, code):
    client, db, sent, _ = api
    detail = _refused(client, db, sent, _form(country=code))
    assert detail.startswith("Forma isn't available in the US or Canada yet.")


def test_allowlist_comes_from_settings(api, monkeypatch):
    client, db, sent, _ = api
    monkeypatch.setattr(settings, "allowed_countries", ["GB"])
    detail = _refused(client, db, sent, _form(country="FR"))
    assert detail.startswith("Forma isn't available in your country yet.")


# Review new problem 4: the terms say the UK only until an insurer confirms
# EU cover in writing, so the server mustn't let anyone else in.
def test_the_allowlist_is_the_uk_only_matching_the_terms():
    assert settings.allowed_countries == ["GB"]
    assert Settings().allowed_countries == ["GB"]
    assert auth.check_country("gb") == "GB"
    terms = (LEGAL_PUBLISHED_DIR / f"{settings.terms_version}.md").read_text(encoding="utf-8")
    assert "available to people who live in the United Kingdom" in terms


@pytest.mark.parametrize("code", sorted(EU_MEMBER_STATES))
def test_eu_countries_are_refused_until_the_terms_cover_them(api, code):
    client, db, sent, _ = api
    detail = _refused(client, db, sent, _form(country=code))
    assert detail == auth._not_available(code)


@pytest.mark.parametrize("raw, expected", [
    ("GB,IE", ["GB", "IE"]),
    (" gb , ie ,fr", ["GB", "IE", "FR"]),
    ("UK,EL", ["GB", "GR"]),
    ("GB,GB,uk", ["GB"]),
    ('["GB", "IE"]', ["GB", "IE"]),
    ("GB,Ireland,I3", ["GB"]),
    ("", ["GB"]),
    (" , ", ["GB"]),
])
def test_allowed_countries_comes_from_the_environment(monkeypatch, raw, expected):
    monkeypatch.setenv("ALLOWED_COUNTRIES", raw)
    assert Settings().allowed_countries == expected


def test_a_country_set_in_the_environment_can_join(api, monkeypatch):
    client, db, sent, _ = api
    monkeypatch.setattr(settings, "allowed_countries", Settings(allowed_countries="GB,IE").allowed_countries)
    assert client.get("/api/v1/auth/config").json()["allowed_countries"] == ["GB", "IE"]
    r = client.post("/api/v1/auth/register", json=_form(country="ie"))
    assert r.status_code == 201 and r.json()["country"] == "IE"


# Review finding 11: the server used to take any two letters but US and CA.
@pytest.mark.parametrize("code", ["PR", "GU", "VI", "AS", "MP", "AU", "ZZ", "CH", "NO", "IS"])
def test_anywhere_off_the_allowlist_is_refused(api, code):
    client, db, sent, _ = api
    detail = _refused(client, db, sent, _form(country=code))
    assert detail == auth._not_available(code)
    assert detail.startswith("Forma isn't available in your country yet.")


def test_greece_by_its_eu_code_is_read_as_gr(api, monkeypatch):
    client, db, sent, _ = api
    monkeypatch.setattr(settings, "allowed_countries", ["GB", "GR"])
    assert client.post("/api/v1/auth/register", json=_form(country="el")).json()["country"] == "GR"


def test_auth_config_hands_the_allowlist_to_the_sign_up_page(api):
    client, db, sent, _ = api
    body = client.get("/api/v1/auth/config").json()
    assert body == {"invite_required": False, "allowed_countries": ["GB"]}


def test_uk_is_read_as_gb(api):
    client, db, sent, _ = api
    assert client.post("/api/v1/auth/register", json=_form(country="UK")).json()["country"] == "GB"


@pytest.mark.parametrize("bad, message", [
    (None, auth.NO_COUNTRY),
    ("", auth.NO_COUNTRY),
    ("United Kingdom", auth.BAD_COUNTRY),
    ("G1", auth.BAD_COUNTRY),
])
def test_country_is_required_and_must_be_a_code(api, bad, message):
    client, db, sent, _ = api
    assert _refused(client, db, sent, _form(country=bad)) == message


def test_terms_box_is_required(api):
    client, db, sent, _ = api
    assert _refused(client, db, sent, _form(terms_accepted=False)) == auth.TERMS_UNTICKED


def test_health_box_is_required(api):
    client, db, sent, _ = api
    assert _refused(client, db, sent, _form(health_consent=False)) == auth.HEALTH_UNTICKED


@pytest.mark.parametrize("field", ["terms_text_shown", "health_text_shown"])
def test_a_form_without_the_box_text_is_stale(api, field):
    client, db, sent, _ = api
    assert _refused(client, db, sent, _form(**{field: "  "})) == auth.STALE_FORM


def test_ineligible_signup_does_not_spend_the_invite(api, monkeypatch):
    client, db, sent, _ = api
    monkeypatch.setattr(settings, "require_invite", True)
    db.add(InviteCode(code="RIDE01", max_uses=1))
    db.commit()
    _refused(client, db, sent, _form(invite_code="ride01", date_of_birth="2010-01-01"))
    db.expire_all()
    assert db.query(InviteCode).one().uses == 0


def test_messages_are_house_style():
    """British English, no dashes of any kind, no exclamation marks."""
    for text in (
        auth.STALE_FORM, auth.NO_DATE_OF_BIRTH, auth.BAD_DATE_OF_BIRTH, auth.UNDER_18,
        auth.NO_COUNTRY, auth.BAD_COUNTRY, auth.TERMS_UNTICKED, auth.HEALTH_UNTICKED,
        auth._not_available("US"), auth._not_available("FR"),
        email_service.SAFETY_PANEL_TEXT, email_service.REFUND_TEXT,
        auth.LEGAL_DOC_UNAVAILABLE, MISSING_LEGAL_TEXT,
        auth.LOGIN_EMAIL_LIMITED, auth.RESET_EMAIL_LIMITED,
        auth.REGISTER_EMAIL_LIMITED, auth.INVITE_GUESSES_LIMITED,
    ):
        assert "—" not in text and "–" not in text and "!" not in text
        assert " - " not in text
        assert "kicker" not in text.lower()


# === The sign-up email ===


def test_signup_sends_one_email_with_the_link_and_the_record(api):
    client, db, sent, _ = api
    r = client.post("/api/v1/auth/register", json=_form())
    assert r.status_code == 201, r.text
    assert len(sent) == 1
    mail = sent[0]
    assert mail["to"] == "rider@example.com"
    assert mail["subject"] == "Welcome to Forma: confirm your email"
    body = mail["body"]

    # The confirm link still works.
    token = body.split("/verify-email?token=")[1].split()[0]
    assert verify_email_token(token, "verify") == r.json()["id"]

    # The durable record.
    assert email_service.SAFETY_PANEL_TEXT in body
    assert (
        "cancel within 14 days of first subscribing, email gareth@ridewithforma.com, "
        "and I'll refund that first payment in full"
    ) in body
    assert "https://ridewithforma.com/privacy" in body
    assert f"version {settings.terms_version}" in body
    assert f"privacy policy version {settings.privacy_version}" in body
    assert "Please keep this email" in body
    assert "—" not in body and "–" not in body and "!" not in body

    # Review finding 13: the durable record carries the full terms, read
    # from the published file, not a link to a page that 404s.
    terms = (LEGAL_PUBLISHED_DIR / f"{settings.terms_version}.md").read_text(encoding="utf-8")
    appendix = body.split(f"The terms you accepted, version {settings.terms_version}\n\n", 1)[1]
    assert appendix.strip() == email_service.plain_text(terms)
    for line in terms.splitlines():
        if line.startswith("## "):
            assert f"\n{line[3:]}\n" in appendix, line
    assert "**" not in appendix and "\n#" not in appendix
    # Every word of the terms is there, in order.
    words = lambda text: text.replace("**", "").replace("#", "").split()
    assert words(appendix) == words(terms)


def test_resend_verification_still_sends_the_short_email(api):
    client, db, sent, me = api
    me["id"] = client.post("/api/v1/auth/register", json=_form()).json()["id"]
    sent.clear()
    r = client.post("/api/v1/auth/resend-verification")
    assert r.status_code == 200 and r.json() == {"status": "sent"}
    assert sent[0]["subject"] == "One click to confirm your email"
    assert "/verify-email?token=" in sent[0]["body"]


# === Re-acceptance ===


def _beta_rider(db, **kw) -> User:
    user = User(email="beta@example.com", hashed_password="x", **kw)
    db.add(user)
    db.commit()
    return user


def test_beta_account_without_a_record_is_not_current(api):
    client, db, sent, me = api
    me["id"] = _beta_rider(db).id
    body = client.get("/api/v1/users/me").json()
    assert body["terms_current"] is False and body["terms_version"] is None


def test_reaccept_brings_the_rider_up_to_date(api):
    client, db, sent, me = api
    me["id"] = _beta_rider(
        db, terms_version="terms-2026-01-01", date_of_birth=date(1990, 5, 17), country="GB"
    ).id
    body = client.get("/api/v1/users/me").json()
    assert body["terms_current"] is False and body["needs_profile_consent"] is False

    r = client.post(
        "/api/v1/auth/reaccept-terms",
        json={"text_shown": TERMS_BOX},
        headers={"x-forwarded-for": "81.2.69.160"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "terms_version": settings.terms_version}

    me_now = client.get("/api/v1/users/me").json()
    assert me_now["terms_current"] is True and me_now["terms_version"] == settings.terms_version
    event = db.query(ConsentEvent).one()
    assert (event.kind, event.source, event.doc_version, event.text_shown, event.ip) == (
        "reaccept", "reaccept", _stamp(settings.terms_version), TERMS_BOX, "81.2.69.160"
    )
    assert db.get(User, me["id"]).terms_accepted_at is not None


# === Beta accounts with no date of birth or country (review new problem 11) ===


def _reaccept(client, **kw):
    return client.post(
        "/api/v1/auth/reaccept-terms",
        json={"text_shown": TERMS_BOX, **kw},
        headers={"x-forwarded-for": "81.2.69.160"},
    )


def _nothing_recorded(db, me):
    db.expire_all()
    user = db.get(User, me["id"])
    assert db.query(ConsentEvent).count() == 0
    assert user.terms_version is None and user.terms_accepted_at is None
    assert user.date_of_birth is None and user.country is None


def test_a_beta_account_with_no_age_or_country_needs_profile_consent(api):
    client, db, sent, me = api
    me["id"] = _beta_rider(db).id
    assert client.get("/api/v1/users/me").json()["needs_profile_consent"] is True
    for kw in ({"date_of_birth": date(1990, 5, 17)}, {"country": "GB"}):
        user = db.get(User, me["id"])
        user.date_of_birth, user.country = None, None
        for k, v in kw.items():
            setattr(user, k, v)
        db.commit()
        assert client.get("/api/v1/users/me").json()["needs_profile_consent"] is True, kw


def test_a_registered_rider_does_not_need_profile_consent(api):
    client, db, sent, me = api
    me["id"] = client.post("/api/v1/auth/register", json=_form()).json()["id"]
    assert client.get("/api/v1/users/me").json()["needs_profile_consent"] is False


@pytest.mark.parametrize("kw, message", [
    ({"country": "GB"}, auth.NO_DATE_OF_BIRTH),
    ({"country": "GB", "date_of_birth": ""}, auth.NO_DATE_OF_BIRTH),
    ({"date_of_birth": "1990-05-17"}, auth.NO_COUNTRY),
    ({"date_of_birth": "1990-05-17", "country": "  "}, auth.NO_COUNTRY),
    ({"date_of_birth": "2026-10-09", "country": "GB"}, auth.BAD_DATE_OF_BIRTH),
    ({"date_of_birth": "1890-01-01", "country": "GB"}, auth.BAD_DATE_OF_BIRTH),
    ({"date_of_birth": "1990-05-17", "country": "FR"}, auth._not_available("FR")),
    ({"date_of_birth": "1990-05-17", "country": "US"}, auth._not_available("US")),
])
def test_reaccept_asks_a_beta_account_for_what_sign_up_would_have(api, kw, message):
    client, db, sent, me = api
    me["id"] = _beta_rider(db).id
    r = _reaccept(client, **kw)
    assert r.status_code == 400 and r.json()["detail"] == message
    _nothing_recorded(db, me)
    assert client.get("/api/v1/users/me").json()["needs_profile_consent"] is True


def test_reaccept_records_the_age_row_for_a_beta_account(api):
    client, db, sent, me = api
    me["id"] = _beta_rider(db).id
    r = _reaccept(client, date_of_birth="2008-10-08", country=" uk ")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "terms_version": settings.terms_version}

    db.expire_all()
    user = db.get(User, me["id"])
    assert (user.date_of_birth, user.country) == (date(2008, 10, 8), "GB")
    rows = {e.kind: e for e in db.query(ConsentEvent).all()}
    assert set(rows) == {"reaccept", "age"}
    age = rows["age"]
    assert (age.text_shown, age.doc_version, age.source, age.ip) == (
        auth.AGE_CONSENT_TEXT, _stamp(settings.terms_version), "reaccept", "81.2.69.160"
    )
    body = client.get("/api/v1/users/me").json()
    assert body["needs_profile_consent"] is False and body["terms_current"] is True


def test_reaccept_with_only_the_country_missing_rechecks_the_date_on_file(api):
    client, db, sent, me = api
    me["id"] = _beta_rider(db, date_of_birth=date(1990, 5, 17)).id
    assert _reaccept(client).json()["detail"] == auth.NO_COUNTRY
    r = _reaccept(client, country="GB")
    assert r.status_code == 200, r.text
    assert sorted(e.kind for e in db.query(ConsentEvent).all()) == ["age", "reaccept"]
    db.expire_all()
    assert db.get(User, me["id"]).date_of_birth == date(1990, 5, 17)


def test_reaccept_with_only_the_date_missing_keeps_the_country(api):
    client, db, sent, me = api
    me["id"] = _beta_rider(db, country="GB").id
    r = _reaccept(client, date_of_birth="1985-01-02")
    assert r.status_code == 200, r.text
    db.expire_all()
    user = db.get(User, me["id"])
    assert (user.date_of_birth, user.country) == (date(1985, 1, 2), "GB")
    assert sorted(e.kind for e in db.query(ConsentEvent).all()) == ["age", "reaccept"]


def test_reaccept_for_a_complete_account_records_no_age_row(api):
    client, db, sent, me = api
    me["id"] = _beta_rider(
        db, terms_version="terms-2026-01-01", date_of_birth=date(1990, 5, 17), country="GB"
    ).id
    assert _reaccept(client).status_code == 200
    assert [e.kind for e in db.query(ConsentEvent).all()] == ["reaccept"]


def test_reaccept_still_checks_a_country_sent_for_a_complete_account(api):
    client, db, sent, me = api
    me["id"] = _beta_rider(
        db, terms_version="terms-2026-01-01", date_of_birth=date(1990, 5, 17), country="GB"
    ).id
    assert _reaccept(client, country="CA").json()["detail"] == auth._not_available("CA")
    assert _reaccept(client, date_of_birth="2026-10-09").json()["detail"] == auth.BAD_DATE_OF_BIRTH
    assert db.query(ConsentEvent).count() == 0
    db.expire_all()
    user = db.get(User, me["id"])
    assert (user.date_of_birth, user.country, user.terms_version) == (
        date(1990, 5, 17), "GB", "terms-2026-01-01"
    )


# === An under-18 date of birth on re-acceptance (review round 3, new problem 4) ===


@pytest.fixture
def minor_api(api, monkeypatch):
    """The api fixture with Stripe configured and its renewal stop recorded,
    not called."""
    from app.services import billing_service

    stopped = []
    monkeypatch.setattr(billing_service, "is_configured", lambda: True)
    monkeypatch.setattr(
        billing_service, "stop_renewal_for_review",
        lambda user: stopped.append((user.id, user.stripe_customer_id)) or 1,
    )
    return (*api, stopped)


def _alerts(sent):
    return [m for m in sent if m["subject"] == "Forma safety alert: minor"]


def _minor_holds(db, user_id):
    from app.models.safety import SafetyHold

    db.expire_all()
    return db.query(SafetyHold).filter(
        SafetyHold.user_id == user_id, SafetyHold.red_flag == "minor"
    ).all()


def _minor_events(db, user_id):
    from app.models.safety import SafetyEvent

    db.expire_all()
    return db.query(SafetyEvent).filter(
        SafetyEvent.user_id == user_id, SafetyEvent.kind == "minor"
    ).all()


def test_an_under_18_date_holds_the_account_tells_gareth_and_stops_renewal(minor_api):
    from app.services import safety_service

    client, db, sent, me, stopped = minor_api
    me["id"] = _beta_rider(db, stripe_customer_id="cus_123").id
    r = _reaccept(client, date_of_birth="2009-03-01", country="GB")
    assert r.status_code == 403
    assert r.json() == {"detail": auth.MINOR_ACCOUNT}

    [hold] = _minor_holds(db, me["id"])
    assert (hold.level, hold.source, hold.lifted_at) == ("hold_all", auth.MINOR_HOLD_SOURCE, None)
    assert safety_service.lift_kind(hold) == "admin_only"
    assert safety_service.minor_hold(db, me["id"]).id == hold.id
    [event] = _minor_events(db, me["id"])
    assert (event.source, event.stated_age, event.hold_id) == ("reaccept", 17, hold.id)
    assert "2009-03-01" in event.matched and "age 17" in event.matched
    # Gareth is told, once, and the event says so.
    [alert] = _alerts(sent)
    assert alert["to"] == settings.founder_alert_email
    assert me["id"] in alert["body"] and "2009-03-01" in alert["body"]
    assert event.founder_alerted_at is not None
    assert stopped == [(me["id"], "cus_123")]
    # Nothing is agreed and nothing is kept on the profile.
    _nothing_recorded(db, me)
    # Records are kept to the 21st birthday the stated age implies.
    from datetime import timedelta

    said = event.created_at
    until = safety_service.minor_retention_until(event)
    assert until.date() == said.replace(year=said.year + 4).date() + timedelta(days=1)


def test_an_adult_date_sent_next_cannot_undo_the_hold(minor_api):
    client, db, sent, me, stopped = minor_api
    me["id"] = _beta_rider(db, stripe_customer_id="cus_123").id
    assert _reaccept(client, date_of_birth="2010-01-01", country="GB").status_code == 403
    for kw in ({"date_of_birth": "1990-01-01", "country": "GB"}, {}, {"country": "GB"}):
        r = _reaccept(client, **kw)
        assert r.status_code == 403 and r.json()["detail"] == auth.MINOR_ACCOUNT, kw
    _nothing_recorded(db, me)
    assert len(_minor_holds(db, me["id"])) == 1
    assert len(_minor_events(db, me["id"])) == 1
    assert len(_alerts(sent)) == 1 and len(stopped) == 1
    # Box 2 is refused too, as for any account held as under 18.
    r = client.post("/api/v1/auth/health-consent", json={"text_shown": HEALTH_BOX})
    assert r.status_code == 403


def test_an_under_18_date_on_file_cannot_be_overwritten_by_re_acceptance(minor_api):
    """Round 3: 2010-01-01 on file, only the country missing, was replaced by
    1990-01-01 with an age row saying 18 or over."""
    client, db, sent, me, stopped = minor_api
    me["id"] = _beta_rider(db, date_of_birth=date(2010, 1, 1)).id
    r = _reaccept(client, date_of_birth="1990-01-01", country="GB")
    assert r.status_code == 403 and r.json()["detail"] == auth.MINOR_ACCOUNT
    db.expire_all()
    user = db.get(User, me["id"])
    assert (user.date_of_birth, user.country, user.terms_version) == (date(2010, 1, 1), None, None)
    assert db.query(ConsentEvent).count() == 0
    [event] = _minor_events(db, me["id"])
    assert event.stated_age == 16
    assert len(_alerts(sent)) == 1
    # No Stripe customer, so nothing to stop.
    assert stopped == []


def test_an_under_18_date_on_a_complete_account_is_held_too(minor_api):
    client, db, sent, me, stopped = minor_api
    me["id"] = _beta_rider(
        db, terms_version="terms-2026-01-01", date_of_birth=date(1990, 5, 17), country="GB"
    ).id
    r = _reaccept(client, date_of_birth="2010-01-01")
    assert r.status_code == 403 and r.json()["detail"] == auth.MINOR_ACCOUNT
    db.expire_all()
    user = db.get(User, me["id"])
    assert (user.date_of_birth, user.terms_version) == (date(1990, 5, 17), "terms-2026-01-01")
    assert [e.stated_age for e in _minor_events(db, me["id"])] == [16]


def test_an_adult_date_on_file_stands_whatever_date_is_sent(api):
    client, db, sent, me = api
    me["id"] = _beta_rider(db, date_of_birth=date(1990, 5, 17)).id
    r = _reaccept(client, date_of_birth="1985-01-02", country="GB")
    assert r.status_code == 200, r.text
    db.expire_all()
    user = db.get(User, me["id"])
    assert (user.date_of_birth, user.country) == (date(1990, 5, 17), "GB")
    assert sorted(e.kind for e in db.query(ConsentEvent).all()) == ["age", "reaccept"]


def test_the_eighteenth_birthday_is_not_held(minor_api):
    client, db, sent, me, stopped = minor_api
    me["id"] = _beta_rider(db).id
    assert _reaccept(client, date_of_birth="2008-10-08", country="GB").status_code == 200
    assert _minor_holds(db, me["id"]) == [] and _alerts(sent) == []


def test_an_account_already_held_by_the_chat_is_refused_with_nothing_new(minor_api):
    from app.services import safety_service

    client, db, sent, me, stopped = minor_api
    me["id"] = _beta_rider(
        db, terms_version="terms-2026-01-01", date_of_birth=date(1990, 5, 17), country="GB",
        stripe_customer_id="cus_123",
    ).id
    safety_service.open_hold(db, me["id"], "hold_all", "Said 15 in chat", "detector", red_flag="minor")
    r = _reaccept(client)
    assert r.status_code == 403 and r.json()["detail"] == auth.MINOR_ACCOUNT
    assert db.query(ConsentEvent).count() == 0
    assert _minor_events(db, me["id"]) == [] and _alerts(sent) == [] and stopped == []


def test_a_failed_alert_still_holds_the_account(minor_api, monkeypatch):
    client, db, sent, me, stopped = minor_api

    async def broken(*a, **k):
        raise RuntimeError("Postmark is down")

    monkeypatch.setattr(email_service, "send_safety_alert", broken)
    me["id"] = _beta_rider(db).id
    r = _reaccept(client, date_of_birth="2011-06-01", country="GB")
    assert r.status_code == 403
    [event] = _minor_events(db, me["id"])
    assert event.founder_alerted_at is None
    assert len(_minor_holds(db, me["id"])) == 1


def test_an_adult_freed_by_review_can_re_accept_with_their_real_date(minor_api):
    from app.services import safety_service

    client, db, sent, me, stopped = minor_api
    me["id"] = _beta_rider(db).id
    _reaccept(client, date_of_birth="2010-01-01", country="GB")
    [hold] = _minor_holds(db, me["id"])
    safety_service.admin_lift(db, hold.id, "Typo in the year; adult, checked by email")
    r = _reaccept(client, date_of_birth="1990-01-01", country="GB")
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(User, me["id"]).date_of_birth == date(1990, 1, 1)


def test_the_under_18_answer_is_house_style_and_matches_the_chat():
    from app.services.coach_service import MINOR_CLOSING

    text = auth.MINOR_ACCOUNT
    assert text.endswith(MINOR_CLOSING)
    assert "18 and over" in text
    for bad in ("\u2014", "\u2013", "!", " - ", "kicker"):
        assert bad not in text


def test_a_new_date_of_birth_in_settings_leaves_an_age_row(api):
    client, db, sent, me = api
    me["id"] = _beta_rider(db, country="GB").id
    r = client.patch("/api/v1/users/me", json={"date_of_birth": "1990-05-17"},
                     headers={"x-forwarded-for": "81.2.69.160"})
    assert r.status_code == 200 and r.json()["needs_profile_consent"] is False
    age = db.query(ConsentEvent).one()
    assert (age.kind, age.text_shown, age.source, age.ip) == (
        "age", auth.AGE_CONSENT_TEXT, "app", "81.2.69.160"
    )
    # The settings form resending the same date adds nothing.
    client.patch("/api/v1/users/me", json={"date_of_birth": "1990-05-17", "full_name": "Sam"})
    client.patch("/api/v1/users/me", json={"full_name": "Sam R"})
    assert db.query(ConsentEvent).count() == 1


def test_reaccept_needs_the_text_shown(api):
    client, db, sent, me = api
    me["id"] = _beta_rider(db).id
    assert client.post("/api/v1/auth/reaccept-terms", json={"text_shown": ""}).status_code == 422
    assert client.post("/api/v1/auth/reaccept-terms", json={"text_shown": "   "}).status_code == 400
    assert db.query(ConsentEvent).count() == 0
    assert db.get(User, me["id"]).terms_version is None


# === Box 2 for the beta riders, who were never asked ===


def test_a_beta_rider_has_not_given_health_consent_and_never_sees_the_date(api):
    client, db, sent, me = api
    me["id"] = _beta_rider(db).id
    body = client.get("/api/v1/users/me").json()
    assert body["health_consent_current"] is False
    assert "health_consent_at" not in body


def test_health_consent_records_box_2_against_the_privacy_policy(api):
    client, db, sent, me = api
    me["id"] = _beta_rider(db, terms_version=settings.terms_version).id
    r = client.post(
        "/api/v1/auth/health-consent",
        json={"text_shown": HEALTH_BOX},
        headers={"x-forwarded-for": "81.2.69.160"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "privacy_version": settings.privacy_version}
    assert client.get("/api/v1/users/me").json()["health_consent_current"] is True
    event = db.query(ConsentEvent).one()
    assert (event.kind, event.doc_version, event.text_shown, event.ip) == (
        "health_data", _stamp(settings.privacy_version), HEALTH_BOX, "81.2.69.160"
    )
    assert db.get(User, me["id"]).health_consent_at is not None


def test_health_consent_needs_the_text_shown(api):
    client, db, sent, me = api
    me["id"] = _beta_rider(db).id
    assert client.post("/api/v1/auth/health-consent", json={"text_shown": ""}).status_code == 422
    assert client.post("/api/v1/auth/health-consent", json={"text_shown": "  "}).status_code == 400
    assert db.query(ConsentEvent).count() == 0
    assert db.get(User, me["id"]).health_consent_at is None


def test_the_screening_refuses_a_rider_who_never_gave_box_2(api):
    from app.api.v1.onboarding import HEALTH_CONSENT_FIRST
    from app.models.safety import HealthScreening

    client, db, sent, me = api
    me["id"] = _beta_rider(db, terms_version=settings.terms_version).id
    answers = {f"q{i}": False for i in range(1, 9)}
    r = client.post(
        "/api/v1/onboarding/screening", json={"answers": answers, "long_break": False}
    )
    assert r.status_code == 400 and r.json()["detail"] == HEALTH_CONSENT_FIRST
    assert db.query(HealthScreening).count() == 0

    client.post("/api/v1/auth/health-consent", json={"text_shown": HEALTH_BOX})
    r = client.post(
        "/api/v1/onboarding/screening", json={"answers": answers, "long_break": False}
    )
    assert r.status_code == 200
    assert db.query(HealthScreening).count() == 1


# === Profile edits meet the same rules ===


def test_an_under_18_date_in_a_profile_edit_holds_the_account(minor_api):
    """Round 3, problem 4, the same gap in PATCH /users/me: an under-18 date
    was just refused with nothing recorded, and an adult date could follow.
    Now it holds the account exactly as re-acceptance does."""
    client, db, sent, me, stopped = minor_api
    me["id"] = _beta_rider(
        db, date_of_birth=date(1990, 5, 17), stripe_customer_id="cus_9"
    ).id
    r = client.patch(
        "/api/v1/users/me", json={"date_of_birth": "2009-01-01", "full_name": "Kid"}
    )
    assert r.status_code == 403 and r.json() == {"detail": auth.MINOR_ACCOUNT}
    db.expire_all()
    user = db.get(User, me["id"])
    # The date sent is kept on the event, not the profile, and nothing else
    # in the edit is saved.
    assert user.date_of_birth == date(1990, 5, 17) and user.full_name != "Kid"
    [hold] = _minor_holds(db, me["id"])
    assert (hold.source, hold.lifted_at) == ("profile", None)
    [event] = _minor_events(db, me["id"])
    assert (event.source, event.stated_age, event.hold_id) == ("profile", 17, hold.id)
    assert len(_alerts(sent)) == 1 and stopped == [(me["id"], "cus_9")]

    # An adult date next can't undo it, and nothing more is written.
    r = client.patch("/api/v1/users/me", json={"date_of_birth": "1980-01-01"})
    assert r.status_code == 403 and r.json()["detail"] == auth.MINOR_ACCOUNT
    db.expire_all()
    assert db.get(User, me["id"]).date_of_birth == date(1990, 5, 17)
    assert len(_minor_holds(db, me["id"])) == 1 and len(_minor_events(db, me["id"])) == 1
    assert len(_alerts(sent)) == 1 and len(stopped) == 1
    # Edits that leave the date alone still save.
    r = client.patch("/api/v1/users/me", json={"full_name": "Sam", "date_of_birth": "1990-05-17"})
    assert r.status_code == 200 and r.json()["full_name"] == "Sam"


def test_a_bad_date_in_a_profile_edit_is_refused_with_nothing_held(api):
    client, db, sent, me = api
    me["id"] = _beta_rider(db, date_of_birth=date(1990, 5, 17)).id
    r = client.patch("/api/v1/users/me", json={"date_of_birth": "2099-01-01"})
    assert r.status_code == 400 and r.json()["detail"] == auth.BAD_DATE_OF_BIRTH
    assert _minor_holds(db, me["id"]) == []
    assert db.get(User, me["id"]).date_of_birth == date(1990, 5, 17)


def test_profile_edit_country_is_checked_and_cannot_be_cleared(api, monkeypatch):
    client, db, sent, me = api
    me["id"] = _beta_rider(db).id
    r = client.patch("/api/v1/users/me", json={"country": "us"})
    assert r.status_code == 400 and "US or Canada" in r.json()["detail"]
    for blocked in ("pr", "zz", "au", "ie"):
        r = client.patch("/api/v1/users/me", json={"country": blocked})
        assert r.status_code == 400, blocked
        assert r.json()["detail"].startswith("Forma isn't available in your country yet.")
    monkeypatch.setattr(settings, "allowed_countries", ["GB", "IE"])
    r = client.patch("/api/v1/users/me", json={"country": "ie"})
    assert r.status_code == 200 and r.json()["country"] == "IE"
    r = client.patch("/api/v1/users/me", json={"country": None, "full_name": "Sam"})
    assert r.status_code == 200 and r.json()["country"] == "IE"


# === The published documents (review finding 13) ===


def _stamp(version: str) -> str:
    """What a consent row should say, worked out here from the file itself."""
    raw = (LEGAL_PUBLISHED_DIR / f"{version}.md").read_bytes()
    return f"{version}#{hashlib.sha256(raw).hexdigest()[:12]}"


def test_settings_name_the_published_drafts_until_gareth_approves():
    assert settings.terms_version == "terms-2026-10-draft"
    assert settings.privacy_version == "privacy-2026-10-draft"
    assert settings.terms_doc().doc_version == _stamp(settings.terms_version)
    assert settings.privacy_doc().doc_version == _stamp(settings.privacy_version)


@pytest.mark.parametrize("version_attr", ["terms_version", "privacy_version"])
def test_each_published_file_names_its_own_version(version_attr):
    version = getattr(settings, version_attr)
    text = (LEGAL_PUBLISHED_DIR / f"{version}.md").read_text(encoding="utf-8")
    assert f"**Version: {version}**" in text
    # The note to Gareth at the top of the working draft is not part of it.
    assert "DRAFT for Gareth" not in text and "Before publishing" not in text
    assert "—" not in text and "–" not in text
    for claim in ("injury-proof", "prevents injury"):
        assert claim not in text.lower()


# Review new problem 10: the published folder is read at startup. If it's
# missing from a deploy, the API must keep serving, not crash-loop.


@pytest.fixture
def no_published_folder(tmp_path, monkeypatch):
    """The published folder as a deploy without it would see: empty."""
    published_legal_doc.cache_clear()
    monkeypatch.setattr(app_config, "LEGAL_PUBLISHED_DIR", tmp_path / "published")
    yield
    published_legal_doc.cache_clear()


def test_a_missing_published_file_is_logged_and_stamped_missing(no_published_folder, caplog):
    with caplog.at_level(logging.ERROR, logger="app.config"):
        missing = Settings()
    assert missing.terms_doc().doc_version == f"{settings.terms_version}#missing"
    assert missing.privacy_doc().doc_version == f"{settings.privacy_version}#missing"
    assert missing.terms_doc().text == MISSING_LEGAL_TEXT
    assert missing.missing_legal_docs() == [settings.terms_version, settings.privacy_version]
    logged = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("LEGAL DOCUMENT MISSING" in m and settings.terms_version in m for m in logged)
    assert any("LEGAL DOCUMENT MISSING" in m and settings.privacy_version in m for m in logged)


def test_a_present_published_file_is_not_missing():
    assert settings.missing_legal_docs() == []
    assert not settings.terms_doc().missing


def test_sign_up_keeps_working_against_a_missing_file(api, monkeypatch, caplog):
    client, db, sent, me = api
    monkeypatch.setattr(settings, "terms_version", "terms-1999-01-01")
    monkeypatch.setattr(settings, "privacy_version", "privacy-1999-01-01")
    with caplog.at_level(logging.ERROR):
        r = client.post("/api/v1/auth/register", json=_form())
    assert r.status_code == 201, r.text
    rows = {e.kind: e.doc_version for e in db.query(ConsentEvent).all()}
    assert rows == {
        "terms": "terms-1999-01-01#missing",
        "age": "terms-1999-01-01#missing",
        "health_data": "privacy-1999-01-01#missing",
    }
    assert any("published file is missing" in r.getMessage() for r in caplog.records)
    # The email says plainly that the text isn't attached, not nothing.
    assert MISSING_LEGAL_TEXT in sent[0]["body"]
    # The stand-in is never served as the published text.
    for doc in ("terms", "privacy"):
        r = client.get(f"/api/v1/auth/legal/{doc}")
        assert r.status_code == 503 and r.json()["detail"] == auth.LEGAL_DOC_UNAVAILABLE
    me["id"] = db.query(User).one().id
    assert client.post("/api/v1/auth/health-consent", json={"text_shown": HEALTH_BOX}).status_code == 200


def test_startup_logs_a_missing_published_file_as_critical(monkeypatch, caplog):
    """After Sentry is set up, so the alert reaches Sentry, not only the logs."""
    from app import main

    monkeypatch.setattr(settings, "terms_version", "terms-1999-01-01")
    with caplog.at_level(logging.CRITICAL, logger="app.main"):
        assert main.check_legal_docs() == ["terms-1999-01-01"]
    (record,) = [r for r in caplog.records if r.name == "app.main"]
    assert record.levelno == logging.CRITICAL
    assert "LEGAL DOCUMENT MISSING at startup: terms-1999-01-01" in record.getMessage()
    caplog.clear()
    monkeypatch.undo()
    assert main.check_legal_docs() == []
    assert not [r for r in caplog.records if r.name == "app.main"]


def test_the_api_starts_without_the_published_folder():
    """The deploy case end to end: a fresh interpreter, both versions naming
    files that aren't there, imports the app and answers requests."""
    root = Path(__file__).resolve().parent.parent
    script = (
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "c = TestClient(app)\n"
        "print(c.get('/api/v1/auth/config').status_code, c.get('/api/v1/auth/legal/terms').status_code)\n"
    )
    env = {**os.environ, "TERMS_VERSION": "terms-1999-01-01", "PRIVACY_VERSION": "privacy-1999-01-01",
           "SENTRY_DSN": "", "POSTMARK_SERVER_TOKEN": ""}
    done = subprocess.run([sys.executable, "-c", script], cwd=root, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == "200 503"
    assert "LEGAL DOCUMENT MISSING: terms-1999-01-01" in done.stderr


@pytest.mark.parametrize("bad", ["../../.env", "terms", "terms-../../x", "privacy-2026/10", ""])
def test_a_version_can_only_name_a_file_in_the_published_folder(bad):
    with pytest.raises(RuntimeError):
        published_legal_doc(bad)


def test_the_stamp_always_fits_the_column_with_its_hash():
    from app.models.safety import ConsentEvent as CE

    width = CE.__table__.c.doc_version.type.length
    assert len(settings.terms_doc().doc_version) <= width
    assert len(settings.privacy_doc().doc_version) <= width


def test_the_published_text_is_served_word_for_word(api):
    client, db, sent, _ = api
    for doc, version in (("terms", settings.terms_version), ("privacy", settings.privacy_version)):
        body = client.get(f"/api/v1/auth/legal/{doc}").json()
        assert body["version"] == version
        assert body["doc_version"] == _stamp(version)
        assert body["text"] == (LEGAL_PUBLISHED_DIR / f"{version}.md").read_text(encoding="utf-8")
    assert client.get("/api/v1/auth/legal/cookies").status_code == 404
