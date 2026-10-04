"""Registration rules found in the launch audit, 4 Oct 2026."""

import pytest
from pydantic import ValidationError

from app.schemas.user import UserCreate, UserLogin


def test_email_is_lower_cased_at_register_and_login():
    """A phone that capitalised the first letter used to lock a rider out of
    the account they registered in lower case."""
    reg = UserCreate(email="Rider.Name@Gmail.COM", password="x" * 8, health_consent=True)
    login = UserLogin(email=" rider.name@gmail.com ", password="x" * 8)
    assert reg.email == login.email == "rider.name@gmail.com"


def test_health_consent_defaults_to_not_given():
    assert UserCreate(email="a@b.com", password="x" * 8).health_consent is False


def test_bad_email_still_rejected():
    with pytest.raises(ValidationError):
        UserCreate(email="not-an-email", password="x" * 8)
