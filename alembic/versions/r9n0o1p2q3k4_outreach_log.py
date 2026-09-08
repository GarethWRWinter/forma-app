"""Outreach log: the coach's emails to riders who went quiet.

Revision ID: r9n0o1p2q3k4
Revises: q8m9n0o1p2j3
"""

import sqlalchemy as sa
from alembic import op

revision = "r9n0o1p2q3k4"
down_revision = "q8m9n0o1p2j3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outreach_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("threshold_days", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_outreach_log_user_id", "outreach_log", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_outreach_log_user_id", table_name="outreach_log")
    op.drop_table("outreach_log")
