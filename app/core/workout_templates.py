"""
Structured workout template library.

Each template defines steps as a list of dicts:
  {
    "step_type": str (warmup/steady_state/interval_on/interval_off/cooldown/free_ride/ramp),
    "duration_seconds": int,
    "power_target_pct": float (% of FTP, e.g. 0.75 = 75%),
    "power_low_pct": float | None,
    "power_high_pct": float | None,
    "cadence_target": int | None,
    "repeat_count": int | None (for intervals, how many repeats of on/off pair),
    "notes": str | None,
  }

The 'repeat_count' on an interval_on step means the following on/off pair
should be repeated that many times.
"""


# === Recovery ===

RECOVERY_SPIN = {
    "name": "Recovery Spin",
    "workout_type": "recovery",
    "description": "Easy spin to flush legs. Keep effort very low.",
    "duration_seconds": 2700,  # 45 min
    "planned_if": 0.55,
    "steps": [
        {"step_type": "warmup", "duration_seconds": 300, "power_target_pct": 0.45, "notes": "Very easy spin"},
        {"step_type": "steady_state", "duration_seconds": 2100, "power_target_pct": 0.50, "cadence_target": 85},
        {"step_type": "cooldown", "duration_seconds": 300, "power_target_pct": 0.40},
    ],
}


# === Endurance ===

ENDURANCE_Z2_SHORT = {
    "name": "Short endurance ride",
    "workout_type": "endurance",
    "description": "Steady riding in Zone 2, an easy pace you could chat at. It builds your aerobic base.",
    "duration_seconds": 3600,  # 1 hour
    "planned_if": 0.65,
    "steps": [
        {"step_type": "warmup", "duration_seconds": 600, "power_target_pct": 0.55, "power_low_pct": 0.45, "power_high_pct": 0.65},
        {"step_type": "steady_state", "duration_seconds": 2700, "power_target_pct": 0.65, "power_low_pct": 0.56, "power_high_pct": 0.75, "cadence_target": 90},
        {"step_type": "cooldown", "duration_seconds": 300, "power_target_pct": 0.50},
    ],
}

ENDURANCE_Z2_LONG = {
    "name": "Long endurance ride",
    "workout_type": "endurance",
    "description": "A long ride in Zone 2, an easy pace you could chat at. Stay disciplined on the climbs.",
    "duration_seconds": 7200,  # 2 hours
    "planned_if": 0.65,
    "steps": [
        {"step_type": "warmup", "duration_seconds": 600, "power_target_pct": 0.55},
        {"step_type": "steady_state", "duration_seconds": 6300, "power_target_pct": 0.68, "power_low_pct": 0.56, "power_high_pct": 0.75, "cadence_target": 88},
        {"step_type": "cooldown", "duration_seconds": 300, "power_target_pct": 0.50},
    ],
}

ENDURANCE_Z2_EXTENDED = {
    "name": "Extended endurance ride",
    "workout_type": "endurance",
    "description": "An extended ride in Zone 2, an easy pace you could chat at. Time in the saddle is the point.",
    "duration_seconds": 10800,  # 3 hours
    "planned_if": 0.65,
    "steps": [
        {"step_type": "warmup", "duration_seconds": 900, "power_target_pct": 0.55},
        {"step_type": "steady_state", "duration_seconds": 9600, "power_target_pct": 0.68, "power_low_pct": 0.56, "power_high_pct": 0.75, "cadence_target": 88},
        {"step_type": "cooldown", "duration_seconds": 300, "power_target_pct": 0.50},
    ],
}


# === Tempo ===

TEMPO_BLOCKS = {
    "name": "Tempo Blocks",
    "workout_type": "tempo",
    "description": "3x15 min tempo blocks with recovery. Builds muscular endurance.",
    "duration_seconds": 4500,  # 75 min
    "planned_if": 0.80,
    "steps": [
        {"step_type": "warmup", "duration_seconds": 600, "power_target_pct": 0.55},
        {"step_type": "interval_on", "duration_seconds": 900, "power_target_pct": 0.83, "power_low_pct": 0.76, "power_high_pct": 0.90, "cadence_target": 85, "repeat_count": 3, "notes": "Tempo effort"},
        {"step_type": "interval_off", "duration_seconds": 300, "power_target_pct": 0.55},
        {"step_type": "cooldown", "duration_seconds": 300, "power_target_pct": 0.50},
    ],
}


# === Sweet Spot ===

SWEET_SPOT_2x20 = {
    "name": "Sweet Spot 2x20",
    "workout_type": "sweet_spot",
    "description": "2x20 min sweet spot intervals. Maximum fitness gain for moderate stress.",
    "duration_seconds": 4200,  # 70 min
    "planned_if": 0.88,
    "steps": [
        {"step_type": "warmup", "duration_seconds": 600, "power_target_pct": 0.55},
        {"step_type": "interval_on", "duration_seconds": 1200, "power_target_pct": 0.90, "power_low_pct": 0.88, "power_high_pct": 0.93, "cadence_target": 88, "repeat_count": 2, "notes": "Sweet spot"},
        {"step_type": "interval_off", "duration_seconds": 300, "power_target_pct": 0.55},
        {"step_type": "cooldown", "duration_seconds": 300, "power_target_pct": 0.50},
    ],
}

