"""Lightweight in-memory rate limiting for sensitive endpoints (auth).

A per-client sliding window, no external dependency: enough to blunt online
password brute-force and signup abuse on a single-instance deployment. It is
process-local: with multiple backend instances each holds its own counter, so
before scaling horizontally, swap this for a shared store (Redis). Combined
with bcrypt's per-attempt cost, this makes online guessing impractical.

What is counted, and against what:
- every request, per caller's address (client_ip), on every limited endpoint;
- per account email (EmailLimit): every attempt on login and forgot-password,
  only the failed ones on sign-up, as a backstop that holds even when the
  address can't be trusted;
- wrong answers to a guessable secret, per address (FailureLimit): wrong
  invite codes;
- the same wrong answers from everyone together (SurgeAlarm), which never
  refuses anyone: it says when to tell Gareth that the guessing is spread
  over more addresses than the per-address limit can hold.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import threading
import time
from collections import deque
from typing import NamedTuple

from fastapi import HTTPException, Request

from app.config import settings

# Past this many keys, a hit first sweeps out the ones with nothing left in
# their window, so a stream of new addresses or emails can't grow the table
# without end.
_SWEEP_AT = 10_000

TOO_MANY = "Too many attempts. Please wait and try again."


class _SlidingWindow:
    def __init__(self, max_hits: int, window_seconds: float):
        self.max = max_hits
        self.window = window_seconds
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> int | None:
        """Record a hit. Returns None if allowed, or seconds-to-retry if over."""
        now = time.monotonic()
        with self._lock:
            if len(self._hits) >= _SWEEP_AT and key not in self._hits:
                self._sweep(now)
            dq = self._hits.setdefault(key, deque())
            while dq and dq[0] <= now - self.window:
                dq.popleft()
            if len(dq) >= self.max:
                return max(1, math.ceil(self.window - (now - dq[0])))
            dq.append(now)
            return None

    def retry_after(self, key: str) -> int | None:
        """Seconds until `key` may hit again, or None if it may now. Records
        nothing."""
        now = time.monotonic()
        with self._lock:
            dq = self._hits.get(key)
            if not dq:
                return None
            while dq and dq[0] <= now - self.window:
                dq.popleft()
            if len(dq) >= self.max:
                return max(1, math.ceil(self.window - (now - dq[0])))
            return None

    def _sweep(self, now: float) -> None:
        cutoff = now - self.window
        for key in [k for k, dq in self._hits.items() if not dq or dq[-1] <= cutoff]:
            del self._hits[key]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def __len__(self) -> int:
        return len(self._hits)


# Stamped by the Next.js frontend (frontend/src/middleware.ts) on every /api/*
# request it forwards, carrying settings.edge_secret (FORMA_EDGE_SECRET).
EDGE_HEADER = "x-forma-edge"


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def forwarded_hops(request: Request) -> list[str]:
    """The X-Forwarded-For hops, left to right, blanks dropped."""
    xff = request.headers.get("x-forwarded-for") or ""
    return [hop.strip() for hop in xff.split(",") if hop.strip()]


def via_edge(request: Request) -> bool:
    """Whether this request came through Forma's own frontend, so that its
    first X-Forwarded-For hop is the rider.

    With settings.edge_secret set: only when x-forma-edge carries it,
    compared in constant time. Only Vercel's server side holds the secret
    (frontend/src/middleware.ts), and Vercel overwrites X-Forwarded-For with the
    address that really connected to it, so a request with the secret has a
    first hop nobody chose. Anything else, a direct call to Railway with
    whatever headers it likes, x-vercel-id included, is not trusted.

    With no secret set, the older rule: any x-vercel-id. A direct caller can
    forge that and pick its own first hop (review round 3, new problem 2),
    which is why the secret exists; the rule stays only so nothing breaks
    before FORMA_EDGE_SECRET is set on both sides."""
    secret = settings.edge_secret
    if secret:
        sent = (request.headers.get(EDGE_HEADER) or "").strip()
        # Hashed first, so the comparison takes the same time whatever the
        # length of the guess.
        return bool(sent) and hmac.compare_digest(_digest(sent), _digest(secret))
    return bool(request.headers.get("x-vercel-id"))


class IpReading(NamedTuple):
    """How client_ip reached its answer, for GET /auth/ip-check."""

    ip: str | None
    hops: int
    via_edge: bool


def read_client_ip(request: Request) -> IpReading:
    """The address the limits key on, with how it was read.

    The browser reaches the API through the Vercel frontend (/api/* is
    forwarded to Railway). Vercel overwrites X-Forwarded-For with the
    rider's real address, and Railway's edge then appends the address that
    connected to it, which is Vercel's. So:

    - Through the frontend (via_edge): the FIRST hop is the rider. The last
      is one of Vercel's shared addresses, and keying on it put every rider
      in one bucket (review R16).
    - Anything else is a direct call to Railway: the LAST hop, the address
      Railway saw. The first is whatever the caller chose to send, so keying
      on it let anyone dodge every limit with a new fake address each time
      (launch audit, 4 Oct 2026).

    With no X-Forwarded-For at all, the connecting address. The address is
    cut to 64 characters, the width of consent_events.ip, and is None when
    there is none."""
    hops = forwarded_hops(request)
    edge = via_edge(request)
    if hops:
        ip = hops[0] if edge else hops[-1]
    else:
        client = getattr(request, "client", None)
        ip = (client.host if client else "") or ""
    return IpReading(ip[:64] or None, len(hops), edge)


def client_ip(request: Request) -> str | None:
    """The address of the person calling, as far as it can be trusted (see
    read_client_ip). Consent rows record the same one
    (safety_service.client_ip)."""
    return read_client_ip(request).ip


def _client_key(request: Request) -> str:
    return client_ip(request) or "unknown"


def _too_many(retry: int, detail: str = TOO_MANY) -> HTTPException:
    return HTTPException(
        status_code=429, detail=detail, headers={"Retry-After": str(retry)}
    )


def rate_limit(max_hits: int, window_seconds: float):
    """FastAPI dependency: allow `max_hits` per client per window, else 429."""
    window = _SlidingWindow(max_hits, window_seconds)

    def _dep(request: Request) -> None:
        retry = window.hit(_client_key(request))
        if retry is not None:
            raise _too_many(retry)

    _dep.window = window  # for tests: reset() between cases
    return _dep


# === Per account, whatever the address ===


def normalise_email(email: str) -> str:
    """The same form the account is stored and looked up under (lower case,
    no surrounding space), so "Rider@x.com" and "rider@x.com " share one
    bucket, exactly as they share one account."""
    return (email or "").strip().lower()


def wait_phrase(seconds: int) -> str:
    """'a minute', 'N minutes' or 'an hour', rounded up, for a 429 the
    rider reads."""
    minutes = max(1, math.ceil(seconds / 60))
    if minutes == 1:
        return "a minute"
    if minutes == 60:
        return "an hour"
    return f"{minutes} minutes"


class EmailLimit:
    """At most `max_hits` per normalised email per window, from anywhere.

    The address limits can be split across many addresses (a botnet, or a
    direct caller choosing its own first hop); this can't. Each check()
    counts one, so it counts whatever its caller checks: every attempt on
    login and forgot-password, only the failed ones on sign-up (where a
    request that would succeed is never checked, so nobody else's junk can
    stop the real owner joining). Known email or not, it answers the same
    way, so it says nothing about which emails have accounts."""

    def __init__(self, max_hits: int, window_seconds: float, detail: str):
        self._window = _SlidingWindow(max_hits, window_seconds)
        # "{wait}" is filled with wait_phrase(seconds to retry).
        self._detail = detail

    def check(self, email: str) -> None:
        retry = self._window.hit(normalise_email(email))
        if retry is not None:
            raise _too_many(retry, self._detail.format(wait=wait_phrase(retry)))

    def reset(self) -> None:
        self._window.reset()


class FailureLimit:
    """At most `max_hits` failures per key per window. Once a key has that
    many, it isn't let through to try at all, right answer or wrong, until
    the oldest falls out of the window: a guesser stopped only on wrong
    answers would learn the right one from the first try that wasn't
    refused. A success records nothing, and every other key is untouched.

    For a secret short enough to guess, such as a one-word invite code,
    keyed on the caller's address. It never closes the door for everyone:
    the shared cap it replaces let one address, inside its own limits, keep
    sign-up paused for every rider with the right code (review round 4, new
    problem C). Guessing spread over many addresses is SurgeAlarm's to
    report."""

    def __init__(self, max_hits: int, window_seconds: float, detail: str):
        self._window = _SlidingWindow(max_hits, window_seconds)
        # "{wait}" is filled with wait_phrase(seconds to retry).
        self._detail = detail

    def check(self, key: str) -> None:
        """Raise 429 while `key` has used up its failures. Call before
        trying; it records nothing."""
        retry = self._window.retry_after(key)
        if retry is not None:
            raise _too_many(retry, self._detail.format(wait=wait_phrase(retry)))

    def failed(self, key: str) -> None:
        """Count one failure against `key`."""
        self._window.hit(key)

    def reset(self) -> None:
        self._window.reset()


class SurgeAlarm:
    """Counts events from everyone together and says when to raise the
    alarm: once more than `threshold` have happened inside the window, and
    then at most once per window however long it goes on. It never refuses
    anyone; the caller decides who to tell.

    Only the latest threshold + 1 events are kept, which is all "more than
    threshold in the window" needs, so a flood can't grow it."""

    def __init__(self, threshold: int, window_seconds: float):
        self.threshold = threshold
        self.window = window_seconds
        self._events: deque[tuple[float, str]] = deque(maxlen=threshold + 1)
        self._last_alarm: float | None = None
        self._lock = threading.Lock()

    def record(self, key: str) -> int | None:
        """Count one event from `key`. Returns None, or, when the alarm is
        due now, how many different keys the latest threshold + 1 events
        came from."""
        now = time.monotonic()
        with self._lock:
            self._events.append((now, key))
            if len(self._events) <= self.threshold:
                return None
            if self._events[0][0] <= now - self.window:
                return None  # the oldest of them is out of the window
            if self._last_alarm is not None and now - self._last_alarm < self.window:
                return None  # already raised within the window
            self._last_alarm = now
            return len({k for _, k in self._events})

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._last_alarm = None
