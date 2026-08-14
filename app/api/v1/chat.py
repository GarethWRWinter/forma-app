"""AI Coach chat API endpoints with SSE streaming."""

import inspect

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user, require_paid_access
from app.core.exceptions import BadRequestException, NotFoundException
from app.core.ratelimit import rate_limit
from app.database import get_db
from app.models.user import User
from app.schemas.chat import (
    ChatMessageRequest,
    ChatMessageResponse,
    ChatSessionCreate,
    ChatSessionDetailResponse,
    ChatSessionResponse,
    ChatSessionUpdate,
    TTSRequest,
)
from app.services import attachment_service
from app.services.attachment_service import AttachmentError
from app.services.coach_service import (
    create_session,
    delete_session,
    get_session,
    get_sessions,
    stream_response,
    stream_voice_response,
    update_session,
)

router = APIRouter(prefix="/chat", tags=["chat"])

# Parsing a ride file costs real work, so this is generous for a rider and
# tight for a script. Keyed per client, like the auth limits.
_attachment_limit = rate_limit(30, 3600)


def _attachment_kwargs(stream_fn, attachment_ids: list[str]) -> dict:
    """Hand the ids to the coach only once its stream can take them.

    The coach's side of this lands in a separate change. Inspecting the
    signature lets the two arrive in either order without a 500 in between.
    """
    if not attachment_ids:
        return {}
    try:
        params = inspect.signature(stream_fn).parameters
    except (TypeError, ValueError):
        return {}
    accepts = "attachment_ids" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    return {"attachment_ids": attachment_ids} if accepts else {}


def _verify_attachments(db: Session, user: User, attachment_ids: list[str]) -> None:
    """Every id must be one of this rider's own files, or the send fails."""
    if not attachment_ids:
        return
    found = attachment_service.get_attachments(db, user.id, attachment_ids)
    if len(found) != len(set(attachment_ids)):
        raise NotFoundException(
            detail="One of those files is no longer available. Upload it again."
        )


