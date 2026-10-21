"""The safeguarding protocol (prd/legal/safeguarding-protocol.md) is what
Gareth follows with an alert in front of him, so it must stay true to the
code: every review command on it parses, every option the review tool has is
explained, every alert Forma sends has its subject and a section, and the
numbers it quotes are the numbers the code uses. The launch plan keeps the
safety system's launch gate, and none of the three docs uses a dash.

Reads files and calls the email builder with sending replaced; no database,
no model, no Stripe."""

from __future__ import annotations

import asyncio
import re
import shlex
from datetime import timedelta
from pathlib import Path

import pytest

from app.services import coach_service, email_service, safety_screen, safety_service
from scripts import review_safety_event as tool

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "prd" / "legal" / "safeguarding-protocol.md"
PLAN = ROOT / "prd" / "launch-plan.md"
AUDIT = ROOT / "prd" / "launch-audit.md"
SCRIPT = "scripts/review_safety_event.py"
DB_ONLY = "railway run --service Postgres bash -c "
WITH_STRIPE = "railway run railway run --service Postgres bash -c "
INNER = 'DATABASE_URL="$DATABASE_PUBLIC_URL" .venv/bin/python ' + SCRIPT

SAFETY_GATE = (
    "Safety system (built 8 Oct 2026):** red team re-run with the live model and no "
    "reply rated unsafe; Kickr test of ERG cap, sprint release, pause and stop; "
    "FORMA_EDGE_SECRET set on Vercel and Railway; ip-check confirmed on a real request."
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _command_lines() -> list[str]:
    """Every line in a fenced block of the protocol that runs the tool."""
    blocks = re.findall(r"```\n(.*?)```", _text(PROTOCOL), re.S)
    return [line for block in blocks for line in block.splitlines() if SCRIPT in line]


def _tool_args(line: str) -> list[str]:
    """What the tool itself receives from one documented command, after the
    shell has stripped both layers of quoting."""
    outer = shlex.split(line)
    inner = shlex.split(outer[outer.index("-c") + 1])
    return inner[inner.index(SCRIPT) + 1:]


def test_the_protocol_documents_runnable_commands_for_every_action():
    lines = _command_lines()
    assert len(lines) >= 6
    seen = set()
    for line in lines:
        args = _tool_args(line)
        parsed = tool.parse_args(args)  # exits on anything argparse refuses
        seen |= {flag for flag in ("--reviewed", "--lift-hold", "--close-minor") if flag in args}
        if not (parsed.reviewed or parsed.lift_hold or parsed.close_minor):
            seen.add("list")
        # A note sits inside single quotes: an apostrophe would end them early
        # and the tool would not get this prefix.
        outer = shlex.split(line)
        assert outer[outer.index("-c") + 1].startswith(INNER), line
    assert seen == {"list", "--reviewed", "--lift-hold", "--close-minor"}


def test_commands_that_touch_stripe_load_the_api_variables_and_the_rest_only_the_database():
    """--lift-hold turns renewal back on and --close-minor cancels and lists
    payments. Under the Postgres service alone the Stripe keys are missing,
    and the tool would report no subscriptions and no payments to refund."""
    for line in _command_lines():
        args = _tool_args(line)
        if "--lift-hold" in args or "--close-minor" in args:
            assert line.startswith(WITH_STRIPE), line
        else:
            assert line.startswith(DB_ONLY), line


def test_every_option_the_tool_has_is_explained(capsys):
    with pytest.raises(SystemExit):
        tool.parse_args(["--help"])
    options = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", capsys.readouterr().out)) - {"--help"}
    assert {"--reviewed", "--lift-hold", "--close-minor", "--note", "--commit", "--force"} <= options
    text = _text(PROTOCOL)
    missing = sorted(o for o in options if f"`{o}" not in text and f" {o} " not in text)
    assert missing == [], f"Options the protocol never mentions: {missing}"


def _subjects() -> dict[str, str]:
    sent: dict[str, str] = {}

    async def fake_send(to, subject, text_body, from_address=None):
        sent["subject"] = subject
        return True

    kinds = sorted(safety_screen.ALERT_KINDS) + ["reply_failed"] + sorted(email_service._OPS_ALERTS)
    out = {}
    original = email_service.send
    email_service.send = fake_send
    try:
        for kind in kinds:
            asyncio.run(email_service.send_safety_alert(kind, "r@example.com", "u-1", "x"))
            out[kind] = sent.pop("subject")
    finally:
        email_service.send = original
    return out


def test_every_alert_forma_sends_has_its_subject_and_a_section_in_the_protocol():
    text = _text(PROTOCOL)
    headings = set(re.findall(r"^### (.+)$", text, re.M))
    table = text.split("## The alerts", 1)[1].split("\n## ", 1)[0]
    rows = {
        cells[0]: cells[2]
        for cells in (
            [c.strip() for c in line.strip().strip("|").split("|")]
            for line in table.splitlines()
            if line.startswith("| Forma ")
        )
    }
    for kind, subject in _subjects().items():
        assert subject in rows, f"No row for {kind}: {subject}"
        assert rows[subject] in headings, f"{subject} points at a missing section"


def test_the_numbers_and_words_the_protocol_quotes_are_the_codes():
    text = _flat(_text(PROTOCOL))
    assert coach_service.MINOR_CLOSING in text
    assert safety_screen.REPLY_FAILED_MESSAGE in text
    assert coach_service.OUTAGE_FAILED_TURNS == 3 and coach_service.OUTAGE_WINDOW_MINUTES == 10
    assert "More than three coach replies failed across all riders inside ten minutes" in text
    assert coach_service.OPS_ALERT_INTERVAL == timedelta(hours=1)
    assert "each ops alert at most once an hour" in text
    assert safety_screen.WELLBEING_QUIET_DAYS == 14
    assert "no check-in emails or nudges for 14 days" in text
    assert tool.CLOSE_NOTE in text
    assert safety_service.MINOR_QUIET_DAYS == 180
    assert "For 180 days after, the check no longer reads an age" in text
    assert safety_service.MINOR_YOUNGEST_AGE == 13
    assert "as if the rider were 13 when flagged: eight years" in text
    assert safety_service.FORMA_EMAIL == "gareth@ridewithforma.com"


def test_the_launch_plan_keeps_the_safety_gate_and_the_audit_tracks_it():
    plan = _flat(_text(PLAN))
    assert re.search(r"5f\. \[[ x~]\] \*\*LAUNCH GATE: Safety system", plan)
    assert SAFETY_GATE in plan
    assert safety_service.ERG_CAP == 1.30 and "130% of FTP" in plan
    audit = _text(AUDIT)
    for item in ("E5 MUST", "E6 MUST", "J8 MUST", "K4 MUST", "K5 MUST"):
        assert item in audit


def test_the_gate_names_the_secret_and_the_check_the_code_reads():
    """The gate tells Gareth which variable to set and which page to open:
    both must be the ones the API actually has."""
    from app.api.v1 import auth
    from app.config import Settings

    assert Settings.model_fields["edge_secret"].validation_alias == "FORMA_EDGE_SECRET"
    paths = {getattr(route, "path", "") for route in auth.router.routes}
    assert any(path.endswith("/ip-check") for path in paths)
    plan = _flat(_text(PLAN))
    assert "https://app.ridewithforma.com/api/v1/auth/ip-check" in plan
    assert "via_edge true" in plan and "via_edge false" in plan


@pytest.mark.parametrize("path", [PROTOCOL, PLAN, AUDIT], ids=lambda p: p.name)
def test_no_em_or_en_dashes(path):
    text = _text(path)
    assert "\u2013" not in text and "\u2014" not in text


def test_the_protocol_never_exclaims_or_says_kicker():
    # The audit names the "kicker" rule itself, so only the protocol, whose
    # templates go to riders, is checked for the word.
    text = _text(PROTOCOL)
    assert "!" not in text and "kicker" not in text.lower()
