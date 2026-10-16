"""A session the coach booked keeps the name it was booked under (16 Oct 2026).

The calendar used to swap every title for a stock name, so the session Gareth
asked for, shaped like his Sa Calobra PB file, showed as a generic threshold
session.
"""

from datetime import date

from app.core.session_naming import session_display_name, workout_display_name
from app.schemas.training import WorkoutResponse


def _response(**over):
    base = dict(
        id="w-1",
        scheduled_date=date(2026, 10, 19),
        title="Sa Calobra rehearsal",
        workout_type="threshold",
        status="planned",
    )
    base.update(over)
    return WorkoutResponse(**base)


def test_coach_booked_session_keeps_its_name():
    assert _response(phase_id=None).title == "Sa Calobra rehearsal"


def test_plan_session_takes_the_canonical_name():
    shown = _response(phase_id="phase-1", title="3x15 Threshold").title
    assert shown == session_display_name("threshold", "w-1")


def test_an_overlong_or_empty_own_title_falls_back():
    long_title = "Two twelve minute reps on a local climb shaped exactly like the PB file"
    assert _response(phase_id=None, title=long_title).title == session_display_name("threshold", "w-1")
    assert _response(phase_id=None, title="  ").title == session_display_name("threshold", "w-1")


def test_rest_days_keep_their_title():
    assert workout_display_name("rest", "w-2", "Rest", "phase-1") == "Rest"


def test_the_coach_and_the_calendar_agree():
    from types import SimpleNamespace

    from app.services.coach_service import _session_name

    booked = SimpleNamespace(id="w-1", workout_type="threshold", title="Sa Calobra rehearsal", phase_id=None)
    planned = SimpleNamespace(id="w-1", workout_type="threshold", title="3x15 Threshold", phase_id="p")
    assert _session_name(booked) == _response(phase_id=None).title
    assert _session_name(planned) == _response(phase_id="p", title="3x15 Threshold").title
