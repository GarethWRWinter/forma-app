"""
Classify rides into meaningful training categories based on power data.

Uses intensity factor (IF), variability index (VI), duration, and
power zone distribution to determine ride type.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def classify_ride(
    *,
    power_samples: list[int | float] | None = None,
    ftp: int = 0,
    duration_seconds: int = 0,
    normalized_power: float = 0,
    average_power: float = 0,
    intensity_factor: float = 0,
    variability_index: float = 0,
    ride_date: datetime | None = None,
) -> str:
    """
    Classify a ride into a training category and return a descriptive name.

    Classification hierarchy (checked in order):
    1. Insufficient data → use date-based name
    2. Very short rides → "Warm-up" or "Cool-down"
    3. IF-based primary classification
    4. Zone distribution refinement (intervals vs steady)
    5. Duration modifiers
    """
    if ftp <= 0 or duration_seconds < 120:
        return _date_name(ride_date)

    # Calculate IF if not provided
    if intensity_factor <= 0 and normalized_power > 0 and ftp > 0:
        intensity_factor = normalized_power / ftp

    if variability_index <= 0 and normalized_power > 0 and average_power > 0:
        variability_index = normalized_power / average_power

    duration_minutes = duration_seconds / 60

    # Very short rides
    if duration_minutes < 15:
        if intensity_factor < 0.65:
            return "Warm-up Spin"
        return "Short Effort"

    # Analyze power zone distribution if we have samples
    zone_pct = {}
    has_intervals = False
    if power_samples and ftp > 0:
        zone_pct = _zone_percentages(power_samples, ftp)
        has_intervals = _detect_intervals(power_samples, ftp)

    # Classification based on IF + zone distribution
    if intensity_factor < 0.55:
        if duration_minutes > 60:
            return "Recovery Ride"
        return "Easy Spin"

    elif intensity_factor < 0.70:
        if duration_minutes >= 150:
            return "Long Endurance Ride"
        elif duration_minutes >= 90:
            return "Endurance Ride"
        else:
            return "Endurance Ride"

    elif intensity_factor < 0.80:
        # Tempo zone - check for sweet spot intervals
        z3_z4 = zone_pct.get("Z3", 0) + zone_pct.get("Z4", 0)
        if has_intervals and z3_z4 > 30:
            return "Sweet Spot Intervals"
        elif z3_z4 > 40:
            return "Sweet Spot"
        elif duration_minutes >= 90:
            return "Tempo Endurance"
        else:
            return "Tempo Ride"

    elif intensity_factor < 0.90:
        z4 = zone_pct.get("Z4", 0)
        z5 = zone_pct.get("Z5", 0)
        if has_intervals and z5 > 10:
            return "VO2 Max Intervals"
        elif has_intervals and z4 > 15:
            return "Threshold Intervals"
        elif variability_index > 1.15:
            return "Group Ride"
        else:
            return "Threshold Ride"

    elif intensity_factor < 1.0:
        z5 = zone_pct.get("Z5", 0)
        z6 = zone_pct.get("Z6", 0)
        if has_intervals and z5 > 15:
            return "VO2 Max Intervals"
        elif z6 > 10:
            return "Anaerobic Intervals"
        elif variability_index > 1.2:
            return "Race / Hard Group Ride"
        else:
            return "Hard Threshold"

    else:  # IF >= 1.0
        z6_z7 = zone_pct.get("Z6", 0) + zone_pct.get("Z7", 0)
        if duration_minutes < 45:
            if z6_z7 > 20:
                return "Sprint Training"
            return "Time Trial Effort"
        elif variability_index > 1.3:
            return "Race"
        else:
            return "Race / TT Effort"


def _zone_percentages(power_samples: list, ftp: int) -> dict[str, float]:
    """Calculate percentage of time in each power zone."""
    zones = {
        "Z1": (0, 0.55),
        "Z2": (0.56, 0.75),
        "Z3": (0.76, 0.90),
        "Z4": (0.91, 1.05),
        "Z5": (1.06, 1.20),
        "Z6": (1.21, 1.50),
        "Z7": (1.51, 5.00),
    }

    counts = {z: 0 for z in zones}
    total = 0

    for p in power_samples:
        if p is None or p < 0:
            continue
        total += 1
        ratio = p / ftp if ftp > 0 else 0
        for zone, (low, high) in zones.items():
            if low <= ratio <= high:
                counts[zone] += 1
                break
        else:
            if ratio > 5.0:
                counts["Z7"] += 1

    if total == 0:
        return {z: 0.0 for z in zones}

    return {z: (c / total) * 100 for z, c in counts.items()}


def _detect_intervals(power_samples: list, ftp: int) -> bool:
    """
    Detect if a ride contains structured intervals by looking for
    repeated transitions between high and low power zones.

    Returns True if at least 3 work/rest transitions are found.
    """
    if not power_samples or ftp <= 0:
        return False

    # Use 30-second smoothing to avoid noise
    window = min(30, len(power_samples) // 4)
    if window < 5:
        return False

    smoothed = []
    for i in range(len(power_samples) - window + 1):
        chunk = [p for p in power_samples[i:i + window] if p is not None and p >= 0]
        if chunk:
            smoothed.append(sum(chunk) / len(chunk))

    if not smoothed:
        return False

    # Classify each smoothed point as "work" (above 85% FTP) or "rest" (below 70% FTP)
    work_threshold = ftp * 0.85
    rest_threshold = ftp * 0.70

    in_work = False
    transitions = 0

    for power in smoothed:
        if not in_work and power >= work_threshold:
            in_work = True
            transitions += 1
        elif in_work and power < rest_threshold:
            in_work = False

    return transitions >= 3


def _date_name(ride_date: datetime | None) -> str:
    """Generate a name based on the ride date and time of day."""
    if ride_date is None:
        return "Ride"

    hour = ride_date.hour
    day = ride_date.strftime("%a %d %b")

    if hour < 6:
        return f"Early Morning Ride, {day}"
    elif hour < 10:
        return f"Morning Ride, {day}"
    elif hour < 14:
        return f"Midday Ride, {day}"
    elif hour < 17:
        return f"Afternoon Ride, {day}"
    else:
        return f"Evening Ride, {day}"
