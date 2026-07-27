"""Refresh-token lifecycle: issue, rotate (with reuse detection), revoke.

Access tokens stay stateless and short-lived. Refresh tokens are the durable
credential, so they get a DB-backed record (`RefreshToken`) that lets us
rotate them on every use and revoke them on logout / password reset / theft.
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.core.exceptions import UnauthorizedException
from app.core.security import create_access_token, create_refresh_token, decode_token
from app.models.base import generate_uuid
from app.models.refresh_token import RefreshToken


def _new_refresh_record(
    db: Session, user_id: str, family_id: str, days: int | None = None
) -> str:
    jti = generate_uuid()
    db.add(RefreshToken(
        id=jti,
        user_id=user_id,
        family_id=family_id,
        expires_at=datetime.utcnow()
        + timedelta(days=days or settings.refresh_token_expire_days),
    ))
    return jti


def issue_pair(
    db: Session, user_id: str, remember_me: bool = False
) -> tuple[str, str]:
    """Fresh login → a new token family. Returns (access, refresh).

    remember_me extends the session (default 30 days vs 7); the lifetime
    is preserved across rotations by deriving it from the prior record.
    """
    days = settings.remember_me_expire_days if remember_me else None
    jti = _new_refresh_record(db, user_id, family_id=generate_uuid(), days=days)
    db.commit()
    return create_access_token(user_id), create_refresh_token(user_id, jti, days=days)


def rotate(db: Session, refresh_jwt: str) -> tuple[str, str]:
    """Exchange a refresh token for a fresh (access, refresh) pair, rotating
    the refresh token. Rejects unknown/expired tokens; on reuse of a revoked
    token, revokes the whole family and forces re-login."""
    payload = decode_token(refresh_jwt)
    if payload.get("type") != "refresh":
        raise UnauthorizedException(detail="Expected refresh token")
    jti = payload.get("jti")
    user_id = payload.get("sub")
    if not jti or not user_id:
        # Pre-rotation token (no jti) or malformed — force a clean login.
        raise UnauthorizedException(detail="Please log in again")

    rec = db.get(RefreshToken, jti)
    if rec is None:
        raise UnauthorizedException(detail="Invalid refresh token")
    if rec.revoked_at is not None:
        # This token was already rotated away — someone is replaying it.
        revoke_family(db, rec.family_id)
        raise UnauthorizedException(detail="Refresh token reuse detected; please log in again")
    if rec.expires_at < datetime.utcnow():
        raise UnauthorizedException(detail="Refresh token expired")

    # Preserve the session's chosen lifetime (remember-me) across rotations
    # by re-deriving it from the outgoing record.
    lifetime_days = max(1, (rec.expires_at - rec.issued_at).days) or None
    new_jti = _new_refresh_record(
        db, user_id, family_id=rec.family_id, days=lifetime_days
    )
    rec.revoked_at = datetime.utcnow()
    rec.replaced_by_id = new_jti
    db.commit()
    return create_access_token(user_id), create_refresh_token(
        user_id, new_jti, days=lifetime_days
    )


def revoke_family(db: Session, family_id: str) -> None:
    db.query(RefreshToken).filter(
        RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None)
    ).update({RefreshToken.revoked_at: datetime.utcnow()})
    db.commit()


def revoke_all_for_user(db: Session, user_id: str) -> int:
    """Kill every active session for a user (password reset / account lock).
    Returns the number of tokens revoked."""
    n = db.query(RefreshToken).filter(
        RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
    ).update({RefreshToken.revoked_at: datetime.utcnow()})
    db.commit()
    return n


def logout(db: Session, refresh_jwt: str) -> None:
    """Revoke the presented token's family (this device's session lineage).
    Idempotent and quiet — an already-invalid token is a no-op."""
    try:
        payload = decode_token(refresh_jwt)
    except UnauthorizedException:
        return
    jti = payload.get("jti")
    if not jti:
        return
    rec = db.get(RefreshToken, jti)
    if rec is not None:
        revoke_family(db, rec.family_id)
