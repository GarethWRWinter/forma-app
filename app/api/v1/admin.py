"""Admin — per-user Forma cost dashboard (reads the forma_calls ledger).

Gated on settings.admin_emails; with the default empty list, nobody can
read it. The PRD's commercial guardrails live here: the $8/user/month
alert threshold and the ~$1.87/user cost model are checked against these
numbers, not the Anthropic invoice.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user, get_db
from app.config import settings
from app.core.exceptions import ForbiddenException
from app.models.forma_call import FormaCall
from app.models.user import User

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.email not in settings.admin_emails:
        raise ForbiddenException(detail="Not authorised")
    return current_user


@router.get("/costs")
def get_costs(
    days: int = Query(30, ge=1, le=365),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Forma spend for the window: totals, per user, and per task."""
    since = datetime.utcnow() - timedelta(days=days)
    base = db.query(FormaCall).filter(FormaCall.ts >= since)

    totals = base.with_entities(
        func.count(FormaCall.id),
        func.coalesce(func.sum(FormaCall.cost_cents), 0.0),
        func.coalesce(func.sum(FormaCall.input_tokens), 0),
        func.coalesce(func.sum(FormaCall.output_tokens), 0),
        func.coalesce(func.sum(FormaCall.cache_read_tokens), 0),
    ).one()
    error_count = base.filter(FormaCall.error.is_(True)).count()

    per_user = (
        base.with_entities(
            FormaCall.user_id,
            func.count(FormaCall.id),
            func.coalesce(func.sum(FormaCall.cost_cents), 0.0),
        )
        .group_by(FormaCall.user_id)
        .order_by(func.sum(FormaCall.cost_cents).desc())
        .all()
    )

    per_task = (
        base.with_entities(
            FormaCall.task,
            FormaCall.model,
            func.count(FormaCall.id),
            func.coalesce(func.sum(FormaCall.cost_cents), 0.0),
            func.coalesce(func.avg(FormaCall.latency_ms), 0.0),
        )
        .group_by(FormaCall.task, FormaCall.model)
        .order_by(func.sum(FormaCall.cost_cents).desc())
        .all()
    )

    return {
        "window_days": days,
        "calls": totals[0],
        "cost_usd": round(totals[1] / 100, 4),
        "tokens_in": totals[2],
        "tokens_out": totals[3],
        "cache_read_tokens": totals[4],
        "errors": error_count,
        "per_user": [
            {"user_id": u, "calls": c, "cost_usd": round(cents / 100, 4)}
            for u, c, cents in per_user
        ],
        "per_task": [
            {
                "task": t,
                "model": m,
                "calls": c,
                "cost_usd": round(cents / 100, 4),
                "avg_latency_ms": round(lat),
            }
            for t, m, c, cents, lat in per_task
        ],
    }


# ---------------------------------------------------------------------------
# Invite codes (closed beta door keys)
# ---------------------------------------------------------------------------

@router.post("/invites")
def create_invites(
    count: int = Query(1, ge=1, le=100),
    max_uses: int = Query(1, ge=1, le=1000),
    note: str = Query("", max_length=255),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Mint invite codes. Readable, unambiguous alphabet (no O/0/I/1)."""
    import secrets

    from app.models.invite import InviteCode

    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    codes = []
    for _i in range(count):
        code = "FORMA-" + "".join(secrets.choice(alphabet) for _ in range(6))
        db.add(InviteCode(code=code, max_uses=max_uses, note=note or None))
        codes.append(code)
    db.commit()
    return {"codes": codes}


@router.get("/invites")
def list_invites(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    from app.models.invite import InviteCode

    rows = db.query(InviteCode).order_by(InviteCode.created_at.desc()).all()
    return {
        "invites": [
            {
                "code": r.code,
                "note": r.note,
                "uses": r.uses,
                "max_uses": r.max_uses,
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            }
            for r in rows
        ]
    }
