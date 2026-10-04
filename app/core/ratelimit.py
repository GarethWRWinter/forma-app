"""Lightweight in-memory rate limiting for sensitive endpoints (auth).

A per-client sliding window, no external dependency — enough to blunt online
password brute-force and signup abuse on a single-instance deployment. It is
process-local: with multiple backend instances each holds its own counter, so
before scaling horizontally, swap this for a shared store (Redis). Combined
with bcrypt's per-attempt cost, this makes online guessing impractical.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request


class _SlidingWindow:
    def __init__(self, max_hits: int, window_seconds: float):
        self.max = max_hits
        self.window = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> int | None:
        """Record a hit. Returns None if allowed, or seconds-to-retry if over."""
        now = time.monotonic()
        with self._lock:
            dq = self._hits[key]
            while dq and dq[0] <= now - self.window:
                dq.popleft()
            if len(dq) >= self.max:
                return int(self.window - (now - dq[0])) + 1
            dq.append(now)
            return None


def _client_key(request: Request) -> str:
    # The LAST X-Forwarded-For hop is the address Railway's proxy saw. The
    # first is whatever the client chose to send, so keying on it let anyone
    # dodge every limit by sending a new fake address each time (launch
    # audit, 4 Oct 2026).
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(max_hits: int, window_seconds: float):
    """FastAPI dependency: allow `max_hits` per client per window, else 429."""
    window = _SlidingWindow(max_hits, window_seconds)

    def _dep(request: Request) -> None:
        retry = window.hit(_client_key(request))
        if retry is not None:
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Please wait and try again.",
                headers={"Retry-After": str(retry)},
            )

    return _dep
