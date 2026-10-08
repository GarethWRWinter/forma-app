import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.models.base import Base


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):
    # Production is Postgres; the memory graph uses JSONB. SQLite has no such
    # type, and without this every fixture that creates the schema dies before
    # the first assertion.
    return "JSON"


@pytest.fixture
def db_session():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _no_real_model_provider(monkeypatch):
    """No test reaches the real model provider, whatever key is in the
    environment: the one provider client (forma_core._client) refuses, so an
    unfaked call fails as if the provider were down. The chat's safety
    classifier is off unless a test brings it in by setting
    coach_service._load_classifier itself, so it never takes a scripted
    reply meant for the coach."""
    from app.core import forma_core
    from app.services import coach_service

    def refuse():
        raise RuntimeError("a test tried to reach the real model provider")

    monkeypatch.setattr(forma_core, "_client", refuse)
    monkeypatch.setattr(coach_service, "_load_classifier", lambda: None)
