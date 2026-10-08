"""The age an under-18 rider told us, kept on their red-flag event.

safety_events.stated_age decides how long a minor's safety records are kept
(safety_service.minor_retention_until): until the 21st birthday that age
implies, if that is later than three years after the account goes. A real
minor can only have joined with a false adult date of birth, so the date of
birth alone would let the records go too early.

Nullable: most events are not about age, and older minor events never
recorded one. For those the age is read again from the rider's own words
(safety_service.minor_retention_until); where the words give none, the purge
keeps its three years, or the date of birth if that runs later.

Revision ID: a7s8f9e0t1y2
Revises: w4s5t6u7v8p9
"""

import sqlalchemy as sa
from alembic import op

revision = "a7s8f9e0t1y2"
down_revision = "w4s5t6u7v8p9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("safety_events", sa.Column("stated_age", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("safety_events", "stated_age")
