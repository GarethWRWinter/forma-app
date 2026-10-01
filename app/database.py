import logging
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

_db_url = settings.database_url
# Name the driver explicitly. SQLAlchemy 2.1 changed what a bare postgresql://
# URL means (psycopg 3 instead of psycopg2), which took every deploy down on
# 1 Oct 2026. The installed driver is psycopg2, so say so.
if _db_url.startswith("postgres://"):
    _db_url = "postgresql://" + _db_url[len("postgres://"):]
if _db_url.startswith("postgresql://"):
    _db_url = "postgresql+psycopg2://" + _db_url[len("postgresql://"):]
logger.info("Database host: %s", _db_url.split("@")[-1] if "@" in _db_url else "unknown")

engine = create_engine(
    _db_url,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=5,
    max_overflow=10,
    connect_args={"connect_timeout": 5},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
