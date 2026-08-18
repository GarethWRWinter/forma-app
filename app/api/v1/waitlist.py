"""Public waitlist endpoint (the landing page form posts here) + admin views.

Joining stores the signal and fires Letter 0. Idempotent: rejoining answers
identically, so the form never leaks who is already on the list.

The queue is deliberately visible. A hundred places is a real constraint, not
a line of copy: Gareth reads every founding rider's first month himself, and a
hundred is what one person can actually do. Handing back a position and a
total lets the demand make that argument instead of the marketing.
"""

import logging

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.api.v1.admin import require_admin
from app.config import settings
from app.core.exceptions import BadRequestException
from app.core.ratelimit import rate_limit
from app.database import get_db
from app.models.user import User
from app.models.waitlist import WaitlistEntry
from app.services import email_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/waitlist", tags=["waitlist"])

_join_limit = rate_limit(5, 600)  # 5 joins / 10 min / IP
_goal_limit = rate_limit(10, 600)  # 10 answers / 10 min / IP

# Share links point at the public site, never at settings.frontend_url. This
# link gets forwarded into club WhatsApp groups and read weeks later, and a
# preview deployment URL would be dead by then.
SITE_URL = "https://ridewithforma.com"

# The number of places. Real, and the reason the list is worth queueing for.
PLACES = 100


class JoinBody(BaseModel):
    email: EmailStr
    # Optional at the API so an older cached page cannot start 422ing, but the
    # form asks for it: the letters are sent by hand and a greeting with no name
    # in it undercuts the whole promise of the list.
    name: str | None = Field(None, max_length=80)
    ref: str | None = Field(None, max_length=16)


class GoalBody(BaseModel):
    token: str = Field(max_length=36)
    goal: str = Field(max_length=280)


async def _send_letter0(
    entry_id: str, email: str, name: str | None = None, position: int | None = None
) -> None:
    """Only runs when waitlist_autosend is on, which it is not.

    Letter 0 goes out by hand: it asks for a reply and promises that a person
    reads every one, and that promise is easiest to keep by writing them
    yourself. Joining just records who is owed a letter.
    """
    from app.database import SessionLocal

    ok = await email_service.send_waitlist_welcome(email, name=name, position=position)
    if ok and email_service.is_configured():
        db = SessionLocal()
        try:
            entry = db.get(WaitlistEntry, entry_id)
            if entry:
                entry.letter0_sent = True
                db.commit()
        finally:
            db.close()


def _total(db: Session) -> int:
    """One count, no joins, no rows returned. /stats runs this on every load."""
    return db.query(func.count(WaitlistEntry.id)).scalar() or 0


def _position(db: Session, entry: WaitlistEntry) -> int:
    """Count who is genuinely ahead: more referrals first, then whoever got
    here earlier. Derived on every read rather than stored, because a stored
    number would be wrong from the next join onwards and one referral would
    mean rewriting the whole list."""
    ahead = (
        db.query(func.count(WaitlistEntry.id))
        .filter(
            or_(
                WaitlistEntry.referrals > entry.referrals,
                and_(
                    WaitlistEntry.referrals == entry.referrals,
                    WaitlistEntry.created_at < entry.created_at,
                ),
            )
        )
        .scalar()
    )
    return 1 + (ahead or 0)


def _queue(db: Session) -> list[WaitlistEntry]:
    """The whole list in queue order, using the same rule _position derives
    one row's answer from: referrals first, then who arrived earlier."""
    return (
        db.query(WaitlistEntry)
        .order_by(WaitlistEntry.referrals.desc(), WaitlistEntry.created_at.asc())
        .all()
    )


def _held(db: Session, entry: WaitlistEntry) -> dict:
    """The only answer the form ever gives, first join or fiftieth. Same shape
    either way, so nobody can use it to test whether an address is on the list."""
    return {
        "status": "held",
        "position": _position(db, entry),
        "total": _total(db),
        "code": entry.code,
        "token": str(entry.id),
        "share_url": f"{SITE_URL}/?ref={entry.code}",
    }


