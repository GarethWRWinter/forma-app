from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.crypto import EncryptedString
from app.models.base import Base, generate_uuid


class StravaToken(Base):
    __tablename__ = "strava_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), unique=True, nullable=False
    )
    access_token: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    refresh_token: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    expires_at: Mapped[str] = mapped_column(DateTime, nullable=False)
    athlete_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scope: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_sync_at: Mapped[str | None] = mapped_column(DateTime, nullable=True)

    # Historical backfill tracking
    backfill_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default=None
    )  # None=not started, "running", "completed", "failed"
    backfill_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    backfill_progress: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    backfill_started_at: Mapped[str | None] = mapped_column(DateTime, nullable=True)
    backfill_completed_at: Mapped[str | None] = mapped_column(DateTime, nullable=True)
    webhook_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class WahooToken(Base):
    __tablename__ = "wahoo_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), unique=True, nullable=False
    )
    access_token: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    refresh_token: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    expires_at: Mapped[str] = mapped_column(DateTime, nullable=False)
    wahoo_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    scope: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_sync_at: Mapped[str | None] = mapped_column(DateTime, nullable=True)

    # Historical import tracking (mirrors StravaToken)
    backfill_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default=None
    )  # None=not started, "running", "completed", "failed"
    backfill_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    backfill_progress: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)


class TrainingPeaksToken(Base):
    __tablename__ = "trainingpeaks_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), unique=True, nullable=False
    )
    access_token: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    refresh_token: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    expires_at: Mapped[str] = mapped_column(DateTime, nullable=False)
    athlete_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_sync_at: Mapped[str | None] = mapped_column(DateTime, nullable=True)


class DropboxToken(Base):
    __tablename__ = "dropbox_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), unique=True, nullable=False
    )
    access_token: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    refresh_token: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    expires_at: Mapped[str] = mapped_column(DateTime, nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    folder_path: Mapped[str] = mapped_column(String(500), default="/cycling", nullable=False)
    last_sync_at: Mapped[str | None] = mapped_column(DateTime, nullable=True)
