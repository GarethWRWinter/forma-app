"""The red-flag check: a fixed screen on every rider message, run before the
model sees it.

The SAFETY LAW tells the coach what to do with chest pain, a head injury or
words of crisis, but a model can miss them, or bury them under a training
answer. This check cannot: it is plain phrase matching, so it fires the same
way every time, and it acts before a single word of the reply is written.

A hit does four things (plan section E):
  1. writes a safety_events row (what matched, what the rider was shown);
  2. opens a hold: hold_all for chest symptoms, fainting, a head injury,
     fever or a rider under 18; easy_only for an injury, a pregnancy, a
     medicine, a condition or a break of four weeks or more;
  3. adds a SAFETY CONTEXT line to the coach's system prompt for this turn:
     what matched, the rider's own emergency numbers, what the reply must
     say, and how the hold lifts, word for word;
  4. for chest symptoms, fainting, a head injury, crisis words, fever and
     heat, puts a fixed card in front of the rider before the model's text,
     in the numbers of the rider's own country.
If the model's reply never comes (the provider fails, or it is cut off
before a word), fallback_reply answers in fixed words instead: a plain no to
riding on, the card's primary action, the safety net, the hold from the
record and how it lifts. Never "send it again".
Crisis and under-18 hits also email Gareth (the safeguarding protocol),
best effort, after the reply has started.

Some words of crisis are also how riders talk about a hard block ("I just
want it all to end, this block is brutal"). When every such phrase sits in a
sentence that names training or food, the hit is a distress_check: no card,
no hold, no email and no quiet window, just a SAFETY CONTEXT line telling the
coach to check in kindly and ask directly whether they're okay. Words about
ending their life, hurting themselves, not wanting to be here or riding into
a lorry always bring the full crisis card, whatever surrounds them.

A knock to the head about the house that the rider shrugs off ("hit my head
on the cupboard door, ouch, anyway") is a head_knock: recorded, with no card
and no hold, and the coach asks once about headache, sickness, dizziness or
confusion. Any crash, sign of a head injury or being knocked out in the same
message makes it a head injury. A head-injury hit that describes a fresh
crash or blow ("crashed again today") carries Hit.new_event, so the holds
code restarts the head-injury clock rather than reading it as the earlier
injury mentioned again.

A general or hypothetical question about a red flag ("what should I do if I
ever get chest pain?", "what are the signs of heatstroke?") is a
safety_question: no card, no hold and no record, just a SAFETY CONTEXT line
so the coach answers with the right safety facts and asks once whether it is
happening to them. Any sign in the message that it is happening to them now
(the same symptom said plainly, "it felt tight today", "again") makes it the
real red flag. An illness long over ("had covid in the summer, fully
recovered") is not a fever now.

The phrase lists lean towards firing: a false alarm costs the rider a tap on
"This was a mistake"; a miss can cost far more. The guards are there for the
idioms riders use every day ("chest strap", "killing myself on these
intervals", "I'm 15 minutes late"), not to second-guess real symptoms.

ReplyGuard is the other half: a sentence-by-sentence check on the coach's
own words on their way to the rider. It holds back any claim of an action no
tool confirmed, swaps any "tell me when you're cleared" for the one true way
a hold lifts, keeps other countries' numbers out of a safety reply, keeps
Gareth and any review out of a crisis reply, and adds any line the SAFETY LAW
requires that the reply left out.

This module also builds the safety summary every coach surface reads (the
hold, the screening, the layoff gate, the rider's country), so the chat,
briefings, goal reads and plan review all see one picture.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.models.safety import HealthScreening, SafetyEvent, SafetyHold
from app.models.user import User
from app.services import safety_service

logger = logging.getLogger(__name__)

# ── Where the rider is: the numbers and words that work there ───────────────

_UK = {"GB", "UK", "IM", "JE", "GG"}
_EUROPE_112 = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU",
    "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES",
    "SE", "IS", "LI", "NO", "CH",
}
_NORTH_AMERICA = {"US", "CA"}

# Services that only exist in the UK. Named to a rider anywhere else, the
# first number they try in a crisis fails.
_UK_ONLY = (
    r"\bnhs\b|\bsamaritans\b|\bshout\b|\b85258\b|\b116 ?123\b|\b111\b|"
    r"\bbeat\b|0808 801 0677|beateatingdisorders"
)

# Per country: the emergency number; where to go today; where heart symptoms
# or fainting are seen today (somewhere with an ECG, never a routine GP slot,
# so in France that's 15 or les urgences, not the médecin traitant); the
# urgent rung; the crisis lines; the words that must never reach that rider;
# and what a reply that already names the right place looks like (heart_rx,
# hospital_rx). "unknown" keeps the UK numbers with the EU and US alongside.
LOCALES: dict[str, dict] = {
    "UK": {
        "label": "the UK",
        "club": "a British Cycling club with a Go-Ride section",
        "emergency": "999",
        "emergency_short": "999",
        "emergency_rx": r"\b999\b",
        "hospital": "A&E",
        "heart_today": (
            "A&E or NHS 111 (in Northern Ireland, your GP out-of-hours service), "
            "not a routine GP appointment"
        ),
        "heart_rx": r"A&E|\b111\b|emergency department|out-of-hours",
        "hospital_rx": r"A&E|emergency department",
        "today": (
            "NHS 111 (call 111, or 111.nhs.uk in England) or a same-day GP "
            "appointment; in Northern Ireland, your GP or GP out-of-hours service"
        ),
        "today_short": "NHS 111",
        "gp": "your GP",
        "crisis": (
            "Samaritans are free, day or night, on 116 123, or text SHOUT to 85258."
        ),
        "danger": "If you're in immediate danger, call 999 or go to A&E.",
        "crisis_rx": r"116 ?123",
        "invalid": r"\b988\b|\b741741\b|\b3114\b",
        "swaps": (),
    },
    "IE": {
        "label": "Ireland",
        "club": "a Cycling Ireland club",
        "emergency": "112 or 999",
        "emergency_short": "112",
        "emergency_rx": r"\b(?:112|999)\b",
        "hospital": "the emergency department",
        "heart_today": "the emergency department, or your GP out-of-hours service",
        "heart_rx": r"emergency department|out-of-hours",
        "hospital_rx": r"emergency department",
        "today": "your GP, or your GP out-of-hours service",
        "today_short": "your GP",
        "gp": "your GP",
        "crisis": (
            "Samaritans are free, day or night, on 116 123, or text HELLO to 50808."
        ),
        "danger": "If you're in immediate danger, call 112 or 999.",
        "crisis_rx": r"116 ?123|50808",
        "invalid": r"\bnhs\b|\bshout\b|\b85258\b|\b111\b|\b988\b|\b741741\b|\bbeat\b|0808 801 0677",
        "swaps": ((r"\bA&E\b", "the emergency department"),),
    },
    "FR": {
        "label": "France",
        "club": "a cycling club affiliated to the FFC (the French cycling federation)",
        "emergency": "15 (SAMU) or 112",
        "emergency_short": "112",
        "emergency_rx": r"\b(?:15|112)\b",
        "hospital": "the emergency department (les urgences)",
        "heart_today": (
            "call 15 (SAMU) for advice, or go to les urgences (the emergency department)"
        ),
        "heart_rx": r"urgences|\b15\b|emergency department",
        "hospital_rx": r"urgences|emergency department",
        "today": "your médecin traitant, or 15 out of hours",
        "today_short": "your médecin traitant",
        "gp": "your doctor",
        "crisis": "Call 3114, free, day or night: it's France's suicide prevention line.",
        "danger": "If you're in immediate danger, call 15 or 112.",
        "crisis_rx": r"\b3114\b",
        "invalid": _UK_ONLY + r"|\b988\b|\b741741\b",
        "swaps": (
            (r"\b999\b", "15 or 112"),
            (r"\bA&E\b", "the emergency department"),
            (r"\bGPs?\b", "doctor"),
        ),
    },
    "EU": {
        "label": "the EU",
        "club": "a cycling club affiliated to your national cycling federation",
        "emergency": "112",
        "emergency_short": "112",
        "emergency_rx": r"\b112\b",
        "hospital": "the emergency department",
        "heart_today": "the emergency department or your out-of-hours doctor today",
        "heart_rx": r"emergency department|out-of-hours",
        "hospital_rx": r"emergency department",
        "today": "your doctor or out-of-hours service",
        "today_short": "a doctor",
        "gp": "your doctor",
        "crisis": "Your country's crisis line is listed at findahelpline.com.",
        "danger": "If you're in immediate danger, call 112.",
        "crisis_rx": r"findahelpline",
        "invalid": _UK_ONLY + r"|\b988\b|\b741741\b",
        "swaps": (
            (r"\b999\b", "112"),
            (r"\bA&E\b", "the emergency department"),
            (r"\bGPs?\b", "doctor"),
        ),
    },
    "US": {
        "label": "the US",
        "club": "a USA Cycling club",
        "emergency": "911",
        "emergency_short": "911",
        "emergency_rx": r"\b911\b",
        "hospital": "the ER",
        "heart_today": "the ER",
        "heart_rx": r"\bER\b|emergency room|emergency department",
        "hospital_rx": r"\bER\b|emergency room|emergency department",
        "today": "your doctor or urgent care",
        "today_short": "your doctor or urgent care",
        "gp": "your doctor",
        "crisis": (
            "Call or text 988, or chat at 988lifeline.org, free, day or night, or text "
            "HOME to 741741."
        ),
        "danger": "If you're in immediate danger, call 911 or go to the ER.",
        "crisis_rx": r"\b988\b",
        "invalid": _UK_ONLY,
        "swaps": (
            (r"\b999\b", "911"),
            (r"\bA&E\b", "the ER"),
            (r"\bGPs?\b", "doctor"),
        ),
    },
    "CA": {
        "label": "Canada",
        "club": "a cycling club affiliated to Cycling Canada",
        "emergency": "911",
        "emergency_short": "911",
        "emergency_rx": r"\b911\b",
        "hospital": "the emergency department",
        "heart_today": "the emergency department",
        "heart_rx": r"emergency department",
        "hospital_rx": r"emergency department",
        "today": "your doctor or a walk-in clinic",
        "today_short": "your doctor",
        "gp": "your doctor",
        "crisis": "Call or text 988, free, day or night.",
        "danger": "If you're in immediate danger, call 911 or go to the emergency department.",
        "crisis_rx": r"\b988\b",
        "invalid": _UK_ONLY + r"|\b741741\b",
        "swaps": (
            (r"\b999\b", "911"),
            (r"\bA&E\b", "the emergency department"),
            (r"\bGPs?\b", "doctor"),
        ),
    },
    "unknown": {
        "label": "unknown",
        "club": "a cycling club (in the UK, a British Cycling club with a Go-Ride section)",
        "emergency": "999 (112 in the EU, 911 in the US)",
        "emergency_short": "999",
        "emergency_rx": r"\b(?:999|112|911)\b",
        "hospital": "A&E (the emergency department)",
        "heart_today": "A&E or NHS 111 in the UK, or an emergency department elsewhere",
        "heart_rx": r"A&E|\b111\b|emergency department",
        "hospital_rx": r"A&E|emergency department",
        "today": "NHS 111 in the UK, or your doctor",
        "today_short": "NHS 111 in the UK, or a doctor",
        "gp": "your GP",
        "crisis": (
            "In the UK and Ireland, Samaritans are free, day or night, on 116 123, "
            "and in the UK you can text SHOUT to 85258. In the US, call or text 988. "
            "Elsewhere, findahelpline.com lists your local line."
        ),
        "danger": (
            "If you're in immediate danger, call your local emergency number (999 "
            "in the UK, 112 in the EU, 911 in the US)."
        ),
        "crisis_rx": r"116 ?123|\b988\b|findahelpline",
        "invalid": None,
        "swaps": (),
    },
}


def locale_for(country: str | None) -> str:
    """Which set of numbers a rider gets: UK, IE, FR, EU, US, CA or unknown."""
    code = (country or "").strip().upper()
    if code in _UK:
        return "UK"
    if code in ("IE", "FR", "US", "CA"):
        return code
    if code in _EUROPE_112:
        return "EU"
    return "unknown"


def _local(country: str | None) -> dict:
    return LOCALES[locale_for(country)]


# ── The fixed cards, in the rider's own numbers ─────────────────────────────

# Each card names one situation and says what to do on every branch of it,
# so a rider who reads nothing else still has the right number.
_CARD_TEXT = {
    "chest": (
        "If you have chest pain, pressure or tightness, pain in your arm, jaw, "
        "neck or back that comes with effort or breathlessness, or you're short of "
        "breath at rest, call {emergency} now if it's still happening, lasted more "
        "than a few minutes, or came with sweating, sickness, faintness or "
        "breathlessness. Don't ride or drive. If it has fully settled, you still "
        "need to be seen today: {heart_today}. Don't ride until a doctor has "
        "checked you."
    ),
    "faint": (
        "If you passed out, or it came with chest pain, a racing or irregular "
        "heartbeat or breathlessness, call {emergency} now. Otherwise stop, lie "
        "down with your legs raised, don't stay alone and don't drive. Get seen "
        "today for an ECG, a heart tracing: {heart_today}. Don't ride until a "
        "doctor has checked your heart."
    ),
    "heart": (
        "If your heart is racing or irregular right now, or it came with chest "
        "pain, faintness or breathlessness, stop and call {emergency} now. If "
        "it has settled, get seen today for an ECG, a heart tracing: "
        "{heart_today}. Don't ride until a doctor has checked your heart."
    ),
    "head": (
        "After a blow to the head: if you were knocked out, have a headache "
        "that hasn't gone away, have been sick, have a gap in your memory or "
        "take blood thinners, go to {hospital} today, and don't drive yourself. "
        "Call {emergency} for a fit, drowsiness, confusion, weakness, slurred "
        "speech, trouble with vision or balance, a headache that gets worse, "
        "repeated vomiting or clear fluid from the nose or ears. Otherwise get "
        "advice today from {today_short}. Don't be alone for the next 24 hours, "
        "don't drink alcohol, and don't ride, train or race until a doctor has "
        "cleared you. If you were wearing a helmet, replace it, even if it looks "
        "fine."
    ),
    "crisis": (
        "I'm really glad you told me. If you're thinking about ending your life "
        "or hurting yourself, please talk to someone now. {crisis} {danger}"
    ),
    "fever": (
        "With a fever, don't ride or train at all: rest until it has been gone for "
        "24 hours without paracetamol or ibuprofen. Call {emergency} if you're "
        "struggling to breathe, your lips turn blue, you're confused or you have "
        "chest pain. Get seen today ({today_short}) if you cough up blood, the "
        "fever lasts more than three days or you're getting worse."
    ),
    "heat": (
        "In heat like this, know the signs. If you or anyone with you becomes "
        "confused or slurred, has hot dry skin, collapses or has a fit, that's "
        "heatstroke: call {emergency} and cool them down while you wait. A "
        "headache, dizziness, nausea, cramps or heavy sweating is heat "
        "exhaustion: stop, get into the shade, cool down and drink. If you're "
        "not better within 30 minutes, call {emergency}."
    ),
}

# How the app styles each card. The app knows "emergency" (red, with a call
# button) and "crisis"; "warning" is the quieter fever and heat card.
CARD_STYLE = {
    "chest": "emergency",
    "faint": "emergency",
    "heart": "emergency",
    "head": "emergency",
    "crisis": "crisis",
    "fever": "warning",
    "heat": "warning",
}

# The card each red flag brings. One emergency-style card at most: the
# highest of these is shown, since each one already carries the call.
CARD_FOR_KIND = {
    "chest_pain": "chest",
    "fainting": "faint",
    "palpitations": "heart",
    "head_injury": "head",
    "crisis": "crisis",
    "fever": "fever",
    "heat": "heat",
}
_EMERGENCY_CARD_ORDER = ("chest", "faint", "heart", "head")
# The card styles the chat page actually shows (SafetyCard.tsx). Only these
# count as already said when the reply check decides what a reply still
# needs: until the app shows "warning" cards, the reply carries the fever and
# heat lines itself.
RENDERED_CARD_STYLES = frozenset({"emergency", "crisis"})
_STYLE_ORDER = ("emergency", "crisis", "warning")


def card_text(card: str, country: str | None = None) -> str:
    """A fixed card, word for word, in the numbers for the rider's country."""
    return _CARD_TEXT[card].format(**_local(country))


# The country-unknown versions: what a rider with no country on file sees.
EMERGENCY_CARD = card_text("chest")
CRISIS_CARD = card_text("crisis")
CARDS = {"emergency": EMERGENCY_CARD, "crisis": CRISIS_CARD}

# ── What each red flag does ─────────────────────────────────────────────────

# emergency: a card now. crisis: a card now and Gareth is emailed. urgent:
# hold_all, triage today. minor: the rider may be under 18, the account is
# held and Gareth is emailed. info: the coach needs to know, and may hold
# hard work.
SEVERITIES = ("emergency", "crisis", "urgent", "minor", "info")

HOLD_FOR = {
    "chest_pain": "hold_all",
    "palpitations": "hold_all",
    "fainting": "hold_all",
    "head_injury": "hold_all",
    "fever": "hold_all",
    "minor": "hold_all",
    "injury": "easy_only",
    "pregnancy": "easy_only",
    "medication": "easy_only",
    "condition": "easy_only",
    "layoff": "easy_only",
}

HOLD_REASONS = {
    "chest_pain": "Chest pain, pressure or tightness, or breathlessness at rest, mentioned in chat",
    "palpitations": "Palpitations mentioned in chat",
    "fainting": "Fainting or nearly fainting mentioned in chat",
    "head_injury": "A blow to the head or a damaged helmet mentioned in chat",
    "fever": "Fever or illness below the neck mentioned in chat",
    "minor": "The rider said they are under 18. Forma is for adults",
    "injury": "An injury or worrying pain mentioned in chat",
    "pregnancy": "Pregnancy, or a recent birth, mentioned in chat",
    "medication": "A medicine that changes how to train mentioned in chat",
    "condition": "A health condition mentioned in chat",
    "layoff": "Easing back in after a break from riding",
}

# Where a hold says it came from. A break the rider tells us about is not a
# symptom: the app shows it as "Easing back in", not as a medical hold.
HOLD_SOURCE_FOR = {"layoff": "layoff"}

# Which part of the SAFETY LAW the coach follows for each red flag.
LAW_RULE = {
    "chest_pain": "2a",
    "palpitations": "2a",
    "fainting": "2b",
    "head_injury": "2c",
    "fever": "2d",
    "injury": "2e",
    "pregnancy": "2f",
    "medication": "2g",
    "condition": "2g",
    "restriction": "2h",
    "heat": "2j",
    "layoff": "2k",
    "big_jump": "2l",
    "head_knock": "2c, only if a sign of a head injury follows",
    "crisis": "rule 4",
    "minor": "2n",
    "responsibility": "rule 6",
    "distress_check": "rule 4, only if they say they're not okay",
    "safety_question": "3, and the red flag's own part only if it is happening to them",
}

# Crisis words with clear training context, and general questions about a
# red flag: no card, no hold, no email to Gareth and no quiet window. The
# coach checks in and asks directly, or answers with the safety facts.
SOFT_KINDS = frozenset({"distress_check", "safety_question"})
# The red flags a general or hypothetical question can be about: the ones
# that bring a card or a hold. Never crisis or an age: those are always real.
QUESTION_KINDS = frozenset(
    {"chest_pain", "palpitations", "fainting", "head_injury", "fever", "heat", "injury",
     "pregnancy"}
)
# Kinds that are not about the rider's own body, so leave no safety record.
UNRECORDED_KINDS = frozenset({"safety_question"})
# The one line a check-in reply can't go without, and the check that it's there.
DISTRESS_LINE = "Separately from the training: are you okay?"
# The line a reply to a shrugged-off knock to the head can't go without.
HEAD_KNOCK_LINE = (
    "If you get a headache, feel sick, dizzy or confused, or your vision blurs after "
    "that knock, don't ride, and get checked today ({today_short})."
)
# Kinds that bring no card and no hold: the context and the fixed reply say
# nothing about either.
QUIET_KINDS = frozenset({"distress_check", "head_knock", "safety_question"})
_ASKED_IF_OKAY = (
    r"\b(?:are|r) (?:you|u) (?:\w+ )?(?:ok|okay|alright|all right|doing (?:ok|okay|alright|"
    r"all right))\b|\b(?:you|u) (?:ok|okay|alright|all right)\s*\?|\bhow are you (?:doing|"
    r"feeling|holding up|getting on|coping)\b|\bhow(?:'s| is) (?:everything|life|things)\b"
)

ALERT_KINDS = frozenset({"crisis", "minor"})

# About a standing fact rather than a new symptom. Once a doctor has cleared
# the rider for one named medicine or condition, mentioning that same one
# again is not news, and re-holding them every time they say "bisoprolol"
# would teach them to stop telling us. Only the same named term counts (a
# clearance for asthma never covers cardiomyopathy), and only for a year.
STANDING_KINDS = frozenset({"medication", "condition", "pregnancy"})
CLEARANCE_COVERS_DAYS = 365

# A rider who marked a chat hold "This was a mistake" is not re-held for the
# same words within this window. Only for the kinds where a misread is the
# likely story and a missed hold costs little: a chest symptom, a faint, a
# head injury, a fever, a medicine, a pregnancy or a child reopens every
# time, because a real one must never be waved through by an old tap.
MISTAKE_QUIET_DAYS = 30
QUIET_AFTER_MISTAKE = frozenset({"injury", "layoff"})


# ── How a hold lifts: one sentence per kind, served from code ───────────────

_LIFT = {
    "default": (
        "Once a doctor has checked you over and cleared you, tap I've been "
        "cleared on the hold notice (or Settings, then Health) and add "
        "anything they told you to avoid."
    ),
    # Chest symptoms, palpitations and fainting: the record says what was
    # checked, not just that someone said yes.
    "heart": (
        "Once a doctor has checked your heart and cleared you, tap I've been "
        "cleared on the hold notice (or Settings, then Health), and add the "
        "tests they did and anything they told you to avoid."
    ),
    # UK grassroots concussion guidance: symptom-free first, then a gradual
    # return, and nothing high-risk before day 21.
    "head_injury": (
        "Once a doctor has checked you and you've had no symptoms for 24 to 48 "
        "hours, tap I've been cleared on the hold notice (or Settings, then "
        "Health). Then build back gradually from easy riding, drop back a step "
        "if any symptom returns, and no racing or group riding before day 21 "
        "after the injury."
    ),
    "injury": (
        "Once a physio or doctor has looked at it and cleared you, tap I've "
        "been cleared on the hold notice (or Settings, then Health) and add "
        "anything they told you to avoid."
    ),
    "pregnancy": (
        "Once your midwife or obstetrician has said you can ride harder, tap "
        "I've been cleared on the hold notice (or Settings, then Health) and "
        "add the limits they gave you."
    ),
    # A fever lifts on the rider's own word ("My fever has gone"), never a
    # doctor's, and leaves an easy week that ends by itself.
    "fever": (
        "When the fever has been gone for 24 hours without paracetamol or "
        "ibuprofen and your chest has cleared, tap My fever has gone on the hold "
        "notice. Your first week back after that is easy riding only, with no "
        "intervals or tests, and it ends by itself."
    ),
    "screening": (
        "Once a doctor has cleared you, tap I've been cleared on the hold notice "
        "(or Settings, then Health) and add anything they told you to avoid. If "
        "a health answer was wrong, change it in Settings, then Health."
    ),
}
_LIFT_KEY = {
    "chest_pain": "heart",
    "palpitations": "heart",
    "fainting": "heart",
}

# A break isn't a medical hold, but a break for a medical reason needs a
# doctor first (terms section 2), so the layoff sentence says both.
_LAYOFF_LIFT = (
    "This hold keeps your first {weeks} back to easy riding by feel{until}. If "
    "you stopped because of illness, injury, surgery, a heart problem, "
    "concussion or pregnancy, see a doctor before you start."
)


def layoff_weeks(days: float | None) -> str:
    """How long the easy start lasts, in words: two weeks after four weeks or
    more off, four after three months or more."""
    if days is None:
        return "two weeks (four after three months or more off)"
    return "four weeks" if days >= safety_service.LONG_LAYOFF_GAP_DAYS else "two weeks"


def _as_datetime(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def lift_sentence(hold) -> str | None:
    """The one way to say how this hold lifts. None for a hold the rider
    can't lift (an under-18 account): the coach says nothing about lifting."""
    red_flag = getattr(hold, "red_flag", None)
    source = getattr(hold, "source", None)
    if red_flag == "minor" or source == "admin":
        return None
    ends = _as_datetime(getattr(hold, "expires_at", None))
    if source == "layoff" or red_flag == "layoff":
        opened = _as_datetime(getattr(hold, "opened_at", None))
        if ends is not None and opened is not None:
            span = round((ends - opened).total_seconds() / 86400)
            weeks = "four weeks" if span >= safety_service.LONG_LAYOFF_GATE_DAYS else "two weeks"
        else:
            weeks = layoff_weeks(layoff_days_from(getattr(hold, "note", None) or ""))
        until = f", until {ends.day} {ends.strftime('%B')}" if ends is not None else ""
        return _LAYOFF_LIFT.format(weeks=weeks, until=until)
    if ends is not None:
        when = f"{ends.day} {ends.strftime('%B')}"
        if red_flag == "fever":
            return (
                "Your first week back after the fever is easy riding only, with no "
                f"intervals or tests. It ends by itself on {when}."
            )
        if red_flag == "head_injury":
            # The easy hold ends HEAD_EASY_DAYS after the injury, and racing
            # waits until HEAD_NO_RACING_DAYS after it
            # (safety_service.lift_head_injury, no_racing_or_group_until).
            back = ends + timedelta(
                days=safety_service.HEAD_NO_RACING_DAYS - safety_service.HEAD_EASY_DAYS
            )
            return (
                f"Easy riding only until {when} after your head injury, then build "
                f"back gradually. No racing or group riding before "
                f"{back.day} {back.strftime('%B')}."
            )
        return f"This hold ends by itself on {when}."
    if source == "screening":
        return _LIFT["screening"]
    key = _LIFT_KEY.get(red_flag or "", red_flag or "")
    return _LIFT.get(key, _LIFT["default"])


# ── The phrase lists ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Rule:
    kind: str
    severity: str
    patterns: tuple
    # (where, regex): "before" / "after" search the few words either side of
    # the hit, inside its clause; a guard that matches voids the hit.
    guards: tuple = ()
    negatable: bool = True  # "no chest pain" is not chest pain
    third_party: bool = True  # "my dad has a heart condition" is not the rider
    # Words that are crisis words on their own but often training talk: when
    # every match comes with clear training context ("I want it all to end,
    # this block is brutal"), the hit becomes this gentler kind instead.
    soft_kind: str | None = None


def _rx(*patterns: str) -> tuple:
    return tuple(re.compile(p) for p in patterns)


def _g(where: str, pattern: str) -> tuple:
    return (where, re.compile(pattern))


_BODY = (
    r"knee|knees|back|lower back|hip|hips|ankle|ankles|achilles|shoulder|neck|"
    r"wrist|foot|feet|groin|hamstring|hamstrings|calf|calves|quad|quads|elbow|"
    r"heel|it ?band|itb|glute|shin|shins"
)
_BONE = (
    r"collarbone|clavicle|wrist|arm|elbow|hand|finger|thumb|rib|ribs|hip|pelvis|"
    r"leg|femur|kneecap|knee cap|ankle|foot|toe|scaphoid|shoulder|vertebra|"
    r"vertebrae|neck|back|jaw|nose|bone|tibia|fibula|humerus"
)
# Words that turn "killing myself", "want to die" and the like into effort.
_EFFORT = (
    r"climb|climbs|hill|hills|interval|intervals|effort|efforts|session|ride|"
    r"sprint|sprints|race|rep|reps|set|sets|workout|turbo|trainer|segment|kom|"
    r"qom|strava|descent|wheel|pace|watts|ftp|zwift|laughing|training|bike|"
    r"group ride|chaingang|chain gang|chaingang|club run|crit|tt|time trial|"
    r"ramp|test|rollers|gym|squats"
)

# Kit worn on or near the chest, and the head units that read it. A strap
# that is tight, died or dropped out is not a symptom.
_KIT = (
    r"straps?|belts?|bands?|monitors?|hrms?|sensors?|batter(?:y|ies)|jerseys?|bibs?|"
    r"bib shorts|vests?|jackets?|gilets?|kit|base ?layers?|skinsuits?|garmin|wahoo|"
    r"polar|whoop|coros|head unit|computer|harness|backpack|rucksack|camelbak|zip"
)
# What a device reports, rather than what a body feels.
_READOUT = r"data|trace|reading|readings|graph|file|recording|numbers|zones?|rate"

# Where heart pain spreads: the arm, the jaw, the neck, the back. The jaw, the
# left arm or both arms with effort is heart pain until a doctor says it
# isn't; a neck, a back or one arm only when it spreads or comes with
# breathlessness, since riders ache there after every long ride.
_ACHE_SITE = (
    r"(?:(?:my|the|left|right|both|upper|lower)\s+)*(?:arms?|jaw|neck|back|shoulders?|"
    r"shoulder ?blades?)"
)
_ACHE_SITES = rf"{_ACHE_SITE}(?:(?:,|,? and|,? or) {_ACHE_SITE})*"
# Not numbness or tingling: in a rider's hands and arms that is the bars
# pressing on a nerve, far more often than the heart.
_ACHE_WORDS = (
    r"aches?|ached|aching|achy|hurts?|hurting|hurt|pains?|painful|tight|tightness|heavy|"
    r"heaviness|pressure|discomfort"
)
_STRONG_SITE_RX = re.compile(r"\bjaw\b|\bleft arm\b|\bboth arms\b")
_SPREADING_RX = re.compile(
    r"\b(?:spread\w*|radiat\w*|shoot\w*|going|goes|went|travell?\w*|mov(?:es|ed|ing))"
    r"(?: \w+){0,2}? (?:down|up|into|to|through|across) (?:my |the )?(?:left )?"
    r"(?:arms?|jaw|neck|shoulders?|back)\b"
)
# Breathless beyond the effort: not "out of breath on the climb".
_BREATHLESS_RX = re.compile(
    r"\b(?:short of breath|shortness of breath|breathless(?:ness)?|struggl\w* to breathe|"
    r"(?:can't|cannot|couldn't|could not) (?:breathe|catch my breath|get my breath)|"
    r"hard to breathe|gasping)\b"
)
_HEART_EFFORT_RX = re.compile(
    r"\b(?:effort|efforts|exertion|exercise|exercising|climb|climbs|climbing|hill|hills|"
    r"interval|intervals|sprint|sprints|rep|reps|ride|rides|riding|rode|session|workout|"
    r"training|turbo|race|racing|threshold|vo2\w*|ftp|test|push|pushing|pushed|going hard|"
    r"went hard|go hard|hard|on the bike)\b"
)
# An ache with an everyday cause: clenching, gripping the bars, the gym, a
# crash, the bike's position, a bad night.
_ACHE_EXPLAINED_RX = re.compile(
    r"\b(?:clench\w*|grind\w*|grit\w*|teeth|tooth|dentist|wisdom|grip|gripping|bars|"
    r"handlebars?|drops|hoods|cobbles|gravel|vibration|pothole|aero|position|bike fit|"
    r"saddle|gym|weights|lifting|lifted|push-?ups|press-?ups|pull-?ups|bench|curls|crash\w*|"
    r"fell|fall|came off|slept|sleeping|pillow|desk|laptop|typing|carrying|backpack|"
    r"rucksack|stretch\w*|massage|foam roll\w*|physio)\b"
)

# "Blacked out the dates in my diary", "blacked out the logos on my frame":
# blacking something out, not the rider.
_NOT_BLACKED_OUT_OBJECT = (
    r"(?! (?:the|my|our|your|his|her|their|those|these|some|all|a|an|all the|all my) "
    r"(?:\w+ )?(?:dates?|diary|diaries|calendar|windows?|names?|text|numbers?|details|faces?|"
    r"screen|bits?|parts?|sections?|lines?|words?|slots?|holidays?|room|car|bike|frame|"
    r"wheels?|rims?|logos?|decals?|stickers?|kit|lights?|trim|plates?|spokes|hubs|bars|stem|"
    r"entries|boxes|squares|cells)\b)"
)

_FAINT_GUARDS = (
    # Asleep on the sofa after a long ride.
    _g("after", r"^\s*(?:on|in) (?:the |my )?(?:sofa|couch|bed|settee)\b"),
    _g("after", r"^\s*(?:asleep|early|for a nap|for an hour|in front of)"),
    _g("after", r"^\s*(?:the |some )?(?:bottles|flyers|leaflets|drinks)\b"),
    # A figure of speech: "nearly fainted when I saw the price of the new
    # Di2", "almost passed out laughing".
    _g("after", r"^\s*(?:\w+\s+)?(?:laugh\w*|giggl\w*|with (?:laughter|shock|surprise|"
                r"excitement|joy|embarrassment)|in shock|from shock|at the (?:price|cost|bill|"
                r"sight|news|thought|idea)|(?:seeing|reading|hearing) (?:the|that|my|how much)\b)"),
    # Shock at something seen or heard, unless it's blood, a needle or an
    # injury (a faint at the sight of blood is still a faint).
    _g("after", r"^\s*when (?:i|we) (?:saw|heard|read|got|opened|looked at|checked|found out|"
                r"realised|realized|noticed|stood on)\b(?![^.!?;\n]{0,30}\b(?:blood|needles?|"
                r"injection|jab|wound|gash|bone|injur\w*|cut)\b)"),
    # The head unit or the screen went black, not the rider.
    _g("before", r"\b(?:garmin|wahoo|polar|coros|screen|display|head unit|computer|phone|"
                 r"strap|monitor|hrm|sensor|power meter|lights?|zwift|app|tv|laptop|ipad)\s+"
                 r"(?:has |had |just |completely |totally |then |also )?$"),
)
# Losing consciousness, in any words: coming round, being out for seconds or
# minutes, blanking out, waking up on the floor. Each pattern names its
# match "loc"; after a crash or a fall it is a head injury (knocked out),
# otherwise a faint (checked below: _incident).
_DURATION_N = (
    r"a few|a couple of|a couple|several|some|\d+|a|an|one|two|three|four|five|six|ten|"
    r"fifteen|twenty|thirty|forty|half a"
)
_LOC_PATTERNS = (
    # "came to on the garage floor", "when I came round", "came round a few
    # seconds later". Not "came to the club run", "came round the corner",
    # "came to the conclusion" or "came round to the idea".
    r"\b(?P<loc>(?:came|come|coming|comes) (?:to|round|around))\b"
    r"(?=\s*(?:$|[.,!?;:)\n]|(?:an? |the )?(?:ambulance|paramedics?|police|crowd|doctor|"
    r"nurse|marshals?|people|someone|somebody|everyone)\b|i (?:was|'d|had|found|couldn't|"
    r"didn't|saw|felt|realised|"
    r"realized|could|knew|heard|noticed|wasn't)\b|on (?:the |my |a )?(?:\w+ )?(?:floor|ground|"
    r"deck|road|tarmac|"
    r"verge|grass|pavement|back|front|side)\b|in (?:the )?(?:back of )?(?:the |an? )?(?:ambulance|"
    r"hospital|a&e|road|"
    r"ditch|hedge|verge|gutter|recovery position)\b|with (?:people|someone|somebody|paramedics|"
    r"a crowd|my \w+|everyone|blood|a paramedic|an ambulance)\b|and (?:i|i'd|i'm|was|there|"
    r"didn't|couldn't|had|my|people|someone|everyone|found|realised|realized|felt|saw|the "
    r"(?:ambulance|paramedics?|bike))\b|lying\b|face\b|to find\b|to see\b|(?:after )?(?:a few|"
    r"several|\d+|some|a couple of|a|about \w+|maybe \w+) (?:seconds?|secs?|minutes?|mins?|"
    r"moments?) (?:later|after)\b|later\b|surrounded\b|not knowing\b|confused\b|dazed\b|"
    r"next to\b|beside\b|under\b|covered\b|feeling\b|again\b|properly\b|fully\b|"
    r"straight away\b|quickly\b))",
    # "I was out for a few seconds", "out cold for about a minute". A few
    # minutes out only counts with something more (checked below).
    r"\b(?:i was|i'd been|i had been|was|been|must have been|might have been|could have been|"
    r"knocked) (?:out|unconscious|out cold|spark "
    r"out|sparko)(?: cold)? (?:for )?(?:about |around |roughly |maybe |nearly |almost |at least |"
    rf"a good |over |just |only |what felt like )?(?P<loc>(?:{_DURATION_N}) ?(?P<locunit>"
    r"seconds?|secs?|moments?|minutes?|mins?)|a (?:second|moment|minute|bit|while))\b",
    # "went unconscious", "I was out cold", "lay unconscious".
    r"\b(?P<loc>(?:went|go|going|was|were|i was|been|lay|lying|left|found|knocked|rendered|"
    r"fell) (?:briefly |completely |totally |properly |fully |just )?(?:unconscious|"
    r"out cold|spark out|sparko))\b",
    # "blanked out completely", but not "blanked out the dates" or "my mind
    # blanked out on her name".
    r"\b(?<!nearly )(?<!almost )(?<!mind )(?<!brain )(?<!memory )(?P<loc>blanked out)\b"
    + _NOT_BLACKED_OUT_OBJECT
    + r"(?! (?:on|about|when (?:asked|someone))\b)",
    r"\b(?P<loc>los(?:t|e|ing) (?:con(?:s|sc|c)?ious(?:ness|nes)?|consiousness|consciousnes))\b",
    # "woke up on the garage floor", "found myself on the ground". Not
    # waking on a mate's floor after a party.
    r"\b(?P<loc>(?:woke|wake|waking|woken|came) up (?:\w+ ){0,2}?(?:on|in) (?:the |a )?"
    r"(?:\w+ )?(?:floor|ground|deck|road|tarmac|verge|grass|pavement|ditch|gutter))\b",
    r"\b(?P<loc>found myself (?:lying |sat |sitting |flat |sprawled |face down )?(?:on|in) "
    r"(?:the |a )?(?:\w+ )?(?:floor|ground|deck|road|tarmac|verge|grass|pavement|ditch|"
    r"gutter))\b",
    # "next thing I knew I was on the ground with people over me". Not "next
    # thing I knew I was in the break".
    r"\b(?P<loc>next thing i (?:knew|know|remember))\b(?=[^.!?\n]{0,50}?\b(?:floor|ground|"
    r"deck|road|tarmac|lying|ambulance|hospital|paramedics?|verge|ditch|on my back|people "
    r"(?:were )?(?:standing|around|over)|someone (?:was )?(?:standing|over|asking|shaking))\b)",
)
# Waking on a floor by choice, not coming round.
_LOC_SLEPT_RX = re.compile(
    r"\b(?:party|night out|drinks|drunk|pub|sleepover|camping|tent|sleeping bag|air ?bed|"
    r"mattress|slept|crashed at|crashed on|hostel|festival|sofa ?bed|nap|napping|dozed|"
    r"fell asleep)\b"
)
# Others in the scene, not the rider: "we came round the corner".
_LOC_NOT_RIDER_RX = re.compile(
    r"\b(?:we|they|he|she|it|you|bunch|group|peloton|car|cars|van|lorry|bus|riders?|lads|"
    r"guys|everyone|people|leaders?|break|mates?|friends?|wife|husband|partner|dad|mum)\s+"
    r"(?:\w+\s+)?$"
)
# Between the words of being out and a crash told after them: the crash
# came first ("out for a minute after I hit the deck").
_CRASH_FIRST_RX = re.compile(r"\b(?:after|when|because|from|since|as|following|once)\b")
# A few minutes out: a ride, unless something says it was being knocked out.
# A gap in memory, in any words. A head injury only after a crash, a
# collision or a fall (checked below: _incident); "I don't remember the ride
# back being this hilly" is not one.
_AMNESIA_PATTERNS = (
    r"\b(?P<amn>(?:don't|dont|do not|can't|cant|cannot|can not|couldn't|could not) "
    r"(?:really |even |actually |properly |fully )?(?:remember|recall)(?: [\w']+){0,2}? "
    r"(?:how i got (?:home|back|there|here|to (?:the|a|my) \w+)|getting (?:home|back|"
    r"there)|(?:the |my )?(?:ride|way|trip|journey|walk|drive|spin|roll|cycle) (?:home|back)"
    r"(?! (?:being|was|is)\b)|the rest of (?:the|my) (?:ride|day|evening|morning|afternoon|"
    r"race)|(?:anything|much|everything|a thing|a lot) (?:after|until|till|from|between|for "
    r"(?:a|the) (?:bit|while|few))|(?:the )?(?:next|last|following) (?:few |couple of |\d+ |"
    r"ten |five |twenty |thirty )?(?:seconds|minutes|mins|hours|bit|part|mile|miles|km|"
    r"kilometres)|how i ended up|where i was|what i was doing|the ambulance|being in the "
    r"ambulance|arriving|the hospital|who (?:helped|picked) me|how long i was))",
    r"\b(?P<amn>no (?:idea|memory|recollection) (?:of )?(?:how i got (?:home|back|there|here)|"
    r"getting (?:home|back)|(?:the )?(?:ride|way|trip|journey) (?:home|back)|the rest of the "
    r"(?:ride|day)|what happened (?:next|after)))",
    r"\b(?P<amn>lost (?:a |an )?(?:big |huge |massive |whole )?(?:chunk|bit|piece|block|stretch"
    r"|few minutes|couple of minutes) of (?:time|memory|the (?:ride|day|afternoon|morning|"
    r"evening)))\b(?! (?:to|on|in|at|against|behind|fixing|sorting|waiting|chasing)\b)",
    r"\b(?P<amn>(?:a |big |huge |massive )?(?:chunk|gap|blank|hole|blank bit) (?:in|missing "
    r"from|out of) (?:my )?(?:memory|the (?:day|ride|afternoon|morning|evening)))\b",
    r"\b(?P<amn>missing time)\b(?! (?:to|on|in|at|against|behind)\b)",
    r"\b(?P<amn>(?:everything|it|all|the rest|the next bit|what happened) (?:after|from|"
    r"following|since) (?:that|the crash|the fall|it|then|there|the impact|hitting \w+)"
    r"(?: \w+){0,2}? (?:is|was|'s|are) (?:all |just |completely |a bit |pretty )?(?:a )?"
    r"(?:blank|blur|haze|black hole|gone|missing))\b",
    r"\b(?P<amn>(?:it's|it is|everything's|everything is|its|all) (?:all |just )?(?:a )?"
    r"blank (?:after|from|between|until|till))\b",
)
_LOC_MINUTES_BACKED_RX = re.compile(
    r"\b(?:said|says|told|apparently|reckon\w*|according to|someone|somebody|people|mates?|"
    r"wife|husband|partner|paramedics?|ambulance|floor|ground|deck|unconscious|out cold|"
    r"came to|came round|collaps\w*|faint\w*|passed out|blacked out|woke)\b"
)

_N = (
    r"\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"a few|a couple of|a couple|several"
)
_WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "a few": 3, "a couple of": 2, "a couple": 2, "several": 3,
}
_SOON = (
    r"next (?:week|weekend|saturday|sunday|month)|this (?:weekend|saturday|sunday|week)|"
    r"tomorrow|on (?:saturday|sunday)|in (?:\d+|two|three|four|five|six|seven|ten|"
    r"a few|a couple of) days|in (?:a|one|two) weeks?"
)

_I_AM = r"(?:i'm|i am|im)"
_AGE_WORD = r"1[0-7]|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen"
_AGE_WORDS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
}
# Words after a number that make it a measure, not an age: a weight, a
# distance, a time, a temperature, a place in a league.
_NOT_AN_AGE = (
    r"%|percent|per cent|kg|kgs|kilos?|km|kms|k\b|miles?|mi\b|"
    r"minutes?|mins?|min\b|hours?|hrs?|hr\b|h\b|days?|weeks?|wks?|months?|"
    r"stone|st\b|lbs?|pounds|watts?|w\b|bpm|th\b|seconds?|secs?|s\b|kph|"
    r"mph|w/kg|points?|places?|positions?|spots?|"
    r"degrees?|deg\b|°|º|c\b|celsius|centigrade|"
    r"metres?|meters?|m\b|x\b|\.\d|:\d|/|-\d|rpm|kj|k?cals?|calories|psi|reps?|laps?|"
    r"mm\b|cm\b"
)
# "I'm 16 and 18 watts up": the second number's unit belongs to the first too,
# so "16" is a measure. Also "16 or 17 kg", "16 to 18 km", "16-18 bpm".
_SHARED_UNIT = (
    rf"^\s*(?:and|or|to|-|/|,)\s*(?:a half|a bit|a quarter|three quarters|"
    rf"\d+(?:\.\d+)?)\s*(?:{_NOT_AN_AGE})"
)
# An age taken back at once: "I'm 17, no wait 47", "17... sorry 47". Read
# past the end of the clause, since "..." ends one. A correction to a future
# birthday ("I'm 17, actually 18 in May") is still 17.
_AGE_CORRECTION = re.compile(
    r"^[\s.,!;:…\-]*(?:(?:no|nope|wait|sorry|oops|typo|i mean|i meant|meant|actually|"
    r"correction|err|erm|(?:that )?should (?:say|be|read)|i meant to (?:say|type|write))"
    r"\b[\s.,!;:…\-]*){1,4}"
    r"(?:i'm |i am |im )?(?P<fixed>\d{1,2})\b"
    rf"(?!\s*(?:{_NOT_AN_AGE}|next|this|in\b|on\b|soon|tomorrow|later|by\b))"
)
# A past event after a bare age: "aged 16 I broke my collarbone", "as a 16
# year old I raced". Not a regular verb ending in -eed ("I need"), and not
# when the event is recent ("aged 16, I had my first race last week").
_PAST_EVENT = re.compile(
    r"^\s*,?\s*(?:and |then |when )?i (?:\w+ ){0,2}?(?:was|were|had|did|broke|rode|won|got|"
    r"went|fell|came|took|lost|learnt|bought|made|became|left|quit|ran|began|tore|"
    r"used to|\w{2,}(?<!e)ed)\b"
)
_RECENT = re.compile(
    r"\b(?:last (?:week|weekend|month|night|saturday|sunday|season)|yesterday|today|"
    r"this (?:week|year|season|morning|month)|recently|just|so far|now)\b"
)

# A pregnancy, with the week count and any word that makes it a new one kept
# in what matched: "12 weeks pregnant", "pregnant again, 8 weeks", "pregnant
# with our second". Not "a pregnant pause".
_WEEKS_N = (
    r"\d{1,2}(?:\.5)?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
)
_AGAIN_WORDS = (
    r"again|another|second|third|fourth|fifth|sixth|next|new|2nd|3rd|4th|5th|6th"
)
_PREGNANCY = (
    # "12 weeks pregnant", "3 months pregnant"
    rf"\b(?:(?P<wb>{_WEEKS_N})(?: and a half)?[- ](?P<ub>weeks?|wks?|months?)[- ])?"
    r"pregnan(?:t|cy)\b(?! pauses?\b)"
    # "again", "with our second", "with baby number two", "for the third time"
    r"(?: (?P<again>again)\b"
    r"| with (?:my |our |a |the |baby |child )?(?:number |no\.? ?)?(?P<nth>second|third|"
    r"fourth|fifth|sixth|next|another|2nd|3rd|4th|5th|6th|two|three|four|five|six|[2-6])\b"
    r"| for the (?P<time>second|third|fourth|fifth|2nd|3rd|4th|5th) time\b)?"
    # ", 8 weeks", " (12 weeks)", ", about 3 months", ", week 8"
    rf"(?:[,(]? ?(?:about |around |nearly |almost |roughly |just over |just under |now |at )?"
    rf"(?P<wa>{_WEEKS_N})[- ]?(?P<ua>weeks?|wks?|months?)\b"
    r"(?! ago\b| after\b| post| since\b| old\b)"
    r"|[,(]? ?(?:at |in )?week (?P<wk>\d{1,2})\b)?"
)

# Time lost, not time lost to a rival: "I lost a few seconds", but not "lost a
# few seconds on the climb" or "lost ten seconds to the leader".
_LOST_N = r"a few|a couple of|a couple|some|several|\d+|a|one|two|three|five|ten"
_NOT_RACE_TIME = (
    r"(?!\s*(?:on|to|in|at|over|against|from|off|per|each|every|behind|overall|there|of|"
    r"a lap|a km|a mile)\b)"
)
_LOST_TIME = rf"\blost (?P<lost>{_LOST_N}) (?:seconds?|secs?|minutes?|mins?)\b{_NOT_RACE_TIME}"

# Signs of a head injury said with no word about the head: a headache, being
# sick, dizzy, foggy or confused, a few seconds lost. They count only next to
# something that happened to the rider (_incident): a crash, a fall, or a
# blow to the head they say they did or didn't take.
_HEAD_SYMPTOM = (
    r"\b(?P<sym>headaches?|sore head|head (?:hurts|aches|is (?:sore|hurting|aching|pounding|"
    r"throbbing|killing me|spinning|foggy|fuzzy))|nausea|nauseous|nauseated|queasy|"
    r"vomit(?:ed|ing|ted)?|threw up|thrown up|throwing up|dizzy|dizziness|light-?headed|woozy|"
    r"groggy|dazed|drowsy|disorientated|disoriented|confusion|brain fog|foggy head|"
    r"seeing stars|saw stars|blurr(?:ed|y) vision|double vision|ringing in my ears|"
    # "my vision is blurry", "vision's been a bit blurry", "seeing double".
    r"(?:vision|eyesight|sight)(?:'s| is| was| has| had| keeps| kept| went| goes| going|"
    r" has been|'s been| have been)?(?: \w+){0,2}? (?:blurr(?:y|ed|ing)|fuzzy|double|hazy|"
    r"cloudy|funny|weird|wonky)|seeing double|(?:everything|it)(?:'s| is| was| looks| looked|"
    r" went| goes| has been|'s been)(?: \w+){0,2}? blurr(?:y|ed)|blurriness|"
    r"memory (?:gap|loss)|gap in my memory|"
    r"(?:feel|feels|felt|feeling|been|was|got|am|i'm|im|i am)(?: \w+){0,2}? (?:sick(?! of\b)|"
    r"confused(?! (?:about|by|as to|why|what|how|whether|with)\b)|foggy|fuzzy|spaced out|"
    r"out of it|not (?:quite )?(?:with it|right in the head))|"
    rf"lost (?:{_LOST_N}) (?:seconds?|secs?|minutes?|mins?){_NOT_RACE_TIME})\b"
)
# Something that happened to the rider. Checked below (_incident): not a near
# miss, not someone else's, not an app or a sugar crash, not years ago.
_INCIDENT_RX = re.compile(
    r"\bcrash(?:ed|es)?\b"
    r"|\b(?:came|come|comes|coming|fell|falling|fall|thrown|flew|flown) off\b"
    r"|\b(?:i|we|both of us|then) (?:\w+ )?went down\b"
    r"|\bwent down (?:hard|heavily|badly|on (?:a |the |some )?(?:\w+ )?(?:corner|bend|"
    r"roundabout|descent|ice|gravel|diesel|oil|mud|leaves|wet|drain|pothole|cattle grid|tram|"
    r"railway|crossing|cobbles|white line|manhole))\b"
    r"|\bfell (?:over|down|hard|badly|heavily)\b"
    r"|\bcame down (?:hard|heavily|badly|on (?:a |the |some )?(?:\w+ )?(?:corner|bend|ice|"
    r"gravel|diesel|oil|mud|leaves|wet|descent|roundabout|drain|cobbles|tarmac|road))\b"
    r"|\b(?:hit|hitting) the (?:deck|ground|tarmac|floor|kerb|curb)\b"
    r"|\b(?:had|took|taken|have had) (?:a |an |another )?(?:bad |big |nasty |heavy |little |"
    r"small |silly )?(?:fall|tumble|spill|off|accident|crash|collision|wipe-?out)\b"
    # A second or another one: "had a second off this morning", "another
    # spill today", "my third crash". Not "took a second off my PB".
    r"|\b(?:another|second|third|fourth|2nd|3rd|4th)(?: (?:bad|big|nasty|heavy|little|small|"
    r"silly|proper))? (?:fall|tumble|spill|accident|crash|collision|wipe-?out)\b"
    r"|\b(?:(?:had|took|taken|have had|on|after|since) (?:a |an |my |the )?|my |another )"
    r"(?:(?:second|third|fourth|2nd|3rd|4th|another) )?(?:(?:bad|big|nasty|heavy|little|small|"
    r"silly|proper) )?off\b(?![-'])(?! (?:my|his|her|their|your|our|of|from|per|each|every|"
    r"the|pace|work|school|today's|day|days|season|seasons|week|weekend|road|piste|switch|"
    r"button|time|bike|position|chance|peak)\b)"
    r"|\b(?:wiped out|stacked it|binned it|over the (?:bars|handlebars|top of the bars))\b"
    # The wheels going from under the rider: "slid out on the gravel",
    # "lost the front on the descent". Not "lost the front of the group".
    r"|\b(?:slid|slide|slides|sliding|skidded|skidding|skids|skid) out\b"
    r"|\bslipped out\b(?! (?:of|for|to|early|before|after|at|the door|quietly)\b)"
    r"|\blost (?:the|my) (?:front|back|rear)(?: (?:wheel|end|tyre|tire))?\b(?! (?:of|light|"
    r"mudguard|brake|pads?|bottle|cage|mech|derailleur|tyre pressure)\b)"
    r"|\b(?:toppled|topple|toppling|keeled) (?:over|off|sideways)\b"
    r"|\bfell (?:sideways|onto|on to)\b"
    # Running into something: "collided with a car door", "went into the
    # back of a parked van". Not "rode into the car park".
    r"|\b(?:collided|colliding|collide|collides)\b"
    r"|\b(?:rode|ride|riding|went|go|going|cycled|smashed|ploughed|plowed|slammed|careered|"
    r"piled|ran|run|crashed) (?:straight |right |head ?-?first |full pelt )?(?:into|in to) "
    r"(?:the back of |the side of )?(?:a |an |the |some |someone's |this |that )?"
    r"(?:parked |stationary |oncoming |open |opening )?(?:car|van|lorry|truck|bus|post|tree|"
    r"barrier|bollard|kerb|curb|hedge|ditch|gate|fence|cyclist|rider|pedestrian|walker|dog|"
    r"sign|signpost|lamp ?post|railings?|car door|door|wing mirror|wheelie bin|bin|bench|"
    r"wall)\b(?![-\w]| (?:park|parks|wash|share|club|free|lane|lanes|space|spaces|stop|"
    r"station|of (?:fatigue|pain|tiredness)|at \d)\b)"
    r"|\b(?:clipped|clip|clipping) (?:a |the |his |her |their |someone's |another |a rider's "
    r"|the rider's )?(?:back )?(?:wheel|pedestrian|kerb|curb|bollard|car|van|rider|cyclist|"
    r"barrier|wing mirror|mirror)\b"
    r"|\b(?:hit|hitting) (?:a |the )?(?:parked )?(?:car|van|lorry|truck|bus|tree|post|barrier|"
    r"bollard|railings?|gate|fence|pedestrian|car door|wing mirror|signpost|lamp ?post)\b"
    r"(?![-\w]| (?:park|parks|wash|share|club|free|lane|lanes|space|spaces)\b)"
    r"|\b(?:got|was|been|being) (?:hit|clipped|brought down|knocked over|run over|run into)\b"
    r"(?! (?:by|with) (?:a |the )?(?:bug|virus|cold|flu|covid|wave|wall|bonk|tiredness|"
    r"fatigue|cramp)\b)"
    r"|\b(?:hit|knocked off|taken out|t-?boned|clipped|doored|struck|knocked down) by (?:a |"
    r"an |the )?(?:car|van|lorry|truck|bus|driver|taxi|motorbike|cyclist|rider)\b"
    r"|\b(?:got|was|been|being) (?:knocked off|taken out|doored|t-?boned)\b"
    r"|\bknocked off (?:my|the) bike\b"
    # "the off" and "my off": a crash, in British club talk. "After the off on
    # Saturday", "since my off".
    r"|\b(?:after|since|from|following|in|during) (?:the|my|that|this|an|a|yesterday's|"
    r"\w+day's|last week's|today's|this morning's) (?:little |big |bad |nasty |heavy |small |"
    r"silly )?(?:fall|spill|tumble|accident|collision|wipe-?out)\b"
    r"|\b(?:after|since|from|in|during) (?:the|my|that|this|an|a|yesterday's|\w+day's|"
    r"last week's) (?:little |big |bad |nasty |heavy |small |silly )?off\b(?![-'])"
    r"(?! (?:season|seasons|day|days|week|weeks|weekend|road|piste|switch|ramp|licence|"
    r"license|peak|chance)\b)"
    r"|\bmy (?:little |big |bad |nasty |heavy |small |silly )?off\b(?![-'])(?! (?:day|days|"
    r"season|seasons|week|weekend|the|my|work|time|bike|road|switch|button|position|chance|"
    r"peak)\b)"
)
# Kit, food or numbers that crash or come off, not the rider.
_INCIDENT_NOT_RIDER = re.compile(
    r"\b(?:sugar|energy|caffeine|glycogen|carbs?|insulin|stock|market|app|computer|garmin|"
    r"wahoo|zwift|laptop|phone|software|website|site|server|strava|trainerroad|game|ipad|tv|"
    r"system|head unit|chain|bottles?|cage|lights?|mount|bag|saddle ?bag|cleats?|cover|caps?|"
    r"tyres?|tires?|wheels?|mudguards?|numbers?|stickers?|tape|plug|pedals?|cranks?|hanger|"
    r"mech|derailleur|paint|straps?|helmet|lid|visor|power|pace|watts|ftp|speed|form|fitness|"
    r"motivation|"
    r"interest|heart rate|hr|price|prices|weight|watched|watching|watch|saw|seen|see|"
    r"witnessed|filmed|footage of|video of|clip of)(?:'s)?\s+(?:\w+\s+)?$"
)
_NEAR_MISS = re.compile(
    r"(?:\b(?:nearly|almost|near|close to|narrowly|avoided|avoiding|avoid|without|no|not|"
    r"never)|n't)\s+(?:(?:a|an|the|any|have|had|actually|quite|really|even)\s+)?$"
)
_INCIDENT_AFTER_NOT = re.compile(
    r"^\s*(?:diet|course|pad|mats?|test|dummy|helmet|and burn|"
    r"out (?:on|at|in|early|asleep|for)|on (?:the |a |my )?(?:sofa|couch|bed|settee)|"
    r"at (?:a |my |his |her |their )?(?:mate|friend|place|house)|asleep|"
    r"(?:my|the) (?:app|computer|laptop|phone|garmin|wahoo|zwift|game|server|site|website|"
    r"system|program|software|tv|ipad|head unit)|"
    r"the (?:back|front|pace|wheel|wheels|group|bunch|peloton|boil|drops|hoods|top|plan|radar|"
    r"wagon|turbo|trainer|rollers|gas|lead)|a cliff|"
    r"(?:my |the )?(?:meds|medication|antibiotics|caffeine|coffee|sugar|alcohol|booze|tablets|"
    r"pills)|second|third|last|well|better|"
    r"(?:a|my|the) (?:rest|recovery|good|hard|big|long|tough|bad|great|easy) (?:week|block|day|"
    r"ride|session|month|season)|holiday|nights|shift|work|"
    # Kit that fell off a shelf, not the rider.
    r"(?:the |a |my )?(?:shelf|table|hook|peg|car roof|roof rack|rack|counter|worktop|"
    r"wardrobe|cupboard|desk|chair))\b"
)
# "Knocked me out" about the effort, the heat or a cold, not a blow:
# "that session knocked me out", "the heat knocked me out today".
_KNOCKED_OUT = r"\bknocked (?:myself |me )?(?:out|unconscious)\b(?! of\b)"
_KNOCKED_OUT_RX = re.compile(_KNOCKED_OUT)
_NOT_A_BLOW = re.compile(
    r"\b(?:session|sessions|workout|workouts|intervals?|efforts?|ride|rides|training|block|"
    r"week|day|race|sportive|climb|climbs|turbo|zwift|heat|sun|humidity|cold|flu|bug|virus|"
    r"jet ?lag|pills?|tablets?|meds|medication|antihistamines?|wine|beer|drinks?|gin|"
    r"tiredness|fatigue|miles|hours|stage|film|book|pints?|that one|it all)(?:'s)?\s+"
    r"(?:\w+\s+){0,2}$"
)
_HELMET_HIT_RX = re.compile(
    r"\bhelmet (?:saved (?:me|my)|took (?:the|a|most of the) (?:hit|impact|brunt|knock|blow)|"
    r"did its job)\b|\bconcuss(?:ion|ed)\b"
)
# A denial the rider isn't sure of is no denial: "I wasn't knocked out I
# don't think", "not knocked out as far as I know". Someone who can't be sure
# whether they were out may well have been.
_UNSURE_AFTER = re.compile(
    r"^\s*,?\s*(?:i (?:don't|dont|do not) think(?: so)?|i think|i'm not sure|im not sure|"
    r"i am not sure|not sure|as far as i (?:know|can tell|remember|can remember)|afaik|"
    r"i (?:don't|dont|do not|can't|cant|cannot) (?:remember|know|recall|be sure)|i guess|"
    r"i believe|probably|maybe|i doubt it|hard to (?:say|tell|know)|who knows)"
    r"(?=\s*(?:$|[,.!?;]|but\b|though\b|and\b|so\b|tbh\b|either\b|really\b))"
)
_UNSURE_BEFORE = re.compile(
    r"(?:\bi (?:don't|dont|do not) think|\bi think|\bas far as i (?:know|can tell|remember)|"
    r"\bafaik|\bprobably|\bmaybe|\bi'm not sure if|\bnot sure if|\bi guess|\bi believe|"
    r"\bpretty sure|\bfairly sure)\b[^.!?;\n]{0,20}$"
)
_UNSURE_KINDS = frozenset({"head_injury", "fainting"})

# A helmet damaged with no blow to the rider: a strap, buckle or visor that
# broke, or a lid dropped, sat on or knocked off a shelf. Never when the
# rider crashed, came off or took a blow (checked with _rider_incidents).
_HELMET_PART = (
    r"(?:chin ?straps?|straps?|buckles?|clips?|visors?|peaks?|lights?|mounts?|cameras?|"
    r"pads?|padding|liners?|bags?|box|case|hooks?|locks?|stickers?|covers?|dials?|"
    r"adjusters?|retention|lens|shield|mirrors?)"
)
_HELMET_PART_RX = re.compile(rf"\b{_HELMET_PART}\b")
_HELMET_PART_BEFORE_RX = re.compile(
    rf"\b{_HELMET_PART} (?:on|of|from|for) (?:my |the |his |her |their |our )?$"
)
_HELMET_PART_AFTER_RX = re.compile(rf"^\s*{_HELMET_PART}\b")
_HELMET_HANDLED_RX = re.compile(
    r"\b(?:dropp(?:ed|ing)|drop it|dropped it|sat on|sitting on|stood on|trod on|stepped on|"
    r"chewed|ran over|drove over|reversed over|putting it on|taking it off|doing (?:it|the "
    r"strap) up|car park|shelf|shelves|wardrobe|cupboard|in the (?:post|boot|car|bag|box|"
    r"garage|loft|shed|hall)|in transit|courier|delivery|delivered|arrived|"
    r"(?:fell|fallen|falling|came|come) (?:off|out of|from) (?:the |a |my )?(?:shelf|table|"
    r"hook|peg|car|roof|rack|bag|bars|handlebars|counter|bench|wardrobe|cupboard|locker|"
    r"desk|chair|side))\b"
)
# A knock to the head about the house, said with a shrug: "Hit my head on the
# cupboard door, ouch, anyway", "banged my head on the garage door, no harm
# done". Recorded as head_knock: no card and no hold, unless a crash, a sign
# of a head injury or being knocked out sits anywhere in the message.
_HOUSEHOLD_RX = re.compile(
    r"^\s*(?:\w+\s+)?(?:on|against|off|into|under) (?:the |a |an |my |our |that |this |some |"
    r"their )?(?:\w+ )?(?:cupboards?|cabinets?|kitchen|doors?|doorway|door ?frame|garage door|"
    r"car door|boot|tailgate|beams?|shelf|shelves|ceiling|table|desk|bunk|bed|headboard|"
    r"loft|hatch|windows?|sink|tap|stairs|bannister|banister|radiator|lamp|light|"
    r"fridge|freezer|wardrobe|car roof|roof rack|bike rack|rack|wall|hook|fan|tumble dryer|"
    r"washing machine|counter|worktop|mantelpiece|archway|arch|lintel|porch|shed|"
    r"cooker hood|extractor)\b"
)
_SHRUGGED_OFF_RX = re.compile(
    r"\b(?:no harm done|no harm|no damage(?: done)?|ouch|anyway|anyways|lol|haha|ha ha|hehe|"
    r"laughed|laughing|all good|i'm fine|im fine|i'm ok|i'm okay|im ok|no problem|nothing "
    r"serious|silly me|clumsy|daft|whoops|oops|doh)\b"
)
_HEAD_SIGN_ANY_RX = re.compile(
    r"\bunconscious\b|\bblanked out\b|\bcame (?:to|round)\b|\bwas out for\b|"
    r"\bconcuss|\bknocked (?:myself |me )?(?:out|unconscious)\b|\bout cold\b|\bblacked out\b|"
    r"\bpassed out\b|\b(?:can't|cannot|don't|do not|couldn't) (?:remember|recall)\b|"
    r"\bmemory\b|\bhelmet\b|\bblood thinners?\b|\bwarfarin\b"
)

# A head injury that is a new event rather than the earlier one mentioned
# again: "crashed again", "another crash", or a crash or blow dated today,
# this morning, yesterday or last night, or told with where it happened
# ("hit my head on the kerb") and nothing tying it back to an earlier one
# ("since the crash", "how long after hitting my head", "last week").
_AGAIN_AFTER_RX = re.compile(r"^\s*(?:\w+\s+)?(?:again|twice|a second time|another time)\b")
_AGAIN_BEFORE_RX = re.compile(r"\b(?:another|a second|second)\s+(?:\w+\s+)?$")
_AGAIN_IN_RX = re.compile(r"\b(?:another|second|third|fourth|2nd|3rd|4th|again)\b")
_FRESH_TIME_RX = re.compile(
    r"\b(?:today|this (?:morning|afternoon|evening|lunchtime)|tonight|just now|earlier today|"
    r"yesterday|last night|an? (?:hour|few hours|couple of hours) ago|"
    r"\d+ (?:minutes?|mins?|hours?|hrs?) ago|on (?:today's|tonight's|this morning's|this "
    r"evening's) (?:ride|commute|club run|chain ?gang|race|crit))\b"
)
_STALE_TIME_RX = re.compile(
    r"\b(?:last (?:week|weekend|month|year|season|time|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)|the other day|(?:\d+|a|one|two|three|four|five|six|a few|a "
    r"couple of|several) (?:days?|weeks?|months?|years?) ago|a fortnight ago|in (?:january|"
    r"february|march|april|may|june|july|august|september|october|november|december))\b"
)
_BACK_REFERENCE_RX = re.compile(
    r"\b(?:since|after|from|following|before|about)\s+(?:i\s+|i've\s+|the\s+|my\s+|"
    r"that\s+|this\s+|your\s+|\w+'s\s+)?(?:\w+\s+)?$"
)
_PLACE_AFTER_RX = re.compile(
    r"^\s*(?:\w+\s+){0,2}?(?:on|onto|into|against|off|at|in|over|during) (?:[\w']+ ){0,4}?"
    r"(?:kerb|curb|ice|road|tarmac|gravel|corner|bend|roundabout|descent|pothole|wall|car|van|"
    r"lorry|truck|bus|post|barrier|ground|deck|floor|railings?|bollard|drain|cattle grid|"
    r"tram ?lines?|wet|mud|leaves|diesel|oil|cobbles|white line|manhole|crossing|track|trail|"
    r"path|lane|junction|lights|hedge|ditch|tree|gate|step|steps|stem|bars|handlebars|bike|"
    r"crit|race|sportive|club run|chain ?gang|group ride|commute|climb|sprint|bunch|"
    r"peloton|velodrome|cyclocross|cx)\b"
)

# After a hard effort, a few seconds lost is a faint. In a race report it is
# usually time lost to a rival, so it counts only with faint words nearby, or
# with effort words and no race-time words.
_FAINT_WORDS_RX = re.compile(
    r"\b(?:blank(?:ed|ing)? out|unconscious|out cold|"
    r"black(?:ed|ing)? out|pass(?:ed|ing)? out|faint\w*|gr[ae]y(?:ed)? out|vision|"
    r"everything went|keel(?:ed)? over|collaps\w*|came to|came round|woke up on)\b"
)
_RACE_TIME_RX = re.compile(
    r"\b(?:gap|lead|overall|gc|segment|kom|qom|strava|pb|pr|split|splits|time trial|tt|"
    r"finish line|leaderboard|rival|rivals|bunch|peloton|position|places?|podium|"
    r"sprint finish|line|leader|leaders|winner|chasers?|break|breakaway)\b"
)

# An event long past and done with: "I broke my collarbone 10 years ago". Not
# when it still bothers them, or something recent sits beside it.
_YEARS_N = (
    r"a|one|\d+|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|"
    r"thirty|a few|a couple of|a couple|several|many|some"
)
_LONG_AGO_RX = re.compile(
    rf"\b(?:(?:{_YEARS_N}) years? (?:ago|back|before)|years (?:ago|back|before)|ages ago|"
    r"a long time ago|long ago|decades? ago|"
    r"when i was (?:a kid|a child|young|younger|little|a teenager|a teen|at school|at uni|"
    r"a junior|a student|\d{1,2}|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
    r"seventeen|eighteen|nineteen|twenty|in year \d{1,2}|in (?:the )?sixth form|at (?:primary|"
    r"secondary|high) school|in high school)|as a (?:kid|child|teenager|teen|junior|youngster|"
    r"boy|girl|student)|(?:in|back in) (?P<year>(?:19|20)\d\d)|back in the day|at school|"
    r"at uni(?:versity)?|aged? \d{1,2}|at (?:the )?age (?:of )?\d{1,2})\b"
)
_HEALED_RX = re.compile(
    r"\b(?:(?:fully|completely|long since|all|properly) healed|healed (?:fine|up|well|ok|okay|"
    r"completely|fully|long ago|years ago)|no (?:problems|issues|trouble|bother) (?:with it )?"
    r"since|(?:hasn't|has not|never) (?:bothered|troubled) me|"
    # "I tweaked my back lifting the bike but it's fine now".
    r"(?:(?:it's|its|it is|it was|it feels|feels|feeling|i'm|im|i am|i feel|all|totally|"
    r"completely|back to) )?(?:fine|ok|okay|alright|all right|all good|sorted|grand|normal|"
    r"good as new|right as rain) (?:again )?now|(?:it's|its|it is|it has|it's) (?:all )?"
    r"(?:gone|settled|cleared up|passed) now)\b"
)
_STILL_NOW_RX = re.compile(
    r"\b(?:still|again|lately|recently|this (?:week|month|morning|weekend)|today|yesterday|"
    r"last (?:night|week|weekend|month|saturday|sunday|ride)|(?:\d+|a|one|two|three|four|five|"
    r"six|a few|a couple of|several) (?:days?|weeks?) ago|flar\w*|playing up|acting up|"
    r"bother(?:s|ing)? me|giving me (?:grief|trouble|problems|jip|gyp)|hurts|hurting|aches|"
    r"aching|sore|painful|niggl\w*|worse|swollen|swelling|re-?injur\w*|re-?broke|"
    r"broke it again|ever since|since then)\b"
)
_SEGMENT_BREAK_RX = re.compile(
    r",|;|\b(?:but|though|although|whereas|then|these days|nowadays)\b"
)

# Crisis words that are often training talk ("I just want it all to end, this
# block is brutal"), and the context that makes them so. A match counts as
# training talk only when its own sentence names training or food, and
# nothing in the message sounds like more than a bad session.
_TRAINING_CONTEXT_RX = re.compile(
    r"\b(?:block|blocks|taper\w*|intervals?|sessions?|workouts?|reps|sets|ftp|w/kg|watts?|"
    r"\d+ ?(?:w|kj|bpm|rpm|km|kms|miles?)|vo2\w*|threshold|sweet ?spot|tempo|zone ?\d|z\d|"
    r"turbo|zwift|trainerroad|rollers|goal event|target event|a-?race|races?|racing|sportive|"
    r"crit|criterium|time trial|tt|climbs?|climbing|hill reps|training|(?:the|my|this) plan|"
    r"power|pb|kom|qom|strava|segment|ramp test|ftp test|recovery week|rest week|off-?season|"
    r"rides?|riding|club run|chain ?gang|cake|cakes|biscuits?|pizza|chocolate|crisps|pudding|"
    r"dessert|ice cream|doughnuts?|donuts?|burger|chips|kebab|takeaway|sweets|snacks?)\b"
)
_LIFE_WITHOUT_RX = re.compile(
    r"\bwithout (?:\w+ )?(?:cycling|bikes?|coffee|tea|beer|wine|chips|cheese|sport|racing|"
    r"riding|training)\b"
)
_DISTRESS_RX = re.compile(
    r"\b(?:low|sad|depress\w*|anxious|anxiety|struggling|not coping|can't cope|cannot cope|"
    r"can't take (?:it|this|any ?more)|dark|darkest|hopeless|empty|numb|alone|lonely|crying|"
    r"cried|tears|life|living|alive|die|dying|dead|death|suicid\w*|kill|harm|hurt myself|"
    r"any ?more|every ?(?:day|night|time)|all the time|nobody|no one|no-one|seriously|"
    r"honestly|not joking|i mean it|for real|genuinely|can't even|cannot even)\b"
)

# The guards both crisis rules share.
_CRISIS_GUARDS = (
    # Cyclists "kill themselves" on climbs and "want to die" on the
    # last rep. The idiom names the effort, after a preposition; the
    # crisis does not.
    _g("after", rf"^\s*(?:on|up|in|at|over|during|doing|for|with|trying|to|"
                rf"through|round|around|into|after)\s+(?:\w+\s+){{0,3}}?"
                rf"(?:{_EFFORT})\b"),
    _g("after", r"^\s*laughing\b"),
    # "I want it all to end on a high", "with a podium".
    _g("after", r"^\s*(?:with|on|in|at)\s+(?:a |an |the |my )?(?:\w+\s+){0,2}?(?:high|"
                r"win|podium|finish|bang|kick|flourish|smile|pb|pr|kom|qom|result|"
                r"medal|trophy|jersey|sprint)\b"),
    _g("before", r"\b(?:nearly|almost)\s+$"),
    _g("after", r"^\s*(?:some )?slack\b"),
    _g("after", r"^\s*(?:pace|attack|break|breakaway|move|pull|turn|turns|mission)\b"),
    _g("after", r"^\s*at (?:climbing|sprinting|descending|cornering|pacing|\w+ing)\b"),
    _g("after", r"^\s*(?:at work|in (?:this|the) (?:office|meeting|queue|traffic|"
                r"rain|cold|wind))"),
    _g("after", r"^\s*(?:on )?(?:caffeine|coffee|gels?|carbs?|sugar|espresso|"
                r"energy drinks?|beta.?alanine|bicarb|nitrates?|beetroot)\b"),
    _g("before", r"\b(?:caffeine|coffee|gel|gels|carbs?|sugar|beta.?alanine|"
                 r"espresso|energy drinks?)\s+(?:\w+\s+)?$"),
    _g("before", r"\b(?:legs|power|numbers|data|bike|watts|ftp|garmin)\s+"
                 r"(?:\w+\s+)?$"),
    # Kit that's about to die: "my Wahoo is going to die", "the HRM's
    # going to die before the end".
    _g("before", r"\b(?:garmin|wahoo|polar|whoop|coros|hrms?|straps?|monitors?|"
                 r"sensors?|batter(?:y|ies)|phone|lights?|di2|power meter|computer|"
                 r"head unit|watch|laptop|ipad)(?:'s)?\s+(?:\w+\s+)?$"),
)

# Any part of the head a blow can land on: the temple, the forehead, the
# skull are the head as much as "head" is.
_HEAD_PART = r"(?:head|temple|temples|forehead|skull|noggin)"
_HEAD_WHERE = (
    r"(?:my |the back of my |the side of my |the front of my |the top of my )?"
    r"(?:left |right )?"
)
# What a head, a face or a helmet hits when a rider comes down.
_SURFACE = (
    r"ground|road|floor|tarmac|deck|kerb|curb|kerbstone|pavement|car|windscreen|bonnet|wall|"
    r"post|tree|barrier|stem|bars|handlebars?|top tube|bike|railings?|bollard|lamp ?post|step|"
    r"steps|rock|rocks|stone|stones|gate|van|lorry|truck|bus|signpost|sign|fence|pole|branch|"
    r"car door|door|dashboard|wing mirror|mirror|concrete|gravel|ice|verge|ditch|drain|"
    r"manhole|pothole|track|trail|path|cobbles|bench|bin"
)
# A blow to the head said outright: "hit my head", "hitting my head on the
# stem", "bashed my temple", "took a knock to the forehead". Banging your
# head against a wall is frustration, not a crash.
_BLOW_PATTERNS = (
    r"\b(?P<blow>(?:hit|banged|bashed|smacked|smashed|cracked|knocked|whacked|bumped|"
    r"bounced|clattered|clouted|thumped|slammed|split|gashed|cut|grazed|scraped|bruised|"
    rf"busted|bust|opened up) {_HEAD_WHERE}{_HEAD_PART})\b"
    r"(?! against (?:a |the )?(?:brick |proverbial )?wall)",
    # "I don't remember hitting my head", "hitting my head on the stem".
    r"\b(?P<blow>(?:hitting|banging|bashing|smacking|smashing|cracking|knocking|whacking|"
    rf"bumping|bouncing|clattering|thumping|slamming|splitting) {_HEAD_WHERE}{_HEAD_PART})\b"
    r"(?! against (?:a |the )?(?:brick |proverbial )?wall)",
    # "a blow to the head", "took a knock on the temple", "a bang on my forehead".
    r"\b(?P<blow>(?:(?:took|taken|got|had|copped|taking|getting) )?(?:a |another )"
    r"(?:(?:nasty|big|hard|bad|heavy|proper|good|real|solid|sharp) )?(?:blow|knock|bang|"
    r"bump|whack|smack|thump|clout|crack|hit) (?:to|on|in) (?:the |my )?(?:back of (?:the |my )?|"
    rf"side of (?:the |my )?|front of (?:the |my )?|top of (?:the |my )?)?{_HEAD_PART})\b",
)
_HEAD_GUARDS = (
    # Knocked out of the race, the competition, the first round.
    _g("after", r"^\s*(?:of\b|in the (?:first|second|third|next|\w+) "
                r"(?:round|heat|stage)|early|by)"),
    # "Banged my head against a brick wall" with a coach or a bike shop.
    _g("after", r"^\s*against (?:a |the )?(?:brick|proverbial) wall"),
)

# "X fever" that is excitement, not illness: cabin fever, race day fever,
# Tour de France fever, football fever, baby fever.
_FEVER_IDIOM = (
    r"cabin|race|race day|race-day|raceday|match day|game day|spring|gold|saturday night|"
    r"football|footy|soccer|rugby|cricket|tennis|golf|cup|world cup|euros|olympics?|olympic|"
    r"tour|france|giro|italia|vuelta|espana|classics|cx|sportive|strava|zwift|bike|bikes|"
    r"cycling|gear|kit|christmas|xmas|festive|holiday|wedding|election|transfer|title|"
    r"playoffs?|play-?off|derby|disco|dance|boogie|beatle|beatles|baby|shopping|spending|"
    r"upgrade|carbon|watt|watts|ftp|kom|qom|podium|medal|crit|gravel|mtb|championship|"
    r"champs|final|finals|grand prix|f1|wimbledon|ashes|marathon|lottery|pre-race|prerace|"
    r"start line"
)

# The rider is the one who is pregnant, said beside "we're expecting".
_CARRYING_RX = re.compile(
    r"\b(?:i'm|i am|im) the one (?:carrying(?= *(?:$|[.!?,;:)\n]|this time\b|(?:the|our|my) "
    r"baby\b|it\b|and\b|so\b|but\b))|who's pregnant|who is pregnant|that's pregnant|having "
    r"(?:the|our) baby)\b|\b(?:i'm|i am|im) pregnant\b|\b(?:i'm|i am|im) carrying (?:the|our|"
    r"my|a) baby\b|\bmy (?:bump|midwife|"
    r"obstetrician|pregnancy|antenatal|booking appointment|\d+ ?(?:week|wk) scan)\b"
)

# Common blood pressure medicines by name, generic and UK brand.
_BP_MEDICINES = (
    r"amlodipine|felodipine|nifedipine|lercanidipine|diltiazem|verapamil|ramipril|lisinopril|"
    r"perindopril|enalapril|captopril|candesartan|losartan|irbesartan|valsartan|olmesartan|"
    r"telmisartan|indapamide|bendroflumethiazide|doxazosin|istin|tritace|zestril|coversyl|"
    r"amias|cozaar"
)

# "every time I see the hill reps", "whenever I look at the plan": what the
# rider is reacting to is training.
_EVERY_TIME_TRAINING = (
    r"(?:a (?:little|bit) )?(?:inside )?(?:every ?time|whenever|each time) (?:i |we )?"
    r"(?:see|saw|look at|looked at|think (?:about|of)|read|open|opened|hear|get to|do|did|"
    r"start|started|check|checked)(?: \w+){0,4}? "
    rf"(?:{_EFFORT}|plan|schedule|calendar|week|block|reps)\b"
)
# Tablets that are kit, not medicine: saving these up is not a plan.
_SUPPLEMENTS = (
    r"caffeine|salt|electrolytes?|hydration|energy|iron|vitamins?|zinc|magnesium|nuun|"
    r"dextrose|glucose|beetroot|nitrates?|creatine|protein|supplements?|antihistamines?|"
    r"hay ?fever|gum|sweets|chamois"
)
# Medicines that could be saved up.
_PILLS = (
    r"pills|tablets|meds|medication|medicines|painkillers|paracetamol|sleeping pills|"
    r"sleeping tablets|codeine|tramadol|co-codamol|ibuprofen|antidepressants|sertraline|"
    r"diazepam|insulin"
)

# At rest, with nothing to explain a racing heart.
_AT_REST = (
    r"at rest|in bed|lying (?:down|in bed|awake|there)|while (?:resting|sitting|lying)|resting|"
    r"for no reason|out of nowhere|on the sofa|on the couch|sitting (?:down|still|on the sofa)|"
    r"watching (?:tv|telly)|at my desk|soft[- ]?pedal\w*|out of the blue|from nowhere|"
    r"for no (?:apparent|obvious) reason|easy (?:spin|ride|pace|effort|riding|spinning)|"
    r"recovery (?:ride|spin|pace)|zone ?[12]|z[12]|warm(?:ing)?[- ]?up|asleep|woke me|"
    r"(?:couldn't|could not|wouldn't|would not|won't|can't|cannot) (?:settle|calm|slow)\w*"
)
# A heart going too fast or too hard, in the rider's words: "racing",
# "felt like it was going to jump out of my chest", "going haywire".
_HEART_RUNNING = (
    r"racing|pounding|thumping|hammering|banging|thudding|going (?:mental|crazy|nuts|"
    r"haywire|berserk|mad|wild|ten to the dozen)|all over the place|jump(?:ing)? out|"
    r"(?:going to|gonna|trying to|about to) (?:jump|burst|leap|explode|beat) out|"
    r"burst(?:ing)? out|beat(?:ing)? out of|bounc(?:ing|ed) (?:around|about)"
)
# A heart rate this high at rest or soft pedalling is a racing heart, not a
# number to train by.
_HR_JUMP_BPM = 200
_HR_JUMP_REST_RX = re.compile(
    r"\b(?:at rest|resting|rest day|soft[- ]?pedal\w*|easy (?:spin|spinning|pedal\w*|riding|"
    r"ride|pace|effort)|spinning (?:easy|along)|recovery (?:ride|spin|pace)|coasting|"
    r"freewheel\w*|cool(?:ing)?[- ]?down|sitting|lying|in bed|on the sofa|on the couch|"
    r"doing nothing|for no (?:apparent )?reason|out of nowhere|out of the blue|from nowhere|"
    r"stayed there|wouldn't come down|didn't come down|wouldn't drop|didn't drop|stuck|"
    r"zone ?[12]|z[12]|endurance (?:pace|ride|effort)|low (?:effort|intensity)|warm(?:ing)?[- ]?up|"
    r"at my desk|asleep|woke|walking|standing)\b"
)

# ── General patterns for each red flag, as a clinician would describe it ────
# The phrase lists above began as the words riders were seen to use. These
# describe the clinical sign itself, so new wording of the same thing is
# caught: a crisis with a plan, losing consciousness, a memory gap after a
# fall, a blow to any part of the head, a measured fever, a band round the
# chest, a racing heart out of nowhere, a pregnancy in weeks, eating only
# when it's earned, heat illness after a hot ride, and the school year.

# A plan, an arrangement or a goodbye that means ending their life. On its
# own each is often ordinary ("I have a plan for Sunday"), so the weaker ones
# count only beside words about ending it, or beside another of them
# (checked below: _crisis_plan).
_CRISIS_ANCHOR_RX = re.compile(
    r"\b(?:end (?:it|it all|things|my life)|ending (?:it|it all|things|my life)|kill(?:ing)? "
    r"myself|suicid\w*|overdos\w*|tak(?:e|ing) my (?:own )?life|(?:not|no longer|never) "
    r"(?:be |being )?(?:here|around|alive)|be gone|(?:i'm|i am|im) gone|when i'm gone|after "
    r"i'm gone|once i'm gone|better off|goodbyes?|(?:not|never) wake up|disappear for good|"
    r"no way out|(?:can't|cannot|can not) (?:go on|do this any ?more|take (?:it|this) any ?more|"
    r"keep going)|tired of (?:living|life|being alive|everything)|done with (?:life|living|"
    r"everything|it all)|(?:nothing|no reason) (?:left )?to live for|a burden|burden (?:on|to)|"
    r"hurt(?:ing)? myself|harm(?:ing)? myself|want(?:ed)? to die|wish i (?:was|were) dead|"
    r"(?:don't|do not) want to (?:be here|live|be alive|exist|wake up)|hopeless|"
    r"worthless|no point (?:in )?(?:living|life|anything|going on)|for good|"
    # Intent: "I just haven't yet", "going to go through with it".
    r"(?:stopped|stop|no longer) seeing (?:the |any )?point|(?:can't|cannot|don't) see (?:the|any) "
    r"point|see no point|"
    r"stopped caring|(?:don't|do not|no longer) care (?:any ?more|about anything)|nothing "
    r"matters|given up on (?:everything|life|it all)|"
    r"(?:haven't|have not|not) (?:\w+ )?yet|(?:go|going|gone|went) through with it|"
    r"act(?:ed|ing)? on it|(?:going|gonna) to do it|do it (?:soon|tonight|this week|"
    r"tomorrow)|when i do it|before i do it|"
    # Giving things away: "won't need them".
    r"(?:won't|will not|wont) (?:need|be needing) (?:them|it|any of (?:it|them)|anything)"
    r"(?= *(?:$|[.,!?;\n]| any ?more\b| again\b| where\b| soon\b)))\b"
)
# Strong on their own: goodbye letters, letters to the family, affairs in
# order.
_PLAN_STRONG = (
    r"\b(?P<plan>(?:goodbye|farewell|suicide) (?:letters?|notes?|messages?|texts?|videos?)|"
    r"(?:written|wrote|writing|write|left|leaving|leave|drafted|drafting|finished|started|"
    r"recorded) (?:\w+ ){0,2}?letters (?:to|for) (?:all )?(?:my |the )?(?:family|kids|children|"
    r"wife|husband|partner|parents|mum|mom|dad|loved ones|son|daughter|boys|girls|sons|"
    r"daughters|friends)|"
    r"(?:letters?|notes?|messages?) (?:to|for) (?:\w+ ){0,3}?(?:for )?(?:when|after|once|in "
    r"case) (?:i'm|i am|im|i've) (?:gone|dead|not here|not around|died)|"
    r"(?:know|knew|worked out|figured out|decided|planned) (?:exactly |just |already )?(?:how|"
    r"where|when) (?:i(?:'d| would| will|'ll)|to) do it,? and (?:where|when|how)|"
    r"(?:put|putting|get|getting|got|have|had|sort|sorting|sorted|sorted out) (?:all )?(?:of )?"
    r"(?:out )?my affairs (?:in order|sorted|straight)|sort(?:ed|ing)? out my affairs)\b"
)
# Count only beside words about ending it, beside another plan, or with
# words of distress and no training in sight.
_PLAN_MID = (
    r"\b(?P<planmid>(?:know|knew|worked out|work out|figured out|decided|planned|planning|"
    r"thought (?:about|through)|researched|looked up|googled) (?:exactly |just |already |"
    r"precisely |roughly )?(?:how|where|when|the way) (?:i(?:'d| would| will|'ll| am going to|"
    r"'m going to|'m gonna| could| can| should)|to) do it|"
    r"(?:giving|given|gave|give|been giving|started giving|handing|handed) (?:away )?(?:all "
    r"(?:of )?my|most (?:of )?my|my|everything i (?:own|have)|everything)(?: (?:things|stuff|"
    r"belongings|possessions|bikes?|kit|clothes|gear|books|records|stuff|cycling kit))?"
    r"(?: and (?:my |all my )?(?:\w+ )?(?:things|stuff|belongings|possessions|bikes?|kit|"
    r"clothes|gear|books|records))?"
    r"(?= away\b| to (?:people|friends|family|my)\b| *(?:$|[.,!?;\n]))|"
    r"(?:written|wrote|writing|write|left|leaving|drafted|drafting|recorded) (?:\w+ ){0,2}?"
    r"(?:letter|note|notes|message|messages|video|videos) (?:to|for) (?:all )?(?:my |the )?"
    r"(?:family|kids|children|wife|husband|partner|parents|mum|mom|dad|loved ones|son|"
    r"daughter|friends|everyone|people i love))\b"
)
# Count only beside words about ending it, or beside a stronger plan.
_PLAN_WEAK = (
    r"\b(?P<planweak>(?:i've|i have|i've got|i have got|i've made|i made|made|making|got|"
    r"there's|i had|have) (?:a |the |my )?plans?(?! (?:for|to (?:ride|race|train|do|go|get|"
    r"build|make)|b\b)| b\b)|(?:the|my) plan(?:'s| is) (?:made|set|ready|in place|done|"
    r"sorted|decided)|(?:made|making|make|sorted|sorting|finalised|finalized) (?:all )?"
    r"(?:the |my |some |final |last )?arrangements|(?:picked|chosen|chose|set|decided on|got) "
    r"(?:a|the) (?:date|day|time|place|spot)|said (?:my )?goodbyes?|(?:it's|it is|everything's) "
    r"(?:all )?(?:planned|sorted|arranged)|(?:written|made|updated|sorted) (?:a |my )?will)\b"
)
_PLAN_ANY_RX = re.compile(f"{_PLAN_STRONG}|{_PLAN_MID}|{_PLAN_WEAK}")

# Not waking up, never having been born, everyone better off: crisis words
# with a training context are a check-in, the same as "I want it all to end".
# "not wake up until Monday", "hope I don't wake up with sore legs".
_WAKE_ON = (
    r"(?! (?:until|till|til|before|for|early|late|at|in time|on time|with|feeling|to|tired|"
    r"sore|stiff|aching|hungry|in pain|again until|this side of)\b)"
)
_NOT_WAKE = (
    r"\b(?:go|went|going|fall|fell|falling|drift|drifting|get) (?:to sleep|asleep|off)"
    rf"(?: \w+){{0,3}}? (?:and|then) (?:just )?(?:not|never|don't|didn't) (?:wake|waking) up\b"
    rf"{_WAKE_ON}",
    r"\b(?:wish|wishes|wished|hope|hopes|hoping|pray|prays|praying|want|wants|wanted|wanna|"
    r"would like|i'd like|'d rather|would rather|rather) (?:i |to )?(?:could |would |just |"
    r"simply )*(?:not|never|didn't|"
    rf"don't|wouldn't) (?:wake|waking) up\b{_WAKE_ON}",
    r"\b(?:easier|better|nicer|best|fine|a relief|wouldn't mind|would not mind|don't mind|"
    r"do not mind|wouldn't care)\b[^.!?\n]{0,20}?\bif i (?:just )?(?:didn't|did not|never|"
    rf"don't|wouldn't) wake up\b{_WAKE_ON}",
    r"\bwish i(?:'d| had)? never (?:been born|existed)\b",
    r"\bwish i (?:didn't|did not|could not|couldn't) exist\b",
    r"\bwish i could (?:just )?(?:stop existing|cease to exist|sleep forever|go to sleep "
    r"forever|vanish|fade away|not be here)\b",
    # "Everyone would be better off.", "my family would be better off with
    # me gone". Not "the group would be better off with a slower pace".
    r"\b(?:everyone|everybody|they|people|the world|(?:my|the) (?:\w+ (?:and|&) (?:my |the )?)?"
    r"(?:family|kids|children|wife|husband|partner|mum|mom|dad|parents|friends|son|daughter|"
    r"boys|girls|team|club))(?:'d| would| will|"
    r"'ll| are| is)? (?:all |honestly |really |just |genuinely |probably |actually |so |much )*"
    r"(?:be )?better off\b(?= *(?:$|[.!?;\n])| (?:without me|if i (?:was|were|wasn't|weren't|"
    r"was not|were not|died|disappeared|'m gone|am gone|was gone|were gone|wasn't around|"
    r"weren't around|didn't exist|wasn't here|weren't here)|with me (?:gone|dead|out of the way)|"
    r"(?:once|when|after|if) i'm (?:gone|dead))\b)",
)

# A body temperature of 38.0C (100.4F) or more, in any format: "38.9",
# "39C", "101F", "a temp of 38.5". Not the weather (checked below:
# _body_temperature).
_BODY_TEMP_NUM = (
    r"(?P<bt>3[89](?:\.\d+)?|4[0-3](?:\.\d+)?|10[0-8](?:\.\d+)?)"
    r"(?:\s?(?:°|º|degrees?|deg))?\s?(?P<btu>c|f|celsius|centigrade|fahrenheit)?"
    r"(?![\w.])(?!\s?(?:km|k\b|kms|miles?|mi\b|mph|kph|w\b|watts?|kg|kgs|%|bpm|rpm|min\b|mins|"
    r"minutes?|s\b|secs?|seconds?|hours?|hrs?|m\b|metres?|meters?|psi|km/h|cal|kcal|kj|"
    r"years?|yrs?|y/o|yo\b))"
)
_BODY_TEMP_WORD = (
    r"(?:temp|temps|temperature|temperatures|thermometer|fever|reading|my temp|"
    r"body temp(?:erature)?)"
)
_BODY_TEMP_PATTERNS = (
    rf"\b(?P<btw>{_BODY_TEMP_WORD})\b[^.!?;\n]{{0,30}}?\b{_BODY_TEMP_NUM}",
    rf"\b(?P<btw>running|been running|i'm running|i am running|was running) (?:at |a |a temp of |"
    rf"a temperature of |about |around |nearly )?{_BODY_TEMP_NUM}",
    rf"\b{_BODY_TEMP_NUM}[^.!?;\n]{{0,15}}?\b(?P<btw>temp|temperature|fever|on the thermometer)\b",
)
# Body words that make a number a temperature of the rider's: "my temp",
# "the thermometer", or illness words in the message.
_BODY_TEMP_ANCHOR_RX = re.compile(
    r"\b(?:my (?:temp|temperature|body temp\w*)|thermometer|fever\w*|running a|been running|"
    r"i'm running|i am running|ill|unwell|sick|poorly|rough|awful|grim|dreadful|flu|covid|"
    r"bug|virus|infection|chills|shiver\w*|burning up|aching|aches|achy|sweats|sweating|"
    r"in bed|paracetamol|calpol|ibuprofen|lemsip|cough\w*|sore throat|feel(?:ing)? (?:hot|"
    r"terrible|horrible)|clammy|headache)\b"
)
_WEATHER_RX = re.compile(
    r"\b(?:outside|out there|forecast\w*|weather|in the shade|in the sun|sunshine|heat ?wave|"
    r"degrees out|on the road|air|ambient|garmin|wahoo|car|thermostat|room|water|pool|sea|"
    r"holiday|abroad|summer|spain|mallorca|majorca|"
    r"france|italy|arizona|desert|climb|ride|rode|riding|sportive|race|event|tour|"
    r"tyres?|tires?|track|tarmac|road|garage|shed|kitchen|house|office|greenhouse|"
    r"conservatory|gym|pain cave|turbo room|fridge|freezer|oven)\b"
)

# Gastroenteritis: sickness and diarrhoea together, or named.
_GASTRO_PATTERNS = (
    r"\b(?:sickness|vomiting|being sick|been sick|throwing up|puking|spewing) (?:and|&|\+|n|"
    r"with|plus) (?:diarrh?o?ea|diarrhea|the runs|the trots|d)\b",
    r"\b(?:diarrh?o?ea|diarrhea|the runs|the trots) (?:and|&|\+|n|with|plus) (?:sickness|"
    r"vomiting|being sick|been sick|throwing up|puking|v)\b",
    r"\bd ?(?:and|&|\+|n) ?v\b",
    r"\b(?:stomach|tummy|gastric|sickness|vomiting|winter vomiting|gut|norovirus|noro) "
    r"(?:bug|virus|flu)\b",
    r"\bgastro(?:enteritis)?\b",
    r"\bnoro(?:virus)?\b",
    r"\bfood poisoning\b",
    r"\b(?:can't|cannot|couldn't|could not|unable to) keep (?:anything|food|water|fluids|"
    r"drinks?|it|much) down\b",
    r"\b(?:been|have been|i've been|i was|was|kept|keep) (?:sick|vomiting|throwing up|being "
    r"sick)(?: \w+)? (?:all (?:night|day|morning|evening|weekend)|since (?:yesterday|last "
    r"night|this morning|\w+day)|for (?:\d+|two|three|a couple of|a few) (?:days|hours))\b",
    r"\b(?:had|got|have|i've had|i've got|with|been having) (?:bad |terrible |awful )?"
    r"(?:the runs|diarrh?o?ea|diarrhea)\b[^.!?;\n]{0,40}?\b(?:sick|vomit\w*|throw\w* up|"
    r"threw up|puk\w*|since|all (?:night|day)|for (?:\d+|two|three|a couple of|a few) days)\b",
)
_GASTRO_GUARDS = (
    # A risk, a scare or an outbreak, not an illness the rider has.
    _g("after", r"^\s*(?:risks?|scares?|warnings?|outbreaks?|in the news|going (?:a)?round)\b"),
    # Going round the office, or something to avoid.
    _g("after", r"^\s*(?:\w+\s+)?(?:bug |virus )?(?:is |that's |that is )?(?:going (?:a)?round|"
                r"doing the rounds|has been going (?:a)?round)"),
    _g("before", r"\b(?:avoid|avoiding|catch|catching|prevent|preventing|risk of|worried about|"
                 r"scared of|afraid of|in case of|hope i don't get|hope i dont get)\s+"
                 r"(?:\w+\s+){0,2}$"),
    # "the gels gave me the runs on the ride": the effort, not an illness.
    _g("before", r"\b(?:gels?|caffeine|beetroot|energy drink|bars?|sports drink|carbs?)\b"
                 r"(?:\s+\w+){0,4}\s*$"),
)
# Hot and cold together, or burning up: a fever in the rider's words.
_FEVERISH_PATTERNS = (
    r"\b(?P<feverish>(?:shiver\w*|chills|teeth chattering|freezing cold|cold sweats?|can't get "
    r"warm|couldn't get warm)\b[^.!?;\n]{0,40}?\b(?:burning up|boiling up|roasting|sweating|"
    r"sweats|feverish|hot|on fire|boiling))\b",
    r"\b(?P<feverish>(?:burning up|boiling up|sweating|sweats|feverish|hot|boiling)\b"
    r"[^.!?;\n]{0,40}?\b(?:shiver\w*|chills|teeth chattering|cold sweats?|can't get "
    r"warm|couldn't get warm))\b",
    r"\b(?P<feverish>burning up)\b(?! (?:the|a|that|this|those|my|on|in|at|during|calories|"
    r"energy|matches|kj|watts|fuel|glycogen|road|hill|climb)\b)",
)
_FEVERISH_GUARDS = (
    _g("before", r"\b(?:legs|quads|lungs|thighs|calves|muscles|glutes|engine|tyres?|brakes?|"
                 r"rims?|discs?)\b(?:\s+\w+){0,3}\s*$"),
)
# Feeling hot then cold out on the bike is the weather and the effort.
_FEVERISH_EFFORT_RX = re.compile(
    r"\b(?:climb\w*|descent|descend\w*|ride|riding|rode|turbo|session|kit|gilet|jacket|"
    r"layers?|weather|rain|wind|cafe|summit|top|interval\w*|effort|sprint|race|hill)\b"
)

# An illness that has been and gone: "had a fever last month, all better
# now", "had covid in the summer, fully recovered". Not one this week, or
# one still there.
_ILLNESS_PAST_RX = re.compile(
    r"\b(?:last (?:month|year|summer|spring|autumn|fall|winter|season|christmas|easter)|"
    r"in (?:the )?(?:summer|spring|autumn|fall|winter)|over (?:the )?(?:summer|winter|"
    r"christmas|easter)|at (?:christmas|easter)|in (?:january|february|march|april|may|june|"
    r"july|august|september|october|november|december)|back in \w+|(?:\d+|two|three|four|"
    r"five|six|seven|eight|several|a few|a couple of|many) (?:weeks|months|years) (?:ago|back)|"
    r"a (?:month|year|fortnight) ago|months ago|a while (?:ago|back)|ages ago|earlier (?:this|"
    r"in the) year|last time|years ago)\b"
)
_ILLNESS_OVER_RX = re.compile(
    r"\b(?:all better|fully recovered|recovered|better now|over it|got over it|cleared up|"
    r"fine now|(?:i'm|im|i am|all|totally|completely) fine|right as rain|back to (?:normal|"
    r"full health|full fitness|training|riding)|shaken it off|shook it off|feeling (?:great|"
    r"good|fine|normal|100%) now|no symptoms|symptom[- ]free|all clear|long gone|well (?:now|"
    r"again)|gone now|(?:totally|completely|fully) (?:better|over it|cleared|gone)|"
    r"(?:it's|it has|it) (?:all )?(?:gone|cleared|passed))\b"
)
_ILLNESS_NOW_RX = re.compile(
    r"\b(?:still|(?:ill|sick|poorly|unwell|feverish|fever|temperature|bug|flu|covid|coughing|"
    r"aching|it|symptoms) again|again (?:today|now|this week)|this week|today|tonight|yesterday|"
    r"last night|this morning|"
    r"currently|at the moment|right now|(?:a|one|two|three|four|five|six|\d) days? ago|"
    r"since (?:yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"(?:come|came|comes|coming) back|lingering|ongoing|hasn't (?:gone|cleared|shifted)|"
    r"not (?:gone|cleared|shifted|right)|can't shake|can't get rid|ever since|since then|"
    r"long covid|keep getting|keeps coming)\b"
)

# A band, belt, vice or weight round the chest or ribs, said as a feeling:
# "it felt like a belt tightening round my ribs", "a vice-like pressure in
# my chest". Not the heart-rate strap itself.
_CONSTRICTOR = (
    r"(?:band|bands|belt|belts|strap|vice|vise|clamp|weight|rope|corset|fist|hand|boa|python|"
    r"snake|elephant|brick|bricks|ton of bricks|tonne of bricks|ton|tonne|anvil|iron band|"
    r"steel band|tight band|tight belt|vice grip)"
)
_CHEST_SITE = r"(?:chest|ribs|rib ?cage|breast ?bone|sternum|torso)"
_CHEST_CONSTRICT_PATTERNS = (
    rf"\b(?:like|as if|as though|as tho|kind of like|sort of like)\b(?: [\w']+){{0,5}}? "
    rf"(?:a |an |the |some |my )?(?:\w+ )?{_CONSTRICTOR}\b[^.!?;\n]{{0,40}}?\b{_CHEST_SITE}\b",
    rf"\b{_CHEST_SITE}\b[^.!?;\n]{{0,40}}?\b(?:like|as if|as though)\b(?: [\w']+){{0,5}}? "
    rf"(?:a |an |the |some )?(?:\w+ )?{_CONSTRICTOR}\b",
    rf"\b(?:vice|vise|band|clamp|belt)[- ]?like\b[^.!?;\n]{{0,30}}?\b{_CHEST_SITE}\b",
    # "like someone was squeezing my ribs", "as if something was crushing my chest".
    r"\b(?:like|as if|as though) (?:someone|somebody|something|a \w+|an \w+)(?: \w+){0,2}? "
    r"(?:squeez\w*|crush\w*|clamp\w*|tighten\w*|press\w*|sitting on|standing on|kneeling on|"
    rf"gripping|wrapped round|wrapped around|stood on|sat on)(?: \w+){{0,2}}? (?:my |the )?"
    rf"{_CHEST_SITE}\b",
    rf"\b{_CHEST_SITE}\b[^.!?;\n]{{0,30}}?\b(?:vice|vise|band|clamp|belt)[- ]?like\b",
    # "something squeezing round my ribs", "a band tightening across my
    # chest", "tightness round my ribs on the climb".
    r"\b(?:something|it|pressure|tightness|a tightness|a squeeze|a crushing|heaviness|"
    rf"(?:a |an |the )?(?:tight |iron |steel |heavy )?{_CONSTRICTOR})(?: \w+){{0,2}}? "
    r"(?:tighten\w*|squeez\w*|crush\w*|clamp\w*|"
    r"clos\w* in|pressing|pushing down|sitting|wrapped|wrapping|gripping|constrict\w*)"
    rf"(?: \w+){{0,2}}? (?:round|around|across|on|over|in|on to|onto) (?:my |the )?{_CHEST_SITE}\b",
    r"\b(?:tightness|pressure|heaviness|squeezing|crushing|constriction|a squeeze|a band|"
    r"a tight band|tight band|band of pressure)(?: \w+){0,2}? (?:round|around|across|in|"
    r"behind|under) (?:my |the )?(?:ribs|rib ?cage)\b",
)
_CHEST_CONSTRICT_GUARDS = (
    # The strap or the kit itself, said plainly.
    _g("before", rf"\b(?:{_KIT}|heart rate|hr)\s+(?:\w+\s+){{0,3}}$"),
    _g("match", rf"^(?:{_KIT}|heart rate|hr)\b"),
    _g("match", rf"^{_CHEST_SITE}(?:'s)? (?:\w+ )?(?:{_KIT}|day|press|fly|flies|protector)\b"),
    # Ribs after a crash, a stitch, a cough, the gym.
    _g("after", r"\b(?:crash\w*|fell|fall|came off|bruis\w*|broke|broken|crack\w*|fractur\w*|"
                r"stitch|cough\w*|sneez\w*|gym|bench|lifting|massage|loosen\w*|adjust\w*|"
                r"strap|hrm|monitor)\b"),
    _g("before", r"\b(?:crash\w*|fell|fall|came off|bruis\w*|broke|broken|crack\w*|"
                 r"fractur\w*|stitch|cough\w*|sneez\w*|gym|bench|lifting|massage)\b"),
    # A weight off the chest is relief.
    _g("after", r"^\s*(?:\w+\s+){0,3}(?:lifted|off)\b"),
    _g("match", r"\b(?:lifted|off (?:my|the) (?:chest|shoulders))\b"),
)
# Breathless enough to wake them, or unable to lie flat: the heart until a
# doctor says otherwise.
_BREATHLESS_WORDS = (
    r"breathless(?:ness)?|short of breath|shortness of breath|gasping|struggling (?:to|for) "
    r"(?:breathe|breath|air)|unable to breathe|couldn't breathe|could not breathe|can't breathe|"
    r"cannot breathe|fighting for (?:breath|air)|out of breath|choking for air|"
    r"couldn't get (?:my |a )?breath|can't get (?:my |a )?breath|gasping for (?:breath|air)"
)
_CHEST_NIGHT_PATTERNS = (
    rf"\b(?:woke|wake|wakes|waking|woken|waken)(?: me)?(?: up)?\b(?: [\w']+){{0,5}}? "
    rf"(?:{_BREATHLESS_WORDS})\b",
    rf"\b(?:{_BREATHLESS_WORDS})\b(?: [\w']+){{0,4}}? (?:woke|wakes|waking|wake|keeps waking|"
    r"kept waking) me(?: up)?\b",
    r"\b(?:can't|cannot|couldn't|could not|unable to|can no longer|struggle to|struggling to|"
    r"not able to) (?:lie|lay|sleep|be)(?: lying| laying)? (?:flat|down|on my back)\b"
    rf"(?=[^.!?\n]{{0,60}}?\b(?:breath\w*|breathe|breathing|gasp\w*|air|suffocat\w*|"
    r"drowning)\b)",
    rf"\b(?:{_BREATHLESS_WORDS})\b[^.!?\n]{{0,40}}?\b(?:can't|cannot|couldn't|could not|"
    r"unable to) (?:lie|lay|sleep|be) (?:flat|down|on my back)\b",
    # "breathless when I lie flat", "lying flat makes me short of breath".
    rf"\b(?:{_BREATHLESS_WORDS})\b(?: [\w']+){{0,3}}? (?:lie|lying|lay|laying) (?:flat|down|"
    r"on my back)\b",
    r"\b(?:lie|lying|lay|laying) (?:flat|down|on my back)\b(?: [\w']+){0,4}? "
    rf"(?:{_BREATHLESS_WORDS})\b",
    r"\b(?:sleep|sleeping|slept|prop(?:ped)? (?:myself )?up) (?:\w+ ){0,3}?(?:on|with) "
    r"(?:\d|two|three|four|five|extra|more|loads of|a pile of|a stack of) pillows\b"
    r"(?=[^.!?\n]{0,40}?\b(?:breath\w*|breathe|breathing|gasp\w*|air)\b)",
    r"\b(?:have to|need to|got to|must) (?:sleep|be) (?:propped up|sitting up|sat up|"
    r"upright)\b(?=[^.!?\n]{0,40}?\b(?:breath\w*|breathe|breathing|gasp\w*|air)\b)",
)
_CHEST_NIGHT_GUARDS = (
    _g("after", r"\b(?:nightmare|bad dream|dream|blocked nose|cold|hay ?fever|snoring|"
                r"sleep apnoea|sleep apnea|reflux|heartburn|big dinner|massage|back)\b"),
    _g("before", r"\b(?:nightmare|bad dream|dream|blocked nose|reflux|heartburn|massage|"
                 r"physio)\b(?:\s+\w+){0,6}\s*$"),
)

# A heart rate of 200 or more said anywhere near the heart: "HR shot from
# 110 to 230 out of nowhere", "a heart rate of 210 while soft pedalling"
# (checked below: _hr_jump).
_HR_ANY_PATTERNS = (
    r"\b(?:heart ?rate|hr|pulse|heart|bpm)\b[^.!?;\n]{0,50}?\b(?P<bpm>2\d\d|3[0-4]\d)\b"
    r"(?! ?(?:w\b|watts?|kj|m\b|km|kms|metres?|meters?|miles?|mi\b|k\b|s\b|secs?|seconds?|"
    r"kcal|cals?|calories|ft|feet|rpm|psi|g\b|grams?|ml|%|minutes?|mins?))",
    r"\b(?P<bpm>2\d\d|3[0-4]\d) ?(?:bpm|beats(?: a| per) min\w*)\b",
)

# What a general question can name: the red flags themselves, as nouns.
_QUESTION_TOPICS = (
    r"\b(?:heart attacks?|cardiac arrests?|cardiac events?|heart problems?|chest pains?|"
    r"chest tightness|angina|palpitations?|irregular heart ?beats?|arrhythmias?|a-?fib|"
    r"faint(?:ing|s)?|pass(?:ing|es)? out|black(?:ing|s)? out|collaps(?:e|es|ing)|"
    r"concussions?|head injur(?:y|ies)|(?:a )?(?:knock|blow|bang) (?:to|on) the head|"
    r"hit(?:s|ting)? (?:your|my|their|his|her|one's) head|heat ?stroke|heat exhaustion|"
    r"sun ?stroke|overheating|fevers?|(?:a )?high temperature|covid|flu|chest infections?|"
    r"gastro\w*|sickness bugs?|food poisoning)\b"
)

# Eating only when it's earned, or so little it can't fuel the riding,
# for the sake of weight or the bike.
_EAT = (
    r"(?:eat|eating|ate|have (?:dinner|lunch|breakfast|a meal|food|anything|tea|supper|carbs|"
    r"dessert|pudding)|dinner|lunch|breakfast|food|meals?|tea|supper)"
)
_RESTRICTION_PATTERNS = (
    # "I don't let myself eat dinner", "won't allow myself to eat".
    r"\b(?:don't|dont|do not|won't|wont|will not|never|not|didn't) (?:let|letting|allow|"
    rf"allowing) myself (?:to )?(?:eat|have (?:dinner|lunch|breakfast|a meal|food|anything|"
    r"tea|supper|carbs|dessert|pudding|seconds|snacks?))\b",
    # "I only eat if I've earned it", "only have dinner once I've ridden".
    r"\b(?:only|just|can only|i can only|allowed to|let myself) (?:eat|have (?:dinner|lunch|"
    r"breakfast|a meal|food|carbs|tea|supper))\b(?: \w+){0,3}? (?:if|when|after|once|on days) "
    r"i(?:'ve| have)? (?:earned|ridden|rode|trained|burned|burnt|done|been out|exercised|"
    r"worked out|deserve)\b",
    r"\b(?:only|just|can only|allowed to) eat\b[^.!?\n]{0,30}?\b(?:earned|deserve[ds]?)\b",
    # "If I don't ride I don't eat dinner".
    r"\b(?:if|when|on days|days) (?:i|that i) (?:don't|dont|do not|haven't|didn't|can't|cant|"
    r"couldn't) (?:ride|train|cycle|exercise|get out|go out|do a session|burn|work out|"
    r"get a ride in)\b[^.!?\n]{0,30}?\b(?:i )?(?:don't|dont|do not|won't|wont|can't|cant|"
    r"skip|no|not allowed to) (?:let myself )?(?:eat|have (?:dinner|lunch|breakfast|a meal|"
    r"food|tea|supper)|dinner|lunch|food|meals?|breakfast|tea|supper)\b"
    r"(?! as (?:much|many)| so much| much| extra| the (?:extra|gels|bars)| on the bike| gels|"
    r" bars| recovery)",
    # "On rest days I don't eat".
    r"\b(?:on |my )?(?:rest|off|non-?riding) days? i (?:don't|dont|do not|barely|hardly|won't|"
    r"never) (?:let myself )?(?:eat|have)\b(?! as (?:much|many)| so much| much| extra| the "
    r"(?:extra|gels|bars)| gels| bars| recovery| carbs| snacks)",
    r"\bstarv(?:e|es|ed|ing) myself\b",
    # "a bowl of soup a day", "living on black coffee", "just a salad a
    # day": counted with weight, lightness or the bike (checked below).
    r"\b(?P<scant>(?:getting by|living|surviving|existing|running|riding|training|getting "
    r"through (?:the day|the week)|subsisting) on (?:just |only |nothing but |about |a |one |"
    r"an )?(?:\w+ ){0,3}?(?:soup|salad|apples?|black coffee|coffee|water|rice cakes?|crackers?|"
    r"fruit|yoghurt|yogurt|shakes?|lettuce|broth|cucumber|celery|bananas?|gels?|toast|"
    r"cereal bars?|one meal|a meal|crispbreads?|air))\b",
    r"\b(?P<scant>(?:only|just|nothing but|no more than|barely|at most) (?:a |one |an |two |a "
    r"single )?(?:\w+ ){0,2}?(?:soup|salad|apples?|coffee|rice cakes?|crackers?|fruit|yoghurt|"
    r"yogurt|shakes?|lettuce|broth|bananas?|toast|cereal bars?|meals?|bowls?(?: of \w+)?|"
    r"crispbreads?) (?:a|per|each) day)\b",
    r"\b(?P<scant>(?:a |one |an )?(?:bowl|cup|mug|tin|carton) of (?:soup|broth|porridge) "
    r"(?:a|per|each) day)\b",
    # "skipping dinner so I'm light for the hill climb".
    r"\b(?P<scant>(?:skip|skipping|skipped|cut|cutting|cut out|cutting out|missing|miss|not "
    r"having|no|stopped (?:eating|having)|going without|giving up) (?:\w+ ){0,2}?(?:meals?|"
    r"breakfast|lunch|dinner|tea|supper|food|eating))\b",
)
# What makes scant eating about weight or riding light.
_LIGHT_FOR_RIDING_RX = re.compile(
    r"\b(?:to be light\w*|to get light\w*|so (?:i'm|i am|im|i'll be|i can be|i stay) "
    r"(?:light|lighter|lean|leaner|skinny|thin)|(?:be|get|stay|keep|feel) (?:light|lighter|"
    r"lean|leaner|skinny|thin)|light for|lighter for|to (?:lose|drop|shed|make|cut) "
    r"(?:weight|kilos?|kg|pounds|lbs|the weight)|make weight|race weight|racing weight|"
    r"target weight|weigh(?:-| )?in|w/kg|watts per kilo|power to weight|hill ?climb|"
    r"climbs?|climbing|lose weight|losing weight|drop weight|dropping weight|weight loss|"
    r"get my weight down|for the (?:race|climb|event|sportive|season))\b"
)

# Heat illness after a hot ride or event: sick, confused or a pounding
# headache. Either order in one sentence.
_HOT_EVENT = (
    r"(?:(?:hot|scorching|boiling|baking|sweltering|roasting|blistering|sweaty|humid|muggy|"
    r"sunny|heatwave|tropical)(?: \w+){0,2}? (?:ride|rides|sportive|event|race|crit|day|days|"
    r"afternoon|morning|weather|session|stage|climb|gran fondo|fondo|audax|tt|time trial|"
    r"club run|outing|spin|conditions|one|sunday|saturday|commute|chaingang|chain gang|"
    r"century|100|tour|camp)|in the (?:heat|sun|baking sun|midday sun|blazing sun)|"
    r"(?:the|that) heat|heat of the day|heat ?wave)"
)
_HEAT_TOLL = (
    r"(?:vomit\w*|threw up|throwing up|thrown up|(?:been|was|got|being) sick|sick (?:twice|"
    r"three times|again|everywhere|all night|for hours)|confus\w*|disorient\w*|delirious|"
    r"didn't know where i was|couldn't think straight|not making sense|slurr\w*|"
    r"(?:pounding|splitting|thumping|throbbing|blinding|crushing|awful|horrendous|massive|"
    r"horrible|terrible|banging|raging|bad|stinking) headache|headache (?:for hours|all "
    r"(?:night|evening|day|afternoon))|stopped sweating|collaps\w*|passed out|fainted)"
)
_HEAT_ILLNESS_PATTERNS = (
    rf"\b(?P<hotill>{_HOT_EVENT}\b[^.!?\n]{{0,80}}?\b{_HEAT_TOLL})\b",
    rf"\b(?P<hotill>{_HEAT_TOLL}\b[^.!?\n]{{0,60}}?\b{_HOT_EVENT})\b",
)

# School years said as the rider's own: sixth form starting, Year 11,
# GCSEs, a US grade. Each implies an age (_minor_age).
_SCHOOL_PATTERNS = (
    r"\b(?:starting|start|starts|started|begin|begins|beginning|joining|join|going into|go "
    r"into|moving up to|move up to|off to|back to|going back to|in my first year (?:at|of)|"
    r"first (?:week|day|term) (?:at|of|in)) (?:at )?(?:the )?(?:lower |upper )?(?:sixth|6th)"
    r"[- ]form\b",
    r"\b(?:sixth|6th)[- ]form (?:starts|begins|started|is starting|kicks off|starting|"
    r"beginning|exams|work|homework|timetable|is (?:busy|hard|full on|intense))\b",
    r"\b(?:in|into|starting|start|starts|started|finishing|finish|doing|going into|go into|"
    r"moving up to|move up to|back to|end of|start of|half way through|halfway through) year "
    r"(?P<year>7|8|9|10|11|12|13)\b",
    r"\byear (?P<year>7|8|9|10|11|12|13) (?:mocks|exams|options|work experience|report|"
    r"parents' evening|parents evening|prom|starts|begins|exam)\b",
    r"\b(?:my|doing|sitting|revising for|taking|got|getting|get) (?:my )?gcse (?:mocks|exams|"
    r"results|options|year|revision|coursework|maths|english|science|pe)\b",
    r"\b(?:my )?gcses? (?:start|starts|are (?:coming|next|soon|in)|next|this (?:year|summer|"
    r"term)|in (?:may|june|the summer)|coming up|results day)\b",
    r"\b(?:my|got my|getting my|get my) gcses?\b",
    rf"\b(?:{_I_AM} )?(?:in|starting|start|entering|going into|finishing) (?:the )?"
    r"(?P<grade>9|10|11|12|ninth|tenth|eleventh|twelfth)(?:th)? grade\b",
    rf"\b{_I_AM} (?:a )?(?:high school|hs) (?P<hs>freshman|sophomore|junior)\b",
    rf"\b{_I_AM} (?:a )?(?P<hs>freshman|sophomore|junior) (?:in|at) high school\b",
)
_SCHOOL_GUARDS = (
    _g("before", r"\b(?:when i was|back when i was|back in|as a|i was)\s+(?:\w+\s+){0,1}$"),
    _g("after", r"^\s*(?:of\b|in a row\b|at uni|of (?:riding|racing|training|my|the))"),
    _g("before", r"\b(?:teach|teaching|teacher|teachers|tutor|tutoring|lecturer|staff|work at|"
                 r"working at|works at|head of|governor|parent|parents)\b(?:\s+\w+){0,6}\s*$"),
)

# A pregnancy said in weeks or months along: "I'm 14 weeks along", "20
# weeks gone". Not "two weeks gone since the race" or "six weeks along in
# the plan".
_WEEKS_LONG_N = (
    rf"{_WEEKS_N}|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"(?:twenty|thirty)(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?|forty"
)
_PREGNANCY_ALONG = (
    rf"\b(?:{_I_AM}|i'm now|i am now|now|currently|just|at)\s+(?:about |around |"
    rf"nearly |almost |just over |just under |roughly |over )?(?P<wa>{_WEEKS_LONG_N})(?: and a "
    r"half)?[- ](?P<ua>weeks?|wks?|months?)[- ](?:along|gone)\b(?! (?:in|into|with|on|of|"
    r"through|since|now since)\b)"
)

RULES: tuple[_Rule, ...] = (
    _Rule(
        "chest_pain", "emergency",
        _rx(
            r"\bchest(?:'s)? (?:pains?|tightness|pressure|discomfort|aches?|aching|"
            r"hurts?|hurting|is (?:sore|tight|hurting|painful|aching|heavy)|"
            r"feels? (?:tight|heavy|crushed|sore|painful|funny|weird|strange)|"
            r"felt (?:tight|heavy|crushed|sore|painful|funny|weird|strange)|"
            r"went tight|got tight|tightened|tightening|was (?:sore|tight|painful|heavy))\b",
            # Up to three words between: "my chest was hurting", "my chest has
            # been tight", "chest got really tight", "my chest is killing me".
            r"\bchest(?:'s)? (?:\w+ ){0,3}?(?:hurts?|hurting|tight|tighter|tightness|tightening|"
            r"tightened|pains?|painful|aches?|aching|pressure|heavy|heavier|heaviness|"
            r"crush\w*|squeez\w*|sore|discomfort|killing me)\b",
            r"\b(?:pains?|tightness|pressure|aches?|aching|discomfort|heaviness|crushing|"
            r"squeezing|tight feeling|a weight|tight|heavy|sharp|stabbing|dull)(?: \w+){0,2} "
            r"(?:in|across|on|around|at|behind) (?:the (?:centre|center|middle|left|right|"
            r"left side|right side|front) of )?(?:my |the )?chest\b",
            # Behind or in the breastbone (the sternum): the middle of the chest.
            r"\b(?:pains?|tightness|pressure|aches?|aching|discomfort|heaviness|crushing|"
            r"squeezing|tight feeling|a weight|tight|heavy|sharp|stabbing|dull|sore)(?: \w+){0,2} "
            r"(?:in|across|on|around|at|behind|under|beneath|below|down) (?:my |the )?"
            r"(?:breast ?bone|sternum)\b",
            r"\b(?:breast ?bone|sternum)(?:'s)? (?:\w+ ){0,3}?(?:pains?|painful|hurts?|hurting|"
            r"aches?|aching|tight|tightness|pressure|heavy|sore|discomfort|killing me)\b",
            # A burning in the middle of the chest, not heartburn or cold air
            # (checked below: _chest_burn).
            r"\b(?P<burn>burning)(?: (?:feeling|sensation|pain|ache))? (?:in|across|behind|at|"
            r"around) (?:the (?:centre|center|middle|front) of )?(?:my |the )?(?:chest|"
            r"breast ?bone|sternum)\b",
            r"\btight (?:across|around|in) (?:my |the )?chest\b",
            r"\b(?:tight|heavy|sore|painful|aching|crushing) chest\b",
            # "My chest felt like an elephant was sitting on it", "like a vice".
            # A weight lifted off the chest is relief, not pain.
            r"\bchest(?:'s)? (?:felt|feels|feel|was|is|went|has felt|had felt)(?: \w+){0,2}? like "
            r"(?:an? |the |some )?(?:elephant|weight|ton|tonne|brick|bricks|vice|vise|band|belt|"
            r"someone|something|hand|fist|rock|boulder)\b(?! (?:had been |has been |was |being )?"
            r"(?:lifted|taken off))",
            r"\b(?:an? )?(?:elephant|weight|ton of bricks|tonne of bricks|someone|something) "
            r"(?:\w+ ){0,2}?(?:sitting|sat|pressing|pushing|standing|stood|crushing|squeezing|"
            r"kneeling) (?:on|down on) (?:my |the )?chest\b",
            r"\bheart (?:really |is |was |has been |keeps |kept |started |is really |"
            r"was really )?(?:hurts?|hurting|aching|aches|painful|sore)\b",
            r"\bheart pains?\b",
            r"\b(?:i think )?(?:i'm|i am|im) having (?:a )?heart attack\b",
            r"\bam i having (?:a )?heart attack\b",
            r"\bangina\b",
            # Breathless at rest is SAFETY LAW 2a too: "came on at rest".
            r"\b(?:short of breath|breathless|out of breath|struggling to breathe|"
            r"struggle to breathe|can't breathe|cannot breathe|can't catch my breath|"
            r"can't get my breath|hard to breathe|gasping for (?:air|breath))"
            r"(?: \w+){0,2}? (?:at rest|while resting|when resting|resting|lying down|"
            r"in bed|sitting (?:down|still)|doing nothing|for no reason)\b",
            # Heart pain that isn't in the chest: the jaw, the left arm or both
            # arms with effort or breathlessness; the neck, the back or one arm
            # when it spreads or comes with breathlessness. Even after "no
            # chest pain" (checked below: _heart_ache).
            rf"\b(?P<site>{_ACHE_SITES}) (?:\w+ ){{0,2}}?(?:{_ACHE_WORDS})\b",
            rf"\b(?:{_ACHE_WORDS})(?: \w+){{0,2}}? (?:in|down|into|through|up|across|along|to) "
            rf"(?P<site>{_ACHE_SITES})\b",
        ),
        guards=(
            # The heart-rate strap, the jersey, the kit: tight, but not a chest.
            _g("before", rf"\b(?:{_KIT})\s+(?:\w+\s+){{0,2}}$"),
            _g("match", rf"^chest(?:'s)? (?:\w+ ){{0,3}}?(?:{_KIT}|day|press|fly|flies|infection|"
                        r"freezer|x-?ray|protector)\b"),
            _g("match", rf"^heart (?:{_READOUT})\b"),
            # Sore from the gym, coughing at a cold, a bench press.
            _g("after", r"^\s*(?:\w+\s+){0,3}(?:from|after) (?:the )?(?:gym|bench|"
                        r"press|push ?ups|press ?ups|weights|lifting|chest day|pec)"),
            _g("before", r"\b(?:chest day|bench press|push ?ups|press ?ups|pecs?)\s+"
                         r"(?:\w+\s+){0,3}$"),
        ),
    ),
    # A band, belt, vice or weight round the chest or ribs, said as a
    # feeling ("like a belt tightening round my ribs").
    _Rule(
        "chest_pain", "emergency",
        _rx(*_CHEST_CONSTRICT_PATTERNS),
        guards=_CHEST_CONSTRICT_GUARDS,
    ),
    # Breathlessness that wakes them, or that stops them lying flat.
    _Rule(
        "chest_pain", "emergency",
        _rx(*_CHEST_NIGHT_PATTERNS),
        guards=_CHEST_NIGHT_GUARDS,
    ),
    _Rule(
        "palpitations", "urgent",
        _rx(
            r"\bpalpitations?\b",
            r"\bheart (?:is |was |keeps |kept |has been |started )?(?:fluttering|"
            r"skipping(?: beats)?|missing beats|jumping about|flipping|flip-flopping)\b",
            r"\b(?:skipped|missed|extra|irregular|erratic) (?:heart ?beats?|beats)\b",
            r"\birregular (?:heart rate|heart rhythm|pulse)\b",
            r"\bheart (?:was |is )?racing (?:at rest|in bed|lying down|while resting|"
            r"for no reason)\b",
            r"\bracing heart (?:at rest|in bed|lying down|for no reason)\b",
            # "my heart was racing and skipping beats", "my heart went really
            # fast and irregular", "my heart was beating irregularly".
            r"\bheart (?:\w+ ){0,4}?(?:fluttering|skipping|skipped|missing beats|missed beats|"
            r"jumping about|flipping|flip-flopping|irregular(?:ly)?|erratic(?:ally)?|"
            r"out of rhythm)\b",
            r"\b(?:skipping|missing) (?:a )?beats?\b",
        ),
        guards=(
            # The heart-rate reading, or the strap or head unit giving it.
            _g("match", rf"^heart (?:\w+ ){{0,4}}?(?:{_READOUT}|{_KIT})\b"),
            _g("before", rf"\b(?:{_KIT}|{_READOUT})\s+(?:is |was |keeps |kept |has been |"
                         r"started |just |seems to be |seemed to be )?$"),
        ),
    ),
    # A fluttering said on its own: "no chest pain, just a weird fluttering".
    # Not nerves in the stomach, an eyelid, a flag, or a heart-rate trace.
    _Rule(
        "palpitations", "urgent",
        _rx(r"\bflutter(?:ing|s|y)?\b"),
        guards=(
            _g("before", rf"\b(?:eye|eyes|eyelid|eyelids|stomach|tummy|belly|butterfl\w*|nerves|"
                         rf"nervous|flags?|leaves|wings?|tape|bunting|ribbon|number|jersey|"
                         rf"curtains?|pages?|trace|graph|line|signal|power|{_KIT}|{_READOUT})\b"
                         r"(?:\s+\w+){0,3}\s*$"),
            _g("after", r"^\s*(?:in|of) (?:my |the )?(?:stomach|tummy|belly|eyes?|eyelids?|wind|"
                        r"breeze|nerves)\b"),
            _g("after", r"^\s*(?:kick|kicks|valve|board)\b"),
        ),
    ),
    # A heart racing or pounding at rest, in bed or for no reason, and a heart
    # rate that jumps to 200 or more at rest or soft pedalling (checked
    # below: _hr_jump). Not nerves or excitement before a race, and not a
    # glitch the rider names.
    _Rule(
        "palpitations", "urgent",
        _rx(
            rf"\bheart (?:\w+ ){{0,5}}?(?P<racing>{_HEART_RUNNING})\b"
            rf"(?=[^.!?;\n]{{0,60}}?\b(?:{_AT_REST})\b)",
            rf"\b(?:{_AT_REST})\b[^.!?;\n]{{0,40}}?\bheart (?:\w+ ){{0,5}}?"
            rf"(?P<racing>{_HEART_RUNNING})\b",
            r"\b(?:heart ?rate|hr|pulse)\b(?: \w+){0,3}? (?:jumped|shot|spiked|leapt|leaped|"
            r"rocketed|surged|went|jumps|shoots|spikes|goes|was|hit|sat at|stayed at|stuck at|"
            r"stayed|read)(?: from (?:about |around |roughly )?\d{2,3}(?: ?bpm)?)?(?: \w+){0,2}? "
            r"(?:to |at |over |above |past |up to |around |about |like )?(?P<bpm>\d{3})(?!\d)",
            # Any heart rate of 200 or more beside the heart, at rest or low effort.
            *_HR_ANY_PATTERNS,
            r"\b(?:like |about |around |over |at )?(?P<bpm>\d{3}) ?bpm\b(?=[^.!?;\n]{0,30}?\b"
            r"(?:for no reason|out of nowhere|at rest|while resting|sitting|lying)\b)",
        ),
        guards=(
            _g("after", r"\b(?:cadence lock|glitch\w*|dodgy|faulty|interference|static|error|"
                        r"wrong|not right|not real|picking up (?:my )?cadence)\b"),
        ),
    ),
    # Actually losing consciousness is an emergency on its own (SAFETY LAW
    # 2b); nearly fainting is urgent. Same kind, so the first that matches
    # sets the severity.
    _Rule(
        "fainting", "emergency",
        _rx(
            r"\b(?<!nearly )(?<!almost )(?<!near )fainted\b",
            r"\b(?<!nearly )(?<!almost )passed out\b",
            rf"\b(?<!nearly )(?<!almost )blacked out\b{_NOT_BLACKED_OUT_OBJECT}",
            r"\blost consciousness\b",
            r"\bi (?:just )?collapsed\b",
        ),
        guards=_FAINT_GUARDS,
    ),
    # Losing consciousness in any words, with no crash or fall: coming round
    # on the floor, out for a few seconds, blanking out (checked below:
    # _loc_check). After a crash it is the head injury rule's.
    _Rule(
        "fainting", "emergency",
        _rx(*_LOC_PATTERNS),
        guards=_FAINT_GUARDS,
    ),
    _Rule(
        "fainting", "urgent",
        _rx(
            r"\bfainting\b",
            rf"\b(?:nearly|almost|near) (?:fainted|passed out|blacked out{_NOT_BLACKED_OUT_OBJECT}|"
            r"keeled over|collapsed)\b",
            # "Went all grey and woozy and had to lie down on the verge" (checked
            # below: no crash, which is the head injury check's).
            r"\b(?P<grey>(?:went|go|goes|going|turned|turn|turning) (?:all |really |very |"
            r"a bit |quite |totally |completely )?(?:grey|gray|white|pale))\b"
            r"(?=[^.!?;\n]{0,50}?\b(?:woozy|dizzy|faint|light-?headed|wobbly|clammy|"
            r"had to (?:lie|lay|sit|get) down|lie down|lay down|sit down|"
            r"on the (?:floor|ground|verge|deck)|couldn't see|vision)\b)",
            r"\b(?P<grey>(?:woozy|light-?headed|faint))\b[^.!?;\n]{0,40}?"
            r"\bhad to (?:lie|lay) down\b",
            r"\b(?:felt|feel|feeling|went) (?:really |very |a bit |quite )?faint\b",
            r"\b(?:my )?vision (?:went|going|goes) (?:black|grey|gray|dark)\b",
            # "I thought I was going to faint", "everything went black"
            r"\b(?:going to|gonna|about to) (?:faint|pass out|black out|keel over)\b",
            r"\beverything (?:went|goes|going|turned) (?:black|grey|gray|dark|white)\b",
            # A denial the rider takes back at once: "I didn't pass out but
            # nearly did", "I haven't fainted, though I came close".
            r"\b(?:didn't|did not|haven't|have not|hasn't|wasn't|was not|never) (?:quite |actually |"
            r"fully |completely )?(?:faint(?:ed)?|pass(?:ed)? out|black(?:ed)? out|collapsed?|"
            r"keel(?:ed)? over|los(?:e|t) consciousness)\b[^.!?;\n]{0,25}?\b(?:but|though|although|"
            r"just)\b[^.!?;\n]{0,15}?\b(?:nearly|almost|came close|close to it|very close|near enough)\b",
            # "I didn't black out but I lost a few seconds" (checked below:
            # faint words or an effort nearby, and no crash).
            _LOST_TIME,
            # A denial they aren't sure of: "I didn't pass out I don't think"
            # (checked below: _UNSURE_AFTER).
            r"\b(?P<unsure>(?:didn't|did not) (?:quite |actually |fully )?(?:faint|pass out|"
            r"black out|lose consciousness))\b",
        ),
        guards=_FAINT_GUARDS,
    ),
    # A knock to the head about the house, shrugged off: "Hit my head on the
    # cupboard door, ouch, anyway", "banged my head on the garage door, no harm
    # done". No card and no hold; recorded, and the coach asks once about
    # symptoms. Anything more (a crash, a sign of a head injury, being knocked
    # out) makes it a head injury (checked below: _household_knock).
    _Rule(
        "head_knock", "info",
        _rx(*_BLOW_PATTERNS),
        guards=_HEAD_GUARDS,
        third_party=False,
    ),
    _Rule(
        "head_injury", "urgent",
        _rx(
            *_BLOW_PATTERNS,
            rf"\b{_HEAD_PART} (?:hit|struck|smacked|bounced off|went into|slammed into|"
            r"smashed into|hitting|smacking|bouncing off|going into|slamming into|smashing into) "
            rf"(?:the |my |a |an |some )?(?:\w+ )?(?:{_SURFACE})\b",
            rf"\blanded (?:right |straight |flat )?on (?:my |the side of my |the back of my |the "
            rf"top of my )?{_HEAD_PART}\b",
            # "went head first into the barrier", "landed head first on the road".
            r"\b(?:went|go|going|landed|landing|flew|flying|dived|dove|thrown|pitched|fell|"
            r"falling) head ?-?first (?:into|onto|over|on|off|through|in) (?:the |a |an |some )?"
            rf"(?:\w+ )?(?:{_SURFACE})\b",
            # The face or the helmet taking the blow, after a crash or a fall
            # (checked below): "my face hit the tarmac", "smashed my face
            # into the road", "face-planted".
            r"\b(?P<faceblow>(?:hit|banged|bashed|smacked|smashed|cracked|whacked|slammed|planted|"
            r"split|cut|gashed|bust|busted) (?:my |the side of my |the front of my )?(?:face|chin|"
            r"jaw|nose|cheek|cheekbone|eye ?socket|lip|helmet)) (?:\w+ )?(?:on|into|against|off|"
            rf"onto|in to) (?:the |a |an |some )?(?:\w+ )?(?:{_SURFACE})\b",
            r"\b(?P<faceblow>(?:my )?(?:face|chin|jaw|nose|cheek|helmet|lid) (?:hit|struck|smacked|"
            r"slammed into|smashed into|went into|bounced off|took|met|scraped along|skidded along|"
            r"hitting|smacking|slamming into|smashing into|going into|bouncing off)) "
            r"(?:the |a |an |some )?(?:\w+ )?"
            rf"(?:{_SURFACE}|full force|the brunt|the impact|the hit|most of it)\b",
            r"\b(?P<faceblow>face-?plant(?:ed|ing|s)?|faceplant(?:ed|ing|s)?)\b",
            r"\bconcuss(?:ion|ed)\b",
            # A damaged helmet (checked below: not a strap or a visor, and not
            # dropped or sat on, unless the rider crashed or took a blow).
            r"\b(?:cracked|broke|broken|split|smashed|snapped|dented) (?:my |the )?"
            r"(?P<helmet>helmet)\b",
            r"\b(?P<helmet>helmet) (?:is |was |got |has )?(?:cracked|broken|split|smashed|snapped|"
            r"dented)\b",
            r"\b(?P<helmet>helmet)\b[^.!?;\n]{0,25}\b(?:cracked|split|smashed|snapped|dented)\b",
            r"\bhead (?:injury|injuries|knock|trauma)\b",
        ),
        guards=_HEAD_GUARDS,
        third_party=False,  # every pattern is already about "my" head or helmet
    ),
    # Knocked out, but not by a session, the heat or a cold ("that session
    # knocked me out", "the heat knocked me out today").
    _Rule(
        "head_injury", "urgent",
        _rx(_KNOCKED_OUT),
        guards=(
            _g("after", r"^\s*(?:of\b|in the (?:first|second|third|next|\w+) "
                        r"(?:round|heat|stage)|early|by)"),
            _g("before", _NOT_A_BLOW.pattern),
        ),
        third_party=False,
    ),
    # Signs of a head injury after a crash, said with no word about the head:
    # "a headache since the crash yesterday", "felt sick and dizzy since I
    # crashed", "didn't hit my head I think but I feel sick". Checked below
    # (_incident): something happened to the rider, recently.
    _Rule(
        "head_injury", "urgent",
        _rx(_HEAD_SYMPTOM),
    ),
    # A gap in memory around a crash is a sign of a head injury on its own:
    # "I can't remember the crash", "no memory of hitting the ground". Not
    # "I can't remember the last time I crashed", and not someone else's.
    _Rule(
        "head_injury", "urgent",
        _rx(
            r"\b(?:don't|dont|do not|can't|cant|cannot|can not|couldn't|could not) "
            r"(?:really |even |actually |properly )?(?:remember|recall)\b(?: [\w']+){0,4}? "
            r"(?:the |my |that )?(?:crash|crashing|accident|fall|falling|impact|landing|"
            r"coming off|going down|(?:hit|hitting) the (?:ground|road|deck|floor|tarmac)|"
            r"what happened|"
            # "can't remember the bit before I hit the ground"
            r"(?:bit|part|moment|seconds?|minutes?|anything|much|everything) (?:just )?"
            r"(?:before|after|of) (?:i |the |my )?(?:hit|hitting|crash\w*|came off|coming off|fell|"
            r"falling|went down|going down|impact|landed|landing|went over))\b",
            r"\b(?:no|have no|got no|i've no) (?:memory|recollection) of (?:the |my |that )?"
            r"(?:crash|crashing|accident|fall|falling|impact|landing|coming off|going down|"
            r"hitting|what happened)\b",
            r"\b(?:memory (?:gap|loss)|gap in my memory|my memory is (?:hazy|blank|patchy))\b"
            r"(?: [\w']+){0,4}? (?:after|from|since|around) (?:the |my |that )?"
            r"(?:crash|accident|fall|coming off|off)\b",
        ),
        guards=(
            # Remembering a date, a count or a route is not a memory gap.
            _g("match", r"\b(?:last|first|time|times|how many|which|when|where|date|year|"
                        r"name|details|route|segment)\b"),
        ),
    ),
    # Knocked out in any words after a crash or a fall: "came to with
    # paramedics around me", "out for about a minute after I hit the deck"
    # (checked below: _loc_check, with an incident).
    _Rule(
        "head_injury", "urgent",
        _rx(*_LOC_PATTERNS),
        guards=_FAINT_GUARDS,
    ),
    # A gap in memory after a crash, a collision or a fall, in any words:
    # "can't remember how I got home", "don't remember the ride back", "lost
    # a chunk of time" (checked below: an incident in the message).
    _Rule(
        "head_injury", "urgent",
        _rx(*_AMNESIA_PATTERNS),
    ),
    _Rule(
        "fever", "urgent",
        _rx(
            r"\bfever(?:ish|s)?\b",
            r"\b(?:the |got |have |had |caught |down with |getting over )flu\b",
            r"\bcovid\b",
            r"\bchest infection\b",
            r"\b(?:got|have|had|with|and) (?:the )?chills\b",
        ),
        guards=(
            # Hay fever is an allergy, not a fever, and "race day fever" or
            # "Tour de France fever" is excitement. Glandular, scarlet or
            # yellow fever is still a fever.
            _g("before", rf"(?:\bhay[- ]?|\b(?:{_FEVER_IDIOM})\s+)$"),
            _g("after", r"^\s*pitch\b"),
            # A jab is not an illness, and the pandemic is not a symptom.
            _g("after", r"^\s*(?:jab|shot|vaccine|vaccination|booster|test(?:s)? "
                        r"(?:was |came back |were )?negative|lockdowns?|times|era|"
                        r"pandemic|restrictions)"),
            _g("before", r"\b(?:since|during|before|after|pre|post)[- ]?\s*$"),
        ),
    ),
    # A temperature reading, kept apart so the weather guards only touch it.
    _Rule(
        "fever", "urgent",
        _rx(
            r"\b(?:running|got|have|had|with|i've a|i have a|i had a) (?:a )?"
            r"(?:high )?temperature\b",
            r"\b(?:my (?:temperature|temp) (?:is|was|hit|went up to|reading)|"
            r"(?:temperature|temp) of) (?:about |over |nearly )?3[89](?:\.\d)?\b",
        ),
        guards=(
            _g("after", r"^\s*(?:today|tomorrow|outside|out there|forecast|this week|"
                        r"of (?:the )?(?:day|room|water|air))"),
            _g("before", r"\b(?:weather|forecast|outside|air|room|water|road|tyre|tire|"
                         r"the)\s+(?:\w+\s+){0,2}$"),
        ),
    ),
    # A measured temperature of 38.0C (100.4F) or more in any format, said
    # of the rider (checked below: _body_temperature).
    _Rule(
        "fever", "urgent",
        _rx(*_BODY_TEMP_PATTERNS),
    ),
    # Shivery and burning up: a fever in the rider's own words.
    _Rule(
        "fever", "urgent",
        _rx(*_FEVERISH_PATTERNS),
        guards=_FEVERISH_GUARDS,
    ),
    # Sickness and diarrhoea: an illness that stops training like a fever.
    _Rule(
        "fever", "urgent",
        _rx(*_GASTRO_PATTERNS),
        guards=_GASTRO_GUARDS,
    ),
    # A general question about a red flag the symptom rules have no words for:
    # "what are the signs of a heart attack?", "if I ever pass out on a ride
    # what should my mates do?". Only ever a safety_question (checked in
    # _first_hit: _hypothetical).
    _Rule(
        "safety_question", "info",
        _rx(_QUESTION_TOPICS),
        negatable=False,
        third_party=False,
    ),
    _Rule(
        "injury", "info",
        _rx(
            r"\b(?:i'm|i am|im|i've been|i got|got|been|i was) (?:badly |a bit |slightly )?injured\b",
            r"\b(?:my|an|this|old|new|recent) (?:\w+ )?injury\b",
            r"\b(?:sprained|twisted|rolled|tweaked) (?:my )?(?:ankle|wrist|knee|back)\b",
            r"\bsprain(?:ed)?\b",
            rf"\b(?:broke|broken|fractured|fractures?|cracked) (?:my |a )?(?:{_BONE})\b",
            r"\bstress fracture\b",
            r"\bdislocat(?:ed|ion)\b",
            r"\b(?:tendinitis|tendonitis|tendinopathy|plantar fasciitis|bursitis)\b",
            r"\b(?:itb|it band|iliotibial)(?: band)? (?:syndrome|pain|issues?|problems?|flare)",
            r"\btorn (?:my )?(?:acl|mcl|meniscus|hamstring|calf|muscle|ligament|"
            r"cartilage|rotator cuff|achilles)\b",
            r"\b(?:pulled|strained|tore) (?:a |my )?(?:muscle|hamstring|calf|groin|quad|back)\b",
            r"\b(?:sharp|shooting|stabbing|searing|severe|worsening|constant) pains?\b",
            r"\bpain (?:is )?(?:getting worse|keeps getting worse|worse every)\b",
            rf"\b(?:{_BODY}) (?:pain|injury|problem|niggle)\b",
            rf"\bmy (?:{_BODY})(?:'s|s)? (?:is |are |has been |have been |'s been |keeps |keep )?"
            r"(?:killing me|"
            r"hurts?|hurting|really hurts|painful|swollen|swelling|giving way|gives way|"
            r"locking|locked|clicking painfully)\b",
            r"\bcan't (?:put|bear) (?:any )?weight\b",
            rf"\bswollen (?:{_BODY}|joint)\b",
        ),
        guards=(
            # Talking about injury, not having one.
            _g("before", r"\b(?:avoid|avoiding|prevent|preventing|prevention of|"
                         r"risk of|reduce the risk of|insult to|without)\s+(?:\w+\s+)?$"),
            _g("after", r"^[\s-]*(?:time|free|prevention|risk|prone)\b"),
            _g("before", r"\b(?:no pain,? no gain|pain cave)\b"),
            # The bike, not the body.
            _g("after", r"^\s*(?:frame|wheel|spoke|chain|saddle|derailleur|hanger|"
                        r"carbon|bars|handlebars?|crank|mech|pedal)\b"),
        ),
    ),
    _Rule(
        "pregnancy", "info",
        _rx(
            # What matched keeps the week count and any "again", so a later
            # mention can be told apart from the pregnancy a clearance covered
            # (_new_pregnancy): "8 weeks pregnant", "pregnant again, 8 weeks".
            _PREGNANCY,
            # "I'm 14 weeks along", "now 20 weeks gone".
            _PREGNANCY_ALONG,
            rf"\b(?:my |a |another |this )?(?P<mark>{_AGAIN_WORDS}) pregnancy\b",
            r"\b(?:postpartum|post-partum|post partum|postnatal|post-natal|post natal)\b",
            r"\bi (?:just |recently |only )?(?:had|gave birth to) (?:a|my|our) "
            r"(?:baby|son|daughter|little one|twins)\b",
            r"\b(?:since|after) giving birth\b",
            r"\bi gave birth\b",
            r"\b(?:my )?(?:c-section|c section|caesarean|cesarean)\b",
            r"\bbreastfeeding\b",
            # "I'm in my second trimester", "third trimester now".
            r"\b(?:first|second|third|1st|2nd|3rd|last|final) trimester\b",
            # "Just found out I'm expecting!", "I'm expecting a baby", "I'm
            # expecting in March". Not "I'm expecting a parcel" or "a hard race".
            rf"\b{_I_AM} (?:now |currently |also |finally )?expecting\b(?= *(?:$|[.!?,;:)\n]|"
            r"again\b|(?:a |my |our |another |the )?(?:baby|babies|twins|triplets|child|"
            r"little one)\b|"
            r"(?:our|my) (?:first|second|third|fourth|1st|2nd|3rd|4th|next)\b|"
            r"a second (?:baby|child)\b|in (?:january|february|march|april|may|june|"
            r"july|august|september|october|november|december|the (?:spring|summer|autumn|fall|"
            r"winter|new year))\b))",
            # "I'm the one carrying", "I'm carrying our baby".
            rf"\b{_I_AM} the one (?:carrying(?= *(?:$|[.!?,;:)\n]|this time\b|(?:the|our|my) "
            r"baby\b|it\b|and\b|so\b|but\b))|who's pregnant|who is pregnant|that's pregnant|"
            r"having (?:the|our) baby)\b",
            rf"\b{_I_AM} carrying (?:a baby|the baby|our baby|my baby|twins)\b",
            # "We're expecting our first": counted only when the rider is the
            # one carrying (checked below: _CARRYING_RX), since it is as often
            # said by the partner.
            r"\b(?P<we>(?:we're|we are|were) expecting) (?:a baby|our (?:first|second|third|1st|"
            r"2nd|3rd|fourth|next)(?: (?:baby|child|little one))?|twins|a little one|another)\b",
        ),
        guards=(
            _g("before", r"\btrying (?:to get|for|to be)\s+$"),
            _g("before", r"\b(?:trying for a baby)\b"),
            # "Pregnant with possibilities".
            _g("after", r"^\s*with (?:possibilit\w*|potential|promise|meaning|hope|"
                        r"anticipation|ideas?|excitement|expectation\w*|opportunit\w*|intent|"
                        r"menace|danger|significance|emotion|tension|drama)\b"),
        ),
    ),
    _Rule(
        "medication", "info",
        _rx(
            r"\bbeta[- ]?blockers?\b",
            r"\b(?:bisoprolol|atenolol|propranolol|metoprolol|nebivolol|carvedilol|"
            r"sotalol|labetalol)\b",
            r"\binsulin\b",
            r"\b(?:warfarin|apixaban|eliquis|rivaroxaban|xarelto|edoxaban|"
            r"dabigatran|pradaxa|clopidogrel)\b",
            r"\b(?:blood thinners?|anti-?coagulants?)\b",
            # Blood pressure medicines: they lower the pressure the heart
            # works against, and some cause dizziness on standing up.
            rf"\b(?:{_BP_MEDICINES})\b",
            r"\b(?:heart|blood pressure|bp) (?:medication|medicine|meds|tablets|pills)\b",
            # "tablets for my blood pressure", "medication for my heart".
            r"\b(?:medication|medicine|medicines|meds|tablets|pills)(?: \w+)? for (?:my )?"
            r"(?:high )?(?:blood pressure|bp|hypertension|heart)\b",
            # Oral steroids: "a course of steroids", "I'm on prednisolone".
            r"\b(?:prednisolone|prednisone|dexamethasone)\b",
            r"\b(?:a (?:short )?)?course of (?:oral )?steroids\b",
            rf"\b(?:{_I_AM}|i've been|ive been|been|i was|currently|they've put me|put me|"
            r"the doctor put me|my gp put me|the gp put me) on (?:oral )?steroids\b",
            rf"\b(?:i take|{_I_AM} taking|i've been taking|been taking|i've started|i started|"
            r"started on|i got|i've got|i was prescribed|been prescribed|prescribed me) "
            r"(?:some )?(?:oral )?steroids\b",
            r"\bsteroid (?:tablets|course)\b",
        ),
        guards=(
            # Sports nutrition talks about insulin all the time.
            _g("after", r"^\s*(?:spikes?|response|sensitivity|resistance|levels?|"
                        r"index|release|production|surge|peaks?|secretion)\b"),
        ),
    ),
    _Rule(
        "condition", "info",
        _rx(
            r"\b(?:i have|i've got|ive got|i've been diagnosed with|i was diagnosed with|"
            r"diagnosed with|i suffer from|i've had|i live with) (?:a |an )?"
            r"(?:heart condition|heart problem|heart murmur|arrhythmia|atrial "
            r"fibrillation|af|afib|a-fib|cardiomyopathy|hypertension|high blood "
            r"pressure|epilepsy|type 1 diabetes|type 2 diabetes|diabetes|asthma|"
            r"long covid)\b",
            # "I'm diabetic about my power numbers" is a figure of speech.
            r"\b(?:i'm|i am|im) (?:a )?(?:type [12] )?diabetic\b(?! about\b)",
            r"\b(?:i'm|i am|im) (?:an )?(?:epileptic|asthmatic)\b",
        ),
    ),
    _Rule(
        "restriction", "info",
        _rx(
            # A daily calorie figure (checked below: under 1,800 and about eating).
            r"\b(?P<kcal>\d{3,4}|\d,\d{3}) ?(?:k?cals?|calories|kcal)\b",
            r"\bskip(?:ping)? meals\b",
            r"\b(?:barely|hardly) eat(?:ing)?\b",
            r"\bnot eating (?:much|enough|anything|properly)\b",
            r"\b(?:only|just) eat(?:ing)? (?:once|one meal) a day\b",
            r"\bomad\b",
            r"\bpurg(?:e|ing) (?:after|what|when|if)\b",
            # "I've been purging". Not purging the garage, old kit or files.
            r"\b(?:i've been|ive been|i have been|been|i'm|im|i am|i keep|keep|started|i've|"
            r"and|then|i) purg(?:e|ed|ing)\b(?! (?:the|my|old|all|some|files?|data|photos?|"
            r"wardrobe|kit|garage|shed|cupboards?|emails?|inbox|strava|garmin|cache|history|"
            r"list|records?|account|stuff|clutter|clothes|everything|bikes?|of|through)\b)",
            r"\blaxatives?\b",
            r"\bbinge(?:d|ing)?\b",
            r"\b(?:missed|missing|lost|stopped having|haven't had|have not had) "
            r"(?:my |a )?periods?\b",
            r"\bperiods? (?:has|have) stopped\b",
            # A target weight loss: checked below, a red flag only faster than
            # about 1% of body weight a week.
            r"\b(?:lose|drop|shed|cut|get rid of|take off) (?P<amount>\d+(?:\.\d)?) ?"
            r"(?P<unit>kg|kgs|kilos?|lbs?|pounds|stone) in (?:(?P<count>\d+|a|an|one|two|"
            r"three|four|five|six|eight|ten) )?(?P<period>days?|weeks?|fortnight|months?)\b",
            r"\b(?:fear of food|scared of eating|afraid to eat|afraid of eating)\b",
            r"\bguilty (?:about|after|when|every time|everytime|whenever|each time|if) (?:i )?"
            r"(?:eat|eating|ate|i've eaten)\b",
            # Cutting out carbohydrate altogether, or riding fasted every day.
            r"\b(?:stopped|stop|cut out|cutting out|given up|giving up|quit|quitting|dropped|"
            r"banned|ditched|cut) (?:eating )?(?:all )?carbs?(?: \w+)? (?:completely|altogether|"
            r"entirely|totally)\b",
            r"\b(?:completely|totally|entirely) (?:stopped|cut out|given up|quit|dropped|ditched|"
            r"cut) (?:eating )?(?:all )?carbs?\b",
            r"\b(?:no|zero) carbs? (?:at all|whatsoever|ever)\b",
            r"\b(?:riding|ride|rides|train|training|do (?:all )?my (?:rides|riding|training)|"
            r"doing (?:all )?my (?:rides|riding|training)) fasted (?:every day|every ride|all the "
            r"time|daily|on every ride|for every ride|every session|each day|for everything)\b",
            r"\b(?:every|all (?:of )?my) (?:ride|rides|session|sessions) (?:is |are )?(?:done )?"
            r"fasted\b",
            r"\bearn (?:my|the) (?:food|dinner|meals?|calories|lunch|breakfast)\b",
            r"\bburn off (?:what i ate|dinner|lunch|breakfast|the calories|calories|that meal)\b",
        ),
        guards=(
            _g("after", r"^\s*(?:watch|watched|watching|on netflix|on tv|the series)"),
            _g("before", r"\bnetflix\b"),
        ),
    ),
    # Making yourself sick after eating. Not the effort ("made myself sick on
    # the last interval") or being sick of something.
    _Rule(
        "restriction", "info",
        _rx(r"\b(?:make|makes|making|made) myself (?:sick|throw up|vomit)\b(?! of\b)"),
        guards=(
            _g("after", rf"^\s*(?:on|during|in|at|with|from|doing|pushing|going|riding|"
                        rf"trying|through)\s+(?:\w+\s+){{0,3}}?(?:{_EFFORT}|nerves|worry|"
                        r"worrying|stress|pace|effort|efforts|it)\b"),
            _g("after", r"^\s*(?:with|from) (?:nerves|worry|worrying|stress|excitement)\b"),
        ),
    ),
    # Eating only when it's earned, or so little it can't fuel the riding,
    # for weight or the bike (checked below: _scant_for_weight).
    _Rule(
        "restriction", "info",
        _rx(*_RESTRICTION_PATTERNS),
        guards=(
            _g("after", r"^\s*(?:watch|watched|watching|on netflix|on tv|the series)"),
            # A missed meal with a reason that isn't weight: work, a long
            # ride with food on it, a fast before a test.
            _g("after", r"^\s*(?:\w+\s+){0,4}(?:because|cos|as) (?:of )?(?:work|the meeting|"
                        r"i was busy|i forgot|traffic|a blood test|surgery|an operation|"
                        r"ramadan)\b"),
        ),
    ),
    # An age the rider gives as their own. Each pattern names the age it
    # implies (_minor_age): "age" is the age itself, "turning" the birthday
    # coming up, "year" the school year (Year 7 starts at 11 in England and
    # Wales). Retention needs it: a minor's records are kept to their 21st
    # birthday, worked out from this age rather than the adult date of birth
    # they must have given to sign up.
    _Rule(
        "minor", "minor",
        _rx(
            # "I'm actually 15", "honestly I'm really only 16".
            rf"\b{_I_AM} (?:(?:actually|really|honestly|genuinely|truthfully) )?"
            rf"(?:only |just |nearly |almost |still |now )?(?P<age>{_AGE_WORD})\b"
            rf"(?!\s*(?:{_NOT_AN_AGE}|out\b|off\b|down\b|up\b|behind|ahead|from|into|in\b|at\b|"
            r"on\b|of\b|for\b|over\b|under\b|short|clear|back|again\b))",
            # "I'm nearly 18" is 17.
            rf"\b{_I_AM} (?:nearly|almost|not yet|not quite) (?P<turning>18|eighteen)\b"
            rf"(?!\s*(?:{_NOT_AN_AGE}))",
            # Not "when I turned 16": that's a memory.
            r"(?<!when )(?<!since )(?<!after )(?<!before )(?<!until )"
            r"\b(?:my age is|i turned|i just turned|i've just turned|i have just turned) "
            rf"(?P<age>1[0-7])\b(?!\s*(?:{_NOT_AN_AGE}))",
            r"\bi'll be (?P<turning>1[0-7]|eighteen|18) (?:next|this|in)\b",
            # Year 12 is 16 to 17 in England and Wales, Year 13 is 17 to 18.
            rf"\b{_I_AM} (?:in )?year (?P<year>7|8|9|10|11|12|13)\b",
            rf"\b{_I_AM} (?:a |an )year (?P<year>7|8|9|10|11|12|13) (?:student|pupil)\b",
            r"\b(?:doing|sitting|revising for|taking) (?:my )?gcses?\b",
            rf"\b{_I_AM} (?:a |an |only a |just a )?(?P<age>{_AGE_WORD})[- ]?(?:years?|yrs?)"
            r"[- ]?old\b",
            rf"\b{_I_AM} (?:in |at |doing )?(?:the |my )?(?:lower |upper )?(?:sixth|6th)[- ]form\b",
            r"\bi (?:go to|attend|study at) (?:a |my |the )?(?:sixth|6th)[- ]form\b",
            # "My mum says I'm too young at 15", "I'm too young to race at 16".
            rf"\b{_I_AM} (?:\w+ ){{0,2}}?(?:too young|under ?age|not old enough)\b"
            rf"(?: [\w']+){{0,4}}? (?:at|aged?|being) (?P<age>{_AGE_WORD})\b"
            rf"(?!\s*(?:{_NOT_AN_AGE}))",
            # "I'm a junior rider, 16". Never "junior" alone: a junior doctor
            # is an adult, and junior gears are a gear ratio.
            rf"\b{_I_AM} (?:a |an )?(?:junior|youth|school ?boy|school ?girl|student|pupil)"
            r"(?: (?:rider|racer|cyclist|road racer|track rider|triathlete|athlete))?,? "
            rf"(?:aged |age )?(?P<age>{_AGE_WORD})\b(?!\s*(?:{_NOT_AN_AGE}))",
        ),
        guards=(
            _g("after", r"^\s*years? (?:into|of|in|riding|cycling|racing|on|since|"
                        r"ago|older|younger|clean|sober|married|training)"),
            _g("after", r"^\s*(?:of|in) (?:my |the |our )?(?:plan|training|cycling|racing|"
                        r"riding|block|build)"),
            # "year 13 of riding", "year 12 in a row".
            _g("after", r"^\s*(?:of\b|in a row\b)"),
            # A weight or a power figure: "I'm 16 and a half stone", "I'm 16
            # and 18 watts up".
            _g("after", _SHARED_UNIT),
        ),
        negatable=False,
    ),
    # An age said without "I'm": "15 year old here", "16yo here", "age 15",
    # "turning 17 next month", "my sixth form". Someone else's age, a
    # category or a past age is left alone ("aged 16 I broke my collarbone").
    _Rule(
        "minor", "minor",
        _rx(
            rf"\b(?P<bare>{_AGE_WORD})[- ](?:years?|yrs?)[- ]old\b",
            r"\b(?P<bare>1[0-7]) ?(?:yo|y/o|y\.o)\b",
            r"\b(?:age:?|aged) (?P<bare>1[0-7])\b",
            r"\bmy (?:sixth|6th)[- ]form\b",
            # "turning 17 next month", "I turn 18 in March": a birthday to come.
            rf"\b(?:turning|turn) (?P<turning>{_AGE_WORD}|18|eighteen)\b"
            r"(?= (?:next|this|in|on|soon|later|tomorrow|before|at the end)\b)",
        ),
        guards=(
            _g("before", r"(?<!\bas )\b(?:my|our|his|her|their|your|a|an|the|this|that|these|"
                         r"those|every|each)\s+$"),
            _g("before", r"\b\w+'s\s+$"),
            # "my bike is 16 years old": the first person is the rule above.
            _g("before", r"\b(?:is|was|are|were|turns|turned|be|'s|it's|it is)\s+(?:nearly |"
                         r"almost |about |over |now )?$"),
            _g("before", r"\b(?:at|since|from|started|start|starting|began|begin|when i was|"
                         r"back when|back in|before|after|until|till|by|in my|for)\s+$"),
            # "aged 16 and over", but not "aged 16 and I want to race".
            _g("after", r"^\s*(?:-|to\b|and\b(?!\s+(?:i|i'm|im|i've|i'd|i'll|my)\b)|or\b|"
                        r"category|cat\b|group|bracket|division|"
                        r"races?\b|squad|team|club|section|bike|frame|car|helmet|wheels?|"
                        r"tyres?|kit|shoes|saddle|record|photo|picture|video|article|book|"
                        r"son|daughter|kids?\b|child|children|nephew|niece|grandson|"
                        r"granddaughter|juniors|riders|days|years)"),
        ),
        negatable=False,
    ),
    # The school year said as the rider's own: "Sixth form starts next week",
    # "starting Year 12 in September", "my GCSE mocks", "in 10th grade".
    # Someone else's (a son, a pupil) or a year long past is left alone.
    _Rule(
        "minor", "minor",
        _rx(*_SCHOOL_PATTERNS),
        guards=_SCHOOL_GUARDS,
        negatable=False,
    ),
    # Words of crisis that no training context explains away: ending their
    # life, hurting themselves, not wanting to be here, riding into a lorry.
    # These always bring the crisis card and tell Gareth.
    _Rule(
        "crisis", "crisis",
        _rx(
            r"\bkill(?:ing)? myself\b",
            r"\b(?:want|wanna|going|gonna|plan|planning|thinking about|thought about|"
            r"thinking of|thought of|tempted|ready) (?:to )?(?:end (?:it all|my life|"
            r"things|it)|ending (?:it all|my life|things|it)|take my (?:own )?life|"
            r"taking my (?:own )?life|die|dying|be dead|not wake up|hurt myself|"
            r"hurting myself|harm myself|harming myself|cut myself|cutting myself)\b",
            r"\bend it all\b",
            r"\bending it all\b",
            r"\btake my (?:own )?life\b",
            r"\bwish i (?:was|were) dead\b",
            r"\bwish i (?:wasn't|weren't|was not|were not) (?:here|alive|around|born)\b",
            r"\bwish i (?:could|would) (?:just )?(?:disappear|not wake up|never wake up)\b",
            r"\bbetter off (?:without me|dead|if i (?:wasn't|weren't|was not|were not) "
            r"(?:here|around|alive))\b",
            r"\b(?:don't|dont|do not) want to (?:be here|live|be alive|exist|wake up)"
            r"(?: any ?more)?\b",
            r"\bno (?:point|reason) (?:in )?(?:living|life|me being here|being alive)\b",
            r"\bwhat'?s the point (?:of|in) (?:living|life|me|being alive)\b",
            r"\b(?:don't|dont|do not|can't|cant|cannot) see (?:the|any) point (?:in|of) "
            r"(?:living|life|being here|being alive|me)\b",
            r"\b(?:want|wanted|wanna|need|wish|just want) (?:my life|this life|life) to "
            r"(?:end|stop|be over)\b",
            r"\b(?:nobody|no one|no-one|noone)(?:'d| would| will| is going to| is gonna| would "
            r"even| would really| would actually| will even)? miss me\b",
            r"\b(?:nobody|no one|no-one|noone)(?:'d| would| will)?(?: even)? (?:notice|care) if "
            r"i (?:was|were|wasn't|weren't|died|disappeared)\b",
            r"\bself[- ]?harm(?:ing)?\b",
            r"\b(?:hurting|harming|cutting) myself\b",
            # Suicide intervals and suicide sprints are a drill, not a crisis.
            r"\bsuicid(?:e|al)\b(?! (?:intervals?|sprints?|drills?|efforts?|sets?|reps?|"
            r"repeats?|hill repeats?|laps?|shuttles?|runs?|climbs?|sessions?|workouts?|"
            r"squats|burpees|blocks?|pace)\b)",
            r"\b(?:i'm|i am|im|i feel|i felt|feel|feeling|i've been feeling|makes me feel|"
            r"made me feel) (?:so |completely |totally |utterly |just |really |pretty |"
            r"like )*(?:i'm |a )?worthless\b",
            r"\boverdos(?:e|ed|ing)\b",
        ),
        guards=_CRISIS_GUARDS + (
            # "I want to die every time I see the hill reps": a reaction to
            # the plan, checked kindly below as a distress_check.
            _g("after", rf"^\s*{_EVERY_TIME_TRAINING}"),
        ),
        negatable=False,
        third_party=False,
    ),
    # Pills saved up or stockpiled: a plan, whatever else the message says.
    # Not caffeine, salt or electrolyte tablets.
    _Rule(
        "crisis", "crisis",
        _rx(
            rf"\b(?:{_PILLS})\b (?:\w+ ){{0,2}}?(?:saved(?: up)?|stockpiled|hoarded|"
            r"stashed away|put by|put aside|squirrell?ed away)\b",
            rf"\b(?:stockpil\w*|sav(?:ed|ing|e) up|hoard(?:ed|ing)|stash(?:ed|ing)|"
            rf"stor(?:ed|ing) up|squirrell?(?:ed|ing) away|a stockpile of) (?:\w+ ){{0,2}}?"
            rf"(?:{_PILLS})\b",
        ),
        guards=_CRISIS_GUARDS + (
            _g("match", rf"\b(?:{_SUPPLEMENTS})\b"),
            _g("after", rf"^\s*(?:[\w']+\s+){{0,2}}?(?:{_SUPPLEMENTS})\b"),
        ),
        negatable=False,
        third_party=False,
    ),
    # Riding into traffic or crashing on purpose: crisis words wherever they
    # sit, so the effort guards never touch them ("I want to ride into a
    # lorry on the club run" is not an idiom).
    _Rule(
        "crisis", "crisis",
        _rx(
            # Not "ride into the car park", "ride into the bus lane" or
            # "under the bridge".
            r"\b(?:thinking about|thought about|thinking of|thought of|think about|want to|"
            r"wanted to|wanna|tempted to|feel like|felt like|could just|might just|urge to|"
            r"wish i could|considered|considering|going to|gonna|planning to|plan to) (?:just )?"
            r"(?:crash(?:ing)?|com(?:e|ing) off|swerv(?:e|ing)|steer(?:ing)?|veer(?:ing)?|"
            r"throw(?:ing)? myself|rid(?:e|ing)|cycl(?:e|ing)|driv(?:e|ing)|step(?:ping)?|"
            r"jump(?:ing)?|walk(?:ing)?) (?:\w+ ){0,3}?(?:on purpose|deliberately|intentionally|"
            r"(?:into|in front of|under) (?:the path of )?(?:a |an |the |some |oncoming )?"
            r"(?:lorry|lorries|truck|trucks|bus|buses|car|cars|train|trains|van|vans|"
            r"oncoming traffic)(?![-\w]| (?:park|parks|wash|share|club|free|stop|station|lane|"
            r"lanes|lights?|jam|queue|seat|door|boot|rack|bay|depot)\b)"
            r"|off (?:a |the )?(?:bridge|cliff))\b",
            # The urge itself, with no vehicle named: "I want to ride into traffic".
            r"\b(?:want to|wanted to|wanna|tempted to|feel like|felt like|urge to|"
            r"thinking about|thought about) (?:just )?(?:rid(?:e|ing)|cycl(?:e|ing)|steer(?:ing)?|"
            r"swerv(?:e|ing)|veer(?:ing)?|step(?:ping)?|walk(?:ing)?) (?:out )?into (?:the )?"
            r"traffic\b(?![-\w]| (?:lights?|jam|queue|free|calming)\b)",
            r"\bi (?:\w+ )?(?:crashed|came off|rode into (?:a|the) (?:lorry|truck|bus|car|van|"
            r"traffic)) (?:\w+ ){0,2}?(?:on purpose|deliberately|intentionally)\b"
            r"(?!\s*,?\s*(?:to avoid|to miss|so (?:that )?i (?:didn't|wouldn't|did not|would not)|"
            r"rather than|instead of)\b)",
        ),
        negatable=False,
        third_party=False,
    ),
    # A plan, an arrangement or goodbyes: letters to the family, giving
    # things away, knowing how they would do it, affairs in order (checked
    # below: _crisis_plan, since "I have a plan for Sunday" is not one).
    _Rule(
        "crisis", "crisis",
        _rx(_PLAN_STRONG, _PLAN_MID, _PLAN_WEAK),
        guards=(
            # Kit given away when upgrading or clearing out.
            _g("after", r"^\s*(?:\w+\s+){0,4}?(?:old|spare|unused|outgrown|second-?hand)\b"),
            _g("match", r"\b(?:old|spare|unused|outgrown)\b"),
            _g("after", r"\b(?:declutter\w*|clear(?:ing)? out|sell\w*|ebay|upgrad\w*|charity "
                        r"shop|moving (?:house|out)|to the club|juniors|new one|new bike|"
                        r"race|ride|sunday|saturday|tomorrow|weekend|trip|holiday|camp|"
                        r"sportive|the (?:dog|cat)|lift)\b"),
        ),
        negatable=False,
    ),
    # Not waking up, never having been born, everyone better off. In a
    # sentence that names training they are a check-in, like the rule below.
    _Rule(
        "crisis", "crisis",
        _rx(*_NOT_WAKE),
        guards=_CRISIS_GUARDS,
        negatable=False,
        third_party=False,
        soft_kind="distress_check",
    ),
    # Words of crisis that riders also use about a hard block, a taper or a
    # slice of cake. On their own they bring the crisis card. When every one
    # sits in a sentence that names training or food ("I want it all to end
    # so I can taper", "what's the point of anything under 200W"), the hit is
    # a distress_check instead: no card, no hold, no email to Gareth, and the
    # coach checks in kindly and asks directly whether they're okay.
    _Rule(
        "crisis", "crisis",
        _rx(
            r"\b(?:don't|dont|do not) want to (?:go on|carry on)(?: any ?more)?\b",
            r"\bno (?:point|reason) (?:in )?(?:anything|going on|carrying on|trying any ?more)\b",
            r"\bwhat'?s the point (?:of|in) (?:anything|going on)\b",
            r"\b(?:don't|dont|do not|can't|cant|cannot) see (?:the|any) point (?:in|of) "
            r"(?:anything|going on|carrying on|it all|any of it)\b",
            r"\b(?:don't|dont|do not|can't|cant|cannot) see (?:the|any) point any ?more\b",
            # "I don't see the point of training or anything", "of riding
            # anymore, or of anything". Not "or anything like that".
            r"\b(?:don't|dont|do not|can't|cant|cannot) see (?:the|any) point (?:in|of) "
            r"(?:training|riding|cycling|racing|the bike|getting up|going out|trying|bothering|"
            r"doing anything|work|eating)(?: any ?more)?,? (?:or|and) (?:of |in )?(?:anything|"
            r"anything else|everything|life|living|going on)\b(?! (?:like that|like this|else "
            r"like|similar))",
            # "I don't see the point." with nothing after it.
            r"\b(?:don't|dont|do not|can't|cant|cannot) see (?:the|any) point(?= *(?:$|[.!?,;\n]))",
            # "I can't go on", but not "I can't go on the club run".
            r"\b(?:can't|cant|cannot|can not) go on(?= *(?:$|[.!?,;\n]|like this|living|"
            r"with (?:life|this|it all|everything)|any ?more *(?:$|[.!?,;\n])|"
            r"the way (?:things are|it is|they are|i am|i'm going|i feel)|"
            r"feeling (?:like this|this way)|this way))",
            # "I just want it all to end", "I want everything to stop". Not
            # "I want it all to end with a sprint finish" (the effort guard).
            r"\b(?:want|wanted|wanna|need|wish|just want) (?:it all|everything|all of (?:it|this)|"
            r"things) to (?:end|stop|be over)\b",
            r"\bi (?:really |just |honestly |actually |genuinely |fucking |so )*(?:hate|loathe|"
            r"despise) myself\b",
            # "Life isn't worth living", "life's not worth living anymore". Not
            # "life is worth living", and a training or food sentence ("life
            # without cake isn't worth living") is a distress check.
            # What life is without ("without cake") is read as the sentence's
            # context (_training_talk), not as distress.
            r"(?:\b(?:my |this |a |the |your )?life\b(?P<ctx>[\s\w']{0,40}?))?"
            r"(?:\bisn't|\bis not|\bisnt|'s not|\bain't|\bnot|\bno longer|\bhardly|\bbarely)\s+"
            r"(?:really\s+|even\s+|just\s+|actually\s+)?worth (?:living|being alive|being here)\b",
            # "I want to die every time I see the hill reps, lol": a reaction to
            # the plan, so a check-in rather than the crisis card, unless
            # anything else in the message sounds like more.
            rf"\b(?:(?:want|wanna|wanted|going|gonna) (?:to )?(?:die|cry)|could (?:just )?die|"
            rf"kill(?:ing)? myself) {_EVERY_TIME_TRAINING}",
        ),
        guards=_CRISIS_GUARDS,
        negatable=False,
        third_party=False,
        soft_kind="distress_check",
    ),
    # 30C or above (86F), or a heat alert: SAFETY LAW 2j and the heat card.
    _Rule(
        "heat", "info",
        _rx(
            # A temperature (checked below: not one long past, "It was 30
            # degrees in Mallorca").
            r"\b(?P<temp>[34]\d(?:\.\d)? ?(?:°|º|degrees?|deg)? ?(?:c|celsius|centigrade))\b",
            r"\b(?P<temp>[34]\d(?:\.\d)? ?(?:degrees|°|º))(?! ?f)",
            r"\b(?P<temp>(?:8[6-9]|9\d|1[01]\d)(?:\.\d)? ?(?:°|º|degrees?)? ?(?:f|fahrenheit))\b",
            # Heat illness, said outright.
            r"\bheat ?stroke\b",
            r"\bsun ?stroke\b",
            r"\bheat exhaustion\b",
            r"\b(?:stopped|stop|stops|wasn't|weren't|was not|couldn't|could not|no longer) "
            r"sweating\b(?! (?:so|as) much\b| buckets\b| like\b)",
            r"\bheat ?wave\b",
            r"\bheat[- ]health alert\b",
            r"\b(?:amber|red|yellow) (?:heat|extreme heat|heat-health) (?:alert|warning)\b",
            r"\bextreme heat\b",
        ),
        guards=(
            # A body temperature is a fever, not the weather.
            _g("before", r"\b(?:my|a|body|running a|got a|have a|had a|with a) "
                         r"(?:temperature|temp)\b(?:\s+\w+){0,3}\s*$"),
            _g("before", r"\bfever\b"),
            _g("before", r"(?:-|minus\s*)$"),
            # Angles, not air: knee bend, lean, saddle tilt.
            _g("before", r"\b(?:lean|leaning|angle|knee|bend|turn|turned|rotate|rotated|"
                         r"tilt|tilted|hip|saddle|bars?|stem)\b(?:\s+\w+){0,3}\s*$"),
            _g("after", r"^\s*(?:of )?(?:lean|angle|knee|bend|tilt|rotation|incline|"
                        r"gradient|slope|turn)\b"),
            _g("before", r"\b(?:pool|water|sea|lake|bath|shower|sauna|oven|hot tub|wash|"
                         r"washing)\b"),
        ),
    ),
    # Heat illness after a hot ride or event: vomiting, confusion or a
    # pounding headache ("after the hot sportive I was vomiting").
    _Rule(
        "heat", "info",
        _rx(*_HEAT_ILLNESS_PATTERNS),
        guards=(
            _g("before", r"\b(?:hot (?:chocolate|drink|bath|shower|tub|water)|sauna)\b"
                         r"(?:\s+\w+){0,6}\s*$"),
        ),
    ),
    # A break of four weeks or more the rider tells us about: SAFETY LAW 2k.
    # The length is checked below.
    _Rule(
        "layoff", "info",
        _rx(
            rf"\b(?:haven't|have not|havent|hadn't|had not|not) (?:ridden|been riding|"
            rf"been on (?:the|my|a) bike|cycled|been cycling|ridden a bike|touched "
            rf"(?:the|my) bike|been out on the bike)(?: (?:at all|properly|much))? "
            rf"(?:for|in) (?:about |over |nearly |almost |around |roughly |the (?:last|past) )?"
            rf"(?P<n>{_N}) (?P<unit>weeks?|months?|years?)\b",
            rf"\b(?:off|away from) (?:the|my) bike (?:for )?(?:about |over |nearly |almost |"
            rf"around |roughly |the (?:last|past) )?(?P<n>{_N}) (?P<unit>weeks?|months?|years?)\b",
            rf"\b(?P<n>{_N}) (?P<unit>weeks?|months?|years?) off (?:the bike|riding|cycling|"
            rf"the saddle)\b",
            rf"\bfirst (?:ride|time on the bike|time riding|proper ride|ride back) "
            rf"(?:in|for|after) (?:about |over |nearly |almost |around |roughly )?"
            rf"(?P<n>{_N}) (?P<unit>weeks?|months?|years?)\b",
            rf"\b(?:coming|getting|got|came) back (?:to riding |to cycling |on the bike |"
            rf"into riding )?after (?:about |over |nearly |almost |around |roughly )?"
            rf"(?P<n>{_N}) (?P<unit>weeks?|months?|years?)(?: off)?\b",
        ),
        negatable=False,  # the negation is the point of the first pattern
    ),
    # An event or ride far beyond anything recent, or a rider who wants to be
    # told it will be fine: SAFETY LAW 2l. A distance counts only when the
    # ride is close (checked below).
    _Rule(
        "big_jump", "info",
        _rx(
            rf"\b(?:only|just) (?:ever )?(?:done|ridden|been on|had|completed) "
            rf"(?:{_N}) (?:proper |bike |real |long )?(?:rides?|times)\b",
            r"\b(?:never|haven't ever|have never|never ever) (?:ridden|done|cycled|been) "
            r"(?:more than|further than|over|beyond|anything (?:like|near|close to)|that far)\b",
            r"\b(?P<km>[1-9]\d\d) ?(?:km|kms|k|kilometres?|kilometers?)\b",
            r"\b(?P<mi>[6-9]\d|[1-9]\d\d) ?(?:miles?|mi)\b",
            r"\btell me (?:that )?(?:i'm|i am|i'll be|it's|it is|it'll be|i will be) "
            r"(?:fine|ok|okay|safe|alright|all right|good to go)\b",
        ),
        guards=(
            # "Only done two rides this week" is a quiet week, not a novice.
            _g("after", r"^\s*(?:this|last|in the last|so far this|since|over the)\b"),
        ),
    ),
    # Who is responsible if they're hurt: SAFETY LAW rule 6.
    _Rule(
        "responsibility", "info",
        _rx(
            r"\b(?:is|are|would|will|could|can) (?:forma|you|the app|ride ?with ?forma|"
            r"gareth)(?: be)? (?:\w+ ){0,2}(?:responsible|liable|to blame|at fault|"
            r"accountable)\b",
            r"\b(?:responsible|liable|liability|accountable)\b[^.?!\n]{0,40}\bif i "
            r"(?:get|got|am|'m|was|were) (?:hurt|injured)\b",
            r"\bif i (?:get|got|am|was) (?:hurt|injured)\b[^.?!\n]{0,40}\b(?:responsible|"
            r"liable|liability|sue|fault|compensat)",
            r"\b(?:can|could|would|should) i sue\b",
            r"\bsue (?:you|forma|gareth)\b",
            r"\bmy (?:legal )?rights\b",
            r"\bwhose fault\b",
            r"\b(?:claim|get) compensation\b",
        ),
        negatable=False,
        third_party=False,
    ),
)

# A negation counts only when it sits directly before the symptom: "no chest
# pain", "not chest pain", "without any chest pain", "didn't hit my head",
# "did not have chest pain" ("not" contracted counts as "not"). "Never" is
# not a negation here: "I've never had chest pain like this before" is chest
# pain now.
_NEGATION = re.compile(
    r"(?:\b(?:no|not|without)|n't)\s+"
    r"(?:(?:have|has|had|get|got|feel|felt|notice|noticed|been)\s+)?"
    r"(?:(?:any|a|an|real|actual|proper)\s+)?$"
)
# ...and stops counting when the clause goes on to say it's happening now:
# "no chest pain until the last climb", "didn't have pain like this before".
# Or that it did happen, just not now: "I don't have chest pain now but I did
# on the climb", "no chest pain today, but yesterday I had it". "No chest
# pain, but I did feel tired" stays a denial: the "did" has to stand alone or
# lead into when or where it happened.
_NEGATION_UNDONE = re.compile(
    r"\b(?:like (?:this|that|it)|before|until|till|til|up to now|until now|this time|"
    r"any ?more|again)\b"
    r"|\bbut (?:i|it|there|that)(?: (?:definitely|really|actually|certainly|still))? "
    r"(?:did|do|does|had|has|have)(?: (?:it|them|some|one|that))?"
    r"(?=\s*(?:$|[,.!?;]|on\b|in\b|at\b|during\b|when\b|while\b|after\b|before\b|"
    r"earlier\b|yesterday\b|last\b|this\b|then\b|again\b|too\b|today\b|tonight\b|"
    r"since\b|a bit\b|a little\b))"
    r"|\bbut (?:yesterday|earlier|last (?:night|week|time|ride|weekend)|this morning|"
    r"on (?:the|my|that) (?:climb|ride|hill|interval|effort|turbo|way)|during|at the time)\b"
)
# "I don't have chest pain now": said that way, it was there before.
_NEGATION_UNDONE_NEXT = re.compile(
    r"^\s*(?:now|right now|at the moment|at the minute)\b(?! that\b)"
)
_NOT_NEGATION = re.compile(r"\bnot (?:sure|certain|just|only)\b")
_RELATIONS = (
    r"wife|husband|partner|girlfriend|boyfriend|dad|father|mum|mom|mother|son|"
    r"daughter|brother|sister|friend|mate|mates|teammate|team mate|coach|kid|kids|"
    r"child|uncle|aunt|grandad|granddad|grandma|grandfather|grandmother|colleague|"
    r"boss|neighbour|neighbor|dog"
)
# Someone else's symptom, close before the hit: "my dad has", "she's got",
# "her". Only the last few words count. A bare possessive is not someone
# else: "this morning's session" and "Saturday's race" are the rider's own.
_THIRD_PARTY = re.compile(
    rf"(?P<who>\bmy (?:{_RELATIONS})(?:'s)?"
    r"|\b(?:he|she|they)(?:'s|'d|'ve)?"
    r"|\b(?:his|her|their|him|them))"
    r"(?P<gap>[\s,]+(?:[\w']+[\s,]+){0,2})$"
)
# The rider speaking after the other person is named: "my wife says I
# fainted", "she said I passed out", "I told her my chest hurts".
_FIRST_PERSON = re.compile(r"\b(?:i|i'm|im|i've|i'd|i'll|me|my|myself)\b")
_RIDERS_OWN = re.compile(r"\bmy\s+$")


def _someone_else(before: str) -> bool:
    """Whether the words just before a hit put it on someone other than the
    rider."""
    m = _THIRD_PARTY.search(before)
    if m is None or _RIDERS_OWN.search(before):
        return False
    return not _FIRST_PERSON.search(m.group("gap"))


def _negated(before: str, after: str) -> bool:
    """Whether a hit is plainly denied: a negation directly before it, and
    nothing after it saying it has happened now."""
    if not _NEGATION.search(before) or _NOT_NEGATION.search(before):
        return False
    return not (_NEGATION_UNDONE.search(after) or _NEGATION_UNDONE_NEXT.search(after))


_CLAUSE_ENDS = ".!?;\n"
_WINDOW = 30
_CALORIE_EATING = re.compile(
    r"\b(?:eat|eating|ate|intake|diet|dieting|limit|limiting|restrict|restricting|"
    r"cutting|cut to|cut down|stick to|sticking to|only|under|below|less than|"
    r"deficit|a day|per day|daily|/day|maximum|max)\b"
)
_CALORIE_SPENDING = re.compile(
    r"\b(?:burn|burned|burnt|burning|ride|rode|riding|used|expended|kj|output|"
    r"session|workout|spent|on the bike|in the bag|per hour|an hour|/hr|/h|gel|gels|"
    r"bar|bars|drink|bottle)\b"
)
_CALORIE_LIMIT = 1800


@dataclass(frozen=True)
class Hit:
    kind: str
    matched: str
    severity: str
    # The age a minor hit implies ("I'm 15" is 15, "turning 17 next month" is
    # 16, "Year 13" is 17), for keeping the records to their 21st birthday.
    # None on every other kind.
    stated_age: int | None = None
    # A head-injury hit that describes a fresh crash or blow ("crashed again
    # today and hit my head on the kerb", "came off on the ice this
    # morning"), not the earlier injury mentioned again ("how long after
    # hitting my head can I race?"). The holds code (safety_service.open_hold)
    # restarts the head-injury clock for a new one. False on every other kind.
    new_event: bool = False
    # What a safety_question is about: the red flags it asks after,
    # comma-separated in the order they matched ("chest_pain,heat"). None on
    # every other kind.
    topic: str | None = None

    def as_dict(self) -> dict:
        out = {"kind": self.kind, "matched": self.matched, "severity": self.severity}
        if self.kind == "minor":
            out["stated_age"] = self.stated_age
        if self.kind == "head_injury":
            out["new_event"] = self.new_event
        if self.kind == "safety_question":
            out["topic"] = self.topic
        return out


def _normalise(text: str) -> str:
    text = (text or "").lower()
    for curly, plain in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"')):
        text = text.replace(curly, plain)
    return re.sub(r"[ \t]+", " ", text)


def _clause(text: str, start: int, end: int) -> tuple[int, int]:
    lo = max(text.rfind(c, 0, start) for c in _CLAUSE_ENDS) + 1
    ends = [i for i in (text.find(c, end) for c in _CLAUSE_ENDS) if i != -1]
    return lo, (min(ends) if ends else len(text))


def _group(match: re.Match, name: str) -> str | None:
    try:
        return match.group(name)
    except IndexError:
        return None


def _count(word: str | None) -> float:
    if not word:
        return 1.0
    word = word.strip()
    return float(word) if word[0].isdigit() else float(_WORD_NUMBERS.get(word, 1))


def _calorie_restriction(match: re.Match, before: str, after: str) -> bool:
    """A calorie number is a red flag only when it is a daily eating figure
    under 1,800, not what a ride burned or a gel carries."""
    value = int(match.group("kcal").replace(",", ""))
    if value >= _CALORIE_LIMIT:
        return False
    context = f"{before} {after}"
    if _CALORIE_SPENDING.search(context):
        return False
    return bool(_CALORIE_EATING.search(context))


# Faster than this is a red flag: about 1% of body weight a week for a
# typical rider. The rider's own weight is never needed to decide.
_LOSS_KG_PER_WEEK = 0.75
_TO_KG = {"kg": 1.0, "kgs": 1.0, "kilo": 1.0, "kilos": 1.0, "lb": 0.4536,
          "lbs": 0.4536, "pound": 0.4536, "pounds": 0.4536, "stone": 6.35}
_TO_WEEKS = {"day": 1 / 7, "days": 1 / 7, "week": 1.0, "weeks": 1.0,
             "fortnight": 2.0, "month": 4.35, "months": 4.35}


def _fast_weight_loss(match: re.Match) -> bool:
    kg = float(match.group("amount")) * _TO_KG.get(match.group("unit"), 1.0)
    weeks = _count(match.group("count")) * _TO_WEEKS.get(match.group("period"), 1.0)
    return weeks > 0 and kg / weeks > _LOSS_KG_PER_WEEK


def _layoff_days(match: re.Match) -> float:
    unit = match.group("unit").rstrip("s")
    per = {"week": 7, "month": 30, "year": 365}.get(unit, 7)
    return _count(match.group("n")) * per


def layoff_days_from(text: str) -> float | None:
    """How long a break the rider described lasted, in days, or None if the
    text doesn't say."""
    norm = _normalise(text)
    for rule in RULES:
        if rule.kind != "layoff":
            continue
        for pattern in rule.patterns:
            m = pattern.search(norm)
            if m:
                return _layoff_days(m)
    return None


# The burn of a hard effort, not an injury: "searing pain in my quads on the
# last rep". It stays an injury with any hallmark of one: a joint or tendon,
# swelling, a pop or tear, a limp, or pain that lasts or gets worse.
_EFFORT_BURN_RX = re.compile(r"^(?:searing|burning)\b")
_MUSCLE_RX = re.compile(
    r"\b(?:quads?|legs?|thighs?|calves|calf|hamstrings?|glutes?|lungs?|muscles?)\b"
)
_EFFORT_CONTEXT_RX = re.compile(
    r"\b(?:rep|reps|interval|intervals|effort|efforts|sprint|sprints|climb|climbs|climbing|"
    r"hill|set|sets|last|final|ramp|test|kom|qom|segment|turbo|vo2|vo2max|threshold|"
    r"over-?unders?|race|attack|finish|max|all-out)\b"
)
_INJURY_HALLMARK_RX = re.compile(
    r"\b(?:swollen|swelling|swell|swells|pop|popped|popping|snap|snapped|tear|tore|torn|"
    r"limp|limping|bruis\w*|can't (?:put|bear)|still|since|days?|weeks?|getting worse|"
    r"worse|won't go|hasn't gone|not going away|joint|knee|knees|ankle|achilles|tendon|"
    r"hip|back|shoulder|wrist|neck|shin|foot|feet|heel|numb\w*|tingl\w*|sharp|stabbing|"
    r"shooting|night)\b"
)


def _effort_burn(match: re.Match, before: str, after: str) -> bool:
    if not _EFFORT_BURN_RX.search(match.group(0)):
        return False
    context = f"{before} {after}"
    return bool(
        _MUSCLE_RX.search(after)
        and _EFFORT_CONTEXT_RX.search(context)
        and not _INJURY_HALLMARK_RX.search(context)
    )


def _age_number(word: str) -> int:
    return int(word) if word.isdigit() else _AGE_WORDS[word]


_GRADES = {"9": 9, "10": 10, "11": 11, "12": 12, "ninth": 9, "tenth": 10, "eleventh": 11,
           "twelfth": 12}


def _minor_age(match: re.Match) -> int | None:
    """The age a minor hit implies: the age said, a year under a birthday to
    come, the youngest age in a school year, 14 for GCSEs (the youngest in
    the two GCSE years) and 16 for sixth form."""
    for name, shift in (("age", 0), ("bare", 0), ("turning", -1)):
        word = _group(match, name)
        if word:
            return _age_number(word) + shift
    year = _group(match, "year")
    if year:
        return int(year) + 4
    grade = _group(match, "grade")
    if grade:
        # 9th grade starts at 14 in the US.
        return _GRADES.get(grade, 9) + 5
    hs = _group(match, "hs")
    if hs:
        return {"freshman": 14, "sophomore": 15, "junior": 16}.get(hs, 14)
    words = match.group(0)
    if "gcse" in words:
        return 14
    if "sixth" in words or "6th" in words:
        return 16
    return None


def _corrected_age(tail: str) -> int | None:
    """The age a rider corrects to straight after giving one ("I'm 17, no
    wait 47"), or None if they don't."""
    m = _AGE_CORRECTION.match(tail)
    return int(m.group("fixed")) if m else None


def _past_age(match: re.Match, after: str) -> bool:
    """A bare age that dates a past event ("aged 16 I broke my collarbone"),
    rather than the rider's age now."""
    return bool(
        _group(match, "bare")
        and _PAST_EVENT.search(after)
        and not _RECENT.search(after)
    )


def _segment(text: str, start: int, end: int) -> str:
    """The part of a sentence a phrase sits in, between commas or a "but":
    "I broke my collarbone 10 years ago" in "I broke my collarbone 10 years
    ago, but last week I sprained my ankle"."""
    lo, hi = _clause(text, start, end)
    cuts = list(_SEGMENT_BREAK_RX.finditer(text, lo, start))
    seg_lo = cuts[-1].end() if cuts else lo
    cut = _SEGMENT_BREAK_RX.search(text, end, hi)
    return text[seg_lo:cut.start() if cut else hi]


def _long_ago(segment: str) -> bool:
    """Whether these words date something a year or more back ("10 years
    ago", "as a kid", "in 2015"), with nothing saying it's still going on or
    that something else is recent."""
    if _STILL_NOW_RX.search(segment):
        return False
    for m in _LONG_AGO_RX.finditer(segment):
        year = _group(m, "year")
        if year is None or int(year) < datetime.utcnow().year:
            return True
    return False


def _old_and_healed(text: str, match: re.Match) -> bool:
    """An injury long past ("I broke my collarbone 10 years ago") or said to
    have healed ("it healed fine years ago"), and not bothering them now."""
    if _long_ago(_segment(text, match.start(), match.end())):
        return True
    lo, hi = _clause(text, match.start(), match.end())
    clause = text[lo:hi]
    return bool(_HEALED_RX.search(clause)) and not _STILL_NOW_RX.search(clause)


def _incident(text: str, skip: tuple[int, int] = (-1, -1)) -> re.Match | None:
    """Something that happened to the rider that could hurt their head: a
    crash, a fall, an off, or a blow to the head they say they did or didn't
    take ("I didn't hit my head" still says something happened). Not a near
    miss, someone else's crash, an app or a sugar crash, a chain that came
    off, the back of the group, or a crash years ago. None if there is none.
    `skip` is a span already used by the hit itself."""
    for m in _incidents(text):
        if not (m.start() < skip[1] and m.end() > skip[0]):
            return m
    return None


@functools.lru_cache(maxsize=128)
def _incidents(text: str, helmets: bool = True) -> tuple[re.Match, ...]:
    """Every incident in a message (_incident), worked out once per message:
    each sign of a head injury and each lost second asks again. Without
    `helmets`, only what happened to the rider: a crash, a fall, a blow."""
    found: list[re.Match] = []
    searches = (
        (_INCIDENT_RX, "crash"),
        (_HEAD_HIT_RX, "head"),
        (_KNOCKED_OUT_RX, "out"),
        (_HELMET_DAMAGE_RX, "helmet"),
        (_HELMET_HIT_RX, "head"),
    )
    for rx, what in searches:
        if what == "helmet" and not helmets:
            continue
        for m in rx.finditer(text):
            lo, hi = _clause(text, m.start(), m.end())
            before = text[max(lo, m.start() - _WINDOW):m.start()]
            after = text[m.end():min(hi, m.end() + _WINDOW)]
            if _someone_else(before):
                continue
            if what == "crash" and (
                _INCIDENT_NOT_RIDER.search(before)
                or _NEAR_MISS.search(before)
                or _INCIDENT_AFTER_NOT.search(after)
            ):
                continue
            if what == "out" and _NOT_A_BLOW.search(before):
                continue
            if what == "helmet" and _helmet_handled(text, m.start(), m.end()):
                continue
            if _long_ago(_segment(text, m.start(), m.end())):
                continue
            found.append(m)
    return tuple(found)


def _helmet_handled(text: str, start: int, end: int) -> bool:
    """A damaged helmet that says nothing about the rider's head: a strap,
    buckle or visor that broke, or a lid dropped, sat on or knocked off a
    shelf ("Helmet visor cracked when I dropped it in the car park"), with no
    crash, fall or blow of the rider's own anywhere in the message."""
    lo, hi = _clause(text, start, end)
    words = text[start:end]
    before = text[max(lo, start - _WINDOW):start]
    after = text[end:hi]
    part = (
        _HELMET_PART_RX.search(words)
        or _HELMET_PART_BEFORE_RX.search(before)
        or _HELMET_PART_AFTER_RX.search(after)
    )
    if not (part or _HELMET_HANDLED_RX.search(text[lo:hi])):
        return False
    return not _incidents(text, False)


_HEAD_SYMPTOM_RX = re.compile(_HEAD_SYMPTOM)


def _household_knock(match: re.Match, text: str, hi: int) -> re.Match | None:
    """A knock to the head about the house that the rider shrugs off ("Hit
    my head on the cupboard door, ouch, anyway"), with no crash, no sign of a
    head injury and nothing about being knocked out, a memory gap or a
    helmet anywhere in the message. The place it happened, or None."""
    place = _HOUSEHOLD_RX.match(text[match.end():hi])
    if place is None or not _SHRUGGED_OFF_RX.search(text):
        return None
    if _HEAD_SIGN_ANY_RX.search(text) or _HEAD_SYMPTOM_RX.search(text):
        return None
    for m in _incidents(text, False):
        if not (m.start() < match.end() and m.end() > match.start()):
            return None
    return place


def _new_head_event(text: str) -> bool:
    """Whether a head injury is a fresh crash or blow rather than an earlier
    one mentioned again: "crashed again today", "another crash", a crash or
    blow dated today, this morning, yesterday or last night ("came off on the
    ice this morning"), or one told with where it happened ("hit my head on
    the kerb") with nothing tying it back to an earlier one ("how long after
    hitting my head", "a headache since the crash", "last week")."""
    for m in _incidents(text):
        lo, hi = _clause(text, m.start(), m.end())
        before = text[max(lo, m.start() - _WINDOW):m.start()]
        after = text[m.end():min(hi, m.end() + 40)]
        if _AGAIN_AFTER_RX.search(after) or _AGAIN_BEFORE_RX.search(before):
            return True
        # "had a second off", "another spill": the crash's own words say so.
        if _AGAIN_IN_RX.search(m.group(0)):
            return True
        if _STALE_TIME_RX.search(_segment(text, m.start(), m.end())):
            continue
        back = bool(
            _BACK_REFERENCE_RX.search(before)
            or re.match(r"(?:since|after|from|following|before)\b", m.group(0))
        )
        # The words that date this crash, not the symptom beside it: up to
        # the next break after it, and (when nothing ties it back) from the
        # last break before it.
        own_after = re.split(r",|;|\b(?:and|but|then|now|so|since|after)\b", after, maxsplit=1)[0]
        own_before = re.split(r",|;|\b(?:and|but|then|so)\b", before)[-1]
        if _FRESH_TIME_RX.search(own_after):
            return True
        if not back and _FRESH_TIME_RX.search(own_before):
            return True
        if not back and _PLACE_AFTER_RX.search(after):
            return True
    return False


# Heartburn, indigestion or cold air: a burning chest with an everyday cause.
_CHEST_BURN_EXPLAINED_RX = re.compile(
    r"\b(?:heartburn|indigestion|reflux|acid|cold air|freezing air|icy air|the cold|curry|"
    r"spicy|chilli|after (?:eating|food|dinner|lunch|breakfast|a meal|the meal))\b"
)
_NOT_HEARTBURN_RX = re.compile(
    r"\b(?:not|isn't|wasn't|doesn't feel|didn't feel|nothing|never|unlike) (?:like |as |"
    r"the same as )?(?:\w+ )?(?:heartburn|indigestion|reflux)\b"
)


def _chest_burn(text: str, lo: int, hi: int) -> bool:
    """A burning in the middle of the chest counts unless the sentence gives
    it an everyday cause (heartburn, a curry, cold air), and doesn't take
    that cause back ("not like heartburn")."""
    clause = text[lo:hi]
    return not _CHEST_BURN_EXPLAINED_RX.search(clause) or bool(_NOT_HEARTBURN_RX.search(clause))


_NERVES_RX = re.compile(
    r"\b(?:excite\w*|nerves|nervous|anticipation|adrenaline|thinking about (?:the |my |"
    r"tomorrow's |sunday's |saturday's )?(?:race|event|start|sportive|crit|tt))\b"
)


def _hr_jump(match: re.Match, text: str, lo: int, hi: int) -> bool:
    """A heart rate of 200 or more at rest or soft pedalling: a racing heart,
    not a training number."""
    return int(match.group("bpm")) >= _HR_JUMP_BPM and bool(_HR_JUMP_REST_RX.search(text[lo:hi]))


# Said of a temperature that has been and gone: "It was 30 degrees in
# Mallorca", "Holiday in Spain was 35 degrees". Not when the message says
# it's hot now or will be ("it's 31 degrees", "forecast 38", "tomorrow").
_PAST_HEAT_RX = re.compile(
    r"\b(?:was|were|had been|got (?:up )?to|reached|peaked|topped out|yesterday|last (?:week|"
    r"weekend|year|summer|month|time|trip|holiday|saturday|sunday)|on holiday|holiday|back from|"
    r"ago)\b"
)
_HEAT_NOW_RX = re.compile(
    r"\b(?:is|are|it's|its|will|going to|gonna|forecast\w*|tomorrow|tonight|today|this (?:week|"
    r"weekend|afternoon|morning|evening|summer)|next|now|currently|at the moment|planning|"
    r"plan to|heading|off to|upcoming|expected|due|should i|can i|still)\b|'ll\b"
)


# How the heat left them: a past temperature beside any of these still
# counts ("I fainted and had chest pain. It was 35 degrees out").
_HEAT_TOLL_RX = re.compile(
    r"\b(?:faint\w*|dizz\w*|light-?headed|woozy|collaps\w*|passed out|blacked out|chest|heart|"
    r"palpitations?|sick|vomit\w*|threw up|nause\w*|headaches?|cramp\w*|confus\w*|shiver\w*|"
    r"chills|sweat\w*|heat ?stroke|exhaust\w*|unwell|ill|rough|awful|grim|struggl\w*|wilted|"
    r"cooked|overheat\w*|dehydrat\w*|sunburn\w*|sunstroke|burnt|fried)\b"
)


def _past_heat(match: re.Match, text: str) -> bool:
    """A temperature that has been and gone, with nothing in the message
    about heat now or to come, or about what it did to them."""
    segment = _segment(text, match.start(), match.end())
    return bool(
        _PAST_HEAT_RX.search(segment)
        and not _HEAT_NOW_RX.search(text)
        and not _HEAT_TOLL_RX.search(text)
    )


def _heart_ache(match: re.Match, text: str, lo: int, hi: int) -> bool:
    """An ache in the arm, jaw, neck or back that may be the heart: the jaw,
    the left arm or both arms with effort or breathlessness, or anywhere on
    the list when it spreads or comes with breathlessness. Not an ache with
    an everyday cause in the same sentence (clenching, gripping the bars, the
    gym, a crash)."""
    clause = text[lo:hi]
    if _ACHE_EXPLAINED_RX.search(clause):
        return False
    spreading = bool(_SPREADING_RX.search(clause))
    breathless = bool(_BREATHLESS_RX.search(text))
    if spreading or breathless:
        return True
    if not _STRONG_SITE_RX.search(match.group("site")):
        return False
    outside = text[:match.start()] + " " + text[match.end():]
    return bool(_HEART_EFFORT_RX.search(outside))


def heart_ache_hit(matched: str) -> bool:
    """Whether a chest_pain hit is an ache in the arm, jaw, neck or back
    rather than the chest itself, so the reply can say why it still counts."""
    words = _normalise(matched)
    return not re.search(r"\bchest\b|\bheart\b|\bbreath|\bangina\b", words) and bool(
        re.search(r"\b(?:arms?|jaw|neck|back|shoulders?)\b", words)
    )


def _lost_time_faint(match: re.Match, text: str) -> bool:
    """A few seconds lost that sound like a faint: faint words nearby ("I
    didn't black out but I lost a few seconds"), or an effort with nothing
    about race time. After a crash it is the head injury check's."""
    if _incident(text, (match.start(), match.end())) is not None:
        return False
    outside = text[:match.start()] + " " + text[match.end():]
    if _FAINT_WORDS_RX.search(outside):
        return True
    return bool(_HEART_EFFORT_RX.search(outside)) and not _RACE_TIME_RX.search(outside)


def _training_talk(match: re.Match, text: str, lo: int, hi: int) -> bool:
    """Whether words of crisis come with clear training context: their own
    sentence names training or food ("this block is brutal", "so I can
    taper", "under 200W", "that cake lol"), and nothing in the message
    sounds like more than a bad session."""
    sentence = text[lo:match.start()] + " " + text[match.end():hi]
    # What life is said to be worthless without: "life without cake", "life
    # without the bike".
    without = (
        f"{_group(match, 'ctx') or ''} {sentence}" if "ctx" in match.re.groupindex else ""
    )
    if not (
        _TRAINING_CONTEXT_RX.search(sentence)
        or _TRAINING_CONTEXT_RX.search(_group(match, "ctx") or "")
        or _LIFE_WITHOUT_RX.search(without)
    ):
        return False
    rest = text[:match.start()] + " " + text[match.end():]
    return not _DISTRESS_RX.search(rest)


def _loc_check(match: re.Match, text: str, lo: int, hi: int, *, after_crash: bool) -> bool:
    """Losing consciousness in any words: a faint with no crash in the
    message, a head injury after one. A few minutes out needs something more
    than the words (someone said so, the floor, collapsing), since "I was out
    for 20 minutes" is usually a ride; waking on a floor after a party is
    sleep."""
    words = match.group(0)
    if re.match(r"(?:came|come|coming|comes) ", words):
        lo_before = text[max(lo, match.start() - _WINDOW):match.start()]
        if _LOC_NOT_RIDER_RX.search(lo_before):
            return False  # "we came round the corner"
    span = (match.start(), match.end())
    incidents = [
        m for m in _incidents(text) if not (m.start() < span[1] and m.end() > span[0])
    ]
    crashed = bool(incidents)
    if after_crash and not crashed:
        return False
    if not after_crash:
        for m in incidents:
            # Out after a crash is the head injury rule's, whichever order
            # it is told in ("out for a minute after I hit the deck"); a fall
            # after blacking out ("blanked out and toppled sideways") is
            # still a faint.
            if m.start() < match.start():
                return False
            if _CRASH_FIRST_RX.search(text[match.end():m.start()]):
                return False
    unit = _group(match, "locunit") or ""
    if unit.startswith("min") or re.search(r"\ba (?:minute|bit|while)\b", words):
        outside = text[:match.start()] + " " + text[match.end():]
        if not (crashed or _LOC_MINUTES_BACKED_RX.search(outside)):
            return False
    if re.search(r"\bwoke|\bwake|\bwaking|\bfound myself|\bcame up\b", words):
        if _LOC_SLEPT_RX.search(text[lo:hi]):
            return False
    return True


def _body_temperature(match: re.Match, text: str, lo: int, hi: int) -> bool:
    """A temperature of 38.0C (100.4F) or more that is the rider's own: said
    with "my temp", a thermometer or a fever, or beside words of illness,
    and not the weather, a ride or a place."""
    raw = match.group("bt")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return False
    unit = (_group(match, "btu") or "").lower()
    fahrenheit = unit.startswith("f") or (not unit.startswith("c") and value >= 90)
    if fahrenheit:
        if not 100.4 <= value <= 108:
            return False
    elif not 38.0 <= value <= 43.5:
        return False
    clause = text[lo:hi]
    strong = re.search(
        r"\b(?:my (?:temp|temperature|body temp\w*)|fever\w*|running (?:a |at )?"
        r"(?:temp|temperature|\d))", clause
    )
    if _WEATHER_RX.search(clause) and not strong:
        return False
    if "." in raw and _group(match, "btw") and re.match(r"te?mp", match.group("btw")):
        # "temperature reading of 38.7": the weather is rarely given to a
        # tenth of a degree.
        return True
    return bool(strong or _BODY_TEMP_ANCHOR_RX.search(text))


def _illness_over(match: re.Match, text: str) -> bool:
    """An illness that has been and gone ("had a fever last month, all
    better now", "had covid in the summer, fully recovered"): dated two weeks
    or more back and said to be over, or years ago, with nothing in the
    sentence saying it is still there or back."""
    lo, hi = _clause(text, match.start(), match.end())
    clause = text[lo:hi]
    if _ILLNESS_NOW_RX.search(clause):
        return False
    segment = _segment(text, match.start(), match.end())
    if _long_ago(segment):
        return True
    return bool(_ILLNESS_PAST_RX.search(clause) and _ILLNESS_OVER_RX.search(clause))


def _feverish(match: re.Match, text: str, lo: int, hi: int) -> bool:
    """Hot and cold together, or burning up, said as an illness rather than
    the ride: not "sweating on the climb then freezing on the descent"."""
    if "burning up" == match.group("feverish"):
        return True
    return not _FEVERISH_EFFORT_RX.search(text[lo:hi])


def _crisis_plan(match: re.Match, text: str, lo: int, hi: int) -> bool:
    """A plan or an arrangement that means ending their life. Goodbye
    letters, letters to the family and affairs put in order count on their
    own. Knowing how they'd do it, giving things away or a note to the
    family count beside words about ending it, beside another plan, or with
    words of distress and no training in the sentence. "I have a plan",
    "made arrangements" or "picked a date" count only beside words about
    ending it or a stronger plan."""
    if _group(match, "plan"):
        return True
    outside = text[:match.start()] + " " + text[match.end():]
    if _CRISIS_ANCHOR_RX.search(outside):
        return True
    others = [
        m for m in _PLAN_ANY_RX.finditer(text)
        if not (m.start() < match.end() and m.end() > match.start())
    ]
    stronger = [m for m in others if _group(m, "plan") or _group(m, "planmid")]
    if _group(match, "planweak"):
        return bool(stronger)
    if others:
        return True
    sentence = text[lo:match.start()] + " " + text[match.end():hi]
    return bool(_DISTRESS_RX.search(outside)) and not _TRAINING_CONTEXT_RX.search(sentence)


_BODY_TEMP_RXS = tuple(re.compile(p) for p in _BODY_TEMP_PATTERNS)


def _body_temp_in(text: str, lo: int, hi: int) -> bool:
    """Whether this sentence gives the rider's own temperature of 38C or
    more (_body_temperature)."""
    for rx in _BODY_TEMP_RXS:
        for m in rx.finditer(text, lo, hi):
            if _body_temperature(m, text, lo, hi):
                return True
    return False


def _scant_for_weight(match: re.Match, text: str) -> bool:
    """Eating very little counts as restriction when it is for weight or
    riding light: "a bowl of soup a day so I'm light for the hill climb"."""
    return bool(_LIGHT_FOR_RIDING_RX.search(text))


def _passes_check(
    kind: str,
    match: re.Match,
    before: str,
    after: str,
    text: str = "",
    lo: int = 0,
    hi: int = 0,
) -> bool:
    """The checks a phrase alone can't make: a calorie figure that's about
    eating, a weight loss faster than about 1% a week, a break of four weeks
    or more, a long ride that's coming up soon, the burn of a hard effort, an
    injury long past, an ache that may be the heart, a crash behind the signs
    of a head injury, a few seconds lost after an effort."""
    text = text or match.string
    hi = hi or len(text)
    if kind == "injury":
        return not _effort_burn(match, before, after) and not _old_and_healed(text, match)
    if kind == "fever":
        # An illness long over is not a fever now.
        if _illness_over(match, text):
            return False
        if _group(match, "bt"):
            return _body_temperature(match, text, lo, hi)
        if _group(match, "feverish"):
            return _feverish(match, text, lo, hi)
        return True
    if kind in ("fainting", "head_injury") and _group(match, "loc"):
        return _loc_check(match, text, lo, hi, after_crash=kind == "head_injury")
    if kind == "head_injury" and (_group(match, "amn") or _group(match, "faceblow")):
        # A memory gap, or the face or helmet taking a blow, after a crash
        # or a fall.
        return _incident(text, (match.start(), match.end())) is not None
    if kind == "crisis" and (
        _group(match, "plan") or _group(match, "planmid") or _group(match, "planweak")
    ):
        return _crisis_plan(match, text, lo, hi)
    if kind == "restriction" and _group(match, "scant"):
        return _scant_for_weight(match, text)
    if kind == "heat" and _group(match, "hotill"):
        return not _long_ago(_segment(text, match.start(), match.end()))
    if kind == "heat" and _group(match, "temp") and _body_temp_in(text, lo, hi):
        # "my temp hit 39C overnight" is a fever, not the weather.
        return False
    if kind == "chest_pain" and _group(match, "site"):
        return _heart_ache(match, text, lo, hi)
    if kind == "chest_pain" and _group(match, "burn"):
        return _chest_burn(text, lo, hi)
    if kind == "palpitations" and _group(match, "bpm"):
        return _hr_jump(match, text, lo, hi)
    if kind == "palpitations" and _group(match, "racing"):
        # Nerves or excitement before a race, said in the same sentence.
        return not _NERVES_RX.search(text[lo:hi])
    if kind == "head_knock":
        return _household_knock(match, text, hi) is not None
    if kind == "head_injury" and _group(match, "blow"):
        return _household_knock(match, text, hi) is None
    if kind == "head_injury" and _group(match, "helmet"):
        return not _helmet_handled(text, match.start(), match.end())
    if kind == "head_injury" and _group(match, "sym"):
        return _incident(text, (match.start(), match.end())) is not None
    if kind == "fainting" and _group(match, "grey"):
        # After a crash, going grey and woozy is the head injury check's.
        return _incident(text) is None
    if kind == "fainting" and _group(match, "lost"):
        return _lost_time_faint(match, text)
    if kind == "fainting" and _group(match, "unsure"):
        return bool(_UNSURE_AFTER.search(text[match.end():hi]))
    if kind == "pregnancy" and _group(match, "we"):
        return bool(_CARRYING_RX.search(text))
    if kind == "heat" and _group(match, "temp"):
        return not _past_heat(match, text)
    if kind == "restriction":
        if _group(match, "kcal"):
            return _calorie_restriction(match, before, after)
        if _group(match, "amount"):
            return _fast_weight_loss(match)
    elif kind == "layoff":
        return _layoff_days(match) >= safety_service.LAYOFF_GAP_DAYS
    elif kind == "big_jump" and (_group(match, "km") or _group(match, "mi")):
        return bool(re.search(_SOON, f"{before} {after}"))
    return True


def detect_red_flags(text: str) -> list[dict]:
    """Every red flag in a rider's message, at most one per kind:
    [{"kind", "matched", "severity"}]. Pure: no database, no model."""
    return [h.as_dict() for h in _detect(text)]


def _detect(text: str) -> list[Hit]:
    norm = _normalise(text)
    if not norm.strip():
        return []
    hits: list[Hit] = []
    found: set[str] = set()
    # Gentler hits (a distress_check, a general question) found by a rule
    # whose kind no later rule finds for real: kept until the end, so a
    # later rule of the same kind can still find the real thing.
    softer: dict[str, Hit] = {}
    questions: dict[str, Hit] = {}
    for rule in RULES:
        if rule.kind in found:
            continue
        hit = _first_hit(rule, norm)
        if hit is None:
            continue
        if hit.kind == "safety_question":
            for topic in (hit.topic or rule.kind).split(","):
                questions.setdefault(topic, replace(hit, topic=topic))
            continue
        if hit.kind != rule.kind:
            softer.setdefault(rule.kind, hit)
            continue
        hits.append(hit)
        found.update((rule.kind, hit.kind))
    for kind, hit in softer.items():
        if kind not in found and hit.kind not in found:
            hits.append(hit)
            found.add(hit.kind)
    asked = {k: h for k, h in questions.items() if k not in found}
    if asked:
        # One general question, about every red flag it asked after.
        words = list(dict.fromkeys(h.matched for h in asked.values()))
        hits.append(Hit(
            "safety_question", "; ".join(words)[:200], "info", topic=",".join(asked),
        ))
    if "head_injury" in found and "head_knock" in found:
        # A knock about the house beside a real head injury is that injury.
        hits = [h for h in hits if h.kind != "head_knock"]
    return hits


# ── A general or hypothetical question, not a symptom now ───────────────────

# The frames of a general question: "what should I do if I ever get", "what
# are the signs of", "how do I know if I've got", "hypothetically".
_QUESTION_FRAME_RX = re.compile(
    r"what (?:happens|would happen|will happen|could happen|should (?:i|you|we|one|someone|"
    r"a rider) do|do (?:i|you|we) do|to do|would you do|would you (?:advise|recommend|"
    r"suggest)|is the (?:right|best|correct) thing to do|are you (?:meant|supposed) to do)\b"
    r"[^.!?\n]{0,30}?\b(?:if|when|after|in case)\b"
    r"|\bwhat(?:'s| is| are|'re)? (?:the )?(?:main |common |typical |usual |early |first |key |"
    r"warning |tell-?tale |classic |obvious )?(?:signs?|symptoms?|red flags?|warning signs?|"
    r"causes?|difference|protocol|rules?|guidelines?|first aid|treatment|risks?|dangers?)\b"
    r"|\bhow (?:do|would|can|could|should|will|does|to)(?: (?:i|you|we|one|someone|anyone|a "
    r"rider|riders|people))?(?: \w+){0,2}? (?:know|tell|spot|recognise|recognize|avoid|"
    r"prevent|identify|be sure|distinguish)\b"
    r"|\b(?:hypothetically|in theory|theoretically|out of (?:interest|curiosity)|just "
    r"(?:curious|wondering)|god forbid|for future reference|asking for a friend|generally "
    r"speaking)\b"
    r"|\bin case (?:i|you|we|someone|anyone) (?:ever|should|were to)\b"
    r"|\bif (?:i|you|we|someone|somebody|anyone|a rider|one) (?:ever|were to|was to|"
    r"happen(?:ed)? to|should ever)\b"
)
# "Signs of" or "symptoms of" asks only in a question or a request: "I've
# got symptoms of flu" is a symptom.
_SIGNS_OF_RX = re.compile(r"\b(?:signs?|symptoms?|warning signs?|red flags?) (?:of|that|for)\b")
_ASKING_RX = re.compile(
    r"^\s*(?:what|which|how|when|can|could|should|would|is|are|do|does|will|tell me|explain|"
    r"remind me|talk me through|run me through|list)\b"
)
# "Should I ride if I get a fever?" in a question: a time to come. Not "if I
# have a fever" or "if I'm having chest pain", which are now.
_IF_EVENT_RX = re.compile(
    r"\bif (?:i|you|we|someone|somebody|anyone|a rider|one)(?: \w+)? (?:get|gets|got|develop|"
    r"develops|experience|experiences|faint|faints|fainted|pass(?:es|ed)? out|black(?:s|ed)? "
    r"out|collapse[sd]?|crash(?:es|ed)?|fall|falls|fell|come off|comes off|came off|hit|hits|"
    r"bang|bangs|banged|suffer|suffers|end up|ended up|catch|catches|caught|go down|come down|"
    r"became|become|becomes|had)\b"
)
# The conditional itself, so "if I've got" doesn't read as having it now.
_CONDITIONAL_RX = re.compile(
    r"\b(?:if|whether|in case)\s+(?:i|you|we|someone|somebody|anyone|they|one|a rider)"
    r"(?:'ve|'d)?(?:\s+(?:ever|have|had|has|got|get|gets|did|do|does|were to|was to|should|"
    r"happen(?:ed)? to|been))*\b"
)
# Words that say it is happening to them, or has: it fires for real.
_REAL_NOW_RX = re.compile(
    r"\b(?:i've had|i had|i have had|i'm having|i am having|i've been having|i keep|i kept|"
    r"i've got|i have got|i got|i felt|i feel|i'm feeling|i am feeling|it felt|it feels|"
    r"i get|i often get|i always get|i sometimes get|i usually get|i've noticed|i noticed|"
    r"i've started|i started|happened|happening|today|tonight|yesterday|this morning|"
    r"this afternoon|this evening|last night|just now|right now|at the moment|again|still|"
    r"since|recently|lately|keeps? (?:getting|having|coming)|been getting|been having)\b"
    r"|\bmy (?:\w+ )?(?:chest|heart|head|helmet|temperature|temp|pulse|heart rate|hr|ribs|"
    r"breathing) (?:is|was|has|had|hurts|hurt|feels|felt|keeps|kept|went|goes|'s|did)\b"
    r"|\bthe (?:\w+ )?(?:pain|tightness|pressure|palpitations|fluttering|headache|fever|"
    r"temperature|faint|dizziness|crash|fall|knock|blow) (?:i|that i|which i|was|is|has|i've)\b"
)
# Anywhere in the message: the question is about something they had.
_REAL_ELSEWHERE_RX = re.compile(
    r"\b(?:it|this|that|they|these|those) (?:felt|feels|was|has been|came|comes|happened|"
    r"happens|started|starts|keeps|kept|hurt|hurts|came back|comes back|lasted|went)\b"
    r"(?=[^.!?\n]{0,40}?\b(?:today|yesterday|this morning|last night|tonight|on the|during|"
    r"again|still|now|earlier|when i|while i)\b)"
    r"|\bi (?:had|have had|'ve had|felt|got|'ve got|have got|keep getting|kept getting) "
    r"(?:it|this|that|them|these|those|something like (?:it|that|this))\b"
    r"|\bhappened (?:to me|today|yesterday|again|this morning|last night|on (?:the|my))\b"
)


def _hypothetical(match: re.Match, text: str, lo: int, hi: int) -> bool:
    """Whether a red flag sits in a general or hypothetical question ("what
    should I do if I ever get chest pain?", "what are the signs of
    heatstroke?", "how do I know if I've got concussion?") with nothing in
    the message saying it is happening to them, or has."""
    sentence = text[lo:hi]
    question = (hi < len(text) and text[hi] == "?") or bool(_ASKING_RX.search(sentence))
    framed = bool(_QUESTION_FRAME_RX.search(sentence)) or (
        question and bool(_SIGNS_OF_RX.search(sentence) or _IF_EVENT_RX.search(sentence))
    )
    if not framed:
        return False
    if _RIDERS_OWN.search(text[lo:match.start()]):
        return False  # "how do I deal with my concussion"
    if _REAL_NOW_RX.search(_CONDITIONAL_RX.sub(" ", sentence)):
        return False
    return not _REAL_ELSEWHERE_RX.search(text)


def _unsure(before: str, after: str) -> bool:
    """A denial the rider isn't sure of: "I wasn't knocked out I don't
    think", "as far as I know I didn't hit my head"."""
    return bool(_UNSURE_AFTER.search(after) or _UNSURE_BEFORE.search(before))


def _matched_words(rule: _Rule, m: re.Match, text: str) -> str:
    """What a hit records as matched. Signs of a head injury name the crash
    too ("headache since the crash"), so the record and the coach both see
    why a headache was held."""
    words = m.group(0).strip()
    if rule.kind == "head_knock":
        # Where it happened too: "hit my head on the cupboard door".
        lo, hi = _clause(text, m.start(), m.end())
        place = _household_knock(m, text, hi)
        if place is not None:
            words = text[m.start():m.end() + place.end()].strip()
    if rule.kind == "head_injury" and _group(m, "sym"):
        crash = _incident(text, (m.start(), m.end()))
        if crash is not None:
            lo, hi = min(m.start(), crash.start()), max(m.end(), crash.end())
            if hi - lo <= 120:
                words = text[lo:hi].strip()
            else:
                words = f"{words} ... {crash.group(0).strip()}"
    return words[:200]


def _first_hit(rule: _Rule, text: str) -> Hit | None:
    soft: Hit | None = None
    question: Hit | None = None
    for pattern in rule.patterns:
        for m in pattern.finditer(text):
            lo, hi = _clause(text, m.start(), m.end())
            before = text[max(lo, m.start() - _WINDOW):m.start()]
            after = text[m.end():min(hi, m.end() + _WINDOW)]
            # "before" / "after": the few words either side; "match": the
            # matched words themselves ("chest strap was tight").
            where_text = {"before": before, "after": after, "match": m.group(0)}
            if any(g.search(where_text[where]) for where, g in rule.guards):
                continue
            if (
                rule.negatable
                and _negated(before, text[m.end():min(hi, m.end() + 40)])
                and not (rule.kind in _UNSURE_KINDS and _unsure(before, text[m.end():hi]))
            ):
                continue
            # "my dad says I have a heart condition": the hit itself opens
            # with the rider speaking, so it is theirs.
            if (
                rule.third_party
                and not _FIRST_PERSON.match(m.group(0))
                and _someone_else(before)
            ):
                continue
            if not _passes_check(rule.kind, m, before, after, text, lo, hi):
                continue
            if rule.kind == "safety_question":
                if not _hypothetical(m, text, lo, hi):
                    continue
                topics = question_topics(Hit("safety_question", m.group(0), "info"))
                if topics and question is None:
                    question = Hit(
                        "safety_question", m.group(0).strip()[:200], "info",
                        topic=",".join(topics),
                    )
                continue
            if rule.kind in QUESTION_KINDS and _hypothetical(m, text, lo, hi):
                # A general question. Keep looking: the same red flag said
                # plainly anywhere else makes it the real thing.
                if question is None:
                    question = Hit(
                        "safety_question", _matched_words(rule, m, text), "info",
                        topic=rule.kind,
                    )
                continue
            if rule.soft_kind is not None and _training_talk(m, text, lo, hi):
                # Training talk, most likely. Keep looking: one match
                # anywhere without that context makes it the real thing.
                if soft is None:
                    soft = Hit(rule.soft_kind, m.group(0).strip()[:200], "info")
                continue
            stated_age = None
            if rule.kind == "minor":
                # The whole clause: "aged 16, I had my first race last week".
                if _past_age(m, text[m.end():hi]):
                    continue  # "aged 16 I broke my collarbone"
                stated_age = _minor_age(m)
                fixed = _corrected_age(text[m.end():m.end() + 40])
                if fixed is not None:
                    if fixed >= 18:
                        continue  # "I'm 17, no wait 47"
                    stated_age = fixed
            return Hit(
                rule.kind, _matched_words(rule, m, text), rule.severity, stated_age,
                new_event=rule.kind == "head_injury" and _new_head_event(text),
            )
    return soft or question


def stated_age_from(text: str) -> int | None:
    """The under-18 age a rider's words give, or None if they give none:
    for a minor red-flag record written before stated_age was kept."""
    for hit in _detect(text):
        if hit.kind == "minor":
            return hit.stated_age
    return None


# ── Acting on a message ─────────────────────────────────────────────────────


@dataclass
class ScreenResult:
    """What the check found in one message and what it did about it."""

    hits: list[Hit] = field(default_factory=list)
    # (style, text) in the order the rider should see them: emergency first.
    # The style is how the app shows it ("emergency", "crisis", "warning").
    cards: list[tuple[str, str]] = field(default_factory=list)
    # Which card each entry in `cards` is: "chest", "faint", "crisis", ...
    card_names: list[str] = field(default_factory=list)
    context_line: str | None = None
    event_ids: dict[str, str] = field(default_factory=dict)  # kind -> safety_events.id
    # Kinds Gareth should be emailed about: crisis and minor hits, unless the
    # same thing was already flagged for this rider in the last half hour.
    alerts: list[str] = field(default_factory=list)
    # The rider's country, for the numbers in the cards and the reply.
    country: str | None = None
    # The rider's own words, for the parts of a reply that depend on what
    # they asked ("Can I finish the set?" gets a plain no).
    message: str = ""

    @property
    def kinds(self) -> set[str]:
        return {h.kind for h in self.hits}

    @property
    def stated_age(self) -> int | None:
        """The age an under-18 hit implies, or None without one."""
        return next((h.stated_age for h in self.hits if h.kind == "minor"), None)

    def card_chunks(self) -> list[dict]:
        """The SSE payloads, sent before any model text."""
        names = self.card_names or [None] * len(self.cards)
        chunks = []
        for (style, text), name in zip(self.cards, names):
            chunk = {"type": "safety", "kind": style, "text": text}
            if name:
                chunk["card"] = name
            chunks.append(chunk)
        return chunks


def _cards_for(hits: list[Hit], country: str | None) -> list[tuple[str, str, str]]:
    """(style, text, name) for each card this message brings: at most one
    emergency-style card (the first of chest, faint, heart, head), then the
    crisis card, then the fever and heat warnings."""
    names = {CARD_FOR_KIND[h.kind] for h in hits if h.kind in CARD_FOR_KIND}
    chosen = [n for n in _EMERGENCY_CARD_ORDER if n in names][:1]
    chosen += [n for n in ("crisis", "fever", "heat") if n in names]
    return [(CARD_STYLE[n], card_text(n, country), n) for n in chosen]


def _card_shown_for(hit: Hit, shown: list[str]) -> str | None:
    """The style of the card that covered this hit, for the record."""
    name = CARD_FOR_KIND.get(hit.kind)
    if name is None:
        return None
    if name in shown:
        return CARD_STYLE[name]
    if name in _EMERGENCY_CARD_ORDER and any(n in _EMERGENCY_CARD_ORDER for n in shown):
        return "emergency"
    return None


def _quiet_after_mistake(db: Session, user_id: str, kind: str) -> SafetyHold | None:
    if kind not in QUIET_AFTER_MISTAKE:
        return None
    since = datetime.utcnow() - timedelta(days=MISTAKE_QUIET_DAYS)
    return (
        db.query(SafetyHold)
        .filter(
            SafetyHold.user_id == user_id,
            SafetyHold.red_flag == kind,
            SafetyHold.lifted_how == "mistake",
            SafetyHold.lifted_at >= since,
        )
        .order_by(SafetyHold.lifted_at.desc())
        .first()
    )


# The one named thing a standing hit is about, so a clearance covers that and
# nothing else. Brand and generic names of one medicine count as one; two
# medicines in the same class do not. First match wins, most specific first.
_TERMS: dict[str, tuple[tuple[str, str], ...]] = {
    "medication": (
        ("bisoprolol", r"bisoprolol"), ("atenolol", r"atenolol"),
        ("propranolol", r"propranolol"), ("metoprolol", r"metoprolol"),
        ("nebivolol", r"nebivolol"), ("carvedilol", r"carvedilol"),
        ("sotalol", r"sotalol"), ("labetalol", r"labetalol"),
        ("beta blocker", r"beta[- ]?blockers?"),
        ("insulin", r"insulin"),
        ("warfarin", r"warfarin"), ("apixaban", r"apixaban|eliquis"),
        ("rivaroxaban", r"rivaroxaban|xarelto"), ("edoxaban", r"edoxaban"),
        ("dabigatran", r"dabigatran|pradaxa"), ("clopidogrel", r"clopidogrel"),
        ("blood thinner", r"blood thinners?"), ("anticoagulant", r"anti-?coagulants?"),
        ("amlodipine", r"amlodipine|istin"), ("felodipine", r"felodipine"),
        ("nifedipine", r"nifedipine"), ("lercanidipine", r"lercanidipine"),
        ("diltiazem", r"diltiazem"), ("verapamil", r"verapamil"),
        ("ramipril", r"ramipril|tritace"), ("lisinopril", r"lisinopril|zestril"),
        ("perindopril", r"perindopril|coversyl"), ("enalapril", r"enalapril"),
        ("captopril", r"captopril"), ("candesartan", r"candesartan|amias"),
        ("losartan", r"losartan|cozaar"), ("irbesartan", r"irbesartan"),
        ("valsartan", r"valsartan"), ("olmesartan", r"olmesartan"),
        ("telmisartan", r"telmisartan"), ("indapamide", r"indapamide"),
        ("bendroflumethiazide", r"bendroflumethiazide"), ("doxazosin", r"doxazosin"),
        ("prednisolone", r"prednisolone"), ("prednisone", r"prednisone"),
        ("dexamethasone", r"dexamethasone"), ("steroids", r"steroid"),
        ("heart medication", r"heart (?:medication|medicine|meds|tablets|pills)|"
                             r"(?:medication|medicine|medicines|meds|tablets|pills)(?: \w+)? "
                             r"for (?:my )?heart"),
        ("blood pressure medication", r"(?:blood pressure|bp) (?:medication|medicine|meds|"
                                      r"tablets|pills)|(?:medication|medicine|medicines|meds|"
                                      r"tablets|pills)(?: \w+)? for (?:my )?(?:high )?"
                                      r"(?:blood pressure|bp|hypertension)"),
    ),
    "condition": (
        ("heart condition", r"heart condition"), ("heart problem", r"heart problem"),
        ("heart murmur", r"heart murmur"), ("arrhythmia", r"arrhythmia"),
        ("atrial fibrillation", r"atrial fibrillation|\baf\b|\bafib\b|\ba-fib\b"),
        ("cardiomyopathy", r"cardiomyopathy"),
        ("high blood pressure", r"hypertension|high blood pressure"),
        ("epilepsy", r"epilep"),
        ("type 1 diabetes", r"type 1 diabet"), ("type 2 diabetes", r"type 2 diabet"),
        ("diabetes", r"diabet"),
        ("asthma", r"asthma"), ("long covid", r"long covid"),
    ),
    "pregnancy": (
        ("pregnancy", r"pregnan|trimester|\bexpecting\b|\bcarrying\b|"
                      r"(?:weeks?|wks?|months?)[- ](?:along|gone)"),
        ("after a birth", r"\b(?:post-?partum|post partum|post-?natal|post natal|baby|son|"
                          r"daughter|little one|twins|birth|c-section|c section|"
                          r"caesarean|cesarean|breastfeeding)\b"),
    ),
}


def standing_term(kind: str, matched: str) -> str | None:
    """The one named medicine, condition or pregnancy stage a hit is about,
    or None if it names none."""
    text = _normalise(matched)
    for term, rx in _TERMS.get(kind, ()):
        if re.search(rx, text):
            return term
    return None


def _already_cleared(db: Session, user_id: str, hit: Hit) -> datetime | None:
    """When the rider was cleared for this exact medicine, condition or
    pregnancy stage, or None.

    It counts only when a hold that covered a mention of the same named term
    was lifted by clearance in the last 12 months. A screening clearance
    names nothing, so it covers nothing here; a clearance for one term never
    covers another (asthma never covers cardiomyopathy, a knee never covers a
    heart medicine); and a year on, the rider is asked again."""
    if hit.kind not in STANDING_KINDS:
        return None
    term = standing_term(hit.kind, hit.matched)
    if term is None:
        return None
    now = datetime.utcnow()
    since = now - timedelta(days=CLEARANCE_COVERS_DAYS)
    rows = (
        db.query(SafetyEvent.matched, SafetyEvent.created_at, SafetyHold.lifted_at)
        .join(SafetyHold, SafetyHold.id == SafetyEvent.hold_id)
        .filter(
            SafetyEvent.user_id == user_id,
            SafetyEvent.kind == hit.kind,
            SafetyHold.user_id == user_id,
            SafetyHold.lifted_how == "clearance",
            SafetyHold.lifted_at >= since,
        )
        .order_by(SafetyHold.lifted_at.desc())
        .all()
    )
    for matched, mentioned_at, lifted_at in rows:
        if standing_term(hit.kind, matched or "") != term:
            continue
        if term == "pregnancy" and _new_pregnancy(hit.matched, matched or "", mentioned_at, now):
            continue
        return lifted_at
    return None


# A pregnancy lasts about 40 weeks from the first day of the last period, and
# rarely past 42.
_PREGNANCY_WEEKS = 40
_PREGNANCY_WEEKS_MAX = 42
# How far behind the cleared pregnancy a stated week count may be before it
# is a different pregnancy rather than a rider rounding or misremembering.
_PREGNANCY_SLACK_WEEKS = 8
_ORDINALS = {
    "2nd": "second", "two": "second", "2": "second", "3rd": "third", "three": "third",
    "3": "third", "4th": "fourth", "four": "fourth", "4": "fourth", "5th": "fifth",
    "five": "fifth", "5": "fifth", "6th": "sixth", "six": "sixth", "6": "sixth",
    "another": "again", "next": "again", "new": "again",
}
_PREGNANCY_RX = re.compile(_PREGNANCY)
_PREGNANCY_ALONG_RX = re.compile(_PREGNANCY_ALONG)
_PREGNANCY_MARK_RX = re.compile(rf"\b(?P<mark>{_AGAIN_WORDS}) pregnancy\b")


_TENS = {"twenty": 20, "thirty": 30, "forty": 40}
_TEENS = {
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}


def _week_count(word: str) -> float | None:
    """A week count said in digits or words, up to forty."""
    word = word.strip()
    if word[:1].isdigit():
        return float(word)
    if word in _TEENS:
        return float(_TEENS[word])
    parts = re.split(r"[- ]", word)
    if parts[0] in _TENS:
        return float(_TENS[parts[0]] + (_WORD_NUMBERS.get(parts[1], 0) if len(parts) > 1 else 0))
    return _count(word)


def _pregnancy_facts(matched: str) -> tuple[float | None, str | None]:
    """(weeks pregnant, the word that makes it a new pregnancy) from what a
    pregnancy hit matched: "pregnant again, 8 weeks" is (8.0, "again")."""
    text = _normalise(matched)
    weeks = mark = None
    m = _PREGNANCY_RX.search(text)
    if m:
        for n, unit in (("wb", "ub"), ("wa", "ua")):
            if _group(m, n):
                count = _count(m.group(n).replace(".5", "")) + (0.5 if ".5" in m.group(n) else 0)
                weeks = count * (4.35 if (_group(m, unit) or "").startswith("month") else 1.0)
                break
        if weeks is None and _group(m, "wk"):
            weeks = float(m.group("wk"))
        mark = _group(m, "again") or _group(m, "nth") or _group(m, "time")
    else:
        along = _PREGNANCY_ALONG_RX.search(text)
        if along:
            count = _week_count(along.group("wa"))
            if count is not None:
                weeks = count * (4.35 if along.group("ua").startswith("month") else 1.0)
    if mark is None:
        mm = _PREGNANCY_MARK_RX.search(text)
        mark = mm.group("mark") if mm else None
    if mark is not None:
        mark = _ORDINALS.get(mark, mark)
    return weeks, mark


def _new_pregnancy(now_matched: str, then_matched: str, then_at, now: datetime) -> bool:
    """Whether a pregnancy mentioned now is a different one from the
    pregnancy a clearance covered: the rider says "again" (or "our second")
    where the cleared mention didn't, gives a lower week count than the
    cleared one, gives a count far behind where that pregnancy would be by
    now, or mentions it more than 40 weeks after the cleared one (sooner
    when the cleared one gave its week count)."""
    weeks_now, mark_now = _pregnancy_facts(now_matched)
    weeks_then, mark_then = _pregnancy_facts(then_matched)
    if mark_now is not None and mark_now != mark_then:
        return True
    then_at = _as_datetime(then_at)
    elapsed = (now - then_at).days / 7 if then_at is not None else 0.0
    lasts = _PREGNANCY_WEEKS
    if weeks_then is not None:
        lasts = min(lasts, _PREGNANCY_WEEKS_MAX - weeks_then)
    if elapsed > lasts:
        return True
    if weeks_now is not None and weeks_then is not None:
        if weeks_now < weeks_then:
            return True
        if weeks_now + _PREGNANCY_SLACK_WEEKS < weeks_then + elapsed:
            return True
    return False


def _settled_hits(hits: list[Hit], text: str) -> list[Hit]:
    """The hits to act on, made consistent whatever produced them (the regex,
    or the regex merged with the classifier): a knock about the house beside
    a head injury is that injury, a general question about a red flag that
    is also there for real is that red flag, and a head injury the
    classifier added is marked new when the words describe a fresh crash or
    blow."""
    real = {h.kind for h in hits}
    settled: list[Hit] = []
    for h in hits:
        if h.kind == "safety_question":
            left = [t for t in question_topics(h) if t not in real]
            if not left:
                continue
            h = replace(h, topic=",".join(left))
        settled.append(h)
    hits = settled
    if not any(h.kind == "head_injury" for h in hits):
        return hits
    norm = _normalise(text)
    return [
        replace(h, new_event=_new_head_event(norm))
        if h.kind == "head_injury" and not h.new_event else h
        for h in hits
        if h.kind != "head_knock"
    ]


def _new_event_kwargs(hit: Hit) -> dict:
    """open_hold's new_event for a head injury the detector reads as a fresh
    crash or blow (Hit.new_event), so the holds code restarts the clock
    rather than treating it as the earlier injury mentioned again. Empty
    when open_hold takes no such argument."""
    if hit.kind != "head_injury":
        return {}
    try:
        takes = "new_event" in inspect.signature(safety_service.open_hold).parameters
    except (TypeError, ValueError):
        takes = False
    return {"new_event": hit.new_event} if takes else {}


def screen_message(
    db: Session,
    user: User,
    text: str,
    *,
    message_id: str | None = None,
    source: str = "chat",
    hits: list[Hit] | None = None,
) -> ScreenResult:
    """Run the check on one rider message and act on what it finds.

    `hits`, when given, are acted on in place of the regex's own: the chat
    passes the regex's hits merged with the safety classifier's verdict, so
    they are acted on by exactly the same code.

    Never raises. Detection is pure and runs first, so the cards and the
    context line survive even if the database writes fail."""
    country = getattr(user, "country", None)
    result = ScreenResult(country=country, message=text or "")
    try:
        result.hits = list(hits) if hits is not None else _detect(text)
    except Exception:
        logger.exception("Red-flag detection failed (user=%s)", user.id)
        return result
    try:
        result.hits = _settled_hits(result.hits, text)
    except Exception:
        logger.exception("Settling the red-flag hits failed (user=%s)", user.id)
    if any(h.kind == "minor" for h in result.hits):
        # Forma lifted an under-18 hold on this account by hand: it decided
        # they are an adult, so for safety_service.MINOR_QUIET_DAYS the
        # under-18 rule opens nothing on their words ("I'm 16 and 18 watts
        # up"): no hold, no card and no adults-only reply. The chat still
        # records a genuine admission and emails Gareth
        # (coach_service._admission_after_lift). The coach can still flag
        # the account on purpose. A failed lookup keeps the hit, which errs
        # on the safe side.
        try:
            quiet = safety_service.minor_quiet_until(db, user)
        except Exception:
            logger.exception("Checking the under-18 quiet window failed (user=%s)", user.id)
            quiet = None
        if quiet is not None:
            result.hits = [h for h in result.hits if h.kind != "minor"]
    if not result.hits:
        return result

    severity_order = {s: i for i, s in enumerate(SEVERITIES)}
    result.hits.sort(key=lambda h: severity_order.get(h.severity, len(SEVERITIES)))

    cards = _cards_for(result.hits, country)
    result.cards = [(style, card) for style, card, _ in cards]
    result.card_names = [name for _, _, name in cards]

    notes: dict[str, str] = {}
    hold_now = None
    try:
        result.alerts = [
            h.kind for h in result.hits
            if h.kind in ALERT_KINDS and not recently_flagged(db, user.id, h.kind)
        ]
    except Exception:
        logger.exception("Checking earlier alerts failed (user=%s)", user.id)
        result.alerts = [h.kind for h in result.hits if h.kind in ALERT_KINDS]
    try:
        for hit in result.hits:
            hold = None
            level = HOLD_FOR.get(hit.kind)
            if level:
                quiet = _quiet_after_mistake(db, user.id, hit.kind)
                if quiet is not None:
                    notes[hit.kind] = (
                        "no hold opened, because the rider marked an earlier one for "
                        f"this a mistake on {quiet.lifted_at.date().isoformat()}. If this "
                        "is a new or current problem, call apply_safety_hold"
                    )
                elif (cleared := _already_cleared(db, user.id, hit)) is not None:
                    notes[hit.kind] = (
                        "no hold opened, because the rider confirmed a doctor's clearance "
                        f"for {standing_term(hit.kind, hit.matched)} on "
                        f"{cleared.date().isoformat()}. Any limits they recorded still "
                        "apply. If this is new or has changed, call apply_safety_hold"
                    )
                else:
                    before = safety_service.current_hold(db, user.id)
                    # The easy start after a break ends by itself; nothing
                    # else (a clearance included) lifts it.
                    ends = (
                        safety_service.layoff_hold_ends(layoff_days_from(hit.matched))
                        if hit.kind == "layoff" else None
                    )
                    hold = safety_service.open_hold(
                        db, user, level, HOLD_REASONS[hit.kind],
                        HOLD_SOURCE_FOR.get(hit.kind, "detector"),
                        red_flag=hit.kind, note=f'Matched "{hit.matched}"',
                        expires_at=ends, commit=False, **_new_event_kwargs(hit),
                    )
                    if before is not None and before.id == hold.id:
                        notes[hit.kind] = f"a {hold.level} hold was already open"
                    else:
                        notes[hit.kind] = f"{level} hold opened"
            if hit.kind in UNRECORDED_KINDS:
                # A general question is not about the rider's own body.
                continue
            fields = dict(
                user_id=user.id,
                kind=hit.kind,
                source=(source or "chat")[:20],
                message_id=message_id,
                matched=hit.matched[:200],
                card_shown=_card_shown_for(hit, result.card_names),
                hold_id=hold.id if hold is not None else None,
            )
            # The age a minor gave, so their records are kept to their 21st
            # birthday (purge_service reads it through safety_service).
            if hit.stated_age is not None and hasattr(SafetyEvent, "stated_age"):
                fields["stated_age"] = hit.stated_age
            event = SafetyEvent(**fields)
            db.add(event)
            db.flush()
            result.event_ids[hit.kind] = event.id
        db.commit()
        hold_now = safety_service.current_hold(db, user.id)
    except Exception:
        logger.exception("Recording a red flag failed (user=%s)", user.id)
        try:
            db.rollback()
        except Exception:
            pass
        result.event_ids = {}

    if any(note.endswith("hold opened") for note in notes.values()):
        # Label the planned sessions "On hold" to match a new full hold.
        try:
            from app.services.plan_service import sync_hold_marks

            sync_hold_marks(db, user.id)
        except Exception:
            logger.exception("Marking held sessions failed (user=%s)", user.id)
            try:
                db.rollback()
            except Exception:
                pass

    result.context_line = _context_line(result, notes, hold_now)
    return result


# ── What the reply must say, per red flag (the SAFETY CONTEXT line) ─────────

# Each kind's must-dos, filled with the rider's own numbers. Word for word
# lines are quoted so the coach can't soften them.
_MUSTS: dict[str, tuple[str, ...]] = {
    "chest_pain": (
        "If they asked whether to ride, train or carry on, the reply opens with "
        "\"No.\" Then ask whether it is happening now, and say what each answer means "
        "in the same breath: happening now, lasted more than a few minutes, or came "
        "with sweating, sickness, faintness, breathlessness or pain spreading to the "
        "arm, jaw, neck or back means call {emergency} now, even if it has settled. "
        "Settled without any of those still means being seen today: {heart_today}, "
        "for an ECG (a heart tracing) and a blood test. They don't drive themselves. "
        "Being short of breath at rest counts the same as chest pain here.",
        'Say "No riding of any kind, including easy spins, indoor sessions and '
        'commuting", and no other hard exertion (the gym, heavy lifting, running for '
        "a train) until a doctor has cleared them.",
        "Ask whether they have a known heart condition and whether anyone in their "
        "family died suddenly before 50. If it comes back, they shouldn't be alone.",
    ),
    "palpitations": (
        "If they asked whether to ride, train or carry on, the reply opens with "
        "\"No.\" Ask whether it is happening now. Racing or irregular now, or it came "
        "with chest pain, faintness or breathlessness: call {emergency} now. Settled: "
        "get seen today for an ECG (a heart tracing): {heart_today}.",
        'Say "No riding of any kind, including easy spins, indoor sessions and '
        'commuting" until a doctor has cleared them.',
        "Ask about a known heart condition and any family history of sudden death "
        "before 50.",
    ),
    "fainting": (
        "Open with the action, and a plain no if they asked to carry on: \"Stop "
        "riding now.\" Then: sit or lie down with legs raised, sip a sugary drink if "
        "they haven't eaten, cool down, don't stay alone, don't drive or ride.",
        "If they had chest pain, a racing or irregular heartbeat or breathlessness "
        "with it, or actually lost consciousness, tell them to call {emergency} now, "
        "in the same sentence you ask about those.",
        "Otherwise they get seen today for an ECG (a heart tracing): {heart_today}. "
        "Ask about any family history of sudden death before 50. No riding of any "
        "kind until a doctor has checked their heart.",
    ),
    "head_injury": (
        "If they asked about riding, the reply opens with \"No.\": no riding, "
        "training or racing until a doctor has checked them and they're free of "
        "symptoms.",
        "Include, word for word: \"Don't be alone for the next 24 hours, and don't "
        "drive.\" No alcohol for the next 24 hours.",
        "Ask whether they were knocked out, have a memory gap, have been sick, have "
        "neck pain, or take blood thinners. A headache that has lasted since the "
        "injury, or any of those: {hospital} today, with someone else driving. If "
        "their own message already says one of those, don't ask: tell them directly "
        "that it means {hospital} today, with someone else driving. Call {emergency} "
        "for a fit, drowsiness or being hard to wake, confusion, weakness or "
        "numbness, slurred speech, trouble with vision or balance, repeated vomiting, "
        "clear fluid from the nose or ears, or a headache that gets worse.",
        "Never say they have concussion, and never say they were sick unless they "
        "did. Describe the return as \"UK grassroots guidance after a suspected "
        "concussion: 24 to 48 hours of relative rest, a gradual return only once "
        "symptom-free, and no racing or group riding before day 21 after the "
        "injury.\" Replace the helmet, even if it looks fine.",
    ),
    "fever": (
        "If they asked to train, the reply opens with \"No.\": no riding or training "
        "at all with a fever. Name no condition: say \"training hard with a fever and "
        "a chest infection can put strain on the heart\", and that the fitness lost "
        "in a few days off is small.",
        "No riding at all until the fever has been gone 24 hours without paracetamol "
        "or ibuprofen and the chest has cleared, then at least 7 days of easy riding "
        "only, with no intervals or tests. An event date never changes this: say so, "
        "and that they should be ready to miss it.",
        "Call {emergency} for breathing difficulty at rest, blue lips, confusion or "
        "chest pain. Get seen today ({today_short}) for coughing up blood, a fever "
        "lasting more than three days, or getting worse.",
        "Include: \"If you get chest pain or a racing or irregular heartbeat when you "
        "ride again, stop and call {emergency}. If you're breathless out of "
        "proportion to the effort, stop and see {gp} before riding again.\"",
    ),
    "injury": (
        "Open with a plain answer: don't ride through it, and not the session they "
        "named. Skip that session with skip_workout. Never put a ride you prescribe "
        "in its place: the plan tools refuse it.",
        "Say: \"If you ride, keep it completely pain-free, flat, light gear, high "
        "cadence, and stop at the first twinge. Until a physio has seen it, that's "
        "the limit.\"",
        "Tell them to book a physiotherapist or sports medicine doctor soon. Get it "
        "seen today ({today_short}) for a hot, swollen or red joint with a fever or "
        "feeling unwell, not being able to bear weight, or numbness or tingling; "
        "within a day or two for locking, giving way, swelling or night pain.",
        "Describe the pain only in their words: never add worsening, sudden or under "
        "load. Ask once whether anything changed on the bike recently (fit, cleats, "
        "saddle), and suggest they tell the physio, without telling them to change it.",
    ),
    "pregnancy": (
        "If they asked for hard efforts or targets, open with no. No power, "
        "heart-rate or intensity targets for hard work: hard efforts wait for their "
        "midwife or obstetrician. The talk test (still able to talk in full "
        "sentences), no exhaustion or overheating, and riding indoors is safer as "
        "pregnancy goes on.",
        "Contact their maternity unit or midwife now for vaginal bleeding, leaking "
        "fluid, regular painful contractions, chest pain, dizziness or feeling faint, "
        "a severe headache, calf pain or swelling, breathlessness before exercise, or "
        "reduced baby movements; {emergency} if severe.",
    ),
    "medication": (
        "Easy riding by feel only (the talk test, or effort out of 10), with no power "
        "or heart-rate targets, until a doctor clears them. Ask what the medicine is "
        "for and whether their doctor set exercise limits.",
        "On a beta blocker, heart-rate zones don't apply: if they want to push harder "
        "to reach their zones, open with no, because the heart rate is held down on "
        "purpose and chasing it means riding far too hard. Heat tolerance is lower too.",
        "Safety net: chest pain, fainting or a racing or irregular heartbeat while "
        "riding means stop and call {emergency}, never \"speak to your doctor\".",
        "Include, word for word: \"Don't skip, change or re-time your medication to "
        "train; ask {gp} or the doctor who prescribes it.\"",
        "If they say their doctor already knows, that is a clearance (I've been "
        "cleared), never a mistake.",
    ),
    "condition": (
        "Easy riding by feel only (the talk test, or effort out of 10), with no power "
        "or heart-rate targets, until a doctor clears them. Ask whether their doctor "
        "set exercise limits. Safety net: chest pain, fainting or a racing or "
        "irregular heartbeat while riding means stop and call {emergency}.",
    ),
    "restriction": (
        "If they asked for a calorie, weight or training target, open by declining "
        "it plainly. Set no calorie, deficit, weight or rate-of-loss numbers, and use "
        "no W/kg or body-weight figures at all, the rider's own included. Never call "
        "any rate of loss safe or unsafe. Don't add or increase training: the plan "
        "tools refuse increases for 28 days.",
        "Explain RED-S once, plainly: Relative Energy Deficiency in Sport, when you "
        "eat too little for the training you do. Its signs include missed or "
        "irregular periods, low libido, getting ill often, stress fractures and "
        "dizziness.",
        "Refer to {gp} and a registered sports dietitian.{beat} No guesses about "
        "their motives, and one open question at most.",
    ),
    "crisis": (
        "Open warmly: you've read what they wrote and you're glad they said it. "
        "Then ask directly and kindly whether they are having thoughts of ending "
        "their life or hurting themselves.",
        "Give these numbers and only these: {crisis} {danger} Then {gp}, as the "
        "follow-up.",
        "Say, word for word: \"No one at Forma reads chats as they happen, so please "
        "use these numbers now.\"",
        "Encourage them to tell someone they trust today, and offer to pause the plan.",
        "Never mention Gareth, a flag, a review or anyone at Forma, never promise "
        "contact, and don't claim feelings of your own. End by pointing them to "
        "people, never with \"I'm here whenever you want to talk\" or a question "
        "to you.",
    ),
    "minor": (
        "This context confirms the account is now on hold (hold_all). Tell them "
        "kindly that Forma is for adults, 18 and over, and you can't coach them, with "
        "no training content and no more questions. Suggest {club} or a qualified "
        "youth coach, with a parent or guardian involved.",
        "Never mention a flag, a review, Gareth or anyone at Forma.",
    ),
    "heat": (
        "Open with the heat action, before anything else. No hard outdoor efforts "
        "between 11am and 3pm; at 35C or above, no outdoor riding then at any "
        "intensity, and a shorter, easy ride. Move it early or late, or indoors only "
        "if the room is cool with a fan, and expect lower power.",
        "The heatstroke signs are already on the card above your reply. If you "
        "mention them, use: \"If you or anyone with you becomes confused or slurred, "
        "has hot dry skin, collapses or has a fit, that's heatstroke: call "
        "{emergency} and cool them down while you wait.\" If they ride alone, they "
        "tell someone the route and when they'll be back.",
        "Drink to thirst, with electrolytes (salt) when sweating heavily; drinking "
        "far beyond thirst is risky too. Ask whether anything lowers their heat "
        "tolerance (beta blockers, water tablets, a recent illness, not being used to "
        "the heat). Never play the risk down. Any disagreement with the plan data "
        "comes last, in one line.",
    ),
    "layoff": (
        "Open with a plain answer if they asked for a test or hard work: no maximal "
        "test and no hard intervals now. Their first {weeks} back are easy riding by "
        "feel. Say \"you can start with easy riding by feel\", never that it's fine. "
        "Any hard effort, when it comes, starts with a proper warm-up of 15 to 20 "
        "minutes.",
        "Ask why they stopped: illness, injury, surgery, a heart problem, concussion "
        "or pregnancy means seeing {gp} first. After three months or more off, also "
        "ask about heart, lung or metabolic conditions (such as diabetes), chest pain "
        "or fainting on exertion, and family history of sudden death before 50; any "
        "yes means {gp} first.",
        "Safety net: stop and call {emergency} for chest pain, faintness or a racing "
        "heart. Believe them about the break: any question about the data comes "
        "last, in one line.",
    ),
    "big_jump": (
        "If it is far beyond their recent riding (about 1.5 times their longest ride "
        "in the last 8 weeks), or they say they're new, or they ask you to say it "
        "will be fine: open with, word for word, \"I don't recommend it, and I can't "
        "tell you you'll be fine.\" Say why in one line, then the better option (a "
        "shorter route, a later event).",
        "Only after that, the go-anyway list, built on what they told you: pace it "
        "low, eat from the start (about 60 g of carbohydrate an hour), drink to "
        "thirst with some salt rather than forcing down plain water, ride with others "
        "but keep a gap until used to groups, plan bail-out points, carry a phone, ID "
        "and money, know where the broom wagon is, tell someone the route, descend "
        "with care, and stop and call {emergency} for chest pain, dizziness or "
        "confusion.",
        "Believe them. Any question about the data comes last, in one line, and "
        "don't read back their stored rides or events: the account might not be "
        "theirs.",
    ),
    "responsibility": (
        "No legal opinion either way, and no sentence of your own about who is "
        "responsible or liable: the check removes them. The terms are at "
        "ridewithforma.com/terms, they don't take away any rights the law gives the "
        "rider, and questions about them go to gareth@ridewithforma.com.",
        "Include, word for word: \"If you have a health condition, symptoms or take "
        "medicine, check with {gp} before training.\" Say Forma is AI and can be "
        "wrong, it isn't medical advice, they decide what they ride, and they stop "
        "and get help if anything feels wrong.",
        "Don't encourage them to ride the plan in this reply.",
    ),
    "head_knock": (
        "A knock to the head about the house that they shrugged off: no card and no "
        "hold. Answer what they asked. Then ask once, in a plain sentence of its own, "
        "whether they've had a headache, felt sick, dizzy or confused, or had blurred "
        "vision since. If they have, or they were knocked out, follow SAFETY LAW 2c in "
        "full. Don't play it up or down, and say nothing about a check, a hold or a card.",
    ),
    "distress_check": (
        "Their words are most likely about training, but they could mean the rider "
        "is struggling, and the check can't tell which. Answer what they asked, then "
        "check in kindly and ask them directly whether they're okay, in a plain "
        f"sentence of its own, such as \"{DISTRESS_LINE}\"",
        "No crisis numbers, no flag_for_review, and no mention of a check, a flag, "
        "a review or anyone at Forma, unless they tell you they're not okay. If they "
        "do, or anything they say is about ending their life, hurting themselves or "
        "not wanting to be here, follow SAFETY LAW rule 4 in full. Don't play the "
        "words down or joke about them.",
    ),
}

# Heart pain that isn't in the chest, said for the reply: the rider may have
# said "no chest pain" and will need telling why it still counts.
_HEART_ACHE_MUST = (
    "The rider described pain in the arm, jaw, neck or back with effort or "
    "breathlessness. Pain there can come from the heart even with no chest pain: "
    "tell them so plainly, and treat it exactly as chest pain."
)

# A general question about a red flag: what the answer must carry, so the
# coach answers with the right safety facts rather than none.
_QUESTION_LEAD = (
    "They asked a general question, not about something they said is happening to "
    "them, so no card was shown and nothing was put on hold. Answer it plainly and "
    "accurately with the safety facts below, without playing the risk up or down, and "
    "say nothing about a check, a hold or a card. Then ask once, in a plain sentence of "
    "its own, whether this is happening to them now or has happened recently; if it is "
    "or has, follow SAFETY LAW {laws} in full."
)
_QUESTION_FACTS = {
    "chest_pain": (
        "Chest pain, pressure or tightness on a ride, or breathlessness at rest, means stop "
        "at once. Call {emergency} if it is still there, lasts more than a few minutes, or "
        "comes with sweating, sickness, faintness, breathlessness or pain spreading to the "
        "arm, jaw, neck or back. If it settles, get seen the same day: {heart_today}. No "
        "riding until a doctor has checked your heart."
    ),
    "palpitations": (
        "A racing, pounding or irregular heartbeat at rest, at low effort or out of "
        "proportion to the effort means stop. Call {emergency} if it is still going or "
        "comes with chest pain, faintness or breathlessness; if it settles, get seen the "
        "same day for an ECG (a heart tracing): {heart_today}."
    ),
    "fainting": (
        "Fainting or nearly fainting on or after a ride: stop, sit or lie down with legs "
        "raised, and don't ride or drive on. Losing consciousness during exercise, or "
        "fainting with chest pain, a racing heart or breathlessness, means call "
        "{emergency}; otherwise get seen the same day for an ECG: {heart_today}."
    ),
    "head_injury": (
        "After any blow to the head: stop riding, don't be alone for 24 hours and don't "
        "drive. Go to {hospital} the same day for being knocked out, a memory gap, being "
        "sick, a headache that won't go, neck pain or taking blood thinners; call "
        "{emergency} for a fit, drowsiness, confusion, weakness, slurred speech or trouble "
        "with vision or balance. No riding until symptom-free, then a gradual return, and "
        "no racing or group riding before day 21 (UK grassroots guidance after a suspected "
        "concussion). Replace the helmet."
    ),
    "fever": (
        "No riding or training with a fever, or with an illness below the neck such as a "
        "chest infection or sickness and diarrhoea: wait until the fever has been gone 24 "
        "hours without paracetamol or ibuprofen, then at least 7 days of easy riding only. "
        "Call {emergency} for breathing difficulty at rest, blue lips, confusion or chest "
        "pain."
    ),
    "heat": (
        "Heat exhaustion (heavy sweating, dizziness, a headache, feeling sick, cramps) means "
        "stop, get into the shade, cool down and drink. Heatstroke (confusion or slurred "
        "speech, hot dry skin, collapsing or a fit) means call {emergency} and cool them "
        "down while waiting. In hot weather, no hard outdoor efforts between 11am and 3pm."
    ),
    "injury": (
        "Don't ride through pain: keep any riding pain-free, and see a physiotherapist or "
        "sports medicine doctor about an injury that hasn't settled. Get it seen today "
        "({today_short}) for a hot, swollen joint with a fever, not being able to bear "
        "weight, or numbness or tingling."
    ),
    "pregnancy": (
        "In pregnancy, hard efforts and any power or heart-rate targets wait for your "
        "midwife or obstetrician; easy riding by the talk test, no overheating, and riding "
        "indoors is safer as pregnancy goes on."
    ),
}
# What each topic of a general question is, by its words, for a question
# hit that has lost its topic (one passed back as a plain dict).
_QUESTION_TOPIC_WORDS = (
    ("chest_pain", r"\bchest|\bheart attack|\bcardiac|\bangina|\bbreath|\bribs\b"),
    ("palpitations", r"\bpalpitation|\bheart ?(?:rate|racing|flutter|skip|beat)|\bbpm\b|"
                     r"\bpulse\b|\barrhythm|\ba-?fib\b"),
    ("fainting", r"\bfaint|\bpass(?:ed|ing|es)? out|\bblack(?:ed|ing)? out|\bcollaps|"
                 r"\bconscious|\bcame (?:to|round)"),
    ("head_injury", r"\bhead\b|\bconcuss|\bhelmet|\bknocked (?:out|unconscious)|\btemple|"
                    r"\bskull|\bforehead"),
    ("fever", r"\bfever|\btemperature|\btemp\b|\bflu\b|\bcovid|\binfection|\bchills|"
              r"\bdiarrh|\bsickness|\bgastro|\bnoro|\bfood poisoning|\bburning up"),
    ("heat", r"\bheat|\boverheat|\bsun ?stroke|\bdegrees|\b\d+ ?[cf]\b|\bsweating"),
    ("injury", r"\binjur|\bsprain|\bbroken? (?:a |my |your )?(?:bone|collarbone|wrist|arm|leg)|"
               r"\bfractur|\btorn\b|\btendon|\bligament"),
    ("pregnancy", r"\bpregnan|\btrimester|\bexpecting|\bweeks (?:along|gone)"),
)


def question_topics(hit) -> list[str]:
    """The red flags a safety_question asks after, from its topic or, when
    that was lost on the way, from its words."""
    topic = getattr(hit, "topic", None)
    if topic:
        return [t for t in topic.split(",") if t in QUESTION_KINDS]
    words = _normalise(getattr(hit, "matched", "") or "")
    return [kind for kind, rx in _QUESTION_TOPIC_WORDS if re.search(rx, words)]


def _question_laws(topics: list[str]) -> str:
    laws = []
    for topic in topics:
        law = LAW_RULE.get(topic)
        if law and law not in laws:
            laws.append(law)
    return " and ".join(laws) or "rule 3"


def question_musts(hit, country: str | None) -> list[str]:
    """What a reply to a general question about a red flag must carry: the
    plain answer with the safety facts, in the rider's own numbers, and one
    question about whether it is happening to them."""
    loc = _local(country)
    topics = question_topics(hit)
    out = [_QUESTION_LEAD.format(laws=_question_laws(topics))]
    out.extend(_QUESTION_FACTS[t].format(**loc) for t in topics if t in _QUESTION_FACTS)
    return out


# A rider who may be under 18 gets no training content at all, so only these
# kinds' must-dos still apply alongside "minor".
_STILL_APPLY_FOR_A_MINOR = frozenset(
    {"minor", "crisis", "chest_pain", "palpitations", "fainting", "head_injury"}
)

# Beat, introduced rather than pasted bare: a number with no context reads
# as an accusation.
_BEAT_LINE = (
    "If food or weight is feeling hard to manage, Beat, the UK eating disorder "
    "charity, can help: 0808 801 0677 in England; other UK numbers at "
    "beateatingdisorders.org.uk."
)


def _fill(template: str, loc: dict, weeks: str | None = None) -> str:
    beat = (
        f' Include, word for word: "{_BEAT_LINE}"'
        if loc["label"] in ("the UK", "unknown") else ""
    )
    return template.format(**loc, beat=beat, weeks=weeks or layoff_weeks(None))


def musts_for(
    kinds: set[str], country: str | None, matched: dict[str, str] | None = None
) -> list[str]:
    """What the reply must do for these red flags, in the rider's numbers."""
    loc = _local(country)
    if "minor" in kinds:
        kinds = kinds & _STILL_APPLY_FOR_A_MINOR
    weeks = layoff_weeks(layoff_days_from((matched or {}).get("layoff", "")))
    out: list[str] = []
    for kind in _MUSTS:
        if kind in kinds:
            out.extend(_fill(line, loc, weeks) for line in _MUSTS[kind])
    return out


def numbers_line(country: str | None) -> str:
    """The rider's own emergency and crisis numbers, for any coach prompt."""
    loc = _local(country)
    if loc["label"] == "unknown":
        return (
            "The rider's country isn't known: give the UK numbers and add 112 for the "
            f"EU and 911 for the US. Emergency: {loc['emergency']}. Crisis: "
            f"{loc['crisis']}"
        )
    return (
        f"The rider is in {loc['label']}. Use only these numbers and services: "
        f"emergency {loc['emergency']}; heart symptoms or fainting are seen today: "
        f"{loc['heart_today']}; other urgent care today: {loc['today']}; crisis: "
        f"{loc['crisis']} {loc['danger']} Never name another country's numbers or "
        "services."
    )


def _context_line(
    result: ScreenResult, notes: dict[str, str], hold: SafetyHold | None = None
) -> str:
    lines = [
        "SAFETY CONTEXT (this message only). Forma's red-flag check runs on the "
        "rider's words before you do. It matched:"
    ]
    for hit in result.hits:
        action = notes.get(hit.kind)
        if hit.kind == "safety_question":
            topics = question_topics(hit)
            lines.append(
                f'- safety_question about {", ".join(topics) or "a red flag"}, matched '
                f'"{hit.matched}": a general question, not a symptom they said they have; '
                "no card, no hold and no alert. Follow SAFETY LAW "
                f"{_question_laws(topics)} only if it is happening to them."
            )
            continue
        if action is None and hit.kind in SOFT_KINDS:
            action = "no card, no hold and no alert"
        elif action is None and hit.kind == "head_knock":
            action = "no card and no hold, recorded only"
        lines.append(
            f'- {hit.kind}, matched "{hit.matched}"'
            + (f": {action}" if action else "")
            + f". Follow SAFETY LAW {LAW_RULE.get(hit.kind, 'rule 3')}."
        )
    lines.append(numbers_line(result.country))
    musts = musts_for(result.kinds, result.country, {h.kind: h.matched for h in result.hits})
    question = next((h for h in result.hits if h.kind == "safety_question"), None)
    if question is not None:
        musts.extend(question_musts(question, result.country))
    chest = next((h for h in result.hits if h.kind == "chest_pain"), None)
    if chest is not None and heart_ache_hit(chest.matched):
        musts.insert(0, _HEART_ACHE_MUST)
    if "head_injury" in result.kinds:
        direct = head_direct_line(result.message)
        if direct:
            musts.insert(0, "Say this first (after a plain \"No.\" if they asked to ride), "
                            "word for word: \"" + _fill(direct, _local(result.country)) + "\"")
    if musts:
        lines.append("This reply must:")
        lines.extend(f"- {m}" for m in musts)
    for (style, text), name in zip(result.cards, result.card_names or [None] * len(result.cards)):
        lines.append(
            f'The app has already shown the rider this {style} card above your reply: '
            f'"{text}" Your reply still puts safety first. Do not repeat the card word '
            "for word."
        )
    if hold is not None:
        lift = lift_sentence(hold)
        if lift:
            lines.append(
                f"A {hold.level} hold is open. Say how it lifts in exactly these words: "
                f'"{lift}"'
            )
        else:
            lines.append(
                f"A {hold.level} hold is open that the rider can't lift. Say nothing "
                "about how it lifts."
            )
    if result.kinds and result.kinds <= QUIET_KINDS:
        lines.append(
            "Nothing was put on hold and no card was shown, so say nothing about a "
            "check, a hold or a card."
        )
    else:
        lines.append(
            "A hold never lifts by telling you in chat: never suggest it does, and never "
            "mention the This was a mistake button. The check matches words, so it can be "
            "wrong. If the words plainly meant something else (a strap, a jersey, someone "
            "else), say in one line that the check misread them and the notice at the top "
            "of the page lets them dismiss it. Otherwise treat it as real."
        )
    lines.append(
        "Only say you've done something (a hold, a skip, a change, a pause, a flag) "
        "when this context or a tool result in this reply confirms it."
    )
    return "\n".join(lines)


# ── The coach's own words, checked on the way out ──────────────────────────

# A sentence ends at . ! ? or : (with any closing quote, bracket or markdown)
# followed by whitespace, or at a line break.
_SENTENCE_END = re.compile(r"[.!?:][\"'\u201d\u2019)\]*_]*\s+|\n+")
_COLON_END = re.compile(r":[\"'\u201d\u2019)\]*_]*\s*$")

# The mistake button is for a check that misread the rider's words. The
# coach never offers it: a rider whose doctor knows is cleared, not mistaken.
_MISTAKE_RX = re.compile(
    r"\bthis was a mistake\b|\b(?:mark|marked|marking) (?:it|this|the hold) (?:as )?a "
    r"mistake\b|\bclear it yourself\b"
)
# Wording that says telling the coach lifts a hold. Nothing said in chat
# lifts a hold: only I've been cleared does, and it records who cleared them.
_TELL_ME_UNTIL = re.compile(
    r"\b(until|once|when|as soon as) you(?:'ve)? (?:tell|told|let) me (?:that )?"
    r"(?=(?:a|your|the) (?:doctor|gp|physio|physiotherapist|midwife|obstetrician|"
    r"cardiologist|consultant)\b[^.!?\n]{0,40}\b(?:cleared|signed|given|ok|okay|happy))",
    re.I,
)
_CLEARED = (
    r"(?:cleared you|cleared for|you've been cleared|you're cleared|you are cleared|"
    r"i've been cleared|been cleared|clearance|all[- ]clear|signed you off|given you "
    r"the go-ahead|given you the ok)"
)
_CHAT_LIFT_RX = re.compile(
    rf"\b(?:tell|let) me\b[^.!?\n]{{0,80}}{_CLEARED}"
    rf"|{_CLEARED}[^.!?\n]{{0,60}}\b(?:just )?(?:tell|let) me(?: know)?\b"
    r"|\b(?:that|this|which|it) (?:will )?lifts? (?:the|your) hold\b"
    r"|\bi(?:'ll| will| can) lift (?:the|your|this) hold\b"
    r"|\b(?:the|your) hold (?:lifts|will lift|comes off|is lifted) (?:when|once|as "
    r"soon as) you (?:tell|let) me\b"
)
# A hold, a pause or a freeze the coach says it has put on.
_HOLD_CLAIM_RX = re.compile(
    r"\b(?:i've|i have|i'm|i am)\s+(?:now\s+|just\s+|already\s+|also\s+)?(?:put|placed|"
    r"applied|added|opened|set|got|putting|placing)\b[^.!?\n]{0,50}\bhold\b"
    r"|\b(?:a|the|your|this) (?:full |easy[- ]only |safety )?hold is (?:now )?(?:on|in "
    r"place|open|applied|sitting)\b"
    r"|\b(?:i've|i have)\s+(?:now\s+|already\s+|just\s+)?(?:paused|frozen|suspended|"
    r"locked)\b[^.!?\n]{0,40}\b(?:plan|training|account|riding|sessions?)\b"
)
# A change to the plan the coach says it has made.
_PLAN_CLAIM_RX = re.compile(
    r"\b(?:i've|i have)\s+(?:now\s+|just\s+|already\s+|also\s+|gone ahead and\s+)?"
    r"(?:pulled|swapped|moved|skipped|changed|replaced|added|removed|dropped|cut|"
    r"rescheduled|updated|turned|marked|switched|taken out|shortened|cancelled|canceled|"
    r"rebuilt|scrapped)\b[^.!?\n]{0,60}\b(?:session|ride|workout|intervals?|plan|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow|week|"
    r"calendar|vo2|sweet spot|threshold|tempo)"
)
# In a crisis or under-18 turn: nothing that implies a person is watching,
# and nothing that casts the coach as the support.
_REVIEW_RX = re.compile(
    r"\bgareth\b|\bflag(?:ged|ging)?\b|\breview(?:ed|ing)?\b|\bsomeone (?:at|from) "
    r"forma\b|\b(?:the )?forma team\b|\bour side\b|"
    r"(?<!rate )(?<!hr )(?<!heart )(?<!glucose )(?<!pressure )\bmonitor(?:ed|ing)?\b|\bcheck in "
    r"(?:on|with) you\b|\breach out to you\b|\bget in touch with you\b|\bcontact you\b"
)
_NOT_MONITORED_RX = re.compile(
    r"\b(?:isn't|is not|not) monitored\b|\bno one at forma reads chats\b"
)
_ALWAYS_HERE_RX = re.compile(
    r"\bwhenever you (?:want|need|feel like you need) to talk\b|"
    r"\bi'm (?:always |still |right )?here\b|\bi'll (?:always )?be here\b"
)
# In a restriction turn: no weights, W/kg, calorie figures or rates of loss.
_BODY_NUMBERS_RX = re.compile(
    r"\bw/kg\b|\bwatts per kilo|\bpower[- ]to[- ]weight\b|\b\d+(?:\.\d+)?\s?(?:kg|kgs|"
    r"kilos?|lbs?|pounds|stone)\b|\b\d{1,2},?\d{3}\s?(?:k?cals?|calories|kcal)\b|"
    r"\b\d{3}\s?(?:k?cals?|calories|kcal)\b|\brate of (?:weight )?loss\b"
)
# A nudge to ride the plan, straight after a question about who's
# responsible if they're hurt (SAFETY LAW rule 6).
_RIDE_THE_PLAN_RX = re.compile(
    r"\bride (?:this week's|the|your|these|those) (?:sessions?|plan|workouts?)\b|"
    r"\b(?:keep|carry on) (?:riding|following) (?:the|your) plan\b|"
    r"\bstick (?:to|with) (?:the|your) plan\b|\btrust the plan\b"
)
# A statement of who is or isn't liable, after a question about it (SAFETY
# LAW rule 6). The fixed answer says what the terms do; the model never
# offers a legal opinion of its own, either way.
_LIABILITY_RX = re.compile(
    r"\b(?:responsible|liable|liability|at fault|to blame|accountable|negligen\w*|"
    r"compensat\w*|sue|sued|suing)\b"
)
# Playing a safety risk down.
_MINIMISING_RX = re.compile(
    r"\bstakes are lower\b|\bprobably nothing\b|\bit could be nothing\b|"
    r"\bnothing to worry about\b|\bno big deal\b"
)
# The stock line judges kept finding. Only the phrase goes; the rest of the
# sentence usually carries the advice.
_STOCK_RX = re.compile(
    r"\b(?:that's|that is|this is|this isn't|that isn't) not me being (?:over[- ]?)?"
    r"cautious(?: for (?:the sake of it|its own sake))?[,:;.]?\s*",
    re.I,
)
# "999 (112 in the EU, 911 in the US)" and friends, for a rider whose country
# is known: the other countries' numbers are noise at best.
_OTHER_REGIONS_RX = (
    (re.compile(r"\s*\((?:or\s+)?(?:999|112|911|988)\b[^)]*\b(?:EU|US|UK|Europe|America)\b[^)]*\)"), ""),
    (re.compile(r",?\s+or\s+(?:999|112|911)\s+in\s+the\s+(?:EU|US|UK)\b"), ""),
)
# A safety net that sends chest pain, fainting or a racing heart to "your
# doctor" drops a rung; it becomes the emergency number.
_HEART_WORDS_RX = re.compile(
    r"\bchest (?:pain|pressure|tightness)|\bfaint|\bpass(?:ed)? out\b|\bblack(?:ed)? out\b|"
    r"\bpalpitation|\bheart\b[^.!?]{0,40}\b(?:racing|irregular|strange|fluttering|pounding|"
    r"skipping|doing something)",
    re.I,
)
# Kit, not a body: the strap, the monitor and what they read. These are never
# symptoms, so "the heart rate trace looks irregular" is not a racing heart.
_DEVICE_PHRASE_RX = re.compile(
    r"\b(?:chest|hr|heart[- ]rate) (?:straps?|belts?|monitors?|sensors?)\b|"
    r"\bheart[- ]rate (?:data|trace|traces|reading|readings|graph|numbers|file|recording|"
    r"chart|channel|signal|line|figures|values)\b|\bhrms?\b|"
    r"\bfaint (?:signal|reading|readings|trace|connection|line)\b|"
    r"\b(?:screen|display|head unit|garmin|wahoo|computer|phone)(?:'s)? (?:\w+ )?"
    r"(?:blacked out|went black)\b",
    re.I,
)
# A sentence about kit that failed: a strap or a head unit that died or
# dropped out. In a turn with no red flag it is never treated as a symptom
# or as playing one down.
_DEVICE_RX = re.compile(
    r"\b(?:straps?|monitors?|hrms?|sensors?|batter(?:y|ies)|garmin|wahoo|polar|whoop|"
    r"coros|head unit|power meter|dropouts?|dropped out|dropping out|drops out|cut out|"
    r"cutting out|died|disconnected)\b",
    re.I,
)
_SEE_DOCTOR_RX = re.compile(
    r"\b(?:speak to|see|call|contact|ring|talk to|phone) (?:your |a )?(?:doctor|gp)"
    r"(?: straight away| immediately| urgently| right away| now| today| first)?",
    re.I,
)
_PLAN_TOOLS = frozenset({"update_workout", "swap_workout_date", "add_workout", "skip_workout"})
_PLAN_DONE = ("Updated", "Swapped", "Added", "Skipped")

# ── The lines a safety reply can't go without ──────────────────────────────

# What the rider asked, for the parts of a reply that answer it. Only a
# question about riding or training on gets a plain "No." in front of a ban:
# "What should I do?" or "Should I go to A&E?" never does.
_ASKS_TO_RIDE_RX = re.compile(
    r"(?<!what )(?<!how )\b(?:should|can|could|shall|may|do) i (?:still |just |even |"
    r"safely |actually )?(?:do|ride|train|race|finish|go out|carry on|keep|push|"
    r"get out|crack on|try|attempt|head out|go for)\b(?! (?:anything|something|about)\b)"
    r"|\bis it (?:ok|okay|alright|all right|fine|safe|sensible|wise|a good idea) to "
    r"(?:ride|train|race|do|go|carry on|keep|push|finish)\b"
    r"|\b(?:am i|i'm|im) (?:ok|okay|fine|good|alright|safe) to (?:ride|train|race|do|go)\b"
    r"|\bwhat (?:pace|power|watts|zones?|intensity|heart rate|speed)\b"
    r"|\b(?:i'll|ill|i will|i'm going to|im going to|i'm gonna|gonna) (?:just )?(?:ride|"
    r"push|do it|train|race|crack on|carry on|keep going|go out)\b"
    r"|\bride (?:it )?(?:through|out)\b|\bpush through\b"
)
# The rider is talking about now: "right now", "at the moment", "still".
_HAPPENING_NOW_RX = re.compile(
    r"\bright now\b|\bat the moment\b|\bstill (?:got|have|hurts|hurting|tight|there|going)\b"
    r"|\b(?:i've|i have) got\b|\bhappening now\b|\bnow\b"
)
_BETA_BLOCKER_RX = re.compile(
    r"\bbeta[- ]?blockers?\b|\b(?:bisoprolol|atenolol|propranolol|metoprolol|nebivolol|"
    r"carvedilol|sotalol|labetalol)\b"
)
# A head injury with any of the signs that mean hospital today.
_HEAD_HOSPITAL_RX = re.compile(
    r"\bheadache|\bhead (?:hurts|is sore|aches|pounding)|\bsick\b|\bvomit|\bthrew up\b|"
    r"\bunconscious|\bout cold|\bcame to\b|\bcame round\b|\bblanked out|\bwas out for\b|"
    r"\bchunk of time|\bno idea how i got|"
    r"\bknocked (?:out|unconscious)|\bblacked out|\bpassed out|\bmemory|\bcan't remember|"
    r"\bdon't remember|\bblood thinner|\bwarfarin|\bapixaban|\brivaroxaban|\bdrowsy|"
    r"\bconfus|\bdizzy"
)
# The signs, in the rider's own words, that mean hospital today after a blow
# to the head, most serious first. When the rider says one, the reply tells
# them so directly rather than listing conditions for them to match.
_HEAD_SIGNS: tuple[tuple[str, re.Pattern], ...] = (
    ("knocked_out", re.compile(
        r"\bknocked (?:myself |me )?(?:out|unconscious)\b(?! of\b)|\bout cold\b|"
        r"\bblacked out\b|\bpassed out\b|\blost consciousness\b|\bunconscious\b|"
        r"\bblanked out\b|\b(?:came|come) (?:to|round)\b(?= *(?:$|[.,!?;]|on|in|with|and|"
        r"lying|i\b|after|a few|later))|\bwas out (?:cold )?for\b|\bwoke up on the\b|"
        r"\bnext thing i knew\b"
    )),
    ("sick", re.compile(
        r"\bvomit(?:ed|ing|ted)?\b|\bthrew up\b|\bthrown up\b|\bthrowing up\b|"
        r"\b(?:been|was|got|being) sick\b(?! of\b)"
    )),
    ("memory", re.compile(
        r"\b(?:can't|cannot|can not|don't|do not|couldn't) remember\b|"
        r"\bmemory (?:gap|loss|is (?:hazy|blank|patchy))\b|\bgap in my memory\b|"
        r"\bno memory of\b|\blost (?:my )?memory\b|\bno (?:idea|recollection) (?:of )?how i got\b|"
        r"\blost (?:a |an )?(?:big |huge )?(?:chunk|bit|piece|block) of (?:time|memory|the)\b|"
        r"\b(?:chunk|gap|blank|hole) (?:in|missing from) (?:my )?memory\b"
    )),
    ("headache", re.compile(
        r"\bheadaches?\b|\bsore head\b|\bhead (?:hurts|aches|is (?:sore|hurting|aching|"
        r"pounding|throbbing|killing me)|(?:is )?(?:pounding|throbbing))\b"
    )),
    ("blood_thinners", re.compile(
        r"\bblood thinners?\b|\banti-?coagulants?\b|\b(?:warfarin|apixaban|eliquis|"
        r"rivaroxaban|xarelto|edoxaban|dabigatran|pradaxa|clopidogrel)\b"
    )),
)
# A sign the rider denies ("I wasn't knocked out", "no headache"), with any
# word of negation a few words before it: the reply must never tell them
# they have something they said they don't.
_SIGN_DENIED = re.compile(r"(?:\b(?:no|not|never|without|nor)\b|n't\b)(?:\s+[\w']+){0,3}\s*$")
_SIGN_GONE = re.compile(
    r"^\s*(?:[\w',]+\s+){0,6}?(?:has |had |have |is |it's )?(?:gone|went away|cleared|"
    r"passed|stopped|eased|settled|disappeared|lifted)\b"
)
# "had a headache last night" is past; "I've had a headache since" is not.
_SIGN_PAST = re.compile(
    r"(?<!'ve )(?<!have )(?<!has )(?<!'s )\bhad (?:a |an |some |a bit of a |a slight |"
    r"a mild |a bad )?$"
)
_HELMET_DAMAGE_RX = re.compile(
    r"\b(?P<v1>cracked|split|smashed|snapped|dented|broke|broken)(?: my| the)? helmet\b|"
    r"\bhelmet(?:'s| is| was| got| has| has been)?(?: \w+){0,2}? "
    r"(?P<v2>cracked|split|smashed|snapped|dented|broken)\b"
)
_HELMET_VERB = {
    "cracked": "crack", "split": "split", "smashed": "smash", "snapped": "snap",
    "dented": "dent", "broke": "break", "broken": "break",
}
_HEAD_HIT_RX = re.compile(
    r"\b(?:hit|banged|bashed|smacked|smashed|cracked|knocked|whacked|bumped|bounced|"
    r"clattered|clouted|thumped|slammed|split|gashed|"
    r"hitting|banging|bashing|smacking|smashing|cracking|knocking|whacking|bumping|bouncing|"
    rf"clattering|thumping|slamming|splitting) {_HEAD_WHERE}{_HEAD_PART}\b|"
    rf"\b{_HEAD_PART} (?:hit|struck|smacked|bounced off|went into|slammed into|smashed into)\b|"
    rf"\blanded on my {_HEAD_PART}\b|"
    rf"\b(?:blow|knock|bang|bump|whack|smack|thump|clout) (?:to|on) (?:the |my )?{_HEAD_PART}\b|"
    r"\bhead ?-?first (?:into|onto|over|on|off|through|in) (?:the |a |an |some )?(?:\w+ )?"
    rf"(?:{_SURFACE})\b"
)
# A crash named with no blow to the head said: "I can't remember the crash".
_CRASH_RX = re.compile(
    r"\b(?:crash|crashed|crashing|accident|came off|come off|coming off|fell off|"
    r"went down|hit the deck)\b"
)
_HEAD_LINE = {
    "headache": "You still have a headache after {ger}",
    "sick": "You were sick after {ger}",
    "memory": "You have a gap in your memory after {ger}",
    "blood_thinners": "You take blood thinners and you've {perf}",
}
_OUT_WORDS = (
    ("blacked out", "You blacked out"), ("passed out", "You passed out"),
    ("lost consciousness", "You lost consciousness"),
)


def head_sign(message: str) -> tuple[str, str] | None:
    """The most serious sign of a worse head injury the rider says they have
    (knocked out, sick, a memory gap, a headache still there, blood
    thinners), as (sign, the words that said it), or None. A sign they
    deny, one that has gone, or one about someone else doesn't count."""
    text = _normalise(message)
    for sign, rx in _HEAD_SIGNS:
        for m in rx.finditer(text):
            lo, hi = _clause(text, m.start(), m.end())
            before = text[max(lo, m.start() - 40):m.start()]
            after = text[m.end():hi]
            if _SIGN_DENIED.search(before) or _someone_else(before):
                continue
            if sign == "headache" and (_SIGN_GONE.search(after) or _SIGN_PAST.search(before)):
                continue
            return sign, m.group(0)
    return None


def head_direct_line(message: str) -> str | None:
    """The line that tells a rider with a sign of a worse head injury,
    directly, to go to hospital today: "You still have a headache after
    hitting your head hard enough to crack your helmet, so go to {hospital}
    today, with someone else driving." None without such a sign. The
    {hospital} is left for the rider's country."""
    found = head_sign(message)
    if found is None:
        return None
    sign, words = found
    text = _normalise(message)
    helmet = _HELMET_DAMAGE_RX.search(text)
    if helmet:
        verb = _HELMET_VERB[helmet.group("v1") or helmet.group("v2")]
        ger = f"hitting your head hard enough to {verb} your helmet"
        perf = f"hit your head hard enough to {verb} your helmet"
    elif _HEAD_HIT_RX.search(text):
        ger, perf = "hitting your head", "hit your head"
    elif _CRASH_RX.search(text):
        ger, perf = "the crash", "crashed"
    else:
        ger, perf = "a blow to the head", "had a blow to the head"
    if sign == "knocked_out":
        opening = next(
            (line for key, line in _OUT_WORDS if key in words), "You were knocked out"
        )
        lead = f"{opening} after {ger}"
    else:
        lead = _HEAD_LINE[sign].format(ger=ger, perf=perf)
    return lead + ", so go to {hospital} today, with someone else driving."


# The reply says it as a conclusion, not a condition: "go to A&E today" in a
# sentence with no "if" in it.
_DIRECT_HOSPITAL = (
    r"(?:^|[.!?\n]\s*)(?![^.!?\n]*\bif\b)[^.!?\n]*\b(?:go|get|head|going) (?:straight |now )?"
    r"(?:to )?(?:the )?(?:{hospital_rx})[^.!?\n]*\btoday\b"
)
_EVENT_RX = re.compile(r"\b(?:event|race|racing|sportive|gran fondo|audax|competition)\b")
# A request for numbers or more load, rather than a symptom described.
_RESTRICTION_ASK_RX = re.compile(
    r"\d|\bcalorie|\bkcal\b|\bdouble\b|\bdeficit\b|\bdiet\b|\blose\b|\bdrop\b|\bshed\b|"
    r"\bextra (?:sessions?|rides?|training)|\bmore training\b"
)
# A safety net: the emergency number tied to it happening now or coming
# back, not just any mention of the number (in France, 15 is also where a
# settled chest pain is seen).
_NET = (
    r"(?:comes? back|happening now|there now|returns?|again|gets? worse|at any point)"
    r"[^.!?\n]{0,80}(?:{emergency_rx})|(?:{emergency_rx})[^.!?\n]{0,40}(?:comes? back|"
    r"happening now|returns?|again|gets? worse|at any point)"
)
# Any reply that already rules riding out, in whatever words.
_NO_RIDE = (
    r"\b(?:don't|do not|no|not|never|stop|won't|shouldn't|should not) (?:ride|riding|"
    r"train|training|exercise|exercising|cycle|cycling|race|racing)\b|\boff the bike\b|"
    r"\bno (?:riding|training|exercise|ride|session)\b|\bdon't do (?:it|the|today|this|that)\b"
)

# Kinds that rule out riding altogether: next to one of these, advice on how
# to ride (pain-free spins, heat tips, a go-anyway list) contradicts the ban.
_NO_RIDING_KINDS = frozenset(
    {"chest_pain", "palpitations", "fainting", "head_injury", "fever", "minor"}
)
# The order a reply covers several red flags in: the most urgent first.
_KIND_ORDER = (
    "chest_pain", "palpitations", "fainting", "head_injury", "crisis", "minor",
    "fever", "pregnancy", "medication", "condition", "injury", "restriction",
    "heat", "layoff", "big_jump", "responsibility", "head_knock", "distress_check",
    "safety_question",
)


@dataclass(frozen=True)
class _Turn:
    """What the lines for one reply depend on."""

    kinds: frozenset
    matched: dict
    severity: dict
    text: str  # the rider's words and what matched, lower case
    loc: dict
    message: str = ""  # the rider's own words only, as they wrote them

    @property
    def asked_to_ride(self) -> bool:
        return bool(_ASKS_TO_RIDE_RX.search(self.text))

    def says(self, rx) -> bool:
        return bool(re.search(rx, self.text))

    @property
    def no_riding_advice(self) -> bool:
        """Advice on how to ride is left out: next to a red flag that rules
        riding out it contradicts the ban, and in a crisis it is noise."""
        return bool(self.kinds & _NO_RIDING_KINDS) or "crisis" in self.kinds


def _turn(
    kinds: set[str],
    matched: dict[str, str],
    country: str | None,
    message: str = "",
    severity: dict[str, str] | None = None,
) -> _Turn:
    kinds = set(kinds)
    if "minor" in kinds:
        kinds &= _STILL_APPLY_FOR_A_MINOR
    text = _normalise(" ".join([message or "", *matched.values()]))
    return _Turn(
        frozenset(kinds), dict(matched), dict(severity or {}), text, _local(country),
        message or "",
    )


def _heat_celsius(matched: str) -> float | None:
    """The temperature a heat hit named, in C, or None for a heatwave or alert."""
    m = re.search(r"(\d+(?:\.\d)?)", matched or "")
    if not m:
        return None
    value = float(m.group(1))
    if re.search(r"\d\s?(?:°|º|degrees?|deg)?\s?f(?:ahrenheit)?\b", matched.lower()):
        return (value - 32) * 5 / 9
    return value


def _hot_enough_for_no_midday(turn: _Turn) -> bool:
    celsius = _heat_celsius(turn.matched.get("heat", ""))
    return celsius is None or celsius >= 35


def _pregnant(turn: _Turn) -> bool:
    """Pregnant now, rather than after a birth: "pregnant", "second
    trimester", "I'm expecting", "I'm the one carrying"."""
    return bool(re.search(
        r"pregnan|trimester|\bexpecting\b|\bcarrying\b|(?:weeks?|wks?|months?)[- ](?:along|gone)",
        turn.matched.get("pregnancy", ""),
    ))


def _distance_only(turn: _Turn) -> bool:
    """A long ride coming up is only a big jump if it's far beyond their
    riding; that's the coach's call, so nothing is forced on a distance alone."""
    return bool(re.match(r"\d", turn.matched.get("big_jump", "")))


@dataclass(frozen=True)
class _Need:
    """One line the SAFETY LAW requires: the reply already covers it if
    `check` matches; otherwise `line` is added, in the rider's numbers."""

    check: str
    line: str
    when: Callable[[_Turn], bool] | None = None
    # How to ride: left out next to a red flag that rules riding out.
    ride: bool = False
    # The card above the reply already says it.
    card: bool = False
    # The hold's lift sentence already says it (a layoff's easy weeks).
    lift: bool = False
    # Builds the line from what the rider said, in place of `line`.
    make: Callable[[_Turn], str] | None = None


_GO_ANYWAY = (
    "If you go anyway: pace it low, eat from the start (about 60 g of carbohydrate "
    "an hour), drink to thirst with some salt rather than forcing down plain water, "
    "ride with others but keep a gap until you're used to groups, plan bail-out "
    "points, carry your phone, ID and money, know where the broom wagon is, tell "
    "someone the route, descend with care, and stop and call {emergency} for chest "
    "pain, dizziness or confusion."
)

# Per red flag, in the order a reply needs them: the direct answer first,
# then the primary action, then the safety net. A model reply that leaves
# one out gets it added at the end; a reply that never arrived is built from
# them (fallback_reply).
_REQUIRED: dict[str, tuple[_Need, ...]] = {
    "chest_pain": (
        _Need(_NO_RIDE, "Don't ride, not even an easy spin, until a doctor has checked you."),
        _Need("{heart_rx}", "If it has settled, you still need to be seen today: "
                            "{heart_today}. Don't drive yourself."),
        _Need(_NET, "If it's happening now or comes back at any point, call {emergency} now."),
    ),
    "palpitations": (
        _Need(_NO_RIDE, "Don't ride, not even an easy spin, until a doctor has checked you."),
        _Need(r"\bECG\b|heart tracing|{heart_rx}",
              "If it has settled, you still need to be seen today for an ECG, a heart "
              "tracing: {heart_today}."),
        _Need(_NET, "If it's happening now or comes back at any point, call {emergency} now."),
    ),
    "fainting": (
        _Need(_NO_RIDE, "Don't ride again until a doctor has checked your heart."),
        _Need(r"\bECG\b|heart tracing|{heart_rx}",
              "Get seen today for an ECG, a heart tracing: {heart_today}."),
        _Need("{emergency_rx}", "Call {emergency} now if you black out, or get chest pain, "
                                "a racing or irregular heartbeat or breathlessness."),
    ),
    "head_injury": (
        # The rider said they have a sign that means hospital today: say so,
        # as a conclusion, before anything else.
        _Need(_DIRECT_HOSPITAL, "",
              when=lambda t: head_direct_line(t.message) is not None,
              make=lambda t: head_direct_line(t.message) or ""),
        _Need(_NO_RIDE, "Don't ride, train or race until a doctor has checked you and "
                        "you're free of symptoms."),
        _Need("{hospital_rx}",
              "A headache that hasn't gone away, being sick, a gap in your memory or "
              "having been knocked out means {hospital} today, with someone else "
              "driving.",
              when=lambda t: t.says(_HEAD_HOSPITAL_RX)),
        _Need(r"(?=[\s\S]*slurred)(?=[\s\S]*(?:vision|balance))"
              r"(?=[\s\S]*(?:gets|getting) worse)(?=[\s\S]*(?:{emergency_rx}))",
              "Call {emergency} now for a fit, drowsiness or being hard to wake, "
              "confusion, weakness or numbness, slurred speech, trouble with vision or "
              "balance, a headache that gets worse, repeated vomiting, or clear fluid "
              "from the nose or ears."),
        _Need(r"(?=[\s\S]*(?:alone[^.]{0,40}24 hours|24 hours[^.]{0,40}alone))"
              r"(?=[\s\S]*\b(?:don't|do not|not to) drive\b)",
              "Don't be alone for the next 24 hours, and don't drive."),
        _Need(r"\balcohol\b", "Don't drink alcohol for the next 24 hours."),
        _Need(r"replace (?:the|your|it)\b[^.!?\n]{0,30}helmet|replace (?:the|your) helmet|"
              r"new helmet|helmet[^.!?\n]{0,40}replace",
              "Replace the helmet before you ride again, even if it looks fine.",
              when=lambda t: t.says(r"\bhelmet\b"), card=True),
    ),
    "fever": (
        _Need(r"\b(?:don't|do not|no|not) (?:train|training|ride|riding)\b|\brest\b",
              "Don't ride or train at all while you have a fever: training through it "
              "can put strain on the heart, and the fitness you lose in a few days off "
              "is small."),
        _Need(r"ready to miss|doesn't change|does not change",
              "An event date doesn't change this. I'll look at the event with you once "
              "you're clear, so be ready to miss it if you need to.",
              when=lambda t: t.says(_EVENT_RX)),
        _Need(r"struggling to breathe|blue lips|lips (?:turn|go) blue|cough(?:ing)? up blood",
              "Call {emergency} now if you're struggling to breathe at rest, your lips "
              "turn blue, you're confused or you have chest pain. Get seen today "
              "({today_short}) if you cough up blood, the fever lasts more than three "
              "days or you're getting worse.", card=True),
        _Need(r"palpitation|irregular heartbeat|racing heart|heart racing",
              "If you get chest pain or a racing or irregular heartbeat when you ride "
              "again, stop and call {emergency}. If you're breathless out of proportion "
              "to the effort, stop and see {gp} before riding again."),
    ),
    "injury": (
        _Need(r"ride through|riding through|push through|don't do (?:the|it|that)|skip",
              "Don't ride through that pain, and skip any hard or climbing sessions "
              "until it's been looked at."),
        _Need(r"physio|sports (?:medicine )?doctor",
              "Book a physio or sports medicine doctor to look at it soon, and tell them "
              "about any recent change to your bike, saddle or cleats."),
        _Need(r"pain[- ]free",
              "If you ride, keep it completely pain-free, flat, light gear, high cadence, "
              "and stop at the first twinge. Until a physio has seen it, that's the "
              "limit.", ride=True),
        _Need(r"can't (?:put|bear) (?:any )?weight|numb|tingl",
              "Get it seen today ({today_short}) if it turns hot, swollen or red with a "
              "fever, you can't put weight on it, or you get numbness or tingling. If it "
              "locks, gives way or swells, get it seen within a day or two."),
    ),
    "pregnancy": (
        _Need(r"talk test|full sentences|hold a conversation",
              "No full-gas efforts, and no power or heart-rate targets for hard work, for "
              "now. Ride at a level where you can still talk in full sentences, avoid "
              "overheating and exhaustion, and harder efforts wait for your midwife or "
              "obstetrician.", ride=True),
        _Need(r"indoors|fall risk",
              "Riding indoors takes away the fall risk as the bump grows.",
              when=_pregnant, ride=True),
        _Need(r"bleeding|leaking fluid|baby movements|contractions",
              "Stop and contact your maternity unit or midwife straight away for vaginal "
              "bleeding, leaking fluid, regular painful contractions, fewer baby "
              "movements, chest pain, dizziness or feeling faint, a severe headache, calf "
              "pain or swelling, or breathlessness before you start. If it's severe, call "
              "{emergency}.", when=_pregnant),
        _Need(r"bleeding|calf",
              "Stop and contact your midwife or {gp} straight away for heavy bleeding, "
              "chest pain, dizziness or feeling faint, a severe headache, calf pain or "
              "swelling, or breathlessness before you start. If it's severe, call "
              "{emergency}.", when=lambda t: not _pregnant(t)),
    ),
    "medication": (
        _Need(r"(?:heart[- ]rate|hr) zones? (?:don't|do not|won't|will not|no longer)|"
              r"won't reach|can't reach|will not reach|(?:holds?|keeps?) (?:it|your heart "
              r"rate) down|chas(?:e|ing) (?:it|your heart rate)",
              "Don't push harder to chase your heart rate. Beta blockers hold it down on "
              "purpose, so it won't reach your old zones however hard you go, and chasing "
              "it means riding far too hard. Ride easy and by feel for now (the talk "
              "test, or effort out of 10).",
              when=lambda t: t.says(_BETA_BLOCKER_RX), ride=True),
        _Need(r"by feel|talk test|out of 10",
              "Keep your riding easy and by feel for now (the talk test, or effort out of "
              "10), with no power or heart-rate targets, until a doctor clears you.",
              when=lambda t: not t.says(_BETA_BLOCKER_RX), ride=True),
        _Need(r"what (?:it's|it is|the medicine is|they're|it's prescribed) for|"
              r"any limits?|limit on",
              "Ask the doctor who prescribes it what it's for and whether they want any "
              "limit on hard efforts."),
        _Need("{emergency_rx}",
              "If you get chest pain, feel faint, or your heart races or beats irregularly "
              "while riding, stop and call {emergency} now."),
        _Need(r"\bskip\b[\s\S]{0,40}\bmedic|re-?time",
              "Don't skip, change or re-time your medication to train; ask {gp} or the "
              "doctor who prescribes it."),
    ),
    "condition": (
        _Need(r"by feel|talk test|out of 10",
              "Keep your riding easy and by feel for now (the talk test, or effort out of "
              "10), with no power or heart-rate targets, until a doctor clears you.",
              ride=True),
        _Need("{emergency_rx}",
              "If you get chest pain, feel faint, or your heart races or beats irregularly "
              "while riding, stop and call {emergency} now."),
    ),
    "restriction": (
        _Need(r"won't set|will not set|can't set|not going to set|won't give|won't add|"
              r"won't build|won't double",
              "I won't set a calorie or weight target, or add training to lose weight.",
              when=lambda t: bool(_RESTRICTION_ASK_RX.search(t.text))),
        _Need(r"RED-S|energy deficiency",
              "Eating too little for the training you do can lead to RED-S (Relative "
              "Energy Deficiency in Sport): getting ill more often, stress fractures, "
              "dizziness, missed or irregular periods, and slower riding, not faster."),
        _Need(r"dietitian",
              "Please talk to {gp} and a registered sports dietitian about eating enough "
              "for the riding you want to do."),
        _Need(r"0808 801 0677", _BEAT_LINE,
              when=lambda t: t.loc["label"] in ("the UK", "unknown")),
    ),
    "crisis": (
        _Need(r"thoughts of (?:ending|hurting|harming|suicide|killing)|ending your life|"
              r"suicid|kill yourself",
              "Are you having thoughts of ending your life or hurting yourself? If you "
              "are, please don't wait."),
        _Need("{crisis_rx}", "{crisis} {danger}"),
        _Need(r"reads chats|as they happen|monitored",
              "No one at Forma reads chats as they happen, so please use these numbers now."),
        _Need(r"\bGP\b|\bdoctor\b", "Talk to {gp} too, if your mood stays low."),
        _Need(r"someone you trust", "Tell someone you trust how you're feeling today."),
    ),
    "minor": (
        _Need(r"for adults|18 and over|over 18|aged 18",
              "Forma is for adults, 18 and over, so I can't coach you or build you a "
              "plan. {club_cap} or a qualified youth coach is the right place to start, "
              "with a parent or guardian involved."),
    ),
    "heat": (
        _Need(r"\bearly\b|evening|\bbefore \d|\bafter \d|indoors|inside|move it",
              "Don't ride outdoors between 11am and 3pm in heat like this. Go early in the "
              "morning or in the evening, keep it shorter and easy, or ride indoors with a "
              "fan in a cool room, and expect lower power.",
              when=_hot_enough_for_no_midday, ride=True),
        _Need(r"\bearly\b|evening|\bbefore \d|\bafter \d|indoors|inside|move it",
              "No hard efforts outdoors between 11am and 3pm: do it early in the morning "
              "or in the evening, or indoors with a fan, and expect lower power.",
              when=lambda t: not _hot_enough_for_no_midday(t), ride=True),
        _Need(r"heat ?stroke",
              "If you or anyone with you becomes confused or slurred, has hot dry skin, "
              "collapses or has a fit, that's heatstroke: call {emergency} and cool them "
              "down while you wait.", card=True),
        _Need(r"tell someone|your route",
              "If you ride alone, tell someone your route and when you'll be back: "
              "heatstroke can stop you calling for help.", ride=True),
        _Need(r"electrolyte|drink to thirst|\bsalt\b",
              "Drink to thirst, with electrolytes when you're sweating heavily, wear sun "
              "cream, and pick a route with shade and places to refill.", ride=True),
    ),
    "layoff": (
        _Need(r"warm[- ]?up",
              "No all-out test or hard intervals yet, and never a hard effort without a "
              "proper warm-up of 15 to 20 minutes.", ride=True),
        _Need(r"easy riding",
              "Your first {weeks} back are easy riding by feel, and harder sessions come "
              "after that.", ride=True, lift=True),
        _Need(r"why (?:did )?you stop|what kept you|why you stopped|what stopped you|"
              r"stopped because of illness",
              "What kept you off the bike? If it was illness, injury, surgery, a heart "
              "problem, concussion or pregnancy, see {gp} before you start.", lift=True),
        _Need("{emergency_rx}",
              "When you ride, stop and call {emergency} for chest pain, faintness or a "
              "racing heart."),
    ),
    "big_jump": (
        _Need(r"don't recommend|do not recommend|can't tell you|cannot tell you",
              "I don't recommend it, and I can't tell you you'll be fine: it's a very big "
              "step from where your riding is now.",
              when=lambda t: not _distance_only(t), ride=True),
        _Need(r"shorter|later (?:event|sportive|date|one)",
              "If there's a shorter route, switch to it, or pick a later event.",
              when=lambda t: not _distance_only(t), ride=True),
        _Need(r"bail[- ]?out", _GO_ANYWAY, when=lambda t: not _distance_only(t), ride=True),
    ),
    "responsibility": (
        _Need(r"ridewithforma\.com/terms",
              "I can't give you a legal answer. The terms at ridewithforma.com/terms set "
              "out who is responsible for what, and they don't take away any rights the "
              "law gives you. Questions about them go to gareth@ridewithforma.com.",
              when=lambda t: "crisis" not in t.kinds),
        _Need(r"can be wrong|not medical advice|isn't medical advice",
              "Plainly: Forma is an AI coach and can be wrong. It gives general training "
              "guidance, not medical advice, and it doesn't replace your doctor. You "
              "decide what you ride and how hard, and you stop and get help whenever "
              "something feels wrong.",
              when=lambda t: "crisis" not in t.kinds),
        _Need(r"check with (?:your|a) (?:gp|doctor)",
              "If you have a health condition, symptoms or take medicine, check with {gp} "
              "before training.",
              when=lambda t: "crisis" not in t.kinds),
    ),
    # A knock about the house, shrugged off: the signs to watch for.
    "head_knock": (
        _Need(r"headache|feel(?:ing)? sick|dizz|confus|blurr",
              HEAD_KNOCK_LINE),
    ),
    # Crisis words with clear training context: the reply asks, plainly.
    "distress_check": (
        _Need(_ASKED_IF_OKAY, DISTRESS_LINE),
    ),
    # A general question about an emergency red flag: the answer names the
    # emergency number, so the safety facts go in if it didn't.
    "safety_question": (
        _Need(r"{emergency_rx}", "", when=lambda t: bool(_turn_question_topics(t)
                                                          & _EMERGENCY_TOPICS),
              make=lambda t: " ".join(
                  _QUESTION_FACTS[k] for k in _turn_question_topics_ordered(t)
                  if k in _EMERGENCY_TOPICS
              )),
    ),
}


# The topics of a general question whose answer must name the emergency number.
_EMERGENCY_TOPICS = frozenset(
    {"chest_pain", "palpitations", "fainting", "head_injury", "fever", "heat"}
)


def _turn_question_topics_ordered(turn: _Turn) -> list[str]:
    words = turn.matched.get("safety_question")
    if not words:
        return []
    return question_topics(SimpleNamespace(matched=words, topic=None))


def _turn_question_topics(turn: _Turn) -> set[str]:
    return set(_turn_question_topics_ordered(turn))


def _need_text(need: _Need, turn: _Turn) -> str:
    """One required line, filled in for this rider."""
    return _format(need.make(turn) if need.make is not None else need.line, turn)


def _format(template: str, turn: _Turn) -> str:
    loc = turn.loc
    club = loc["club"]
    weeks = layoff_weeks(layoff_days_from(turn.matched.get("layoff", "")))
    return template.format(**loc, club_cap=club[:1].upper() + club[1:], weeks=weeks)


def _check_rx(check: str, loc: dict) -> str:
    for key in ("emergency_rx", "crisis_rx", "heart_rx", "hospital_rx"):
        check = check.replace("{" + key + "}", loc[key])
    return check


def _required_lines(turn: _Turn, said: str, shown: str = "", lift: str = "") -> list[str]:
    """The lines the reply still needs, in order: for each red flag (the most
    urgent first), every required line whose check `said` doesn't already
    pass. `shown` is the cards' text and `lift` the hold's lift sentence,
    which count only for the lines a card or the lift already carries. Each
    added line counts as said for the ones after it, so a safety net is never
    given twice."""
    lines: list[str] = []
    for kind in _KIND_ORDER:
        if kind not in turn.kinds:
            continue
        for need in _REQUIRED.get(kind, ()):
            if need.ride and turn.no_riding_advice:
                continue
            if need.when is not None and not need.when(turn):
                continue
            covered = said
            if need.card:
                covered += "\n" + shown
            if need.lift:
                covered += "\n" + lift
            covered += "\n" + "\n".join(lines)
            if re.search(_check_rx(need.check, turn.loc), covered, re.I):
                continue
            filled = _need_text(need, turn)
            if filled and filled not in lines:
                lines.append(filled)
    return lines


# ── When the model's reply never arrives ───────────────────────────────────

# The kinds whose ban is total: a rider who asks whether to ride or carry on
# hears "No." first.
_PLAIN_NO_KINDS = frozenset({"chest_pain", "palpitations", "fainting", "head_injury", "fever"})

# The crisis reply opens by showing the words were read, in terms of what
# the rider said.
_CRISIS_ACK = (
    (r"worthless", "Feeling worthless is a heavy thing to carry, and you matter far "
                   "more than any training plan."),
    (r"better off", "You matter, and feeling like a burden is something people can "
                    "help with."),
)
_CRISIS_ACK_DEFAULT = "What you're feeling matters, and you don't have to carry it on your own."


def _lead(kind: str, turn: _Turn) -> str | None:
    """The opening a reply built from fixed text needs before a kind's lines."""
    if kind == "crisis":
        ack = next(
            (line for rx, line in _CRISIS_ACK if re.search(rx, turn.text)),
            _CRISIS_ACK_DEFAULT,
        )
        return f"I've read what you wrote, and thank you for telling me. {ack}"
    if kind == "fainting":
        lead = (
            "Stop riding now. Lie down with your legs raised, don't stay alone, and "
            "don't drive."
        )
        if turn.severity.get("fainting") == "emergency":
            lead = (
                f"Losing consciousness during exercise is an emergency: call "
                f"{turn.loc['emergency']} now, even if you feel fine. " + lead
            )
        return lead
    if kind in ("chest_pain", "palpitations") and _HAPPENING_NOW_RX.search(turn.text):
        return (
            f"If it's there now, call {turn.loc['emergency']} now and don't wait to "
            "see if it settles."
        )
    return None


def hold_statement(hold, since: datetime | None = None) -> str | None:
    """What the hold means, said as a fact from the record: "I've put" only
    for a hold opened in this turn, and nothing at all without a hold."""
    if hold is None:
        return None
    opened = getattr(hold, "opened_at", None)
    new = since is not None and opened is not None and opened >= since
    level = getattr(hold, "level", None)
    if getattr(hold, "red_flag", None) == "minor":
        return "I've put this account on hold." if new else "This account is on hold."
    if level == "hold_all":
        return "I've put all your training on hold." if new else "All your training is on hold."
    if level == "easy_only":
        if getattr(hold, "source", None) == "layoff" or getattr(hold, "red_flag", None) == "layoff":
            return (
                "I've set your plan to easy riding while you ease back in." if new
                else "Your plan is on easy riding while you ease back in."
            )
        return (
            "I've set your plan to easy riding only." if new
            else "Your plan is on easy riding only."
        )
    return None


def _plain_no(kind: str, turn: _Turn) -> bool:
    """Whether this kind's block opens with "No." for this rider's question."""
    if not turn.asked_to_ride:
        return False
    if kind in _PLAIN_NO_KINDS:
        return True
    # Chasing heart rate on a beta blocker: no. Riding easy on one: yes.
    return kind == "medication" and turn.says(_BETA_BLOCKER_RX) and turn.says(
        r"\bpush|\bharder\b|\bzones?\b|\bhit\b"
    )


def fallback_reply(
    kinds: set[str],
    country: str | None,
    hold=None,
    *,
    matched: dict[str, str] | None = None,
    message: str = "",
    severity: dict[str, str] | None = None,
    since: datetime | None = None,
    shown: str = "",
) -> str | None:
    """A complete reply in fixed words, for a safety turn whose model reply
    never came (a provider failure, a reply cut off before any text) or an
    account that may belong to a child.

    It answers the question first ("No." to riding on, "Stop riding now."),
    restates the card's primary action, gives the safety net, states the hold
    from the record and says how it lifts. Never "send it again": the message
    arrived, was checked and acted on, and a rider with chest pain or in
    crisis needs to act, not retry. None when no red flag matched, so the
    caller falls back to an honest retry line."""
    turn = _turn(kinds, matched or {}, country, message, severity)
    if not turn.kinds:
        return None
    if turn.kinds <= QUIET_KINDS and hold is None:
        # Nothing to act on but the check-in or the signs to watch for: say
        # the reply failed, then ask.
        lines = _required_lines(turn, "", shown)
        return " ".join([REPLY_FAILED_MESSAGE, *lines])
    statement = hold_statement(hold, since)
    lift = lift_sentence(hold) if hold is not None else None
    tail = " ".join(x for x in (statement, lift) if x)
    # The lift sentence counts as said for the lines it covers, so the
    # fallback doesn't say the same thing twice.
    remaining = _required_lines(turn, "", shown, lift or "")
    # A crisis comes after any emergency but always last, so the reply ends
    # by pointing to people.
    order = [k for k in _KIND_ORDER if k in turn.kinds and k != "crisis"]
    order += ["crisis"] if "crisis" in turn.kinds else []
    blocks: list[str] = []
    first = True
    crisis_at = None
    for kind in order:
        own = []
        for need in _REQUIRED.get(kind, ()):
            if need.ride and turn.no_riding_advice:
                continue
            filled = _need_text(need, turn)
            if filled in remaining:
                own.append(filled)
                remaining.remove(filled)
        lead = _lead(kind, turn)
        parts = ([lead] if lead else []) + own
        if not parts:
            continue
        if first and _plain_no(kind, turn):
            parts[0] = "No. " + parts[0]
        first = False
        if kind == "crisis":
            crisis_at = len(blocks)
            blocks.extend(_crisis_paragraphs(parts, lead is not None, offer_pause=(
                not (turn.kinds & _NO_RIDING_KINDS)
                and getattr(hold, "level", None) != "hold_all"
            )))
            continue
        blocks.append(" ".join(parts))
    if tail:
        # In a crisis the hold goes before the crisis paragraphs.
        blocks.insert(len(blocks) if crisis_at is None else crisis_at, tail)
    return "\n\n".join(blocks) or None


def _crisis_paragraphs(parts: list[str], has_lead: bool, *, offer_pause: bool) -> list[str]:
    """The crisis reply in three short paragraphs: what was heard and the
    question; the numbers; then the people around them, last."""
    lead = parts[:1] if has_lead else []
    rest = parts[1:] if has_lead else list(parts)
    question = [p for p in rest if p.startswith("Are you having thoughts")]
    people = [p for p in rest if p.startswith(("Talk to ", "Tell someone you trust"))]
    numbers = [p for p in rest if p not in question and p not in people]
    if offer_pause:
        trust = [p for p in people if p.startswith("Tell someone you trust")]
        others = [p for p in people if p not in trust]
        people = others + ["If the plan is adding to the pressure, I can pause it."] + trust
    return [" ".join(x) for x in (lead + question, numbers, people) if x]


# What the rider sees when a reply fails in an ordinary turn: honest about
# what happened, and nothing about it not arriving (it did).
REPLY_FAILED_MESSAGE = "I couldn't finish a reply just now. Try sending that again in a moment."


class ReplyGuard:
    """The last check on the coach's own words before they reach the rider.

    Text is fed in as it streams and comes back a sentence at a time. Each
    sentence is checked against what the SAFETY LAW and the safety records
    say is true:
      - a claim of a hold or a plan change waits until a tool confirms it,
        and is dropped if none does (everything after it waits too, so the
        reply keeps its order);
      - "tell me when you're cleared" becomes the one true way the hold lifts,
        and the This was a mistake button is never offered in a safety turn;
      - in a crisis or under-18 turn, nothing about Gareth, a flag, a review or
        "I'm here whenever", which imply a person is watching;
      - in a restriction turn, no weights, W/kg or calorie figures;
      - after a question about who is responsible, no sentence of the
        model's own about liability, either way (the fixed answer covers it);
      - in any safety turn, no playing it down, chest pain or fainting goes to
        the emergency number rather than "speak to your doctor", and no other
        country's numbers or services (999 becomes 911 for a US rider);
      - a lead-in ending in a colon that nothing followed ("Here's what I
        changed:" before a tool that failed) is dropped.
    At the end, any line the law requires that the reply left out is added,
    and a hold opened this turn always ends with how it lifts. If nothing at
    all reached the rider in a safety turn, the whole fixed reply goes out
    instead (fallback_reply).

    Never raises into the stream: a failed check lets the sentence through,
    because the rider must always get an answer."""

    def __init__(
        self,
        screen: ScreenResult | None,
        country: str | None,
        hold_now: Callable[[], SafetyHold | None],
        *,
        since: datetime | None = None,
    ):
        hits = screen.hits if screen is not None else []
        self.kinds = {h.kind for h in hits}
        self.matched = {h.kind: h.matched for h in hits}
        self.severity = {h.kind: h.severity for h in hits}
        self.message = getattr(screen, "message", "") if screen is not None else ""
        # The cards already above the reply: what they say counts as said for
        # the lines a card carries, so the reply doesn't repeat them.
        self._shown = " ".join(
            text for style, text in (screen.cards if screen is not None else [])
            if style in RENDERED_CARD_STYLES
        )
        self._turn = _turn(self.kinds, self.matched, country, self.message, self.severity)
        self.country = country
        self._loc = _local(country)
        self._hold_now = hold_now
        self.since = since or datetime.utcnow()
        self._buf = ""
        self._colon = ""
        # (claim kind or None, text): a claim waiting on a tool, and every
        # sentence after it, in order.
        self._pending: list[tuple[str | None, str]] = []
        self.said = ""
        self.dropped: list[str] = []
        self._lift_said = False
        self._hold_done = False
        self._plan_done = False
        self._plan_tried = False
        self._capitalise = False
        self._break = ""
        self.safety_turn = bool(self.kinds) or self._hold() is not None

    # ── Feeding text in ──

    def feed(self, text: str) -> str:
        """Take streamed text; return what is ready for the rider."""
        if not text:
            return ""
        self._buf += text
        out = []
        while True:
            m = _SENTENCE_END.search(self._buf)
            if not m:
                break
            segment, self._buf = self._buf[: m.end()], self._buf[m.end():]
            out.append(self._take(segment))
        return "".join(out)

    def round_break(self) -> str:
        """The paragraph break between one round of the model's writing and
        the next, so a fresh sentence never glues onto the last."""
        if self._pending:
            if not self._pending[-1][1][-1:].isspace():
                self._pending.append((None, "\n\n"))
            return ""
        if self.said and not self.said[-1].isspace():
            return self._emit("\n\n")
        return ""

    def before_tools(self) -> str:
        """The model stopped to call a tool: whatever it wrote is complete.
        A lead-in ending in a colon is dropped: what it introduced hasn't
        happened yet, and may never."""
        out = self._take(self._buf) if self._buf.strip() else ""
        self._buf = ""
        self._drop_dangling_colon()
        return out

    def after_tools(self, results: list[tuple[str, str]]) -> str:
        """The tools ran: release what they back up, drop what they don't."""
        for name, result in results:
            text = result or ""
            if name == "apply_safety_hold" and (
                text.startswith("Hold applied now") or "already open" in text
            ):
                self._hold_done = True
                self.safety_turn = True
            if name in _PLAN_TOOLS:
                self._plan_tried = True
                if text.startswith(_PLAN_DONE):
                    self._plan_done = True
        return self._resolve(final=False)

    def flush(self) -> str:
        """The reply is finished: the last sentence, anything still waiting,
        then any line the law requires that's missing. A safety turn where
        nothing reached the rider gets the whole fixed reply instead."""
        out = self._take(self._buf) if self._buf.strip() else ""
        self._buf = ""
        self._drop_dangling_colon()
        out += self._resolve(final=True)
        if not self.said.strip():
            fallback = self.fallback()
            if fallback:
                return out + self._emit(fallback)
        try:
            extra = self._missing_lines()
        except Exception:
            logger.exception("Working out the required safety lines failed")
            extra = []
        if extra:
            out += self._emit(("\n\n" if self.said.strip() else "") + "\n\n".join(extra))
        return out

    def fallback(self, extra_kinds: set[str] | None = None) -> str | None:
        """The fixed reply for this turn (fallback_reply), or None when no
        red flag matched. `extra_kinds` adds what the record already knows
        (an account held as under 18). Never raises."""
        try:
            return fallback_reply(
                self.kinds | set(extra_kinds or ()), self.country, self._hold(),
                matched=self.matched, message=self.message, severity=self.severity,
                since=self.since, shown=self._shown,
            )
        except Exception:
            logger.exception("Building the fixed safety reply failed")
            return None

    # ── Inside ──

    def _hold(self) -> SafetyHold | None:
        try:
            return self._hold_now()
        except Exception:
            logger.exception("Reading the hold for the reply check failed")
            return None

    def _drop(self, segment: str, why: str) -> None:
        self.dropped.append(segment.strip())
        self._capitalise = True
        # A dropped sentence that ended a paragraph leaves the break behind.
        tail = segment[len(segment.rstrip()):]
        if "\n" in tail:
            self._break = tail
        logger.info("Reply check dropped a sentence (%s): %r", why, segment.strip()[:160])

    def _emit(self, text: str) -> str:
        if not text:
            return ""
        if self._break:
            if self.said and not self.said.endswith("\n") and not text[0].isspace():
                text = self._break + text
            self._break = ""
        if self._capitalise and text.strip():
            lead = len(text) - len(text.lstrip())
            if self.said.rstrip()[-1:] in ("", ".", "!", "?", ":") or self.said.endswith("\n"):
                text = text[:lead] + text[lead:lead + 1].upper() + text[lead + 1:]
            self._capitalise = False
        if self.said and not self.said[-1].isspace() and not text[0].isspace():
            text = " " + text
        self.said += text
        return text

    def _take(self, segment: str) -> str:
        if not segment.strip():
            if self._pending:
                self._pending.append((None, segment))
                return ""
            if self._colon:
                self._colon += segment
                return ""
            return self._emit(segment) if self.said else ""
        try:
            checked = self._check(segment)
        except Exception:
            logger.exception("Reply check failed; letting the sentence through")
            checked = (None, segment)
        if checked is None:
            return ""
        claim, text = checked
        if claim or self._pending:
            if self._colon:
                self._pending.append((None, self._colon))
                self._colon = ""
            self._pending.append((claim, text))
            return ""
        if _COLON_END.search(text):
            out = self._release_colon()
            self._colon = text
            return out
        return self._release_colon() + self._emit(text)

    def _release_colon(self) -> str:
        colon, self._colon = self._colon, ""
        return self._emit(colon) if colon else ""

    def _drop_dangling_colon(self) -> None:
        if self._colon.strip():
            self._drop(self._colon, "a lead-in with nothing after it")
        self._colon = ""
        while self._pending and (
            not self._pending[-1][1].strip() or _COLON_END.search(self._pending[-1][1])
        ):
            _, text = self._pending.pop()
            if text.strip():
                self._drop(text, "a lead-in with nothing after it")

    def _resolve(self, *, final: bool) -> str:
        if not self._pending:
            return ""
        hold_ok = None
        keep_waiting = False
        out = []
        pending, self._pending = self._pending, []
        for claim, text in pending:
            if keep_waiting:
                self._pending.append((claim, text))
                continue
            if claim == "hold":
                if hold_ok is None:
                    hold_ok = self._hold_done or self._hold() is not None
                ok = hold_ok
            elif claim == "plan":
                if self._plan_done:
                    ok = True
                elif self._plan_tried or self.safety_turn:
                    ok = False
                elif final:
                    ok = True  # no tool touched the plan: a recap of an earlier change
                else:
                    keep_waiting = True
                    self._pending.append((claim, text))
                    continue
            else:
                ok = True
            if ok:
                out.append(self._emit(text))
            else:
                self._drop(text, f"a {claim} claim no tool confirmed")
        return "".join(out)

    def _check(self, segment: str) -> tuple[str | None, str] | None:
        low = segment.lower().replace("\u2019", "'").replace("\u2018", "'")
        tail = segment[len(segment.rstrip()):]

        if self.kinds and _MISTAKE_RX.search(low):
            self._drop(segment, "the mistake button offered")
            return None

        segment = _TELL_ME_UNTIL.sub(lambda m: m.group(1) + " ", segment)
        low = segment.lower().replace("\u2019", "'")
        if _CHAT_LIFT_RX.search(low) and (self.safety_turn or self._hold() is not None):
            hold = self._hold()
            lift = lift_sentence(hold) if hold is not None else None
            if lift is None or self._lift_said:
                self._drop(segment, "a hold said to lift in chat")
                return None
            self._lift_said = True
            return None, lift + (tail or " ")

        if self.kinds & {"crisis", "minor"} and (
            (_REVIEW_RX.search(low) and not _NOT_MONITORED_RX.search(low))
            or _ALWAYS_HERE_RX.search(low)
        ):
            self._drop(segment, "a review or always-here line in a crisis or minor turn")
            return None
        if "responsibility" in self.kinds and _RIDE_THE_PLAN_RX.search(low):
            self._drop(segment, "a nudge to ride the plan after a liability question")
            return None
        if "responsibility" in self.kinds and _LIABILITY_RX.search(low):
            self._drop(segment, "a statement about liability (the fixed answer covers it)")
            return None
        if "restriction" in self.kinds and _BODY_NUMBERS_RX.search(low):
            self._drop(segment, "a weight or calorie figure in a restriction turn")
            return None
        if self.safety_turn:
            # A held rider asking about a strap that died: the strap being
            # "nothing to worry about" is about kit, not a symptom.
            kit_only = not self.kinds and bool(_DEVICE_RX.search(low))
            if _MINIMISING_RX.search(low) and not kit_only:
                self._drop(segment, "playing a safety risk down")
                return None
            if _STOCK_RX.search(segment):
                rest = _STOCK_RX.sub("", segment, count=1)
                if not rest.strip():
                    self._drop(segment, "a stock line")
                    return None
                lead = len(rest) - len(rest.lstrip())
                segment = rest[:lead] + rest[lead:lead + 1].upper() + rest[lead + 1:]
            segment = self._escalate(segment)
            segment = self._localise(segment)
            if segment is None:
                return None
            low = segment.lower().replace("\u2019", "'")

        if "i've been cleared" in low:
            if self._lift_said and self._is_lift_sentence(segment):
                self._drop(segment, "the lift sentence a second time")
                return None
            self._lift_said = True
        if _HOLD_CLAIM_RX.search(low) and not (self._hold_done or self._hold() is not None):
            return "hold", segment
        if _PLAN_CLAIM_RX.search(low) and not self._plan_done:
            return "plan", segment
        return None, segment

    def _is_lift_sentence(self, segment: str) -> bool:
        hold = self._hold()
        lift = lift_sentence(hold) if hold is not None else None
        return bool(lift) and segment.strip() == lift.strip()

    def _escalate(self, segment: str) -> str:
        """Chest pain, fainting or a racing heart in a safety net goes to the
        emergency number, never to "speak to your doctor"."""
        probe = _DEVICE_PHRASE_RX.sub(" ", segment)
        if not _HEART_WORDS_RX.search(probe) or re.search(
            self._loc["emergency_rx"], segment
        ):
            return segment
        return _SEE_DOCTOR_RX.sub(f"call {self._loc['emergency']} now", segment)

    def _localise(self, segment: str) -> str | None:
        if self._loc["label"] == "unknown":
            return segment
        for rx, repl in _OTHER_REGIONS_RX:
            segment = rx.sub(repl, segment)
        for pattern, repl in self._loc["swaps"]:
            segment = re.sub(pattern, repl, segment)
        invalid = self._loc["invalid"]
        if invalid and re.search(invalid, segment, re.I):
            self._drop(segment, f"a service that doesn't work in {self._loc['label']}")
            return None
        return segment

    def _missing_lines(self) -> list[str]:
        hold = self._hold()
        lift = None
        if (
            hold is not None
            and not self._lift_said
            and hold.opened_at is not None
            and hold.opened_at >= self.since
        ):
            lift = lift_sentence(hold)
            if lift and lift in self.said:
                lift = None
        # The lift sentence goes last, but counts as said for the lines it
        # already covers (a layoff's easy weeks, for one).
        lines = _required_lines(self._turn, self.said, self._shown, lift or "")
        if lift:
            # A hold the reply never mentioned is stated plainly, from the
            # record, in front of how it lifts.
            statement = (
                None if re.search(r"\bhold\b", self.said, re.I)
                else hold_statement(hold, self.since)
            )
            lines.append(f"{statement} {lift}" if statement else lift)
        return lines


# ── Telling Gareth (the safeguarding protocol) ──────────────────────────────

_background: set = set()


def _run_soon(coro) -> None:
    """Run a coroutine without making anyone wait for it: on the running loop
    when there is one, otherwise on a short-lived thread."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        threading.Thread(target=asyncio.run, args=(coro,), daemon=True).start()
        return
    task = loop.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _alert(
    kind: str,
    user_email: str,
    user_id: str,
    excerpt: str,
    event_ids: list[str],
    session_factory=None,
) -> bool:
    from app.services import email_service

    try:
        sent = await email_service.send_safety_alert(kind, user_email, user_id, excerpt)
    except Exception:
        logger.exception("Safety alert email failed (user=%s kind=%s)", user_id, kind)
        return False
    if not sent:
        logger.error("Safety alert email not sent (user=%s kind=%s)", user_id, kind)
        return False
    if event_ids:
        if session_factory is None:
            from app.database import SessionLocal as session_factory
        db = session_factory()
        try:
            now = datetime.utcnow()
            for event in db.query(SafetyEvent).filter(SafetyEvent.id.in_(event_ids)):
                event.founder_alerted_at = now
            db.commit()
        except Exception:
            logger.exception("Stamping founder_alerted_at failed (user=%s)", user_id)
        finally:
            db.close()
    return True


def alert_founder(
    kind: str, user: User, excerpt: str, event_ids: list[str] | None = None
) -> None:
    """Email Gareth about a crisis or under-18 red flag. Best effort and
    fire-and-forget: the rider's reply never waits on the mail provider."""
    _run_soon(_alert(kind, user.email, user.id, excerpt, list(event_ids or [])))


def alert_founder_for(result: ScreenResult, user: User, excerpt: str) -> None:
    """One email per alerting kind the check found in this message."""
    for kind in result.alerts:
        event_id = result.event_ids.get(kind)
        alert_founder(kind, user, excerpt, [event_id] if event_id else [])


# After a rider talks about hopelessness or worse, nothing Forma sends should
# press them about missed sessions, streaks or compliance for this long.
WELLBEING_QUIET_DAYS = 14


def wellbeing_quiet_until(db: Session, user_id: str) -> datetime | None:
    """The end of the quiet window after the rider's latest crisis words (from
    the check or the coach), or None if there are none in the last 14 days.
    Nudges, check-in emails and compliance prompts should stay silent, and
    plan language gentle, until then. Never raises: a failure reads as quiet,
    since pressing a rider in crisis costs more than one missed nudge."""
    since = datetime.utcnow() - timedelta(days=WELLBEING_QUIET_DAYS)
    try:
        last = (
            db.query(SafetyEvent.created_at)
            .filter(
                SafetyEvent.user_id == user_id,
                SafetyEvent.kind == "crisis",
                SafetyEvent.created_at >= since,
            )
            .order_by(SafetyEvent.created_at.desc())
            .first()
        )
    except Exception:
        logger.exception("Reading the wellbeing window failed (user=%s)", user_id)
        return datetime.utcnow() + timedelta(days=WELLBEING_QUIET_DAYS)
    return last[0] + timedelta(days=WELLBEING_QUIET_DAYS) if last else None


def recently_flagged(db: Session, user_id: str, kind: str, minutes: int = 30) -> bool:
    """Whether this kind was already flagged for this rider just now (by the
    check or by the coach), so Gareth gets one email per incident, not one
    per message."""
    since = datetime.utcnow() - timedelta(minutes=minutes)
    return (
        db.query(SafetyEvent.id)
        .filter(
            SafetyEvent.user_id == user_id,
            SafetyEvent.kind == kind,
            SafetyEvent.created_at >= since,
        )
        .first()
        is not None
    )


# ── The safety picture every coach surface reads ────────────────────────────

SCREEN_QUESTIONS = {
    "q1": "a heart condition or high blood pressure",
    "q2": "chest pain, pressure or tightness",
    "q3": "fainting, a blackout or losing balance from dizziness in the past 12 months",
    "q4": "a parent, brother or sister who died suddenly or had an inherited heart condition before 50",
    "q5": "a long-term condition or regular prescribed medicine",
    "q6": "an injury, a bone, joint or muscle problem, or surgery or concussion in the past three months",
    "q7": "pregnant, or a baby in the past 12 months",
    "q8": "told by a doctor to avoid hard exercise or exercise only under supervision",
}

ALLOWED_MEANS = {
    "all": "No safety limit on intensity today.",
    "easy": (
        "Easy riding only: recovery and endurance sessions with no step above 75% "
        "of FTP. No hard intervals, no tests, no intensity targets."
    ),
    "none": "Nothing at all: no sessions, no tests, no targets. Rest only.",
}


def triage_region(country: str | None) -> str:
    """Which rung of SAFETY LAW rule 1 applies: UK, EU, US or unknown."""
    code = (country or "").strip().upper()
    if code in _UK:
        return "UK"
    if code in _EUROPE_112:
        return "EU"
    if code in _NORTH_AMERICA:
        return "US"
    return "unknown"


def _screen_line(screening: HealthScreening) -> str:
    answers = screening.answers if isinstance(screening.answers, dict) else {}
    yeses = [SCREEN_QUESTIONS[q] for q in sorted(SCREEN_QUESTIONS) if answers.get(q) is True]
    when = screening.created_at.date().isoformat() if screening.created_at else "date unknown"
    said = "; ".join(yeses) if yeses else "no to every question"
    cleared = (
        f"confirmed on {screening.clearance_confirmed_at.date().isoformat()}"
        + (f" (by {screening.clearance_by})" if screening.clearance_by else "")
        if screening.clearance_confirmed_at
        else "not confirmed"
    )
    line = f"HEALTH SCREEN {screening.version} ({when}): yes to {said}. Clearance: {cleared}."
    if not yeses:
        line = f"HEALTH SCREEN {screening.version} ({when}): {said}."
    if screening.long_break:
        line += " Said they had four weeks or more off the bike in the three months before."
    return line


def coach_safety_context(db: Session, user: User) -> dict:
    """The rider's safety picture for any coach prompt: what intensity is
    allowed today and why, the open hold, the health screen, any limits a
    doctor set, the layoff gate, and the country for the triage numbers.

    SAFETY LAW rule 7 reads this. Never raises: a failure reads as "unknown",
    and the coach is told to treat unknown conservatively."""
    country = getattr(user, "country", None)
    context: dict = {
        "country": country or "unknown",
        "triage_region": triage_region(country),
        "numbers": numbers_line(country),
    }
    try:
        state = safety_service.safety_state(db, user)
        screening = safety_service.latest_screening(db, user.id)
    except Exception:
        logger.exception("Safety state failed (user=%s)", user.id)
        context["allowed_intensity"] = "unknown"
        context["means"] = (
            "The safety state could not be read. Prescribe nothing above easy "
            "riding this time."
        )
        return context

    context["allowed_intensity"] = state["allowed"]
    context["means"] = ALLOWED_MEANS.get(state["allowed"], "")
    hold = state.get("hold")
    if hold:
        since = (hold.get("opened_at") or "")[:10]
        lift = lift_sentence(SimpleNamespace(**hold))
        context["SAFETY HOLD"] = (
            f"{hold['level']} since {since}, from {hold['source']}: {hold['reason']}."
            " You never lift it yourself, and telling you in chat lifts nothing."
            + (
                f' If you mention how it lifts, use exactly: "{lift}"'
                if lift else " The rider can't lift it: say nothing about lifting it."
            )
        )
    if screening is not None:
        context["health_screen"] = _screen_line(screening)
    # Every limit a clinician set and nobody has retired, kept apart from the
    # screening so a re-screen, a second clearance or a rider who never
    # screened loses none of them.
    try:
        limits = safety_service.limit_lines(db, user)
        # A limit stamped on the screening before the limits table existed
        # (the migration copies them, so this is belt and braces).
        legacy = getattr(screening, "clearance_limits", None) if screening is not None else None
        if legacy and not any(line.startswith(f"{legacy} (from ") for line in limits):
            limits.append(legacy)
    except Exception:
        logger.exception("Reading the doctor's limits failed (user=%s)", user.id)
        limits = None
    if limits:
        context["doctor_limits"] = (
            "; ".join(limits) + " (hard constraints that beat the plan)"
        )
    elif limits is None:
        context["doctor_limits"] = (
            "Unknown: they could not be read. Prescribe nothing above easy riding "
            "this time."
        )
    quiet = wellbeing_quiet_until(db, user.id)
    if quiet is not None:
        context["wellbeing"] = (
            f"The rider talked about hopelessness or worse recently. Until "
            f"{quiet.date().isoformat()}: no pressure about missed sessions, streaks or "
            "compliance, gentle plan language, and offer to pause the plan rather than "
            "push it. If it comes up again, follow SAFETY LAW rule 4."
        )
    if state.get("layoff_gate_until"):
        context["layoff_gate"] = (
            f"Back from four weeks or more off: no hard intervals and no tests "
            f"before {state['layoff_gate_until']} (SAFETY LAW 2k)."
        )
    if state.get("no_racing_or_group_until"):
        context["no_racing_or_group_until"] = (
            f"After a head injury: no racing, no group rides and no chain gangs before "
            f"{state['no_racing_or_group_until']}, whatever the plan or a clearance says."
        )
    return context


def safety_changed_since(db: Session, user_id: str, since: datetime | None) -> bool:
    """Whether a hold opened or lifted, or the screening or a clearance
    changed, after `since`. Cached briefings and nudges written before that
    are out of date."""
    if since is None:
        return False
    try:
        now = datetime.utcnow()
        if (
            db.query(SafetyHold.id)
            .filter(
                SafetyHold.user_id == user_id,
                (SafetyHold.opened_at > since)
                | (SafetyHold.lifted_at > since)
                # A hold that ended by itself since then (the easy week after
                # a fever, the easy start after a break).
                | ((SafetyHold.expires_at > since) & (SafetyHold.expires_at <= now)),
            )
            .first()
            is not None
        ):
            return True
        return (
            db.query(HealthScreening.id)
            .filter(
                HealthScreening.user_id == user_id,
                (HealthScreening.created_at > since)
                | (HealthScreening.clearance_confirmed_at > since),
            )
            .first()
            is not None
        )
    except Exception:
        logger.exception("Safety change check failed (user=%s)", user_id)
        return False
