"""Stripe billing columns on users.

Revision ID: y0u1v2w3x4r5
Revises: x8t9u0v1w2q3
"""

import sqlalchemy as sa
from alembic import op

revision = "y0u1v2w3x4r5"
down_revision = "x8t9u0v1w2q3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("stripe_customer_id", sa.String(64), nullable=True))
    op.create_index("ix_users_stripe_customer_id", "users", ["stripe_customer_id"])
    op.add_column(
        "users",
        sa.Column(
            "subscription_status", sa.String(20), nullable=False, server_default="none"
        ),
    )
    op.add_column(
        "users", sa.Column("subscription_period_end", sa.DateTime(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("users", "subscription_period_end")
    op.drop_column("users", "subscription_status")
    op.drop_index("ix_users_stripe_customer_id", table_name="users")
    op.drop_column("users", "stripe_customer_id")
