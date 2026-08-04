"""Email verification flag. Existing accounts predate verification and
are grandfathered in as verified.

Revision ID: x8t9u0v1w2q3
Revises: w7s8t9u0v1p2
"""

import sqlalchemy as sa
from alembic import op

revision = "x8t9u0v1w2q3"
down_revision = "w7s8t9u0v1p2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "email_verified", sa.Boolean(), nullable=False, server_default="false"
        ),
    )
    op.execute("UPDATE users SET email_verified = true")


def downgrade() -> None:
    op.drop_column("users", "email_verified")
