"""Ride locale from the file's own GPS — ground truth for where it happened.

Revision ID: w7s8t9u0v1p2
Revises: v6r7s8t9u0o1
"""

import sqlalchemy as sa
from alembic import op

revision = "w7s8t9u0v1p2"
down_revision = "v6r7s8t9u0o1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rides", sa.Column("location_name", sa.String(120), nullable=True))


def downgrade() -> None:
    op.drop_column("rides", "location_name")
