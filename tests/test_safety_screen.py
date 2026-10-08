"""The red-flag check, what it does in the coach chat, the safety gate on the
plan tools and plan review, and the session-type fix.

The chat tests drive the real stream with a fake model, so the order of what
reaches the rider (card first, then words) is checked end to end.
"""

import asyncio
import functools
import inspect
import json
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.workout_templates import VO2MAX_5x5, get_template
from app.models.base import Base
from app.models.briefing import Briefing
from app.models.chat import ChatMessage, ChatRole, ChatSession
from app.models.plan_proposal import PlanProposal
from app.models.ride import Ride, RideSource
from app.models.safety import HealthScreening, SafetyEvent, SafetyHold
from app.models.training import Workout, WorkoutStatus, WorkoutStep
from app.models.user import User
from app.services import coach_service, plan_review_service, safety_screen
from app.services import safety_service as ss
from app.services.plan_service import _create_workout_steps
from app.services.safety_screen import CRISIS_CARD, EMERGENCY_CARD, detect_red_flags

TODAY = datetime.utcnow().date()


def kinds(text: str) -> dict:
    return {h["kind"]: h["severity"] for h in detect_red_flags(text)}


# ── Detection ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text, kind, severity", [
    ("I've had chest pain on the last two climbs", "chest_pain", "emergency"),
    ("my chest felt tight on the last interval", "chest_pain", "emergency"),
    ("there's a pressure in my chest when I sprint", "chest_pain", "emergency"),
    ("I think I'm having a heart attack", "chest_pain", "emergency"),
    ("my chest strap died and now I have chest pain", "chest_pain", "emergency"),
    ("my heart rate monitor says 190 and my chest hurts", "chest_pain", "emergency"),
    ("I keep getting palpitations at night", "palpitations", "urgent"),
    ("I fainted after the ride", "fainting", "emergency"),
    ("I nearly passed out on the turbo", "fainting", "urgent"),
    ("I collapsed at the top of the climb", "fainting", "emergency"),
    ("I went dizzy and nearly blacked out", "fainting", "urgent"),
    ("I felt faint on the last rep", "fainting", "urgent"),
    ("crashed and hit my head on the kerb", "head_injury", "urgent"),
    ("my helmet is cracked", "head_injury", "urgent"),
    ("I think I've got concussion", "head_injury", "urgent"),
    ("got knocked out in the crash", "head_injury", "urgent"),
    ("I've got a fever", "fever", "urgent"),
    ("running a temperature since Sunday", "fever", "urgent"),
    ("I've got the flu", "fever", "urgent"),
    ("I sprained my ankle", "injury", "info"),
    ("my knee hurts on long rides", "injury", "info"),
    ("sharp pain in my knee when I stand", "injury", "info"),
    ("I'm pregnant", "pregnancy", "info"),
    ("I had a baby in March", "pregnancy", "info"),
    ("I'm on bisoprolol", "medication", "info"),
    ("I take insulin for my type 1", "medication", "info"),
    ("I'm on blood thinners", "medication", "info"),
    ("I have type 1 diabetes", "condition", "info"),
    ("I'm eating 1200 calories a day", "restriction", "info"),
    ("I've missed my period for three months", "restriction", "info"),
    ("I'm 15", "minor", "minor"),
    ("i am 14 years old", "minor", "minor"),
    ("I'm only 13, is that ok?", "minor", "minor"),
    ("I'm doing my GCSEs this year", "minor", "minor"),
    ("I want to kill myself", "crisis", "crisis"),
    ("honestly I don't want to be here anymore", "crisis", "crisis"),
    ("everyone would be better off without me", "crisis", "crisis"),
    ("there's no point in anything", "crisis", "crisis"),
    ("I'm worthless", "crisis", "crisis"),
    ("I've been self-harming again", "crisis", "crisis"),
    ("I want to end it all", "crisis", "crisis"),
    ("I want to kill myself and the bike can't fix it", "crisis", "crisis"),
])
def test_red_flags_are_caught(text, kind, severity):
    assert kinds(text).get(kind) == severity


@pytest.mark.parametrize("text, not_kind", [
    # The idioms riders use every day.
    ("my chest strap died halfway through", "chest_pain"),
    ("my heart rate monitor keeps dropping out", "chest_pain"),
    ("my chest is sore from the gym", "chest_pain"),
    ("chest is a bit sore after chest day", "chest_pain"),
    ("the strap feels tight across my chest", "chest_pain"),
    ("No chest pain, just tired legs", "chest_pain"),
    ("my dad had chest pain last year", "chest_pain"),
    ("that climb nearly gave me a heart attack", "chest_pain"),
    ("my heart was racing on the climb", "palpitations"),
    ("I passed out on the sofa after the ride", "fainting"),
    ("my power collapsed in the last hour", "fainting"),
    ("knocked out of the race in the first round", "head_injury"),
    ("cabin fever is real this winter", "fever"),
    ("had my flu jab yesterday", "fever"),
    ("since covid I've ridden more", "fever"),
    ("high temperature today, 32C", "fever"),
    ("the temperature is 38 degrees outside", "fever"),
    ("how do I avoid an injury?", "injury"),
    ("I've been injury free all year", "injury"),
    ("my legs hurt after yesterday", "injury"),
    ("my mate's knee injury", "injury"),
    ("my wife is pregnant", "pregnancy"),
    ("we're trying for a baby", "pregnancy"),
    ("carbs cause an insulin spike", "medication"),
    ("my dad is on warfarin", "medication"),
    ("I burned 1200 calories on the ride", "restriction"),
    ("a gel has 100 calories", "restriction"),
    ("binge watching the Tour", "restriction"),
    ("I'm 15 minutes late for the ride", "minor"),
    ("I'm 16 stone and want to lose a bit", "minor"),
    ("I'm 17th on the leaderboard", "minor"),
    ("I'm 12 weeks into the plan", "minor"),
    ("I'm 15 years into cycling", "minor"),
    ("my son is 15", "minor"),
    ("I killed myself on that last climb", "crisis"),
    ("I'm killing myself on these intervals", "crisis"),
    ("I want to die on this climb", "crisis"),
    ("cutting myself some slack this week", "crisis"),
    ("that was suicidal pace", "crisis"),
    ("my legs feel worthless today", "crisis"),
    ("I overdosed on caffeine before the TT", "crisis"),
    ("I don't want to be here at work", "crisis"),
])
def test_everyday_idioms_are_left_alone(text, not_kind):
    assert not_kind not in kinds(text)


# ── Review findings 2, 12 and 14: what the check missed, and what it misread ──


@pytest.mark.parametrize("text, kind, severity", [
    # "Never had ... like this before" is chest pain now, not a denial.
    ("I've never had chest pain like this before", "chest_pain", "emergency"),
    ("No chest pain until the last climb, then it hit hard", "chest_pain", "emergency"),
    ("I didn't have chest pain before but now I do", "chest_pain", "emergency"),
    ("I haven't had pain in my chest like this before", "chest_pain", "emergency"),
    # The chest, said the way riders say it.
    ("my chest was hurting", "chest_pain", "emergency"),
    ("my chest has been tight since the climb", "chest_pain", "emergency"),
    ("chest got really tight on the last rep", "chest_pain", "emergency"),
    ("my chest is killing me", "chest_pain", "emergency"),
    ("crushing feeling in my chest", "chest_pain", "emergency"),
    ("pain in the centre of my chest", "chest_pain", "emergency"),
    ("my heart hurts", "chest_pain", "emergency"),
    ("short of breath at rest", "chest_pain", "emergency"),
    ("I get breathless even at rest", "chest_pain", "emergency"),
    ("I thought I was going to faint", "fainting", "urgent"),
    ("everything went black for a second", "fainting", "urgent"),
    ("my heart was racing and skipping beats", "palpitations", "urgent"),
    # A bare possessive is the rider's own session, not someone else.
    ("In this morning's session I fainted", "fainting", "emergency"),
    ("During Saturday's race I blacked out", "fainting", "emergency"),
    # The rider speaking after someone else is named.
    ("my wife says I fainted", "fainting", "emergency"),
    ("she said I passed out at the lights", "fainting", "emergency"),
    ("I told her my chest hurts", "chest_pain", "emergency"),
    ("my dad says I have a heart condition", "condition", "info"),
    ("my mum thinks I'm 15 and too young to race", "minor", "minor"),
    # Under 18, said without "I'm 15".
    ("15 year old here, want to get into racing", "minor", "minor"),
    ("I'm a 16 year old cyclist", "minor", "minor"),
    ("16yo here", "minor", "minor"),
    ("age 15", "minor", "minor"),
    ("I'm aged 15", "minor", "minor"),
    ("I'm in year 12", "minor", "minor"),
    ("I'm in sixth form", "minor", "minor"),
    ("I go to sixth form", "minor", "minor"),
    # Crisis, said plainly.
    ("I can't go on", "crisis", "crisis"),
    ("I can't go on like this", "crisis", "crisis"),
    ("nobody would miss me", "crisis", "crisis"),
    ("no one would even miss me", "crisis", "crisis"),
    ("I hate myself", "crisis", "crisis"),
    ("I don't see the point.", "crisis", "crisis"),
    ("honestly I don't see the point", "crisis", "crisis"),
    # Still an injury: a hallmark of one, or a joint.
    ("sharp pain in my quads on the last rep", "injury", "info"),
    ("searing pain in my knee on the last rep", "injury", "info"),
    ("searing pain in my quads that has lasted three days", "injury", "info"),
])
def test_the_phrasings_the_review_found_missing_are_caught(text, kind, severity):
    assert kinds(text).get(kind) == severity


@pytest.mark.parametrize("text, not_kind", [
    # A plain denial directly before the words.
    ("I didn't hit my head", "head_injury"),
    ("crashed but didn't hit my head", "head_injury"),
    ("I crashed but did not hit my head", "head_injury"),
    ("I wasn't knocked out", "head_injury"),
    ("I'm not short of breath at rest", "chest_pain"),
    ("no chest pain at all, just tired legs", "chest_pain"),
    ("without any chest pain", "chest_pain"),
    # Cycling talk.
    ("suicide intervals are brutal", "crisis"),
    ("we did suicide sprints at club", "crisis"),
    ("suicide drills on the track tonight", "crisis"),
    ("suicide efforts on the turbo", "crisis"),
    ("there was a pregnant pause", "pregnancy"),
    ("I'm diabetic about my power numbers", "condition"),
    ("searing pain in my quads on the last rep", "injury"),
    ("burning pain in my legs on the final sprint", "injury"),
    # Kit, not a body (red-team finding 17).
    ("My chest strap died halfway through the ride so the heart rate data is rubbish. "
     "Can you still read the session?", "chest_pain"),
    ("my HR strap dropped out", "chest_pain"),
    ("the HRM battery died", "chest_pain"),
    ("my Garmin died and the heart rate data dropped out", "chest_pain"),
    ("my chest strap was tight", "chest_pain"),
    ("my chest strap keeps dropping out", "chest_pain"),
    ("my chest strap felt tight across my chest", "chest_pain"),
    ("the strap is skipping beats", "palpitations"),
    ("my heart rate was skipping all over the place", "palpitations"),
    ("my heart rate data looks irregular", "palpitations"),
    ("my Garmin screen blacked out", "fainting"),
    ("my head unit went black mid ride", "fainting"),
    ("my wahoo is going to die", "crisis"),
    ("the HRM is going to die before the end", "crisis"),
    ("my garmin's going to die", "crisis"),
    ("my phone is going to die", "crisis"),
    ("my lights are going to die", "crisis"),
    ("I'm going to pass out on the sofa", "fainting"),
    # Someone else's age, a category, a past age, a bike.
    ("my 15 year old son wants to ride", "minor"),
    ("coaching a 16 year old", "minor"),
    ("the age 15 category", "minor"),
    ("I started racing at age 15", "minor"),
    ("my bike is 16 years old", "minor"),
    ("a 16 year old frame", "minor"),
    ("back in my sixth form days", "minor"),
    # Words that only sound like a crisis.
    ("I can't go on the club run on Saturday", "crisis"),
    ("I can't go on any more rides this week", "crisis"),
    ("nobody would miss me on the club run if I skipped it", "crisis"),
])
def test_the_false_alarms_the_review_found_are_left_alone(text, not_kind):
    assert not_kind not in kinds(text)


def test_breathless_at_rest_brings_the_chest_card_and_a_full_hold(db_session):
    user = _user(db_session, country="GB")
    result = safety_screen.screen_message(db_session, user, "I'm short of breath at rest today")
    assert result.card_names == ["chest"]
    assert "short of breath at rest, call 999 now" in result.cards[0][1]
    hold = ss.current_hold(db_session, user.id)
    assert (hold.level, hold.red_flag) == ("hold_all", "chest_pain")
    assert "short of breath at rest counts the same as chest pain" in result.context_line


def test_a_named_persons_possessive_now_counts_as_the_rider():
    # The bare possessive rule is gone: "Tom's" can't be told apart from
    # "this morning's", and a missed symptom costs more than a false alarm.
    assert "injury" in kinds("Tom's knee injury")
    # A named relation still is someone else.
    assert "injury" not in kinds("my brother's knee injury")


def test_kit_words_never_hide_words_of_crisis():
    assert kinds("my garmin died and honestly I want to die")["crisis"] == "crisis"
    assert kinds("the strap dropped out. I want to end it all")["crisis"] == "crisis"


def test_a_strap_that_died_and_a_real_chest_pain_in_one_message_still_fires():
    assert kinds("my chest strap died and my chest was hurting")["chest_pain"] == "emergency"
    assert kinds("the strap felt tight, but my chest hurts")["chest_pain"] == "emergency"


def test_a_message_with_nothing_in_it_has_no_hits():
    assert detect_red_flags("Great ride today, legs felt strong on the climbs") == []
    assert detect_red_flags("") == []


def test_one_hit_per_kind_and_curly_apostrophes_count():
    hits = detect_red_flags("I’m 15 and I’m 15 and my chest hurts and chest pain")
    assert sorted(h["kind"] for h in hits) == ["chest_pain", "minor"]


# ── Fixtures ────────────────────────────────────────────────────────────────


def _user(db, email="rider@example.com", **kw) -> User:
    kw.setdefault("ftp", 250)
    user = User(email=email, hashed_password="x", full_name="Sam Rider", **kw)
    db.add(user)
    db.commit()
    return user


def _workout(db, user, template=VO2MAX_5x5, day=None, status=WorkoutStatus.planned) -> Workout:
    w = Workout(
        user_id=user.id,
        scheduled_date=day or TODAY + timedelta(days=2),
        title=template["name"],
        description=template["description"],
        workout_type=template["workout_type"],
        planned_duration_seconds=template["duration_seconds"],
        planned_tss=70,
        status=status,
    )
    db.add(w)
    db.flush()
    _create_workout_steps(db, w, template)
    db.commit()
    db.refresh(w)
    return w


def _steps(db, workout) -> list[WorkoutStep]:
    return (
        db.query(WorkoutStep)
        .filter(WorkoutStep.workout_id == workout.id)
        .order_by(WorkoutStep.step_order)
        .all()
    )


def _screening(db, user, tier="easy_only", answers=None, cleared=False, limits=None):
    s = HealthScreening(
        user_id=user.id, version=ss.SCREENING_VERSION,
        answers=answers or {"q1": True}, long_break=False, any_yes=tier != "none",
        tier=tier, created_at=datetime.utcnow(),
        clearance_confirmed_at=datetime.utcnow() if cleared else None,
        clearance_by="Dr Patel" if cleared else None, clearance_limits=limits,
    )
    db.add(s)
    db.commit()
    return s


@pytest.fixture
def no_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(
        safety_screen, "alert_founder",
        lambda kind, user, excerpt, event_ids=None: sent.append((kind, excerpt, event_ids)),
    )
    return sent


# ── screen_message: holds, events, cards, context ──────────────────────────


def test_chest_pain_opens_hold_all_shows_the_card_and_records_it(db_session):
    user = _user(db_session)
    result = safety_screen.screen_message(
        db_session, user, "I had chest pain on the climb", message_id="m1"
    )
    assert result.cards == [("emergency", EMERGENCY_CARD)]
    hold = ss.current_hold(db_session, user.id)
    assert (hold.level, hold.source, hold.red_flag) == ("hold_all", "detector", "chest_pain")
    event = db_session.query(SafetyEvent).one()
    assert (event.kind, event.card_shown, event.hold_id, event.message_id) == (
        "chest_pain", "emergency", hold.id, "m1"
    )
    assert "SAFETY CONTEXT" in result.context_line
    assert "hold_all hold opened" in result.context_line
    assert "SAFETY LAW 2a" in result.context_line
    assert result.alerts == []


@pytest.mark.parametrize("text, level, cards", [
    ("I fainted on Sunday", "hold_all", ["faint"]),
    ("crashed and hit my head", "hold_all", ["head"]),
    ("I've got a fever", "hold_all", ["fever"]),
    ("I'm 20 weeks pregnant", "easy_only", []),
    ("I've just started bisoprolol", "easy_only", []),
    ("I sprained my ankle", "easy_only", []),
    ("I'm 15 and want a race plan", "hold_all", []),
    ("I haven't ridden for six months", "easy_only", []),
])
def test_each_red_flag_opens_its_hold(db_session, text, level, cards):
    user = _user(db_session)
    result = safety_screen.screen_message(db_session, user, text)
    assert ss.current_hold(db_session, user.id).level == level
    assert result.card_names == cards


