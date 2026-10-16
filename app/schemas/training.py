"""Pydantic schemas for training plans, phases, workouts, and steps."""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.session_naming import workout_display_name


# --- Workout Steps ---

class WorkoutStepResponse(BaseModel):
    """A single step within a structured workout."""
    id: str
    step_order: int
    step_type: str
    duration_seconds: int
    power_target_pct: float | None = None
    power_low_pct: float | None = None
    power_high_pct: float | None = None
    cadence_target: int | None = None
    repeat_count: int | None = None
    notes: str | None = None

    model_config = ConfigDict(from_attributes=True)


# --- Workouts ---

class ActualRideSummary(BaseModel):
    """Summary of the actual ride linked to a workout (the 'did' side)."""
    id: str
    title: str | None = None
    ride_date: datetime
    moving_time_seconds: int | None = None
    duration_seconds: int | None = None
    distance_meters: float | None = None
    elevation_gain_meters: float | None = None
    average_power: float | None = None
    normalized_power: float | None = None
    intensity_factor: float | None = None
    tss: float | None = None
    average_hr: int | None = None

    model_config = ConfigDict(from_attributes=True)


class WorkoutResponse(BaseModel):
    """Summary of a planned workout."""
    id: str
    # None for a session booked outside a plan; it decides the shown name.
    phase_id: str | None = None
    scheduled_date: date
    title: str
    description: str | None = None
    workout_type: str
    planned_duration_seconds: int | None = None
    planned_tss: float | None = None
    planned_if: float | None = None
    status: str
    actual_ride_id: str | None = None
    actual_ride: ActualRideSummary | None = None
    execution_score: float | None = None
    execution_feedback: str | None = None
    execution_adjustments: list[dict[str, Any]] | None = None
    execution_assessed_at: datetime | None = None
    sort_order: int = 0

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def _apply_canonical_name(self) -> "WorkoutResponse":
        # Single source of truth for the session name: this is the name shown
        # everywhere and the one Forma refers to (workout_display_name).
        self.title = workout_display_name(
            self.workout_type, self.id, self.title, self.phase_id
        )
        return self


class WorkoutAssessmentResponse(BaseModel):
    """Score + supportive feedback + suggested adjustments for a completed workout."""
    workout_id: str
    score: float
    feedback: str
    adjustments: list[dict[str, Any]] = []
    assessed_at: datetime | None = None


class WorkoutDetailResponse(WorkoutResponse):
    """Workout with full step details."""
    steps: list[WorkoutStepResponse] = []


class WorkoutUpdateRequest(BaseModel):
    """Update a workout's status or link a ride."""
    status: str | None = None
    actual_ride_id: str | None = None


class WorkoutLinkRideRequest(BaseModel):
    """Link an actual ride to a workout."""
    ride_id: str


# --- Training Phases ---

class TrainingPhaseResponse(BaseModel):
    """A phase within a training plan."""
    id: str
    phase_type: str
    start_date: date
    end_date: date
    target_weekly_tss: float | None = None
    target_weekly_hours: float | None = None
    focus: str | None = None
    sort_order: int = 0
    workout_count: int = 0

    model_config = ConfigDict(from_attributes=True)


# --- Training Plans ---

class PlanGenerateRequest(BaseModel):
    """Request to generate a new training plan."""
    goal_event_id: str | None = None
    periodization_model: str = Field(
        "traditional",
        description="traditional, polarized, or sweet_spot",
    )
    name: str | None = None


class TrainingPlanResponse(BaseModel):
    """Training plan summary."""
    id: str
    name: str
    goal_event_id: str | None = None
    start_date: date
    end_date: date
    status: str
    periodization_model: str
    created_at: datetime | None = None
    total_weeks: int = 0
    phase_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class TrainingPlanDetailResponse(TrainingPlanResponse):
    """Plan with phases."""
    phases: list[TrainingPhaseResponse] = []


class PlanListResponse(BaseModel):
    """List of training plans."""
    plans: list[TrainingPlanResponse]
    total: int


class PlanWorkoutsResponse(BaseModel):
    """Calendar view of plan workouts."""
    plan_id: str
    workouts: list[WorkoutResponse]
    total: int


# --- Exports ---

class ExportResponse(BaseModel):
    """Export metadata."""
    filename: str
    format: str
    content_type: str
    size_bytes: int
