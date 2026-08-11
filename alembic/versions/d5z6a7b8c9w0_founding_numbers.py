"""Founding hundred: rider numbers on users.

Revision ID: d5z6a7b8c9w0
Revises: c4y5z6a7b8v9
"""

import sqlalchemy as sa
from alembic import op

revision = "d5z6a7b8c9w0"
down_revision = "c4y5z6a7b8v9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("founding_number", sa.Integer(), nullable=True))
    op.create_unique_constraint(
        "uq_users_founding_number", "users", ["founding_number"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_users_founding_number", "users", type_="unique")
    op.drop_column("users", "founding_number")
