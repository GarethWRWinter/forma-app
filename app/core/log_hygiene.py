"""Keep credentials out of the logs.

OpenWeatherMap only takes its key in the query string (appid=), and httpx logs
every request URL at INFO, so the key was written to Railway's logs on every
weather lookup (found 16 Oct 2026) and, through Sentry's logging integration,
into the breadcrumbs of any error event. OAuth callbacks arrive with a one-time
code and a signed state in theirs, which Uvicorn's access log records.

Two layers, because one would only hold until the next library logs a URL:

1. The plain HTTP clients log at WARNING, so request lines are never written.
   httpx2 stays at INFO: it is the Anthropic client's, its URLs carry no
   secrets, and one line per model call is how a turn's tool rounds are
   counted from the logs.
2. A filter redacts credential query parameters from anything that does get
   logged, on those loggers, Uvicorn's, and every root handler.
"""

import logging
import re

# Exact names only. Strava's keys= (a list of stream names) must survive.
_SECRET_PARAMS = (
    "appid",
    "api_key",
    "apikey",
    "key",
    "token",
    "access_token",
    "refresh_token",
    "client_secret",
    "code",
    "state",
    "password",
    "signature",
)

_SECRET_QUERY = re.compile(
    r"([?&](?:%s)=)[^&\s\"'#]+" % "|".join(_SECRET_PARAMS),
    re.IGNORECASE,
)

REDACTED = "REDACTED"

# Request lines carry URLs; nothing else in these is worth INFO.
QUIET_LOGGERS = ("httpx", "httpcore", "httpcore2")

FILTERED_LOGGERS = (
    "httpx",
    "httpx2",
    "httpcore",
    "httpcore2",
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
)


def redact(text: str) -> str:
    """The text with every credential query value replaced."""
    return _SECRET_QUERY.sub(rf"\1{REDACTED}", text)


def _redact_arg(value):
    # Numbers stay numbers: Uvicorn's access line formats the status with %d.
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # httpx passes an httpx.URL, not a str, so check the rendered form.
    text = str(value)
    cleaned = redact(text)
    return cleaned if cleaned != text else value


class RedactSecretsFilter(logging.Filter):
    """Rewrites a record in place, so every handler and Sentry see it clean."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _redact_arg(v) for k, v in record.args.items()}
        return True


_FILTER = RedactSecretsFilter()


def configure_log_hygiene() -> None:
    """Quiet the HTTP clients and redact secrets. Safe to call twice."""
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in FILTERED_LOGGERS:
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)
    for handler in logging.getLogger().handlers:
        if _FILTER not in handler.filters:
            handler.addFilter(_FILTER)
