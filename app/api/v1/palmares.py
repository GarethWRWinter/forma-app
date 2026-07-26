"""Palmarès endpoint — the rider's trophy cabinet."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.palmares_service import get_palmares

router = APIRouter(prefix="/palmares", tags=["palmares"])


@router.get("")
def read_palmares(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The Cabinet (raced goals + records) and the Log (PRs + milestones)."""
    return get_palmares(db, current_user)