def test_given_hits_are_acted_on_in_place_of_the_regexs_own(db_session):
    # The chat passes the regex's hits merged with the classifier's verdict;
    # screen_message acts on those, not on a fresh read of the words.
    oblique = "an elephant sat on me all ride"
    assert safety_screen._detect(oblique) == []
    user = _user(db_session)
    added = safety_screen.Hit("chest_pain", "an elephant sat on me", "emergency")
    result = safety_screen.screen_message(db_session, user, oblique, hits=[added])
    assert result.card_names == ["chest"]
    assert ss.current_hold(db_session, user.id).red_flag == "chest_pain"
    assert [e.kind for e in db_session.query(SafetyEvent)] == ["chest_pain"]

    # A regex hit the merge set aside is not acted on.
    fever = "I have got a fever of 39"
    assert [h.kind for h in safety_screen._detect(fever)] == ["fever"]
    other = _user(db_session, email="other@example.com")
    quiet = safety_screen.screen_message(db_session, other, fever, hits=[])
    assert quiet.hits == [] and quiet.cards == []
    assert ss.current_hold(db_session, other.id) is None


def test_crisis_shows_the_card_alerts_once_and_opens_no_hold(db_session):
    user = _user(db_session)
    first = safety_screen.screen_message(db_session, user, "I want to end it all")
    assert first.cards == [("crisis", CRISIS_CARD)]
    assert first.alerts == ["crisis"]
    assert ss.current_hold(db_session, user.id) is None
    assert "follow SAFETY LAW rule 4" in first.context_line.replace("Follow", "follow")
    # The same incident a minute later: still a card, not a second email.
    again = safety_screen.screen_message(db_session, user, "there's no point in anything")
    assert again.cards == [("crisis", CRISIS_CARD)]
    assert again.alerts == []


def test_an_under_18_hit_holds_the_account_and_alerts_gareth_without_a_card(db_session):
    user = _user(db_session)
    result = safety_screen.screen_message(db_session, user, "I'm 15, can I use Forma?")
    assert result.alerts == ["minor"]
    assert result.cards == []
    assert "SAFETY LAW 2n" in result.context_line
    # The detector holds the account itself: the coach saying so is then true.
    hold = ss.current_hold(db_session, user.id)
    assert (hold.level, hold.source, hold.red_flag) == ("hold_all", "detector", "minor")
    assert "can't lift" in result.context_line
    assert "Never mention a flag, a review, Gareth or anyone at Forma" in result.context_line


def test_both_cards_emergency_first(db_session):
    user = _user(db_session)
    result = safety_screen.screen_message(
        db_session, user, "My chest hurts. I don't want to be here anymore."
    )
    assert [k for k, _ in result.cards] == ["emergency", "crisis"]
    assert result.card_chunks()[0] == {
        "type": "safety", "kind": "emergency", "card": "chest", "text": EMERGENCY_CARD,
    }


def test_a_second_mention_does_not_stack_holds(db_session):
    user = _user(db_session)
    safety_screen.screen_message(db_session, user, "chest pain again")
    result = safety_screen.screen_message(db_session, user, "the chest pain is back")
    assert db_session.query(SafetyHold).count() == 1
    assert "a hold_all hold was already open" in result.context_line
    assert db_session.query(SafetyEvent).count() == 2


def test_a_hold_marked_a_mistake_is_not_reopened_by_the_same_words(db_session):
    user = _user(db_session)
    safety_screen.screen_message(db_session, user, "my knee hurts on the climbs")
    hold = ss.current_hold(db_session, user.id)
    ss.lift_hold(db_session, user, hold.id, "mistake")
    result = safety_screen.screen_message(db_session, user, "my knee hurts on the climbs")
    assert ss.current_hold(db_session, user.id) is None
    assert "marked an earlier one for this a mistake" in result.context_line


@pytest.mark.parametrize("text", [
    "my chest felt tight",
    "I nearly fainted on the turbo",
    "I've just started bisoprolol",
    "I'm 16",
])
def test_a_mistake_never_quiets_a_heart_symptom_a_medicine_or_a_child(db_session, text):
    # A real one must never be waved through by an old tap on "mistake".
    user = _user(db_session)
    safety_screen.screen_message(db_session, user, text)
    hold = ss.current_hold(db_session, user.id)
    try:
        ss.lift_hold(db_session, user, hold.id, "mistake")
    except getattr(ss, "HoldNotLiftable", ()):
        pass  # an under-18 hold can't be marked a mistake at all
    safety_screen.screen_message(db_session, user, text)
    assert ss.current_hold(db_session, user.id) is not None


# ── Review finding 3: a clearance covers the one thing it was for ──────────


def _clear(db, user, by="My GP", days_ago=0):
    ss.confirm_clearance(db, user, by, None)
    if days_ago:
        for hold in db.query(SafetyHold).filter(SafetyHold.lifted_how == "clearance"):
            hold.lifted_at = datetime.utcnow() - timedelta(days=days_ago)
        db.commit()


def test_a_screening_clearance_alone_never_quiets_a_new_mention(db_session):
    # The review's probe: Q5 yes (asthma), cleared, then a new heart condition.
    user = _user(db_session)
    _screening(db_session, user, answers={"q5": True}, cleared=True)
    result = safety_screen.screen_message(
        db_session, user, "I've been diagnosed with cardiomyopathy"
    )
    hold = ss.current_hold(db_session, user.id)
    assert (hold.level, hold.red_flag) == ("easy_only", "condition")
    assert "clearance" not in result.context_line.split("This reply must")[0]
    # A Q5 clearance never covers a heart medicine either.
    other = _user(db_session, email="q5@example.com")
    _screening(db_session, other, answers={"q5": True}, cleared=True)
    safety_screen.screen_message(db_session, other, "I'm on bisoprolol")
    assert ss.current_hold(db_session, other.id).red_flag == "medication"


def test_a_clearance_for_one_condition_never_covers_a_heart_condition(db_session):
    user = _user(db_session)
    safety_screen.screen_message(db_session, user, "I have asthma")
    _clear(db_session, user)
    assert ss.current_hold(db_session, user.id) is None
    result = safety_screen.screen_message(db_session, user, "I have asthma, it's fine")
    assert ss.current_hold(db_session, user.id) is None
    assert "confirmed a doctor's clearance for asthma on" in result.context_line
    safety_screen.screen_message(db_session, user, "I was diagnosed with cardiomyopathy")
    hold = ss.current_hold(db_session, user.id)
    assert (hold.level, hold.red_flag) == ("easy_only", "condition")


def test_a_clearance_for_one_medicine_never_covers_another(db_session):
    user = _user(db_session)
    safety_screen.screen_message(db_session, user, "I'm on bisoprolol")
    _clear(db_session, user)
    safety_screen.screen_message(db_session, user, "the bisoprolol makes me tired")
    assert ss.current_hold(db_session, user.id) is None
    for text in ("I've just started warfarin", "my doctor put me on a beta blocker"):
        safety_screen.screen_message(db_session, user, text)
        hold = ss.current_hold(db_session, user.id)
        assert hold is not None and hold.red_flag == "medication", text
        _clear(db_session, user)


def test_a_cleared_pregnancy_never_covers_the_weeks_after_the_birth(db_session):
    user = _user(db_session)
    safety_screen.screen_message(db_session, user, "I'm 12 weeks pregnant")
    _clear(db_session, user, by="My midwife")
    safety_screen.screen_message(db_session, user, "still pregnant, all going well")
    assert ss.current_hold(db_session, user.id) is None
    safety_screen.screen_message(db_session, user, "I had my baby three weeks ago")
    assert ss.current_hold(db_session, user.id).red_flag == "pregnancy"


def test_a_clearance_expires_after_twelve_months(db_session):
    user = _user(db_session)
    safety_screen.screen_message(db_session, user, "I'm pregnant")
    _clear(db_session, user, by="My midwife", days_ago=safety_screen.CLEARANCE_COVERS_DAYS + 30)
    safety_screen.screen_message(db_session, user, "I'm pregnant again")
    assert ss.current_hold(db_session, user.id).red_flag == "pregnancy"

    other = _user(db_session, email="year@example.com")
    safety_screen.screen_message(db_session, other, "I'm on bisoprolol")
    _clear(db_session, other, days_ago=safety_screen.CLEARANCE_COVERS_DAYS - 30)
    safety_screen.screen_message(db_session, other, "I'm on bisoprolol")
    assert ss.current_hold(db_session, other.id) is None


def test_a_mistake_is_never_a_clearance(db_session):
    user = _user(db_session)
    safety_screen.screen_message(db_session, user, "I'm on bisoprolol")
    hold = ss.current_hold(db_session, user.id)
    ss.lift_hold(db_session, user, hold.id, "mistake")
    safety_screen.screen_message(db_session, user, "I'm on bisoprolol")
    assert ss.current_hold(db_session, user.id) is not None


@pytest.mark.parametrize("kind, matched, term", [
    ("medication", "bisoprolol", "bisoprolol"),
    ("medication", "beta-blockers", "beta blocker"),
    ("medication", "eliquis", "apixaban"),
    ("condition", "i'm a type 1 diabetic", "type 1 diabetes"),
    ("condition", "i have afib", "atrial fibrillation"),
    ("condition", "i'm asthmatic", "asthma"),
    ("pregnancy", "pregnant", "pregnancy"),
    ("pregnancy", "i had a baby", "after a birth"),
    ("medication", "something else", None),
])
def test_each_standing_hit_names_one_term(kind, matched, term):
    assert safety_screen.standing_term(kind, matched) == term


def test_a_clearance_for_one_thing_does_not_cover_another(db_session):
    user = _user(db_session)
    _screening(db_session, user, answers={"q1": True}, cleared=True)
    safety_screen.screen_message(db_session, user, "I'm 12 weeks pregnant")
    hold = ss.current_hold(db_session, user.id)
    assert (hold.level, hold.red_flag) == ("easy_only", "pregnancy")


def test_a_chat_hold_lifted_by_clearance_is_not_reopened_by_the_same_medicine(db_session):
    user = _user(db_session)
    safety_screen.screen_message(db_session, user, "I'm on bisoprolol")
    ss.confirm_clearance(db_session, user, "My GP", None)
    safety_screen.screen_message(db_session, user, "the bisoprolol makes me tired")
    assert ss.current_hold(db_session, user.id) is None


def test_the_cards_survive_a_database_failure(db_session, monkeypatch):
    user = _user(db_session)

    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(ss, "open_hold", boom)
    result = safety_screen.screen_message(db_session, user, "I think I'm having a heart attack")
    assert result.cards == [("emergency", EMERGENCY_CARD)]
    assert result.context_line
    assert result.event_ids == {}


# ── Telling Gareth ──────────────────────────────────────────────────────────


def _factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_the_alert_email_stamps_founder_alerted_at(monkeypatch):
    factory = _factory()
    db = factory()
    event = SafetyEvent(user_id="u1", kind="crisis", source="chat", matched="end it all")
    db.add(event)
    db.commit()
    sent = []

    async def fake_send(kind, email, user_id, excerpt):
        sent.append((kind, email, user_id, excerpt))
        return True

    monkeypatch.setattr("app.services.email_service.send_safety_alert", fake_send)
    ok = asyncio.run(safety_screen._alert(
        "crisis", "sam@example.com", "u1", "I want to end it all", [event.id], factory
    ))
    assert ok and sent == [("crisis", "sam@example.com", "u1", "I want to end it all")]
    db.expire_all()
    assert db.get(SafetyEvent, event.id).founder_alerted_at is not None
    db.close()


def test_a_failed_alert_never_raises_and_stamps_nothing(monkeypatch):
    factory = _factory()

    async def failing_send(*a):
        raise RuntimeError("postmark down")

    monkeypatch.setattr("app.services.email_service.send_safety_alert", failing_send)
    assert asyncio.run(safety_screen._alert("minor", "a@b.c", "u1", "x", ["e1"], factory)) is False


def test_alert_founder_never_makes_the_reply_wait(monkeypatch):
    started = []

    async def slow_send(*a):
        started.append(True)
        await asyncio.sleep(0.05)
        return True

    monkeypatch.setattr("app.services.email_service.send_safety_alert", slow_send)
    user = User(id="u1", email="sam@example.com", hashed_password="x")

    async def reply():
        safety_screen.alert_founder("crisis", user, "excerpt")
        assert started == []  # scheduled, not awaited
        await asyncio.sleep(0.1)
        return started

    assert asyncio.run(reply()) == [True]


# ── The coach chat: card first, then words ──────────────────────────────────


class _Delta:
    def __init__(self, text):
        self.text = text


class _Event:
    type = "content_block_delta"

    def __init__(self, text):
        self.delta = _Delta(text)


class _Usage:
    input_tokens = 10
    output_tokens = 5


class _Final:
    content = []
    stop_reason = "end_turn"
    usage = _Usage()


class _FakeStream:
    def __init__(self, texts):
        self.texts = texts

    def __iter__(self):
        return iter(_Event(t) for t in self.texts)

    def get_final_message(self):
        return _Final()


@pytest.fixture
def fake_model(monkeypatch):
    """The coach's model, replaced: records the system it was given and
    streams a fixed reply."""
    calls = []

    @contextmanager
    def fake_stream(**kwargs):
        calls.append(kwargs)
        yield _FakeStream(["Stop riding for now. ", "Is it happening right now?"])

    monkeypatch.setattr(coach_service.forma_core, "stream", fake_stream)
    monkeypatch.setattr(coach_service, "maybe_autotitle_session", lambda *a, **k: None)
    monkeypatch.setattr(coach_service, "_relevant_memories", lambda *a, **k: None)
    monkeypatch.setattr("app.services.memory_service.extract_memories", lambda *a, **k: None)
    monkeypatch.setattr("app.services.voice_service.is_voice_enabled", lambda: False)
    return calls


def _session(db, user) -> ChatSession:
    s = ChatSession(user_id=user.id, title="Chat - 8 Oct 2026")
    db.add(s)
    db.commit()
    return s


def _run(agen) -> list[dict]:
    async def collect():
        return [chunk async for chunk in agen]

    return [json.loads(c[len("data: "):]) for c in asyncio.run(collect())]


def test_the_emergency_card_reaches_the_rider_before_any_model_text(
    db_session, fake_model, no_alerts
):
    user = _user(db_session)
    session = _session(db_session, user)
    chunks = _run(coach_service.stream_response(
        db_session, user, session, "I've got chest pain on the bike"
    ))
    assert chunks[0] == {
        "type": "safety", "kind": "emergency", "card": "chest", "text": EMERGENCY_CARD,
    }
    first_text = next(i for i, c in enumerate(chunks) if c["type"] == "text")
    assert first_text > 0
    assert chunks[-1] == {"type": "done"}

    # The hold is open, the event recorded against the rider's message.
    assert ss.current_hold(db_session, user.id).level == "hold_all"
    user_msg = db_session.query(ChatMessage).filter(ChatMessage.role == ChatRole.user).one()
    assert db_session.query(SafetyEvent).one().message_id == user_msg.id

    # The model was told, in the uncached per-turn block, after the law.
    system = fake_model[0]["system"]
    assert "SAFETY CONTEXT" in system[-1]["text"]
    assert "cache_control" not in system[-1]
    # And the rider context it read already carries the hold.
    assert '"SAFETY HOLD": "hold_all' in system[1]["text"]
    assert no_alerts == []


def test_crisis_words_bring_the_crisis_card_and_alert_gareth(db_session, fake_model, no_alerts):
    user = _user(db_session)
    session = _session(db_session, user)
    chunks = _run(coach_service.stream_response(
        db_session, user, session, "I don't want to be here anymore"
    ))
    assert chunks[0] == {"type": "safety", "kind": "crisis", "card": "crisis", "text": CRISIS_CARD}
    assert [k for k, _, _ in no_alerts] == ["crisis"]
    event = db_session.query(SafetyEvent).one()
    assert no_alerts[0][2] == [event.id]


def test_the_voice_path_sends_the_card_first_too(db_session, fake_model, no_alerts):
    user = _user(db_session)
    session = _session(db_session, user)
    chunks = _run(coach_service.stream_voice_response(
        db_session, user, session, "I'm 15 and I want to kill myself"
    ))
    assert chunks[0]["type"] == "safety" and chunks[0]["kind"] == "crisis"
    assert sorted(k for k, _, _ in no_alerts) == ["crisis", "minor"]


def test_an_ordinary_message_gets_no_card(db_session, fake_model, no_alerts):
    user = _user(db_session)
    session = _session(db_session, user)
    chunks = _run(coach_service.stream_response(
        db_session, user, session, "my chest strap died halfway through"
    ))
    assert all(c["type"] != "safety" for c in chunks)
    assert db_session.query(SafetyEvent).count() == 0
    assert "SAFETY CONTEXT" not in (fake_model[0]["system"][-1]["text"])


