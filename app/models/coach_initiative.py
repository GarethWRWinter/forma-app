"""coach_initiatives — the moments the coach speaks first.

Most riders will never arrive with the right question. They do not know that
their easy rides have not been easy for a month, and they will not think to
tell anyone that the knee they mentioned in March never really settled. So the
coach opens the conversation instead of waiting to be asked.

An initiative is one thought the coach wants to raise, held until the rider
answers it or waves it away. Three things generate them, and they are stored in
one table because the rider experiences them as one thing: their coach noticed
something. The safeguard that makes this a coach rather than a notification
engine lives in the read path, not here: at most ONE initiative is ever pending
across all three kinds, so the rider never faces a queue of Forma's thoughts.

headline, body and question are stored as prose rather than codes because the
rider is being spoken to, not alerted. subject_type and subject_id remember
what a given initiative was ABOUT, which is what lets a dismissal mean "not
this again for a fortnight" instead of just "not now".

Sibling to plan_proposals: that table holds the coach's arguments for changing
the plan, this one holds the coach's questions about the rider. Both wait for
the rider, neither ever acts alone.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, generate_uuid

# open_loop     — something they told the coach that was never followed up
# ride_insight  — something measurable in a recent ride they would never ask about
# weekly_checkin — the two or three things no device can see
INITIATIVE_KINDS = ("open_loop", "ride_insight", "weekly_checkin")

# pending -> opened when the rider takes it into a conversation, or dismissed
# with one tap. There is deliberately no "why" on a dismissal: asking a rider
# to justify closing a card is the moment an app stops feeling like a coach.
INITIATIVE_STATUSES = ("pending", "opened", "dismissed")


class CoachInitiative(Base):
    __tablename__ = "coach_initiatives"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), index=True, nullable=False
    )

    kind: Mapped[str] = mapped_column(String(24), nullable=False)

    # Soft references, deliberately not foreign keys: the record of what the
    # coach raised must outlive the memory or the ride it was raised about.
    subject_type: Mapped[str | None] = mapped_column(String(24), nullable=True)  # memory|ride
    subject_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    headline: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user = relationship("User")

    __table_args__ = (
        # The gate every generator respects: is anything already pending.
        Index("ix_coach_initiatives_user_status", "user_id", "status"),
        # The cooldown lookup: has this rider already waved away this memory.
        Index("ix_coach_initiatives_subject", "user_id", "subject_type", "subject_id"),
    )
