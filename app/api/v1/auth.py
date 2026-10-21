import logging
from datetime import date, datetime
from types import SimpleNamespace

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import COUNTRY_ALIASES, LegalDoc, settings
from app.core.exceptions import (
    BadRequestException,
    ConflictException,
    ForbiddenException,
    NotFoundException,
    UnauthorizedException,
)
from app.core.ratelimit import (
    EmailLimit,
    FailureLimit,
    SurgeAlarm,
    client_ip,
    rate_limit,
    read_client_ip,
)
from app.core.security import (
    create_email_token,
    hash_password,
    verify_email_token,
    verify_password,
)
from app.api.v1.deps import get_current_user
from app.database import get_db
from app.models.safety import SafetyEvent
from app.models.user import User
from app.schemas.user import NormalisedEmail, TokenRefresh, TokenResponse, UserCreate, UserLogin, UserResponse
from app.services import email_service, safety_service, token_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# Per-IP caps on the sensitive auth endpoints (window in seconds). The
# address is ratelimit.client_ip: the rider's own, through Vercel or not.
_login_limit = rate_limit(10, 60)      # 10 login attempts / minute
_register_limit = rate_limit(5, 300)   # 5 signups / 5 minutes
_refresh_limit = rate_limit(30, 60)    # 30 refreshes / minute
_email_limit = rate_limit(5, 600)      # 5 reset/verify emails / 10 minutes

# Per-account backstops that hold however many addresses an attacker has:
# guessing one rider's password, or filling one inbox with reset emails.
# Same answer for an email with no account, so neither reveals which do.
LOGIN_EMAIL_LIMITED = (
    "Too many login attempts for this email. Wait {wait} and try again, or "
    "reset your password from the login page."
)
RESET_EMAIL_LIMITED = (
    "Too many reset requests for this email. Check your inbox and spam folder "
    "for the last link, or wait {wait} and try again."
)
_login_email_limit = EmailLimit(10, 15 * 60, LOGIN_EMAIL_LIMITED)   # 10 / 15 minutes
_reset_email_limit = EmailLimit(5, 60 * 60, RESET_EMAIL_LIMITED)    # 5 / hour

# Failed sign-ups per email, from anywhere (review round 3, new problem 2):
# the address limit above can be split across addresses, this can't. Only
# failures count, and only a failure is ever refused by it, so nobody who
# knows a rider's email can lock them out of joining (round 4, new problem
# F): the rider's own correct sign-up always goes through.
REGISTER_EMAIL_LIMITED = (
    "Too many sign-ups with this email have failed in the last hour. Check every "
    "detail and try again, or wait {wait} for the form to say what's wrong."
)
_register_email_limit = EmailLimit(5, 60 * 60, REGISTER_EMAIL_LIMITED)  # 5 failures / hour

# A refused sign-up that counts as a failed one: a wrong or missing invite
# code, a form that doesn't pass, an email that already has an account.
# A request turned away by a limit tried nothing, so it isn't counted.
_FAILED_SIGN_UP = (400, 409)

# Wrong invite codes, per address (round 4, new problem C). The launch codes
# are single words, so a guesser could otherwise work through a dictionary.
# After 10 wrong codes in an hour, that address tries no code at all, right
# or wrong, until the oldest falls out of the hour. Every other address is
# untouched: nothing here pauses sign-up for everyone.
INVITE_WRONG_PER_ADDRESS = 10
INVITE_GUESSES_LIMITED = (
    "Too many wrong invite codes have been tried from this connection in the last "
    "hour. Wait {wait} and try again, or reply to your invite email and Gareth will "
    "sort it out."
)
_invite_failures = FailureLimit(INVITE_WRONG_PER_ADDRESS, 60 * 60, INVITE_GUESSES_LIMITED)

# Guessing spread over many addresses (a botnet, or forged first hops while
# FORMA_EDGE_SECRET isn't set) gets past the per-address limit. Past 200
# wrong codes in an hour from everyone together, Gareth is emailed, at most
# once an hour. Nobody is refused for it.
INVITE_ALERT_AT = 200
_invite_surge = SurgeAlarm(INVITE_ALERT_AT, 60 * 60)

