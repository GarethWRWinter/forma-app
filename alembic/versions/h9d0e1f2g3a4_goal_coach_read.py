"""The coach's read on a goal: verdict text + timestamp.

Revision ID: h9d0e1f2g3a4
Revises: g8c9d0e1f2z3
"""

import sqlalchemy as sa
from alembic import op

revision = "h9d0e1f2g3a4"
down_revision = "g8c9d0e1f2z3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("goal_events", sa.Column("coach_read", sa.Text(), nullable=True))
    op.add_column(
        "goal_events", sa.Column("coach_read_at", sa.DateTime(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("goal_events", "coach_read_at")
    op.drop_column("goal_events", "coach_read")
