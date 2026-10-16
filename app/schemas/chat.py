"""Pydantic schemas for AI coach chat."""

from datetime import datetime
from typing import Literal

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


class ChatMessageResponse(BaseModel):
    """A single chat message."""
    id: str
    role: str
    content: str
    tokens_used: int | None = None
    created_at: datetime | None = None
    # The ride files a rider message carried, so each one's card sits under
    # the message that shared it after a reload too (see the session's
    # attachments).
    attachment_ids: list[str] = []

    model_config = ConfigDict(from_attributes=True)


FileChoice = Literal["save", "chat_only", "someone_else"]


class AttachmentCardRide(BaseModel):
    """The saved ride a file card links to."""
    id: str
    title: str | None = None
    date: str | None = None  # YYYY-MM-DD


class AttachmentCardChoice(BaseModel):
    """One button, or one quiet way to change a choice, on a file card."""
    choice: FileChoice
    label: str
    detail: str | None = None
    # Someone else's takes an optional short name, at most 40 characters.
    asks_label: bool = False


class ChatAttachmentCard(BaseModel):
    """The card under the message that shared a ride file: what Forma
    recognised, and the filing choice. Every line is ready to show."""
    id: str
    # Shared by every upload of the same file, so the app shows one card
    # for both and knows which cards a choice reaches.
    file_key: str | None = None
    filename: str
    kind: str
    date: str | None = None  # the ride's start date, YYYY-MM-DD
    start_time: str | None = None
    duration: str | None = None
    duration_s: int | None = None
    distance_km: float | None = None
    # What Forma recognised: in_history, part_of_ride or new.
    state: Literal["in_history", "part_of_ride", "new"]
    # What the card shows: asking, in_history, part_of_ride, saved,
    # chat_only or someone_else.
    view: Literal["asking", "in_history", "part_of_ride", "saved", "chat_only", "someone_else"]
    recognised: str
    question: str | None = None
    # After a choice: shown in place of the recognised line.
    outcome: str | None = None
    note: str | None = None
    undo: str | None = None
    ride: AttachmentCardRide | None = None
    overlap: float | None = None
    hints: list[str] = []
    decided: bool = False
    decision: FileChoice | None = None
    label: str | None = None
    decided_at: str | None = None
    choices: list[AttachmentCardChoice] = []
    changes: list[AttachmentCardChoice] = []


class AttachmentDecisionRequest(BaseModel):
    """What the rider chose on a file card."""
    choice: FileChoice
    # Someone else's only. The service holds it to 40 characters once
    # spaces are tidied; this bound only stops an absurd body.
    label: str | None = Field(None, max_length=200)


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
    # When the latest message landed (or when the chat was opened, while it
    # is empty). The list is ordered by it.
    last_message_at: datetime | None = None
    message_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class ChatSessionDetailResponse(ChatSessionResponse):
    """Chat session with a window of messages."""
    messages: list[ChatMessageResponse] = []
    has_more: bool = False
    # A card for each file the window's messages carried, in the order they
    # were first shared.
    attachments: list[ChatAttachmentCard] = []