def test_the_first_ever_reply_says_it_is_ai_and_only_the_first(db_session, fake_model, no_alerts):
    user = _user(db_session)
    session = _session(db_session, user)
    first = _run(coach_service.stream_response(db_session, user, session, "Hi coach"))
    texts = [c["content"] for c in first if c["type"] == "text"]
    assert texts[0] == "I'm Forma, your AI coach. "
    assert "first ever reply" in fake_model[0]["system"][-1]["text"]
    saved = db_session.query(ChatMessage).filter(ChatMessage.role == ChatRole.assistant).one()
    assert saved.content.startswith("I'm Forma, your AI coach. Stop riding")

    second = _run(coach_service.stream_response(db_session, user, session, "And another"))
    assert "AI coach" not in "".join(c.get("content", "") for c in second if c["type"] == "text")


def test_a_renamed_coach_still_says_it_is_ai(db_session):
    user = _user(db_session, coach_name="Marco")
    assert coach_service._first_reply_intro(db_session, user) == "I'm Marco, your AI coach in Forma."


def test_the_non_streaming_reply_carries_the_card_at_the_top(db_session, monkeypatch, no_alerts):
    user = _user(db_session)
    session = _session(db_session, user)

    class _Resp:
        usage = _Usage()

    monkeypatch.setattr(coach_service.forma_core, "call", lambda **kw: _Resp())
    monkeypatch.setattr(coach_service, "response_text", lambda r: "Stop now.")
    monkeypatch.setattr("app.services.memory_service.extract_memories", lambda *a, **k: None)
    reply = coach_service.get_non_streaming_response(
        db_session, user, session, "I think I'm having a heart attack"
    )
    assert reply.startswith(EMERGENCY_CARD + "\n\nI'm Forma, your AI coach. Stop now.")


# ── The rider context the coach reads ───────────────────────────────────────


def test_the_rider_context_carries_the_safety_picture(db_session):
    user = _user(db_session, country="IE")
    _screening(db_session, user, answers={"q1": True, "q5": True}, limits=None)
    ss.open_hold(db_session, user, "easy_only", "Heart condition on screening", "screening")
    context = json.loads(coach_service._build_rider_context(db_session, user))
    safety = context["safety"]
    assert safety["allowed_intensity"] == "easy"
    assert safety["country"] == "IE" and safety["triage_region"] == "EU"
    assert safety["SAFETY HOLD"].startswith("easy_only since")
    assert "yes to a heart condition or high blood pressure; a long-term condition" in safety["health_screen"]
    assert "Clearance: not confirmed" in safety["health_screen"]
    assert list(context)[:2] == ["profile", "safety"]


def test_doctor_limits_and_the_layoff_gate_reach_the_coach(db_session):
    user = _user(db_session)
    _screening(db_session, user, cleared=True, limits="No efforts above threshold")
    db_session.add(Ride(
        user_id=user.id, source=RideSource.manual,
        ride_date=datetime.combine(TODAY - timedelta(days=40), time(9)),
    ))
    db_session.commit()
    safety = safety_screen.coach_safety_context(db_session, user)
    assert safety["doctor_limits"].startswith("No efforts above threshold")
    assert "layoff_gate" in safety
    assert "confirmed on" in safety["health_screen"] and "Dr Patel" in safety["health_screen"]


def test_an_unknown_country_gets_the_uk_rung():
    assert safety_screen.triage_region(None) == "unknown"
    assert safety_screen.triage_region("gb") == "UK"
    assert safety_screen.triage_region("US") == "US"
    assert safety_screen.triage_region("FR") == "EU"


# ── The session-type bug ────────────────────────────────────────────────────


def test_a_vo2_session_changed_to_recovery_in_chat_loses_its_vo2_steps(db_session):
    user = _user(db_session)
    workout = _workout(db_session, user)
    assert ss.max_step_pct(workout) == pytest.approx(1.20)

    result = coach_service._execute_tool(db_session, user, "update_workout", {
        "workout_id": workout.id, "workout_type": "recovery",
    })
    db_session.expire_all()
    workout = db_session.get(Workout, workout.id)
    steps = _steps(db_session, workout)

    recovery = get_template("recovery")
    assert [s.power_target_pct for s in steps] == [s["power_target_pct"] for s in recovery["steps"]]
    assert all(
        (s.power_target_pct or 0) <= ss.INTENSITY_CEILING["recovery"] for s in steps
    )
    assert not ss.workout_exceeds_ceiling(workout)
    assert workout.title == recovery["name"]
    assert workout.planned_duration_seconds == recovery["duration_seconds"]
    assert workout.planned_if == recovery["planned_if"]
    assert "steps were rebuilt" in result


def test_a_vo2_session_changed_by_an_accepted_proposal_loses_its_vo2_steps(db_session):
    user = _user(db_session)
    workout = _workout(db_session, user)
    proposal = PlanProposal(
        user_id=user.id, trigger="manual", observation="o", rationale="r", status="pending",
        changes=[{
            "action": "update_workout", "workout_id": workout.id,
            "workout_type": "endurance", "title": "Steady hour", "why": "absorb the block",
        }],
    )
    db_session.add(proposal)
    db_session.commit()

    assert plan_review_service.apply_proposal(db_session, user, proposal) == 1
    db_session.expire_all()
    workout = db_session.get(Workout, workout.id)
    assert workout.title == "Steady hour"
    assert ss.max_step_pct(workout) <= ss.INTENSITY_CEILING["endurance"]
    assert not any(
        s.step_type == "interval_on" or getattr(s.step_type, "value", None) == "interval_on"
        for s in _steps(db_session, workout)
    )


def test_a_session_changed_to_rest_has_nothing_to_ride(db_session):
    user = _user(db_session)
    workout = _workout(db_session, user)
    coach_service._execute_tool(db_session, user, "update_workout", {
        "workout_id": workout.id, "workout_type": "rest",
    })
    db_session.expire_all()
    workout = db_session.get(Workout, workout.id)
    assert _steps(db_session, workout) == []
    assert workout.title == "Rest day" and workout.planned_tss == 0.0


def test_a_session_added_in_chat_gets_real_steps(db_session):
    user = _user(db_session)
    result = coach_service._execute_tool(db_session, user, "add_workout", {
        "scheduled_date": (TODAY + timedelta(days=3)).isoformat(),
        "title": "Threshold builder", "workout_type": "threshold",
    })
    workout = db_session.query(Workout).one()
    steps = _steps(db_session, workout)
    assert steps and max(s.power_target_pct or 0 for s in steps) <= ss.INTENSITY_CEILING["threshold"]
    assert "Its steps come from" in result


# ── The safety gate on the plan tools ───────────────────────────────────────


def test_under_easy_only_chat_cannot_add_a_hard_session(db_session):
    user = _user(db_session)
    ss.open_hold(db_session, user, "easy_only", "Knee injury in chat", "detector")
    day = (TODAY + timedelta(days=1)).isoformat()
    refused = coach_service._execute_tool(db_session, user, "add_workout", {
        "scheduled_date": day, "title": "VO2", "workout_type": "vo2max",
    })
    assert refused.startswith("Not changed")
    assert "easy_only safety hold is open (Knee injury in chat)" in refused
    assert db_session.query(Workout).count() == 0

    ok = coach_service._execute_tool(db_session, user, "add_workout", {
        "scheduled_date": day, "title": "Easy spin", "workout_type": "recovery",
    })
    assert ok.startswith("Added workout")


def test_under_hold_all_nothing_but_rest_or_a_skip(db_session):
    user = _user(db_session)
    workout = _workout(db_session, user)
    ss.open_hold(db_session, user, "hold_all", "Chest pain in chat", "detector")
    day = (TODAY + timedelta(days=1)).isoformat()
    refused = coach_service._execute_tool(db_session, user, "add_workout", {
        "scheduled_date": day, "title": "Spin", "workout_type": "recovery",
    })
    assert "nothing can go in the plan except rest" in refused
    assert coach_service._execute_tool(db_session, user, "add_workout", {
        "scheduled_date": day, "title": "Rest", "workout_type": "rest",
    }).startswith("Added workout")
    assert coach_service._execute_tool(db_session, user, "skip_workout", {
        "workout_id": workout.id,
    }).startswith("Skipped workout")


def test_a_hard_session_cannot_be_kept_under_a_hold_by_editing_round_it(db_session):
    user = _user(db_session)
    workout = _workout(db_session, user)
    ss.open_hold(db_session, user, "easy_only", "Pregnancy in chat", "detector")
    refused = coach_service._execute_tool(db_session, user, "update_workout", {
        "workout_id": workout.id, "scheduled_date": (TODAY + timedelta(days=4)).isoformat(),
    })
    assert refused.startswith("Not changed")
    db_session.refresh(workout)
    assert workout.scheduled_date == TODAY + timedelta(days=2)
    # Turning it into the easy version is exactly what the hold asks for.
    assert coach_service._execute_tool(db_session, user, "update_workout", {
        "workout_id": workout.id, "workout_type": "endurance",
    }).startswith("Updated workout")


def test_an_uncleared_screening_yes_gates_like_a_hold(db_session):
    user = _user(db_session)
    _screening(db_session, user, tier="easy_only")
    refused = coach_service._execute_tool(db_session, user, "add_workout", {
        "scheduled_date": (TODAY + timedelta(days=1)).isoformat(),
        "title": "Sweet spot", "workout_type": "sweet_spot",
    })
    assert "health screen needs a doctor's clearance first" in refused


def test_the_layoff_gate_only_holds_the_days_inside_it(db_session):
    user = _user(db_session)
    db_session.add(Ride(
        user_id=user.id, source=RideSource.manual,
        ride_date=datetime.combine(TODAY - timedelta(days=40), time(9)),
    ))
    db_session.commit()
    gate = ss.layoff_gate_until(db_session, user)
    inside = coach_service._execute_tool(db_session, user, "add_workout", {
        "scheduled_date": (gate - timedelta(days=1)).isoformat(),
        "title": "Threshold", "workout_type": "threshold",
    })
    assert f"hard sessions wait until {gate.isoformat()}" in inside
    after = coach_service._execute_tool(db_session, user, "add_workout", {
        "scheduled_date": gate.isoformat(), "title": "Threshold", "workout_type": "threshold",
    })
    assert after.startswith("Added workout")


def test_a_swap_cannot_move_a_hard_session_into_a_held_day(db_session):
    user = _user(db_session)
    hard = _workout(db_session, user, day=TODAY + timedelta(days=2))
    easy = _workout(db_session, user, template=get_template("endurance"), day=TODAY + timedelta(days=3))
    ss.open_hold(db_session, user, "easy_only", "Knee", "coach_tool")
    refused = coach_service._execute_tool(db_session, user, "swap_workout_date", {
        "workout_id_a": hard.id, "workout_id_b": easy.id,
    })
    assert refused.startswith("Not changed")


def test_plan_review_drops_and_acceptance_refuses_what_a_hold_rules_out(db_session):
    user = _user(db_session)
    workout = _workout(db_session, user, template=get_template("endurance"))
    changes = [
        {"action": "update_workout", "workout_id": workout.id, "workout_type": "vo2max"},
        {"action": "skip_workout", "workout_id": workout.id},
    ]
    proposal = PlanProposal(
        user_id=user.id, trigger="manual", observation="o", rationale="r",
        status="pending", changes=changes[:1],
    )
    db_session.add(proposal)
    db_session.commit()

    # The hold opens after the proposal was written, before the rider taps.
    ss.open_hold(db_session, user, "hold_all", "Fever in chat", "detector")
    kept, held = plan_review_service.gate_changes(db_session, user, changes)
    assert [c["action"] for c in kept] == ["skip_workout"] and len(held) == 1

    assert plan_review_service.apply_proposal(db_session, user, proposal) == 0
    db_session.refresh(workout)
    assert workout.workout_type.value == "endurance"


def test_propose_plan_change_files_nothing_the_hold_rules_out(db_session):
    user = _user(db_session)
    workout = _workout(db_session, user, template=get_template("endurance"))
    ss.open_hold(db_session, user, "easy_only", "Knee", "coach_tool")
    result = coach_service._execute_tool(db_session, user, "propose_plan_change", {
        "observation": "o", "rationale": "r",
        "changes": [{
            "action": "update_workout", "workout_id": workout.id,
            "workout_type": "threshold", "why": "w",
        }],
    })
    assert result.startswith("Nothing was filed. Every change you proposed")
    assert db_session.query(PlanProposal).count() == 0


# ── The two safety tools ────────────────────────────────────────────────────


def test_apply_safety_hold_applies_at_once(db_session):
    user = _user(db_session)
    result = coach_service._execute_tool(db_session, user, "apply_safety_hold", {
        "level": "hold_all", "reason": "Under 18", "red_flag": "minor",
    })
    hold = ss.current_hold(db_session, user.id)
    assert (hold.level, hold.source, hold.red_flag) == ("hold_all", "coach_tool", "minor")
    assert result.startswith("Hold applied now: hold_all")
    assert "Say nothing about how it lifts" in result
    assert "never mention a flag" in result
    event = db_session.query(SafetyEvent).one()
    assert (event.source, event.hold_id) == ("coach_tool", hold.id)
    # A lower level never replaces a higher one.
    again = coach_service._execute_tool(db_session, user, "apply_safety_hold", {
        "level": "easy_only", "reason": "Knee", "red_flag": "injury",
    })
    assert again.startswith("A hold_all hold was already open")
    assert ss.current_hold(db_session, user.id).id == hold.id


def test_apply_safety_hold_refuses_a_bad_level(db_session):
    user = _user(db_session)
    assert coach_service._execute_tool(db_session, user, "apply_safety_hold", {
        "level": "maybe", "reason": "x", "red_flag": "other",
    }).startswith("Error")
    assert ss.current_hold(db_session, user.id) is None


def test_flag_for_review_emails_gareth_once_per_incident(db_session, no_alerts):
    user = _user(db_session)
    note = {"reason": "crisis", "note": "Said they don't want to be here; gave Samaritans."}
    result = coach_service._execute_tool(db_session, user, "flag_for_review", note)
    assert result.startswith("Flagged for review")
    assert "Never tell them anyone at Forma will contact them" in result
    coach_service._execute_tool(db_session, user, "flag_for_review", note)
    assert [k for k, _, _ in no_alerts] == ["crisis"]
    assert db_session.query(SafetyEvent).filter(SafetyEvent.kind == "crisis").count() == 2


def test_flag_for_review_after_the_check_already_alerted_sends_nothing_more(db_session, no_alerts):
    user = _user(db_session)
    safety_screen.screen_message(db_session, user, "I'm 15")
    coach_service._execute_tool(db_session, user, "flag_for_review", {
        "reason": "minor", "note": "Said they are 15.",
    })
    assert no_alerts == []


def test_the_safety_tools_are_offered_and_never_wait_for_approval():
    names = {t["name"] for t in coach_service.COACH_TOOLS}
    assert {"apply_safety_hold", "flag_for_review"} <= names
    assert "flag_for_review" in coach_service._NO_PLAN_CHANGE_TOOLS
    assert "apply_safety_hold" not in coach_service._NO_PLAN_CHANGE_TOOLS  # the app refetches


# ── Briefings and nudges written before a hold opened are rewritten ─────────


def test_a_safety_change_marks_earlier_writing_stale(db_session):
    user = _user(db_session)
    written = datetime.utcnow() - timedelta(minutes=5)
    assert not safety_screen.safety_changed_since(db_session, user.id, written)
    ss.open_hold(db_session, user, "hold_all", "Fever", "detector")
    assert safety_screen.safety_changed_since(db_session, user.id, written)
    assert not safety_screen.safety_changed_since(db_session, user.id, datetime.utcnow() + timedelta(minutes=1))


def test_a_cached_briefing_is_rewritten_once_a_hold_opens(db_session, monkeypatch):
    from app.services import briefing_service

    user = _user(db_session)
    db_session.add(Briefing(
        user_id=user.id, date=date.today(), kind="daily", content="Smash the VO2s today.",
        created_at=datetime.utcnow() - timedelta(hours=2),
    ))
    db_session.commit()
    ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector")
    seen = {}

    class _Resp:
        pass

    def fake_call(**kw):
        seen.update(kw)
        return _Resp()

    monkeypatch.setattr(briefing_service.forma_core, "call", fake_call)
    monkeypatch.setattr(briefing_service, "response_text", lambda r: "Riding is on hold today.")
    monkeypatch.setattr(
        "app.services.metrics_service.get_current_fitness",
        lambda db, uid: {"ctl": 0, "atl": 0, "tsb": 0},
    )
    briefing = asyncio.run(briefing_service.get_or_create_briefing(db_session, user))
    # The stale briefing is gone. A held day may be written in fixed words
    # without the model; if the model writes it, it is told nothing is allowed.
    assert "Smash the VO2s" not in briefing.content
    assert "on hold" in briefing.content
    if seen:
        assert '"allowed_intensity": "none"' in seen["messages"][0]["content"]
    assert db_session.query(Briefing).count() == 1


