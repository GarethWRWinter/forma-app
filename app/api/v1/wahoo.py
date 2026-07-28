"""Wahoo Cloud API integration endpoints."""

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
from app.services import wahoo_service
from app.services.metrics_service import recalculate_from_date

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations/wahoo", tags=["integrations"])


@router.get("/auth-url")
def get_wahoo_auth_url(current_user: User = Depends(get_current_user)):
    """Wahoo OAuth authorization URL, state-signed to the requesting user."""
    if not wahoo_service.is_configured():
        raise BadRequestException(detail="Wahoo linking is not configured yet")
    state = create_oauth_state_token(str(current_user.id), provider="wahoo")
    return {"auth_url": wahoo_service.get_auth_url(state=state)}


@router.get("/callback")
async def wahoo_callback(
    code: str = Query(""),
    state: str = Query(""),
    error: str = Query(""),
    db: Session = Depends(get_db),
):
    """OAuth callback (unauthenticated; user id comes from the state token)."""
    frontend_url = settings.frontend_url or "http://localhost:3000"
    frontend_url = f"{frontend_url}/dashboard/settings"

    if error or not code:
        return RedirectResponse(f"{frontend_url}?wahoo=error&reason={error or 'denied'}")

    user_id = verify_oauth_state_token(state, provider="wahoo")
    if not user_id:
        return RedirectResponse(f"{frontend_url}?wahoo=error&reason=invalid_state")

    try:
        token = await wahoo_service.exchange_code(db, user_id, code)
        # First link: pull the rider's history in the background.
        if not token.backfill_status or token.backfill_status == "failed":
            asyncio.create_task(wahoo_service.run_backfill_background(user_id))
            logger.info("Started Wahoo backfill for user %s", user_id)
        return RedirectResponse(f"{frontend_url}?wahoo=connected")
    except Exception as e:
        logger.error("Wahoo callback failed: %s", e)
        return RedirectResponse(f"{frontend_url}?wahoo=error&reason=exchange_failed")


@router.get("/status")
def wahoo_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return wahoo_service.get_connection_status(db, current_user.id)


@router.post("/sync")
async def sync_wahoo(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Manually pull recent Wahoo workouts."""
    try:
        imported = await wahoo_service.sync_workouts(db, current_user)
    except ValueError as e:
        raise BadRequestException(detail=str(e))
    except Exception:
        logger.exception("Wahoo sync failed for user %s", current_user.id)
        raise BadRequestException(
            detail="Wahoo sync failed. Try reconnecting Wahoo in Settings."
        )

    for ride in imported:
        if ride.tss and ride.ride_date:
            recalculate_from_date(db, current_user.id, ride.ride_date.date())

    return {
        "synced": len(imported),
        "rides": [
            {"id": r.id, "title": r.title, "date": str(r.ride_date)}
            for r in imported
        ],
    }


@router.post("/backfill")
async def start_wahoo_backfill(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Import full Wahoo history. Idempotent — existing rides are skipped."""
    status = wahoo_service.get_connection_status(db, current_user.id)
    if not status.get("connected"):
        raise BadRequestException(detail="Wahoo is not connected")
    asyncio.create_task(wahoo_service.run_backfill_background(str(current_user.id)))
    return {"status": "backfill_started"}


@router.delete("")
def disconnect_wahoo(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    wahoo_service.disconnect(db, current_user.id)
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# Webhook (no auth — called by Wahoo's servers; verified by shared token)
# ---------------------------------------------------------------------------

@router.post("/webhook")
async def wahoo_webhook_receive(request: Request):
    """Receive Wahoo workout events; respond fast, process in background."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    if settings.wahoo_webhook_token:
        provided = payload.get("webhook_token") or request.headers.get("x-webhook-token", "")
        if provided != settings.wahoo_webhook_token:
            logger.warning("Wahoo webhook with bad token rejected")
            return JSONResponse(status_code=403, content={"error": "Bad token"})

    logger.info("Wahoo webhook event: %s", payload.get("event_type", "unknown"))
    asyncio.create_task(wahoo_service.handle_webhook_event(payload))
    return JSONResponse(status_code=200, content={"status": "ok"})
