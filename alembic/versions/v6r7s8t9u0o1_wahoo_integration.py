"""Wahoo Cloud API integration: token table + 'wahoo' ride source.

Revision ID: v6r7s8t9u0o1
Revises: u5q6r7s8t9n0
"""

import sqlalchemy as sa
from alembic import op

revision = "v6r7s8t9u0o1"
down_revision = "u5q6r7s8t9n0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wahoo_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id"),
            unique=True, nullable=False,
        ),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("wahoo_user_id", sa.BigInteger(), nullable=True),
        sa.Column("scope", sa.String(255), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("backfill_status", sa.String(20), nullable=True),
        sa.Column("backfill_total", sa.Integer(), nullable=True),
        sa.Column("backfill_progress", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_wahoo_tokens_wahoo_user_id", "wahoo_tokens", ["wahoo_user_id"]
    )
    # New enum value; ADD VALUE is safe outside a transaction block on PG 12+,
    # and alembic runs each migration in one — IF NOT EXISTS keeps it rerunnable.
    op.execute("ALTER TYPE ridesource ADD VALUE IF NOT EXISTS 'wahoo'")


def downgrade() -> None:
    op.drop_index("ix_wahoo_tokens_wahoo_user_id", table_name="wahoo_tokens")
    op.drop_table("wahoo_tokens")
    # Postgres cannot drop an enum value; 'wahoo' stays behind harmlessly.