# GET /auth/ip-check, which anyone can call.
_ip_check_limit = rate_limit(10, 60)


def _frontend() -> str:
    return settings.frontend_url or "http://localhost:3000"


def _verify_link(user_id: str) -> str:
    token = create_email_token(user_id, "verify", hours=24)
    return f"{_frontend()}/verify-email?token={token}"


async def _send_verification_email(user_id: str, email: str, full_name: str | None) -> None:
    await email_service.send_verification(email, full_name, _verify_link(user_id))


async def _send_welcome_email(
    user_id: str,
    email: str,
    full_name: str | None,
    terms_version: str,
    accepted_at: datetime,
    terms_text: str,
    privacy_version: str,
) -> None:
    """Sign-up's one email: the confirm link plus the record of what they
    agreed to, with the full text of the terms they accepted."""
    await email_service.send_registration_welcome(
        email, full_name, _verify_link(user_id), terms_version, accepted_at,
        terms_text=terms_text, privacy_version=privacy_version,
    )

# Verified against when the email is unknown, so a login attempt takes the same
# time whether or not the account exists (defeats timing-based user enumeration).
_DUMMY_HASH = hash_password("forma-nonexistent-account-placeholder")


# === Who can join (terms section 3): adults, in a territory Forma serves ===

ADULT_AGE = 18
# Past this, the year is a slip of the thumb, not a rider.
_OLDEST_PLAUSIBLE = 120
# The age row in consent_events. The date itself lives on the user.
AGE_CONSENT_TEXT = "Date of birth given: 18 or over"

STALE_FORM = "The sign-up page has changed since you opened it. Refresh the page and try again."
NO_DATE_OF_BIRTH = "Add your date of birth. Forma is for adults, 18 and over."
BAD_DATE_OF_BIRTH = "That date of birth doesn't look right. Check the year and try again."
UNDER_18 = (
    "Forma is for adults, so you need to be 18 or over to join. A British Cycling "
    "club can put you in touch with a qualified youth coach."
)
NO_COUNTRY = "Pick where you live, so the coach gives you the right emergency numbers."
BAD_COUNTRY = "Pick where you live from the list, so the coach gives you the right emergency numbers."
TERMS_UNTICKED = "Tick the first box to agree to the terms. Forma can't coach you without it."
HEALTH_UNTICKED = (
    "Tick the second box so Forma can use the health details you share with it. "
    "The coaching can't work without them."
)


def _today() -> date:
    """The server's date. The age gate is judged on it, never the phone's."""
    return datetime.utcnow().date()


def age_on(born: date, today: date) -> int:
    """Whole years old on `today`. Someone born on 29 February turns a year
    older on 1 March in a common year, never a day early."""
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def is_real_birth_date(born: date) -> bool:
    """Not in the future, and not past the oldest plausible rider."""
    today = _today()
    return born <= today and age_on(born, today) <= _OLDEST_PLAUSIBLE


def check_birth_date(born: date | None) -> date:
    """A real date of birth, of any age, or a sentence saying why it won't do."""
    if born is None:
        raise BadRequestException(detail=NO_DATE_OF_BIRTH)
    if not is_real_birth_date(born):
        raise BadRequestException(detail=BAD_DATE_OF_BIRTH)
    return born


def is_under_18(born: date) -> bool:
    return age_on(born, _today()) < ADULT_AGE


def check_age(born: date | None) -> date:
    """The date of birth, or a sentence saying why it won't do."""
    born = check_birth_date(born)
    if is_under_18(born):
        raise BadRequestException(detail=UNDER_18)
    return born


def _not_available(code: str) -> str:
    where = "the US or Canada" if code in {"US", "CA"} else "your country"
    return (
        f"Forma isn't available in {where} yet. Join the list at ridewithforma.com "
        "and I'll tell you when it is."
    )


def check_country(raw: str | None) -> str:
    """ISO 3166 alpha-2, upper case, and on the allowlist (settings.
    allowed_countries: the UK only until the terms say otherwise). An
    allowlist, not a blocklist: a code nobody thought to block (Puerto Rico,
    Guam, a made-up "ZZ") is refused like the US and Canada are."""
    code = (raw or "").strip().upper()
    code = COUNTRY_ALIASES.get(code, code)
    if not code:
        raise BadRequestException(detail=NO_COUNTRY)
    if len(code) != 2 or not (code.isascii() and code.isalpha()):
        raise BadRequestException(detail=BAD_COUNTRY)
    if code not in settings.allowed_countries:
        raise BadRequestException(detail=_not_available(code))
    return code


