"""Waitlist table: signals before sales.

Revision ID: c4y5z6a7b8v9
Revises: b3x4y5z6a7u8
"""

import sqlalchemy as sa
from alembic import op

revision = "c4y5z6a7b8v9"
down_revision = "b3x4y5z6a7u8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "waitlist",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("letter0_sent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_waitlist_email", "waitlist", ["email"])


def downgrade() -> None:
    op.drop_index("ix_waitlist_email", table_name="waitlist")
    op.drop_table("waitlist")
