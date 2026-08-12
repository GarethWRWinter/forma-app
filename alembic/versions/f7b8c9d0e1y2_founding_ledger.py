"""Founding ledger: issued numbers reserved forever, seeded from users.

Revision ID: f7b8c9d0e1y2
Revises: e6a7b8c9d0x1
"""

import sqlalchemy as sa
from alembic import op

revision = "f7b8c9d0e1y2"
down_revision = "e6a7b8c9d0x1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "founding_ledger",
        sa.Column("number", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
    )
    # Numbers already worn move onto the ledger so they stay reserved.
    op.execute(
        "INSERT INTO founding_ledger (number, user_id, issued_at) "
        "SELECT founding_number, id, CURRENT_TIMESTAMP FROM users "
        "WHERE founding_number IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_table("founding_ledger")
