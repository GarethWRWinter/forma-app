"""Coach initiatives: the moments the coach speaks first

Revision ID: m4i5j6k7l8f9
Revises: l3h4i5j6k7e8
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "m4i5j6k7l8f9"
down_revision = "l3h4i5j6k7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coach_initiatives",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id"),
            nullable=False, index=True,
        ),
        sa.Column("kind", sa.String(24), nullable=False),
        # Soft references on purpose: what the coach raised must outlive the
        # memory or the ride it was raised about.
        sa.Column("subject_type", sa.String(24), nullable=True),
        sa.Column("subject_id", sa.String(36), nullable=True),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="pending",
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
    )
    # Asked before every generation runs: is anything already waiting on this
    # rider. It must never cost a scan of their whole history.
    op.create_index(
        "ix_coach_initiatives_user_status",
        "coach_initiatives",
        ["user_id", "status"],
    )
    # The cooldown lookup: has this rider already waved away this exact memory.
    op.create_index(
        "ix_coach_initiatives_subject",
        "coach_initiatives",
        ["user_id", "subject_type", "subject_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_coach_initiatives_subject", table_name="coach_initiatives")
    op.drop_index("ix_coach_initiatives_user_status", table_name="coach_initiatives")
    op.drop_table("coach_initiatives")
