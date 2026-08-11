"""Invite codes for the closed beta, plus which code each user entered by.

Revision ID: z1v2w3x4y5s6
Revises: y0u1v2w3x4r5
"""

import sqlalchemy as sa
from alembic import op

revision = "z1v2w3x4y5s6"
down_revision = "y0u1v2w3x4r5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invite_codes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("code", sa.String(24), nullable=False, unique=True),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("uses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_invite_codes_code", "invite_codes", ["code"])
    op.add_column("users", sa.Column("invited_with", sa.String(24), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "invited_with")
    op.drop_index("ix_invite_codes_code", table_name="invite_codes")
    op.drop_table("invite_codes")
