"""Wahoo Cloud API integration.

The rider links their own Wahoo account via OAuth; from then on every ride
that syncs off their ELEMNT arrives here as a FIT file, either pushed by
Wahoo's workout webhooks or pulled by sync/backfill. Files feed the same
pipeline as a manual upload, so titles, debriefs and metrics all behave
identically regardless of the door the ride came through.

Docs: https://developers.wahooligan.com/cloud
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models.integration import WahooToken
from app.models.ride import Ride
from app.models.user import User

logger = logging.getLogger(__name__)

WAHOO_BASE = "https://api.wahooligan.com"
WAHOO_AUTH_URL = f"{WAHOO_BASE}/oauth/authorize"
WAHOO_TOKEN_URL = f"{WAHOO_BASE}/oauth/token"
SCOPES = "user_read workouts_read offline_data"


def is_configured() -> bool:
    return bool(settings.wahoo_client_id and settings.wahoo_client_secret)


def get_auth_url(state: str = "") -> str:
    from urllib.parse import urlencode

    params = {
        "client_id": settings.wahoo_client_id,
        "redirect_uri": settings.wahoo_redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
    }
    if state:
        params["state"] = state
    return f"{WAHOO_AUTH_URL}?{urlencode(params)}"


async def exchange_code(db: Session, user_id: str, code: str) -> WahooToken:
    """Exchange the OAuth code, fetch the Wahoo user id, upsert the token."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(WAHOO_TOKEN_URL, data={
            "client_id": settings.wahoo_client_id,
            "client_secret": settings.wahoo_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": settings.wahoo_redirect_uri,
        })
        response.raise_for_status()
        data = response.json()

        wahoo_user_id = None
        try:
            user_resp = await client.get(
                f"{WAHOO_BASE}/v1/user",
                headers={"Authorization": f"Bearer {data['access_token']}"},
            )
            if user_resp.status_code == 200:
                wahoo_user_id = user_resp.json().get("id")
        except httpx.HTTPError:
            logger.warning("Wahoo user lookup failed during link", exc_info=True)

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 7200))

    token = db.query(WahooToken).filter(WahooToken.user_id == user_id).first()
    if token is None:
        token = WahooToken(user_id=user_id)
        db.add(token)
    token.access_token = data["access_token"]
    token.refresh_token = data["refresh_token"]
    token.expires_at = expires_at.replace(tzinfo=None)
    token.scope = data.get("scope") or SCOPES
    if wahoo_user_id is not None:
        token.wahoo_user_id = wahoo_user_id
    token.needs_reauth = False
    db.commit()
    db.refresh(token)
    return token


class WahooReauthRequired(Exception):
    """The stored Wahoo credentials are dead: only a fresh OAuth fixes it."""


async def _access_token(db: Session, token: WahooToken) -> str:
    """Current access token, refreshed when within 5 minutes of expiry."""
    expires_at = token.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        return token.access_token

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(WAHOO_TOKEN_URL, data={
            "client_id": settings.wahoo_client_id,
            "client_secret": settings.wahoo_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
        })
        if response.status_code in (400, 401):
            # The refresh token is dead (Wahoo rotates them on every use, so
            # a deploy landing between refresh and commit kills the chain).
            # Record it so the settings card can say "reconnect" instead of
            # rides silently stopping, which is how it failed on 14 Aug.
            token.needs_reauth = True
            db.commit()
            raise WahooReauthRequired(
                "Wahoo rejected the refresh token; the rider must reconnect."
            )
        response.raise_for_status()
        data = response.json()

    token.access_token = data["access_token"]
    token.refresh_token = data.get("refresh_token") or token.refresh_token
    token.expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 7200))
    ).replace(tzinfo=None)
    db.commit()
    return token.access_token


def _workout_file_url(workout: dict) -> str | None:
    """The FIT file URL inside a workout payload, wherever Wahoo put it."""
    summary = workout.get("workout_summary") or {}
    file_info = summary.get("file") or workout.get("file") or {}
    return file_info.get("url")


