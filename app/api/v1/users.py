from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user
from app.core.exceptions import BadRequestException
from app.database import get_db
from app.models.user import User
from app.schemas.user import UserResponse, UserUpdate
from app.services import gdpr_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserResponse)
def get_profile(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/me", response_model=UserResponse)
def update_profile(
    user_in: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    update_data = user_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(current_user, field, value)
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


@router.put("/me/badge-photo")
def set_badge_photo(
    body: BadgePhotoBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not body.data_url.startswith(("data:image/jpeg;base64,", "data:image/png;base64,")):
        raise BadRequestException(detail="Send the photo as a JPEG or PNG data URL")
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
    a scheduled purge removes the data after the retention window."""
    gdpr_service.delete_account(db, current_user)
    return Response(status_code=204)
