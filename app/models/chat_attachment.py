"""A ride file dropped into the chat, held outside the training record.

The rider hands the coach a file mid-conversation: a race they want read, a
mate's power file, a session from a device that never syncs. Making that a
Ride straight away would push it into the ride list, the PMC and the weekly
compliance numbers before anyone agreed it belongs there. So an attachment is
parsed, summarised and analysed, and nothing else. It becomes a real Ride only
when the rider says so, at which point imported_ride_id records the promotion
and the same file can never be imported twice from here.

raw_file keeps the original bytes so the promotion can run the ordinary import
path rather than a second, divergent one. It lives in the row rather than on
disk because the backend's filesystem is ephemeral and may be replicated, so a
file spooled by one instance would be missing from the next.
"""

from sqlalchemy import ForeignKey, LargeBinary, String
from sqlalchemy import JSON as SA_JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, generate_uuid


class ChatAttachment(TimestampMixin, Base):
    __tablename__ = "chat_attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), index=True, nullable=False
    )
    # Nullable because the client can upload before the session exists: the
    # rider drags a file onto a blank chat, and the session is created with
    # the first message.
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    # The compact ride summary the coach is handed in context.
    summary: Mapped[dict] = mapped_column(SA_JSON, nullable=False)
    # Deep read of the file: power curve, climbs, fade, time in zone. Same
    # shape ride_analysis_service writes, so the coach's language about an
    # attachment and about a saved ride cannot drift apart.
    analysis: Mapped[dict | None] = mapped_column(SA_JSON, nullable=True)

    # Set when the rider agrees to keep it. Also the guard against a second
    # import of the same attachment.
    imported_ride_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Gzipped original upload. Only read by save_as_ride.
    raw_file: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
