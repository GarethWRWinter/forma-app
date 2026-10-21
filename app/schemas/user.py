from datetime import date, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, EmailStr, ConfigDict, Field, computed_field, field_validator

from app.config import settings

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
    # Eligibility: 18 or over, and not in a blocked territory. Optional here
    # and checked in the endpoint, so a rider who misses one gets a sentence
    # rather than a validation error.
    date_of_birth: date | None = None
    country: str | None = Field(None, max_length=64)
    # Box 1: the terms and risk acknowledgement.
    terms_accepted: bool = False
    # Box 2: explicit consent to process health data (UK GDPR Article 9),
    # kept separate from box 1 (Article 7(2)).
    health_consent: bool = False
    # The exact label text of each box, recorded word for word in
    # consent_events. A form that sends neither is a page opened before
    # the boxes changed.
    terms_text_shown: str | None = Field(None, max_length=4000)
    health_text_shown: str | None = Field(None, max_length=4000)

    @field_validator("date_of_birth", mode="before")
    @classmethod
    def _blank_date_is_missing(cls, v):
        # An untouched date input posts "", which is a missing date, not a bad one.
        return None if isinstance(v, str) and not v.strip() else v


class UserLogin(BaseModel):
    email: NormalisedEmail
    password: str
    remember_me: bool = False


class UserUpdate(BaseModel):
    full_name: str | None = None
    weight_kg: float | None = None
    height_cm: float | None = None
    # Checked against the same rules as registration: 18 or over, and a
    # territory Forma serves. Sending null leaves them as they are.
    date_of_birth: date | None = None
    country: str | None = Field(None, max_length=64)
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

    @field_validator("date_of_birth", mode="before")
    @classmethod
    def _blank_date_is_missing(cls, v):
        return None if isinstance(v, str) and not v.strip() else v


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
    country: str | None = None
    # Settings reads it for the age-based check-up line on the FTP test.
    date_of_birth: date | None = None
    terms_version: str | None = None
    # Read for health_consent_current only; never sent.
    health_consent_at: datetime | None = Field(default=None, exclude=True)

    model_config = ConfigDict(from_attributes=True)

    @computed_field
    @property
    def terms_current(self) -> bool:
        """False once the terms have changed since the rider last agreed (or
        they never have, like the beta accounts): the app then asks again."""
        return self.terms_version == settings.terms_version

    @computed_field
    @property
    def health_consent_current(self) -> bool:
        """False when the rider has never ticked box 2, the explicit consent
        to use their health details (UK GDPR Art 9). Every beta account joined
        before it was asked: the app asks before any health question."""
        return self.health_consent_at is not None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenRefresh(BaseModel):
    refresh_token: str