@router.post("/sessions", response_model=ChatSessionResponse, status_code=201)
def create_chat_session(
    body: ChatSessionCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new chat session with the AI coach."""
    session = create_session(db, current_user.id, body.title)
    return ChatSessionResponse(
        id=session.id,
        title=session.title,
        created_at=session.created_at,
        message_count=0,
    )


@router.get("/sessions", response_model=list[ChatSessionResponse])
def list_chat_sessions(
    include_archived: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List chat sessions — pinned first, archived hidden unless asked for."""
    sessions = get_sessions(db, current_user.id, include_archived=include_archived)
    return [
        ChatSessionResponse(
            id=s.id,
            title=s.title,
            pinned=s.pinned,
            starred=s.starred,
            archived_at=s.archived_at,
            created_at=s.created_at,
            updated_at=s.updated_at,
            message_count=len(s.messages) if s.messages else 0,
        )
        for s in sessions
    ]


@router.patch("/sessions/{session_id}", response_model=ChatSessionResponse)
def update_chat_session(
    session_id: str,
    body: ChatSessionUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Rename, pin, star or archive a chat session."""
    session = get_session(db, session_id, current_user.id)
    if not session:
        raise NotFoundException(detail="Chat session not found")
    session = update_session(
        db,
        session,
        title=body.title,
        pinned=body.pinned,
        starred=body.starred,
        archived=body.archived,
    )
    return ChatSessionResponse(
        id=session.id,
        title=session.title,
        pinned=session.pinned,
        starred=session.starred,
        archived_at=session.archived_at,
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=len(session.messages) if session.messages else 0,
    )


@router.delete("/sessions/{session_id}", status_code=204)
def delete_chat_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a chat session and its messages. Permanent."""
    session = get_session(db, session_id, current_user.id)
    if not session:
        raise NotFoundException(detail="Chat session not found")
    delete_session(db, session)
    return None


@router.get("/sessions/{session_id}", response_model=ChatSessionDetailResponse)
def get_chat_session(
    session_id: str,
    limit: int = Query(60, ge=1, le=200),
    before: str | None = Query(None, description="Message id — return messages older than this"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a chat session with a window of messages (newest page by default).

    The thread can be years long; the client renders the latest `limit`
    messages and pages backwards with `before` as the rider scrolls up.
    """
    session = get_session(db, session_id, current_user.id)
    if not session:
        raise NotFoundException(detail="Chat session not found")

    ordered = sorted(session.messages or [], key=lambda m: m.created_at)
    if before:
        idx = next((i for i, m in enumerate(ordered) if m.id == before), len(ordered))
        ordered = ordered[:idx]
    window = ordered[-limit:]
    has_more = len(ordered) > len(window)

    return ChatSessionDetailResponse(
        id=session.id,
        title=session.title,
        pinned=session.pinned,
        starred=session.starred,
        archived_at=session.archived_at,
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=len(session.messages) if session.messages else 0,
        has_more=has_more,
        messages=[
            ChatMessageResponse(
                id=m.id,
                role=m.role,
                content=m.content,
                tokens_used=m.tokens_used,
                created_at=m.created_at,
            )
            for m in window
        ],
    )


@router.post("/attachments", status_code=201, dependencies=[Depends(_attachment_limit)])
def upload_chat_attachment(
    file: UploadFile = File(...),
    session_id: str | None = Form(None),
    current_user: User = Depends(require_paid_access),
    db: Session = Depends(get_db),
):
    """Hand the coach a ride file inside a conversation.

    The file is read and analysed, not filed. It joins the ride log, and the
    training load, only when the rider asks for it to be saved.
    """
    if session_id:
        session = get_session(db, session_id, current_user.id)
        if not session:
            raise NotFoundException(detail="Chat session not found")

    # Checked before the read where the client declares a size, so an absurd
    # upload never lands in memory at all.
    if file.size is not None and file.size > attachment_service.MAX_ATTACHMENT_BYTES:
        raise BadRequestException(
            detail=(
                f"That file is larger than "
                f"{attachment_service.MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB. "
                "Send a single ride rather than an archive."
            )
        )

    try:
        attachment = attachment_service.ingest(
            db,
            current_user,
            file.file.read(),
            file.filename or "",
            session_id,
        )
    except AttachmentError as exc:
        raise BadRequestException(detail=str(exc)) from exc

    return {
        "id": attachment.id,
        "filename": attachment.filename,
        "kind": attachment.kind,
        "summary": attachment.summary,
    }


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    body: ChatMessageRequest,
    current_user: User = Depends(require_paid_access),
    db: Session = Depends(get_db),
):
    """
    Send a message to the AI coach and receive a streaming response.

    Returns Server-Sent Events (SSE):
        data: {"type": "text", "content": "..."}
        data: {"type": "done"}
    """
    session = get_session(db, session_id, current_user.id)
    if not session:
        raise NotFoundException(detail="Chat session not found")

    _verify_attachments(db, current_user, body.attachment_ids)

    return StreamingResponse(
        stream_response(
            db,
            current_user,
            session,
            body.content,
            **_attachment_kwargs(stream_response, body.attachment_ids),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


@router.post("/tts")
async def text_to_speech_endpoint(
    body: TTSRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Convert text to speech using ElevenLabs.

    Returns streaming audio/mpeg data. Used for replaying
    past messages as audio.
    """
    from app.services.voice_service import is_voice_enabled, text_to_speech_stream

    if not is_voice_enabled():
        from fastapi import HTTPException
        raise HTTPException(
            status_code=503,
            detail="Voice is not configured. Set ELEVENLABS_API_KEY in environment.",
        )

    return StreamingResponse(
        text_to_speech_stream(body.text),
        media_type="audio/mpeg",
        headers={
            "Content-Type": "audio/mpeg",
            "Cache-Control": "no-cache",
        },
    )


@router.post("/sessions/{session_id}/voice-message")
async def send_voice_message(
    session_id: str,
    body: ChatMessageRequest,
    current_user: User = Depends(require_paid_access),
    db: Session = Depends(get_db),
):
    """
    Send a message and receive both text AND audio chunks via SSE.

    Combines Claude AI response with ElevenLabs TTS, streaming
    sentence-by-sentence for low-latency audio playback.

    Returns Server-Sent Events (SSE):
        data: {"type": "text", "content": "..."}
        data: {"type": "audio", "content": "<base64>", "sentence_index": N}
        data: {"type": "done"}
    """
    session = get_session(db, session_id, current_user.id)
    if not session:
        raise NotFoundException(detail="Chat session not found")

    _verify_attachments(db, current_user, body.attachment_ids)

    return StreamingResponse(
        stream_voice_response(
            db,
            current_user,
            session,
            body.content,
            **_attachment_kwargs(stream_voice_response, body.attachment_ids),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
