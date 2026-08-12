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


# ---------------------------------------------------------------------------
# Founding hundred (rider numbers)
# ---------------------------------------------------------------------------

@router.post("/founding/assign")
def assign_founding_number(
    email: str = Query(...),
    number: int | None = Query(None, ge=1, le=100),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Give a rider their founding number. Explicit number, or next free.
    Idempotent: a rider who already has one keeps it. The ledger is the
    arbiter: a number stays worn even after its rider's account is purged."""
    from datetime import datetime

    from app.api.v1.auth import issue_founding_number
    from app.core.exceptions import BadRequestException, NotFoundException
    from app.models.founding import FoundingLedger

    user = db.query(User).filter(User.email == email.lower().strip()).first()
    if user is None:
        raise NotFoundException(detail="No rider with that email")
    if user.founding_number is not None:
        return {"email": user.email, "founding_number": user.founding_number}

    if number is not None:
        if db.get(FoundingLedger, number) is not None:
            raise BadRequestException(detail=f"Number {number} is already worn")
        db.add(
            FoundingLedger(
                number=number, user_id=str(user.id), issued_at=datetime.utcnow()
            )
        )
        user.founding_number = number
        db.commit()
    else:
        if issue_founding_number(db, user) is None:
            raise BadRequestException(detail="The hundred are all in")
    return {"email": user.email, "founding_number": user.founding_number}


@router.get("/founding")
def list_founding(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    from app.models.founding import FoundingLedger

    rows = (
        db.query(User)
        .filter(User.founding_number.isnot(None))
        .order_by(User.founding_number.asc())
        .all()
    )
    issued = db.query(FoundingLedger).count()
    return {
        # issued counts every number ever worn (ledger); riders lists the
        # living accounts. A gap between the two = departed founding riders.
        "issued": issued,
        "count": len(rows),
        "riders": [
            {"number": r.founding_number, "email": r.email, "name": r.full_name}
            for r in rows
        ],
    }


@router.get("/signals")
def kill_criteria_signals(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """The kill-criteria stopwatch (PRD: waitlist +20/week; retention watched
    from cohort activity). One glance answers: is demand real this week?"""
    from app.models.waitlist import WaitlistEntry

    now = datetime.utcnow()
    total = db.query(WaitlistEntry).count()
    weeks = []
    for w in range(4):
        start = now - timedelta(days=7 * (w + 1))
        end = now - timedelta(days=7 * w)
        adds = (
            db.query(WaitlistEntry)
            .filter(WaitlistEntry.created_at >= start, WaitlistEntry.created_at < end)
            .count()
        )
        weeks.append({"week_ending": end.date().isoformat(), "adds": adds})

    riders = db.query(User).filter(User.is_active.is_(True)).count()
    active_14d = (
        db.query(FormaCall.user_id)
        .filter(FormaCall.ts >= now - timedelta(days=14))
        .distinct()
        .count()
    )
    return {
        "waitlist_total": total,
        "waitlist_adds_by_week": weeks,
        "target_adds_per_week": 20,
        "riders_active_accounts": riders,
        "riders_used_coach_last_14d": active_14d,
    }
