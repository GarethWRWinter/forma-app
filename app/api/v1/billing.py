"""Stripe billing endpoints."""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user
from app.config import settings
from app.core.exceptions import BadRequestException
from app.database import get_db
from app.models.user import User
from app.services import billing_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/status")
def billing_status(current_user: User = Depends(get_current_user)):
    return {
        "configured": billing_service.is_configured(),
        "required": settings.require_subscription,
        "status": current_user.subscription_status,
        "period_end": (
            current_user.subscription_period_end.isoformat()
            if current_user.subscription_period_end
            else None
        ),
        "has_access": billing_service.has_access(current_user),
    }


@router.post("/checkout")
def start_checkout(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not billing_service.is_configured():
        raise BadRequestException(detail="Billing is not configured yet")
    try:
        url = billing_service.create_checkout_session(db, current_user)
    except Exception:
        logger.exception("Checkout session failed for user %s", current_user.id)
        raise BadRequestException(
            detail="Couldn't open checkout. Give it a minute and try again."
        )
    return {"url": url}


@router.post("/portal")
def open_portal(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not billing_service.is_configured():
        raise BadRequestException(detail="Billing is not configured yet")
    try:
        url = billing_service.create_portal_session(db, current_user)
    except Exception:
        logger.exception("Portal session failed for user %s", current_user.id)
        raise BadRequestException(
            detail="Couldn't open the billing portal. Give it a minute and try again."
        )
    return {"url": url}


@router.post("/webhook")
async def stripe_webhook(request: Request):
    """Stripe calls this; the signature is the authentication."""
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        billing_service.handle_webhook(payload, signature)
    except ValueError as e:
        logger.warning("Stripe webhook rejected: %s", e)
        return JSONResponse(status_code=400, content={"error": "invalid signature"})
    return {"status": "ok"}
