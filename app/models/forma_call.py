"""forma_calls — the cost/observability ledger for every Claude call.

One row per Forma call (success or error), written by app.core.forma_core
in its own session. This is what makes the per-user margin measurable:
the PRD's $8/user/month alert threshold reads from this table.
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String

from app.models.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class FormaCall(Base):
    __tablename__ = "forma_calls"

    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, index=True, nullable=False)
    ts = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)
    task = Column(String, nullable=False)          # routing-table key (chat, nudge, ...)
    surface = Column(String, nullable=True)        # where in the product it fired
    model = Column(String, nullable=False)
    input_tokens = Column(Integer, default=0, nullable=False)
    output_tokens = Column(Integer, default=0, nullable=False)
    cache_read_tokens = Column(Integer, default=0, nullable=False)
    cache_write_tokens = Column(Integer, default=0, nullable=False)
    cost_cents = Column(Float, default=0.0, nullable=False)
    latency_ms = Column(Integer, default=0, nullable=False)
    error = Column(Boolean, default=False, nullable=False)
    # Which SAFETY_LAW block the call carried (None for the tasks exempt from
    # it, and for calls made before it existed).
    safety_law_version = Column(String(20), nullable=True)
