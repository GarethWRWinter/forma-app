import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.core.exceptions import BadRequestException, ConflictException, UnauthorizedException
from app.core.ratelimit import rate_limit
from app.core.security import (
    create_email_token,
    hash_password,
    verify_email_token,
    verify_password,
)
from app.api.v1.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.user import TokenRefresh, TokenResponse, UserCreate, UserLogin, UserResponse
from app.services import email_service, token_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# Per-IP caps on the sensitive auth endpoints (window in seconds).
_login_limit = rate_limit(10, 60)      # 10 login attempts / minute
_register_limit = rate_limit(5, 300)   # 5 signups / 5 minutes
_refresh_limit = rate_limit(30, 60)    # 30 refreshes / minute
_email_limit = rate_limit(5, 600)      # 5 reset/verify emails / 10 minutes


def _frontend() -> str:
    return settings.frontend_url or "http://localhost:3000"


async def _send_verification_email(user_id: str, email: str, full_name: str | None) -> None:
    token = create_email_token(user_id, "verify", hours=24)
    link = f"{_frontend()}/verify-email?token={token}"
    await email_service.send_verification(email, full_name, link)

# Verified against when the email is unknown, so a login attempt takes the same
# time whether or not the account exists (defeats timing-based user enumeration).
_DUMMY_HASH = hash_password("forma-nonexistent-account-placeholder")


@router.get("/config")
def auth_config():
    """Public flags the entry pages need before anyone is logged in."""
    return {"invite_required": settings.require_invite}


def _redeem_invite(db: Session, code: str | None) -> str | None:
    """Validate and consume one use of an invite code. Returns the
    normalised code, or raises. No-op (returns None) when the door is open."""
    from datetime import datetime

    from app.models.invite import InviteCode

    if not settings.require_invite:
        return code.strip().upper() if code else None
    if not code or not code.strip():
        raise BadRequestException(
            detail="Forma is invite-only right now. Join the list at ridewithforma.com and we'll call you up."
        )
    normalised = code.strip().upper()
    # Row-lock so two simultaneous signups can't share a single-use code.
    invite = (
        db.query(InviteCode)
        .filter(InviteCode.code == normalised)
        .with_for_update()
        .first()
    )
    if invite is None or invite.uses >= invite.max_uses or (
        invite.expires_at is not None and invite.expires_at < datetime.utcnow()
    ):
        raise BadRequestException(
            detail="That invite code isn't valid any more. Reply to your invite email and we'll sort you out."
        )
    invite.uses += 1
    return normalised


FOUNDING_CAP = 100


def _next_founding_number(db: Session) -> int | None:
    """Next free rider number, or None once the hundred are in. The unique
    constraint on users.founding_number is the final arbiter under races."""
    from sqlalchemy import func

    taken = db.query(func.max(User.founding_number)).scalar() or 0
    return taken + 1 if taken < FOUNDING_CAP else None


@router.post("/register", response_model=UserResponse, status_code=201,
             dependencies=[Depends(_register_limit)])
async def register(
    user_in: UserCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    existing = db.query(User).filter(User.email == user_in.email).first()
    if existing:
        raise ConflictException(detail="Email already registered")

    invited_with = _redeem_invite(db, user_in.invite_code)

    # A validated invite is a founding rider: number them on the way in.
    # Open-door signups (require_invite off) track their code but are not
    # founding; the hundred only count when the door is actually gated.
    founding_number = (
        _next_founding_number(db)
        if invited_with and settings.require_invite
        else None
    )

    user = User(
        email=user_in.email,
        hashed_password=hash_password(user_in.password),
        full_name=user_in.full_name,
        invited_with=invited_with,
        founding_number=founding_number,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Two founding signups landed on the same number: take the next one.
        db.rollback()
        user.founding_number = _next_founding_number(db)
        db.add(user)
        db.commit()
    db.refresh(user)

    # Best-effort: a failed email must never block the signup itself.
    background_tasks.add_task(
        _send_verification_email, str(user.id), user.email, user.full_name
    )
    return user


class EmailTokenBody(BaseModel):
    token: str


class ForgotPasswordBody(BaseModel):
    email: EmailStr


class ResetPasswordBody(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


@router.post("/verify-email")
def verify_email(body: EmailTokenBody, db: Session = Depends(get_db)):
    """Flip the flag on a valid verification link. Idempotent."""
    user_id = verify_email_token(body.token, "verify")
    if not user_id:
        raise BadRequestException(
            detail="That link has expired or already been used. Request a fresh one from Settings."
        )
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise BadRequestException(detail="Account not found")
    if not user.email_verified:
        user.email_verified = True
        db.commit()
    return {"status": "verified"}


@router.post("/resend-verification", dependencies=[Depends(_email_limit)])
async def resend_verification(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    if current_user.email_verified:
        return {"status": "already_verified"}
    background_tasks.add_task(
        _send_verification_email,
        str(current_user.id), current_user.email, current_user.full_name,
    )
    return {"status": "sent"}


@router.post("/forgot-password", dependencies=[Depends(_email_limit)])
async def forgot_password(
    body: ForgotPasswordBody,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Always answers the same way, whether or not the account exists, so
    the endpoint can't be used to probe for registered emails."""
    user = db.query(User).filter(User.email == body.email).first()
    if user and user.is_active and user.deleted_at is None:
        token = create_email_token(str(user.id), "reset", hours=1)
        link = f"{_frontend()}/reset-password?token={token}"
        background_tasks.add_task(
            email_service.send_password_reset, user.email, user.full_name, link
        )
    return {"status": "sent"}


@router.post("/reset-password")
def reset_password(body: ResetPasswordBody, db: Session = Depends(get_db)):
    """Set a new password from a reset link, then revoke every live session:
    whoever holds old tokens is signed out everywhere."""
    user_id = verify_email_token(body.token, "reset")
    if not user_id:
        raise BadRequestException(
            detail="That link has expired. Request a new one and try again within the hour."
        )
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active or user.deleted_at is not None:
        raise BadRequestException(detail="Account not found")

    user.hashed_password = hash_password(body.new_password)
    # A password reset also proves the email is theirs.
    user.email_verified = True
    db.commit()
    token_service.revoke_all_for_user(db, str(user.id))
    logger.info("Password reset completed for user %s", user.id)
    return {"status": "reset"}


@router.post("/login", response_model=TokenResponse,
             dependencies=[Depends(_login_limit)])
def login(user_in: UserLogin, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == user_in.email).first()
    # Always run a bcrypt verify — against a dummy hash when the email is
    # unknown — so response time doesn't reveal whether an email is registered.
    hashed = user.hashed_password if user else _DUMMY_HASH
    password_ok = verify_password(user_in.password, hashed)
    if not user or not password_ok:
        raise UnauthorizedException(detail="Invalid email or password")
    # A suspended or GDPR-deleted account cannot obtain new tokens.
    if not user.is_active or user.deleted_at is not None:
        raise UnauthorizedException(detail="Account is inactive")

    access, refresh = token_service.issue_pair(db, user.id, remember_me=user_in.remember_me)
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/refresh", response_model=TokenResponse,
             dependencies=[Depends(_refresh_limit)])
def refresh_token(body: TokenRefresh, db: Session = Depends(get_db)):
    access, refresh = token_service.rotate(db, body.refresh_token)
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/logout", status_code=204)
def logout(body: TokenRefresh, db: Session = Depends(get_db)):
    """Revoke the presented refresh token's session lineage. Idempotent."""
    token_service.logout(db, body.refresh_token)
    return Response(status_code=204)
