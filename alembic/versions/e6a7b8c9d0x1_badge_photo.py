"""Badge photo: the rider's chosen ground, saved to their account.

Revision ID: e6a7b8c9d0x1
Revises: d5z6a7b8c9w0
"""

import sqlalchemy as sa
from alembic import op

revision = "e6a7b8c9d0x1"
down_revision = "d5z6a7b8c9w0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("badge_photo", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "badge_photo")