def test_the_goal_read_never_pushes_audacity_under_a_hold(db_session, monkeypatch):
    from app.models.onboarding import EventPriority, EventType, GoalEvent
    from app.services import goal_read_service

    user = _user(db_session)
    goal = GoalEvent(
        user_id=user.id, event_name="Fred Whitton", event_date=TODAY + timedelta(days=60),
        event_type=EventType.sportive, priority=EventPriority.a_race,
    )
    db_session.add(goal)
    db_session.commit()
    ss.open_hold(db_session, user, "easy_only", "Knee", "coach_tool")
    seen = {}

    def fake_call(**kw):
        seen.update(kw)
        return object()

    monkeypatch.setattr(goal_read_service.forma_core, "call", fake_call)
    monkeypatch.setattr(goal_read_service, "response_text", lambda r: "A read.")
    goal_read_service.generate_goal_read(db_session, user, goal)
    assert "no push for a\nbigger goal" in seen["system"]
    assert '"allowed_intensity": "easy"' in seen["messages"][0]["content"]


def test_briefings_see_recent_health_notes(db_session):
    from app.models.memory import MemoryEntity
    from app.services.briefing_service import recent_health_notes

    user = _user(db_session)
    db_session.add_all([
        MemoryEntity(
            user_id=user.id, type="health_signal", label="Knee flared on Sunday",
            created_at=datetime.utcnow() - timedelta(days=2),
        ),
        MemoryEntity(
            user_id=user.id, type="health_signal", label="Cold in March",
            created_at=datetime.utcnow() - timedelta(days=200),
        ),
        MemoryEntity(user_id=user.id, type="goal", label="Fred Whitton"),
    ])
    db_session.commit()
    notes = recent_health_notes(db_session, user.id)
    assert len(notes) == 1 and "Knee flared on Sunday" in notes[0]


def test_the_nudge_fallback_never_talks_up_a_held_session(db_session, monkeypatch):
    from app.models.training import PhaseType, TrainingPhase, TrainingPlan
    from app.services import coach_insights_service

    user = _user(db_session)
    plan = TrainingPlan(
        user_id=user.id, name="Plan", start_date=TODAY - timedelta(days=7),
        end_date=TODAY + timedelta(days=60), status="active",
    )
    db_session.add(plan)
    db_session.flush()
    phase = TrainingPhase(
        plan_id=plan.id, phase_type=PhaseType.build, start_date=TODAY - timedelta(days=7),
        end_date=TODAY + timedelta(days=60), sort_order=0,
    )
    db_session.add(phase)
    db_session.flush()
    workout = _workout(db_session, user, day=TODAY)
    workout.phase_id = phase.id
    db_session.commit()
    ss.open_hold(db_session, user, "hold_all", "Chest pain", "detector")

    calls = []

    def down(**kw):
        calls.append(kw)
        raise RuntimeError("model down")

    monkeypatch.setattr(coach_insights_service.forma_core, "call", down)
    nudge = coach_insights_service.generate_daily_nudge(db_session, user)["nudge"]
    # Under a hold the nudge is fixed words from the record, with no model
    # call: what the hold means and how it lifts.
    assert nudge.startswith("All your training is on hold.")
    assert "I've been cleared" in nudge
    assert "make it count" not in nudge
    assert calls == []


# ── Re-verification new problems 1, 2, 6, 7 and 12 ─────────────────────────


def minor_hit(text: str) -> dict | None:
    return next((h for h in detect_red_flags(text) if h["kind"] == "minor"), None)


@pytest.mark.parametrize("text", [
    # A weight or a temperature, not an age.
    "I'm 16 and a half stone",
    "I'm 16 and a half stone and want to get lighter for hill climbs",
    "I'm 17 stone",
    "I'm 17 degrees warmer today",
    "I'm 17 C warmer than yesterday",
    # A past event, dated by an age.
    "aged 16 I broke my collarbone",
    "aged 15 I won my first race",
    "at age 16 I broke my arm",
    "when I was 15 I raced",
    "when I was 15",
    "at 16 I started racing",
    "as a 16 year old I raced in the juniors",
    "when I turned 16 I bought my first bike",
    # An age taken back at once.
    "I'm 17, no wait 47",
    "I'm 17... sorry 47",
    "17... sorry 47",
    "I'm 16, sorry, I mean 46",
    # Others.
    "I'm 16 again in my head lol",
    "I'm in year 13 of riding",
    "year 13 of riding",
    "I'm on junior gears",
    "junior gears are a good idea for him",
    "I'm a junior doctor, 16 years in",
    "I turned 15 minutes into the ride",
    "my daughter is turning 16 next month",
    "she's turning 17 next week",
    "I'm a year 12 teacher",
    # A word before a measure is still a measure.
    "I'm actually 15 minutes late",
    "I'm actually 16 stone",
    "I'm really 17 seconds off the KOM",
    "I'm actually 16th on the leaderboard",
    "I'm honestly 15 behind",
])
def test_an_adult_is_not_read_as_a_minor(text):
    assert minor_hit(text) is None, text


@pytest.mark.parametrize("text, age", [
    ("I'm 15", 15),
    ("I'm 17 and a half", 17),
    ("I'm only 16", 16),
    ("My mum says I'm too young at 15", 15),
    ("My mum says I'm too young at 15 to race", 15),
    ("I'm too young to race at 16, apparently", 16),
    ("turning 17 next month", 16),
    ("I'm turning 18 in March", 17),
    ("I turn 16 on Saturday", 15),
    ("I'll be 18 next year", 17),
    ("I'm nearly 18", 17),
    ("I'm a junior rider, 16", 16),
    ("I'm a student, 17", 17),
    ("I'm in year 13", 17),
    ("I'm in year 12", 16),
    ("I'm a year 12 student", 16),
    ("I'm doing my GCSEs this year", 14),
    ("I'm in sixth form", 16),
    ("16 year old here", 16),
    ("aged 16 and I want to race", 16),
    ("aged 16, I had my first race last week", 16),
    ("I'm 15 and I raced last year", 15),
    # Corrected to another age under 18: the correction is the age.
    ("I'm 16, no 15 actually", 15),
    ("I'm 17, no wait, 16", 16),
    # Corrected to a birthday to come: still 17.
    ("I'm 17, actually 18 in May", 17),
    # Round 4, new problem E: an admission with a word before the age.
    ("honestly I'm actually 15, my dad set this account up with his details", 15),
    ("I'm actually 15", 15),
    ("im really only 16", 16),
    ("I am genuinely 17", 17),
])
def test_every_minor_hit_carries_the_age_it_implies(text, age):
    hit = minor_hit(text)
    assert hit is not None, text
    assert hit["stated_age"] == age


def test_only_a_minor_hit_carries_a_stated_age():
    hits = detect_red_flags("I'm 15 and my chest hurts")
    assert {h["kind"]: h.get("stated_age", "absent") for h in hits} == {
        "chest_pain": "absent", "minor": 15,
    }
    assert safety_screen.stated_age_from("turning 17 next month") == 16
    assert safety_screen.stated_age_from("I'm 17, no wait 47") is None


def test_an_adult_giving_their_weight_is_never_held_as_a_minor(db_session):
    user = _user(db_session)
    result = safety_screen.screen_message(
        db_session, user, "I'm 16 and a half stone and want to get lighter for hill climbs"
    )
    assert "minor" not in result.kinds
    assert ss.current_hold(db_session, user.id) is None


def test_the_stated_age_reaches_the_record(db_session):
    user = _user(db_session)
    result = safety_screen.screen_message(
        db_session, user, "turning 17 next month, can you build me a plan?"
    )
    assert result.stated_age == 16
    event = db_session.query(SafetyEvent).filter(SafetyEvent.kind == "minor").one()
    if hasattr(SafetyEvent, "stated_age"):
        assert event.stated_age == 16
    hold = ss.current_hold(db_session, user.id)
    assert (hold.level, hold.red_flag) == ("hold_all", "minor")


# Problem 2: a denial the rider takes back is not a denial.


@pytest.mark.parametrize("text, kind, severity", [
    ("I don't have chest pain now but I did on the climb", "chest_pain", "emergency"),
    ("I don't have chest pain now", "chest_pain", "emergency"),
    ("no chest pain today, but yesterday I had it on the climb", "chest_pain", "emergency"),
    ("no chest pain today, but I did yesterday", "chest_pain", "emergency"),
    ("I didn't pass out but nearly did", "fainting", "urgent"),
    ("I haven't fainted, though I came close", "fainting", "urgent"),
    ("I don't remember hitting my head", "head_injury", "urgent"),
    ("I can't remember the crash", "head_injury", "urgent"),
    ("I don't remember the crash at all", "head_injury", "urgent"),
    ("no memory of hitting the ground", "head_injury", "urgent"),
    ("hitting my head on the stem", "head_injury", "urgent"),
    ("crashed, hitting my head on the bars", "head_injury", "urgent"),
    ("my head hit the stem", "head_injury", "urgent"),
    ("hit my head on the road", "head_injury", "urgent"),
    ("I smacked my head", "head_injury", "urgent"),
    ("smacked my head on the road when I came off", "head_injury", "urgent"),
])
def test_a_denial_taken_back_and_a_memory_gap_are_caught(text, kind, severity):
    assert kinds(text).get(kind) == severity


@pytest.mark.parametrize("text, not_kind", [
    ("no chest pain, but I did feel tired", "chest_pain"),
    ("no chest pain at all, but my legs are dead", "chest_pain"),
    ("no chest pain, but I was on the limit", "chest_pain"),
    ("no chest pain now that I've changed the strap", "chest_pain"),
    ("I didn't pass out", "fainting"),
    ("I can't remember the last time I crashed", "head_injury"),
    ("I can't remember the last crash I had", "head_injury"),
    ("I can't remember which ride I crashed on", "head_injury"),
    ("my mate can't remember the crash", "head_injury"),
    ("I keep banging my head against a brick wall with my FTP", "head_injury"),
    ("hitting my head against a wall with this plan", "head_injury"),
])
def test_a_plain_denial_and_ordinary_talk_stay_silent(text, not_kind):
    assert not_kind not in kinds(text)


def test_a_memory_gap_after_a_crash_is_told_to_go_to_hospital_today():
    line = safety_screen.head_direct_line("I can't remember the crash, everything's hazy")
    assert line == (
        "You have a gap in your memory after the crash, so go to {hospital} today, "
        "with someone else driving."
    )
    line = safety_screen.head_direct_line("I don't remember hitting my head")
    assert line.startswith("You have a gap in your memory after hitting your head")


# Problem 6: a cleared pregnancy never covers a new one.


def _cleared_pregnancy(db, words, days_ago=0, email="mum@example.com"):
    user = _user(db, email=email)
    safety_screen.screen_message(db, user, words)
    _clear(db, user, by="My midwife")
    if days_ago:
        for event in db.query(SafetyEvent).filter(SafetyEvent.user_id == user.id):
            event.created_at = datetime.utcnow() - timedelta(days=days_ago)
        db.commit()
    assert ss.current_hold(db, user.id) is None
    return user


@pytest.mark.parametrize("first, days_ago, later", [
    # The re-verification's probe.
    ("I'm pregnant", 0, "I'm pregnant again, 8 weeks"),
    ("I'm pregnant", 30, "I'm pregnant with our second"),
    # A lower week count than the cleared one.
    ("I'm 12 weeks pregnant", 0, "I'm 8 weeks pregnant"),
    # Far behind where the cleared pregnancy would be by now.
    ("I'm 12 weeks pregnant", 150, "I'm 14 weeks pregnant"),
    # More than 40 weeks on, or past term for the cleared one.
    ("I'm pregnant", 290, "being pregnant is tiring"),
    ("I'm 30 weeks pregnant", 100, "pregnant and tired"),
])
def test_a_new_pregnancy_is_held_despite_an_earlier_clearance(db_session, first, days_ago, later):
    user = _cleared_pregnancy(db_session, first, days_ago)
    result = safety_screen.screen_message(db_session, user, later)
    hold = ss.current_hold(db_session, user.id)
    assert hold is not None and (hold.level, hold.red_flag) == ("easy_only", "pregnancy")
    assert "confirmed a doctor's clearance" not in (result.context_line or "")


@pytest.mark.parametrize("first, days_ago, later", [
    ("I'm pregnant", 0, "still pregnant, all going well"),
    ("I'm 12 weeks pregnant", 14, "I'm 14 weeks pregnant"),
    ("I'm pregnant", 200, "being pregnant is tiring"),
    ("I'm pregnant with our second", 30, "pregnant with our second, 20 weeks now"),
])
def test_the_same_pregnancy_stays_covered_by_its_clearance(db_session, first, days_ago, later):
    user = _cleared_pregnancy(db_session, first, days_ago)
    safety_screen.screen_message(db_session, user, later)
    assert ss.current_hold(db_session, user.id) is None


@pytest.mark.parametrize("matched, facts", [
    ("pregnant", (None, None)),
    ("12 weeks pregnant", (12.0, None)),
    ("pregnant again, 8 weeks", (8.0, "again")),
    ("pregnant with baby number two", (None, "second")),
    ("3 months pregnant", (13.05, None)),
    ("second pregnancy", (None, "second")),
])
def test_what_a_pregnancy_hit_keeps(matched, facts):
    weeks, mark = safety_screen._pregnancy_facts(matched)
    assert (None if weeks is None else round(weeks, 2), mark) == facts


# Problems 7 and 12: phrasings missed, with ordinary cycling talk left alone.


@pytest.mark.parametrize("text, kind, severity", [
    ("I just want it all to end", "crisis", "crisis"),
    ("sometimes I'm thinking about crashing on purpose", "crisis", "crisis"),
    ("I want to ride into a lorry", "crisis", "crisis"),
    ("sometimes I want to ride into traffic", "crisis", "crisis"),
    ("I can't go on the way things are", "crisis", "crisis"),
    ("I don't see the point of training or anything", "crisis", "crisis"),
    ("I don't see the point of riding anymore, or of anything", "crisis", "crisis"),
    ("I crashed on purpose", "crisis", "crisis"),
    ("My mum says I'm too young at 15", "minor", "minor"),
    ("turning 17 next month", "minor", "minor"),
    ("I'm a junior rider, 16", "minor", "minor"),
    ("I'm in year 13", "minor", "minor"),
    ("my chest felt like an elephant was sitting on it", "chest_pain", "emergency"),
    ("it felt like an elephant sitting on my chest", "chest_pain", "emergency"),
    ("my chest felt like a vice", "chest_pain", "emergency"),
])
def test_the_phrasings_the_reverification_found_missing_are_caught(text, kind, severity):
    assert kinds(text).get(kind) == severity


@pytest.mark.parametrize("text, not_kind", [
    ("I want it all to end with a sprint finish", "crisis"),
    ("I want it all to end on a high", "crisis"),
    ("I'm going to ride into the car park", "crisis"),
    ("going to ride into the bus lane", "crisis"),
    ("I'm going to ride under the bridge", "crisis"),
    ("I want to ride into the traffic-free path", "crisis"),
    ("I want to ride into town", "crisis"),
    ("I'm going to ride into a headwind", "crisis"),
    ("I don't see the point of zwift or anything like that", "crisis"),
    ("I crashed on purpose to avoid the dog", "crisis"),
    ("I want the interval to end", "crisis"),
    ("year 13 of riding", "minor"),
    ("junior gears", "minor"),
    ("I'm on junior gears this season", "minor"),
    ("it felt like a weight was lifted off my chest", "chest_pain"),
    ("my chest felt like a weight had been lifted", "chest_pain"),
    ("my chest strap felt like a vice", "chest_pain"),
])
def test_cycling_talk_near_the_new_phrasings_stays_silent(text, not_kind):
    assert not_kind not in kinds(text)


# ── Re-verification round 3, problems 3, 5 and 8: the detector ─────────────

# Problem 3: crash symptoms with no word about the head, heart pain outside
# the chest, a fluttering, and a few seconds lost.


@pytest.mark.parametrize("text, kind, severity", [
    # The round 3 probes, word for word.
    ("I've got a headache since the crash yesterday", "head_injury", "urgent"),
    ("felt sick and dizzy since I crashed", "head_injury", "urgent"),
    ("I came off yesterday, now I've got a bad headache and feel confused",
     "head_injury", "urgent"),
    ("didn't hit my head I think but I've got a headache and feel sick", "head_injury", "urgent"),
    ("not knocked out as far as I know but I feel foggy", "head_injury", "urgent"),
    ("I wasn't knocked out I don't think", "head_injury", "urgent"),
    ("no chest pain but my left arm and jaw ache after the effort", "chest_pain", "emergency"),
    ("no chest pain, just a weird fluttering", "palpitations", "urgent"),
    ("I didn't black out but I lost a few seconds", "fainting", "urgent"),
    # Variants.
    ("as far as I know I wasn't knocked out but my head is pounding", "head_injury", "urgent"),
    ("I crashed, didn't hit my head but I've got a headache", "head_injury", "urgent"),
    ("I came off and lost a few seconds", "head_injury", "urgent"),
    ("had a crash on Saturday, still got a headache", "head_injury", "urgent"),
    ("I fell off on the ice this morning and feel a bit sick", "head_injury", "urgent"),
    ("got knocked off by a car, feeling dizzy", "head_injury", "urgent"),
    ("hit by a car yesterday and I'm seeing stars", "head_injury", "urgent"),
    ("I went down hard on the corner and feel groggy", "head_injury", "urgent"),
    ("the crash knocked me out for a few seconds", "head_injury", "urgent"),
    ("my jaw aches when I climb", "chest_pain", "emergency"),
    ("pain down my left arm on the climbs", "chest_pain", "emergency"),
    ("ache spreading into my jaw on hard efforts", "chest_pain", "emergency"),
    ("my neck hurts and I'm short of breath", "chest_pain", "emergency"),
    ("there's a fluttering in my chest", "palpitations", "urgent"),
    ("after the last effort I lost a few seconds", "fainting", "urgent"),
    # A denial they aren't sure of is no denial.
    ("I didn't pass out I don't think", "fainting", "urgent"),
    ("I didn't black out, not sure", "fainting", "urgent"),
    # A faint at the sight of blood is still a faint.
    ("I fainted when I saw blood", "fainting", "emergency"),
    ("I nearly fainted in the heat", "fainting", "urgent"),
])
def test_crash_symptoms_heart_aches_and_lost_seconds_are_caught(text, kind, severity):
    assert kinds(text).get(kind) == severity, detect_red_flags(text)


