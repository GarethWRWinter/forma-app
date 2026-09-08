"""The coach knowing where a rider is, and writing first when they go quiet."""

import asyncio
from datetime import datetime, timedelta

import pytest

from app.models.onboarding import EventPriority, EventType, GoalEvent, GoalStatus
from app.models.outreach import OutreachLog
from app.models.user import User
from app.services import outreach_service
from app.services.activation_service import Facts, activation_state, derive_stage
from app.services.coach_service import _looks_like_title, _needs_round_break

NOW = datetime(2026, 9, 3, 12, 0, 0)


def _facts(**over) -> Facts:
    base = dict(
        has_goal=True, data_connected=True, ride_count=3, has_active_plan=True,
        plan_started_at=NOW - timedelta(days=10), rides_since_plan=2,
        last_activity=NOW - timedelta(days=1), now=NOW,
    )
    base.update(over)
    return Facts(**base)


def test_stage_is_the_first_unmet_step_in_order():
    assert derive_stage(_facts(has_goal=False)) == "goal"
    assert derive_stage(_facts(data_connected=False, ride_count=0)) == "data"
    assert derive_stage(_facts(ride_count=0)) == "first_ride"
    assert derive_stage(_facts(has_active_plan=False)) == "plan"
    assert derive_stage(_facts(plan_started_at=NOW - timedelta(days=3))) == "first_week"
    assert derive_stage(_facts(rides_since_plan=0)) == "first_week"
    assert derive_stage(_facts()) == "established"


def test_a_new_rider_with_a_goal_is_told_to_connect_rides(db_session):
    user = User(email="nathan@example.com", hashed_password="x", full_name="Nathan O'Neill",
                created_at=NOW - timedelta(days=1, hours=2))
    db_session.add(user)
    db_session.commit()
    db_session.add(GoalEvent(
        user_id=user.id, event_name="New York Gran Fondo", event_date=datetime(2027, 5, 2).date(),
        event_type=EventType.road_race, priority=EventPriority.a_race, status=GoalStatus.upcoming,
    ))
    db_session.commit()

    state = activation_state(db_session, user, now=NOW)

    assert state["stage"] == "data"
    assert "Connect Wahoo" in state["next_action"]["instruction"]
    assert state["quiet_days"] == 1


def test_outreach_writes_once_per_threshold_and_escalates(db_session, monkeypatch):
    user = User(email="quiet@example.com", hashed_password="x", full_name="Quiet Rider",
                is_active=True, email_verified=True)
    db_session.add(user)
    db_session.commit()

    quiet = {"days": 1}

    def fake_state(db, u, now=None):
        return {"stage": "data", "next_action": {"title": "Connect your rides",
                "instruction": "Settings, then Data in, then Connect Wahoo.", "link": "/dashboard/settings"},
                "quiet_days": quiet["days"], "last_activity": "x", "stage_index": 2, "facts": {}}

    composed = []

    def fake_compose(db, u, state):
        composed.append(state["quiet_days"])
        return ("Before we build the plan", "Nathan, connect your rides. Settings, then Data in.\n\nWhat's your head unit?\n\nForma")

    sent = []

    async def fake_send(to, subject, body, from_address=None):
        sent.append((to, subject))
        return True

    monkeypatch.setattr(outreach_service, "activation_state", fake_state)
    monkeypatch.setattr(outreach_service, "compose", fake_compose)
    monkeypatch.setattr(outreach_service.email_service, "send", fake_send)

    # Day 1: one email. Running again the same day: nothing.
    assert len(asyncio.run(outreach_service.send_due(db_session))) == 1
    assert asyncio.run(outreach_service.send_due(db_session)) == []
    # Day 2: still under the next threshold: nothing.
    quiet["days"] = 2
    assert asyncio.run(outreach_service.send_due(db_session)) == []
    # Day 3 and day 7: one each.
    quiet["days"] = 3
    assert len(asyncio.run(outreach_service.send_due(db_session))) == 1
    quiet["days"] = 9
    assert len(asyncio.run(outreach_service.send_due(db_session))) == 1
    quiet["days"] = 30
    assert asyncio.run(outreach_service.send_due(db_session)) == []

    logged = db_session.query(OutreachLog).filter(OutreachLog.user_id == user.id).all()
    assert sorted(l.threshold_days for l in logged) == [1, 3, 7]
    assert len(sent) == 3


def test_dry_run_sends_and_logs_nothing(db_session, monkeypatch):
    user = User(email="dry@example.com", hashed_password="x", is_active=True, email_verified=True)
    db_session.add(user)
    db_session.commit()
    monkeypatch.setattr(outreach_service, "activation_state", lambda db, u, now=None: {
        "stage": "goal", "next_action": {"title": "Set the goal", "instruction": "x", "link": "/"},
        "quiet_days": 4, "last_activity": "x", "stage_index": 1, "facts": {}})
    monkeypatch.setattr(outreach_service, "compose", lambda db, u, s: ("s", "b"))
    sent = []

    async def fake_send(*a, **k):
        sent.append(a)
        return True

    monkeypatch.setattr(outreach_service.email_service, "send", fake_send)

    report = asyncio.run(outreach_service.send_due(db_session, dry_run=True))

    assert len(report) == 1 and report[0]["threshold_days"] == 3
    assert sent == []
    assert db_session.query(OutreachLog).count() == 0


def test_email_split_and_dash_scrub():
    subject, body = outreach_service._split(
        "Subject: Before we build your plan.\n\nNathan, one thing — connect Wahoo.\n\nForma",
        fallback_subject="fallback",
    )
    assert subject == "Before we build your plan"
    assert "—" not in body and "connect Wahoo" in body
    subject, body = outreach_service._split("no subject line here", fallback_subject="Set the goal")
    assert subject == "Set the goal" and body == "no subject line here"


def test_title_guard_rejects_prose_and_keeps_labels():
    assert _looks_like_title("Fuelling for the 312")
    assert _looks_like_title("Tuesday intervals rethink")
    assert not _looks_like_title("I appreciate the structure you've set up. I should be honest though")
    assert not _looks_like_title("Saddle pain fix.")
    assert not _looks_like_title("Two\nlines")
    assert not _looks_like_title("")


def test_round_break_only_when_text_runs_on():
    assert _needs_round_break("waiting for time to appear.")
    assert not _needs_round_break("")
    assert not _needs_round_break("ends with newline\n")
    assert not _needs_round_break("ends with space ")