def _external_id(workout: dict) -> str:
    return f"wahoo_{workout.get('id')}"


async def _import_workout(
    db: Session, user: User, client: httpx.AsyncClient, access_token: str, workout: dict
) -> Ride | None:
    """Download one workout's FIT file and create the ride. Returns None when
    the workout has no file yet, is not new, or fails to parse."""
    from app.services import ride_service
    from app.models.ride import RideSource

    ext_id = _external_id(workout)
    exists = db.query(Ride.id).filter(Ride.external_id == ext_id).first()
    if exists:
        return None

    file_url = _workout_file_url(workout)
    if not file_url:
        return None

    try:
        # The file URL is pre-signed (credentials live in the URL itself).
        # Sending the OAuth header AS WELL makes the CDN reject the request
        # with a 400, so the download goes bare.
        file_resp = await client.get(file_url, follow_redirects=True)
        file_resp.raise_for_status()
    except httpx.HTTPError:
        logger.warning("Wahoo file download failed for workout %s", workout.get("id"))
        return None

    starts = workout.get("starts")
    start_dt = None
    if starts:
        try:
            start_dt = datetime.fromisoformat(str(starts).replace("Z", "+00:00"))
        except ValueError:
            start_dt = None

    # Cross-source dedupe: the same outing may already be in via upload or
    # an archive import.
    duration = workout.get("minutes")
    duration_secs = int(duration * 60) if duration else None
    if start_dt and ride_service.find_duplicate_ride(db, user.id, start_dt, duration_secs):
        return None

    try:
        ride = ride_service.create_ride_from_fit(
            db, user, file_resp.content,
            filename=f"{ext_id}.fit", source=RideSource.wahoo.value,
        )
    except Exception:
        db.rollback()
        logger.exception("Wahoo workout %s failed to parse", workout.get("id"))
        return None

    ride.external_id = ext_id
    if workout.get("name"):
        # Keep Wahoo's name only when it says something; the classifier's
        # title already covers the generic case.
        pass
    db.commit()
    return ride


async def sync_workouts(db: Session, user: User, pages: int = 1) -> list[Ride]:
    """Pull recent workouts and import any new ones."""
    token = db.query(WahooToken).filter(WahooToken.user_id == user.id).first()
    if token is None:
        raise ValueError("Wahoo is not connected")

    access = await _access_token(db, token)
    imported: list[Ride] = []

    async with httpx.AsyncClient(timeout=60.0) as client:
        for page in range(1, pages + 1):
            response = await client.get(
                f"{WAHOO_BASE}/v1/workouts",
                params={"page": page, "per_page": 30},
                headers={"Authorization": f"Bearer {access}"},
            )
            response.raise_for_status()
            workouts = response.json().get("workouts", [])
            if not workouts:
                break
            for workout in workouts:
                ride = await _import_workout(db, user, client, access, workout)
                if ride:
                    imported.append(ride)

    token.last_sync_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    return imported


async def backfill_history(db: Session, user: User) -> int:
    """Page through the rider's full Wahoo history and import everything."""
    token = db.query(WahooToken).filter(WahooToken.user_id == user.id).first()
    if token is None:
        raise ValueError("Wahoo is not connected")

    token.backfill_status = "running"
    token.backfill_progress = 0
    db.commit()

    access = await _access_token(db, token)
    imported = 0
    page = 1

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            while True:
                response = await client.get(
                    f"{WAHOO_BASE}/v1/workouts",
                    params={"page": page, "per_page": 50},
                    headers={"Authorization": f"Bearer {access}"},
                )
                response.raise_for_status()
                body = response.json()
                workouts = body.get("workouts", [])
                if not workouts:
                    break
                total = (body.get("total") if isinstance(body, dict) else None)
                if total and token.backfill_total != total:
                    token.backfill_total = total
                for workout in workouts:
                    ride = await _import_workout(db, user, client, access, workout)
                    if ride:
                        imported += 1
                    token.backfill_progress = (token.backfill_progress or 0) + 1
                db.commit()
                page += 1
    except Exception:
        token.backfill_status = "failed"
        db.commit()
        raise

    token.backfill_status = "completed"
    db.commit()

    # One recalculation at the end, from the earliest ride.
    from app.services.metrics_service import recalculate_from_date
    earliest = (
        db.query(Ride.ride_date)
        .filter(Ride.user_id == user.id)
        .order_by(Ride.ride_date.asc())
        .first()
    )
    if earliest:
        recalculate_from_date(db, user.id, earliest[0].date())
    return imported


