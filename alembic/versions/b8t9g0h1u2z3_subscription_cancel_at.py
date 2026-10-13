"""Users: when a membership the rider has cancelled ends.

From the dahlia Stripe API versions (2026) a rider who cancels in the
customer portal gets subscription.cancel_at, a timestamp, while the status
stays active and cancel_at_period_end stays false until that date. Forma
stored nothing about it, so Settings went on saying the membership renewed
(found live on 13 Oct 2026). users.subscription_cancel_at holds that date,
set and cleared by the Stripe webhook (billing_service._apply_subscription).

Nullable: a membership that renews has no end date. Existing rows start
empty: riders who cancelled before this column existed are filled in by
scripts/backfill_subscription_cancel_at.py, run once after the deploy.

Revises t1p2q3r4s5m6, main's head, not the safety system's migrations, so
the fix for the live bug can ship from main on its own. The safety-system
branch joins the two lines again in c9u0h1i2v3a4 (a merge revision), so
`alembic upgrade head` still has one head whichever order they deploy in.

Revision ID: b8t9g0h1u2z3
Revises: t1p2q3r4s5m6
"""

import sqlalchemy as sa
from alembic import op

revision = "b8t9g0h1u2z3"
down_revision = "t1p2q3r4s5m6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("subscription_cancel_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "subscription_cancel_at")
