"""Waitlist: a name, a queue you can move up, and the one question.

Revision ID: o6k7l8m9n0h1
Revises: n5j6k7l8m9g0
"""

import secrets

import sqlalchemy as sa
from alembic import op

revision = "o6k7l8m9n0h1"
down_revision = "n5j6k7l8m9g0"
branch_labels = None
depends_on = None

# Copied from the model rather than imported. A migration has to keep working
# whatever the application code becomes, so it carries its own alphabet.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(6))


def upgrade() -> None:
    # First name only. The letters are written and sent by hand, and "Hi there"
    # would contradict the one promise the list is built on.
    op.add_column("waitlist", sa.Column("name", sa.String(80), nullable=True))
    op.add_column("waitlist", sa.Column("code", sa.String(6), nullable=True))
    op.add_column("waitlist", sa.Column("referred_by", sa.String(6), nullable=True))
    op.add_column(
        "waitlist",
        sa.Column("referrals", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("waitlist", sa.Column("goal", sa.String(280), nullable=True))

    # The order below is the whole point. Everyone already on the list has to
    # get a code first: NOT NULL and a unique index both refuse to build over
    # the NULLs the column was just created with.
    conn = op.get_bind()
    used: set[str] = set()
    for (row_id,) in conn.execute(sa.text("SELECT id FROM waitlist")).fetchall():
        code = _code()
        while code in used:
            code = _code()
        used.add(code)
        conn.execute(
            sa.text("UPDATE waitlist SET code = :code WHERE id = :id"),
            {"code": code, "id": row_id},
        )

    op.alter_column("waitlist", "code", existing_type=sa.String(6), nullable=False)
    op.create_index("ix_waitlist_code", "waitlist", ["code"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_waitlist_code", table_name="waitlist")
    op.drop_column("waitlist", "goal")
    op.drop_column("waitlist", "referrals")
    op.drop_column("waitlist", "referred_by")
    op.drop_column("waitlist", "code")
    op.drop_column("waitlist", "name")
