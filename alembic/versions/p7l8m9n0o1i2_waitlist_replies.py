"""Waitlist replies: the research the letters were sent to gather.

Revision ID: p7l8m9n0o1i2
Revises: o6k7l8m9n0h1
"""

import sqlalchemy as sa
from alembic import op

revision = "p7l8m9n0o1i2"
down_revision = "o6k7l8m9n0h1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "waitlist_replies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "waitlist_entry_id",
            sa.String(36),
            sa.ForeignKey("waitlist.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.String(20), nullable=False, server_default="email"),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("themes", sa.JSON(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_waitlist_replies_waitlist_entry_id",
        "waitlist_replies",
        ["waitlist_entry_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_waitlist_replies_waitlist_entry_id", table_name="waitlist_replies")
    op.drop_table("waitlist_replies")
