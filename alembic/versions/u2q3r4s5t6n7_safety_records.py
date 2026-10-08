"""Safety records: holds, red-flag events, consent log, health screening,
ride-mode session starts; terms and country on users; the SAFETY_LAW
version on forma_calls.

safety_holds, safety_events, consent_events and health_screenings carry
user_id with no foreign key on purpose: they are kept for legal claims, so
deleting an account must never cascade into them.

Revision ID: u2q3r4s5t6n7
Revises: t1p2q3r4s5m6
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "u2q3r4s5t6n7"
down_revision = "t1p2q3r4s5m6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safety_holds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("level", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("red_flag", sa.String(40), nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("opened_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("lifted_at", sa.DateTime(), nullable=True),
        sa.Column("lifted_how", sa.String(20), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("subject_deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_safety_holds_user_id", "safety_holds", ["user_id"])

    op.create_table(
        "safety_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("message_id", sa.String(36), nullable=True),
        sa.Column("matched", sa.String(200), nullable=False),
        sa.Column("card_shown", sa.String(20), nullable=True),
        sa.Column("hold_id", sa.String(36), nullable=True),
        sa.Column("founder_alerted_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("subject_deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_safety_events_user_id", "safety_events", ["user_id"])

    op.create_table(
        "consent_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("doc_version", sa.String(40), nullable=False),
        sa.Column("text_shown", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(300), nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("app_build", sa.String(40), nullable=True),
        sa.Column("subject_deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_consent_events_user_id", "consent_events", ["user_id"])

    op.create_table(
        "health_screenings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column(
            "answers",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("long_break", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("any_yes", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tier", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("clearance_confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("clearance_by", sa.String(200), nullable=True),
        sa.Column("clearance_limits", sa.Text(), nullable=True),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("subject_deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_health_screenings_user_id", "health_screenings", ["user_id"])

    op.create_table(
        "ride_session_starts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("workout_id", sa.String(36), nullable=True),
        sa.Column("steps_hash", sa.String(64), nullable=False),
        sa.Column("ftp", sa.Integer(), nullable=False),
        sa.Column("erg", sa.Boolean(), nullable=False),
        sa.Column("max_target_watts", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ride_session_starts_user_id", "ride_session_starts", ["user_id"])

    op.add_column("users", sa.Column("terms_version", sa.String(40), nullable=True))
    op.add_column("users", sa.Column("terms_accepted_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("ride_mode_ack_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("country", sa.String(2), nullable=True))

    op.add_column("forma_calls", sa.Column("safety_law_version", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("forma_calls", "safety_law_version")

    op.drop_column("users", "country")
    op.drop_column("users", "ride_mode_ack_at")
    op.drop_column("users", "terms_accepted_at")
    op.drop_column("users", "terms_version")

    for table in (
        "ride_session_starts",
        "health_screenings",
        "consent_events",
        "safety_events",
        "safety_holds",
    ):
        op.drop_index(f"ix_{table}_user_id", table_name=table)
        op.drop_table(table)
