"""Public waitlist endpoint (the landing page form posts here) + admin views.

Joining stores the signal and fires Letter 0. Idempotent: rejoining answers
identically, so the form never leaks who is already on the list.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.api.v1.admin import require_admin
from app.core.ratelimit import rate_limit
from app.database import get_db
from app.models.user import User
from app.models.waitlist import WaitlistEntry
from app.services import email_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/waitlist", tags=["waitlist"])

_join_limit = rate_limit(5, 600)  # 5 joins / 10 min / IP


class JoinBody(BaseModel):
    email: EmailStr


async def _send_letter0(entry_id: str, email: str) -> None:
    from app.database import SessionLocal

    ok = await email_service.send_waitlist_welcome(email)
    if ok and email_service.is_configured():
        db = SessionLocal()
        try:
            entry = db.get(WaitlistEntry, entry_id)
            if entry:
                entry.letter0_sent = True
                db.commit()
        finally:
            db.close()


@router.post("", dependencies=[Depends(_join_limit)])
async def join_waitlist(
    body: JoinBody,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    email = body.email.lower().strip()
    entry = db.query(WaitlistEntry).filter(WaitlistEntry.email == email).first()
    if entry is None:
        entry = WaitlistEntry(email=email)
        db.add(entry)
        db.commit()
        db.refresh(entry)
        background_tasks.add_task(_send_letter0, str(entry.id), email)
    return {"status": "held"}


@router.get("/admin", dependencies=[Depends(require_admin)])
def list_waitlist(db: Session = Depends(get_db)):
    rows = db.query(WaitlistEntry).order_by(WaitlistEntry.created_at.asc()).all()
    return {
        "count": len(rows),
        "entries": [
            {
                "email": r.email,
                "joined": r.created_at.isoformat(),
                "letter0_sent": r.letter0_sent,
            }
            for r in rows
        ],
    }


@router.post("/admin/send-pending", dependencies=[Depends(require_admin)])
async def send_pending_letter0(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Retry Letter 0 for joiners it never reached (e.g. sends attempted
    while Postmark approval was still pending)."""
    pending = (
        db.query(WaitlistEntry).filter(WaitlistEntry.letter0_sent.is_(False)).all()
    )
    for entry in pending:
        background_tasks.add_task(_send_letter0, str(entry.id), entry.email)
    return {"queued": len(pending)}
