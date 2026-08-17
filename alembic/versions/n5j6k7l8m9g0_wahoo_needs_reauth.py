"""Wahoo needs_reauth: a dead refresh token becomes visible, not silent.

Revision ID: n5j6k7l8m9g0
Revises: m4i5j6k7l8f9
"""

import sqlalchemy as sa
from alembic import op

revision = "n5j6k7l8m9g0"
down_revision = "m4i5j6k7l8f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wahoo_tokens",
        sa.Column(
            "needs_reauth", sa.Boolean(), nullable=False, server_default="false"
        ),
    )


def downgrade() -> None:
    op.drop_column("wahoo_tokens", "needs_reauth")
