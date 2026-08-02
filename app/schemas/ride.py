from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RideDataPoint(BaseModel):
    elapsed_seconds: int | None = None
    power: int | None = None
    heart_rate: int | None = None
    cadence: int | None = None
    speed: float | None = None
    distance: float | None = None
    altitude: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    temperature: int | None = None
    left_right_balance: int | None = None
    torque: float | None = None


class RideResponse(BaseModel):
    id: str
    user_id: str
    source: str
    title: str | None = None
    ride_date: datetime
    duration_seconds: int | None = None
    moving_time_seconds: int | None = None
    distance_meters: float | None = None
    elevation_gain_meters: float | None = None
    average_power: float | None = None
    normalized_power: float | None = None
    max_power: int | None = None
    average_hr: int | None = None
    max_hr: int | None = None
    average_cadence: int | None = None
    average_speed: float | None = None
    intensity_factor: float | None = None
    variability_index: float | None = None
    tss: float | None = None
    ftp_at_time: int | None = None
    calories: int | None = None
    workout_id: str | None = None
    achievement_count: int | None = None
    pr_count: int | None = None
    kudos_count: int | None = None
    zone_summary: dict | None = None
    forma_title: str | None = None
    story: str | None = None
    location_name: str | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class RideListResponse(BaseModel):
    rides: list[RideResponse]
    total: int
    page: int
    per_page: int


class RideDataResponse(BaseModel):
    ride_id: str
    resolution: str
    data_points: list[RideDataPoint]
    total_points: int


class RideRecordRequest(BaseModel):
    """For in-app workout recording - raw JSON time-series from workout player."""
    title: str
    ride_date: datetime
    workout_id: str | None = None
    data_points: list[RideDataPoint]


class PowerCurvePoint(BaseModel):
    duration_seconds: int
    duration_label: str
    best_power: float


class PowerCurveResponse(BaseModel):
    ride_id: str | None = None
    points: list[PowerCurvePoint]
