"""GPX and TCX parsing into the same shape parse_fit_file produces.

Both return {"summary": {}, "records": [...], "laps": [], "start_time": dt}
so create_ride_from_parsed can treat every file format identically. GPX and
TCX carry coordinates in degrees already, so records use "latitude" /
"longitude" keys (the FIT path uses semicircle "position_lat/long" keys and
converts at insert time).
"""

import math
import xml.etree.ElementTree as ET
from datetime import datetime, timezone


def _parse_time(value: str) -> datetime | None:
    """ISO-8601 timestamps as GPX/TCX write them, always returned tz-aware."""
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _find_text_local(elem, name: str) -> str | None:
    """First descendant whose local (namespace-stripped) tag matches."""
    for child in elem.iter():
        if _localname(child.tag) == name:
            return child.text
    return None


def _finalize(records: list[dict], start_time: datetime | None) -> dict:
    """Fill elapsed_seconds, derive distance/speed from GPS when absent."""
    have_distance = any(r.get("distance") is not None for r in records)
    cumulative = 0.0
    prev = None
    for rec in records:
        ts = rec.get("timestamp")
        if start_time and isinstance(ts, datetime):
            rec["elapsed_seconds"] = int((ts - start_time).total_seconds())

        lat, lon = rec.get("latitude"), rec.get("longitude")
        if not have_distance and lat is not None and lon is not None:
            if prev is not None:
                step = _haversine_m(prev[0], prev[1], lat, lon)
                cumulative += step
                if rec.get("speed") is None and isinstance(ts, datetime) and prev[2]:
                    dt = (ts - prev[2]).total_seconds()
                    if dt > 0:
                        rec["speed"] = step / dt
            rec["distance"] = cumulative
            prev = (lat, lon, ts if isinstance(ts, datetime) else None)
    return {"summary": {}, "records": records, "laps": [], "start_time": start_time}


def parse_gpx(file_bytes: bytes) -> dict:
    """GPX 1.1 track with Garmin TrackPointExtension (hr/cad/atemp) and the
    common power spellings (<power>, <pwr:PowerInWatts>)."""
    root = ET.fromstring(file_bytes)
    records: list[dict] = []
    start_time: datetime | None = None

    for trkpt in root.iter():
        if _localname(trkpt.tag) != "trkpt":
            continue
        rec: dict = {}
        try:
            rec["latitude"] = float(trkpt.attrib["lat"])
            rec["longitude"] = float(trkpt.attrib["lon"])
        except (KeyError, ValueError):
            pass

        for child in trkpt:
            name = _localname(child.tag)
            if name == "ele" and child.text:
                rec["altitude"] = float(child.text)
            elif name == "time" and child.text:
                ts = _parse_time(child.text)
                if ts:
                    rec["timestamp"] = ts
                    if start_time is None:
                        start_time = ts
            elif name == "extensions":
                for ext in child.iter():
                    ext_name = _localname(ext.tag)
                    if ext.text is None:
                        continue
                    text = ext.text.strip()
                    if not text:
                        continue
                    if ext_name in ("hr", "heartrate"):
                        rec["heart_rate"] = int(float(text))
                    elif ext_name in ("cad", "cadence"):
                        rec["cadence"] = int(float(text))
                    elif ext_name in ("power", "powerinwatts", "watts"):
                        rec["power"] = int(float(text))
                    elif ext_name == "atemp":
                        rec["temperature"] = int(float(text))
                    elif ext_name == "speed":
                        rec["speed"] = float(text)
                    elif ext_name in ("distance", "dist"):
                        rec["distance"] = float(text)
        if rec:
            records.append(rec)

    return _finalize(records, start_time)


def parse_tcx(file_bytes: bytes) -> dict:
    """TCX Trackpoints with the ActivityExtension Watts/Speed fields."""
    root = ET.fromstring(file_bytes)
    records: list[dict] = []
    start_time: datetime | None = None

    for tp in root.iter():
        if _localname(tp.tag) != "trackpoint":
            continue
        rec: dict = {}
        for child in tp:
            name = _localname(child.tag)
            if name == "time" and child.text:
                ts = _parse_time(child.text)
                if ts:
                    rec["timestamp"] = ts
                    if start_time is None:
                        start_time = ts
            elif name == "position":
                lat = _find_text_local(child, "latitudedegrees")
                lon = _find_text_local(child, "longitudedegrees")
                if lat and lon:
                    rec["latitude"] = float(lat)
                    rec["longitude"] = float(lon)
            elif name == "altitudemeters" and child.text:
                rec["altitude"] = float(child.text)
            elif name == "distancemeters" and child.text:
                rec["distance"] = float(child.text)
            elif name == "heartratebpm":
                value = _find_text_local(child, "value")
                if value:
                    rec["heart_rate"] = int(float(value))
            elif name == "cadence" and child.text:
                rec["cadence"] = int(float(child.text))
            elif name == "extensions":
                watts = _find_text_local(child, "watts")
                if watts:
                    rec["power"] = int(float(watts))
                speed = _find_text_local(child, "speed")
                if speed:
                    rec["speed"] = float(speed)
        if rec:
            records.append(rec)

    return _finalize(records, start_time)