# Problems 5 and 8: ordinary cycling talk, old injuries and figures.


@pytest.mark.parametrize("text, not_kind", [
    # The round 3 probes, word for word.
    ("that session knocked me out", "head_injury"),
    ("the heat knocked me out today", "head_injury"),
    ("nearly fainted when I saw the price of the new Di2", "fainting"),
    ("almost passed out laughing", "fainting"),
    ("I nearly fainted when I saw the price of the bike", "fainting"),
    ("I broke my collarbone 10 years ago", "injury"),
    ("I'm 16 and 18 watts up again this week", "minor"),
    ("I'm 16 and 17 watts up on last test", "minor"),
    # Knocked out by a session, the heat or a cold, not a blow.
    ("yesterday's ride completely knocked me out", "head_injury"),
    ("the cold knocked me out for a week", "head_injury"),
    # Shock at a price or a number.
    ("I nearly fainted when I saw my power numbers", "fainting"),
    ("I nearly passed out when I saw the gradient", "fainting"),
    # Symptoms with no crash of the rider's own.
    ("my chain came off and I've got a headache from the stress", "head_injury"),
    ("I came off the back of the group and felt dizzy with the effort", "head_injury"),
    ("I nearly crashed and felt sick with the fright", "head_injury"),
    ("my mate crashed and has a headache", "head_injury"),
    ("I watched a crash in the pro race and felt sick", "head_injury"),
    ("Zwift crashed and I've got a headache", "head_injury"),
    ("massive sugar crash and a headache after the ride", "head_injury"),
    ("I crashed 10 years ago and haven't had a headache since", "head_injury"),
    ("I'm confused about my zones after the crash last year", "head_injury"),
    ("I've got a headache from the heat", "head_injury"),
    # Time lost to a rival, not a faint.
    ("I lost a few seconds on the climb", "fainting"),
    ("lost ten seconds to the leader in the sprint", "fainting"),
    ("I came off the wheel and lost a few seconds", "fainting"),
    # Everyday aches, not the heart.
    ("my lower back aches after long rides", "chest_pain"),
    ("my neck hurts after the TT", "chest_pain"),
    ("my arms ache after the sprint", "chest_pain"),
    ("my jaw aches from clenching on the climbs", "chest_pain"),
    ("my left arm aches from gripping the bars on the cobbles", "chest_pain"),
    ("my back hurts and I was out of breath on the climb", "chest_pain"),
    ("back pain spreading down my leg", "chest_pain"),
    ("no chest pain, just lower back ache from the long ride", "chest_pain"),
    ("my left arm is numb after long rides", "chest_pain"),
    # Nerves, an eyelid, a flag or a trace.
    ("butterflies fluttering in my stomach before the race", "palpitations"),
    ("my eyelid keeps fluttering", "palpitations"),
    ("the flags were fluttering in the wind", "palpitations"),
    ("my heart rate data looks fluttery", "palpitations"),
    # Injuries long past and healed.
    ("I broke my wrist as a kid", "injury"),
    ("tore my ACL in 2015, fully healed", "injury"),
    ("I broke my arm when I was 12", "injury"),
    ("I fractured my hip years ago and it healed fine", "injury"),
    # A number followed by a unit is a measure, not an age.
    ("I'm 16 or 17 kg lighter than last year", "minor"),
    ("I'm 15 and 20 bpm lower at threshold", "minor"),
    ("I'm 16 to 18 km into the ride", "minor"),
    ("I'm 17, 18 watts up this week", "minor"),
])
def test_the_round_3_false_alarms_stay_silent(text, not_kind):
    assert not_kind not in kinds(text), detect_red_flags(text)


@pytest.mark.parametrize("text", [
    "I broke my collarbone 10 years ago and it still aches",
    "I broke my collarbone 10 years ago, but last week I sprained my ankle",
    "I broke my wrist 3 weeks ago",
    f"I broke my collarbone in {datetime.utcnow().year}",
])
def test_an_injury_that_is_recent_or_still_there_is_still_held(text):
    assert kinds(text).get("injury") == "info"


@pytest.mark.parametrize("text, age", [
    ("I'm 16 and I want to race", 16),
    ("I'm 16 and 17 in May", 16),
])
def test_an_age_followed_by_and_is_still_an_age(text, age):
    hit = minor_hit(text)
    assert hit is not None and hit["stated_age"] == age


def test_an_adult_saying_watts_is_never_held_as_a_minor(db_session):
    user = _user(db_session)
    result = safety_screen.screen_message(
        db_session, user, "I'm 16 and 18 watts up again this week"
    )
    assert result.hits == []
    assert ss.current_hold(db_session, user.id) is None


def test_a_headache_after_a_crash_holds_everything_and_says_hospital_today(db_session):
    user = _user(db_session, country="GB")
    result = safety_screen.screen_message(
        db_session, user, "I've got a headache since the crash yesterday"
    )
    assert [h.matched for h in result.hits] == ["headache since the crash"]
    assert result.card_names == ["head"]
    hold = ss.current_hold(db_session, user.id)
    assert (hold.level, hold.red_flag) == ("hold_all", "head_injury")
    assert (
        "You still have a headache after the crash, so go to A&E today, with someone "
        "else driving." in result.context_line
    )


def test_an_arm_and_jaw_ache_after_effort_is_treated_as_chest_pain(db_session):
    user = _user(db_session, country="GB")
    result = safety_screen.screen_message(
        db_session, user, "no chest pain but my left arm and jaw ache after the effort"
    )
    assert result.card_names == ["chest"]
    assert "pain in your arm, jaw, neck or back that comes with effort" in result.cards[0][1]
    hold = ss.current_hold(db_session, user.id)
    assert (hold.level, hold.red_flag) == ("hold_all", "chest_pain")
    assert "Pain there can come from the heart even with no chest pain" in result.context_line
    # Said only when the hit isn't the chest itself.
    plain = safety_screen.screen_message(db_session, user, "my chest hurts")
    assert "Pain there can come from the heart" not in plain.context_line


# Problem 5: crisis words with clear training context are a distress check.

DISTRESS = [
    "what's the point of anything under 200W",
    "I just want it all to end, this block is brutal",
    "I want it all to end so I can taper",
    "I don't see the point of training or anything else without a goal event",
    "I hate myself for eating that cake lol",
]


@pytest.mark.parametrize("text", DISTRESS)
def test_crisis_words_with_clear_training_context_are_a_distress_check(text):
    assert kinds(text) == {"distress_check": "info"}


@pytest.mark.parametrize("text", [
    # Unambiguous, whatever surrounds it.
    "I want to kill myself, this block is brutal",
    "I don't want to be here anymore, this block is brutal",
    "I want to end my life so I can taper",
    "I want to ride into a lorry on the club run",
    "thinking about crashing on purpose on the descent",
    "I've been self-harming, training is hard",
    "suicidal thoughts again, the plan isn't helping",
    # The same words with no training context, or more than a bad session.
    "I just want it all to end",
    "I hate myself",
    "I hate myself lol",
    "I just want it all to end, this block is brutal and I can't cope",
    "What's the point in anything under 200W, honestly",
    "I hate myself. What's the point of anything under 200W",
])
def test_unambiguous_crisis_words_always_bring_the_crisis_card(text):
    found = kinds(text)
    assert found.get("crisis") == "crisis"
    assert "distress_check" not in found


@pytest.mark.parametrize("text", DISTRESS)
def test_a_distress_check_shows_no_card_holds_nothing_and_alerts_no_one(db_session, text):
    user = _user(db_session)
    result = safety_screen.screen_message(db_session, user, text)
    assert result.kinds == {"distress_check"}
    assert result.cards == [] and result.card_names == []
    assert result.alerts == []
    assert ss.current_hold(db_session, user.id) is None
    event = db_session.query(SafetyEvent).one()
    assert (event.kind, event.card_shown, event.hold_id) == ("distress_check", None, None)
    # No quiet window on emails: that follows real crisis words only.
    assert safety_screen.wellbeing_quiet_until(db_session, user.id) is None
    line = result.context_line
    assert "ask them directly whether they're okay" in line
    assert safety_screen.DISTRESS_LINE in line
    assert "No crisis numbers, no flag_for_review" in line
    assert "notice at the top of the page" not in line
    assert "say nothing about a check, a hold or a card" in line


def test_a_distress_check_in_the_chat_sends_no_card_and_no_email(
    db_session, fake_model, no_alerts
):
    user = _user(db_session)
    session = _session(db_session, user)
    chunks = _run(coach_service.stream_response(
        db_session, user, session, "I just want it all to end, this block is brutal"
    ))
    assert all(c["type"] != "safety" for c in chunks)
    assert no_alerts == []
    assert "distress_check" in fake_model[0]["system"][-1]["text"]
    # The fake reply never asked, so the check-in is added at the end.
    text = "".join(c["content"] for c in chunks if c["type"] == "text")
    assert text.rstrip().endswith(safety_screen.DISTRESS_LINE)


def test_crisis_words_in_a_training_sentence_still_bring_the_card_in_the_chat(
    db_session, fake_model, no_alerts
):
    user = _user(db_session)
    session = _session(db_session, user)
    chunks = _run(coach_service.stream_response(
        db_session, user, session, "I want to kill myself, this block is brutal"
    ))
    assert chunks[0] == {"type": "safety", "kind": "crisis", "card": "crisis", "text": CRISIS_CARD}
    assert [k for k, _, _ in no_alerts] == ["crisis"]


# ── Re-verification round 4: the false-negative and false-positive sweeps ──

# The false-negative sweep, word for word: (message, the kind it must bring).
# "I'm on antidepressants, sertraline" is left out on purpose: an
# antidepressant isn't on the medication rule's list.
ROUND4_MISSED = [
    ('Got a tight band across my chest on the climb today, went away when I stopped',
     'chest_pain'),
    ("There's a pressure in my chest when I push hard, like someone sitting on it", 'chest_pain'),
    ('My chest felt crushed halfway up the hill and it spread into my left arm', 'chest_pain'),
    ('Weird ache in my jaw and left shoulder every time I go over threshold', 'chest_pain'),
    ('Pain behind my breastbone during the intervals, eased off after', 'chest_pain'),
    ("chest's been tight and sore on every ride this week", 'chest_pain'),
    ('I get a burning feeling in the middle of my chest on efforts, not like heartburn',
     'chest_pain'),
    ("Couldn't catch my breath sitting on the sofa last night, chest felt heavy", 'chest_pain'),
    ("I've been getting short of breath at rest the last few days", 'chest_pain'),
    ('sharp pain in my chest on the sprint, took a while to go', 'chest_pain'),
    ('My heart went really fast and irregular on the descent, like 200bpm for no reason',
     'palpitations'),
    ('Heart was skipping beats on the turbo, felt like it was flopping about', 'palpitations'),
    ('my heart rate suddenly jumped to 220 while I was soft pedalling and stayed there for a '
     'few minutes',
     'palpitations'),
    ("Felt my heart racing and pounding in bed after the ride, couldn't settle it",
     'palpitations'),
    ('Garmin showed an irregular heartbeat warning and I felt fluttery', 'palpitations'),
    ('I passed out at the top of the climb', 'fainting'),
    ('Went all grey and woozy and had to lie down on the verge after the effort', 'fainting'),
    ('Nearly fainted after the last interval, everything went dark for a second', 'fainting'),
    ('I collapsed after finishing the TT and came round on the floor', 'fainting'),
    ('Blacked out for a moment on the turbo, woke up slumped on the bars', 'fainting'),
    ('my vision tunnelled and I almost keeled over at the end of the hill rep', 'fainting'),
    ('I fainted at the cafe stop after the ride', 'fainting'),
    ('Lost consciousness briefly during the race finish, mates said I went down', 'fainting'),
    ('Came off on a wet corner and my head hit the tarmac', 'head_injury'),
    ("crashed into the back of a car, helmet's split down the middle", 'head_injury'),
    ("I had a spill on the gravel and since then I've had a headache and feel foggy",
     'head_injury'),
    ("went over the bars yesterday, can't remember the bit before I hit the ground",
     'head_injury'),
    ('Smashed my head on the road when I hit the pothole, bit of a lump', 'head_injury'),
    ('Took a tumble in the crit, cracked my helmet, feel okay though', 'head_injury'),
    ('After the off on Saturday I keep getting headaches and feel sick', 'head_injury'),
    ('Had a crash this morning and now my vision is blurry', 'head_injury'),
    ('I was knocked unconscious in the crash, just for a few seconds', 'head_injury'),
    ('crashed on Sunday, been dizzy and confused since', 'head_injury'),
    ("fell off on the ice and banged my head, now I've got a headache that won't shift",
     'head_injury'),
    ("I've got a temperature of 38.4 and the chills", 'fever'),
    ('Running a fever since yesterday, aching all over', 'fever'),
    ('Got flu, chesty cough and hot and cold sweats', 'fever'),
    ("I've got a chest infection, coughing up stuff", 'fever'),
    ('Feeling feverish and shivery, should I still do the long ride?', 'fever'),
    ('Tested positive for covid, aching and a high temp', 'fever'),
    ("I don't want to be here anymore", 'crisis'),
    ("Honestly I've been thinking about ending it all", 'crisis'),
    ('I keep thinking everyone would be better off without me', 'crisis'),
    ("I've been having thoughts about killing myself", 'crisis'),
    ("I can't go on like this, I just want to disappear for good", 'crisis'),
    ('Sometimes I think about crashing on purpose so it would all stop', 'crisis'),
    ("I've been self harming again", 'crisis'),
    ("I've got pills saved up and I've been thinking about taking them all", 'crisis'),
    ("life isn't worth living since she left", 'crisis'),
    ("I feel hopeless, like there's no point in living", 'crisis'),
    ("I'm 15 and want to get into racing", 'minor'),
    ("I'm in year 11 and want to do the national hill climb", 'minor'),
    ("My mum said I can use the app, I'm 14", 'minor'),
    ("I'm 16, can you build me a plan for junior races?", 'minor'),
    ('im 13 and my dad bought me a power meter', 'minor'),
    ("I'm only seventeen but I'm racing seniors", 'minor'),
    ("I've got my GCSEs next summer so I can only ride weekends, I'm 15", 'minor'),
    ("I'm 10 weeks pregnant, can I keep riding?", 'pregnancy'),
    ("Just found out I'm expecting! Should I change the plan?", 'pregnancy'),
    ('I had my baby 6 weeks ago and want to get back on the bike', 'pregnancy'),
    ("I'm in my second trimester and still riding the turbo", 'pregnancy'),
    ("We're expecting our first in March, I'm the one carrying, so what should change?",
     'pregnancy'),
    ('gave birth by c-section in August, when can I start intervals?', 'pregnancy'),
    ("I've just been put on beta blockers", 'medication'),
    ('My GP started me on bisoprolol last week', 'medication'),
    ('I take warfarin, is crashing a worry?', 'medication'),
    ("I'm on insulin, how should I fuel?", 'medication'),
    ("I've started on blood thinners after a clot", 'medication'),
    ('I take amlodipine for my blood pressure', 'medication'),
    ("I'm on a course of steroids, prednisolone", 'medication'),
    ("I'm eating 1200 calories a day to make weight for the hill climb", 'restriction'),
    ("I've been skipping meals to get lighter", 'restriction'),
    ("I make myself sick after big meals so I don't put weight on", 'restriction'),
    ("I'm only eating one meal a day and riding 10 hours a week", 'restriction'),
    ('I want to drop 6 kilos in 3 weeks before the race', 'restriction'),
    ("I've stopped eating carbs completely and I'm riding fasted every day", 'restriction'),
    ('My periods have stopped since I started training hard and dieting', 'restriction'),
    ("I feel guilty every time I eat and I've been purging", 'restriction'),
    ("I've lost 8kg in a month by barely eating, is that good for climbing?", 'restriction'),
    ("It's going to be 34 degrees on Saturday, I'm planning a 4 hour ride at midday", 'heat'),
    ('Heatwave this week, 36C forecast, should I still do intervals outside?', 'heat'),
    ('Felt dizzy and stopped sweating in the heat on the ride, then got a headache', 'heat'),
    ("It's 31 degrees and humid, doing my threshold session on the road at 1pm", 'heat'),
    ("I'm off to Mallorca next week, forecast 38 degrees, want to do big days", 'heat'),
]


@pytest.mark.parametrize("text, kind", ROUND4_MISSED)
def test_every_red_flag_in_the_round_4_sweep_is_caught(text, kind):
    assert kind in kinds(text), detect_red_flags(text)


