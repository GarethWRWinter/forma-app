import enum
from datetime import datetime

from sqlalchemy import Date, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy import JSON as SA_JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, generate_uuid


class PrimaryGoal(str, enum.Enum):
    build_fitness = "build_fitness"
    target_event = "target_event"
    improve_ftp = "improve_ftp"
    race = "race"
    learn_skills = "learn_skills"


class IndoorOutdoorPreference(str, enum.Enum):
    indoor = "indoor"
    outdoor = "outdoor"
    both = "both"


class EventType(str, enum.Enum):
    road_race = "road_race"
    crit = "crit"
    time_trial = "time_trial"
    gran_fondo = "gran_fondo"
    sportive = "sportive"
    gravel = "gravel"
    mtb = "mtb"
    hill_climb = "hill_climb"
    stage_race = "stage_race"
    charity_ride = "charity_ride"
    century = "century"


class EventPriority(str, enum.Enum):
    a_race = "a_race"
    b_race = "b_race"
    c_race = "c_race"


class GoalStatus(str, enum.Enum):
    upcoming = "upcoming"
    completed = "completed"
    dnf = "dnf"
    dns = "dns"


class OnboardingResponse(Base):
    __tablename__ = "onboarding_responses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    primary_goal: Mapped[str] = mapped_column(Enum(PrimaryGoal), nullable=False)
    secondary_goals: Mapped[dict | None] = mapped_column(SA_JSON, nullable=True)
    current_weekly_volume_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    years_cycling: Mapped[int | None] = mapped_column(Integer, nullable=True)
    indoor_outdoor_preference: Mapped[str | None] = mapped_column(
        Enum(IndoorOutdoorPreference), nullable=True
    )
    completed_at: Mapped[str | None] = mapped_column(DateTime, nullable=True)

    user = relationship("User", back_populates="onboarding_responses")


class GoalEvent(Base):
    __tablename__ = "goal_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    event_name: Mapped[str] = mapped_column(String(255), nullable=False)
    event_date: Mapped[str] = mapped_column(Date, nullable=False)
    event_type: Mapped[str] = mapped_column(Enum(EventType), nullable=False)
    priority: Mapped[str] = mapped_column(Enum(EventPriority), nullable=False)
    target_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The soul of the goal, written during crafting with the coach.
    # why: the emotional why in the rider's own words (the fuel).
    # becoming: who this pursuit is turning them into (the identity line).
    why: Mapped[str | None] = mapped_column(Text, nullable=True)
    becoming: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # The coach's read: Forma's honest verdict on the goal (size, soul,
    # bridge), regenerated on demand as the data changes.
    coach_read: Mapped[str | None] = mapped_column(Text, nullable=True)
    coach_read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Route / challenge data
    route_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    gpx_file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    route_data: Mapped[dict | None] = mapped_column(SA_JSON, nullable=True)

    # Assessment / post-event fields
    status: Mapped[str] = mapped_column(
        Enum(GoalStatus), default=GoalStatus.upcoming, server_default="upcoming"
    )
    actual_ride_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("rides.id"), nullable=True
    )
    finish_time_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finish_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finish_position_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overall_satisfaction: Mapped[int | None] = mapped_column(Integer, nullable=True)
    perceived_exertion: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assessment_data: Mapped[dict | None] = mapped_column(SA_JSON, nullable=True)
    assessment_completed_at: Mapped[str | None] = mapped_column(DateTime, nullable=True)

    user = relationship("User", back_populates="goal_events")
    actual_ride = relationship("Ride", foreign_keys=[actual_ride_id])