SWEET_SPOT_3x15 = {
    "name": "Sweet Spot 3x15",
    "workout_type": "sweet_spot",
    "description": "3x15 min sweet spot intervals. Great bang for your buck.",
    "duration_seconds": 4800,  # 80 min
    "planned_if": 0.88,
    "steps": [
        {"step_type": "warmup", "duration_seconds": 600, "power_target_pct": 0.55},
        {"step_type": "interval_on", "duration_seconds": 900, "power_target_pct": 0.90, "power_low_pct": 0.88, "power_high_pct": 0.93, "cadence_target": 88, "repeat_count": 3, "notes": "Sweet spot"},
        {"step_type": "interval_off", "duration_seconds": 300, "power_target_pct": 0.55},
        {"step_type": "cooldown", "duration_seconds": 300, "power_target_pct": 0.50},
    ],
}


# === Threshold ===

THRESHOLD_2x20 = {
    "name": "Threshold 2x20",
    "workout_type": "threshold",
    "description": "2x20 min at FTP. The classic threshold workout.",
    "duration_seconds": 4200,  # 70 min
    "planned_if": 0.95,
    "steps": [
        {"step_type": "warmup", "duration_seconds": 600, "power_target_pct": 0.55},
        {"step_type": "interval_on", "duration_seconds": 1200, "power_target_pct": 0.97, "power_low_pct": 0.95, "power_high_pct": 1.00, "cadence_target": 90, "repeat_count": 2, "notes": "FTP effort"},
        {"step_type": "interval_off", "duration_seconds": 300, "power_target_pct": 0.55},
        {"step_type": "cooldown", "duration_seconds": 300, "power_target_pct": 0.50},
    ],
}

THRESHOLD_4x10 = {
    "name": "Threshold 4x10",
    "workout_type": "threshold",
    "description": "4x10 min threshold intervals. Build FTP with shorter efforts.",
    "duration_seconds": 4200,  # 70 min
    "planned_if": 0.95,
    "steps": [
        {"step_type": "warmup", "duration_seconds": 600, "power_target_pct": 0.55},
        {"step_type": "interval_on", "duration_seconds": 600, "power_target_pct": 0.97, "power_low_pct": 0.95, "power_high_pct": 1.02, "cadence_target": 90, "repeat_count": 4, "notes": "Threshold"},
        {"step_type": "interval_off", "duration_seconds": 300, "power_target_pct": 0.55},
        {"step_type": "cooldown", "duration_seconds": 300, "power_target_pct": 0.50},
    ],
}

OVER_UNDER_3x12 = {
    "name": "Over-Under 3x12",
    "workout_type": "threshold",
    "description": "3x12 min over-under intervals. 2 min at 105%, 1 min at 90%, repeat 4x. Builds lactate tolerance.",
    "duration_seconds": 4200,  # 70 min
    "planned_if": 0.92,
    "steps": [
        {"step_type": "warmup", "duration_seconds": 600, "power_target_pct": 0.55},
        # Each 12-min block: 4x (2 min over + 1 min under)
        {"step_type": "interval_on", "duration_seconds": 120, "power_target_pct": 1.05, "cadence_target": 95, "repeat_count": 3, "notes": "Over: push above FTP"},
        {"step_type": "interval_off", "duration_seconds": 60, "power_target_pct": 0.90, "notes": "Under: recover at tempo"},
        # The inner 4x pairs happen within each 12-min block (simplified as intervals)
        {"step_type": "steady_state", "duration_seconds": 300, "power_target_pct": 0.55, "notes": "Recovery between blocks"},
        {"step_type": "cooldown", "duration_seconds": 300, "power_target_pct": 0.50},
    ],
}


# === VO2max ===

VO2MAX_5x5 = {
    "name": "VO2max 5x5",
    "workout_type": "vo2max",
    "description": "5x5 min at 106-120% FTP. Builds aerobic ceiling. Very hard.",
    "duration_seconds": 4500,  # 75 min
    "planned_if": 1.05,
    "steps": [
        {"step_type": "warmup", "duration_seconds": 900, "power_target_pct": 0.55},
        {"step_type": "interval_on", "duration_seconds": 300, "power_target_pct": 1.12, "power_low_pct": 1.06, "power_high_pct": 1.20, "cadence_target": 100, "repeat_count": 5, "notes": "VO2max - very hard"},
        {"step_type": "interval_off", "duration_seconds": 300, "power_target_pct": 0.50},
        {"step_type": "cooldown", "duration_seconds": 300, "power_target_pct": 0.45},
    ],
}