@pytest.mark.parametrize("text, kind", [
    # The follow-up probe, word for word.
    ("life's not worth living anymore", "crisis"),
    ("life is not worth living", "crisis"),
    ("life isn't worth living", "crisis"),
    ("I've been stockpiling my tablets", "crisis"),
    ("I've been saving up my pills", "crisis"),
    ("sternum pain on efforts", "chest_pain"),
    ("pain behind my breastbone", "chest_pain"),
    ("pain in my breastbone on the climb", "chest_pain"),
    ("my heart was beating irregularly", "palpitations"),
    ("heart went really fast and irregular", "palpitations"),
    ("I'm expecting a baby", "pregnancy"),
    ("I'm in my third trimester", "pregnancy"),
    ("second trimester now", "pregnancy"),
    ("I think I had heatstroke on the ride", "heat"),
    ("heat exhaustion on Saturday's ride", "heat"),
    ("stopped sweating and felt dizzy in the heat", "heat"),
    ("I've been making myself sick after eating", "restriction"),
    ("I've been purging after meals", "restriction"),
    ("I'm on ramipril", "medication"),
    ("I take lisinopril for blood pressure", "medication"),
    ("on medication for my blood pressure", "medication"),
    ("I take amlodipine", "medication"),
    ("vision's been blurry since the crash", "head_injury"),
    # Re-verification problem 3, still missed in round 4.
    ("After the off on Saturday I keep getting headaches and feel sick", "head_injury"),
    ("since my off on Saturday I keep getting headaches", "head_injury"),
    ("Had a crash this morning and now my vision is blurry", "head_injury"),
    ("had a spill this morning, now my vision's a bit blurry", "head_injury"),
    ("went over the bars yesterday, can't remember the bit before I hit the ground",
     "head_injury"),
    # Variants.
    ("I'm on tablets for my blood pressure", "medication"),
    ("I'm on steroids for my asthma flare", "medication"),
    ("I feel guilty every time I eat", "restriction"),
    ("I've cut out carbs completely", "restriction"),
    ("I ride fasted every day", "restriction"),
    ("We're expecting our first and I'm carrying the baby", "pregnancy"),
    ("I'm the one carrying this time", "pregnancy"),
    ("my hr was 210 at rest", "palpitations"),
    ("I've been stashing pills", "crisis"),
    ("my life is not worth living", "crisis"),
    ("honestly life isn't worth living, the bike is all I have", "crisis"),
    ("went over the bars yesterday and now I've got a headache", "head_injury"),
    ("I came off and my vision's blurry", "head_injury"),
    ("I blacked out on the bike", "fainting"),
    ("I blacked out the moment I stopped", "fainting"),
    ("I blacked out the other day on the turbo", "fainting"),
    # Still a real one beside the new exceptions.
    ("I've got glandular fever", "fever"),
    ("I've had a two day fever", "fever"),
    ("my hay fever is bad and now I've got a fever and chills", "fever"),
    ("my helmet strap snapped in the crash", "head_injury"),
    ("my helmet is cracked", "head_injury"),
    ("I dropped my helmet and cracked it, also I crashed yesterday", "head_injury"),
    ("Hit my head on the cupboard door", "head_injury"),
    ("Hit my head on the cupboard door, ouch, anyway, I feel a bit dizzy", "head_injury"),
    ("fell down the stairs and hit my head, no harm done lol", "head_injury"),
    ("Banged my head on the garage door, no harm done, but I was knocked out for a second",
     "head_injury"),
    ("It was 35 degrees and I felt awful", "heat"),
    ("It was 34 degrees yesterday and tomorrow is the same", "heat"),
    ("I tweaked my back yesterday but it's fine now", "injury"),
    ("I tweaked my back, it's fine now but it hurts on climbs", "injury"),
    ("I want to die every time I see the hill reps. I've been so low for weeks", "crisis"),
    ("I want to die every time I wake up", "crisis"),
    ("life isn't worth living without her", "crisis"),
    ("I hate myself, I haven't touched my bike in weeks", "crisis"),
])
def test_the_round_4_follow_up_variants_are_caught(text, kind):
    assert kind in kinds(text), detect_red_flags(text)


# The false-positive sweep, word for word: 303 ordinary messages.
ROUND4_ORDINARY = [
    'Did 3x12 at sweet spot this morning, felt strong on the last one',
    "Can we swap Thursday's VO2 session to Friday? I've got a work do",
    'Legs were absolutely dead on the club run, think I overcooked Tuesday',
    'Smashed my 20 minute power, 287W, chuffed to bits',
    'That last interval nearly killed me, but I held the watts',
    'My heart rate was sitting at 165 on the threshold efforts, is that about right?',
    'HR strap kept dropping out, the trace looks like a seismograph',
    'Is it worth doing a ramp test this week or wait till the block ends?',
    'I went into the red on the second climb and blew up spectacularly',
    'Zone 2 for 3 hours is so boring, any tips for staying sane?',
    'Cadence drills were weird, I felt like I was bouncing all over the saddle',
    'Should I be doing strength work in the gym over winter?',
    'Managed 400 TSS this week, feeling it in the legs',
    'The turbo session was brutal, sweat everywhere',
    'Can you make Saturday a long endurance ride instead of the tempo?',
    "I've got a sportive in 6 weeks, 160km with 2500m of climbing",
    'My FTP has stalled at 250 for two months now',
    'The over-unders were properly grim, I was on the rivet',
    'Hit a new 5 minute PB, 340W, got to be happy with that',
    'Rest day today, legs feel heavy but okay',
    'I did the recovery spin but my legs felt like lead',
    'Why does my power drop so much after 2 hours?',
    "I've been sleeping badly so I cut the session short",
    "What's the difference between tempo and sweet spot again?",
    'Felt a bit flat today, probably just tired from work',
    'I want to target the club hill climb at the end of October',
    'Rode the 10 mile TT on the A road, 23:40, a minute off my best',
    'I was in the hurt locker for the whole of the last effort',
    "Could we push the long ride to Sunday, it's chucking it down Saturday",
    'My left leg is doing a bit more work than my right according to the pedals',
    'I keep fading on the long climbs, is that pacing or fitness?',
    'Gave it everything on the last sprint and still got pipped on the line',
    "I'm knackered after the week, can I take an extra rest day?",
    'The 30/30s destroyed me, in a good way',
    "I'd like to get faster on the flat, any sessions for that?",
    'Do I need to do intervals in winter or just base miles?',
    'Heart rate drift was about 5% on the long ride',
    'I did 90 minutes at 200W and it felt easy',
    'The structured plan is working, my 1 hour power is up 12 watts',
    'My power meter battery died halfway through so the file is half empty',
    'Thinking of getting a new saddle, my current one is a bit numb after 3 hours',
    'Should I go tubeless before the winter?',
    'Snapped my chain on the climb, walked the last bit',
    'Cracked my rear rim on a pothole, gutted',
    'My Garmin crashed mid ride and I lost the first hour',
    'New Di2 is lovely, the shifts are so crisp',
    'Got a puncture in the lanes and had to fix it in the rain',
    'Do aero socks actually make a difference or is it marketing?',
    'Is a 54/40 chainring too big for the hilly sportive?',
    'I dropped my phone in a puddle, it survived',
    'Wheel truing cost me £30 at the LBS',
    "My helmet strap snapped so I've ordered a new lid",
    'Bought a new helmet, the old one was five years old',
    'Smart trainer keeps losing connection to Zwift',
    'Brakes are squealing like a banshee, need new pads',
    "Swapped to 28mm tyres and it's so much comfier",
    'The bike fit sorted my knee position, much better now',
    "I'm thinking about a power meter, single sided or dual?",
    "Got my winter bike out of the shed, it's filthy",
    'The chain was skipping all ride, think the cassette is worn',
    'What should I eat before a 3 hour ride?',
    "I bonked on the way home, should've had another gel",
    'Two bananas and a flapjack got me round',
    'How many carbs per hour should I aim for on long rides?',
    'I had a massive fry-up after the club run, no regrets',
    'Coffee stop at the cafe was the highlight of the ride',
    'Is beetroot juice worth it before a TT?',
    'Got the hunger knock on the last 20k, legs just stopped',
    "I've been having porridge before every session, works well",
    'I could murder a pizza after that ride',
    'Drank 2 bottles in 3 hours, was that enough?',
    'Ate a whole pack of jelly babies on the climb',
    "I'm trying to eat more protein after rides, any tips?",
    'Cake stop was essential today',
    'Electrolyte tabs or just water for an hour on the turbo?',
    "I'm starving after every ride, is that normal?",
    'Hydration was rubbish today, my wee was dark',
    'Tried a caffeine gel for the first time, felt like a rocket',
    'Should I fuel a 90 minute easy ride or is water fine?',
    'I had a big curry last night and felt it on the ride',
    "It's blowing a gale out there, headwind all the way home",
    "Absolutely freezing this morning, couldn't feel my toes",
    'Black ice on the lanes so I stayed on the turbo',
    'It was 22 degrees and sunny, perfect riding',
    'Rained the whole way round, soaked to the skin',
    'Foggy start but it burned off by 10',
    'Proper autumn day, leaves everywhere on the roads',
    'Wind was so strong I was going 15 kph on the flat',
    "It's grim up north today, staying indoors",
    'Lovely crisp morning, frost on the fields',
    'The roads are covered in mud from the tractors',
    'Bit of drizzle but nothing serious',
    "Forecast says storms all weekend so I'll move the long ride",
    'Got caught in a hailstorm on the moors, mental',
    "It's tipping it down so Zwift it is",
    'Got dropped on the first lap of the crit, embarrassing',
    'Our chaingang was flying today, 40kph average',
    'I got absolutely schooled by a 60 year old on the climb',
    'That bloke on the TT bike went past me like I was standing still',
    'The club sprint for the sign was carnage',
    'Finished 12th in the cat 3 race, happy with that',
    'I was hanging on for dear life in the bunch',
    'Someone attacked on the hill and I just died',
    'Got boxed in for the sprint, gutted',
    'Our club won the team prize at the hill climb',
    "I'm going to crush the Strava segment next week",
    'The race was a crash fest, I stayed upright thankfully',
    'There was a pile-up in front of me but I got round it',
    'Someone crashed in front of me but I managed to avoid it',
    'I nearly crashed on a wet roundabout but held it',
    'Got the KOM on the local climb, buzzing',
    'My mate Dave is a machine, he never gets tired',
    "I'm going to bury myself in the 25 on Sunday",
    "I'd kill for a sub-hour 25",
    'That climb is a killer, 20% at the top',
    'The cobbles shook my fillings loose',
    "I'll be gunning for the podium at the regional champs",
    'Half the bunch got caught behind the tractor',
    'Ended up soloing for 30k after the break went',
    'I was gasping on the steep bit, then recovered on the descent',
    'Bloody hell that was hard',
    "I'm chuffed with how today went",
    'Cracking ride with the lads this morning',
    "I'm absolutely shattered, off to bed",
    'Today was a total nightmare, got lost three times',
    'Work is doing my head in this week',
    "I'm gutted I missed the club run",
    'Proper bonus, the cafe had cheese scones',
    'That descent was hairy, gravel on every bend',
    "I'm sick of the rain",
    "I'm sick to death of headwinds",
    'The kids have been running me ragged this week',
    'I had a wobble on the descent but kept it upright',
    'Honestly my legs were screaming on the last climb',
    "I'm dying to get out on the new bike",
    'My boss is killing me with these deadlines',
    'Had a cheeky extra loop because the sun came out',
    'That Strava segment is my nemesis',
    "I'm well up for the sportive",
    'Feeling rough after the work Christmas do',
    "I'm a bit hungover, should I still ride?",
    "Can't be bothered with the turbo tonight",
    'My wife says I spend too much on bikes',
    "I'm 45 and want to get faster than I was at 35",
    "I'm 52 and racing vets, any tips for recovery?",
    "I've been riding for 16 years and never done structured training",
    "I'm turning 40 next month, want to do a big ride for it",
    'My son is 15 and wants to ride with me on Sundays, can he come on my plan rides?',
    "I'm doing 17 hours a week at the moment, is that too much?",
    'Back in 2009 when I was 16 I raced juniors',
    'We did 16 laps of Richmond Park',
    "I'm aiming for 18 mph average on the sportive",
    'I lost 16 seconds on the last split',
    'I lost a few seconds on the final lap',
    'The heat in the gym knocked the stuffing out of me',
    'I felt sick with nerves before the race',
    'My stomach was all over the place before the start',
    'Got a stitch on the run off the bike',
    "I'm feeling a bit under the weather, think it's just a cold in my nose",
    'My nose has been running all week, just a head cold',
    "I've got a bit of a sniffle but feel fine",
    'My knees were a bit achy on the long ride but nothing major',
    'My calves were tight after the hill reps',
    'Quads are sore from the gym session',
    "I've got DOMS from squats, should I still do the intervals?",
    'Bit of saddle soreness after the long one',
    'My hands go numb on long rides, is that the bars?',
    'Had a massage, my legs feel brand new',
    'Foam rolling is the worst but it helps',
    'My Strava fitness graph is going in the right direction at last',
    'Took the dog for a walk instead of riding, needed the break',
    'Had a week off for holiday, back on it now',
    "I'm back after a week in Spain, did a bit of riding there",
    'What a day, sun out, legs good, coffee great',
    'I want it to be over, this rain has been going for weeks',
    'I just want this block to be over so I can have a rest week',
    "I'm done for today, absolutely cooked",
    "I'm dead on my feet after that",
    "Honestly what's the point of the warm up on such a short session?",
    "I feel like giving up on the hill climb, I'll never be light enough",
    'I hate hill reps',
    'I hate the turbo so much',
    "I'm useless at descending, any tips?",
    "I'm rubbish at pacing long efforts",
    'My chest was heaving at the top of the climb, then I got my breath back',
    'I was breathing out of my ears on that last climb',
    'My heart was pounding at the top of the climb but settled after a minute',
    'Heart rate hit 190 on the sprint, is that normal for me at 34?',
    'I got a bit lightheaded standing up too fast after the turbo, fine after a second',
    'Gel tasted awful, nearly threw up',
    'I went over the bars in a cyclocross race last year, all good now',
    'I broke my wrist when I was a kid',
    'My Garmin says my training load is high, should I worry?',
    'Is it fine to ride with a slight hangover headache?',
    "I've got a headache from staring at spreadsheets all day",
    'Took a paracetamol for a headache earlier, all good now',
    "I'm on hay fever tablets, does that matter?",
    "I'm lactose intolerant so I avoid milky recovery shakes",
    "I'm vegan, how do I get enough protein?",
    "I'm trying to lose a couple of kilos before the hill climb, sensibly",
    'I fast till lunch on rest days, is that okay on easy days only?',
    'Missed lunch at work so I had a big tea',
    'It was 30 degrees in Mallorca, glorious',
    "My wife is pregnant so I'll be riding less after March",
    "My sister just had a baby so I'm off to visit this weekend",
    "My mate crashed on the descent last week, he's fine now",
    "My dad had a heart attack years ago so I'm careful with my own checks",
    'Hay fever is terrible this week, the pollen count is mad',
    'My hay fever is playing up, eyes streaming on the ride',
    'Took an antihistamine for hay fever before the ride',
    'Hay fever season again, sneezing all the way round',
    'Cabin fever from being stuck on the turbo all week',
    'Saturday night fever at the club dinner, great night',
    "Race day fever, can't wait for Sunday",
    'Tour de France fever has got me, rode 100k today',
    'Football fever in the house tonight so I rode early',
    "Had my flu jab yesterday, arm's a bit sore",
    "Covid booster today, should I skip tomorrow's intervals?",
    'My helmet strap buckle broke so I bought a new helmet',
    'The strap on my helmet snapped when I was putting it on',
    'Helmet visor cracked when I dropped it in the car park',
    "My helmet's cracked from being dropped off the shelf, getting a new one",
    "Left my helmet on the car roof and drove off, it's in bits",
    'The dog chewed my helmet strap',
    'Helmet light fell off on the descent',
    'Hit my head on the cupboard door, ouch, anyway about Saturday',
    'Banged my head on the garage door getting the bike out, no harm done',
    "I'm banging my head against a wall with this FTP plateau",
    'Hit the wall at 80k, legs went',
    'Hit my head on the low beam in the cafe, everyone laughed',
    'Got hit by a wave of tiredness at 3pm',
    'I was hit by a gust on the bridge and nearly went in the hedge',
    'Got knocked off my perch at the top of the Strava leaderboard',
    'That sprint knocked the wind out of me',
    "I'm knocked out by how much better the new wheels are",
    "It's warm out, about 24 degrees",
    'Turbo room gets to 28 degrees, need a bigger fan',
    'Holiday in Spain was 35 degrees, I rode at 7am',
    "Summer's over, back to 10 degrees and rain",
    'The Alps were 32 degrees at the bottom and 12 at the top',
    'Sunburnt arms from Saturday, proper cyclist tan now',
    'Forgot sun cream, lobster legs',
    "I've got a cold, just in my head, sniffly nose, no temperature",
    "I'm feeling run down, think I need more sleep",
    "I've got a sore throat, should I ride easy?",
    "My partner's got the flu so I'm sleeping in the spare room",
    'I feel like death warmed up after the night shift',
    "I'm a bit dizzy with excitement about the new bike",
    'My head is spinning with all these training zones',
    "The race was a blur, I don't remember half of it",
    "I've no memory of the last 5k, I was so in the zone",
    'Blacked out the dates in my diary for the training camp',
    'My vision of the season is to do three sportives',
    'I nearly fell asleep at my desk after the early ride',
    'I had to sit down after the hill reps, legs were jelly',
    'I could have collapsed in a heap after that, haha',
    'My legs collapsed under me on the last sprint, nothing left',
    'I died on the climb, got dropped by everyone',
    "I'm dead to the world after a long ride",
    "I'm literally dying on these hills",
    'That ride nearly finished me off',
    'Kill me now, another rest week',
    'I want to die every time I see the hill reps on the plan, lol',
    "I feel like I'm not good enough to race",
    "I'm so stressed with work, riding is my escape",
    "I'm feeling a bit low because the season's over",
    "I'm a bit down about missing the race",
    "I don't see the point of riding in this weather",
    'My heart sank when I saw the forecast',
    "My heart's set on doing the Fred Whitton",
    'I put my heart and soul into that TT',
    'Heartbroken to miss the club dinner',
    'Chest strap HRM chafes, any better ones?',
    "I got a chest cold last month but it's gone now",
    'Got something off my chest with the club captain, much better',
    'Breathless with excitement about the race',
    'My legs have been twitchy after the hard sessions',
    'My eye was twitching all day, too much coffee',
    "Pins and needles in my toes when it's cold, need overshoes",
    "My knee clicks but it doesn't hurt",
    "I tweaked my back lifting the bike onto the car but it's fine now",
    'Bit of a stiff neck from the TT position',
    'Sore bum after a long day in the saddle',
    "I'm on blood pressure tablets, my doctor knows I ride",
    'I take an inhaler before hard efforts, my asthma is well controlled',
    'I take statins, does that affect training?',
    "I'm diabetic and use a CGM on rides",
    "I'm 38 weeks into the training plan, it's been brilliant",
    'Week 12 of the block, pregnant with possibilities, haha',
    "I'm expecting a parcel with new tyres today",
    "We're expecting snow next week",
    'Lost 2kg over the last month by cutting out the biscuits',
    'I skipped breakfast and paid for it on the ride',
    "I'm 15 kilos lighter than when I started riding three years ago",
    "I'm 17st and want to get under 15st",
    "I'm sixteen stone and climbing is hard",
    'Year 10 of riding this sportive, still love it',
    'Our under 16s team won the club league, so proud of my lad',
    'I coach the under 14s on Saturday mornings',
    'Did 14 reps of the hill, aged like milk',
    "At 16 I was racing, at 46 I'm just hanging on",
    "I'm 17 seconds off the club record",
    'My daughter is 12 and just got her first road bike',
]

