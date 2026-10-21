"""Merge: the subscription_cancel_at column with the safety system's line.

b8t9g0h1u2z3 (users.subscription_cancel_at) revises t1p2q3r4s5m6 so it can
ship from main ahead of the safety system. The safety system's migrations
(u2q3r4s5t6n7 to a7s8f9e0t1y2) also start from t1p2q3r4s5m6, which leaves two
heads, and `alembic upgrade head` in the Dockerfile refuses two. This joins
them with no schema change of its own, and upgrades every database state:

- at t1p2q3r4s5m6 (production before either): both lines, then this;
- at b8t9g0h1u2z3 (production after a hotfix from main): the safety line;
- at a7s8f9e0t1y2 (a database that ran the safety system): the column.

Revision ID: c9u0h1i2v3a4
Revises: a7s8f9e0t1y2, b8t9g0h1u2z3
"""

revision = "c9u0h1i2v3a4"
down_revision = ("a7s8f9e0t1y2", "b8t9g0h1u2z3")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
