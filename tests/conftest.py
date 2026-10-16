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
    unfaked call fails as if the provider were down."""
    from app.core import forma_core

    def refuse():
        raise RuntimeError("a test tried to reach the real model provider")

    monkeypatch.setattr(forma_core, "_client", refuse)


@pytest.fixture(autouse=True)
def _no_real_voice_provider(monkeypatch):
    """No test reaches ElevenLabs either, whatever key the environment holds
    (settings read .env): voice is off unless a test turns it on, and the two
    calls that would reach the provider refuse."""
    from app.services import voice_service

    def refuse(*args, **kwargs):
        raise RuntimeError("a test tried to reach the real voice provider")

    monkeypatch.setattr(voice_service, "is_voice_enabled", lambda: False)
    monkeypatch.setattr(voice_service, "text_to_speech", refuse)
    monkeypatch.setattr(voice_service, "text_to_speech_stream", refuse)


# ── A scripted model for the coach's chat loop ──────────────────────────────


class _Delta:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class _Event:
    def __init__(self, type_, **delta):
        self.type = type_
        if delta:
            self.delta = _Delta(**delta)


class _Usage:
    input_tokens = 10
    output_tokens = 5


class _Text:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Thinking:
    """A thinking block with its text left out, as the API sends it by default."""

    type = "thinking"
    thinking = ""


class _ToolUse:
    type = "tool_use"
    _count = 0

    def __init__(self, name, tool_input=None):
        _ToolUse._count += 1
        self.name = name
        self.input = tool_input or {}
        self.id = f"tu_{name}_{_ToolUse._count}"


class _Final:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _FakeStream:
    def __init__(self, texts, blocks, stop_reason):
        self._texts = list(texts)
        thinking = [b for b in blocks if b.type == "thinking"]
        tools = [b for b in blocks if b.type == "tool_use"]
        text = [_Text("".join(self._texts))] if self._texts else []
        self._final = _Final(
            thinking + text + tools,
            stop_reason or ("tool_use" if tools else "end_turn"),
        )
        self._thinking = bool(thinking)

    def __iter__(self):
        yield _Event("message_start")
        if self._thinking:
            yield _Event("content_block_delta", thinking="")
        for t in self._texts:
            yield _Event("content_block_delta", text=t)

    def get_final_message(self):
        return self._final


class ScriptedModel(list):
    """The coach's model, replaced by a script: one entry per round, either
    (texts, blocks) or (texts, blocks, stop_reason). texts stream as words;
    blocks are the round's other content (scripted_model.tool_use(...) and
    scripted_model.thinking()). An empty script answers "Done.".

    calls records what the coach sent on every round, tool_choice included.
    A round sent tool_choice {"type": "none"} never returns a tool call,
    as the provider never would."""

    tool_use = _ToolUse
    thinking = _Thinking

    def __init__(self):
        super().__init__()
        self.calls: list[dict] = []


@pytest.fixture
def scripted_model(monkeypatch):
    """The coach's model, replaced by a script (see ScriptedModel). Ported
    from the safety-system branch (tests/test_reply_guard.py), extended to
    record each call and to honour tool_choice."""
    from contextlib import contextmanager

    from app.core import forma_core
    from app.services import coach_service

    script = ScriptedModel()

    @contextmanager
    def fake_stream(**kwargs):
        # The coach appends to one messages list as rounds go by: keep each
        # round's view as it was sent.
        script.calls.append({**kwargs, "messages": list(kwargs.get("messages") or [])})
        entry = script.pop(0) if script else (["Done."], ())
        texts, blocks, stop_reason = (tuple(entry) + (None,))[:3]
        if kwargs.get("tool_choice") == {"type": "none"}:
            blocks = [b for b in blocks if b.type != "tool_use"]
            stop_reason = stop_reason if stop_reason not in (None, "tool_use") else "end_turn"
        yield _FakeStream(texts, list(blocks), stop_reason)

    def offline(*args, **kwargs):
        raise RuntimeError("no model in tests")

    monkeypatch.setattr(forma_core, "stream", fake_stream)
    monkeypatch.setattr(forma_core, "call", offline)
    monkeypatch.setattr(coach_service, "maybe_autotitle_session", lambda *a, **k: None)
    monkeypatch.setattr(coach_service, "_relevant_memories", lambda *a, **k: None)
    monkeypatch.setattr(coach_service, "_dossier_block", lambda *a, **k: "")
    monkeypatch.setattr("app.services.memory_service.extract_memories", lambda *a, **k: None)
    monkeypatch.setattr("app.services.voice_service.is_voice_enabled", lambda: False)
    return script
