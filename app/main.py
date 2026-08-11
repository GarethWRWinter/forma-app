import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.config import settings
from app.services.auto_sync import (
    start_auto_sync,
    stop_auto_sync,
    start_strava_auto_sync,
    stop_strava_auto_sync,
)
from app.services.strava_service import resume_incomplete_backfills

logger = logging.getLogger(__name__)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    # --- Startup ---
    # A placeholder signing key means every user's JWT is forgeable.
    if "change-me" in settings.secret_key:
        logger.critical(
            "SECRET_KEY is the default placeholder — set a real SECRET_KEY "
            "env var. All auth tokens are forgeable until this is fixed."
        )
    # Without an encryption key, integration tokens sit in the DB as plaintext.
    if not settings.token_encryption_key:
        logger.warning(
            "TOKEN_ENCRYPTION_KEY is not set — Strava/Dropbox tokens are stored "
            "unencrypted. Set it and run scripts/encrypt_integration_tokens.py."
        )

    sync_interval = settings.dropbox_sync_interval
    if sync_interval > 0:
        start_auto_sync(interval=sync_interval)
        logger.info("Dropbox auto-sync enabled (every %ds)", sync_interval)
    else:
        logger.info("Dropbox auto-sync disabled (interval=0)")

    strava_sync_interval = settings.strava_sync_interval
    if strava_sync_interval > 0:
        start_strava_auto_sync(interval=strava_sync_interval)
        logger.info("Strava auto-sync enabled (every %ds)", strava_sync_interval)
    else:
        logger.info("Strava auto-sync disabled (interval=0)")

    # Resume any Strava backfills that were interrupted by a previous restart.
    # Runs in background so we don't block startup.
    asyncio.create_task(resume_incomplete_backfills())
    logger.info("Scheduled Strava backfill resume check")

    # Warm the embedding model + embed any memories that predate the RAG
    # layer. Background thread: model load is CPU/disk work.
    async def _warm_embeddings() -> None:
        def _run() -> None:
            from app.database import SessionLocal
            from app.services.memory_service import embed_missing_entities

            db = SessionLocal()
            try:
                embed_missing_entities(db)
            finally:
                db.close()

        try:
            await asyncio.to_thread(_run)
        except Exception:
            logger.exception("Embedding warm-up failed (semantic retrieval degrades gracefully)")

    asyncio.create_task(_warm_embeddings())
    logger.info("Scheduled embedding warm-up + backfill")

    yield
    # --- Shutdown ---
    stop_auto_sync()
    stop_strava_auto_sync()
    logger.info("App shutdown complete")


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="AI-powered cycling coach with training plans, ride analytics, and conversational coaching",
    lifespan=lifespan,
)

origins = list(settings.cors_origins)
if settings.frontend_url:
    origins.append(settings.frontend_url)
# The landing page posts the waitlist form from the marketing domain.
origins += [
    "https://www.ridewithforma.com",
    "https://ridewithforma.com",
    "https://app.ridewithforma.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request, call_next):
    """Baseline security headers on every response. HSTS is honoured only over
    HTTPS (Railway terminates TLS), so it's a no-op locally."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    return response


app.include_router(api_router, prefix="/api/v1")


@app.get("/health")
def health_check():
    """Health check — always returns 200 so Railway deploys succeed.
    DB status is informational only."""
    result = {"status": "healthy"}
    try:
        from sqlalchemy import text
        from app.database import engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        result["database"] = "connected"
    except Exception as e:
        result["database"] = f"unavailable: {e}"
    return result
