"""
Strava OAuth 2.0 integration and activity sync.

Flow:
1. User clicks "Connect Strava" -> redirected to Strava auth page
2. User authorizes -> Strava redirects back with auth code
3. We exchange code for access/refresh tokens
4. We use access token to fetch activities and streams
5. Tokens auto-refresh (6-hour expiry)
6. Webhook receives new activity notifications for real-time sync
7. Historical backfill imports full ride history on first connect
"""

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models.integration import StravaToken
from app.models.ride import Ride, RideData, RideSource
from app.models.segment import SegmentEffort, StravaSegment
from app.models.user import User
from app.core.formulas import (
    intensity_factor,
    normalized_power,
    training_stress_score,
    variability_index,
)
from app.services.dedup_service import find_dropbox_duplicate

logger = logging.getLogger(__name__)

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = "https://www.strava.com/api/v3"

# TODO(2027-06-01): Strava Developer Program migration deadline.
#   - Update STRAVA_API_BASE to "https://www.api-v3.strava.com" (one-line change).
#   - Verify all calls keep using the Authorization: Bearer header pattern (already correct).
#   - Replace the (unused) oauth/deauthorize flow with POST https://www.strava.com/oauth/revoke
#     {client_id, client_secret, access_token} — add to disconnect() below for clean revocation.
#   - Background: Strava's May 2026 Developer Program changes. We are Standard Tier with a
#     direct OAuth integration (no intermediary broker), so the June 2026 intermediary
#     restriction does not affect us. Required: developer-account Strava subscription
#     from 1 June 2026 (or 30 June 2026 for existing devs). Plan to apply for Extended
#     Access Tier at ≥25 active users to lift rate limits and drop subscription requirement.


class StravaRateLimit(Exception):
    """
    Raised when Strava returns 429. Carries enough info to decide how long
    to wait. Strava response headers used:
      X-ReadRateLimit-Limit: "100,1000"     (short, long)
      X-ReadRateLimit-Usage: "101,250"      (short, long)
    Short window = 15 minutes. Long window = daily (resets at midnight UTC).
    """

    def __init__(self, is_daily: bool, wait_secs: int, usage: str | None = None):
        self.is_daily = is_daily
        self.wait_secs = wait_secs
        self.usage = usage
        super().__init__(
            f"Strava rate limit hit ({'daily' if is_daily else '15-min'}), "
            f"wait {wait_secs}s. Usage: {usage}"
        )


def _parse_rate_limit_response(response) -> StravaRateLimit:
    """
    Inspect a 429 response and return a StravaRateLimit exception with
    the appropriate wait time based on which limit was hit.
    """
    from datetime import datetime, timezone, timedelta

    limit_header = response.headers.get("X-ReadRateLimit-Limit") or response.headers.get("X-RateLimit-Limit", "100,1000")
    usage_header = response.headers.get("X-ReadRateLimit-Usage") or response.headers.get("X-RateLimit-Usage", "")

    try:
        short_limit, long_limit = [int(x) for x in limit_header.split(",")]
    except Exception:
        short_limit, long_limit = 100, 1000

    short_usage, long_usage = 0, 0
    try:
        parts = usage_header.split(",")
        if len(parts) >= 2:
            short_usage = int(parts[0])
            long_usage = int(parts[1])
    except Exception:
        pass

    # Daily limit hit — wait until midnight UTC (+ 60s buffer)
    if long_usage >= long_limit:
        now = datetime.now(timezone.utc)
        midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        wait_secs = int((midnight - now).total_seconds()) + 60
        return StravaRateLimit(is_daily=True, wait_secs=wait_secs, usage=usage_header)

    # 15-min limit hit — wait 16 min to ensure the rolling window clears
    return StravaRateLimit(is_daily=False, wait_secs=16 * 60, usage=usage_header)


