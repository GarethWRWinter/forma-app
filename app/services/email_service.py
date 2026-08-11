"""Transactional email, pluggable by configuration.

With POSTMARK_SERVER_TOKEN set, mail goes out through Postmark. Without it
(local dev, or production before the account exists), the full message is
logged instead, so every flow can be built and tested end to end before a
provider is wired in. Templates speak in Forma's voice: warm, direct,
British English, no em dashes.
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

POSTMARK_API = "https://api.postmarkapp.com/email"


def is_configured() -> bool:
    return bool(settings.postmark_server_token)


async def send(to: str, subject: str, text_body: str) -> bool:
    """Send one transactional email. Returns True when handed to the
    provider (or logged in dev mode); False on provider failure."""
    if not is_configured():
        logger.info(
            "EMAIL (no provider configured)\nTo: %s\nSubject: %s\n\n%s",
            to, subject, text_body,
        )
        return True

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                POSTMARK_API,
                headers={
                    "X-Postmark-Server-Token": settings.postmark_server_token,
                    "Accept": "application/json",
                },
                json={
                    "From": settings.email_from,
                    "To": to,
                    "Subject": subject,
                    "TextBody": text_body,
                    "MessageStream": "outbound",
                },
            )
            response.raise_for_status()
        return True
    except httpx.HTTPError:
        logger.exception("Email send failed (to=%s, subject=%s)", to, subject)
        return False


def _first_name(full_name: str | None, email: str) -> str:
    return (full_name or email.split("@")[0]).split()[0]


async def send_verification(to: str, full_name: str | None, link: str) -> bool:
    name = _first_name(full_name, to)
    return await send(
        to,
        "One click and your coach is ready",
        f"""{name},

Welcome to Forma. One click confirms this address is yours:

{link}

The link works for 24 hours. If you didn't create a Forma account, ignore
this and nothing happens.

See you on the road,
Forma
""",
    )


async def send_waitlist_welcome(to: str) -> bool:
    """Letter 0 of the Founding Hundred letters. Fires on waitlist join."""
    return await send(
        to,
        "your place is held",
        """Your place is held.

Here's the deal I owe you now that you're on the list: one letter a week until
the doors open. Each one contains something you can use on this week's rides.
Real numbers from my own testing, the marginal gains that cost nothing, the
fuelling maths most riders get wrong. If you're not a little faster by launch
day, I'll have failed at the easy half of this.

The first letter lands this week. It's about the test that showed me my own
body was lying to me by 11%.

Until then, one question, and I read every reply: what's the ride you're
training for? A race, a sportive, a climb, or just the club run where you want
to be the one setting the pace. Hit reply and tell me. It genuinely shapes
what I build.

G

PS. You joined a list of one hundred, not one hundred thousand. When I write
that I read every reply, it's because the maths allows it.
""",
    )


async def send_password_reset(to: str, full_name: str | None, link: str) -> bool:
    name = _first_name(full_name, to)
    return await send(
        to,
        "Reset your Forma password",
        f"""{name},

Someone asked to reset the password on your Forma account. If that was
you, this link sets a new one:

{link}

It works for one hour. If it wasn't you, ignore this email; your password
stays as it is and your account is untouched.

Forma
""",
    )