# What a few of them still bring. Medication and condition are by design: the
# coach needs to know. A knock about the house that the rider shrugs off is
# recorded with no card and no hold, and "want to die" about the hill reps is
# a kind check-in, not the crisis card.
ROUND4_BY_DESIGN = {
    "I'm on blood pressure tablets, my doctor knows I ride": {"medication": "info"},
    "I'm diabetic and use a CGM on rides": {"condition": "info"},
    "Hit my head on the cupboard door, ouch, anyway about Saturday": {"head_knock": "info"},
    "Banged my head on the garage door getting the bike out, no harm done":
        {"head_knock": "info"},
    "Hit my head on the low beam in the cafe, everyone laughed": {"head_knock": "info"},
    "I want to die every time I see the hill reps on the plan, lol":
        {"distress_check": "info"},
}


@pytest.mark.parametrize("text", ROUND4_ORDINARY)
def test_the_round_4_ordinary_messages_bring_nothing_more(text):
    assert kinds(text) == ROUND4_BY_DESIGN.get(text, {}), detect_red_flags(text)


@pytest.mark.parametrize("text, not_kind", [
    ("My hayfever is awful today", "fever"),
    ("hay-fever season is brutal", "fever"),
    ("I'm on medication for my hay fever", "medication"),
    ("Saturday night fever at the club dinner, great night", "fever"),
    ("my beard went grey this year", "fainting"),
    ("my face went white when I saw the bill", "fainting"),
    ("my helmet fell off the shelf and cracked", "head_injury"),
    ("I didn't hit my head on the cupboard, no harm done", "head_knock"),
    ("I'm expecting a parcel with new tyres today", "pregnancy"),
    ("I'm expecting a tough race on Sunday", "pregnancy"),
    ("I'm expecting the first race to be hard", "pregnancy"),
    ("I'm the one carrying the bikes to the car", "pregnancy"),
    ("We're expecting our first in March so I'll have less time", "pregnancy"),
    ("My wife is in her second trimester", "pregnancy"),
    ("it's like Zwift on steroids", "medication"),
    ("the pros are all taking steroids", "medication"),
    ("my dad takes tablets for his blood pressure", "medication"),
    ("I made myself sick on the last interval", "restriction"),
    ("I do my easy rides fasted on Sundays", "restriction"),
    ("purging the garage of old kit this weekend", "restriction"),
    ("Did a big kit purge", "restriction"),
    ("I've stopped sweating so much since I got a fan", "heat"),
    ("I've got caffeine pills saved up for race day", "crisis"),
    ("I've saved up enough tablets of electrolyte for the summer", "crisis"),
    ("painkillers stashed in my jersey pocket", "crisis"),
    ("I was saving up for a power meter", "crisis"),
    ("life is worth living", "crisis"),
    ("cycling makes life worth living", "crisis"),
    ("burning in my chest after the curry last night", "chest_pain"),
    ("burning feeling in my chest from the cold air on the climb", "chest_pain"),
    ("my lungs were burning on the climb", "chest_pain"),
    ("my chest strap's been tight", "chest_pain"),
    ("the sternum of the frame looks cracked haha", "chest_pain"),
    ("my heart rate hit 205 on the final sprint", "palpitations"),
    ("my HR shot up to 200 on the climb", "palpitations"),
    ("my heart rate jumped to 190 when I stood up", "palpitations"),
    ("heart was pounding after the sprint", "palpitations"),
    ("my vision went blurry from the sweat on the climb", "head_injury"),
    ("after the off I attacked straight away", "head_injury"),
    ("my off season starts next week", "head_injury"),
    ("since my off-season started I've had headaches", "head_injury"),
    ("since the off-season I've had headaches", "head_injury"),
])
def test_ordinary_talk_near_the_round_4_phrasings_stays_silent(text, not_kind):
    assert not_kind not in kinds(text), detect_red_flags(text)


@pytest.mark.parametrize("text", [
    "a life without cake isn't worth living lol",
    "life without the bike isn't worth living",
    "life isn't worth living without the bike",
])
def test_life_not_worth_living_in_a_cake_or_bike_sentence_is_a_distress_check(text):
    assert kinds(text) == {"distress_check": "info"}


# ── Re-verification round 4, problem A: a fresh head injury is new ─────────


@pytest.mark.parametrize("text, new", [
    # The round 4 probes, word for word.
    ("crashed again today and hit my head on the kerb", True),
    ("came off on the ice this morning, smacked my head, cracked helmet", True),
    # A fresh crash or blow: again, another, today, yesterday, or where.
    ("Had a crash this morning and now my vision is blurry", True),
    ("another crash, hit my head again", True),
    ("yesterday I came off and hit my head on the road", True),
    ("went over the bars yesterday, can't remember the bit before I hit the ground", True),
    ("hit my head on the kerb", True),
    ("Took a tumble in the crit, cracked my helmet, feel okay though", True),
    ("crashed on this morning's club run and hit my head", True),
    # The earlier injury mentioned again.
    ("how long after hitting my head can I do intervals?", False),
    ("when can I race again after hitting my head?", False),
    ("I've still got a headache today since the crash", False),
    ("I hit my head on the kerb last week, can I race on Sunday?", False),
    ("since my crash on the club run I've had headaches", False),
    ("vision's been blurry since the crash", False),
    ("is it ok to race three weeks after concussion?", False),
    ("I think I've got concussion", False),
])
def test_a_fresh_crash_or_blow_is_marked_as_a_new_head_injury(text, new):
    hit = next(h for h in safety_screen._detect(text) if h.kind == "head_injury")
    assert hit.new_event is new
    assert hit.as_dict()["new_event"] is new


def test_only_a_head_injury_hit_carries_new_event():
    hits = {h["kind"]: h for h in detect_red_flags("crashed again today, hit my head, chest pain")}
    assert hits["head_injury"]["new_event"] is True
    assert "new_event" not in hits["chest_pain"]
    assert all(not h.new_event for h in safety_screen._detect("I've got a fever and I'm 15"))


def test_a_new_head_injury_tells_the_holds_code_it_is_new(db_session, monkeypatch):
    calls = []
    real = ss.open_hold

    @functools.wraps(real)
    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(ss, "open_hold", spy)
    user = _user(db_session)
    safety_screen.screen_message(
        db_session, user, "crashed again today and hit my head on the kerb"
    )
    safety_screen.screen_message(
        db_session, user, "how long after hitting my head can I do intervals?"
    )
    assert len(calls) == 2
    if "new_event" in inspect.signature(real).parameters:
        assert [k["new_event"] for k in calls] == [True, False]
    else:
        assert all("new_event" not in k for k in calls)


# ── Re-verification round 4, problem D: what the false alarms now do ───────


def test_a_household_knock_shrugged_off_is_recorded_with_no_card_and_no_hold(db_session):
    user = _user(db_session, country="GB")
    result = safety_screen.screen_message(
        db_session, user, "Banged my head on the garage door getting the bike out, no harm done"
    )
    assert result.kinds == {"head_knock"}
    assert result.cards == [] and result.card_names == [] and result.alerts == []
    assert ss.current_hold(db_session, user.id) is None
    event = db_session.query(SafetyEvent).one()
    assert (event.kind, event.card_shown, event.hold_id) == ("head_knock", None, None)
    assert event.matched == "banged my head on the garage door"
    line = result.context_line
    assert "head_knock" in line and "no card and no hold, recorded only" in line
    assert "whether they've had a headache, felt sick, dizzy or confused" in line
    assert "say nothing about a check, a hold or a card" in line
    assert "notice at the top of the page" not in line


@pytest.mark.parametrize("text", [
    "My hay fever is playing up, eyes streaming on the ride",
    "Race day fever, can't wait for Sunday",
    "It was 30 degrees in Mallorca, glorious",
    "Holiday in Spain was 35 degrees, I rode at 7am",
    "I tweaked my back lifting the bike onto the car but it's fine now",
    "Helmet visor cracked when I dropped it in the car park",
    "My helmet strap snapped so I've ordered a new lid",
    "Blacked out the dates in my diary for the training camp",
    "Week 12 of the block, pregnant with possibilities, haha",
])
def test_the_round_4_false_alarms_open_no_hold_and_show_no_card(db_session, text):
    user = _user(db_session)
    result = safety_screen.screen_message(db_session, user, text)
    assert result.hits == [] and result.cards == []
    assert ss.current_hold(db_session, user.id) is None
    assert db_session.query(SafetyEvent).count() == 0


def test_hits_merged_with_the_classifier_settle_the_same_way(db_session):
    text = "Hit my head on the cupboard door this morning, ouch, anyway about Saturday"
    knock = safety_screen._detect(text)
    assert [h.kind for h in knock] == ["head_knock"]
    # The classifier reads a head injury where the regex read a knock: the
    # knock gives way, and the injury is marked new from the same words.
    added = safety_screen.Hit("head_injury", "hit my head", "urgent")
    settled = safety_screen._settled_hits([*knock, added], text)
    assert [(h.kind, h.new_event) for h in settled] == [("head_injury", True)]
    user = _user(db_session)
    on_hits = coach_service._screen_message_on([*knock, added])
    result = on_hits(db_session, user, text)
    assert result.kinds == {"head_injury"}
    assert result.card_names == ["head"]


# ── Re-verification round 5: each red flag as a clinician describes it ─────
#
# The round 5 sweep found the patterns fitted the old lists and missed 17 of
# 40 new wordings. The patterns now describe the clinical sign itself (a plan,
# losing consciousness, a memory gap after a fall, a blow to any part of the
# head, a measured fever, a band round the chest, and so on). Most sentences
# below were written after the patterns, as a check that they generalise.

# The 17 round 5 misses, word for word.
ROUND5_MISSED = [
    ("Halfway up the climb it felt like a belt tightening round my ribs and I had to stop",
     "chest_pain"),
    ("Woke up at 3am properly breathless and couldn't lie flat", "chest_pain"),
    ("On the easy spin my HR shot from 110 to 230 out of nowhere and my heart felt like it was "
     "going to jump out", "palpitations"),
    ("Blanked out completely for a few seconds on the rollers and toppled sideways", "fainting"),
    ("Came to on the floor of the garage after the turbo session", "fainting"),
    ("Slid out on gravel and bashed my temple on a rock, still throbbing two days later",
     "head_injury"),
    ("Mates said I was out for about a minute after I hit the deck", "head_injury"),
    ("Collided with a car door, can't remember how I got home", "head_injury"),
    ("I've been shivery and burning up all night, temperature was 38.9", "fever"),
    ("Had sickness and diarrhoea since yesterday, can I still ride?", "fever"),
    ("I've written letters to my family and I know how I'd do it", "crisis"),
    ("Honestly I just want to go to sleep and not wake up", "crisis"),
    ("Sixth form starts next week so my training time will drop", "minor"),
    ("I'm 14 weeks along, how hard can I go on the turbo?", "pregnancy"),
    ("I'm getting by on a bowl of soup a day so I'm light for the hill climb", "restriction"),
    ("If I don't ride I don't let myself eat dinner", "restriction"),
    ("After the hot sportive I was vomiting and had a pounding headache for hours", "heat"),
]


@pytest.mark.parametrize("text, kind", ROUND5_MISSED)
def test_the_round_5_misses_are_caught(text, kind):
    assert kind in kinds(text)


