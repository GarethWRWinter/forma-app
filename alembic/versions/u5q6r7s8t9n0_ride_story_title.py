"""Forma-written ride titles + one-line stories.

Revision ID: u5q6r7s8t9n0
Revises: t4p5q6r7s8m9
"""

import sqlalchemy as sa
from alembic import op

revision = "u5q6r7s8t9n0"
down_revision = "t4p5q6r7s8m9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rides", sa.Column("forma_title", sa.String(255), nullable=True))
    op.add_column("rides", sa.Column("story", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("rides", "story")
    op.drop_column("rides", "forma_title")
