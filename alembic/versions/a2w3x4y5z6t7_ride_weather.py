"""Conditions stamped on rides.

Revision ID: a2w3x4y5z6t7
Revises: z1v2w3x4y5s6
"""

import sqlalchemy as sa
from alembic import op

revision = "a2w3x4y5z6t7"
down_revision = "z1v2w3x4y5s6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rides", sa.Column("weather", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("rides", "weather")
