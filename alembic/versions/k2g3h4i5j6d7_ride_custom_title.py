"""Rider-named rides: a title a human chose is never overwritten.

Revision ID: k2g3h4i5j6d7
Revises: j1f2g3h4i5c6
"""

import sqlalchemy as sa
from alembic import op

revision = "k2g3h4i5j6d7"
down_revision = "j1f2g3h4i5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rides",
        sa.Column(
            "title_is_custom",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("rides", "title_is_custom")
