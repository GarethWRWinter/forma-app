"""Country on waitlist entries, so riders from places Forma can't serve yet
(the US and Canada) are told apart from the founding queue.

Revision ID: v3r4s5t6u7o8
Revises: u2q3r4s5t6n7
"""

import sqlalchemy as sa
from alembic import op

revision = "v3r4s5t6u7o8"
down_revision = "u2q3r4s5t6n7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("waitlist", sa.Column("country", sa.String(2), nullable=True))


def downgrade() -> None:
    op.drop_column("waitlist", "country")
