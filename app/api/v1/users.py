import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel, Field, computed_field
from sqlalchemy.orm import Session

from app.api.v1.auth import (
    AGE_CONSENT_TEXT,
    check_birth_date,
    check_country,
    current_legal_doc,
    hold_as_under_18,
    is_under_18,
    needs_profile_consent,
    under_18_response,
)
from app.api.v1.deps import get_current_user
from app.config import settings
from app.core.exceptions import BadRequestException
from app.core.ratelimit import rate_limit
from app.database import get_db
from app.models.user import User
from app.schemas.user import UserResponse, UserUpdate
from app.services import gdpr_service, safety_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


class MeResponse(UserResponse):
    """The rider's own profile: everything in UserResponse, plus what the
    app needs to ask before anything else."""

    @computed_field
    @property
    def needs_profile_consent(self) -> bool:
        """True when there's no date of birth or no country on file (the beta
        accounts). The re-acceptance modal then asks for both, and
        POST /auth/reaccept-terms requires them (review new problem 11)."""
        return needs_profile_consent(self)


@router.get("/me", response_model=MeResponse)
def get_profile(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/me", response_model=MeResponse)
def update_profile(
    user_in: UserUpdate,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    update_data = user_in.model_dump(exclude_unset=True)
    # Who can join is settled at registration; a profile edit can't undo it.
    # Neither can be cleared, and a new value meets the same rules.
    for field, check in (("date_of_birth", check_birth_date), ("country", check_country)):
        if field in update_data:
            value = update_data.pop(field)
            if value is not None:
                update_data[field] = check(value)
    new_born = update_data.get("date_of_birth")
    if new_born is not None and new_born != current_user.date_of_birth:
        # A date of birth under 18 holds the account as under 18, as at
        # re-acceptance, and is kept on the event, not the profile. While the
        # account is held, no new date replaces the one on file, so an adult
        # date sent next can't undo it. Nothing else in this edit is saved.
        if safety_service.minor_hold(db, current_user) is not None:
            return under_18_response(background_tasks)
        if is_under_18(new_born):
            hold_as_under_18(
                db, current_user, new_born, "in a profile edit",
                background_tasks, event_source="profile",
            )
            return under_18_response(background_tasks)
    # A date of birth given here is the same declaration as at sign-up, so
    # it leaves the same age row. A form that resends the date on file
    # doesn't add one.
    dob_given = new_born is not None and new_born != current_user.date_of_birth
    for field, value in update_data.items():
        setattr(current_user, field, value)
    if dob_given:
        safety_service.record_consent(
            db, current_user, "age", current_legal_doc(settings.terms_doc).doc_version,
            AGE_CONSENT_TEXT, request, source="app", commit=False,
        )
    db.commit()
    db.refresh(current_user)
    return current_user


class BadgePhotoBody(BaseModel):
    # A downscaled JPEG data URL from the badge studio. ~2MB ceiling keeps
    # the row honest; the client sends ~200-400KB after its own resize.
    data_url: str = Field(min_length=32, max_length=2_000_000)


@router.get("/me/badge-photo")
def get_badge_photo(current_user: User = Depends(get_current_user)):
    """Kept off /me so the profile payload stays light everywhere else."""
    return {"data_url": current_user.badge_photo}


@router.put("/me/badge-photo", dependencies=[Depends(rate_limit(20, 3600))])
def set_badge_photo(
    body: BadgePhotoBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not body.data_url.startswith(("data:image/jpeg;base64,", "data:image/png;base64,")):
        raise BadRequestException(detail="That photo needs to be a JPEG or PNG.")
    current_user.badge_photo = body.data_url
    db.commit()
    return {"saved": True}


@router.delete("/me/badge-photo", status_code=204)
def clear_badge_photo(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    current_user.badge_photo = None
    db.commit()
    return Response(status_code=204)


@router.get("/me/export")
def export_my_data(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """GDPR data portability — a JSON archive of everything we hold on you."""
    return gdpr_service.export_user_data(db, current_user)


@router.delete("/me", status_code=204)
def delete_my_account(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """GDPR erasure — locks the account and cuts off third-party access now;
    a scheduled purge removes the data after the retention window.

    The membership ends first. If Stripe can't be reached the account stays
    open: locking a rider out while their card goes on being charged is the
    one outcome worse than asking them to try again."""
    from app.services import billing_service

    try:
        billing_service.cancel_all_subscriptions(current_user)
    except Exception:
        logger.exception("Could not cancel Stripe subscription for %s", current_user.id)
        raise BadRequestException(
            detail="I couldn't end your membership just now, so nothing has been deleted. Try again in a minute."
        )
    gdpr_service.delete_account(db, current_user)
    return Response(status_code=204)
