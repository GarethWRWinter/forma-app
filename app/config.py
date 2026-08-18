from pydantic_settings import BaseSettings


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
    email_from: str = "Forma <coach@ridewithforma.com>"
    # The letters are a person writing to a rider, not the system talking, and
    # they ask for a reply. Sending them from the shared transactional address
    # would put those replies in the wrong inbox and make a personal letter
    # arrive from a role account.
    email_from_founder: str = "Gareth at Forma <gareth@ridewithforma.com>"

    # Stripe subscriptions. Dormant until the keys exist; the paywall itself
    # only bites when require_subscription flips true (launch day switch).
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id: str = ""  # the active subscription price (founding offer)
    require_subscription: bool = False

    # Closed beta: when true, registration needs a valid invite code.
    require_invite: bool = False

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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
