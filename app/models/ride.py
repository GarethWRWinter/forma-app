import enum

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy import JSON as SA_JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, generate_uuid


class RideSource(str, enum.Enum):
    fit_upload = "fit_upload"
    strava = "strava"
    trainingpeaks = "trainingpeaks"
    manual = "manual"
    in_app = "in_app"
    dropbox = "dropbox"


class Ride(TimestampMixin, Base):
    __tablename__ = "rides"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), index=True, nullable=False
    )
    external_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    source: Mapped[str] = mapped_column(Enum(RideSource), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ride_date: Mapped[str] = mapped_column(DateTime, index=True, nullable=False)

    # Duration & distance
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    moving_time_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    distance_meters: Mapped[float | None] = mapped_column(Float, nullable=True)
    elevation_gain_meters: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Power
    average_power: Mapped[float | None] = mapped_column(Float, nullable=True)
    normalized_power: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_power: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Heart rate
    average_hr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_hr: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Other
    average_cadence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    average_speed: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Calculated metrics
    intensity_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    variability_index: Mapped[float | None] = mapped_column(Float, nullable=True)
    tss: Mapped[float | None] = mapped_column(Float, nullable=True)
    ftp_at_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    calories: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Links
    workout_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workouts.id", use_alter=True), nullable=True
    )
    fit_file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    strava_activity_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)

    # Strava social/competitive data
    achievement_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kudos_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Coach debrief (Forma's post-ride feedback)
    debrief_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Cached time-in-zone summary for list views:
    # {"z": [s1..s7 seconds], "dom": "z3", "shape": [[h0_100, zone1_7], ...]}
    # or {"none": true} when no power data. "shape" is the ride's power over
    # time in ~48 buckets — the thumbnail fingerprint. Computed lazily on
    # first list render, then free forever.
    zone_summary: Mapped[dict | None] = mapped_column(SA_JSON, nullable=True)

    # Forma's voice on the list: a name for the ride and a one-line story
    # that teaches. Written once by the coach (Haiku), never regenerated.
    forma_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    story: Mapped[str | None] = mapped_column(Text, nullable=True)
    debrief_generated_at: Mapped[str | None] = mapped_column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="rides")
    data_points = relationship("RideData", back_populates="ride", cascade="all, delete-orphan")
    segment_efforts = relationship("SegmentEffort", back_populates="ride", cascade="all, delete-orphan")
    workout = relationship("Workout", foreign_keys=[workout_id])


class RideData(Base):
    __tablename__ = "ride_data"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ride_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("rides.id"), index=True, nullable=False
    )
    timestamp: Mapped[str | None] = mapped_column(DateTime, nullable=True)
    elapsed_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Metrics
    power: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heart_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cadence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    speed: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance: Mapped[float | None] = mapped_column(Float, nullable=True)

    # GPS
    altitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Additional
    temperature: Mapped[int | None] = mapped_column(Integer, nullable=True)
    left_right_balance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    torque: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Relationships
    ride = relationship("Ride", back_populates="data_points")

    __table_args__ = (
        Index("ix_ride_data_ride_elapsed", "ride_id", "elapsed_seconds"),
    )
