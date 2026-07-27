from pydantic import BaseModel, EmailStr, ConfigDict, Field


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str
    remember_me: bool = False


class UserUpdate(BaseModel):
    full_name: str | None = None
    weight_kg: float | None = None
    height_cm: float | None = None
    date_of_birth: str | None = None
    ftp: int | None = None
    max_hr: int | None = None
    resting_hr: int | None = None
    experience_level: str | None = None
    has_power_meter: bool | None = None
    has_smart_trainer: bool | None = None
    has_hr_monitor: bool | None = None
    weekly_hours_available: float | None = None
    preferred_hard_days: list[int] | None = None
    rest_days: list[int] | None = None
    coach_name: str | None = None
    coach_avatar: str | None = None
    coach_tone: str | None = None


class UserResponse(BaseModel):
    id: str
    email: str
    full_name: str | None = None
    weight_kg: float | None = None
    height_cm: float | None = None
    ftp: int | None = None
    max_hr: int | None = None
    resting_hr: int | None = None
    experience_level: str | None = None
    has_power_meter: bool = False
    has_smart_trainer: bool = False
    has_hr_monitor: bool = False
    weekly_hours_available: float | None = None
    preferred_hard_days: list[int] | None = None
    rest_days: list[int] | None = None
    coach_name: str = "Forma"
    coach_avatar: str = "m1_climber"
    coach_tone: str = "balanced"

    model_config = ConfigDict(from_attributes=True)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenRefresh(BaseModel):
    refresh_token: str
