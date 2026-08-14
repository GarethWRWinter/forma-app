"""chat_attachments: ride files the rider hands the coach mid-conversation.

Revision ID: j1f2g3h4i5c6
Revises: i0e1f2g3h4b5
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "j1f2g3h4i5c6"
down_revision = "i0e1f2g3h4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_attachments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("analysis", sa.JSON(), nullable=True),
        sa.Column("imported_ride_id", sa.String(36), nullable=True),
        sa.Column("raw_file", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_chat_attachments_user_id", "chat_attachments", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_attachments_user_id", table_name="chat_attachments")
    op.drop_table("chat_attachments")
