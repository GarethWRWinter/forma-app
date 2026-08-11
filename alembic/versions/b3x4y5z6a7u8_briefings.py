"""Pre-ride briefings table.

Revision ID: b3x4y5z6a7u8
Revises: a2w3x4y5z6t7
"""

import sqlalchemy as sa
from alembic import op

revision = "b3x4y5z6a7u8"
down_revision = "a2w3x4y5z6t7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "briefings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "date", "kind", name="uq_briefing_user_date_kind"),
    )
    op.create_index("ix_briefings_user_id", "briefings", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_briefings_user_id", table_name="briefings")
    op.drop_table("briefings")