@router.get("/config")
def auth_config():
    """Public flags the entry pages need before anyone is logged in. The
    sign-up page builds its country list from allowed_countries, so the
    page and the server can't disagree about who can join."""
    return {
        "invite_required": settings.require_invite,
        "allowed_countries": list(settings.allowed_countries),
    }


@router.get("/ip-check", dependencies=[Depends(_ip_check_limit)])
def ip_check(request: Request, response: Response):
    """What the rate limits make of this request, so the keying can be
    checked on a real one after a deploy (review round 3, new problem 6):
    open /api/v1/auth/ip-check through the app and client_ip should be your
    own address with via_edge true; call Railway directly and via_edge is
    false. Three facts, never the raw headers.

    Only once FORMA_EDGE_SECRET is set (review round 4, new problem G).
    Until then the first hop is trusted on any x-vercel-id, which a direct
    caller can forge, and reporting what was read (via_edge true, or a
    client_ip that is the hop they made up) would tell them the forgery
    works. So client_ip and via_edge are null and hops is left out, the
    same answer whatever the request carries."""
    response.headers["Cache-Control"] = "no-store"
    if not settings.edge_secret:
        return {"client_ip": None, "via_edge": None}
    reading = read_client_ip(request)
    return {"client_ip": reading.ip, "hops": reading.hops, "via_edge": reading.via_edge}


_LEGAL_DOCS = {"terms": settings.terms_doc, "privacy": settings.privacy_doc}

LEGAL_DOC_UNAVAILABLE = (
    "This document can't be shown just now. Email gareth@ridewithforma.com "
    "and I'll send you a copy."
)


def current_legal_doc(get) -> LegalDoc:
    """A published document for a consent row. One whose file is missing is
    still used, stamped "#missing" (config.published_legal_doc), and every
    row recorded against it is logged as an error so the gap is seen."""
    doc = get()
    if doc.missing:
        logger.error(
            "Consent recorded against %s, whose published file is missing", doc.doc_version
        )
    return doc


@router.get("/legal/{doc}")
def legal_document(doc: str):
    """The current published terms or privacy policy, word for word from
    prd/legal/published/, with the stamp consent rows carry. The /terms and
    /privacy pages can render this, so they always show the text riders
    are recorded as agreeing to."""
    get = _LEGAL_DOCS.get(doc)
    if get is None:
        raise NotFoundException(detail="There's no document by that name.")
    published = get()
    if published.missing:
        # Never serve the stand-in as if it were the published text.
        raise HTTPException(status_code=503, detail=LEGAL_DOC_UNAVAILABLE)
    return {"version": published.version, "doc_version": published.doc_version, "text": published.text}


def _invite_abuse_note(addresses: int) -> str:
    """The alert's text for Gareth: what happened, whether the addresses
    can be trusted, and what to do. Internal, and under the alert's 500
    characters."""
    if settings.edge_secret:
        trust = "FORMA_EDGE_SECRET is set, so these are real addresses."
    else:
        trust = (
            "FORMA_EDGE_SECRET isn't set, so each try can claim a new address. "
            "Set it on Vercel and Railway."
        )
    return (
        f"More than {INVITE_ALERT_AT} wrong invite codes were tried in the last hour, "
        f"from {addresses} different addresses. Sign-up stays open: only an address "
        f"with {INVITE_WRONG_PER_ADDRESS} wrong codes in the hour is stopped. {trust} "
        "If a launch code is a single word, swap it for codes made on the admin page, "
        "which can't be guessed."
    )


async def _alert_gareth_invite_abuse(note: str) -> None:
    """The invite-guessing email. Never raises: it runs after the guesser
    has their answer."""
    try:
        sent = await email_service.send_safety_alert("invite_abuse", "", "", note)
    except Exception:
        logger.exception("Invite guessing alert email failed")
        return
    if not sent:
        logger.error("Invite guessing alert email not sent")


