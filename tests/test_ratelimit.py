"""Who the rate limits count (review R16, new problem 5, and round 3 new
problems 2 and 6: the shared edge secret and GET /auth/ip-check), the
per-email backstop on login, forgot-password and sign-up, and wrong invite
codes (round 4, new problems C, F and G).

The browser reaches the API through the Vercel rewrite. Vercel overwrites
X-Forwarded-For with the rider's real address and stamps x-vercel-id;
Railway then appends the address that connected to it, which is Vercel's.
A direct call to Railway has no x-vercel-id, and its first hop is whatever
the caller chose to send."""

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import auth
from app.core import ratelimit
from app.core.ratelimit import EmailLimit, client_ip, normalise_email, rate_limit, wait_phrase
from app.core.security import hash_password
from app.models.base import Base
from app.models.user import User
from app.services import email_service

VERCEL = "76.76.21.21"  # one of Vercel's shared egress addresses
RIDER_A = "81.2.69.160"
RIDER_B = "81.2.69.161"


class _Req:
    def __init__(self, headers: dict, host: str | None = "10.0.0.1"):
        self.headers = headers
        self.client = type("C", (), {"host": host})() if host is not None else None


def _via_vercel(rider: str, extra: str = "") -> dict:
    hops = ", ".join(h for h in (rider, extra, VERCEL) if h)
    return {"x-vercel-id": "lhr1::abcd-1759920000000-0123456789ab", "x-forwarded-for": hops}


# === client_ip ===


def test_through_vercel_the_rider_is_the_first_hop():
    assert client_ip(_Req(_via_vercel(RIDER_A))) == RIDER_A


def test_through_vercel_two_riders_are_two_addresses():
    assert client_ip(_Req(_via_vercel(RIDER_A))) != client_ip(_Req(_via_vercel(RIDER_B)))


def test_a_direct_call_uses_the_last_hop_whatever_the_caller_sent():
    forged = {"x-forwarded-for": f"6.6.6.6, {RIDER_A}"}
    assert client_ip(_Req(forged)) == RIDER_A
    assert client_ip(_Req({"x-forwarded-for": RIDER_A})) == RIDER_A


def test_with_no_forwarded_header_the_connecting_address_is_used():
    assert client_ip(_Req({})) == "10.0.0.1"
    assert client_ip(_Req({"x-vercel-id": "lhr1::x"})) == "10.0.0.1"
    assert client_ip(_Req({}, host=None)) is None
    assert client_ip(_Req({"x-forwarded-for": " , "}, host="")) is None


def test_blank_hops_are_skipped_and_the_address_fits_the_column():
    assert client_ip(_Req({"x-vercel-id": "x", "x-forwarded-for": f" , {RIDER_A} , {VERCEL}"})) == RIDER_A
    assert client_ip(_Req({"x-forwarded-for": f"{RIDER_A}, "})) == RIDER_A
    assert len(client_ip(_Req({"x-forwarded-for": "9" * 500}))) == 64


def test_the_header_names_are_read_case_insensitively_on_a_real_request():
    app = FastAPI()

    @app.get("/ip")
    def ip(request: Request):
        return {"ip": client_ip(request)}

    client = TestClient(app)
    headers = {"X-Vercel-Id": "lhr1::x", "X-Forwarded-For": f"{RIDER_A}, {VERCEL}"}
    assert client.get("/ip", headers=headers).json() == {"ip": RIDER_A}
    assert client.get("/ip", headers={"X-Forwarded-For": f"6.6.6.6, {RIDER_A}"}).json() == {"ip": RIDER_A}
    assert client.get("/ip").json() == {"ip": "testclient"}


# === rate_limit keys on it ===


@pytest.fixture
def limited():
    app = FastAPI()
    dep = rate_limit(2, 3600)

    @app.post("/limited", dependencies=[Depends(dep)])
    def limited_route():
        return {"ok": True}

    return TestClient(app)


def test_riders_through_vercel_no_longer_share_one_bucket(limited):
    for _ in range(2):
        assert limited.post("/limited", headers=_via_vercel(RIDER_A)).status_code == 200
    r = limited.post("/limited", headers=_via_vercel(RIDER_A))
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    # Same Vercel address on the end, different rider: a fresh bucket.
    assert limited.post("/limited", headers=_via_vercel(RIDER_B)).status_code == 200


