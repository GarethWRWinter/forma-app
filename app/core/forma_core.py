"""forma-core — the single funnel for every Claude call Forma makes.

The PRD's non-negotiable architecture: one place that owns model routing,
prompt caching, cost logging, and the provider client. There is no other
path to Claude — services never construct their own anthropic client.

Every call:
  1. resolves its model + max_tokens from the TASKS routing table
     (callers may override max_tokens but never hardcode a model),
  2. gets `cache_control` applied to the stable system prefix,
  3. is logged to `forma_calls` (tokens, cost in cents, latency, surface)
     in its OWN db session, so a caller's rollback never loses the record.

Usage:
    from app.core import forma_core

    resp = forma_core.call(
        user_id=user.id, task="nudge", surface="dashboard",
        system=persona + NUDGE_PROMPT, messages=[...],
    )

    with forma_core.stream(
        user_id=user.id, task="chat", surface="coach",
        system=system_blocks, messages=messages, tools=COACH_TOOLS,
    ) as s:
        for event in s: ...
        final = s.get_final_message()
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)

SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5-20251001"

# Warn the rider once their month-to-date spend passes this fraction of the cap.
BUDGET_SOFT_RATIO = 0.80

# Shown to the rider when the hard cap blocks a conversational turn.
QUOTA_MESSAGE = (
    "You've reached your Forma conversation quota for this month. Your training "
    "plan, rides, and data are all still here — we'll pick the conversation back "
    "up when your quota resets at the start of next month."
)


class BudgetExceededError(Exception):
    """Raised by call()/stream() when a user's month-to-date spend has hit the
    hard cap. Callers on the conversational path surface QUOTA_MESSAGE; cheap
    background tasks degrade to their deterministic fallbacks."""

    def __init__(self, spent_cents: float, budget_cents: int):
        self.spent_cents = spent_cents
        self.budget_cents = budget_cents
        super().__init__(
            f"Monthly Forma budget exceeded: {spent_cents:.1f}c / {budget_cents}c"
        )


@dataclass(frozen=True)
class TaskConfig:
    model: str
    max_tokens: int


# The routing table — the ONLY place a model name is bound to a job.
# Sonnet where Forma reasons as a coach, Haiku for extraction and
# short interpretive lines (mirrors the PRD cost model).
TASKS: dict[str, TaskConfig] = {
    "chat": TaskConfig(SONNET, 2048),          # streaming coach chat (tools)
    "chat_voice": TaskConfig(SONNET, 1024),    # voice chat — shorter replies
    "chat_sync": TaskConfig(SONNET, 2048),     # non-streaming chat turn
    "debrief": TaskConfig(SONNET, 500),        # post-ride debrief
    "assessment": TaskConfig(SONNET, 1500),    # workout execution assessment
    "nudge": TaskConfig(HAIKU, 200),           # daily dashboard nudge
    "explain": TaskConfig(HAIKU, 200),         # metric explanation
    "memory_extraction": TaskConfig(HAIKU, 1500),
    "memory_reading": TaskConfig(HAIKU, 220),  # Brain page narration
    "ride_story": TaskConfig(HAIKU, 160),      # ride title + one-line story
}

# USD per million tokens, matched by model-id prefix. Cache reads bill at
# 0.1x input, cache writes at 1.25x (5-minute TTL).
_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-4-8": (5.00, 25.00),
}

_client_instance: anthropic.Anthropic | None = None


def _client() -> anthropic.Anthropic:
    """One provider client for the whole app — swap providers here."""
    global _client_instance
    if _client_instance is None:
        _client_instance = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client_instance


def _prices_for(model: str) -> tuple[float, float]:
    for prefix, prices in _PRICES_PER_MTOK.items():
        if model.startswith(prefix):
            return prices
    logger.warning("forma-core: no price entry for model %s — logging zero cost", model)
    return (0.0, 0.0)


def cost_cents(model: str, usage) -> float:
    """Cost of one call in US cents from an anthropic Usage object."""
    in_price, out_price = _prices_for(model)
    tokens_in = getattr(usage, "input_tokens", 0) or 0
    tokens_out = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    usd = (
        tokens_in * in_price
        + cache_read * in_price * 0.10
        + cache_write * in_price * 1.25
        + tokens_out * out_price
    ) / 1_000_000
    return usd * 100


def _normalize_system(system) -> list:
    """String system prompts become a cached block; block lists pass through.

    Callers that already manage their own cache breakpoints (the chat
    service's [cached education, dynamic context] split) are untouched.
    """
    if isinstance(system, str):
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    return system


def _log(
    user_id: str,
    task: str,
    surface: str | None,
    model: str,
    usage,
    latency_ms: int,
    error: bool = False,
) -> None:
    """Write a forma_calls row in its own session — never on the caller's
    transaction, so cost records survive caller rollbacks and vice versa."""
    try:
        from app.database import SessionLocal
        from app.models.forma_call import FormaCall

        row = FormaCall(
            user_id=user_id,
            task=task,
            surface=surface,
            model=model,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cost_cents=cost_cents(model, usage) if usage is not None else 0.0,
            latency_ms=latency_ms,
            error=error,
        )
        db = SessionLocal()
        try:
            db.add(row)
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — cost logging must never break Forma
        logger.exception("forma-core: failed to log call (task=%s user=%s)", task, user_id)


# ── Per-user monthly budget (reads the forma_calls ledger) ──────────────────


@dataclass(frozen=True)
class BudgetStatus:
    spent_cents: float
    budget_cents: int
    ratio: float
    state: str  # "ok" | "soft" | "hard"


def _month_start() -> datetime:
    """First instant of the current calendar month, naive UTC — matches the
    naive `forma_calls.ts` default (datetime.utcnow)."""
    now = datetime.utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_to_date_cents(user_id: str) -> float:
    """A user's total Forma spend so far this calendar month, in US cents."""
    from sqlalchemy import func

    from app.database import SessionLocal
    from app.models.forma_call import FormaCall

    db = SessionLocal()
    try:
        total = (
            db.query(func.coalesce(func.sum(FormaCall.cost_cents), 0.0))
            .filter(FormaCall.user_id == user_id, FormaCall.ts >= _month_start())
            .scalar()
        )
        return float(total or 0.0)
    finally:
        db.close()


def budget_status(user_id: str) -> BudgetStatus:
    """Month-to-date spend vs the configured cap, with a coarse state."""
    budget = settings.monthly_budget_cents
    spent = month_to_date_cents(user_id)
    ratio = (spent / budget) if budget else 0.0
    state = "hard" if ratio >= 1.0 else "soft" if ratio >= BUDGET_SOFT_RATIO else "ok"
    return BudgetStatus(spent_cents=spent, budget_cents=budget, ratio=ratio, state=state)


def _enforce_budget(user_id: str, task: str) -> None:
    """Block the call if the rider is over their hard cap; warn once at soft."""
    status = budget_status(user_id)
    if status.state == "hard":
        logger.warning(
            "forma-core: budget hard-cap hit (user=%s task=%s spent=%.1fc/%dc)",
            user_id, task, status.spent_cents, status.budget_cents,
        )
        raise BudgetExceededError(status.spent_cents, status.budget_cents)
    if status.state == "soft":
        logger.info(
            "forma-core: budget soft-cap (user=%s spent=%.1fc/%dc, %.0f%%)",
            user_id, status.spent_cents, status.budget_cents, status.ratio * 100,
        )


def call(
    *,
    user_id: str,
    task: str,
    system,
    messages: list,
    surface: str | None = None,
    tools: list | None = None,
    max_tokens: int | None = None,
    enforce_budget: bool = True,
):
    """One non-streaming Forma call. Returns the anthropic Message.

    Raises BudgetExceededError (before spending anything) when the rider is
    over their monthly cap and enforce_budget is left on."""
    if enforce_budget:
        _enforce_budget(user_id, task)
    cfg = TASKS[task]
    kwargs = {}
    if tools:
        kwargs["tools"] = tools
    t0 = time.monotonic()
    try:
        resp = _client().messages.create(
            model=cfg.model,
            max_tokens=max_tokens or cfg.max_tokens,
            system=_normalize_system(system),
            messages=messages,
            **kwargs,
        )
    except Exception:
        _log(user_id, task, surface, cfg.model, None, int((time.monotonic() - t0) * 1000), error=True)
        raise
    _log(user_id, task, surface, cfg.model, resp.usage, int((time.monotonic() - t0) * 1000))
    return resp


@contextmanager
def stream(
    *,
    user_id: str,
    task: str,
    system,
    messages: list,
    surface: str | None = None,
    tools: list | None = None,
    max_tokens: int | None = None,
    enforce_budget: bool = True,
):
    """One streaming Forma call. Yields the anthropic MessageStream;
    usage is logged from the final message when the block exits.

    Raises BudgetExceededError (before opening the stream) when the rider is
    over their monthly cap and enforce_budget is left on."""
    if enforce_budget:
        _enforce_budget(user_id, task)
    cfg = TASKS[task]
    kwargs = {}
    if tools:
        kwargs["tools"] = tools
    t0 = time.monotonic()
    try:
        with _client().messages.stream(
            model=cfg.model,
            max_tokens=max_tokens or cfg.max_tokens,
            system=_normalize_system(system),
            messages=messages,
            **kwargs,
        ) as s:
            yield s
            final = s.get_final_message()
    except Exception:
        _log(user_id, task, surface, cfg.model, None, int((time.monotonic() - t0) * 1000), error=True)
        raise
    _log(user_id, task, surface, cfg.model, final.usage, int((time.monotonic() - t0) * 1000))
