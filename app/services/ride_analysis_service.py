"""Deep ride analysis: what the coach sees when it actually opens the file.

The coach's normal context carries ride-level aggregates only (NP, IF, TSS,
averages). Those describe the whole ride and nothing smaller, which is how a
26-minute ride's IF of 0.87 once got used to describe a 2:40 climb inside it.

This module reads the per-second streams and computes the things a real
coach would look at: the power curve, where the peaks actually happened, the
climbs with their gradients and the power held on each, honest time in zone,
and whether the rider faded. Everything is computed in Python and returned as
a few dozen numbers, so the model reasons over facts rather than raw samples.

Results are cached on the ride row: opening the same file twice is free.
"""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.formulas import best_efforts, normalized_power, time_in_zones
from app.models.ride import Ride, RideData
from app.models.user import User

logger = logging.getLogger(__name__)

# The durations a coach actually talks in. Sprint through to a long climb.
CURVE_DURATIONS = [5, 15, 30, 60, 120, 300, 480, 1200, 3600]

# A climb worth naming: sustained, not a motorway flyover.
MIN_CLIMB_METRES = 25
MIN_CLIMB_GRADIENT = 2.5


def _samples(db: Session, ride_id: str) -> list[RideData]:
    return (
        db.query(RideData)
        .filter(RideData.ride_id == ride_id)
        .order_by(RideData.elapsed_seconds.asc())
        .all()
    )


def _fmt_clock(seconds: float | int | None) -> str | None:
    """Elapsed seconds as m:ss, so the coach can say where it happened."""
    if seconds is None:
        return None
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _peak_efforts(power: list[int], elapsed: list[int]) -> list[dict]:
    """Best average power per duration, plus WHERE each one started.

    best_efforts() gives the value; the coach also needs the location, so the
    window is re-scanned to find the offset that produced it.
    """
    usable = [d for d in CURVE_DURATIONS if len(power) >= d]
    if not usable:
        return []
    values = best_efforts(power, usable)

    out: list[dict] = []
    for dur in usable:
        target = values.get(dur, 0.0)
        if not target:
            continue
        # Re-scan for the window that produced the best average.
        window = sum(power[:dur])
        best_at, best_val = 0, window / dur
        for i in range(1, len(power) - dur + 1):
            window = window - power[i - 1] + power[i + dur - 1]
            avg = window / dur
            if avg > best_val:
                best_val, best_at = avg, i
        out.append(
            {
                "duration_s": dur,
                "watts": round(best_val),
                "started_at": _fmt_clock(
                    elapsed[best_at] if best_at < len(elapsed) else best_at
                ),
            }
        )
    return out


def _climbs(
    altitude: list[float], distance: list[float], power: list[int], elapsed: list[int]
) -> list[dict]:
    """Find sustained climbs and report what was actually held on each.

    Deliberately simple: walk the altitude stream, open a climb when the road
    tips up, close it when it stops going up for a sustained stretch. Good
    enough to talk about honestly, and it never invents a gradient.
    """
    if not altitude or not distance or len(altitude) != len(distance):
        return []

    climbs: list[dict] = []
    start = None
    descending_for = 0

    for i in range(1, len(altitude)):
        rising = altitude[i] > altitude[i - 1]
        if rising and start is None:
            start = i - 1
            descending_for = 0
        elif start is not None:
            if rising:
                descending_for = 0
            else:
                descending_for += 1
                # 30s of not-climbing closes the climb.
                if descending_for > 30:
                    end = i - descending_for
                    gain = altitude[end] - altitude[start]
                    length = distance[end] - distance[start]
                    if gain >= MIN_CLIMB_METRES and length > 0:
                        gradient = (gain / length) * 100
                        if gradient >= MIN_CLIMB_GRADIENT:
                            seg = [p for p in power[start:end] if p is not None]
                            dur = (
                                elapsed[end] - elapsed[start]
                                if end < len(elapsed)
                                else end - start
                            )
                            climbs.append(
                                {
                                    "started_at": _fmt_clock(elapsed[start]),
                                    "length_km": round(length / 1000, 2),
                                    "gain_m": round(gain),
                                    "avg_gradient_pct": round(gradient, 1),
                                    "duration": _fmt_clock(dur),
                                    "avg_watts": round(sum(seg) / len(seg))
                                    if seg
                                    else None,
                                    "max_watts": max(seg) if seg else None,
                                }
                            )
                    start = None
                    descending_for = 0

    # Biggest first, and never drown the coach in flyovers.
    climbs.sort(key=lambda c: c["gain_m"], reverse=True)
    return climbs[:6]


def _fade(power: list[int], ftp: int | None) -> dict | None:
    """Did the rider hold it together? First third against last third."""
    if len(power) < 900:  # under 15 minutes, fade is meaningless
        return None
    third = len(power) // 3
    first, last = power[:third], power[-third:]
    if not first or not last:
        return None
    np_first = normalized_power(first)
    np_last = normalized_power(last)
    if not np_first:
        return None
    change = ((np_last - np_first) / np_first) * 100
    return {
        "first_third_np": round(np_first),
        "last_third_np": round(np_last),
        "change_pct": round(change, 1),
        "verdict": (
            "faded" if change <= -8 else "negative split" if change >= 8 else "even"
        ),
    }


def analyse_ride(db: Session, user: User, ride: Ride, refresh: bool = False) -> dict:
    """Open the ride file properly. Cached on the ride row after the first read."""
    if ride.analysis and not refresh:
        return ride.analysis

    rows = _samples(db, ride.id)
    if not rows:
        return {
            "available": False,
            "reason": (
                "No per-second data stored for this ride, only the summary. "
                "Say so plainly and ask the rider for what is missing."
            ),
        }

    power = [int(r.power) for r in rows if r.power is not None]
    elapsed = [int(r.elapsed_seconds or 0) for r in rows]
    altitude = [float(r.altitude) for r in rows if r.altitude is not None]
    distance = [float(r.distance) for r in rows if r.distance is not None]
    hr = [int(r.heart_rate) for r in rows if r.heart_rate is not None]
    cadence = [int(r.cadence) for r in rows if r.cadence is not None]

    ftp = ride.ftp_at_time or user.ftp
    result: dict = {
        "available": True,
        "samples": len(rows),
        "ftp_used": ftp,
        "has_power": bool(power),
        "has_gps": any(r.latitude is not None for r in rows),
    }

    if power:
        result["power_curve"] = _peak_efforts(power, elapsed)
        result["fade"] = _fade(power, ftp)
        if ftp:
            zones = time_in_zones(power, ftp)
            result["time_in_zones_min"] = {
                z: round(secs / 60, 1) for z, secs in zones.items() if secs
            }
            # Every peak expressed against THIS rider's FTP, so the coach
            # never has to reach for a ride-level number to describe one.
            for effort in result["power_curve"]:
                effort["pct_ftp"] = round((effort["watts"] / ftp) * 100)

    if altitude and distance:
        result["climbs"] = _climbs(altitude, distance, power, elapsed)

    if hr:
        result["hr"] = {
            "avg": round(sum(hr) / len(hr)),
            "max": max(hr),
        }
    if cadence:
        spinning = [c for c in cadence if c > 0]
        if spinning:
            result["cadence"] = {"avg": round(sum(spinning) / len(spinning))}

    result["computed_at"] = datetime.utcnow().isoformat() + "Z"

    ride.analysis = result
    db.commit()
    return result
