"""One row per email the coach sent a quiet rider, so it never sends twice."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, generate_uuid


class OutreachLog(Base):
    __tablename__ = "outreach_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The activation stage the rider was stuck at, and how many quiet days
    # triggered this one. (stage, threshold_days) is sent at most once.
    stage: Mapped[str] = mapped_column(String(20), nullable=False)
    threshold_days: Mapped[int] = mapped_column(Integer, nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