GENERAL_RED_FLAGS = [
    # Crisis with intent or a plan: letters, giving things away, knowing how,
    # a plan beside words about ending it, not waking up, better off.
    ("Wrote goodbye notes for the kids last night", "crisis"),
    ("I've left notes for my wife and kids for when I'm gone", "crisis"),
    ("I wrote letters to my parents last night, just in case", "crisis"),
    ("I've been giving all my bikes and kit away this week, won't need them", "crisis"),
    ("I've already worked out how I'd do it, I just haven't yet", "crisis"),
    ("I know exactly how I would do it and where", "crisis"),
    ("I've figured out how I'd do it and I've stopped caring", "crisis"),
    ("I've got a plan and I've picked a date, I can't do this any more", "crisis"),
    ("The plan is made, I've said my goodbyes to everyone", "crisis"),
    ("I've made all the arrangements, the family will be better off", "crisis"),
    ("I'm getting my affairs in order this week", "crisis"),
    ("Some nights I hope I just don't wake up in the morning", "crisis"),
    ("Part of me hopes I don't wake up tomorrow", "crisis"),
    ("it would be easier if I just didn't wake up tomorrow", "crisis"),
    ("I wish I'd never been born", "crisis"),
    ("Everyone would honestly be better off.", "crisis"),
    ("My partner and kids would be better off.", "crisis"),
    ("My family would be better off with me gone", "crisis"),
    # Losing consciousness, with no crash: a faint.
    ("I came round on the bathroom floor after the long ride", "fainting"),
    ("When I came to I was lying next to the turbo", "fainting"),
    ("Came round in the back of an ambulance after the hill reps", "fainting"),
    ("I was out cold for a few seconds at the top of the climb", "fainting"),
    ("My wife says I was out for about 20 seconds after the ramp test", "fainting"),
    ("Must have blanked out for a second after the last interval", "fainting"),
    ("Went unconscious briefly after the sprint, my wife found me", "fainting"),
    ("Woke up on the kitchen floor after my interval session", "fainting"),
    ("Lost conciousness for a moment on the rollers", "fainting"),
    ("Next thing I knew I was lying on the verge with people standing over me", "fainting"),
    # Losing consciousness or a memory gap after a crash or a fall.
    ("Crashed on the descent and came to with paramedics around me", "head_injury"),
    ("Came off on the roundabout, the guys said I was out for a couple of minutes",
     "head_injury"),
    ("Came off at the roundabout and when I came to an ambulance was there", "head_injury"),
    ("Came down hard in the crit and was unconscious for a minute", "head_injury"),
    ("Came off on the ice and I don't remember the ride back", "head_injury"),
    ("After the fall I lost a chunk of time, no idea how I got home", "head_injury"),
    ("Went into the back of a parked van, can't recall getting home", "head_injury"),
    ("Took a spill on the gravel, can't remember the rest of the ride", "head_injury"),
    ("Hit by a car on the commute and I lost a chunk of the morning", "head_injury"),
    ("Hit a parked car, everything after that is a blank until the hospital", "head_injury"),
    # A blow to any part of the head, by any object or surface.
    ("Fell off at the junction and smacked my forehead on the kerb", "head_injury"),
    ("Took a tumble and whacked my skull on a signpost", "head_injury"),
    ("Crashed into a hedge and took a blow to the temple from a branch", "head_injury"),
    ("Lost the front on the descent and went head first into the barrier", "head_injury"),
    ("Came off and my face hit the tarmac pretty hard", "head_injury"),
    ("Clipped a wheel, face-planted into the tarmac", "head_injury"),
    ("Came off on the descent and my helmet hit the road hard", "head_injury"),
    ("Had another spill, cut my forehead on the kerb", "head_injury"),
    ("Got doored, smacked my temple on the car door", "head_injury"),
    # A measured fever in any format, feverish, and gastroenteritis.
    ("Thermometer says 38.2 this morning", "fever"),
    ("my temp hit 39C overnight", "fever"),
    ("temperature of 101F since yesterday, feel rough", "fever"),
    ("My temp is 100.9F", "fever"),
    ("Temperature reading of 38.7 this afternoon", "fever"),
    ("Been running 38.5 all day, aching everywhere", "fever"),
    ("Burning up and then shivering, can't get warm", "fever"),
    ("Got D and V, been up all night", "fever"),
    ("Been up all night with D&V", "fever"),
    ("Stomach bug since Tuesday, can I do the club run?", "fever"),
    ("Picked up norovirus at work", "fever"),
    ("Got food poisoning from the cafe stop", "fever"),
    ("I've had the runs and been sick all morning", "fever"),
    # A band, belt, vice or weight round the chest or ribs; breathless at
    # night or lying flat.
    ("A tight band across my chest every time I push on the hills", "chest_pain"),
    ("On the steep bit it was like someone was squeezing my ribs in a vice", "chest_pain"),
    ("Felt a vice tightening round my ribs during the threshold set", "chest_pain"),
    ("It's like an iron band clamped across my chest when I climb", "chest_pain"),
    ("Vice-like pressure in my chest at threshold", "chest_pain"),
    ("Breathlessness wakes me up most nights now", "chest_pain"),
    ("I can't lie flat any more without getting short of breath", "chest_pain"),
    ("Can't sleep lying flat, I get short of breath", "chest_pain"),
    ("I have to sleep propped up on three pillows to breathe", "chest_pain"),
    # A heart rate over 200 out of nowhere or at low effort.
    ("Heart rate went to 215 on a zone 2 ride for no apparent reason", "palpitations"),
    ("My heart rate hit 212 just walking up the stairs", "palpitations"),
    ("My pulse jumped from 95 to 220 on a recovery spin", "palpitations"),
    ("Garmin showed 240 bpm during the warm up and it wouldn't come down", "palpitations"),
    ("My heart was going haywire on the sofa last night", "palpitations"),
    # Pregnancy in weeks along or gone.
    ("I'm 9 weeks gone, can I still race?", "pregnancy"),
    ("Now 22 weeks along and still riding the turbo", "pregnancy"),
    ("I'm sixteen weeks along now", "pregnancy"),
    # Eating restricted for weight or riding.
    ("I only eat if I've earned it on the bike", "restriction"),
    ("I don't let myself eat on days I don't train", "restriction"),
    ("I won't let myself have lunch if I haven't trained", "restriction"),
    ("Living on black coffee and an apple a day to make race weight", "restriction"),
    ("Only eating a yoghurt a day to get lighter for the hill climb", "restriction"),
    ("Skipping lunch and dinner so I'm lighter for the hill climb", "restriction"),
    ("I'm starving myself to get my w/kg up", "restriction"),
    # Heat illness after a hot ride or event.
    ("Rode in the heat all afternoon and got really confused at the end", "heat"),
    ("After the scorching crit I threw up and had a splitting headache", "heat"),
    ("I threw up after riding in the midday sun", "heat"),
    # School years as a stated age.
    ("Starting year 12 in September so less time to train", "minor"),
    ("Going into year 11 after the summer", "minor"),
    ("I've got my GCSE mocks next week", "minor"),
    ("GCSEs start in May so I'll ride less", "minor"),
    ("I start sixth form next week", "minor"),
    ("I'm in 10th grade and race juniors", "minor"),
]


@pytest.mark.parametrize("text, kind", GENERAL_RED_FLAGS)
def test_each_red_flag_is_caught_in_new_wording(text, kind):
    assert kind in kinds(text)


# Everyday talk near the general patterns: none of it opens a hold, shows a
# card or emails Gareth.
GENERAL_ORDINARY = [
    "I know how I'd do it differently next time, attack earlier on the climb",
    "I have a plan for Sunday's race, go with the break",
    "Made arrangements to borrow a bike for the trip",
    "Giving away my old wheels to the club juniors",
    "Wrote a note for my wife saying I'd be back by 12",
    "Wrote a letter to my MP about the cycle lane",
    "I want to go to sleep and not wake up until Monday after that sportive",
    "Everyone would be better off riding on the left at the lights",
    "The group would be better off with a slower pace on the climbs",
    "I came to the club run for the first time in ages",
    "Came to the conclusion I need a lower gear",
    "We came round the corner and there was a tractor",
    "I came round to the idea of a power meter",
    "I was out for 25 minutes on the rollers",
    "I went out for 20 minutes to spin the legs",
    "Blanked out the dates I can't ride on the calendar",
    "My mind blanked out on the name of the climb",
    "Woke up on the floor of my mate's flat after his party, slept fine",
    "Next thing I knew I was in the break with two others",
    "Took a second off my PB on the segment",
    "Can't remember how I got so unfit over winter",
    "I don't remember the ride back being this hilly",
    "Lost a chunk of time to the leaders on the climb",
    "Head first into the training block this week",
    "Face hit the wind like a wall at the top",
    "It was 38 degrees in Seville on holiday, rode at dawn",
    "The temperature was 101F in Arizona yesterday on our trip",
    "Burning up the climb on the chaingang",
    "Shivering on the descent after sweating up the climb",
    "Gels gave me diarrhoea on the sportive",
    "Diarrhoea and vomiting bug going round the office, hope I avoid it",
    "Heart rate strap felt like a vice round my chest, loosened it",
    "My HRM band was tight round my ribs so I adjusted it",
    "Got a stitch in my ribs on the run",
    "Woke up breathless from a nightmare about the race",
    "Couldn't lie flat on the massage table because of my back",
    "Hit 205 bpm in the final sprint, max effort",
    "Averaged 210 watts on the easy ride",
    "Two weeks gone already since the race, time flies",
    "I'm 6 weeks along in the plan, feeling strong",
    "Earned my cake at the cafe stop",
    "If I don't ride I don't need as many carbs",
    "I eat a bowl of soup a day for lunch at work",
    "Taking one bottle to be lighter on the climb",
    "Hot ride today, drank loads and felt great",
    "I teach at a sixth form college, so I ride at weekends",
    "My son starts sixth form next week",
    "When I was in year 11 I broke my wrist",
    "Year 11 of racing for me, still love it",
    "I'm in year 3 of my PhD",
]


@pytest.mark.parametrize("text", GENERAL_ORDINARY)
def test_ordinary_talk_near_the_general_patterns_opens_nothing(text):
    acting = set(safety_screen.HOLD_FOR) | set(safety_screen.CARD_FOR_KIND)
    acting |= safety_screen.ALERT_KINDS
    assert not set(kinds(text)) & acting


@pytest.mark.parametrize("text, kind, not_kind", [
    # Out with no crash is a faint; out after a crash is a head injury,
    # whichever order it is told in; a fall after blacking out is a faint too.
    ("Came to on the floor of the garage after the turbo session", "fainting", "head_injury"),
    ("Mates said I was out for about a minute after I hit the deck", "head_injury", "fainting"),
    ("Crashed on the descent and came to with paramedics around me", "head_injury", "fainting"),
    ("Blanked out completely for a few seconds on the rollers and toppled sideways",
     "fainting", None),
])
def test_losing_consciousness_is_a_faint_or_a_head_injury_by_what_came_first(
    text, kind, not_kind
):
    found = kinds(text)
    assert kind in found
    if not_kind:
        assert not_kind not in found
    if kind == "fainting":
        assert found["fainting"] == "emergency"


@pytest.mark.parametrize("text, fires", [
    ("temperature was 38.9 and I feel rough", True),
    ("my temp hit 39C overnight", True),
    ("temperature of 101F since yesterday, feel rough", True),
    ("38.0 on the thermometer", True),
    ("37.8 on the thermometer this morning", False),
    ("my temp was 100.2F", False),
    ("The thermometer in the garage says 38 degrees, mad", False),
    ("Road temperature was 40C on the Tarmac", False),
])
def test_a_measured_fever_counts_from_38_in_any_format(text, fires):
    assert ("fever" in kinds(text)) is fires


def test_a_measured_fever_is_not_read_as_the_weather():
    assert kinds("my temp hit 39C overnight") == {"fever": "urgent"}


@pytest.mark.parametrize("text, age", [
    ("Sixth form starts next week so my training time will drop", 16),
    ("Starting year 12 in September", 16),
    ("Going into year 11 after the summer", 15),
    ("I've got my GCSE mocks next week", 14),
    ("I'm in 10th grade and race juniors", 15),
])
def test_a_school_year_gives_the_youngest_age_in_it(text, age):
    assert safety_screen.stated_age_from(text) == age


def test_weeks_along_keeps_the_week_count_and_is_a_pregnancy_now():
    hit = next(h for h in safety_screen._detect("I'm 14 weeks along") if h.kind == "pregnancy")
    assert safety_screen._pregnancy_facts(hit.matched) == (14.0, None)
    assert safety_screen.standing_term("pregnancy", hit.matched) == "pregnancy"
    assert safety_screen._pregnancy_facts("i'm sixteen weeks along")[0] == 16.0


# ── Round 5 guards: a general question ──────────────────────────────────────

QUESTIONS = [
    ("What should I do if I ever get chest pain on a ride?", {"chest_pain"}),
    ("What are the signs of heatstroke?", {"heat"}),
    ("How do I know if I've got concussion?", {"head_injury"}),
    ("What would happen if I fainted on the turbo?", {"fainting"}),
    ("What should I do if I get a fever before a race?", {"fever"}),
    ("What are the signs of a heart attack on the bike?", {"chest_pain"}),
    ("If I ever pass out on a ride what should my mates do?", {"fainting"}),
    ("What should I do if I ever get chest pain and how do I spot heatstroke?",
     {"chest_pain", "heat"}),
]


@pytest.mark.parametrize("text, topics", QUESTIONS)
def test_a_general_question_brings_no_card_no_hold_and_no_record(db_session, text, topics):
    user = _user(db_session, country="GB")
    result = safety_screen.screen_message(db_session, user, text)
    assert result.kinds == {"safety_question"}
    assert set(safety_screen.question_topics(result.hits[0])) == topics
    assert result.cards == [] and result.card_names == [] and result.alerts == []
    assert ss.current_hold(db_session, user.id) is None
    assert db_session.query(SafetyEvent).count() == 0
    line = result.context_line
    assert "safety_question about" in line
    assert "whether this is happening to them now or has happened recently" in line
    assert "999" in line
    assert "say nothing about a check, a hold or a card" in line
    assert "notice at the top of the page" not in line


@pytest.mark.parametrize("text, kind", [
    ("What should I do if I ever get chest pain? I had it on the climb today", "chest_pain"),
    ("What should I do if I ever get chest pain? It felt tight on the climb today",
     "chest_pain"),
    ("What should I do about the chest pain I had this morning?", "chest_pain"),
    ("How do I know if I've got concussion? I crashed this morning and hit my head",
     "head_injury"),
    ("What are the signs of heatstroke? I stopped sweating on the ride and felt dizzy", "heat"),
    ("What should I do if I get chest pain again?", "chest_pain"),
    ("How do I deal with my concussion?", "head_injury"),
])
def test_a_real_symptom_beside_a_question_still_fires(db_session, text, kind):
    user = _user(db_session)
    result = safety_screen.screen_message(db_session, user, text)
    assert kind in result.kinds
    assert "safety_question" not in result.kinds
    if kind in safety_screen.HOLD_FOR:
        assert ss.current_hold(db_session, user.id) is not None


def test_a_general_question_in_the_chat_sends_no_card(db_session, fake_model, no_alerts):
    user = _user(db_session, country="GB")
    session = _session(db_session, user)
    chunks = _run(coach_service.stream_response(
        db_session, user, session, "What should I do if I ever get chest pain on a ride?"
    ))
    assert all(c["type"] != "safety" for c in chunks)
    assert no_alerts == []
    assert ss.current_hold(db_session, user.id) is None
    system = fake_model[0]["system"][-1]["text"]
    assert "safety_question about chest_pain" in system
    assert "Call 999 if it is still there" in system


def test_a_question_the_classifier_reads_as_real_is_the_real_red_flag(db_session):
    text = "What should I do if I ever get chest pain on a ride?"
    question = safety_screen._detect(text)
    assert [h.kind for h in question] == ["safety_question"]
    added = safety_screen.Hit("chest_pain", "chest pain", "emergency")
    settled = safety_screen._settled_hits([*question, added], text)
    assert [h.kind for h in settled] == ["chest_pain"]
    user = _user(db_session)
    result = coach_service._screen_message_on([*question, added])(db_session, user, text)
    assert result.kinds == {"chest_pain"} and result.card_names == ["chest"]


def test_the_fixed_reply_to_a_general_question_still_gives_the_safety_facts():
    reply = safety_screen.fallback_reply(
        {"safety_question"}, "GB", None,
        matched={"safety_question": "chest pain"},
        message="What should I do if I ever get chest pain on a ride?",
    )
    assert reply.startswith(safety_screen.REPLY_FAILED_MESSAGE)
    assert "Call 999" in reply and "hold" not in reply


# ── Round 5 guards: an illness long over ────────────────────────────────────


@pytest.mark.parametrize("text", [
    "Had a fever last month, all better now and back on the bike",
    "Had covid in the summer, fully recovered now",
    "Had a fever last month, all better now, can I start intervals again?",
    "Had the flu back in January but I'm fine now",
    "Had a chest infection a couple of months ago, totally cleared up",
    "Had a stomach bug in August, all better now",
])
def test_an_illness_long_over_opens_no_hold(db_session, text):
    user = _user(db_session)
    result = safety_screen.screen_message(db_session, user, text)
    assert "fever" not in result.kinds
    assert result.cards == []
    assert ss.current_hold(db_session, user.id) is None


@pytest.mark.parametrize("text", [
    "Had a fever this week, still feel rough",
    "Had covid last week and I'm still coughing",
    "I've still got a fever from Monday",
    "Had flu last month and it came back again today",
    "Had a fever last week, all better now",
])
def test_a_recent_or_lingering_illness_still_holds(db_session, text):
    user = _user(db_session)
    result = safety_screen.screen_message(db_session, user, text)
    assert "fever" in result.kinds
    assert ss.current_hold(db_session, user.id).level == "hold_all"


# ── Round 5, problem A: a second head injury said any way restarts the clocks


@pytest.mark.parametrize("text", [
    "had a second off this morning, landed on my head",
    "slid out on gravel this morning and bashed my temple",
    "Slid out on gravel this morning and bashed my temple on a rock",
    "Another spill today, cracked my forehead on the stem",
    "Mates said I was out for about a minute after I hit the deck today",
])
def test_a_second_head_injury_said_any_way_is_new(text):
    hit = next(h for h in safety_screen._detect(text) if h.kind == "head_injury")
    assert hit.new_event is True


@pytest.mark.parametrize("text", [
    "took a second off my PB on the segment, then hit my head on the cupboard, ouch, anyway",
    "how long after hitting my head can I do intervals?",
])
def test_a_second_off_the_clock_or_a_remention_is_not_a_new_injury(text):
    hits = [h for h in safety_screen._detect(text) if h.kind == "head_injury"]
    assert all(not h.new_event for h in hits)


@pytest.mark.parametrize("text", [
    "had a second off this morning, landed on my head",
    "Slid out on gravel this morning and bashed my temple on a rock",
])
def test_a_second_head_injury_restarts_the_clocks(db_session, monkeypatch, text):
    t0 = datetime(2026, 10, 8, 9, 0)
    user = _user(db_session)
    monkeypatch.setattr(ss, "_now", lambda: t0)
    safety_screen.screen_message(db_session, user, "crashed and hit my head, bit of a headache")
    monkeypatch.setattr(ss, "_now", lambda: t0 + timedelta(days=1))
    ss.lift_head_injury(db_session, user, ss.open_holds(db_session, user)[0].id, "My GP")
    t1 = t0 + timedelta(days=11)
    monkeypatch.setattr(ss, "_now", lambda: t1)
    monkeypatch.setattr(ss, "_today", lambda: t1.date())
    result = safety_screen.screen_message(db_session, user, text)
    assert [(h.kind, h.new_event) for h in result.hits if h.kind == "head_injury"] == [
        ("head_injury", True)
    ]
    new = [h for h in ss.open_holds(db_session, user) if h.expires_at is None]
    assert len(new) == 1 and ss.remention_of(db_session, new[0]) is None
    t2 = t1 + timedelta(days=1)
    monkeypatch.setattr(ss, "_now", lambda: t2)
    monkeypatch.setattr(ss, "_today", lambda: t2.date())
    ss.lift_head_injury(db_session, user, new[0].id, "My GP")
    assert ss.no_racing_or_group_until(db_session, user, today=t2.date()) == (
        t1 + timedelta(days=ss.HEAD_NO_RACING_DAYS)
    ).date()
