"""Pydantic schemas for AI coach chat."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ChatMessageRequest(BaseModel):
    """Send a message to the AI coach."""
    content: str = Field(..., min_length=1, max_length=4000)


class TTSRequest(BaseModel):
    """Text to convert to speech via ElevenLabs."""
    text: str = Field(..., min_length=1, max_length=5000)


class ChatMessageResponse(BaseModel):
    """A single chat message."""
    id: str
    role: str
    content: str
    tokens_used: int | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ChatSessionCreate(BaseModel):
    """Create a new chat session."""
    title: str | None = None


class ChatSessionUpdate(BaseModel):
    """Rename / pin / star / archive a chat session. All fields optional."""
    title: str | None = Field(None, min_length=1, max_length=255)
    pinned: bool | None = None
    starred: bool | None = None
    archived: bool | None = None


class ChatSessionResponse(BaseModel):
    """Chat session summary."""
    id: str
    title: str | None = None
    pinned: bool = False
    starred: bool = False
    archived_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    message_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class ChatSessionDetailResponse(ChatSessionResponse):
    """Chat session with a window of messages."""
    messages: list[ChatMessageResponse] = []
    has_more: bool = False
