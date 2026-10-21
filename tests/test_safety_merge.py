"""Where the safety system meets what main built while it waited.

The safety branch gated plan tools that only ever wrote template steps; main
let the coach write its own steps (build_steps, 30% to 300% of FTP), book
sessions outside any plan, and confirm a booking with "Booked.". Main also
added a season keeper that builds plans by itself and a season note that
proposes the next goal. These tests pin the joins: the gate judges the steps
that will actually be written, the reply check knows a booking went in, a
refusal never tells the app the plan moved, and nothing is built or pitched
where the safety law says not to.
"""

import asyncio
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core import forma_core
from app.models.chat import ChatSession
from app.models.onboarding import EventPriority, GoalEvent, GoalStatus
from app.models.training import PeriodizationModel, PlanStatus, TrainingPlan, Workout
from app.models.user import User
from app.services import coach_service, safety_screen, season_service
from app.services import safety_service as ss
from app.services.safety_screen import ReplyGuard

TODAY = date.today()
REPS = [
    {"step_type": "warmup", "duration_seconds": 600, "power_low_pct": 50, "power_high_pct": 70},
    {"step_type": "interval_on", "duration_seconds": 600, "power_pct": 100, "repeat_count": 3},
    {"step_type": "interval_off", "duration_seconds": 300, "power_pct": 55},
]


def _user(db, email="rider@example.com") -> User:
    user = User(email=email, hashed_password="x", full_name="Sam Rider", ftp=250)
    db.add(user)
    db.commit()
    return user


