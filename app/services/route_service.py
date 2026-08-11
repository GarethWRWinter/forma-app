import logging
import math
import re
import xml.etree.ElementTree as ET

import httpx

from app.database import SessionLocal
from app.models.onboarding import GoalEvent

logger = logging.getLogger(__name__)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in metres between two points."""
    R = 6_371_000  # Earth radius in metres
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _extract_strava_stats(
    html: str, description: str | None, result: dict
) -> None:
    """
    Try to extract distance and elevation stats from a Strava page.

    Strava embeds stats in several places:
    - og:description often contains "X.X km" and "Xm elevation gain"
    - The HTML body contains stat sections with distance/elevation
    - JSON-LD or embedded data in script tags
    """
    text = (description or "") + " " + html

    # --- Distance ---
    # Try patterns like "6.2 km", "6.2km", "3.8 mi"
    dist_km = None
    km_match = re.search(r'(\d+(?:\.\d+)?)\s*km', text, re.IGNORECASE)
    if km_match:
        dist_km = float(km_match.group(1))
    else:
        mi_match = re.search(r'(\d+(?:\.\d+)?)\s*mi(?:le)?s?', text, re.IGNORECASE)
        if mi_match:
            dist_km = round(float(mi_match.group(1)) * 1.60934, 2)

    if dist_km and dist_km > 0:
        result["total_distance_km"] = dist_km

    # --- Elevation gain ---
    # Try patterns like "143m climbing", "143m elevation", "143 m"
    elev_m = None
    # "Xm climbing" or "Xm of climbing" or "Xm elevation gain"
    elev_match = re.search(
        r'(\d+(?:,\d+)?)\s*m\s+(?:of\s+)?(?:climbing|elevation|elev)',
        text,
        re.IGNORECASE,
    )
    if elev_match:
        elev_m = float(elev_match.group(1).replace(",", ""))
    else:
        # "elevation gain: Xm" or "elev gain Xm"
        elev_match2 = re.search(
            r'(?:climbing|elevation\s*gain|elev\.?\s*gain)[:\s]+(\d+(?:,\d+)?)\s*m',
            text,
            re.IGNORECASE,
        )
        if elev_match2:
            elev_m = float(elev_match2.group(1).replace(",", ""))
        else:
            # "Xft climbing" or "Xft elevation"
            ft_match = re.search(
                r'(\d+(?:,\d+)?)\s*ft\s+(?:of\s+)?(?:climbing|elevation|elev)',
                text,
                re.IGNORECASE,
            )
            if ft_match:
                elev_m = round(float(ft_match.group(1).replace(",", "")) * 0.3048, 1)

    if elev_m and elev_m > 0:
        result["elevation_gain_m"] = elev_m

    # --- Min/Max elevation from HTML stat blocks ---
    # Strava sometimes has "lowest point Xm" / "highest point Xm"
    low_match = re.search(
        r'(?:lowest|min)\s*(?:point|elev)[:\s]+(\d+(?:,\d+)?)\s*m',
        text,
        re.IGNORECASE,
    )
    high_match = re.search(
        r'(?:highest|max)\s*(?:point|elev)[:\s]+(\d+(?:,\d+)?)\s*m',
        text,
        re.IGNORECASE,
    )
    if low_match:
        result["min_elevation_m"] = float(low_match.group(1).replace(",", ""))
    if high_match:
        result["max_elevation_m"] = float(high_match.group(1).replace(",", ""))

    # --- Average gradient ---
    grade_match = re.search(
        r'(?:avg|average)\s*(?:grade|gradient)[:\s]+(\d+(?:\.\d+)?)\s*%',
        text,
        re.IGNORECASE,
    )
    if grade_match:
        result["avg_gradient_pct"] = float(grade_match.group(1))


def fetch_route_data(url: str) -> dict:
    """Fetch a URL and extract route information from its HTML."""
    try:
        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            response = client.get(url)
            response.raise_for_status()
            html = response.text

        if "strava.com/routes/" in url or "strava.com/segments/" in url:
            route_type = "route" if "strava.com/routes/" in url else "segment"

            title = None
            title_match = re.search(
                r'<meta\s+(?:property|name)=["\']og:title["\']\s+content=["\']([^"\']*)["\']',
                html,
                re.IGNORECASE,
            )
            if not title_match:
                title_match = re.search(
                    r'content=["\']([^"\']*)["\'][\s]+(?:property|name)=["\']og:title["\']',
                    html,
                    re.IGNORECASE,
                )
            if title_match:
                title = title_match.group(1)

            description = None
            desc_match = re.search(
                r'<meta\s+(?:property|name)=["\']og:description["\']\s+content=["\']([^"\']*)["\']',
                html,
                re.IGNORECASE,
            )
            if not desc_match:
                desc_match = re.search(
                    r'content=["\']([^"\']*)["\'][\s]+(?:property|name)=["\']og:description["\']',
                    html,
                    re.IGNORECASE,
                )
            if desc_match:
                description = desc_match.group(1)

            result: dict = {
                "source": "strava",
                "url": url,
                "title": title,
                "description": description,
                "type": route_type,
            }

            # Extract distance and elevation from page content
            _extract_strava_stats(html, description, result)

            return result

        # Generic website
        title = None
        title_tag_match = re.search(
            r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL
        )
        if title_tag_match:
            title = title_tag_match.group(1).strip()

        description = None
        meta_desc_match = re.search(
            r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']*)["\']',
            html,
            re.IGNORECASE,
        )
        if not meta_desc_match:
            meta_desc_match = re.search(
                r'content=["\']([^"\']*)["\'][\s]+name=["\']description["\']',
                html,
                re.IGNORECASE,
            )
        if meta_desc_match:
            description = meta_desc_match.group(1)

        return {
            "source": "website",
            "url": url,
            "title": title,
            "description": description,
        }

    except Exception as e:
        logger.error("Error fetching route data from %s: %s", url, e)
        return {"source": "unknown", "url": url, "error": str(e)}


def fetch_route_data_bg(goal_id: str, url: str) -> None:
    """Background task: fetch route data from a URL and persist it on the GoalEvent."""
    db = SessionLocal()
    try:
        data = fetch_route_data(url)
        goal_event = db.query(GoalEvent).filter(GoalEvent.id == goal_id).first()
        if goal_event:
            goal_event.route_data = data
            db.commit()
            logger.info("Saved route data for goal %s from %s", goal_id, url)
        else:
            logger.warning("GoalEvent %s not found when saving route data", goal_id)
    except Exception as e:
        logger.error("Error in fetch_route_data_bg for goal %s: %s", goal_id, e)
        db.rollback()
    finally:
        db.close()


def parse_gpx_route_data(filepath: str) -> dict:
    """Parse a GPX file and extract route statistics + elevation profile."""
    ns = {"gpx": "http://www.topografix.com/GPX/1/1"}

    tree = ET.parse(filepath)
    root = tree.getroot()

    # Extract track name
    name_el = root.find(".//gpx:trk/gpx:name", ns)
    name = name_el.text if name_el is not None and name_el.text else None

    # Gather all trackpoints across all track segments
    trackpoints: list[tuple[float, float, float | None]] = []
    for trkpt in root.findall(".//gpx:trk/gpx:trkseg/gpx:trkpt", ns):
        lat = float(trkpt.attrib["lat"])
        lon = float(trkpt.attrib["lon"])
        ele_el = trkpt.find("gpx:ele", ns)
        ele = float(ele_el.text) if ele_el is not None and ele_el.text else None
        trackpoints.append((lat, lon, ele))

    total_distance_m = 0.0
    elevation_gain_m = 0.0
    elevations: list[float] = []

    # Build raw distance-elevation pairs for profile chart
    raw_profile: list[tuple[float, float | None]] = []  # (cumulative_distance_m, ele)
    if trackpoints:
        raw_profile.append((0.0, trackpoints[0][2]))

    for i in range(len(trackpoints)):
        lat, lon, ele = trackpoints[i]
        if ele is not None:
            elevations.append(ele)

        if i > 0:
            prev_lat, prev_lon, prev_ele = trackpoints[i - 1]
            seg_dist = _haversine(prev_lat, prev_lon, lat, lon)
            total_distance_m += seg_dist
            raw_profile.append((total_distance_m, ele))

            if ele is not None and prev_ele is not None:
                diff = ele - prev_ele
                if diff > 0:
                    elevation_gain_m += diff

    min_elevation = min(elevations) if elevations else None
    max_elevation = max(elevations) if elevations else None

    start_coord = (
        {"lat": trackpoints[0][0], "lon": trackpoints[0][1]}
        if trackpoints
        else None
    )
    end_coord = (
        {"lat": trackpoints[-1][0], "lon": trackpoints[-1][1]}
        if trackpoints
        else None
    )

    # Build sampled elevation profile for chart display
    # Target ~300-500 points max to keep JSON manageable
    elevation_profile = _build_elevation_profile(raw_profile, total_distance_m)

    # Downsampled coordinate track (persists in the DB, unlike the uploaded
    # file on ephemeral disk). Feeds the goal-day wind map and per-segment
    # briefing analysis: [[lat, lon, cumulative_km], ...], <=240 points.
    track: list[list[float]] = []
    if trackpoints:
        step = max(1, len(trackpoints) // 240)
        cumulative = 0.0
        for i in range(len(trackpoints)):
            if i > 0:
                cumulative += _haversine(
                    trackpoints[i - 1][0], trackpoints[i - 1][1],
                    trackpoints[i][0], trackpoints[i][1],
                )
            if i % step == 0 or i == len(trackpoints) - 1:
                track.append([
                    round(trackpoints[i][0], 5),
                    round(trackpoints[i][1], 5),
                    round(cumulative / 1000, 2),
                ])

    return {
        "source": "gpx",
        "name": name,
        "track": track,
        "total_distance_km": round(total_distance_m / 1000, 2),
        "elevation_gain_m": round(elevation_gain_m, 1),
        "min_elevation_m": round(min_elevation, 1) if min_elevation is not None else None,
        "max_elevation_m": round(max_elevation, 1) if max_elevation is not None else None,
        "trackpoints": len(trackpoints),
        "start": start_coord,
        "end": end_coord,
        "elevation_profile": elevation_profile,
    }


def _build_elevation_profile(
    raw_profile: list[tuple[float, float | None]],
    total_distance_m: float,
) -> list[dict]:
    """
    Sample the raw distance-elevation pairs at regular intervals
    to produce a chart-friendly elevation profile.

    Returns list of {"distance_km": float, "elevation_m": float, "gradient_pct": float}.
    Targets ~300-500 points maximum.
    """
    if not raw_profile or total_distance_m <= 0:
        return []

    # Filter out points with no elevation data
    valid = [(d, e) for d, e in raw_profile if e is not None]
    if len(valid) < 2:
        return []

    # Determine sample interval: target ~400 points
    max_points = 400
    sample_interval_m = max(total_distance_m / max_points, 50.0)  # at least 50m apart

    profile: list[dict] = []
    prev_dist = 0.0
    prev_ele = valid[0][1]
    # Always include the first point
    profile.append({
        "distance_km": 0.0,
        "elevation_m": round(valid[0][1], 1),
        "gradient_pct": 0.0,
    })

    last_sampled_dist = 0.0
    for dist_m, ele in valid[1:]:
        if dist_m - last_sampled_dist >= sample_interval_m:
            dist_km = round(dist_m / 1000, 3)
            ele_rounded = round(ele, 1)

            # Calculate gradient from last sampled point
            horiz = dist_m - prev_dist
            gradient = 0.0
            if horiz > 0 and prev_ele is not None:
                gradient = round(((ele - prev_ele) / horiz) * 100, 1)

            profile.append({
                "distance_km": dist_km,
                "elevation_m": ele_rounded,
                "gradient_pct": gradient,
            })

            prev_dist = dist_m
            prev_ele = ele
            last_sampled_dist = dist_m

    # Always include the last point
    last_d, last_e = valid[-1]
    if last_d > last_sampled_dist:
        horiz = last_d - prev_dist
        gradient = 0.0
        if horiz > 0 and prev_ele is not None:
            gradient = round(((last_e - prev_ele) / horiz) * 100, 1)
        profile.append({
            "distance_km": round(last_d / 1000, 3),
            "elevation_m": round(last_e, 1),
            "gradient_pct": gradient,
        })

    return profile


def parse_gpx_route_data_bg(goal_id: str, filepath: str) -> None:
    """Background task: parse a GPX file and persist route data on the GoalEvent."""
    db = SessionLocal()
    try:
        data = parse_gpx_route_data(filepath)
        goal_event = db.query(GoalEvent).filter(GoalEvent.id == goal_id).first()
        if goal_event:
            goal_event.route_data = data
            db.commit()
            logger.info("Saved GPX route data for goal %s from %s", goal_id, filepath)
        else:
            logger.warning("GoalEvent %s not found when saving GPX route data", goal_id)
    except Exception as e:
        logger.error("Error in parse_gpx_route_data_bg for goal %s: %s", goal_id, e)
        db.rollback()
    finally:
        db.close()
