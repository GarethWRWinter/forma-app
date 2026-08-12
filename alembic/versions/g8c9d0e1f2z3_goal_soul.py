"""Goal soul: the why and the becoming, written during goalcraft.

Revision ID: g8c9d0e1f2z3
Revises: f7b8c9d0e1y2
"""

import sqlalchemy as sa
from alembic import op

revision = "g8c9d0e1f2z3"
down_revision = "f7b8c9d0e1y2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("goal_events", sa.Column("why", sa.Text(), nullable=True))
    op.add_column("goal_events", sa.Column("becoming", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("goal_events", "becoming")
    op.drop_column("goal_events", "why")
