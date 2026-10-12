"""Pydantic schemas for AI coach chat."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ChatMessageRequest(BaseModel):
    """Send a message to the AI coach."""
    content: str = Field(..., min_length=1, max_length=4000)
    # Ride files uploaded to /chat/attachments and being handed to the coach
    # with this message. Capped because each one carries a read of the file
    # into context.
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)


class TTSRequest(BaseModel):
    """Text to convert to speech via ElevenLabs."""
    text: str = Field(..., min_length=1, max_length=5000)


class ChatSafetyCard(BaseModel):
    """A fixed red-flag card shown at the top of a reply, as the stream sent
    it: its style ("emergency", "crisis" or "warning"), its words, and which
    card it is (chest, crisis, fever...)."""
    kind: str
    text: str
    card: str | None = None


class ChatMessageResponse(BaseModel):
    """A single chat message."""
    id: str
    role: str
    content: str
    tokens_used: int | None = None
    created_at: datetime | None = None
    # The cards shown above this reply when it was sent, so a reload shows
    # them again (coach_service._cards_snapshot). None on replies without one.
    safety_card: list[ChatSafetyCard] | None = None

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
