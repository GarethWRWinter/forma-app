from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundException, UnauthorizedException
from app.core.security import get_user_id_from_token
from app.database import get_db
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    user_id = get_user_id_from_token(token, token_type="access")
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise UnauthorizedException(detail="User not found")
    # A suspended or deleted account must not authenticate, even with a
    # still-valid (short-lived) access token.
    if not user.is_active or user.deleted_at is not None:
        raise UnauthorizedException(detail="Account is inactive")
    return user


def require_paid_access(user: User = Depends(get_current_user)) -> User:
    """Gate for the surfaces that cost money to serve (the coach, imports).

    A no-op until REQUIRE_SUBSCRIPTION flips true at launch; founder/admin
    emails always pass. Returns 402 so the client can show the join screen
    instead of a generic error."""
    from fastapi import HTTPException

    from app.services import billing_service

    if not billing_service.has_access(user):
        raise HTTPException(
            status_code=402,
            detail="Your Forma membership isn't active. Join from Settings and the coach is yours again.",
        )
    return user