def test_a_rider_keeps_their_bucket_whatever_vercel_address_carries_them(limited):
    limited.post("/limited", headers=_via_vercel(RIDER_A))
    limited.post("/limited", headers={**_via_vercel(RIDER_A), "x-forwarded-for": f"{RIDER_A}, 76.76.21.99"})
    assert limited.post("/limited", headers=_via_vercel(RIDER_A)).status_code == 429


def test_a_direct_caller_cannot_dodge_the_limit_with_forged_first_hops(limited):
    for fake in ("1.1.1.1", "2.2.2.2"):
        assert limited.post("/limited", headers={"x-forwarded-for": f"{fake}, {RIDER_A}"}).status_code == 200
    r = limited.post("/limited", headers={"x-forwarded-for": f"3.3.3.3, {RIDER_A}"})
    assert r.status_code == 429


def test_the_window_forgets_keys_with_nothing_left_in_them(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(ratelimit, "_SWEEP_AT", 3)
    window = ratelimit._SlidingWindow(1, 60)
    for key in ("a", "b", "c"):
        assert window.hit(key) is None
    now[0] += 61
    assert window.hit("d") is None  # the table was full: a, b and c were stale
    assert len(window) == 1
    assert window.hit("d") == 60  # and d is still limited


# === The per-email backstop ===


def test_email_is_normalised_like_the_account_key():
    assert normalise_email("  Rider@Example.COM ") == "rider@example.com"


def test_wait_phrase_reads_naturally():
    assert wait_phrase(1) == wait_phrase(60) == "a minute"
    assert wait_phrase(61) == "2 minutes"
    assert wait_phrase(900) == "15 minutes"
    assert wait_phrase(3600) == wait_phrase(3541) == "an hour"


def test_email_limit_counts_one_email_from_anywhere(monkeypatch):
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: 5000.0)
    limit = EmailLimit(2, 900, "Wait {wait}.")
    limit.check("rider@example.com")
    limit.check(" RIDER@example.com")
    with pytest.raises(ratelimit.HTTPException) as caught:
        limit.check("rider@example.com ")
    assert caught.value.status_code == 429
    assert caught.value.detail == "Wait 15 minutes."
    assert caught.value.headers["Retry-After"] == "900"
    limit.check("someone.else@example.com")


@pytest.fixture
def auth_api(monkeypatch):
    """The real auth endpoints over private SQLite, one known rider, every
    email captured, and every limit starting empty."""
    from app.database import get_db
    from app.main import app

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(email="rider@example.com", hashed_password=hash_password("the-right-password")))
    db.commit()

    sent = []

    async def fake_send(to, subject, text_body, from_address=None):
        sent.append(to)
        return True

    monkeypatch.setattr(email_service, "send", fake_send)
    limits = (auth._login_limit.window, auth._email_limit.window,
              auth._login_email_limit, auth._reset_email_limit)
    for limit in limits:
        limit.reset()
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app), sent
    finally:
        app.dependency_overrides.pop(get_db, None)
        for limit in limits:
            limit.reset()
        db.close()


def _from(n: int) -> dict:
    """A different first hop each time through Vercel: what a botnet, or a
    direct caller forging x-vercel-id, looks like to the address limit."""
    return _via_vercel(f"203.0.113.{n}")


def _login(client, email, password="wrong-password", n=0):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password}, headers=_from(n))


def test_login_is_limited_per_email_however_many_addresses(auth_api):
    client, _ = auth_api
    for n in range(10):
        assert _login(client, "rider@example.com", n=n).status_code == 401
    r = _login(client, "Rider@Example.com", password="the-right-password", n=99)
    assert r.status_code == 429
    assert r.json()["detail"] == auth.LOGIN_EMAIL_LIMITED.format(wait="15 minutes")
    assert 0 < int(r.headers["Retry-After"]) <= 900
    # Another account is untouched.
    assert _login(client, "other@example.com", n=100).status_code == 401


def test_the_login_email_limit_says_nothing_about_which_emails_exist(auth_api):
    client, _ = auth_api
    for n in range(10):
        _login(client, "rider@example.com", n=n)
        _login(client, "nobody@example.com", n=50 + n)
    known = _login(client, "rider@example.com", n=98)
    unknown = _login(client, "nobody@example.com", n=99)
    assert known.status_code == unknown.status_code == 429
    assert known.json() == unknown.json()


