"""Wahoo: record why a connection needs reauth, because the fix differs.

Revision ID: q8m9n0o1p2j3
Revises: p7l8m9n0o1i2
"""

import sqlalchemy as sa
from alembic import op

revision = "q8m9n0o1p2j3"
down_revision = "p7l8m9n0o1i2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wahoo_tokens", sa.Column("reauth_reason", sa.String(40), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("wahoo_tokens", "reauth_reason")
