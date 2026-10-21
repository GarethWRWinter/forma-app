"""Holds that end by themselves (expires_at, for the easy week after a
fever); the doctor's limits kept on their own, append-only, in
clearance_limits; the exchange itself (rider_message, coach_reply) on
safety_events; and retain_until on every retained safety table, which the
purge stamps (three years, or a minor's 21st birthday if later).

clearance_limits carries user_id with no foreign key, like the other safety
records: it is kept for legal claims, so deleting an account must never
cascade into it. The purge stamps subject_deleted_at and retain_until instead.

Revision ID: w4s5t6u7v8p9
Revises: v3r4s5t6u7o8
"""

import sqlalchemy as sa
from alembic import op

revision = "w4s5t6u7v8p9"
down_revision = "v3r4s5t6u7o8"
branch_labels = None
depends_on = None


RETAINED = ("safety_holds", "safety_events", "consent_events", "health_screenings")


def upgrade() -> None:
    op.add_column("safety_holds", sa.Column("expires_at", sa.DateTime(), nullable=True))
    for table in RETAINED:
        op.add_column(table, sa.Column("retain_until", sa.DateTime(), nullable=True))
    op.add_column("safety_events", sa.Column("rider_message", sa.Text(), nullable=True))
    op.add_column("safety_events", sa.Column("coach_reply", sa.Text(), nullable=True))

    op.create_table(
        "clearance_limits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("limits", sa.Text(), nullable=False),
        sa.Column("cleared_by", sa.String(200), nullable=True),
        sa.Column("cleared_for", sa.Text(), nullable=True),
        sa.Column("consent_event_id", sa.String(36), nullable=True),
        sa.Column("recorded_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("retired_at", sa.DateTime(), nullable=True),
        sa.Column("retired_note", sa.Text(), nullable=True),
        sa.Column("subject_deleted_at", sa.DateTime(), nullable=True),
        sa.Column("retain_until", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_clearance_limits_user_id", "clearance_limits", ["user_id"])

    # Limits recorded before this table existed lived only on the screening.
    # Carry them over so the coach keeps every one.
    op.execute(
        """
        INSERT INTO clearance_limits
            (id, user_id, limits, cleared_by, cleared_for, recorded_at,
             subject_deleted_at, retain_until)
        SELECT id, user_id, clearance_limits, clearance_by, 'screening',
               COALESCE(clearance_confirmed_at, created_at), subject_deleted_at,
               retain_until
        FROM health_screenings
        WHERE clearance_limits IS NOT NULL AND TRIM(clearance_limits) <> ''
        """
    )


def downgrade() -> None:
    op.drop_index("ix_clearance_limits_user_id", table_name="clearance_limits")
    op.drop_table("clearance_limits")
    op.drop_column("safety_events", "coach_reply")
    op.drop_column("safety_events", "rider_message")
    for table in reversed(RETAINED):
        op.drop_column(table, "retain_until")
    op.drop_column("safety_holds", "expires_at")