def _redeem_invite(
    db: Session,
    code: str | None,
    address: str,
    background_tasks: BackgroundTasks,
) -> str | None:
    """Validate and consume one use of an invite code. Returns the
    normalised code, or raises. No-op (returns None) when the door is open.

    A wrong code counts against the caller's address (_invite_failures) and
    towards the alarm for everyone (_invite_surge). When the alarm is due,
    the email to Gareth goes on background_tasks, so the caller must hand
    those to the response it returns, refusal included."""
    from datetime import datetime

    from app.models.invite import InviteCode

    if not settings.require_invite:
        return code.strip().upper() if code else None
    if not code or not code.strip():
        raise BadRequestException(
            detail="Forma is invite-only for now, so you'll need the code from your invite email. No invite yet? Join the list at ridewithforma.com."
        )
    # Before the code is looked at: once this address has used up its wrong
    # guesses, its right code waits too, or guessing would carry on
    # regardless. Any other address goes straight on.
    _invite_failures.check(address)
    normalised = code.strip().upper()
    # Row-lock so two simultaneous signups can't share a single-use code.
    invite = (
        db.query(InviteCode)
        .filter(InviteCode.code == normalised)
        .with_for_update()
        .first()
    )
    if invite is None or invite.uses >= invite.max_uses or (
        invite.expires_at is not None and invite.expires_at < datetime.utcnow()
    ):
        _invite_failures.failed(address)
        addresses = _invite_surge.record(address)
        if addresses is not None:
            note = _invite_abuse_note(addresses)
            logger.warning("Invite guessing alarm: %s", note)
            background_tasks.add_task(_alert_gareth_invite_abuse, note)
        raise BadRequestException(
            detail="That invite code doesn't work. Check it against your invite email, and if it still won't go through, reply to that email and Gareth will sort it out."
        )
    invite.uses += 1
    return normalised


FOUNDING_CAP = 100


def _next_founding_number(db: Session) -> int | None:
    """Next free rider number from the permanent ledger, or None once the
    hundred are in. The ledger keeps every number ever issued (even after an
    account purge), so a number can never be reissued."""
    from sqlalchemy import func

    from app.models.founding import FoundingLedger

    taken = db.query(func.max(FoundingLedger.number)).scalar() or 0
    return taken + 1 if taken < FOUNDING_CAP else None


def issue_founding_number(db: Session, user: User) -> int | None:
    """Reserve the next number on the ledger for this rider. The ledger's
    primary key arbitrates races; a couple of retries absorb collisions.
    Returns the number, or None if the hundred are in (or contention wins)."""
    from app.models.founding import FoundingLedger

    for _ in range(3):
        nxt = _next_founding_number(db)
        if nxt is None:
            return None
        db.add(FoundingLedger(number=nxt, user_id=str(user.id)))
        user.founding_number = nxt
        try:
            db.commit()
            return nxt
        except IntegrityError:
            db.rollback()
    logger.warning("founding number contention unresolved for %s", user.email)
    return None


@router.post("/register", response_model=UserResponse, status_code=201,
             dependencies=[Depends(_register_limit)])
