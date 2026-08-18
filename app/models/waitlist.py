"""The waitlist: the signals the Founding Hundred launches into."""

import secrets
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, generate_uuid

# The same unambiguous alphabet the invite minter uses: no O/0 or I/1, because
# these codes get read out over a cafe table and typed in from a phone screen.
# Six characters is 887 million codes, plenty for a list this size.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_referral_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


class WaitlistEntry(Base):
    __tablename__ = "waitlist"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    # First name only. Every letter is written and sent by hand, so the greeting
    # is the first proof that a person is actually on the other end.
    name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Letter 0 delivery: False until Postmark accepts it (pending-approval
    # sandboxing can defer sends; the admin backfill retries these).
    letter0_sent: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
    # The rider's own code, minted on insert. This is the thing they share.
    code: Mapped[str] = mapped_column(
        String(6), unique=True, nullable=False, index=True, default=generate_referral_code
    )
    # Whoever's code brought them here, held as the code rather than a foreign
    # key: a referrer can ask to be removed without orphaning the rows they
    # brought in, and the credit they already earned stays where it landed.
    referred_by: Mapped[str | None] = mapped_column(String(6), nullable=True)
    # Denormalised because it is the first sort key for the whole queue, and
    # counting referrals per rider on every read would not survive a good week.
    # The position itself is never stored: it is derived, so bringing someone
    # in moves you up the moment their row lands.
    referrals: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )
    # The answer to the one question the page asks: which ride is this for.
    goal: Mapped[str | None] = mapped_column(String(280), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
