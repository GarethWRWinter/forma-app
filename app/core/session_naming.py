"""Canonical session naming.

A single source of truth for the human-friendly name of a workout, so the
calendar, the workout detail page, and the AI coach (Forma) all refer to the
same session by the same name — no continuity gaps. The zone/type carries the
"what"; these names carry the character. Structure (e.g. "3×15 sweet spot")
lives in the workout's description, which Forma also reads.

Selection is deterministic from the workout id, so a given workout always
shows the same name everywhere and across reloads.
"""

from __future__ import annotations

SESSION_NAMES: dict[str, list[str]] = {
    "recovery": [
        "Spin the Legs Loose", "Easy Does It", "Coffee-Pace Spin",
        "Just Turning Pedals", "Keep It Gentle", "Shake Out the Legs",
    ],
    "endurance": [
        "Time in the Saddle", "The Long Game", "Bank the Miles",
        "Money in the Bank", "Steady Wins", "Settle In",
        "Aerobic Patience", "Just Keep Pedalling",
    ],
    "tempo": [
        "Comfortably Hard", "Find Your Rhythm", "Hold the Line",
        "The Sustained Push", "Tempo, No Drama",
    ],
    "sweet_spot": [
        "The Sweet Spot", "Just Shy of the Red",
        "Productive Discomfort", "Threshold's Little Sibling",
    ],
    "threshold": [
        "Right at the Edge", "FTP Territory", "The Honest Hour",
        "Toe the Line", "Hold Your Threshold",
    ],
    "vo2max": [
        "Lungs on Fire", "Going Deep", "Maximum Effort",
        "Above the Red", "Embrace the Burn",
    ],
    "sprint": [
        "Full Gas", "Empty the Tank", "All or Nothing", "Send It",
    ],
    "rest": ["Rest"],
}


# Human-friendly label for a workout type — the "what" in plain words, used
# when Forma refers to a session by its kind rather than its character name
# (e.g. "an endurance session"). Never expose the raw enum repr to riders.
WORKOUT_TYPE_LABELS: dict[str, str] = {
    "recovery": "recovery",
    "endurance": "endurance",
    "tempo": "tempo",
    "sweet_spot": "sweet spot",
    "threshold": "threshold",
    "vo2max": "VO2 max",
    "sprint": "sprint",
    "rest": "rest",
}


def workout_type_label(workout_type: object) -> str:
    """Plain, lowercase name of a workout type (e.g. 'endurance', 'sweet spot').

    Accepts a WorkoutType enum member or a raw string; always returns the human
    label, never the 'WorkoutType.endurance' enum repr.
    """
    key = getattr(workout_type, "value", None) or str(workout_type)
    return WORKOUT_TYPE_LABELS.get(key, key.replace("_", " "))


def _stable_index(seed: str, length: int) -> int:
    """Deterministic, stable hash → index. Mirrors the previous frontend logic
    (h = h*31 + charCode, 32-bit) so names stay consistent."""
    if length <= 0:
        return 0
    h = 0
    for ch in seed:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h % length


def session_display_name(workout_type: str, workout_id: object) -> str:
    """The canonical, human-friendly name for a workout."""
    pool = SESSION_NAMES.get(workout_type) or SESSION_NAMES["rest"]
    return pool[_stable_index(str(workout_id), len(pool))]


# Long enough for "Sa Calobra rehearsal, 2 x 12", short enough for a calendar cell.
_OWN_TITLE_MAX = 48


def workout_display_name(workout_type: object, workout_id: object, title: str | None,
                         phase_id: object = None) -> str:
    """The name shown for a session, everywhere.

    A plan's sessions take the canonical name. A session that belongs to no
    plan was booked for this rider by name ("Sa Calobra rehearsal"), so it
    keeps that name: a stock name would hide what the rider asked for. Rest
    days keep their own title.
    """
    key = getattr(workout_type, "value", None) or str(workout_type)
    own = (title or "").strip()
    if key == "rest" or (phase_id is None and 0 < len(own) <= _OWN_TITLE_MAX):
        return own or session_display_name(key, workout_id)
    return session_display_name(key, workout_id)
