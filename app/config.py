import hashlib
import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, NamedTuple

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode

logger = logging.getLogger(__name__)

# === Who can join (terms section 3) ===

# The terms say the United Kingdom, and only the United Kingdom, until the
# insurer confirms EU cover in writing. Adding a country is one environment
# variable (ALLOWED_COUNTRIES=GB,IE,FR), set on the same day the published
# terms say so, never before.
DEFAULT_ALLOWED_COUNTRIES = ("GB",)

# The 27 EU member states, by ISO 3166 alpha-2 code (Greece is GR here, not
# the EU's own EL). Not allowed by default: kept so the day EU cover is
# confirmed, ALLOWED_COUNTRIES can be set from a list someone has checked.
EU_MEMBER_STATES = (
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
    "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
)

# What people type for a code that ISO 3166 spells differently.
COUNTRY_ALIASES = {"UK": "GB", "EL": "GR"}
_COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")


# === The published legal documents ===

# Every version a rider can agree to lives here, one file per version, and
# is never edited once riders have accepted it: a change is a new version.
LEGAL_PUBLISHED_DIR = Path(__file__).resolve().parent.parent / "prd" / "legal" / "published"
_LEGAL_VERSION = re.compile(r"^(terms|privacy)-[a-z0-9][a-z0-9-]*$")
# consent_events.doc_version is String(40). A longer stamp would be cut and
# lose the hash that says which text was agreed to, so it is refused instead.
_DOC_VERSION_MAX = 40


# Stands in for the text when the published file can't be read, so the
# sign-up email still says something true where the terms should be.
MISSING_LEGAL_TEXT = (
    "The full text couldn't be attached to this email. Reply to it and I'll "
    "send you a copy of the version you accepted."
)


class LegalDoc(NamedTuple):
    version: str
    text: str
    sha256: str
    # True when the published file couldn't be read (see published_legal_doc).
    missing: bool = False

    @property
    def doc_version(self) -> str:
        """What every consent row records: the version and the first 12 hex
        characters of the published file's SHA-256, or "#missing" when the
        file couldn't be read, so the record never claims a hash it doesn't
        have."""
        if self.missing:
            return f"{self.version}#missing"
        return f"{self.version}#{self.sha256[:12]}"


