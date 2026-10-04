"""Users: a rider can turn off the coach's check-in emails.

Revision ID: t1p2q3r4s5m6
Revises: s0o1p2q3r4l5
"""

import sqlalchemy as sa
from alembic import op

revision = "t1p2q3r4s5m6"
down_revision = "s0o1p2q3r4l5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("coach_emails_off_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "coach_emails_off_at")
