"""Strava integration API endpoints."""

import asyncio
import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user
from app.config import settings
from app.core.exceptions import BadRequestException
from app.core.security import create_oauth_state_token, verify_oauth_state_token
from app.database import get_db
from app.models.user import User
from app.services import strava_service
from app.services.metrics_service import recalculate_from_date

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.get("/strava/auth-url")
def get_strava_auth_url(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get Strava OAuth authorization URL. The state is a signed, short-lived
    token proving which user started the flow, since the callback is
    unauthenticated.

    Reconnect only. Strava's API Agreement (s5.3) bars its data from AI
    applications, so a rider who has never linked Strava is sent to the
    account-archive import instead; existing links keep working until the
    live integration is retired.
    """
    from app.models.integration import StravaToken

    if not db.query(StravaToken).filter(StravaToken.user_id == current_user.id).first():
        raise BadRequestException(
            detail="Forma can't take a live Strava link. Import your Strava archive in Settings, then Data in, and your whole history comes across."
        )
    state = create_oauth_state_token(str(current_user.id), provider="strava")
    url = strava_service.get_auth_url(state=state)
    return {"auth_url": url}


@router.get("/strava/callback")
async def strava_callback(
    code: str = Query(...),
    state: str = Query(""),
    scope: str = Query(""),
    db: Session = Depends(get_db),
):
    """
    Handle Strava OAuth callback (exchange code for tokens).
    This endpoint is unauthenticated — the user ID comes from the state parameter.
    Redirects back to the frontend settings page after connecting.
    """
    frontend_url = settings.frontend_url or "http://localhost:3000"
    frontend_url = f"{frontend_url}/dashboard/settings"

    if not state:
        return RedirectResponse(f"{frontend_url}?strava=error&reason=missing_state")

    user_id = verify_oauth_state_token(state, provider="strava")
    if not user_id:
        return RedirectResponse(f"{frontend_url}?strava=error&reason=invalid_state")

    try:
        token = await strava_service.exchange_code(db, user_id, code, granted_scope=scope)

        # Strava only grants activity access when the user ticks "View data
        # about your activities" on the consent screen. Without it the token
        # is read-only and every activity fetch 403s — so surface it now as a
        # clear reconnect prompt instead of a fake "connected" that fails later.
        if not strava_service.scope_has_activity_read(token.scope):
            logger.warning(
                "Strava connected for user %s WITHOUT activity scope (granted=%r)",
                user_id, token.scope,
            )
            return RedirectResponse(
                f"{frontend_url}?strava=error&reason=missing_activity_scope"
            )

        # Start historical backfill in background (only on first connect)
        if not token.backfill_status or token.backfill_status == "failed":
            asyncio.create_task(
                strava_service.run_backfill_background(user_id)
            )
            logger.info("Started Strava backfill for user %s", user_id)

        return RedirectResponse(f"{frontend_url}?strava=connected")
    except Exception as e:
        logger.error("Strava callback failed: %s", str(e))
        return RedirectResponse(f"{frontend_url}?strava=error&reason={str(e)}")


@router.post("/strava/sync")
async def sync_strava(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Manually trigger Strava activity sync (recent rides only)."""
    try:
        rides = await strava_service.sync_activities(db, current_user)

        # Recalculate PMC for any new rides with TSS
        for ride in rides:
            if ride.tss and ride.ride_date:
                rd = ride.ride_date
                if hasattr(rd, 'date'):
                    rd = rd.date()
                recalculate_from_date(db, current_user.id, rd)

        # Retroactively link any rides from the past 2 weeks that don't yet
        # have a planned workout associated with them. Idempotent.
        from app.services.workout_assessment_service import backfill_auto_links
        try:
            backfill_auto_links(db, current_user.id, days=14)
        except Exception:
            pass

        return {
            "synced": len(rides),
            "rides": [
                {"id": r.id, "title": r.title, "date": str(r.ride_date)}
                for r in rides
            ],
        }
    except ValueError as e:
        raise BadRequestException(detail=str(e))
    except Exception as e:
        # Expired/revoked tokens or Strava outages should read as "reconnect
        # Strava", not a 500 that the client keeps retrying.
        logger.error("Strava sync failed for user %s: %s", current_user.id, e)
        raise BadRequestException(
            detail="Strava sync failed. Try reconnecting Strava in Settings."
        )


@router.get("/strava/status")
async def strava_status(
    probe: bool = Query(False, description="Make one live Strava call to verify activity access"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check Strava connection status including backfill progress.

    Pass ?probe=true to make a single live call to Strava and confirm the
    stored token can actually read activities (the ground-truth 403 check).
    """
    status = strava_service.get_connection_status(db, current_user.id)
    backfill = strava_service.get_backfill_status(db, current_user.id)
    if backfill:
        status["backfill"] = backfill
    if probe and status.get("connected"):
        status["probe"] = await strava_service.probe_activity_access(db, current_user.id)
    return status


@router.post("/strava/backfill")
async def start_backfill(
    force: bool = Query(False, description="Force restart even if a backfill is running"),
    since_years: int | None = Query(
        None,
        description="Only import rides newer than this many years (e.g. 2 keeps DB lean). Omit for full history.",
        ge=1,
        le=20,
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Manually trigger historical backfill. Safe to call repeatedly — the
    underlying import is idempotent (already-imported rides are skipped).

    If a backfill is currently running and making progress (last update <15min),
    this is a no-op unless `force=true`.

    Pass `since_years=2` (or similar) to keep the import lean — useful after a
    fresh DB to avoid re-pulling decades of data.
    """
    from datetime import datetime, timedelta, timezone
    from app.models.integration import StravaToken

    backfill = strava_service.get_backfill_status(db, current_user.id)
    if backfill and backfill["status"] == "running" and not force:
        # Consider the backfill stale if started > 15 min ago — likely
        # crashed with no recent progress update.
        started_at = backfill.get("started_at")
        stale = True
        if started_at:
            try:
                started = datetime.fromisoformat(started_at)
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                age_secs = (datetime.now(timezone.utc) - started).total_seconds()
                stale = age_secs > 15 * 60
            except Exception:
                stale = True
        if not stale:
            return {
                "status": "already_running",
                "progress": backfill["progress"],
                "total": backfill["total"],
            }

    since = None
    if since_years:
        since = datetime.now(timezone.utc) - timedelta(days=since_years * 365)

    asyncio.create_task(
        strava_service.run_backfill_background(current_user.id, since=since)
    )
    return {
        "status": "backfill_started",
        "since": since.isoformat() if since else None,
    }


@router.post("/strava/backfill-segments")
async def start_segment_backfill(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Backfill segment data for existing rides with Strava activity IDs."""
    asyncio.create_task(
        strava_service.run_segment_backfill_background(current_user.id)
    )
    return {"status": "segment_backfill_started"}


@router.delete("/strava")
def disconnect_strava(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Disconnect Strava integration."""
    strava_service.disconnect(db, current_user.id)
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# Strava Webhook Endpoints (no auth — called by Strava servers)
# ---------------------------------------------------------------------------

@router.get("/strava/webhook")
async def strava_webhook_verify(
    request: Request,
):
    """
    Strava webhook subscription validation.
    Strava sends a GET with hub.mode, hub.challenge, hub.verify_token.
    We must respond with {"hub.challenge": "<value>"} within 2 seconds.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    challenge = params.get("hub.challenge")
    verify_token = params.get("hub.verify_token")

    if mode == "subscribe" and verify_token == settings.strava_webhook_verify_token:
        logger.info("Strava webhook subscription verified")
        return JSONResponse(content={"hub.challenge": challenge})

    logger.warning("Strava webhook verification failed: mode=%s, token=%s", mode, verify_token)
    return JSONResponse(status_code=403, content={"error": "Verification failed"})


@router.post("/strava/webhook")
async def strava_webhook_receive(request: Request):
    """
    Receive Strava webhook events.
    Must respond with 200 within 2 seconds — processing happens in background.
    """
    try:
        event = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    logger.info(
        "Strava webhook event: %s %s (object_id=%s, owner=%s)",
        event.get("object_type"),
        event.get("aspect_type"),
        event.get("object_id"),
        event.get("owner_id"),
    )

    # Process in background so we respond to Strava within 2s
    asyncio.create_task(strava_service.handle_webhook_event(event))

    return JSONResponse(status_code=200, content={"status": "ok"})