def test_the_right_password_still_works_under_the_limit(auth_api):
    client, _ = auth_api
    for n in range(9):
        _login(client, "rider@example.com", n=n)
    r = _login(client, "rider@example.com", password="the-right-password", n=9)
    assert r.status_code == 200 and r.json()["access_token"]


def test_the_login_address_limit_follows_the_rider_through_vercel(auth_api):
    client, _ = auth_api
    for i in range(10):
        r = client.post("/api/v1/auth/login", json={"email": f"r{i}@example.com", "password": "x"},
                        headers=_via_vercel(RIDER_A))
        assert r.status_code == 401
    r = client.post("/api/v1/auth/login", json={"email": "r10@example.com", "password": "x"},
                    headers=_via_vercel(RIDER_A))
    assert r.status_code == 429
    # A different rider behind the same Vercel address isn't caught by it.
    r = client.post("/api/v1/auth/login", json={"email": "r11@example.com", "password": "x"},
                    headers=_via_vercel(RIDER_B))
    assert r.status_code == 401


def test_forgot_password_is_limited_per_email_however_many_addresses(auth_api):
    client, sent = auth_api
    for n in range(5):
        r = client.post("/api/v1/auth/forgot-password", json={"email": "rider@example.com"}, headers=_from(n))
        assert r.status_code == 200 and r.json() == {"status": "sent"}
    r = client.post("/api/v1/auth/forgot-password", json={"email": " RIDER@example.com"}, headers=_from(99))
    assert r.status_code == 429
    assert r.json()["detail"] == auth.RESET_EMAIL_LIMITED.format(wait="an hour")
    assert sent == ["rider@example.com"] * 5
    # An email with no account is answered the same way, and limited the same way.
    for n in range(5):
        r = client.post("/api/v1/auth/forgot-password", json={"email": "nobody@example.com"}, headers=_from(20 + n))
        assert r.json() == {"status": "sent"}
    assert client.post("/api/v1/auth/forgot-password", json={"email": "nobody@example.com"},
                       headers=_from(30)).status_code == 429
    assert len(sent) == 5


def test_the_messages_are_house_style():
    for text in (ratelimit.TOO_MANY, auth.LOGIN_EMAIL_LIMITED.format(wait="15 minutes"),
                 auth.RESET_EMAIL_LIMITED.format(wait="a minute")):
        assert "—" not in text and "–" not in text and "!" not in text and " - " not in text


# === The shared edge secret (review round 3, new problem 2) ===
#
# The Next.js frontend (frontend/src/middleware.ts) stamps every /api/*
# request it forwards with x-forma-edge: FORMA_EDGE_SECRET. Once Railway has
# the same secret, only that header makes the first hop trusted; a forged
# x-vercel-id no longer does.

SECRET = "s3cret-" + "x" * 40


@pytest.fixture
def edge(monkeypatch):
    monkeypatch.setattr(ratelimit.settings, "edge_secret", SECRET)
    return SECRET


def _through_edge(rider: str, secret: str = SECRET) -> dict:
    return {**_via_vercel(rider), ratelimit.EDGE_HEADER: secret}


