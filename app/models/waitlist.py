"""The waitlist: the signals the Founding Hundred launches into."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, generate_uuid


class WaitlistEntry(Base):
    __tablename__ = "waitlist"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    # Letter 0 delivery: False until Postmark accepts it (pending-approval
    # sandboxing can defer sends; the admin backfill retries these).
    letter0_sent: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
