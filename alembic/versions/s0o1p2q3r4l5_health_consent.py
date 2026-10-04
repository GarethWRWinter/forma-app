"""Users: record explicit consent to process health data.

Riders tell the coach about injuries, illness and sleep, which UK GDPR
treats as special-category data (Article 9). Processing it needs explicit
consent, captured at registration and timestamped here.

Revision ID: s0o1p2q3r4l5
Revises: r9n0o1p2q3k4
"""

import sqlalchemy as sa
from alembic import op

revision = "s0o1p2q3r4l5"
down_revision = "r9n0o1p2q3k4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("health_consent_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "health_consent_at")
