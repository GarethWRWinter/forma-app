import enum
from datetime import datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, Float, Integer, String
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, generate_uuid


class ExperienceLevel(str, enum.Enum):
    beginner = "beginner"
    intermediate = "intermediate"
    advanced = "advanced"
    elite = "elite"


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Account lifecycle. is_active gates login (suspension / deactivation);
    # deleted_at marks a GDPR erasure request — the account is locked out
    # immediately and hard-purged after the retention window.
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default="true"
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Soft gate: login works unverified (email trouble must never lock a
    # rider out), the app nudges until this flips.
    email_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )

    # Physical
    weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    height_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    date_of_birth: Mapped[str | None] = mapped_column(Date, nullable=True)

    # Cycling metrics
    ftp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_hr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resting_hr: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Profile
    experience_level: Mapped[str | None] = mapped_column(
        Enum(ExperienceLevel), nullable=True
    )
    has_power_meter: Mapped[bool] = mapped_column(Boolean, default=False)
    has_smart_trainer: Mapped[bool] = mapped_column(Boolean, default=False)
    has_hr_monitor: Mapped[bool] = mapped_column(Boolean, default=False)
    weekly_hours_available: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Training schedule preferences (day numbers: 0=Mon, 6=Sun)
    # hard_days: days for intensity sessions (e.g. [5, 6] for Sat/Sun)
    # rest_days: days with no training (e.g. [4] for Friday)
    preferred_hard_days: Mapped[list | None] = mapped_column(JSON, nullable=True)
    rest_days: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Coach personalisation — the rider's coach: their name for them, their
    # face (pen-sketch avatar key), and their communication tone.
    coach_name: Mapped[str] = mapped_column(String(50), nullable=False, server_default="Forma")
    coach_avatar: Mapped[str] = mapped_column(String(32), nullable=False, server_default="coach_01")
    coach_tone: Mapped[str] = mapped_column(String(24), nullable=False, server_default="balanced")

    # Relationships
    rides = relationship("Ride", back_populates="user", lazy="dynamic")
    training_plans = relationship("TrainingPlan", back_populates="user", lazy="dynamic")
    chat_sessions = relationship("ChatSession", back_populates="user", lazy="dynamic")
    onboarding_responses = relationship("OnboardingResponse", back_populates="user")
    goal_events = relationship("GoalEvent", back_populates="user")
    daily_metrics = relationship("DailyMetrics", back_populates="user", lazy="dynamic")
    workouts = relationship("Workout", back_populates="user", lazy="dynamic")
