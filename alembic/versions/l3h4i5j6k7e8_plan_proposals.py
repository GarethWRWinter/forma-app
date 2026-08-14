"""Plan proposals: the coach's unapplied arguments for changing a plan

Revision ID: l3h4i5j6k7e8
Revises: k2g3h4i5j6d7
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "l3h4i5j6k7e8"
down_revision = "k2g3h4i5j6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plan_proposals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id"),
            nullable=False, index=True,
        ),
        # Soft references on purpose: a proposal is a record of what the coach
        # said, and it must survive the plan or goal being replaced.
        sa.Column("plan_id", sa.String(36), nullable=True),
        sa.Column("goal_id", sa.String(36), nullable=True),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("observation", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=True),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="pending",
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
    )
    # Every read path asks the same question: what is still waiting on this
    # rider. Index it once here rather than scanning their whole history.
    op.create_index(
        "ix_plan_proposals_user_status",
        "plan_proposals",
        ["user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_plan_proposals_user_status", table_name="plan_proposals")
    op.drop_table("plan_proposals")