def get_auth_url(state: str = "") -> str:
    """Generate Strava OAuth authorization URL with user ID in state."""
    params = {
        "client_id": settings.strava_client_id,
        "redirect_uri": settings.strava_redirect_uri,
        "response_type": "code",
        "scope": "read,activity:read_all",
        "approval_prompt": "force",
        "state": state,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{STRAVA_AUTH_URL}?{query}"


def scope_has_activity_read(scope: str | None) -> bool:
    """True if the granted scope lets us read the athlete's activities.

    Strava grants activity access only when the user ticks
    "View data about your activities" on the consent screen. Without it
    the token is `read` only and every /athlete/activities call 403s.
    """
    if not scope:
        return False
    parts = {p.strip() for p in scope.replace(" ", ",").split(",")}
    return "activity:read_all" in parts or "activity:read" in parts


async def exchange_code(
    db: Session, user_id: str, code: str, granted_scope: str | None = None
) -> StravaToken:
    """Exchange authorization code for access/refresh tokens.

    `granted_scope` is the scope Strava actually granted, passed through from
    the callback query string. We store it verbatim (never assume full scope)
    so the connection state reflects what the user truly authorized.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(STRAVA_TOKEN_URL, data={
            "client_id": settings.strava_client_id,
            "client_secret": settings.strava_client_secret,
            "code": code,
            "grant_type": "authorization_code",
        })
        response.raise_for_status()
        data = response.json()

    # Strava echoes the granted scope in the exchange response too; prefer the
    # callback value, fall back to the response, and only then to the request.
    scope = granted_scope or data.get("scope") or "read,activity:read_all"

    # Upsert token
    existing = db.query(StravaToken).filter(StravaToken.user_id == user_id).first()
    if existing:
        existing.access_token = data["access_token"]
        existing.refresh_token = data["refresh_token"]
        existing.expires_at = datetime.fromtimestamp(data["expires_at"], tz=timezone.utc)
        existing.athlete_id = data["athlete"]["id"]
        existing.scope = scope
        db.commit()
        db.refresh(existing)
        return existing

    token = StravaToken(
        user_id=user_id,
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_at=datetime.fromtimestamp(data["expires_at"], tz=timezone.utc),
        athlete_id=data["athlete"]["id"],
        scope=scope,
    )
    db.add(token)
    db.commit()
    db.refresh(token)
    return token


async def _refresh_token_if_needed(db: Session, strava_token: StravaToken) -> str:
    """Refresh access token if expired. Returns valid access token."""
    expires = strava_token.expires_at.replace(tzinfo=timezone.utc) if strava_token.expires_at and strava_token.expires_at.tzinfo is None else strava_token.expires_at
    if expires and expires > datetime.now(timezone.utc):
        return strava_token.access_token

    async with httpx.AsyncClient() as client:
        response = await client.post(STRAVA_TOKEN_URL, data={
            "client_id": settings.strava_client_id,
            "client_secret": settings.strava_client_secret,
            "refresh_token": strava_token.refresh_token,
            "grant_type": "refresh_token",
        })
        response.raise_for_status()
        data = response.json()

    strava_token.access_token = data["access_token"]
    strava_token.refresh_token = data["refresh_token"]
    strava_token.expires_at = datetime.fromtimestamp(data["expires_at"], tz=timezone.utc)
    db.commit()

    return strava_token.access_token


async def sync_activities(
    db: Session, user: User, limit: int = 30
) -> list[Ride]:
    """
    Fetch recent activities from Strava and create/update rides.

    Gets activity summaries first, then fetches streams (power/HR/cadence)
    for each activity.
    """
    strava_token = db.query(StravaToken).filter(StravaToken.user_id == user.id).first()
    if not strava_token:
        raise ValueError("Strava not connected")

    access_token = await _refresh_token_if_needed(db, strava_token)

    # Fetch activities
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {access_token}"}

        # Get activities list
        params = {"per_page": limit}
        if strava_token.last_sync_at:
            params["after"] = int(strava_token.last_sync_at.timestamp())

        response = await client.get(
            f"{STRAVA_API_BASE}/athlete/activities",
            headers=headers,
            params=params,
        )
        if response.status_code == 403:
            # Token lacks activity access. Log Strava's exact refusal — a stale
            # prior authorization can shadow a fresh grant, and the error body
            # tells us which permission Strava thinks is missing.
            logger.error(
                "Strava 403 on /athlete/activities (stored scope=%r): %s",
                strava_token.scope, response.text[:500],
            )
            raise ValueError(
                "Strava hasn't granted activity access. Revoke “Cycling Coach” at "
                "strava.com → Settings → My Apps, then reconnect and tick "
                "“View data about your activities”."
            )
        response.raise_for_status()
        activities = response.json()

    synced_rides = []
    for activity in activities:
        # Skip non-ride activities
        if activity.get("type") not in ("Ride", "VirtualRide"):
            continue

        # Check if already synced
        external_id = str(activity["id"])
        existing = db.query(Ride).filter(
            Ride.user_id == user.id,
            Ride.external_id == external_id,
        ).first()
        if existing:
            continue

        # Skip if a Dropbox ride already covers this activity (richer data)
        start_time = datetime.fromisoformat(
            activity["start_date"].replace("Z", "+00:00")
        )
        dropbox_match = find_dropbox_duplicate(
            db, user.id, start_time, activity.get("elapsed_time"),
        )
        if dropbox_match:
            # Link the Strava activity ID onto the Dropbox ride for reference
            if not dropbox_match.strava_activity_id:
                dropbox_match.strava_activity_id = external_id
            # Fetch segments for the Dropbox ride
            try:
                await fetch_and_store_segments(
                    db, dropbox_match.id, external_id, access_token,
                )
            except Exception:
                logger.warning("Failed to fetch segments for Dropbox ride %s", dropbox_match.id)
            logger.info(
                "Skipping Strava activity %s — Dropbox ride %s already exists",
                external_id, dropbox_match.id,
            )
            continue

        # Create ride from activity summary
        ride = _activity_to_ride(user, activity)
        db.add(ride)
        db.flush()

        # Fetch streams for detailed data
        try:
            streams = await _fetch_activity_streams(
                access_token, activity["id"]
            )
            if streams:
                _create_ride_data_from_streams(db, ride.id, streams)

                # Recalculate NP/IF/TSS from actual power data
                power_stream = streams.get("watts", {}).get("data", [])
                if power_stream:
                    _recalculate_ride_metrics(db, ride, power_stream, user.ftp)
        except Exception:
            # Streams may not be available for all activities
            pass

        # Fetch segment efforts and achievements
        try:
            await fetch_and_store_segments(
                db, ride.id, str(activity["id"]), access_token,
            )
        except Exception:
            logger.warning("Failed to fetch segments for activity %s", activity["id"])

        synced_rides.append(ride)

    # Update last sync time
    strava_token.last_sync_at = datetime.now(timezone.utc)
    db.commit()

    # Try to auto-link each new ride to a planned workout on the same day.
    # Done after the main commit so partial failures don't roll back the sync.
    try:
        from app.services.workout_assessment_service import auto_link_ride_to_workout
        for r in synced_rides:
            try:
                auto_link_ride_to_workout(db, r)
            except Exception:
                logger.exception("Failed to auto-link ride %s to a workout", r.id)
    except Exception:
        logger.exception("Auto-link module failed to load")

    # Auto-generate Coach Forma debriefs for each new ride (fire-and-forget).
    try:
        from app.services.coach_insights_service import generate_ride_debrief
        for r in synced_rides:
            try:
                # NB: synchronous — `await`ing it was the bug that silently
                # killed every Strava-ride debrief (same as audit finding #4).
                generate_ride_debrief(db, user, r)
            except Exception:
                logger.exception("Failed to generate debrief for ride %s", r.id)
            else:
                try:
                    if getattr(r, "debrief_text", None):
                        from app.services.memory_service import extract_memories

                        extract_memories(
                            db, user,
                            f"Ride: {r.title}\n\nForma's debrief:\n{r.debrief_text}",
                            source="debrief", source_ref=str(r.id),
                        )
                except Exception:
                    logger.exception("Memory extraction after debrief failed (ride %s)", r.id)
    except Exception:
        logger.warning("Coach insights module failed to load")

    return synced_rides


def _activity_to_ride(user: User, activity: dict) -> Ride:
    """Convert a Strava activity to a Ride model."""
    start_time = datetime.fromisoformat(
        activity["start_date"].replace("Z", "+00:00")
    )

    return Ride(
        user_id=user.id,
        external_id=str(activity["id"]),
        source=RideSource.strava,
        title=activity.get("name", "Strava Ride"),
        ride_date=start_time,
        duration_seconds=activity.get("elapsed_time"),
        moving_time_seconds=activity.get("moving_time"),
        distance_meters=activity.get("distance"),
        elevation_gain_meters=activity.get("total_elevation_gain"),
        average_power=activity.get("average_watts"),
        max_power=activity.get("max_watts"),
        average_hr=activity.get("average_heartrate"),
        max_hr=activity.get("max_heartrate"),
        average_cadence=activity.get("average_cadence"),
        average_speed=activity.get("average_speed"),
        calories=activity.get("calories"),
        ftp_at_time=user.ftp,
    )


async def _fetch_activity_streams(
    access_token: str, activity_id: int
) -> dict | None:
    """Fetch time-series streams for a Strava activity. Raises StravaRateLimit on 429."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{STRAVA_API_BASE}/activities/{activity_id}/streams",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "keys": "time,watts,heartrate,cadence,altitude,latlng,distance,velocity_smooth",
                "key_by_type": "true",
            },
        )
        if response.status_code == 429:
            raise _parse_rate_limit_response(response)
        if response.status_code != 200:
            return None
        return response.json()


