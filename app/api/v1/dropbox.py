"""Dropbox integration API endpoints."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user
from app.config import settings
from app.core.exceptions import BadRequestException
from app.database import get_db
from app.models.integration import DropboxToken
from app.models.user import User
from app.services import dropbox_service
from app.services.metrics_service import recalculate_from_date

router = APIRouter(prefix="/integrations/dropbox", tags=["integrations"])


class FolderPathUpdate(BaseModel):
    folder_path: str


@router.get("/auth-url")
def get_dropbox_auth_url(
    current_user: User = Depends(get_current_user),
):
    """Get Dropbox OAuth authorization URL."""
    url = dropbox_service.get_auth_url(current_user.id)
    return {"auth_url": url}


@router.get("/callback")
async def dropbox_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    db: Session = Depends(get_db),
):
    """
    Handle Dropbox OAuth callback (exchange code for tokens).
    This is a public endpoint — Dropbox redirects here after user authorizes.
    The 'state' param contains the user_id set during auth URL generation.
    After processing, redirects user back to the frontend settings page.
    """
    frontend_url = settings.frontend_url or "http://localhost:3000"
    frontend_url = f"{frontend_url}/dashboard/settings"

    if error:
        return RedirectResponse(
            url=f"{frontend_url}?dropbox=error&message={error}"
        )

    if not code or not state:
        return RedirectResponse(
            url=f"{frontend_url}?dropbox=error&message=missing_params"
        )

    user_id = state

    # Verify the user exists
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse(
            url=f"{frontend_url}?dropbox=error&message=invalid_user"
        )

    try:
        token = await dropbox_service.exchange_code(db, user_id, code)
        return RedirectResponse(
            url=f"{frontend_url}?dropbox=connected&account={token.account_id or ''}"
        )
    except Exception as e:
        return RedirectResponse(
            url=f"{frontend_url}?dropbox=error&message={str(e)[:100]}"
        )


@router.post("/sync")
async def sync_dropbox(
    limit: int = Query(default=10, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Sync FIT files from the configured Dropbox folder."""
    try:
        rides = await dropbox_service.sync_fit_files(db, current_user.id, limit=limit)

        # Recalculate PMC if new rides were imported
        if rides:
            earliest_date_str = min(r["date"] for r in rides)
            try:
                earliest = datetime.fromisoformat(
                    earliest_date_str.replace(" ", "T")
                ).date()
            except (ValueError, AttributeError):
                from datetime import date
                earliest = date.today()
            recalculate_from_date(db, current_user.id, earliest)

        return {
            "synced": len(rides),
            "rides": rides,
        }
    except ValueError as e:
        raise BadRequestException(detail=str(e))
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("Dropbox sync failed")
        raise BadRequestException(
            detail="Dropbox sync failed. If it keeps happening, disconnect and connect Dropbox again in Settings, then Data in."
        )


@router.get("/status")
def dropbox_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check Dropbox connection status."""
    return dropbox_service.get_connection_status(db, current_user.id)


@router.patch("/folder")
def update_folder_path(
    body: FolderPathUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update the Dropbox folder path to scan for FIT files."""
    token = db.query(DropboxToken).filter(
        DropboxToken.user_id == current_user.id
    ).first()
    if not token:
        raise BadRequestException(detail="Dropbox isn't connected. Settings, then Data in, then Connect Dropbox.")

    token.folder_path = body.folder_path
    db.commit()
    return {"folder_path": token.folder_path}


@router.delete("")
def disconnect_dropbox(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Disconnect Dropbox integration."""
    dropbox_service.disconnect(db, current_user.id)
    return {"status": "disconnected"}