@router.post("", dependencies=[Depends(_join_limit)])
async def join_waitlist(
    body: JoinBody,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    email = body.email.lower().strip()
    name = (body.name or "").strip() or None
    ref = (body.ref or "").strip().upper()
    entry = db.query(WaitlistEntry).filter(WaitlistEntry.email == email).first()
    if entry is None:
        referred_by = None
        if ref:
            # Credit the referrer in SQL rather than reading, adding and
            # writing back in Python: two referrals landing in the same second
            # must not overwrite each other. The row count doubles as the
            # answer to whether the code was real.
            #
            # This branch is the only place a referral is ever counted, and it
            # only runs for an address that was not on the list a moment ago.
            # So a rejoin credits nobody however many times it is submitted,
            # and nobody can refer themselves: a joiner has no code yet.
            credited = (
                db.query(WaitlistEntry)
                .filter(WaitlistEntry.code == ref)
                .update(
                    {WaitlistEntry.referrals: WaitlistEntry.referrals + 1},
                    synchronize_session=False,
                )
            )
            # An unknown code is dropped in silence. The place is still held,
            # and a link someone mistyped never becomes an error on the page.
            referred_by = ref if credited else None
        entry = WaitlistEntry(email=email, name=name, referred_by=referred_by)
        db.add(entry)
        db.commit()
        db.refresh(entry)
        pos = _position(db, entry)
        if settings.waitlist_autosend:
            background_tasks.add_task(_send_letter0, str(entry.id), email, name, pos)
        else:
            # Nothing sends. letter0_sent stays False, which is what puts them
            # on the list of people owed a letter in the admin CSV.
            logger.info(
                "WAITLIST JOIN: %s (%s) is number %s and is owed Letter 0",
                email, name or "no name", pos,
            )
    elif name and not entry.name:
        # Someone who joined before the form asked for a name, coming back with
        # one. Take it: it costs nothing and it makes their next letter better.
        entry.name = name
        db.commit()
    return _held(db, entry)


@router.get("/stats")
def waitlist_stats(db: Session = Depends(get_db)):
    """What the landing page reads to show the queue. Public, no auth, no
    names, one count: it is called on every page load and has to stay free."""
    return {"total": _total(db), "places": PLACES}


@router.post("/goal", dependencies=[Depends(_goal_limit)])
def save_goal(body: GoalBody, db: Session = Depends(get_db)):
    """The answer to the one question, saved against the token from join.

    An unknown token gets saved:false, not a 404, for the same reason join is
    idempotent: this endpoint must not become a way to find out which tokens
    are real.
    """
    goal = body.goal.strip()
    entry = db.get(WaitlistEntry, body.token)
    if entry is None or not goal:
        return {"saved": False}
    entry.goal = goal
    db.commit()
    return {"saved": True}


@router.get("/admin.csv", dependencies=[Depends(require_admin)])
def export_waitlist_csv(db: Session = Depends(get_db)):
    """The list as a CSV, in queue order, ready to paste straight into a sheet.

    The letters go out by hand, one at a time, so this is the working document
    for that job: who they are, what they said, and where they sit. Queue order
    rather than join order, because that is the order they get called up in.
    """
    import csv
    import io

    from fastapi.responses import Response

    rows = _queue(db)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["position", "name", "email", "goal", "referrals", "code",
         "referred_by", "letter0_sent", "joined"]
    )
    for i, r in enumerate(rows, start=1):
        w.writerow(
            [i, r.name or "", r.email, r.goal or "", r.referrals, r.code,
             r.referred_by or "", "yes" if r.letter0_sent else "no",
             r.created_at.strftime("%Y-%m-%d")]
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="forma-waitlist.csv"'
        },
    )


@router.get("/admin", dependencies=[Depends(require_admin)])
def list_waitlist(db: Session = Depends(get_db)):
    rows = db.query(WaitlistEntry).order_by(WaitlistEntry.created_at.asc()).all()
    return {
        "count": len(rows),
        "entries": [
            {
                "email": r.email,
                "name": r.name,
                "joined": r.created_at.isoformat(),
                "letter0_sent": r.letter0_sent,
                # The goals are the point of asking. Surfaced here so the
                # answers are readable without opening psql.
                "code": r.code,
                "referred_by": r.referred_by,
                "referrals": r.referrals,
                "goal": r.goal,
            }
            for r in rows
        ],
    }


@router.post("/admin/send-pending", dependencies=[Depends(require_admin)])
async def send_pending_letter0(
    since: str = Query(
        ...,
        description="Only joiners from this date onwards (YYYY-MM-DD). Required.",
    ),
    confirm: bool = Query(False, description="Must be true before anything sends."),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    """Retry Letter 0 for joiners a send never reached.

    Both guards exist because of who is on this list. The riders who joined
    before autosend was switched on are written to personally, by hand, and
    they all sit at letter0_sent False until that happens. An unscoped backfill
    would mail every one of them a machine-written letter, which is precisely
    the promise this waitlist is built on not breaking. So `since` is required
    and `confirm` defaults to false: getting it wrong takes two mistakes.
    """
    try:
        cutoff = datetime.strptime(since, "%Y-%m-%d")
    except ValueError:
        raise BadRequestException(detail="since must be YYYY-MM-DD")

    pending = (
        db.query(WaitlistEntry)
        .filter(
            WaitlistEntry.letter0_sent.is_(False),
            WaitlistEntry.created_at >= cutoff,
        )
        .order_by(WaitlistEntry.created_at.asc())
        .all()
    )

    if not confirm:
        return {
            "would_send": len(pending),
            "emails": [e.email for e in pending],
            "sent": 0,
            "note": "Dry run. Re-send with confirm=true once this list looks right.",
        }

    for entry in pending:
        background_tasks.add_task(
            _send_letter0, str(entry.id), entry.email, entry.name, _position(db, entry)
        )
    return {"queued": len(pending)}
