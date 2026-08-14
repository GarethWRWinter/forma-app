"""Ride analysis cache: the coach's read of the actual file.

Revision ID: i0e1f2g3h4b5
Revises: h9d0e1f2g3a4
"""

import sqlalchemy as sa
from alembic import op

revision = "i0e1f2g3h4b5"
down_revision = "h9d0e1f2g3a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rides", sa.Column("analysis", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("rides", "analysis")
