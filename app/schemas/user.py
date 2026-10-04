from typing import Annotated

from pydantic import AfterValidator, BaseModel, EmailStr, ConfigDict, Field

# Addresses are stored and looked up lower-case. Without this, a phone that
# capitalised the first letter at login locked a rider out of the account
# they had registered in lower case, and the same person could register
# twice (launch audit, 4 Oct 2026).
NormalisedEmail = Annotated[EmailStr, AfterValidator(lambda v: v.strip().lower())]


class UserCreate(BaseModel):
    email: NormalisedEmail
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = None
    invite_code: str | None = Field(None, max_length=24)
    # Explicit consent to process health data (UK GDPR Article 9).
    health_consent: bool = False


class UserLogin(BaseModel):
    email: NormalisedEmail
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
    email_verified: bool = False
    subscription_status: str = "none"
    founding_number: int | None = None
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