def test_the_secret_is_read_from_forma_edge_secret(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("FORMA_EDGE_SECRET", f"  {SECRET}\n")
    assert Settings().edge_secret == SECRET
    monkeypatch.delenv("FORMA_EDGE_SECRET")
    monkeypatch.setenv("EDGE_SECRET", "not-this-one")
    assert Settings(_env_file=None).edge_secret == ""


def test_with_the_secret_the_edge_header_makes_the_first_hop_the_rider(edge):
    assert client_ip(_Req(_through_edge(RIDER_A))) == RIDER_A
    assert client_ip(_Req({**_through_edge(RIDER_A), "x-forma-edge": f" {SECRET} "})) == RIDER_A


def test_with_the_secret_a_forged_vercel_id_no_longer_picks_the_bucket(edge):
    """Round 3: a direct call adding any x-vercel-id chose its own first hop.
    Railway appends the address that really connected (RIDER_A)."""
    forged = {"x-vercel-id": "lhr1::forged", "x-forwarded-for": f"1.2.3.4, {RIDER_A}"}
    assert client_ip(_Req(forged)) == RIDER_A
    assert client_ip(_Req({**forged, ratelimit.EDGE_HEADER: "a-guess"})) == RIDER_A
    assert client_ip(_Req({**forged, ratelimit.EDGE_HEADER: SECRET[:-1]})) == RIDER_A
    assert client_ip(_Req({**forged, ratelimit.EDGE_HEADER: ""})) == RIDER_A
    assert client_ip(_Req({**forged, ratelimit.EDGE_HEADER: SECRET})) == "1.2.3.4"


def test_the_secret_is_compared_in_constant_time(edge, monkeypatch):
    calls = []
    real = ratelimit.hmac.compare_digest

    def spy(a, b):
        calls.append((len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr(ratelimit.hmac, "compare_digest", spy)
    ratelimit.via_edge(_Req({ratelimit.EDGE_HEADER: "short"}))
    ratelimit.via_edge(_Req({ratelimit.EDGE_HEADER: SECRET}))
    # Both sides hashed first, so a guess's length changes nothing.
    assert calls == [(32, 32), (32, 32)]


def test_without_the_secret_the_older_rule_still_holds(monkeypatch):
    monkeypatch.setattr(ratelimit.settings, "edge_secret", "")
    assert client_ip(_Req(_via_vercel(RIDER_A))) == RIDER_A
    # A header sent before the secret is set changes nothing either way.
    assert client_ip(_Req({"x-forwarded-for": f"6.6.6.6, {RIDER_A}", ratelimit.EDGE_HEADER: "x"})) == RIDER_A


@pytest.fixture
def register_api(monkeypatch):
    """The real register endpoint, every limit live and empty, over private
    SQLite, emails captured, invite door open."""
    from app.config import settings
    from app.database import get_db
    from app.main import app

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    async def fake_send(*a, **k):
        return True

    monkeypatch.setattr(email_service, "send", fake_send)
    monkeypatch.setattr(settings, "require_invite", False)
    limits = (auth._register_limit.window, auth._register_email_limit, auth._invite_failures,
              auth._invite_surge, auth._ip_check_limit.window)
    for limit in limits:
        limit.reset()
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app), db
    finally:
        app.dependency_overrides.pop(get_db, None)
        for limit in limits:
            limit.reset()
        db.close()


def _signup(email: str, **kw) -> dict:
    form = {
        "email": email, "password": "a-long-password", "date_of_birth": "1990-05-17",
        "country": "GB", "terms_accepted": True, "health_consent": True,
        "terms_text_shown": "terms box", "health_text_shown": "health box",
    }
    form.update(kw)
    return form


def test_forged_vercel_ids_no_longer_get_round_the_signup_limit(edge, register_api):
    """Round 3: 12 sign-ups, each with a new forged first hop and an
    x-vercel-id, all got through the address limit."""
    client, _ = register_api
    codes = []
    for n in range(12):
        headers = {"x-vercel-id": "lhr1::forged", "x-forwarded-for": f"1.2.3.{n}, {RIDER_A}"}
        codes.append(client.post("/api/v1/auth/register", json=_signup(f"r{n}@example.com"),
                                 headers=headers).status_code)
    assert codes[:5] == [201] * 5
    assert codes[5:] == [429] * 7


def test_a_consent_row_records_the_address_railway_saw_for_a_forged_call(edge, register_api):
    from app.models.safety import ConsentEvent

    client, db = register_api
    headers = {"x-vercel-id": "lhr1::forged", "x-forwarded-for": f"1.2.3.4, {RIDER_A}"}
    assert client.post("/api/v1/auth/register", json=_signup("a@example.com"), headers=headers).status_code == 201
    assert {e.ip for e in db.query(ConsentEvent).all()} == {RIDER_A}
    db.query(ConsentEvent).delete()
    db.commit()
    assert client.post("/api/v1/auth/register", json=_signup("b@example.com"),
                       headers=_through_edge(RIDER_B)).status_code == 201
    assert {e.ip for e in db.query(ConsentEvent).all()} == {RIDER_B}


# === Failed sign-ups per email (round 4, new problem F) ===
#
# Round 4: five junk attempts with a rider's email locked the rider out of
# joining for an hour. Only failures count now, and only a failure is ever
# refused for it.


def test_failed_signups_are_limited_per_email_however_many_addresses(edge, register_api):
    client, _ = register_api
    codes = [
        client.post("/api/v1/auth/register", json=_signup(" Same@Example.com", country="FR"),
                    headers=_through_edge(f"203.0.113.{n}")).status_code
        for n in range(6)
    ]
    assert codes == [400] * 5 + [429]
    r = client.post("/api/v1/auth/register", json=_signup("same@example.com", country="US"),
                    headers=_through_edge("203.0.113.98"))
    assert r.status_code == 429
    assert r.json()["detail"] == auth.REGISTER_EMAIL_LIMITED.format(wait="an hour")
    assert 0 < int(r.headers["Retry-After"]) <= 3600
    # The owner's own correct sign-up goes through all the same.
    r = client.post("/api/v1/auth/register", json=_signup("same@example.com"), headers=_through_edge("203.0.113.99"))
    assert r.status_code == 201, r.text
    # Someone else's email is untouched.
    assert client.post("/api/v1/auth/register", json=_signup("other@example.com"),
                       headers=_through_edge("203.0.113.100")).status_code == 201


def test_only_failed_signups_count_against_the_email(edge, register_api):
    client, _ = register_api

    def post(n, **kw):
        return client.post("/api/v1/auth/register", json=_signup("me@example.com", **kw),
                           headers=_through_edge(f"203.0.113.{n}")).status_code

    assert [post(n, country="FR") for n in range(4)] == [400] * 4
    assert post(10) == 201
    # Had the success counted, this would be the sixth and refused. It is
    # the fifth failure (the email now has an account), so it gets its reason.
    assert post(11) == 409
    assert post(12) == 429


@pytest.fixture
def invite_api(edge, register_api, monkeypatch):
    """register_api with the invite door shut, one launch code good for a
    hundred, the clock in the test's hands, and every safety alert captured
    instead of sent."""
    from app.config import settings
    from app.models.invite import InviteCode

    client, db = register_api
    monkeypatch.setattr(settings, "require_invite", True)
    db.add(InviteCode(code="HUNDRED", max_uses=100, uses=0))
    db.commit()
    now = [10_000.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: now[0])
    alerts = []

    async def fake_alert(kind, user_email, user_id, excerpt):
        alerts.append({"kind": kind, "email": user_email, "id": user_id, "excerpt": excerpt})
        return True

    monkeypatch.setattr(email_service, "send_safety_alert", fake_alert)
    return client, db, now, alerts


def _addr(n: int) -> str:
    return f"100.64.{n // 250}.{n % 250 + 1}"


def _try_code(client, email: str, code: str | None, address: str):
    return client.post("/api/v1/auth/register", json=_signup(email, invite_code=code),
                       headers=_through_edge(address))


def test_junk_with_a_riders_email_cannot_stop_them_joining(invite_api):
    """Round 4, new problem F, with the invite door shut: someone who knows
    an invitee's email tries it with junk codes from five addresses."""
    client, db, _, _ = invite_api
    for n in range(5):
        assert _try_code(client, "invitee@example.com", f"JUNK{n}", _addr(n)).status_code == 400
    r = _try_code(client, "invitee@example.com", "JUNK9", _addr(9))
    assert r.status_code == 429
    assert r.json()["detail"] == auth.REGISTER_EMAIL_LIMITED.format(wait="an hour")
    # The invitee, with the code from their email, from their own address.
    r = _try_code(client, "Invitee@Example.com", "hundred", "81.2.69.200")
    assert r.status_code == 201, r.text
    db.expire_all()
    assert db.query(User).filter(User.email == "invitee@example.com").count() == 1


def test_a_request_turned_away_by_a_limit_is_not_counted_against_the_email(invite_api):
    client, _, now, _ = invite_api
    guesser = "198.51.100.66"
    for n in range(10):
        assert _try_code(client, f"g{n}@example.com", f"WORD{n}", guesser).status_code == 400
        now[0] += 61
    for _ in range(6):
        r = _try_code(client, "bystander@example.com", "WRONG", guesser)
        assert r.status_code == 429
        assert r.json()["detail"].startswith("Too many wrong invite codes")
        now[0] += 61
    # None of those six tried a code, so this is the email's first failure.
    r = _try_code(client, "bystander@example.com", "WRONG", "81.2.69.201")
    assert r.status_code == 400 and "invite code doesn't work" in r.json()["detail"]


# === Wrong invite codes, per address (round 4, new problem C) ===


def test_one_address_gets_ten_wrong_invite_codes_an_hour(invite_api):
    from app.models.invite import InviteCode

    client, db, now, alerts = invite_api
    guesser = "198.51.100.7"
    start = now[0]
    for n in range(10):
        r = _try_code(client, f"g{n}@example.com", f"WORD{n}", guesser)
        assert r.status_code == 400, (n, r.text)
        now[0] += 61  # inside the address's own 5 requests per 5 minutes
    # The 11th try from that address is refused before the code is looked
    # at, right or wrong, so it can't tell a right guess from a wrong one.
    r = _try_code(client, "lucky@example.com", "hundred", guesser)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) == 2990
    assert r.json()["detail"] == auth.INVITE_GUESSES_LIMITED.format(wait="50 minutes")
    db.expire_all()
    assert db.query(InviteCode).one().uses == 0
    assert db.query(User).count() == 0
    # Every other address goes straight on: a wrong code gets its reason,
    # and the right code gets an account.
    assert _try_code(client, "typo@example.com", "HUNDERD", "81.2.69.10").status_code == 400
    r = _try_code(client, "rider@example.com", "HUNDRED", "81.2.69.11")
    assert r.status_code == 201, r.text
    # An hour after its first wrong code, the guesser's address may try again.
    now[0] = start + 3601
    r = _try_code(client, "late@example.com", "HUNDRED", guesser)
    assert r.status_code == 201, r.text
    db.expire_all()
    assert db.query(InviteCode).one().uses == 2
    assert alerts == []


def test_a_guesser_inside_its_own_limits_never_pauses_sign_up(invite_api):
    """Round 4, new problem C, replayed: one address sends a wrong code a
    minute for six hours, inside its own 5 requests per 5 minutes. Under
    the old shared cap of 50 an hour, riders with the right code got 429
    from minute 75 onwards. Now every one of them gets in, and the guesser
    has its codes looked at 10 times an hour."""
    client, _, now, alerts = invite_api
    start = now[0]
    guesser = "198.51.100.9"
    looked_at = [0] * 6
    riders = []
    for minute in range(360):
        now[0] = start + minute * 60
        r = _try_code(client, f"g{minute}@example.com", f"GUESS{minute}", guesser)
        assert r.status_code in (400, 429), (minute, r.text)
        if r.status_code == 400:
            looked_at[minute // 60] += 1
        if minute % 30 == 15:
            rider = _try_code(client, f"rider{minute}@example.com", "HUNDRED", _addr(minute))
            riders.append((minute, rider.status_code))
    assert riders == [(m, 201) for m in range(15, 360, 30)]
    assert looked_at == [10] * 6
    assert alerts == []


def test_a_missing_invite_code_is_not_a_guess(invite_api):
    client, _, now, _ = invite_api
    address = "198.51.100.30"
    for n in range(15):
        r = _try_code(client, f"nocode{n}@example.com", None, address)
        assert r.status_code == 400 and "invite-only" in r.json()["detail"]
        now[0] += 61
    assert _try_code(client, "rider@example.com", "HUNDRED", address).status_code == 201


def test_guessing_spread_over_many_addresses_tells_gareth_once_an_hour(invite_api):
    client, db, now, alerts = invite_api
    n = 0

    def guess(count):
        nonlocal n
        for _ in range(count):
            r = _try_code(client, f"g{n}@example.com", f"WORD{n}", _addr(n))
            assert r.status_code == 400, (n, r.text)
            n += 1

    guess(auth.INVITE_ALERT_AT)
    assert alerts == []
    guess(1)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["kind"] == "invite_abuse"
    assert alert["email"] == "" and alert["id"] == ""
    assert alert["excerpt"].startswith(
        f"More than {auth.INVITE_ALERT_AT} wrong invite codes were tried in the last hour, "
        f"from {auth.INVITE_ALERT_AT + 1} different addresses."
    )
    assert "FORMA_EDGE_SECRET is set" in alert["excerpt"]
    # Sign-up is never paused by it.
    r = _try_code(client, "rider@example.com", "HUNDRED", "81.2.69.50")
    assert r.status_code == 201, r.text
    # More guessing inside the hour: still one email.
    guess(150)
    assert len(alerts) == 1
    # An hour on, still going: one more.
    now[0] += 3601
    guess(auth.INVITE_ALERT_AT)
    assert len(alerts) == 1
    guess(1)
    assert len(alerts) == 2 and alerts[1]["kind"] == "invite_abuse"
    assert db.query(User).count() == 1


def test_the_alarm_says_when_the_addresses_cannot_be_trusted(monkeypatch):
    monkeypatch.setattr(ratelimit.settings, "edge_secret", "")
    note = auth._invite_abuse_note(3)
    assert "from 3 different addresses" in note
    assert "FORMA_EDGE_SECRET isn't set" in note and "Set it on Vercel and Railway." in note
    monkeypatch.setattr(ratelimit.settings, "edge_secret", SECRET)
    assert "FORMA_EDGE_SECRET is set, so these are real addresses." in auth._invite_abuse_note(3)
    assert SECRET not in auth._invite_abuse_note(3)


def test_the_alarm_note_fits_the_alert(monkeypatch):
    for secret in ("", SECRET):
        monkeypatch.setattr(ratelimit.settings, "edge_secret", secret)
        note = auth._invite_abuse_note(auth.INVITE_ALERT_AT + 1)
        assert len(note) <= email_service.SAFETY_ALERT_EXCERPT_CHARS


def test_a_failed_alert_email_is_logged_and_never_raised(monkeypatch, caplog):
    import asyncio

    async def broken(*a, **k):
        raise RuntimeError("provider down")

    async def refused(*a, **k):
        return False

    monkeypatch.setattr(email_service, "send_safety_alert", broken)
    asyncio.run(auth._alert_gareth_invite_abuse("note"))
    monkeypatch.setattr(email_service, "send_safety_alert", refused)
    asyncio.run(auth._alert_gareth_invite_abuse("note"))
    assert "Invite guessing alert email failed" in caplog.text
    assert "Invite guessing alert email not sent" in caplog.text


def test_failure_limit_counts_only_failures_per_key(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: now[0])
    limit = ratelimit.FailureLimit(2, 600, "Wait {wait}.")
    for _ in range(5):
        limit.check("a")  # checking records nothing
    limit.failed("a")
    limit.check("a")
    limit.failed("a")
    with pytest.raises(ratelimit.HTTPException) as caught:
        limit.check("a")
    assert caught.value.status_code == 429 and caught.value.detail == "Wait 10 minutes."
    assert caught.value.headers["Retry-After"] == "600"
    limit.check("b")  # another key is untouched
    now[0] += 601
    limit.check("a")


def test_surge_alarm_fires_past_the_threshold_at_most_once_a_window(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: now[0])
    alarm = ratelimit.SurgeAlarm(3, 3600)
    assert [alarm.record(k) for k in "aab"] == [None] * 3
    assert alarm.record("c") == 3  # the 4th in the hour, from a, b and c
    assert alarm.record("d") is None  # once a window
    now[0] = 3599
    assert alarm.record("e") is None
    now[0] = 3601
    # An hour since the alarm, but the old events have left the window.
    assert alarm.record("f") is None
    assert alarm.record("g") is None  # d is still from the first hour
    assert alarm.record("h") == 4  # e, f, g and h, all inside the last hour


def test_surge_alarm_stays_quiet_for_a_steady_trickle(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: now[0])
    alarm = ratelimit.SurgeAlarm(3, 100)
    for i in range(1000):
        now[0] = i * 34  # never more than 3 in any 100 seconds
        assert alarm.record("x") is None
    assert len(alarm._events) == 4  # only the latest threshold + 1 are kept
    alarm.reset()
    assert len(alarm._events) == 0


# === GET /auth/ip-check (review round 3, new problem 6) ===


def test_ip_check_through_the_edge(edge, register_api):
    client, _ = register_api
    r = client.get("/api/v1/auth/ip-check", headers=_through_edge(RIDER_A))
    assert r.status_code == 200
    assert r.json() == {"client_ip": RIDER_A, "hops": 2, "via_edge": True}
    assert r.headers["cache-control"] == "no-store"


def test_ip_check_on_a_direct_call_says_what_the_limiter_used_and_echoes_nothing(edge, register_api):
    client, _ = register_api
    headers = {"x-vercel-id": "lhr1::forged", "x-forwarded-for": f"6.6.6.6, 7.7.7.7, {RIDER_A}",
               ratelimit.EDGE_HEADER: "a-guess", "user-agent": "Probe/1.0"}
    r = client.get("/api/v1/auth/ip-check", headers=headers)
    assert r.json() == {"client_ip": RIDER_A, "hops": 3, "via_edge": False}
    for raw in ("6.6.6.6", "7.7.7.7", "a-guess", "lhr1", "Probe", SECRET):
        assert raw not in r.text
    r = client.get("/api/v1/auth/ip-check")
    assert r.json() == {"client_ip": "testclient", "hops": 0, "via_edge": False}


def test_ip_check_before_the_secret_is_set_tells_a_forger_nothing(register_api, monkeypatch):
    """Round 4, new problem G: with no secret, a forged x-vercel-id got
    via_edge true and its made-up first hop back as client_ip, so a direct
    caller learned the forgery works. Now every request gets the same
    answer: nulls, and no hop count."""
    monkeypatch.setattr(ratelimit.settings, "edge_secret", "")
    client, _ = register_api
    forged = {"x-vercel-id": "lhr1::forged", "x-forwarded-for": f"6.6.6.6, {RIDER_A}"}
    for headers in (forged, _via_vercel(RIDER_A), {"x-forwarded-for": f"6.6.6.6, {RIDER_A}"}, {}):
        r = client.get("/api/v1/auth/ip-check", headers=headers)
        assert r.status_code == 200
        assert r.json() == {"client_ip": None, "via_edge": None}
        assert "hops" not in r.json()
        assert r.headers["cache-control"] == "no-store"
        for raw in ("6.6.6.6", RIDER_A, "lhr1", "true", "false"):
            assert raw not in r.text


def test_ip_check_is_rate_limited(edge, register_api):
    client, _ = register_api
    codes = [client.get("/api/v1/auth/ip-check", headers=_through_edge(RIDER_A)).status_code for _ in range(11)]
    assert codes == [200] * 10 + [429]
    assert client.get("/api/v1/auth/ip-check", headers=_through_edge(RIDER_B)).status_code == 200


def test_the_new_messages_are_house_style(monkeypatch):
    notes = []
    for secret in ("", SECRET):
        monkeypatch.setattr(ratelimit.settings, "edge_secret", secret)
        notes.append(auth._invite_abuse_note(201))
    for text in (auth.REGISTER_EMAIL_LIMITED.format(wait="an hour"),
                 auth.INVITE_GUESSES_LIMITED.format(wait="an hour"), *notes):
        assert "—" not in text and "–" not in text and "!" not in text and " - " not in text
        assert "kicker" not in text.lower() and "paused" not in text


def test_the_invite_alarm_email_has_its_own_subject_and_body(monkeypatch):
    """The alarm goes through send_safety_alert, but it is neither a rider's
    red flag nor a failing reply: its own subject, the note, and where to
    look, never the red-flag template with blank rider fields."""
    import asyncio

    sent = []

    async def fake_send(to, subject, text_body, from_address=None):
        sent.append((to, subject, text_body))
        return True

    monkeypatch.setattr(email_service, "send", fake_send)
    note = auth._invite_abuse_note(37)
    assert len(note) <= email_service.SAFETY_ALERT_EXCERPT_CHARS
    assert asyncio.run(email_service.send_safety_alert("invite_abuse", "", "", note)) is True
    (_, subject, body), = sent
    assert subject == "Forma ops alert: someone is guessing invite codes"
    assert body.startswith(note)
    assert "Railway logs" in body and "Invite guessing alarm" in body
    for wrong in ("red flag", "Rider id", "Rider email", "Coach replies are failing"):
        assert wrong not in body and wrong not in subject
    for text in (subject, body):
        assert "—" not in text and "–" not in text and "!" not in text