def _create_ride_data_from_streams(
    db: Session, ride_id: str, streams: dict
) -> None:
    """Create RideData rows from Strava streams."""
    from sqlalchemy import insert

    time_data = streams.get("time", {}).get("data", [])
    if not time_data:
        return

    power_data = streams.get("watts", {}).get("data", [])
    hr_data = streams.get("heartrate", {}).get("data", [])
    cadence_data = streams.get("cadence", {}).get("data", [])
    altitude_data = streams.get("altitude", {}).get("data", [])
    latlng_data = streams.get("latlng", {}).get("data", [])
    distance_data = streams.get("distance", {}).get("data", [])
    speed_data = streams.get("velocity_smooth", {}).get("data", [])

    data_rows = []
    for i, t in enumerate(time_data):
        lat = latlng_data[i][0] if i < len(latlng_data) and latlng_data[i] else None
        lng = latlng_data[i][1] if i < len(latlng_data) and latlng_data[i] else None

        data_rows.append({
            "ride_id": ride_id,
            "elapsed_seconds": t,
            "power": power_data[i] if i < len(power_data) else None,
            "heart_rate": hr_data[i] if i < len(hr_data) else None,
            "cadence": cadence_data[i] if i < len(cadence_data) else None,
            "altitude": altitude_data[i] if i < len(altitude_data) else None,
            "latitude": lat,
            "longitude": lng,
            "distance": distance_data[i] if i < len(distance_data) else None,
            "speed": speed_data[i] if i < len(speed_data) else None,
        })

    if data_rows:
        db.execute(insert(RideData), data_rows)