async def run_backfill_background(user_id: str) -> None:
    """Backfill with its own DB session, safe as an asyncio task."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            return
        count = await backfill_history(db, user)
        logger.info("Wahoo backfill for user %s imported %d rides", user_id, count)
    except Exception:
        logger.exception("Wahoo backfill failed for user %s", user_id)
    finally:
        db.close()


async def handle_webhook_event(payload: dict) -> None:
    """A workout finished syncing to Wahoo's cloud; import it now."""
    from app.database import SessionLocal
    from app.services.metrics_service import recalculate_from_date

    wahoo_user = (payload.get("user") or {}).get("id")
    workout = payload.get("workout_summary") or payload.get("workout") or {}
    if not wahoo_user or not workout:
        logger.warning("Wahoo webhook payload missing user or workout")
        return

    # workout_summary events nest the workout under "workout"; normalise so
    # _import_workout always sees {id, starts, ..., workout_summary:{file}}.
    if "workout" in payload and "workout_summary" in payload:
        workout = {**payload["workout"], "workout_summary": payload["workout_summary"]}
    elif "file" in workout and "workout" in workout:
        inner = workout.pop("workout")
        workout = {**inner, "workout_summary": {"file": workout.get("file")}}

    db = SessionLocal()
    try:
        token = (
            db.query(WahooToken)
            .filter(WahooToken.wahoo_user_id == int(wahoo_user))
            .first()
        )
        if token is None:
            logger.warning("Wahoo webhook for unknown wahoo_user_id %s", wahoo_user)
            return
        user = db.query(User).filter(User.id == token.user_id).first()
        if user is None:
            return

        access = await _access_token(db, token)
        async with httpx.AsyncClient(timeout=60.0) as client:
            ride = await _import_workout(db, user, client, access, workout)

        if ride:
            if ride.tss and ride.ride_date:
                recalculate_from_date(db, user.id, ride.ride_date.date())
            # A fresh ride deserves the coach's eye, same as an upload.
            from app.services.coach_insights_service import generate_ride_debrief
            try:
                generate_ride_debrief(db, user, ride)
            except Exception:
                logger.exception("Wahoo webhook debrief failed for ride %s", ride.id)
            logger.info("Wahoo webhook imported ride %s for user %s", ride.id, user.id)
    finally:
        db.close()


def get_connection_status(db: Session, user_id: str) -> dict:
    token = db.query(WahooToken).filter(WahooToken.user_id == user_id).first()
    if token is None:
        return {"connected": False, "configured": is_configured()}
    status: dict = {
        "connected": True,
        "configured": is_configured(),
        "needs_reauth": bool(token.needs_reauth),
        "wahoo_user_id": token.wahoo_user_id,
        "last_sync_at": token.last_sync_at.isoformat() if token.last_sync_at else None,
    }
    if token.backfill_status:
        status["backfill"] = {
            "status": token.backfill_status,
            "progress": token.backfill_progress or 0,
            "total": token.backfill_total,
        }
    return status


def disconnect(db: Session, user_id: str) -> None:
    db.query(WahooToken).filter(WahooToken.user_id == user_id).delete()
    db.commit()
