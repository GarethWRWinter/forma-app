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