async def register(
    user_in: UserCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Open an account. A refusal that counts as a failed sign-up (a 400 or
    a 409) is counted against the email, from wherever it came; past the
    email's limit, a failing request gets 429 in place of its reason. A
    request that would succeed is never counted or refused by it, whatever
    anyone else has sent with the same email (review round 4, new problem
    F). Refusals are returned, not raised, so an alert queued on
    background_tasks still goes."""
    address = client_ip(request) or "unknown"
    try:
        user, terms_doc, privacy_doc, now = _open_account(
            db, user_in, request, address, background_tasks
        )
    except HTTPException as refused:
        answer = refused
        if refused.status_code in _FAILED_SIGN_UP:
            try:
                _register_email_limit.check(user_in.email)
            except HTTPException as limited:
                answer = limited
        return JSONResponse(
            status_code=answer.status_code,
            content={"detail": answer.detail},
            headers=answer.headers,
            background=background_tasks,
        )

    # A validated invite is a founding rider: number them on the way in.
    # Open-door signups (require_invite off) track their code but are not
    # founding; the hundred only count when the door is actually gated.
    if user.invited_with and settings.require_invite:
        issue_founding_number(db, user)

    # Best-effort: a failed email must never block the signup itself.
    background_tasks.add_task(
        _send_welcome_email, str(user.id), user.email, user.full_name,
        terms_doc.version, now, terms_doc.text, privacy_doc.version,
    )
    return user


def _open_account(
    db: Session,
    user_in: UserCreate,
    request: Request,
    address: str,
    background_tasks: BackgroundTasks,
):
    """Every sign-up check, then the account and its consent rows in one
    transaction. Returns (user, terms_doc, privacy_doc, accepted_at), or
    raises with the reason it can't."""
    # Every check runs before anything is written or an invite is spent, in
    # the order the form asks. A form that sends no box text was opened
    # before the boxes changed, and there is nothing true to record for it.
    terms_text = user_in.terms_text_shown or ""
    health_text = user_in.health_text_shown or ""
    if not terms_text.strip() or not health_text.strip():
        raise BadRequestException(detail=STALE_FORM)
    born = check_age(user_in.date_of_birth)
    country = check_country(user_in.country)
    if not user_in.terms_accepted:
        raise BadRequestException(detail=TERMS_UNTICKED)
    # Health details are the heart of the coaching, and special-category data
    # under UK GDPR: no explicit consent, no account.
    if not user_in.health_consent:
        raise BadRequestException(detail=HEALTH_UNTICKED)

    existing = db.query(User).filter(User.email == user_in.email).first()
    if existing:
        raise ConflictException(
            detail="There's already a Forma account with that email. Log in instead; if you've forgotten the password, the login page has a reset link."
        )

    invited_with = _redeem_invite(db, user_in.invite_code, address, background_tasks)

    # The documents as published now, read once so the consent rows and the
    # email name the same text: the terms box agrees to the terms, the health
    # box to the use the privacy policy describes, and the age row to the
    # terms' who-can-join rules.
    terms_doc = current_legal_doc(settings.terms_doc)
    privacy_doc = current_legal_doc(settings.privacy_doc)
    now = datetime.utcnow()
    user = User(
        email=user_in.email,
        hashed_password=hash_password(user_in.password),
        full_name=user_in.full_name,
        invited_with=invited_with,
        date_of_birth=born,
        country=country,
        terms_version=terms_doc.version,
        terms_accepted_at=now,
        health_consent_at=now,
    )
    db.add(user)
    try:
        # Flush for the id, then write the word-for-word record in the same
        # transaction: there is never an account without its consent rows.
        db.flush()
        for kind, doc, text in (
            ("terms", terms_doc, terms_text),
            ("health_data", privacy_doc, health_text),
            ("age", terms_doc, AGE_CONSENT_TEXT),
        ):
            safety_service.record_consent(
                db, user, kind, doc.doc_version, text, request,
                source="register", commit=False,
            )
        db.commit()
    except IntegrityError:
        # The only unique constraint in play here is the email (two
        # simultaneous signups): answer it honestly, not with a 500.
        db.rollback()
        raise ConflictException(
            detail="There's already a Forma account with that email. Log in instead; if you've forgotten the password, the login page has a reset link."
        )
    db.refresh(user)
    return user, terms_doc, privacy_doc, now


def needs_profile_consent(user) -> bool:
    """True for an account with no date of birth or no country on file: the
    beta riders, who joined before sign-up asked. The terms they accept are
    for adults in the countries Forma serves, so the re-acceptance asks for
    both and records the age row sign-up would have (review new problem 11)."""
    return getattr(user, "date_of_birth", None) is None or not getattr(user, "country", None)


class ReacceptTermsBody(BaseModel):
    # The exact wording of the box the rider ticked, recorded word for word.
    text_shown: str = Field(min_length=1, max_length=4000)
    # Required when the account has none on file (needs_profile_consent),
    # and checked by the same rules as sign-up whenever sent.
    date_of_birth: date | None = None
    country: str | None = Field(None, max_length=64)

    @field_validator("date_of_birth", mode="before")
    @classmethod
    def _blank_date_is_missing(cls, v):
        # An untouched date input posts "", which is a missing date, not a bad one.
        return None if isinstance(v, str) and not v.strip() else v


# === A date of birth under 18 on an existing account (review round 3, new problem 4) ===

# What the rider is told, here and on every later try: the words the coach's
# fixed under-18 reply uses (coach_service.MINOR_CLOSING), with the youth
# coach line sign-up gives.
MINOR_ACCOUNT = (
    "Forma is for adults, 18 and over, so I can't coach you or build you a plan. "
    "A British Cycling club can put you in touch with a qualified youth coach. "
    "This account is on hold and will be closed, and anything you've paid will be "
    "refunded. If you're 18 or over and this was a mistake, email "
    "gareth@ridewithforma.com and I'll sort it out."
)
MINOR_HOLD_REASON = "The date of birth given is under 18. Forma is for adults"
# An under-18 hold, which only Forma lifts (lift_kind "admin_only"); the
# event's source says where on the account the date came from.
MINOR_HOLD_SOURCE = "profile"


async def _alert_gareth_under_18(bind, user_email: str, user_id: str, excerpt: str, event_id: str) -> None:
    """The safeguarding email for an under-18 date of birth, then the event
    stamped as alerted, in a session of its own. Never raises: it runs
    after the rider has their answer."""
    try:
        sent = await email_service.send_safety_alert("minor", user_email, user_id, excerpt)
    except Exception:
        logger.exception("Under-18 alert email failed (user=%s)", user_id)
        return
    if not sent:
        logger.error("Under-18 alert email not sent (user=%s)", user_id)
        return
    session = sessionmaker(bind=bind)()
    try:
        event = session.get(SafetyEvent, event_id)
        if event is not None and event.founder_alerted_at is None:
            event.founder_alerted_at = datetime.utcnow()
            session.commit()
    except Exception:
        logger.exception("Stamping founder_alerted_at failed (user=%s)", user_id)
    finally:
        session.close()


def hold_as_under_18(
    db: Session,
    user: User,
    born: date,
    where: str,
    background_tasks: BackgroundTasks,
    event_source: str,
) -> bool:
    """Hold an account whose date of birth shows under 18, as the chat does
    when a rider says so (SAFETY LAW 2n): an under-18 hold that only Forma
    lifts, a red-flag event carrying the age (it sets how long the records
    are kept, safety_service.minor_retention_until), an email to Gareth, and
    no further renewal before he reviews it (billing_service).

    Once per account: one already held gets nothing new. Returns whether a
    hold was opened. The email and the Stripe call go on background_tasks,
    so the caller must hand those to the response it returns."""
    if safety_service.minor_hold(db, user) is not None:
        return False
    age = max(0, age_on(born, _today()))
    said = f"Date of birth {born.isoformat()} (age {age}) given {where}"
    hold = safety_service.open_hold(
        db, user, "hold_all", MINOR_HOLD_REASON, MINOR_HOLD_SOURCE,
        red_flag="minor", note=said, commit=False,
    )
    db.flush()
    event = SafetyEvent(
        user_id=user.id, kind="minor", source=event_source[:20], matched=said[:200],
        hold_id=hold.id, stated_age=age,
    )
    db.add(event)
    db.commit()
    logger.warning("Under-18 hold opened for user %s: %s", user.id, said)
    try:
        # Here, not at the top: plan_service imports a good deal.
        from app.services.plan_service import sync_hold_marks

        sync_hold_marks(db, user.id)
    except Exception:
        logger.exception("Marking held sessions failed (user=%s)", user.id)
        db.rollback()
    background_tasks.add_task(
        _alert_gareth_under_18, db.get_bind(), user.email, user.id, said, event.id
    )
    try:
        from app.services import billing_service

        customer = getattr(user, "stripe_customer_id", None)
        if customer and billing_service.is_configured():
            # A snapshot, not the row: the call runs after this session closes.
            snapshot = SimpleNamespace(id=user.id, stripe_customer_id=customer)
            background_tasks.add_task(billing_service.stop_renewal_for_review, snapshot)
    except Exception:
        logger.exception("Stopping renewal for an under-18 hold failed (user=%s)", user.id)
    return True


def under_18_response(background_tasks: BackgroundTasks) -> JSONResponse:
    """The refusal for an account held as under 18. Returned, not raised, so
    the alert and the renewal stop queued on background_tasks still run."""
    return JSONResponse(
        status_code=403, content={"detail": MINOR_ACCOUNT}, background=background_tasks
    )


@router.post("/reaccept-terms")
def reaccept_terms(
    body: ReacceptTermsBody,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Agree to the current terms after they change. The app asks whenever
    /users/me says terms_current is false, which includes the beta accounts
    that joined before there was any record to keep, or needs_profile_consent
    is true, when it also asks for a date of birth and country.

    The date of birth on file stands: a date sent only fills a missing one,
    never replaces it. A date under 18, sent or on file, holds the account
    as under 18 (hold_as_under_18) and answers 403 with MINOR_ACCOUNT, as
    does every later try, so an adult date sent next can't undo it. Every
    other refusal (a bad date, a country Forma doesn't serve) is made before
    anything is written, and leaves the old terms in place."""
    if not body.text_shown.strip():
        raise BadRequestException(detail="Refresh the page and tick the box again.")
    if safety_service.minor_hold(db, current_user) is not None:
        return under_18_response(background_tasks)
    profile_due = needs_profile_consent(current_user)
    on_file = current_user.date_of_birth
    on_file_real = on_file is not None and is_real_birth_date(on_file)
    sent = body.date_of_birth
    if sent is not None:
        sent = check_birth_date(sent)
    elif on_file is None:
        raise BadRequestException(detail=NO_DATE_OF_BIRTH)
    elif profile_due and not on_file_real:
        # Only the country was missing, and the age row is about to say "18
        # or over" of a date that isn't one.
        raise BadRequestException(detail=BAD_DATE_OF_BIRTH)

    for born in (sent, on_file if on_file_real else None):
        if born is not None and is_under_18(born):
            hold_as_under_18(
                db, current_user, born, "when re-accepting the terms",
                background_tasks, event_source="reaccept",
            )
            return under_18_response(background_tasks)
    if not on_file_real and sent is not None:
        on_file = sent
    country = current_user.country
    if (body.country or "").strip() or not country:
        country = check_country(body.country)

    terms_doc = current_legal_doc(settings.terms_doc)
    current_user.date_of_birth = on_file
    current_user.country = country
    current_user.terms_version = terms_doc.version
    current_user.terms_accepted_at = datetime.utcnow()
    safety_service.record_consent(
        db, current_user, "reaccept", terms_doc.doc_version, body.text_shown,
        request, source="reaccept", commit=False,
    )
    if profile_due or sent is not None:
        safety_service.record_consent(
            db, current_user, "age", terms_doc.doc_version, AGE_CONSENT_TEXT,
            request, source="reaccept", commit=False,
        )
    db.commit()
    return {"ok": True, "terms_version": terms_doc.version}


MINOR_HEALTH_CONSENT_REFUSAL = (
    "This account is on hold because Forma is for adults, 18 and over, so it can't "
    f"take health details. If you think that's wrong, email {safety_service.FORMA_EMAIL}."
)


class HealthConsentBody(BaseModel):
    # The exact wording of box 2 as the rider ticked it, recorded word for word.
    text_shown: str = Field(min_length=1, max_length=4000)


@router.post("/health-consent")
def give_health_consent(
    body: HealthConsentBody,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Box 2, for an account that never ticked it (the beta riders joined
    before it was asked). Health details are special-category data under UK
    GDPR: the app asks for this before the health questions, and the
    screening refuses without it. Recorded against the privacy policy as
    published now."""
    if not body.text_shown.strip():
        raise BadRequestException(detail="Refresh the page and tick the box again.")
    if safety_service.minor_hold(db, current_user) is not None:
        # Forma takes no health details from an account held as under 18,
        # as the screening refuses its answers (onboarding_service).
        raise ForbiddenException(detail=MINOR_HEALTH_CONSENT_REFUSAL)
    privacy_doc = current_legal_doc(settings.privacy_doc)
    current_user.health_consent_at = datetime.utcnow()
    safety_service.record_consent(
        db, current_user, "health_data", privacy_doc.doc_version, body.text_shown,
        request, source="app", commit=False,
    )
    db.commit()
    return {"ok": True, "privacy_version": privacy_doc.version}


class EmailTokenBody(BaseModel):
    token: str


class ForgotPasswordBody(BaseModel):
    email: NormalisedEmail


class ResetPasswordBody(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


@router.post("/verify-email")
def verify_email(body: EmailTokenBody, db: Session = Depends(get_db)):
    """Flip the flag on a valid verification link. Idempotent."""
    user_id = verify_email_token(body.token, "verify")
    if not user_id:
        raise BadRequestException(
            detail="That link has expired. Log in and press Resend the link at the top of the page for a fresh one."
        )
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise BadRequestException(detail="That link belongs to an account that no longer exists.")
    if not user.email_verified:
        user.email_verified = True
        db.commit()
    return {"status": "verified"}


@router.post("/resend-verification", dependencies=[Depends(_email_limit)])
async def resend_verification(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    if current_user.email_verified:
        return {"status": "already_verified"}
    background_tasks.add_task(
        _send_verification_email,
        str(current_user.id), current_user.email, current_user.full_name,
    )
    return {"status": "sent"}


@router.post("/forgot-password", dependencies=[Depends(_email_limit)])
async def forgot_password(
    body: ForgotPasswordBody,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Always answers the same way, whether or not the account exists, so
    the endpoint can't be used to probe for registered emails. Limited per
    address and per email: the second stops anyone filling one inbox."""
    _reset_email_limit.check(body.email)
    user = db.query(User).filter(User.email == body.email).first()
    if user and user.is_active and user.deleted_at is None:
        token = create_email_token(str(user.id), "reset", hours=1)
        link = f"{_frontend()}/reset-password?token={token}"
        background_tasks.add_task(
            email_service.send_password_reset, user.email, user.full_name, link
        )
    return {"status": "sent"}


@router.post("/reset-password")
def reset_password(body: ResetPasswordBody, db: Session = Depends(get_db)):
    """Set a new password from a reset link, then revoke every live session:
    whoever holds old tokens is signed out everywhere."""
    user_id = verify_email_token(body.token, "reset")
    if not user_id:
        raise BadRequestException(
            detail="That link has expired. Reset links last an hour, so request a new one and use it straight away."
        )
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active or user.deleted_at is not None:
        raise BadRequestException(detail="That link belongs to an account that no longer exists.")

    user.hashed_password = hash_password(body.new_password)
    # A password reset also proves the email is theirs.
    user.email_verified = True
    db.commit()
    token_service.revoke_all_for_user(db, str(user.id))
    logger.info("Password reset completed for user %s", user.id)
    return {"status": "reset"}


@router.post("/login", response_model=TokenResponse,
             dependencies=[Depends(_login_limit)])
def login(user_in: UserLogin, db: Session = Depends(get_db)):
    # Before the password is checked, so a locked email costs no bcrypt and
    # a right password doesn't get through once the limit is reached.
    _login_email_limit.check(user_in.email)
    user = db.query(User).filter(User.email == user_in.email).first()
    # Always run a bcrypt verify, against a dummy hash when the email is
    # unknown, so response time doesn't reveal whether an email is registered.
    hashed = user.hashed_password if user else _DUMMY_HASH
    password_ok = verify_password(user_in.password, hashed)
    if not user or not password_ok:
        raise UnauthorizedException(detail="That email and password don't match. Try again.")
    # A suspended or GDPR-deleted account cannot obtain new tokens.
    if not user.is_active or user.deleted_at is not None:
        raise UnauthorizedException(detail="This account has been closed. If that's a surprise, email gareth@ridewithforma.com.")

    access, refresh = token_service.issue_pair(db, user.id, remember_me=user_in.remember_me)
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/refresh", response_model=TokenResponse,
             dependencies=[Depends(_refresh_limit)])
def refresh_token(body: TokenRefresh, db: Session = Depends(get_db)):
    access, refresh = token_service.rotate(db, body.refresh_token)
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/logout", status_code=204)
def logout(body: TokenRefresh, db: Session = Depends(get_db)):
    """Revoke the presented refresh token's session lineage. Idempotent."""
    token_service.logout(db, body.refresh_token)
    return Response(status_code=204)
