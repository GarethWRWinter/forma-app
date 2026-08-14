/**
 * Classify a ridden route against the wind that was blowing at the time.
 *
 * Mirrors the server-side goal-day logic (briefing_service.analyze_route_wind)
 * so a ride debrief reads the road the same way a race-morning briefing does:
 * where the wind was a tax to be paid, where it was free speed, and where it
 * was just asking for attention.
 */

export interface WindSegment {
  from_km: number;
  to_km: number;
  wind: "head" | "tail" | "cross";
}

export interface WindRoute {
  track: [number, number][];
  km: number[];
  segments: WindSegment[];
  wind_deg?: number | null;
  wind_kph?: number | null;
}

const R = 6371; // km

function haversine(a: [number, number], b: [number, number]): number {
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(b[0] - a[0]);
  const dLon = toRad(b[1] - a[1]);
  const lat1 = toRad(a[0]);
  const lat2 = toRad(b[0]);
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

/** Compass bearing travelled from a to b, degrees clockwise from north. */
function bearing(a: [number, number], b: [number, number]): number {
  const toRad = (d: number) => (d * Math.PI) / 180;
  const lat1 = toRad(a[0]);
  const lat2 = toRad(b[0]);
  const dLon = toRad(b[1] - a[1]);
  const y = Math.sin(dLon) * Math.cos(lat2);
  const x =
    Math.cos(lat1) * Math.sin(lat2) -
    Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
  return (((Math.atan2(y, x) * 180) / Math.PI) + 360) % 360;
}

/**
 * @param windFromDeg direction the wind is coming FROM (meteorological)
 */
export function buildWindRoute(
  points: { latitude?: number | null; longitude?: number | null }[],
  windFromDeg: number | null | undefined,
  windKph: number | null | undefined,
  maxPoints = 240
): WindRoute | null {
  const gps = points.filter(
    (p) => p.latitude != null && p.longitude != null
  ) as { latitude: number; longitude: number }[];
  if (gps.length < 2 || windFromDeg == null) return null;

  // Thin the track: a map line and a wind read need shape, not every sample.
  const step = Math.max(1, Math.floor(gps.length / maxPoints));
  const track: [number, number][] = [];
  for (let i = 0; i < gps.length; i += step) {
    track.push([gps[i].latitude, gps[i].longitude]);
  }
  if (track.length < 2) return null;

  const km: number[] = [0];
  for (let i = 1; i < track.length; i++) {
    km.push(km[i - 1] + haversine(track[i - 1], track[i]));
  }

  // Wind blows TO the opposite of where it comes from.
  const windTo = (windFromDeg + 180) % 360;

  const raw: WindSegment[] = [];
  for (let i = 1; i < track.length; i++) {
    const b = bearing(track[i - 1], track[i]);
    // Smallest angle between where you are heading and where the wind pushes.
    const diff = Math.abs((((windTo - b + 540) % 360) - 180));
    const wind: WindSegment["wind"] =
      diff <= 60 ? "tail" : diff >= 120 ? "head" : "cross";
    raw.push({ from_km: km[i - 1], to_km: km[i], wind });
  }

  // Merge neighbours, then fold away blips under a kilometre: a rider cannot
  // act on fifty metres of crosswind, so naming it is noise.
  const merged: WindSegment[] = [];
  for (const seg of raw) {
    const last = merged[merged.length - 1];
    if (last && last.wind === seg.wind) last.to_km = seg.to_km;
    else merged.push({ ...seg });
  }
  const cleaned: WindSegment[] = [];
  for (const seg of merged) {
    const last = cleaned[cleaned.length - 1];
    if (last && seg.to_km - seg.from_km < 1) last.to_km = seg.to_km;
    else cleaned.push({ ...seg });
  }

  return {
    track,
    km,
    segments: cleaned,
    wind_deg: windFromDeg,
    wind_kph: windKph ?? null,
  };
}

/** Kilometres spent in each wind state, for the one-line summary. */
export function windTotals(segments: WindSegment[]) {
  const t = { head: 0, tail: 0, cross: 0 };
  for (const s of segments) t[s.wind] += s.to_km - s.from_km;
  return t;
}