def _day(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


def _book(db, user, **extra) -> str:
    return coach_service._execute_tool(db, user, "add_workout", {
        "scheduled_date": _day(3), "title": "Climb reps", "workout_type": "threshold", **extra,
    })


# ── The gate judges the coach's own steps ───────────────────────────────────


def test_under_an_easy_hold_the_coachs_hard_steps_are_refused_on_an_easy_type(db_session):
    user = _user(db_session)
    ss.open_hold(db_session, user, "easy_only", "Knee", "detector")
    refused = _book(db_session, user, workout_type="endurance", steps=REPS)
    assert refused.startswith("Not changed") and "75% of FTP" in refused
    assert db_session.query(Workout).count() == 0


def test_steps_on_a_rest_day_are_refused_because_no_gate_judges_a_rest_day(db_session):
    user = _user(db_session)
    ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector")
    refused = _book(db_session, user, workout_type="rest", steps=REPS)
    assert refused.startswith("Error: nothing was booked.") and "rest day carries no steps" in refused
    assert db_session.query(Workout).count() == 0


def test_new_steps_under_an_injury_hold_are_a_prescribed_ride_and_refused(db_session):
    user = _user(db_session)
    _book(db_session, user, workout_type="endurance")
    workout = db_session.query(Workout).one()
    ss.open_hold(db_session, user, "easy_only", "Knee pain", "detector", red_flag="injury")
    easy = [{"step_type": "steady_state", "duration_seconds": 1800, "power_pct": 60}]
    refused = coach_service._execute_tool(db_session, user, "update_workout", {
        "workout_id": workout.id, "steps": easy,
    })
    assert refused == coach_service._INJURY_REFUSAL
    db_session.expire_all()
    assert not db_session.get(Workout, workout.id).steps


def test_a_retype_keeps_a_name_it_was_booked_under_and_renames_a_stock_one(db_session):
    user = _user(db_session)
    _book(db_session, user, title="Sa Calobra rehearsal", steps=REPS)
    _book(db_session, user, title="Threshold 2x20", steps=REPS)
    own, stock = (
        db_session.query(Workout).filter(Workout.title == t).one()
        for t in ("Sa Calobra rehearsal", "Threshold 2x20")
    )
    for w in (own, stock):
        result = coach_service._execute_tool(db_session, user, "update_workout", {
            "workout_id": w.id, "workout_type": "recovery",
        })
        assert result.startswith("Updated.") and "steps were rebuilt" in result
    db_session.expire_all()
    assert db_session.get(Workout, own.id).title == "Sa Calobra rehearsal"
    assert db_session.get(Workout, stock.id).title == "Recovery Spin"


# ── The reply check and the app hear what really happened ───────────────────


def test_the_reply_check_releases_a_booking_claim_once_the_booking_went_in():
    screen = safety_screen.ScreenResult(hits=safety_screen._detect("Book me a session"), country=None)
    guard = ReplyGuard(screen, None, lambda: None, since=datetime.utcnow() - timedelta(minutes=1))
    held = guard.feed("I've booked Monday's session. ") + guard.before_tools()
    assert held == ""
    out = guard.after_tools([("add_workout", "Booked. Climb reps is on Monday 19 October.")])
    assert out == "I've booked Monday's session. "


def test_a_refused_change_never_tells_the_app_the_plan_moved(db_session, monkeypatch):
    user = _user(db_session)
    ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector")
    rounds = iter([
        SimpleNamespace(stop_reason="tool_use", usage=None, content=[
            SimpleNamespace(type="tool_use", id="t1", name="add_workout", input={
                "scheduled_date": _day(2), "title": "Spin", "workout_type": "recovery",
            }),
        ]),
        SimpleNamespace(stop_reason="end_turn", usage=None, content=[
            SimpleNamespace(type="text", text="That one can't go in while the hold is on."),
        ]),
    ])

    class _Stream:
        def __init__(self, final):
            self._final = final

        def __iter__(self):
            for block in self._final.content:
                if block.type == "text":
                    yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(text=block.text))

        def get_final_message(self):
            return self._final

    @contextmanager
    def fake_stream(**kwargs):
        yield _Stream(next(rounds))

    monkeypatch.setattr(forma_core, "stream", fake_stream)
    monkeypatch.setattr(forma_core, "call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(coach_service, "_relevant_memories", lambda *a, **k: None)
    monkeypatch.setattr(coach_service, "_dossier_block", lambda *a, **k: "")
    monkeypatch.setattr(safety_screen, "alert_founder", lambda *a, **k: None)
    session = ChatSession(user_id=user.id, title="Chat")
    db_session.add(session)
    db_session.commit()

    async def run():
        return [c async for c in coach_service.stream_response(db_session, user, session, "Add a spin")]

    chunks = [json.loads(c[len("data: "):]) for c in asyncio.run(run())]
    assert not any(c["type"] == "plan_updated" for c in chunks)
    assert db_session.query(Workout).count() == 0


# ── What the season keeper builds and the season note pitches ───────────────


def _past_goal(db, user, status) -> GoalEvent:
    goal = GoalEvent(
        user_id=user.id, event_name="Race A", event_date=TODAY - timedelta(days=2),
        event_type="road_race", priority=EventPriority.a_race, status=status,
    )
    db.add(goal)
    db.commit()
    return goal


@pytest.mark.parametrize("status", [GoalStatus.dnf, GoalStatus.dns])
def test_after_a_dnf_or_dns_the_season_note_pitches_no_new_goal(db_session, status):
    user = _user(db_session)
    _past_goal(db_session, user, status)
    state = season_service.season_state(db_session, user)
    in_chat = season_service.season_note(state)
    morning = season_service.season_note(state, in_chat=False)
    for note in (in_chat, morning):
        assert "Propose the next goal" not in note
        assert "do not propose a new goal" in note
    assert "how the race went" in morning and "how they are" in morning


def test_the_keeper_builds_nothing_for_an_account_held_as_under_18(db_session, monkeypatch):
    user = _user(db_session)
    _past_goal(db_session, user, GoalStatus.completed)
    db_session.add(TrainingPlan(
        user_id=user.id, name="Plan for Race A", start_date=TODAY - timedelta(days=60),
        end_date=TODAY - timedelta(days=2), status=PlanStatus.completed,
        periodization_model=PeriodizationModel.traditional,
    ))
    db_session.commit()
    monkeypatch.setattr("app.services.billing_service.has_access", lambda u: True)
    snap = season_service._read(db_session, user, TODAY)
    assert season_service._eligible(user, snap) is True

    ss.open_hold(db_session, user, "hold_all", "Says they are 15", "coach_tool", red_flag="minor")
    snap = season_service._read(db_session, user, TODAY)
    assert snap.minor_held and season_service._eligible(user, snap) is False
    assert season_service.season_state(db_session, user)["rebuilds"] is False
