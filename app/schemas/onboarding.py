"""Pydantic schemas for onboarding quiz and goal events."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


# --- Onboarding Quiz ---

class OnboardingQuizRequest(BaseModel):
    """Submit onboarding quiz answers."""
    primary_goal: str = Field(
        ...,
        description="Primary goal: build_fitness, target_event, improve_ftp, race, learn_skills",
    )
    secondary_goals: list[str] | None = Field(
        None, description="Optional secondary goals"
    )
    current_weekly_volume_hours: float | None = Field(
        None, ge=0, le=40, description="Current weekly training hours"
    )
    years_cycling: int | None = Field(
        None, ge=0, le=80, description="Years of cycling experience"
    )
    indoor_outdoor_preference: str | None = Field(
        None, description="indoor, outdoor, or both"
    )


class OnboardingStatusResponse(BaseModel):
    """Onboarding completion status."""
    completed: bool
    completed_at: datetime | None = None
    primary_goal: str | None = None
    secondary_goals: list[str] | None = None

    model_config = ConfigDict(from_attributes=True)


class OnboardingResponse(BaseModel):
    """Full onboarding response detail."""
    id: str
    primary_goal: str
    secondary_goals: list[str] | None = None
    current_weekly_volume_hours: float | None = None
    years_cycling: int | None = None
    indoor_outdoor_preference: str | None = None
    completed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


# --- Goal Events ---

class GoalEventCreate(BaseModel):
    """Create a new goal event."""
    event_name: str = Field(..., min_length=1, max_length=255)
    event_date: date
    event_type: str = Field(
        ...,
        description="road_race, crit, time_trial, gran_fondo, sportive, gravel, mtb",
    )
    priority: str = Field(
        ..., description="a_race, b_race, c_race"
    )
    target_duration_minutes: int | None = Field(None, ge=1)
    notes: str | None = None
    why: str | None = Field(None, description="The emotional why, in the rider's own words")
    becoming: str | None = Field(None, max_length=255, description="Who this pursuit is turning the rider into")
    route_url: str | None = Field(None, max_length=2048, description="URL to Strava route/segment, race website, etc.")


class GoalEventUpdate(BaseModel):
    """Update an existing goal event."""
    event_name: str | None = Field(None, min_length=1, max_length=255)
    event_date: date | None = None
    event_type: str | None = None
    priority: str | None = None
    target_duration_minutes: int | None = None
    notes: str | None = None
    why: str | None = None
    becoming: str | None = Field(None, max_length=255)
    route_url: str | None = Field(None, max_length=2048)


class GoalAssessmentSubmit(BaseModel):
    """Submit post-event assessment."""
    status: str = Field(..., description="completed, dnf, or dns")
    actual_ride_id: str | None = None
    finish_time_seconds: int | None = None
    finish_position: int | None = None
    finish_position_total: int | None = None
    overall_satisfaction: int | None = Field(None, ge=1, le=10)
    perceived_exertion: int | None = Field(None, ge=1, le=10)
    assessment_data: dict | None = None


class PlannedVsActualResponse(BaseModel):
    """Comparison of planned targets vs actual ride data."""
    target_time_seconds: int | None = None
    actual_time_seconds: int | None = None
    target_np: int | None = None
    actual_np: int | None = None
    actual_if: float | None = None
    actual_vi: float | None = None
    actual_tss: float | None = None
    pacing_analysis: str | None = None
    time_delta_seconds: int | None = None
    time_delta_pct: float | None = None


class GoalEventResponse(BaseModel):
    """Goal event response."""
    id: str
    event_name: str
    event_date: date
    event_type: str
    priority: str
    target_duration_minutes: int | None = None
    notes: str | None = None
    why: str | None = None
    becoming: str | None = None
    days_until: int | None = None
    route_url: str | None = None
    gpx_file_path: str | None = None
    route_data: dict | None = None
    status: str | None = "upcoming"
    actual_ride_id: str | None = None
    finish_time_seconds: int | None = None
    finish_position: int | None = None
    finish_position_total: int | None = None
    overall_satisfaction: int | None = None
    perceived_exertion: int | None = None
    assessment_data: dict | None = None
    assessment_completed_at: datetime | None = None
    needs_assessment: bool = False

    model_config = ConfigDict(from_attributes=True)


class GoalEventListResponse(BaseModel):
    """List of goal events."""
    goals: list[GoalEventResponse]
    total: int


class GoalReadinessResponse(BaseModel):
    """Fitness readiness assessment for a goal event."""
    goal_id: str
    current_ctl: float
    target_ctl: float
    current_ftp: int | None = None
    w_per_kg: float | None = None
    current_tsb: float
    projected_tsb_on_event: float | None = None
    days_until: int
    readiness_score: int = Field(ge=0, le=100)
    readiness_label: str
    recommendations: list[str]


# --- Race Day Projection ---

class PerformanceEstimate(BaseModel):
    """Estimated performance for a course at a given fitness level."""
    estimated_time_seconds: int
    avg_power_watts: int
    avg_speed_kph: float
    ftp_used: int
    ctl_used: float


class PacingSegment(BaseModel):
    """Per-segment pacing target."""
    distance_km: float
    elevation_m: float
    gradient_pct: float
    target_power_watts: int
    target_power_pct_ftp: int
    estimated_speed_kph: float
    zone: str
    zone_name: str


class FitnessTrajectoryPoint(BaseModel):
    """Projected fitness at a point in time."""
    date: str
    ctl: float
    ftp: int
    label: str | None = None


class PerformanceImprovement(BaseModel):
    """Delta between current and projected performance."""
    time_saved_seconds: int
    speed_gain_kph: float
    ftp_gain_watts: int


class RaceProjectionResponse(BaseModel):
    """Complete race day projection with pacing and fitness trajectory."""
    current_performance: PerformanceEstimate
    projected_performance: PerformanceEstimate | None = None
    improvement: PerformanceImprovement | None = None
    pacing_strategy: list[PacingSegment]
    fitness_trajectory: list[FitnessTrajectoryPoint]
