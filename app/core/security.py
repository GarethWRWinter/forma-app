from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from app.config import settings
from app.core.exceptions import UnauthorizedException


def hash_password(password: str) -> str:
    password_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(
        plain_password.encode("utf-8"), hashed_password.encode("utf-8")
    )


def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    to_encode = {"sub": user_id, "exp": expire, "type": "access"}
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


def create_refresh_token(
    user_id: str, jti: str | None = None, days: int | None = None
) -> str:
    """Refresh token carrying a `jti` that maps to a RefreshToken row, so the
    token can be revoked and rotated. `jti` is optional only for throwaway
    (dev) minting; the auth flow always supplies one tied to a DB record.
    `days` overrides the default lifetime (remember-me sessions)."""
    expire = datetime.now(timezone.utc) + timedelta(
        days=days or settings.refresh_token_expire_days
    )
    to_encode = {"sub": user_id, "exp": expire, "type": "refresh"}
    if jti is not None:
        to_encode["jti"] = jti
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


def create_oauth_state_token(user_id: str, provider: str) -> str:
    """Short-lived signed state for OAuth flows. The callback is
    unauthenticated, so the state must prove which user started the flow."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=15)
    to_encode = {"sub": user_id, "exp": expire, "type": f"oauth_state:{provider}"}
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


def create_email_token(user_id: str, purpose: str, hours: int) -> str:
    """Signed single-purpose token for email links (verification, reset).
    Stateless by design; expiry and purpose live in the token itself."""
    expire = datetime.now(timezone.utc) + timedelta(hours=hours)
    to_encode = {"sub": user_id, "exp": expire, "type": f"email:{purpose}"}
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


def verify_email_token(token: str, purpose: str) -> str | None:
    """Return the user id if the email token is valid for this purpose."""
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.algorithm]
        )
    except JWTError:
        return None
    if payload.get("type") != f"email:{purpose}":
        return None
    return payload.get("sub")


def verify_oauth_state_token(token: str, provider: str) -> str | None:
    """Return the user id if the state token is valid, else None."""
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.algorithm]
        )
    except JWTError:
        return None
    if payload.get("type") != f"oauth_state:{provider}":
        return None
    return payload.get("sub")


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        return payload
    except JWTError:
        raise UnauthorizedException(detail="Invalid or expired token")


def get_user_id_from_token(token: str, token_type: str = "access") -> str:
    payload = decode_token(token)
    if payload.get("type") != token_type:
        raise UnauthorizedException(detail=f"Expected {token_type} token")
    user_id = payload.get("sub")
    if user_id is None:
        raise UnauthorizedException(detail="Invalid token payload")
    return user_id
