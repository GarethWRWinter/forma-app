"""The founding ledger: every rider number ever issued, reserved forever.

Rows are never deleted — not even when the rider's account is purged — so a
number can never be reissued. Deliberately holds no personal data (no email,
no name): after a GDPR purge the user_id points at nothing, which is exactly
the anonymisation we want while the number stays taken.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FoundingLedger(Base):
    __tablename__ = "founding_ledger"

    number: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