def _recalculate_ride_metrics(
    db: Session, ride: Ride, power_data: list, ftp: int | None
) -> None:
    """Recalculate NP/IF/TSS from actual power stream data."""
    np = normalized_power(power_data)
    avg_power = sum(power_data) / len(power_data) if power_data else 0

    ride.normalized_power = round(np, 1) if np else None
    ride.average_power = round(avg_power, 1) if avg_power else None
    ride.max_power = max(power_data) if power_data else None

    if ftp and ftp > 0 and np > 0:
        if_val = intensity_factor(np, ftp)
        vi_val = variability_index(np, avg_power) if avg_power > 0 else None
        duration = ride.duration_seconds or len(power_data)
        tss_val = training_stress_score(duration, np, if_val, ftp)

        ride.intensity_factor = round(if_val, 3)
        ride.variability_index = round(vi_val, 3) if vi_val else None
        ride.tss = round(tss_val, 1)


async def fetch_and_store_segments(
    db: Session,
    ride_id: str,
    strava_activity_id: str,
    access_token: str,
) -> int:
    """
    Fetch detailed activity from Strava and store segment efforts,
    achievement counts, and kudos. Returns number of segment efforts stored.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{STRAVA_API_BASE}/activities/{strava_activity_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code == 429:
            raise _parse_rate_limit_response(response)
        if response.status_code != 200:
            logger.warning(
                "Failed to fetch detailed activity %s (status %d)",
                strava_activity_id, response.status_code,
            )
            return 0
        activity = response.json()

    # Update ride-level counts
    ride = db.query(Ride).filter(Ride.id == ride_id).first()
    if not ride:
        return 0

    ride.achievement_count = activity.get("achievement_count")
    ride.pr_count = activity.get("pr_count")
    ride.kudos_count = activity.get("kudos_count")

    # Process segment efforts
    segment_efforts = activity.get("segment_efforts", [])
    stored = 0

    for effort in segment_efforts:
        seg_data = effort.get("segment", {})
        strava_seg_id = seg_data.get("id")
        if not strava_seg_id:
            continue

        # Upsert segment metadata
        segment = (
            db.query(StravaSegment)
            .filter(StravaSegment.strava_segment_id == strava_seg_id)
            .first()
        )
        if not segment:
            segment = StravaSegment(
                strava_segment_id=strava_seg_id,
                name=seg_data.get("name", "Unknown Segment"),
                distance_meters=seg_data.get("distance"),
                average_grade=seg_data.get("average_grade"),
                climb_category=seg_data.get("climb_category"),
                city=seg_data.get("city"),
                state=seg_data.get("state"),
            )
            db.add(segment)
            db.flush()

        # Skip if effort already stored
        strava_effort_id = effort.get("id")
        if strava_effort_id:
            existing_effort = (
                db.query(SegmentEffort)
                .filter(SegmentEffort.strava_effort_id == strava_effort_id)
                .first()
            )
            if existing_effort:
                continue

        # Parse achievements for best type
        achievements = effort.get("achievements", [])
        achievement_type = None
        for ach in achievements:
            t = ach.get("type")
            if t in ("overall", "kom"):
                achievement_type = "kom"
                break
            elif t == "pr":
                achievement_type = "pr"
            elif t and not achievement_type:
                achievement_type = t

        segment_effort = SegmentEffort(
            ride_id=ride_id,
            segment_id=segment.id,
            strava_effort_id=strava_effort_id,
            elapsed_time_seconds=effort.get("elapsed_time", 0),
            moving_time_seconds=effort.get("moving_time"),
            average_watts=effort.get("average_watts"),
            max_watts=effort.get("max_watts"),
            average_hr=effort.get("average_heartrate"),
            max_hr=effort.get("max_heartrate"),
            pr_rank=effort.get("pr_rank"),
            kom_rank=effort.get("kom_rank"),
            achievement_type=achievement_type,
        )
        db.add(segment_effort)
        stored += 1

    db.flush()
    logger.info(
        "Stored %d segment efforts for ride %s (strava activity %s)",
        stored, ride_id, strava_activity_id,
    )
    return stored


def _store_segments_from_activity(
    db: Session, ride_id: str, activity: dict
) -> int:
    """
    Store segment efforts from an already-fetched detailed activity response.
    Used by webhook handler which already has the full activity data.
    """
    ride = db.query(Ride).filter(Ride.id == ride_id).first()
    if not ride:
        return 0

    ride.achievement_count = activity.get("achievement_count")
    ride.pr_count = activity.get("pr_count")
    ride.kudos_count = activity.get("kudos_count")

    segment_efforts = activity.get("segment_efforts", [])
    stored = 0

    for effort in segment_efforts:
        seg_data = effort.get("segment", {})
        strava_seg_id = seg_data.get("id")
        if not strava_seg_id:
            continue

        segment = (
            db.query(StravaSegment)
            .filter(StravaSegment.strava_segment_id == strava_seg_id)
            .first()
        )
        if not segment:
            segment = StravaSegment(
                strava_segment_id=strava_seg_id,
                name=seg_data.get("name", "Unknown Segment"),
                distance_meters=seg_data.get("distance"),
                average_grade=seg_data.get("average_grade"),
                climb_category=seg_data.get("climb_category"),
                city=seg_data.get("city"),
                state=seg_data.get("state"),
            )
            db.add(segment)
            db.flush()

        strava_effort_id = effort.get("id")
        if strava_effort_id:
            existing_effort = (
                db.query(SegmentEffort)
                .filter(SegmentEffort.strava_effort_id == strava_effort_id)
                .first()
            )
            if existing_effort:
                continue

        achievements = effort.get("achievements", [])
        achievement_type = None
        for ach in achievements:
            t = ach.get("type")
            if t in ("overall", "kom"):
                achievement_type = "kom"
                break
            elif t == "pr":
                achievement_type = "pr"
            elif t and not achievement_type:
                achievement_type = t

        segment_effort = SegmentEffort(
            ride_id=ride_id,
            segment_id=segment.id,
            strava_effort_id=strava_effort_id,
            elapsed_time_seconds=effort.get("elapsed_time", 0),
            moving_time_seconds=effort.get("moving_time"),
            average_watts=effort.get("average_watts"),
            max_watts=effort.get("max_watts"),
            average_hr=effort.get("average_heartrate"),
            max_hr=effort.get("max_heartrate"),
            pr_rank=effort.get("pr_rank"),
            kom_rank=effort.get("kom_rank"),
            achievement_type=achievement_type,
        )
        db.add(segment_effort)
        stored += 1

    db.flush()
    return stored


def get_connection_status(db: Session, user_id: str) -> dict:
    """Check if Strava is connected and token status."""
    token = db.query(StravaToken).filter(StravaToken.user_id == user_id).first()
    if not token:
        return {"connected": False}

    return {
        "connected": True,
        "athlete_id": token.athlete_id,
        "last_sync_at": token.last_sync_at,
        "scope": token.scope,
        "can_read_activities": scope_has_activity_read(token.scope),
        "token_expired": token.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc) if token.expires_at else True,
    }


async def probe_activity_access(db: Session, user_id: str) -> dict:
    """Make ONE live call to Strava's activities endpoint and report the result.

    This is the ground-truth diagnostic: it proves whether the stored token can
    actually read activities right now, independent of what `scope` claims.
    """
    token = db.query(StravaToken).filter(StravaToken.user_id == user_id).first()
    if not token:
        return {"connected": False, "ok": False, "reason": "not_connected"}

    try:
        access_token = await _refresh_token_if_needed(db, token)
    except Exception as e:  # refresh-token itself revoked/expired
        return {"connected": True, "ok": False, "http_status": None,
                "stored_scope": token.scope, "reason": f"refresh_failed: {e}"}

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{STRAVA_API_BASE}/athlete/activities",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"per_page": 1},
        )
        # Second opinion: /athlete needs only `read`. 200 here + 403 above
        # isolates the failure to the activity permission specifically.
        athlete_resp = await client.get(
            f"{STRAVA_API_BASE}/athlete",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    reason = None
    if resp.status_code == 403:
        reason = "missing_activity_scope"
        logger.error(
            "Strava probe 403 (stored scope=%r, /athlete=%s): %s",
            token.scope, athlete_resp.status_code, resp.text[:500],
        )
    elif resp.status_code == 401:
        reason = "token_invalid"
    elif resp.status_code == 429:
        reason = "rate_limited"
    return {
        "connected": True,
        "ok": resp.status_code == 200,
        "http_status": resp.status_code,
        "stored_scope": token.scope,
        "reason": reason,
    }


def disconnect(db: Session, user_id: str) -> None:
    """Remove Strava connection."""
    db.query(StravaToken).filter(StravaToken.user_id == user_id).delete()
    db.commit()


# ---------------------------------------------------------------------------
# Historical Backfill
# ---------------------------------------------------------------------------

async def backfill_history(
    db: Session, user: User, since: datetime | None = None
) -> int:
    """
    Import ride history from Strava.

    Args:
        since: If provided, only import rides newer than this UTC datetime.
            Useful for keeping a fresh DB lean (e.g. only re-import the
            last 2 years after a volume wipe). If None, imports full history.

    Paginates through activities, fetches streams for each ride.
    Updates progress on StravaToken so the frontend can show progress.
    Returns total rides imported.
    """
    strava_token = db.query(StravaToken).filter(StravaToken.user_id == user.id).first()
    if not strava_token:
        raise ValueError("Strava not connected")

    # Mark backfill as running
    strava_token.backfill_status = "running"
    strava_token.backfill_started_at = datetime.now(timezone.utc)
    strava_token.backfill_progress = 0
    db.commit()

    try:
        access_token = await _refresh_token_if_needed(db, strava_token)

        # Phase 1: Collect all activity summaries
        all_activities = []
        page = 1
        per_page = 200  # Strava max

        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                # Build params, optionally constraining to rides after `since`.
                # Strava's `after` is a unix timestamp; activities returned
                # are strictly newer than it.
                params: dict = {"page": page, "per_page": per_page}
                if since is not None:
                    params["after"] = int(since.timestamp())

                # Pagination with smart rate-limit retry (handles daily vs 15-min)
                for attempt in range(5):
                    response = await client.get(
                        f"{STRAVA_API_BASE}/athlete/activities",
                        headers={"Authorization": f"Bearer {access_token}"},
                        params=params,
                    )
                    if response.status_code == 429:
                        rl = _parse_rate_limit_response(response)
                        logger.warning(
                            "Rate limited fetching page %d (attempt %d/5): %s, waiting %ds",
                            page, attempt + 1,
                            "DAILY" if rl.is_daily else "15-min",
                            rl.wait_secs,
                        )
                        strava_token.backfill_status = (
                            "paused_daily" if rl.is_daily else "paused"
                        )
                        db.commit()
                        await asyncio.sleep(rl.wait_secs)
                        strava_token.backfill_status = "running"
                        db.commit()
                        access_token = await _refresh_token_if_needed(db, strava_token)
                        continue
                    if response.status_code == 403:
                        strava_token.backfill_status = "failed_no_activity_scope"
                        db.commit()
                        raise ValueError(
                            "Strava activity access not granted. Reconnect and tick "
                            "“View data about your activities”."
                        )
                    response.raise_for_status()
                    break
                else:
                    raise RuntimeError(f"Exhausted retries fetching activities page {page}")

                activities = response.json()

                if not activities:
                    break

                # Include all cycling-type activities. Strava exposes:
                #   type: legacy field — "Ride" covers road/MTB/gravel
                #   sport_type: newer field — more granular
                cycling_types = {"Ride", "VirtualRide", "EBikeRide", "Velomobile"}
                rides = [
                    a for a in activities
                    if a.get("type") in cycling_types
                    or a.get("sport_type") in cycling_types
                    or a.get("sport_type") in {"MountainBikeRide", "GravelRide"}
                ]
                all_activities.extend(rides)
                page += 1

                # Safety: 50 pages * 200 = 10,000 activities
                if page > 50:
                    break

        # Update total count
        strava_token.backfill_total = len(all_activities)
        db.commit()

        logger.info(
            "Strava backfill: found %d rides for user %s",
            len(all_activities), user.email,
        )

        # Phase 2: Import each activity (skip already synced)
        imported = 0
        for i, activity in enumerate(all_activities):
            external_id = str(activity["id"])

            # Skip if already exists
            existing = db.query(Ride).filter(
                Ride.user_id == user.id,
                Ride.external_id == external_id,
            ).first()
            if existing:
                strava_token.backfill_progress = i + 1
                if (i + 1) % 50 == 0:
                    db.commit()
                continue

            # Skip if a Dropbox ride already covers this activity
            start_time = datetime.fromisoformat(
                activity["start_date"].replace("Z", "+00:00")
            )
            dropbox_match = find_dropbox_duplicate(
                db, user.id, start_time, activity.get("elapsed_time"),
            )
            if dropbox_match:
                if not dropbox_match.strava_activity_id:
                    dropbox_match.strava_activity_id = external_id
                # Fetch segments for the Dropbox ride
                try:
                    access_token = await _refresh_token_if_needed(db, strava_token)
                    await fetch_and_store_segments(
                        db, dropbox_match.id, external_id, access_token,
                    )
                    await asyncio.sleep(2.0)
                except Exception:
                    logger.warning("Backfill: failed to fetch segments for Dropbox ride %s", dropbox_match.id)
                strava_token.backfill_progress = i + 1
                if (i + 1) % 50 == 0:
                    db.commit()
                continue

            # Refresh token if needed (long backfill can outlast 6h token)
            access_token = await _refresh_token_if_needed(db, strava_token)

            ride = _activity_to_ride(user, activity)
            db.add(ride)
            db.flush()

            # Fetch streams with smart rate-limit retry.
            # On 429: parse Strava's rate-limit headers — if the DAILY limit
            # (1000 req/day) was hit, wait until midnight UTC. If the 15-min
            # rolling limit was hit, wait 16 minutes. Backfill runs in
            # background so long waits are fine.
            for attempt in range(10):
                try:
                    streams = await _fetch_activity_streams(access_token, activity["id"])
                    if streams:
                        _create_ride_data_from_streams(db, ride.id, streams)
                        power_stream = streams.get("watts", {}).get("data", [])
                        if power_stream:
                            _recalculate_ride_metrics(db, ride, power_stream, user.ftp)
                    break  # Success
                except StravaRateLimit as rl:
                    logger.warning(
                        "Strava rate limit (%s) on streams for %s, waiting %ds (attempt %d/10)",
                        "DAILY" if rl.is_daily else "15-min",
                        activity["id"], rl.wait_secs, attempt + 1,
                    )
                    strava_token.backfill_status = (
                        "paused_daily" if rl.is_daily else "paused"
                    )
                    db.commit()
                    await asyncio.sleep(rl.wait_secs)
                    strava_token.backfill_status = "running"
                    db.commit()
                    access_token = await _refresh_token_if_needed(db, strava_token)
                except httpx.HTTPStatusError as e:
                    logger.warning("Failed to fetch streams for activity %s: %s", activity["id"], e)
                    break

            # Fetch segments for the new ride (with rate-limit retry)
            for seg_attempt in range(10):
                try:
                    await fetch_and_store_segments(
                        db, ride.id, str(activity["id"]), access_token,
                    )
                    break
                except StravaRateLimit as rl:
                    logger.warning(
                        "Strava rate limit (%s) on segments for %s, waiting %ds",
                        "DAILY" if rl.is_daily else "15-min",
                        activity["id"], rl.wait_secs,
                    )
                    strava_token.backfill_status = (
                        "paused_daily" if rl.is_daily else "paused"
                    )
                    db.commit()
                    await asyncio.sleep(rl.wait_secs)
                    strava_token.backfill_status = "running"
                    db.commit()
                    access_token = await _refresh_token_if_needed(db, strava_token)
                except Exception:
                    logger.warning("Backfill: failed to fetch segments for activity %s", activity["id"])
                    break

            imported += 1
            strava_token.backfill_progress = i + 1

            # Commit in batches
            if imported % 10 == 0:
                db.commit()
                logger.info("Strava backfill: %d/%d imported", imported, len(all_activities))

            # Rate limit pacing: 3s to account for extra detailed activity call
            await asyncio.sleep(3.0)

        # Mark complete
        strava_token.backfill_status = "completed"
        strava_token.backfill_completed_at = datetime.now(timezone.utc)
        strava_token.last_sync_at = datetime.now(timezone.utc)
        db.commit()

        logger.info(
            "Strava backfill complete: %d rides imported for %s",
            imported, user.email,
        )
        return imported

    except Exception as e:
        logger.exception("Strava backfill failed for user %s", user.email)
        strava_token.backfill_status = "failed"
        db.commit()
        raise


async def run_backfill_background(
    user_id: str, since: datetime | None = None
) -> None:
    """
    Run backfill in a background task with its own DB session.
    Called via asyncio.create_task() so it doesn't block the request.

    Args:
        since: Optional UTC datetime cutoff — only imports rides newer than this.
    """
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            logger.error("Backfill: user %s not found", user_id)
            return

        count = await backfill_history(db, user, since=since)
        logger.info(
            "Background backfill done: %d rides for %s (since=%s)",
            count, user.email, since.isoformat() if since else "all-time",
        )

        # Recalculate PMC after backfill
        from app.services.metrics_service import recalculate_from_date
        from datetime import date

        earliest_ride = (
            db.query(Ride)
            .filter(Ride.user_id == user_id, Ride.source == RideSource.strava)
            .order_by(Ride.ride_date.asc())
            .first()
        )
        if earliest_ride and earliest_ride.ride_date:
            rd = earliest_ride.ride_date
            if hasattr(rd, "date"):
                rd = rd.date()
            recalculate_from_date(db, user_id, rd)
            logger.info("PMC recalculated from %s for %s", rd, user.email)

    except Exception:
        logger.exception("Background backfill failed for user %s", user_id)
    finally:
        db.close()


async def backfill_segments(db: Session, user_id: str) -> int:
    """
    Backfill segment data for existing rides that have a strava_activity_id
    but no segment efforts yet.
    """
    strava_token = db.query(StravaToken).filter(StravaToken.user_id == user_id).first()
    if not strava_token:
        raise ValueError("Strava not connected")

    access_token = await _refresh_token_if_needed(db, strava_token)

    # Find rides with Strava link but no segments
    from sqlalchemy import func as sqlfunc

    rides_with_segments = (
        db.query(SegmentEffort.ride_id).distinct().subquery()
    )
    rides_needing_segments = (
        db.query(Ride)
        .filter(
            Ride.user_id == user_id,
            Ride.strava_activity_id.isnot(None),
            ~Ride.id.in_(db.query(rides_with_segments.c.ride_id)),
        )
        .all()
    )

    logger.info(
        "Segment backfill: %d rides need segments for user %s",
        len(rides_needing_segments), user_id,
    )

    total = 0
    for ride in rides_needing_segments:
        try:
            access_token = await _refresh_token_if_needed(db, strava_token)
            count = await fetch_and_store_segments(
                db, ride.id, ride.strava_activity_id, access_token,
            )
            total += count
            db.commit()
        except Exception:
            logger.warning(
                "Segment backfill: failed for ride %s (strava %s)",
                ride.id, ride.strava_activity_id,
            )
            db.rollback()

        await asyncio.sleep(3.0)

    logger.info("Segment backfill complete: %d efforts stored", total)
    return total


async def run_segment_backfill_background(user_id: str) -> None:
    """Run segment backfill in a background task."""
    db = SessionLocal()
    try:
        count = await backfill_segments(db, user_id)
        logger.info("Background segment backfill done: %d efforts for user %s", count, user_id)
    except Exception:
        logger.exception("Background segment backfill failed for user %s", user_id)
    finally:
        db.close()


def get_backfill_status(db: Session, user_id: str) -> dict | None:
    """Get the current backfill progress for a user."""
    token = db.query(StravaToken).filter(StravaToken.user_id == user_id).first()
    if not token:
        return None

    return {
        "status": token.backfill_status,
        "total": token.backfill_total,
        "progress": token.backfill_progress or 0,
        "started_at": token.backfill_started_at.isoformat() if token.backfill_started_at else None,
        "completed_at": token.backfill_completed_at.isoformat() if token.backfill_completed_at else None,
    }


async def resume_incomplete_backfills() -> None:
    """
    On app startup, find any Strava backfills that were interrupted (server
    restart mid-backfill) and restart them. Idempotent — already-imported
    rides are skipped by `external_id` uniqueness.

    Targets tokens with status in {"running", "paused"} — these indicate an
    in-flight backfill that stopped without completing.
    """
    # Brief delay so the app has finished starting
    await asyncio.sleep(20)

    db = SessionLocal()
    try:
        tokens = db.query(StravaToken).filter(
            StravaToken.backfill_status.in_(["running", "paused", "paused_daily"])
        ).all()

        if not tokens:
            logger.info("No incomplete Strava backfills to resume")
            return

        for token in tokens:
            logger.info(
                "Resuming Strava backfill for user %s (was %s)",
                token.user_id, token.backfill_status,
            )
            # Fire-and-forget — each runs in its own DB session
            asyncio.create_task(run_backfill_background(token.user_id))
    except Exception:
        logger.exception("Failed to resume incomplete backfills")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Webhook Handler
# ---------------------------------------------------------------------------

async def handle_webhook_event(event: dict) -> None:
    """
    Process a Strava webhook event. Called from the webhook endpoint.

    Event format:
    {
        "object_type": "activity",
        "object_id": 12345678,
        "aspect_type": "create",  # create, update, delete
        "owner_id": 98765,        # athlete ID
        "subscription_id": 1,
        "event_time": 1234567890
    }
    """
    object_type = event.get("object_type")
    aspect_type = event.get("aspect_type")
    owner_id = event.get("owner_id")
    activity_id = event.get("object_id")

    # Only process activity creates/updates
    if object_type != "activity" or aspect_type not in ("create", "update"):
        return

    db = SessionLocal()
    try:
        # Find user by Strava athlete ID
        strava_token = (
            db.query(StravaToken)
            .filter(StravaToken.athlete_id == owner_id)
            .first()
        )
        if not strava_token:
            logger.warning("Webhook: no user found for athlete %s", owner_id)
            return

        user = db.query(User).filter(User.id == strava_token.user_id).first()
        if not user:
            return

        access_token = await _refresh_token_if_needed(db, strava_token)

        # Fetch the full activity
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{STRAVA_API_BASE}/activities/{activity_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if response.status_code != 200:
                logger.warning("Webhook: failed to fetch activity %s (status %d)", activity_id, response.status_code)
                return
            activity = response.json()

        # Skip non-rides
        if activity.get("type") not in ("Ride", "VirtualRide"):
            return

        external_id = str(activity["id"])

        if aspect_type == "create":
            # Check if already exists (dedup)
            existing = db.query(Ride).filter(
                Ride.user_id == user.id,
                Ride.external_id == external_id,
            ).first()
            if existing:
                return

            # Skip if a Dropbox ride already covers this activity
            start_time = datetime.fromisoformat(
                activity["start_date"].replace("Z", "+00:00")
            )
            dropbox_match = find_dropbox_duplicate(
                db, user.id, start_time, activity.get("elapsed_time"),
            )
            if dropbox_match:
                if not dropbox_match.strava_activity_id:
                    dropbox_match.strava_activity_id = external_id
                # Store segments on the Dropbox ride (no extra API call needed)
                _store_segments_from_activity(db, dropbox_match.id, activity)
                db.commit()
                logger.info(
                    "Webhook: skipping Strava activity %s — Dropbox ride %s exists (segments stored)",
                    external_id, dropbox_match.id,
                )
                return

            ride = _activity_to_ride(user, activity)
            db.add(ride)
            db.flush()

            # Fetch streams
            try:
                streams = await _fetch_activity_streams(access_token, activity["id"])
                if streams:
                    _create_ride_data_from_streams(db, ride.id, streams)
                    power_stream = streams.get("watts", {}).get("data", [])
                    if power_stream:
                        _recalculate_ride_metrics(db, ride, power_stream, user.ftp)
            except Exception:
                logger.warning("Webhook: failed to fetch streams for %s", activity_id)

            # Store segments from the already-fetched detailed activity
            _store_segments_from_activity(db, ride.id, activity)

            # Update last sync time
            strava_token.last_sync_at = datetime.now(timezone.utc)
            db.commit()

            # Recalculate PMC
            from app.services.metrics_service import recalculate_from_date
            if ride.tss and ride.ride_date:
                rd = ride.ride_date
                if hasattr(rd, "date"):
                    rd = rd.date()
                recalculate_from_date(db, user.id, rd)

            # Auto-link to the planned workout for this day
            try:
                from app.services.workout_assessment_service import auto_link_ride_to_workout
                auto_link_ride_to_workout(db, ride)
            except Exception:
                logger.exception("Webhook: failed to auto-link ride %s", ride.id)

            logger.info(
                "Webhook: synced new ride '%s' for %s",
                ride.title, user.email,
            )

        elif aspect_type == "update":
            # Update existing ride title if changed
            existing = db.query(Ride).filter(
                Ride.user_id == user.id,
                Ride.external_id == external_id,
            ).first()
            if existing and activity.get("name"):
                existing.title = activity["name"]
                db.commit()

    except Exception:
        logger.exception("Webhook processing failed for activity %s", activity_id)
    finally:
        db.close()
