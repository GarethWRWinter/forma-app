"""plan_proposals — the coach's unapplied arguments for changing the plan.

Nothing in a rider's plan changes silently. When the review engine decides the
prescription no longer serves the goal, it writes a row here and stops. The
rider accepts, declines, or opens a conversation about it, and only an accept
ever touches a Workout.

Observation and rationale are stored as prose rather than codes because the
rider is being persuaded by a coach's argument, not notified of a system event.
Keeping the argument next to the edits is what makes an old proposal readable
months later, when the question is "why did my plan change in August".
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, generate_uuid

# What woke the coach up. Plain strings rather than an Enum: new triggers will
# arrive faster than migrations should, and nothing branches on the value.
PROPOSAL_TRIGGERS = (
    "ride_imported",
    "conversation",
    "weekly_review",
    "goal_changed",
    "manual",
)

# pending -> accepted | declined by the rider, or -> superseded by a newer
# proposal for the same plan, so the rider never faces a stale queue.
PROPOSAL_STATUSES = ("pending", "accepted", "declined", "superseded")


class PlanProposal(Base):
    __tablename__ = "plan_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), index=True, nullable=False
    )
    # Soft references, deliberately not foreign keys: a proposal outlives the
    # plan it argued about, and the record of what the coach said should
    # survive a plan being regenerated or a goal being deleted.
    plan_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    goal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    observation: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    # The concrete edits, each carrying its own one-line why: the rider is
    # approving individual sessions, not a mood.
    changes: Mapped[list | None] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user = relationship("User")
