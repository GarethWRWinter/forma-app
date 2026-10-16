"""Credentials never reach the logs (OpenWeatherMap key incident, 16 Oct 2026)."""

import logging

import httpx
from uvicorn.logging import AccessFormatter

from app.core.log_hygiene import RedactSecretsFilter, configure_log_hygiene, redact

FAKE_KEY = "owm-test-key-1234567890"


def _weather_transport():
    return httpx.MockTransport(lambda request: httpx.Response(200, json={}))


def test_weather_request_line_is_not_logged(caplog):
    configure_log_hygiene()
    caplog.set_level(logging.INFO)
    with httpx.Client(transport=_weather_transport()) as client:
        client.get(
            "https://api.openweathermap.org/data/4.0/onecall/current",
            params={"lat": 52.6, "lon": -0.7, "units": "metric", "appid": FAKE_KEY},
        )
    assert FAKE_KEY not in caplog.text


def test_http_clients_are_quiet_but_anthropic_lines_stay():
    configure_log_hygiene()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    # One line per model call is how tool rounds are counted from the logs.
    assert logging.getLogger("httpx2").level in (logging.NOTSET, logging.INFO)


def test_a_url_logged_anyway_is_redacted(caplog):
    # httpx hands the logger an httpx.URL object, not a str.
    configure_log_hygiene()
    caplog.set_level(logging.INFO, logger="httpx2")
    url = httpx.URL(f"https://api.openweathermap.org/data/4.0/onecall/current?lat=1&appid={FAKE_KEY}")
    logging.getLogger("httpx2").info('HTTP Request: %s %s "%s"', "GET", url, "HTTP/1.1 200 OK")
    assert FAKE_KEY not in caplog.text
    assert "appid=REDACTED" in caplog.text
    assert "lat=1" in caplog.text


def test_redact_keeps_harmless_parameters():
    url = (
        "/activities/1/streams?keys=time,watts&key_by_type=true&per_page=30"
        "&code=abc123&state=eyJhbGciOi.payload.sig&token=t0k"
    )
    cleaned = redact(url)
    assert "keys=time,watts" in cleaned
    assert "key_by_type=true" in cleaned
    assert "per_page=30" in cleaned
    assert "abc123" not in cleaned
    assert "eyJhbGciOi" not in cleaned
    assert "t0k" not in cleaned


def test_uvicorn_access_line_still_formats_and_is_redacted():
    # Uvicorn's access formatter unpacks five positional args and formats the
    # status with %d, so the filter must keep the tuple shape and the int.
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "100.64.0.1:1234",
            "GET",
            "/api/v1/integrations/wahoo/callback?code=one-time-code&state=signed.state",
            "1.1",
            307,
        ),
        None,
    )
    RedactSecretsFilter().filter(record)
    line = AccessFormatter(fmt="%(client_addr)s %(request_line)s %(status_code)s").format(record)
    assert "one-time-code" not in line
    assert "signed.state" not in line
    assert "307" in line


def test_configure_twice_adds_one_filter():
    configure_log_hygiene()
    configure_log_hygiene()
    filters = logging.getLogger("httpx2").filters
    assert sum(isinstance(f, RedactSecretsFilter) for f in filters) == 1