@lru_cache(maxsize=None)
def published_legal_doc(version: str) -> LegalDoc:
    """The published file for a terms or privacy version, read and hashed
    once.

    A version that isn't a plain name is refused outright: it could point
    outside the published folder. A well-formed version whose file is
    missing (the folder was never committed, a typo in TERMS_VERSION) logs
    an error and comes back marked missing, stamped "<version>#missing", so
    the API keeps serving instead of crash-looping on Railway (review new
    problem 10). Every consent row recorded against it says so plainly."""
    if not _LEGAL_VERSION.match(version or ""):
        raise RuntimeError(f"Not a legal document version: {version!r}")
    if len(LegalDoc(version, "", "0" * 12).doc_version) > _DOC_VERSION_MAX:
        raise RuntimeError(f"{version} is too long to record with its hash; shorten it.")
    path = LEGAL_PUBLISHED_DIR / f"{version}.md"
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.error(
            "LEGAL DOCUMENT MISSING: %s can't be read from %s (%s). Consent rows "
            "will record %s#missing until the file is committed and deployed.",
            version, path, exc.__class__.__name__, version,
        )
        return LegalDoc(version, MISSING_LEGAL_TEXT, "", missing=True)
    return LegalDoc(version, text, hashlib.sha256(raw).hexdigest())


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql://coaching:coaching@localhost:5432/coaching_db"

    # Auth
    secret_key: str = "change-me-to-a-random-secret-key"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    remember_me_expire_days: int = 30
    algorithm: str = "HS256"

    # Encryption for integration tokens at rest (Fernet key, or comma-separated
    # list for rotation). Deliberately separate from secret_key so rotating the
    # JWT key never risks stored Strava/Dropbox tokens. Empty = encryption off
    # (loud startup warning; reads still tolerate plaintext).
    token_encryption_key: str = ""

    # Anthropic
    anthropic_api_key: str = ""

    # ElevenLabs (Voice)
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "Fahco4VZzobUeiPqni1S"  # Gareth's pick for the male coach (11 Aug 2026)
    elevenlabs_model_id: str = "eleven_turbo_v2_5"  # Low latency (~300ms)
    # Riders don't have amazing patience: a touch quicker than natural.
    # ElevenLabs caps speed at 1.2 (floor 0.7).
    elevenlabs_voice_speed: float = 1.15

    # Strava
    strava_client_id: str = ""
    strava_client_secret: str = ""
    strava_redirect_uri: str = "http://localhost:8000/api/v1/integrations/strava/callback"
    strava_webhook_verify_token: str = "coaching-strava-webhook-verify"

    # Dropbox
    dropbox_client_id: str = ""
    dropbox_client_secret: str = ""
    dropbox_redirect_uri: str = "http://localhost:8000/api/v1/integrations/dropbox/callback"

    # Transactional email (Postmark). Without the token, emails are logged
    # instead of sent, so flows stay testable before the account exists.
    postmark_server_token: str = ""
    # One sender for everything until launch. Deliberately not split into a
    # transactional address and a founder address: a solo founder reading every
    # reply needs one inbox, not two to keep track of. The letters ask people to
    # hit reply, so that inbox has to be a person's.
    email_from: str = "Gareth at Forma <gareth@ridewithforma.com>"

    # New joiners get Letter 0 automatically: a signup at 11pm should not wait
    # until someone is awake. The riders already on the list when this was
    # switched on are a separate case. They are written to personally, by hand,
    # and recorded with scripts/mark_letters_sent.py, which is why nothing here
    # ever sends to an existing row. Only a brand new join triggers a send.
    waitlist_autosend: bool = True

    # Error reporting. Empty means off, so local dev and tests never phone
    # home; setting SENTRY_DSN on Railway is the whole rollout.
    sentry_dsn: str = ""

    # Stripe subscriptions. Dormant until the keys exist; the paywall itself
    # only bites when require_subscription flips true (launch day switch).
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id: str = ""  # the active subscription price (founding offer)
    require_subscription: bool = False

    # Closed beta: when true, registration needs a valid invite code.
    # Default CLOSED. On 2 Sep 2026 a waitlist rider found the sign-up page
    # while this was False and the Railway variable had never been set. The
    # door now stays shut unless someone deliberately opens it.
    require_invite: bool = True

    # The coach writing first. Hourly check; emails at 1, 3 and 7 quiet days
    # per activation stage, each once. Set OUTREACH_ENABLED=false to silence.
    outreach_enabled: bool = True
    outreach_interval: int = 3600

    # OpenWeatherMap One Call 3.0 (ride conditions + briefing forecasts).
    openweather_api_key: str = ""

    # Wahoo Cloud API (developers.wahooligan.com). Integration stays dormant
    # until these are set.
    wahoo_client_id: str = ""
    wahoo_client_secret: str = ""
    wahoo_redirect_uri: str = "http://localhost:8000/api/v1/integrations/wahoo/callback"
    wahoo_webhook_token: str = ""

    # Dropbox auto-sync interval in seconds (0 = disabled, default 15 min)
    dropbox_sync_interval: int = 900

    # Strava auto-sync interval in seconds (0 = disabled, default 5 min).
    # Belt-and-braces against webhook failures and frontend-only sync.
    # Polls every connected Strava user on this cadence.
    strava_sync_interval: int = 300

    # App
    app_name: str = "Advanced Cycling Coach"
    cors_origins: list[str] = ["http://localhost:3000"]
    # Emails allowed to read the /admin/costs dashboard (empty = nobody)
    admin_emails: list[str] = []

    # Per-user monthly Forma spend cap, in US cents. Default $8.00 — the PRD's
    # hard alerting threshold, well above the ~$1.87 expected spend, so only
    # genuine runaway/abuse hits it. Soft-cap warns the rider at 80%.
    monthly_budget_cents: int = 800
    frontend_url: str = ""  # Set to Vercel URL in production

    # Safety and consent. Each version names a file in prd/legal/published/
    # (terms-<version>.md, privacy-<version>.md), hashed at startup; every
    # consent row records "<version>#<first 12 hex of its SHA-256>". When
    # terms_version changes, riders on an older one see the re-acceptance
    # modal. Both stay on the drafts until Gareth approves the final text.
    terms_version: str = "terms-2026-10-draft"
    privacy_version: str = "privacy-2026-10-draft"
    # Where riders can join from (terms section 3), ISO 3166 alpha-2. The UK
    # only, matching the terms, until an insurer confirms EU cover. Set as
    # ALLOWED_COUNTRIES=GB,IE (comma separated). Anything else is refused at
    # sign-up and on profile edits, and GET /auth/config hands the list to
    # the sign-up page.
    allowed_countries: Annotated[list[str], NoDecode] = list(DEFAULT_ALLOWED_COUNTRIES)
    # Crisis and minor red flags email this address (safeguarding protocol).
    founder_alert_email: str = "gareth@ridewithforma.com"
    # The build each consent row was given under. Railway sets
    # RAILWAY_GIT_COMMIT_SHA on every deploy, so this is rarely set by hand.
    app_build: str = ""

    # Shared with the Next.js frontend (frontend/src/middleware.ts), which stamps
    # it on every /api/* request it forwards as the x-forma-edge header. A
    # request carrying it came through Vercel, so its first X-Forwarded-For
    # hop is the rider (app.core.ratelimit.client_ip). Read from
    # FORMA_EDGE_SECRET, the same name on Railway and Vercel. Empty keeps the
    # older x-vercel-id rule, so nothing changes until it is set. Set it on
    # Vercel and redeploy the frontend first, then on Railway: the other
    # order puts every rider in Vercel's one bucket until the frontend
    # catches up.
    edge_secret: str = Field("", validation_alias="FORMA_EDGE_SECRET")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @field_validator("edge_secret", mode="before")
    @classmethod
    def _edge_secret_trimmed(cls, value):
        """A pasted secret often carries a trailing newline; Vercel's copy
        won't, and the two would never match."""
        secret = str(value or "").strip()
        if secret and len(secret) < 32:
            logger.warning(
                "FORMA_EDGE_SECRET is only %d characters; use 32 or more random ones.",
                len(secret),
            )
        return secret

    @field_validator("allowed_countries", mode="before")
    @classmethod
    def _countries_from_env(cls, value):
        """ALLOWED_COUNTRIES=GB,IE (or a JSON list), normalised to upper-case
        ISO codes. A bad entry is logged and dropped, not fatal: a typo in
        one variable mustn't stop the API. An empty result falls back to the
        UK, never to everywhere."""
        if value is None:
            raw: list = []
        elif isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                try:
                    raw = list(json.loads(text))
                except ValueError:
                    raw = text.strip("[]").split(",")
            else:
                raw = text.split(",")
        else:
            raw = list(value)
        codes: list[str] = []
        for item in raw:
            code = str(item).strip().strip("\"'").upper()
            code = COUNTRY_ALIASES.get(code, code)
            if not code:
                continue
            if not _COUNTRY_CODE.match(code):
                logger.error("ALLOWED_COUNTRIES: %r isn't a two-letter country code; ignored.", item)
                continue
            if code not in codes:
                codes.append(code)
        if not codes:
            logger.error(
                "ALLOWED_COUNTRIES is empty or unreadable (%r); using %s.",
                value, ",".join(DEFAULT_ALLOWED_COUNTRIES),
            )
            codes = list(DEFAULT_ALLOWED_COUNTRIES)
        return codes

    @model_validator(mode="after")
    def _app_build_from_railway(self) -> "Settings":
        if not self.app_build:
            self.app_build = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")
        self.app_build = self.app_build[:40]
        return self

    @model_validator(mode="after")
    def _legal_docs_are_published(self) -> "Settings":
        # Read and hash both documents now, so a missing one is logged at
        # startup rather than first noticed at someone's sign-up. A missing
        # file no longer stops the app (see published_legal_doc); a version
        # that isn't a plain name still does, since it's a config error that
        # could point outside the published folder.
        self.terms_doc()
        self.privacy_doc()
        return self

    def missing_legal_docs(self) -> list[str]:
        """The versions whose published file couldn't be read. The startup
        checks log these, and they should be empty on every deploy."""
        return [doc.version for doc in (self.terms_doc(), self.privacy_doc()) if doc.missing]

    def terms_doc(self) -> LegalDoc:
        return published_legal_doc(self.terms_version)

    def privacy_doc(self) -> LegalDoc:
        return published_legal_doc(self.privacy_version)

    @property
    def safety_law_version(self) -> str:
        """The SAFETY_LAW version, read from the text itself in coach_skills.
        A property, not a field, so no environment variable can make the
        record say a different version from the one the coach was sent."""
        from app.core.coach_skills import SAFETY_LAW_VERSION

        return SAFETY_LAW_VERSION


settings = Settings()