VO2MAX_4x4 = {
    "name": "VO2max 4x4",
    "workout_type": "vo2max",
    "description": "4x4 min at 110-120% FTP with 4 min recovery. Classic VO2max session.",
    "duration_seconds": 3900,  # 65 min
    "planned_if": 1.08,
    "steps": [
        {"step_type": "warmup", "duration_seconds": 900, "power_target_pct": 0.55},
        {"step_type": "interval_on", "duration_seconds": 240, "power_target_pct": 1.15, "power_low_pct": 1.10, "power_high_pct": 1.20, "cadence_target": 100, "repeat_count": 4, "notes": "VO2max"},
        {"step_type": "interval_off", "duration_seconds": 240, "power_target_pct": 0.50},
        {"step_type": "cooldown", "duration_seconds": 300, "power_target_pct": 0.45},
    ],
}

VO2MAX_MICRO_INTERVALS = {
    "name": "VO2max Micro Intervals",
    "workout_type": "vo2max",
    "description": "15s on/15s off micro intervals. 3 sets of 10 reps. Accumulate VO2max time.",
    "duration_seconds": 3600,  # 60 min
    "planned_if": 1.05,
    "steps": [
        {"step_type": "warmup", "duration_seconds": 900, "power_target_pct": 0.55},
        {"step_type": "interval_on", "duration_seconds": 15, "power_target_pct": 1.30, "cadence_target": 110, "repeat_count": 30, "notes": "30/30 micro intervals"},
        {"step_type": "interval_off", "duration_seconds": 15, "power_target_pct": 0.45},
        {"step_type": "cooldown", "duration_seconds": 300, "power_target_pct": 0.45},
    ],
}


# === Sprint ===

SPRINT_NEUROMUSCULAR = {
    "name": "Sprint Power",
    "workout_type": "sprint",
    "description": "6x10s max sprints with full recovery. Neuromuscular power development.",
    "duration_seconds": 3600,  # 60 min
    "planned_if": 0.75,
    "steps": [
        {"step_type": "warmup", "duration_seconds": 900, "power_target_pct": 0.55},
        {"step_type": "steady_state", "duration_seconds": 300, "power_target_pct": 0.70, "notes": "Build-up spin"},
        {"step_type": "interval_on", "duration_seconds": 10, "power_target_pct": 2.00, "cadence_target": 120, "repeat_count": 6, "notes": "Flat-out sprint"},
        {"step_type": "interval_off", "duration_seconds": 290, "power_target_pct": 0.45, "notes": "Full recovery between sprints"},
        {"step_type": "cooldown", "duration_seconds": 600, "power_target_pct": 0.45},
    ],
}


# === Template Registry ===

# Map workout types to available templates
WORKOUT_TEMPLATES = {
    "recovery": [RECOVERY_SPIN],
    "endurance": [ENDURANCE_Z2_SHORT, ENDURANCE_Z2_LONG, ENDURANCE_Z2_EXTENDED],
    "tempo": [TEMPO_BLOCKS],
    "sweet_spot": [SWEET_SPOT_2x20, SWEET_SPOT_3x15],
    "threshold": [THRESHOLD_2x20, THRESHOLD_4x10, OVER_UNDER_3x12],
    "vo2max": [VO2MAX_5x5, VO2MAX_4x4, VO2MAX_MICRO_INTERVALS],
    "sprint": [SPRINT_NEUROMUSCULAR],
}


def get_template(workout_type: str, duration_hint: int | None = None) -> dict:
    """
    Get a workout template by type.
    If duration_hint provided, picks closest match.
    """
    templates = WORKOUT_TEMPLATES.get(workout_type, [])
    if not templates:
        return RECOVERY_SPIN

    if duration_hint is None:
        return templates[0]

    # Pick closest duration
    return min(templates, key=lambda t: abs(t["duration_seconds"] - duration_hint))


def estimate_tss(template: dict, ftp: int) -> float:
    """Estimate TSS for a workout template."""
    if_val = template.get("planned_if", 0.65)
    duration = template.get("duration_seconds", 3600)
    # TSS = (duration / 3600) * IF^2 * 100
    return (duration / 3600) * (if_val ** 2) * 100


# === Phase Distribution Configs ===
# Weekly workout type distribution by training phase

PHASE_WORKOUT_MIX = {
    "off_season": {
        "days_per_week": 3,
        "types": ["recovery", "endurance", "endurance"],
    },
    "base": {
        "days_per_week": 4,
        "types": ["endurance", "endurance", "tempo", "recovery"],
    },
    "build": {
        "days_per_week": 5,
        "types": ["endurance", "sweet_spot", "threshold", "endurance", "recovery"],
    },
    "peak": {
        "days_per_week": 5,
        "types": ["endurance", "vo2max", "threshold", "sweet_spot", "recovery"],
    },
    "race": {
        "days_per_week": 4,
        "types": ["recovery", "vo2max", "endurance", "rest"],
    },
    "recovery": {
        "days_per_week": 3,
        "types": ["recovery", "endurance", "recovery"],
    },
}
