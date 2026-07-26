/**
 * Training-zone map — the single source of truth for turning a ride's
 * intensity into the right Forma orb and zone colour. The orb file names
 * live in /public/orbs and follow the z*-name.webp convention.
 */

export type ZoneKey = "z1" | "z2" | "z3" | "z4" | "z5" | "z6" | "z7";

export interface ZoneInfo {
  key: ZoneKey;
  /** Cycling zone name shown in kickers / alt text. */
  name: string;
  /** Orb file base in /public/orbs (light). Dark adds "-dark". */
  orb: string;
  /** Zone colour token value (chips, charts). */
  color: string;
  /**
   * Floor-caustic colour for the orb, sampled from the orb's own interior
   * and brightened. NOTE: the legacy zone orbs predate the z1-z7 palette,
   * so orbTint (what the orb actually is) differs from color (the token)
   * for z1-z3. Re-rendering the zone orbs to the finalized palette will
   * let these reconverge.
   */
  orbTint: string;
}

export const ZONES: Record<ZoneKey, ZoneInfo> = {
  z1: { key: "z1", name: "Recovery", orb: "z1-rest", color: "#8E9196", orbTint: "#5FB8A6" },
  z2: { key: "z2", name: "Endurance", orb: "z2-endurance", color: "#4A72AE", orbTint: "#3E6FB5" },
  z3: { key: "z3", name: "Tempo", orb: "z3-tempo", color: "#439D7C", orbTint: "#5C8A34" },
  z4: { key: "z4", name: "Threshold", orb: "z4-effort", color: "#D9AC34", orbTint: "#E8641B" },
  z5: { key: "z5", name: "VO2 max", orb: "z5-sprint", color: "#E86F22", orbTint: "#E01B1B" },
  // Sprint orb covers Z5-Z6; Z7 is the neuromuscular max-effort orb.
  z6: { key: "z6", name: "Anaerobic", orb: "z5-sprint", color: "#D92420", orbTint: "#E01B1B" },
  z7: { key: "z7", name: "Sprint", orb: "z7-max-effort", color: "#B81743", orbTint: "#8A1A3C" },
};

/**
 * Map a ride's overall intensity factor to the zone that best represents
 * the session. IF is np/FTP, so the standard Coggan bands apply.
 */
export function zoneFromIF(intensityFactor: number | null | undefined): ZoneInfo {
  const f = intensityFactor ?? 0;
  if (f > 0 && f < 0.55) return ZONES.z1;
  if (f < 0.75) return ZONES.z2;
  if (f < 0.9) return ZONES.z3;
  if (f < 1.05) return ZONES.z4;
  if (f < 1.2) return ZONES.z5;
  if (f >= 1.2) return ZONES.z7;
  return ZONES.z2; // no IF yet → endurance, the honest default
}

/** Map a planned workout_type string (e.g. "endurance", "vo2max") to a zone. */
/** Zone from a %FTP power target (Coggan bands). */
export function zoneFromPct(pct: number | null | undefined): ZoneInfo {
  const p = pct ?? 50;
  if (p <= 55) return ZONES.z1;
  if (p <= 75) return ZONES.z2;
  if (p <= 90) return ZONES.z3;
  if (p <= 105) return ZONES.z4;
  if (p <= 120) return ZONES.z5;
  if (p <= 150) return ZONES.z6;
  return ZONES.z7;
}

export function zoneFromWorkoutType(workoutType: string | null | undefined): ZoneInfo {
  const t = (workoutType ?? "").toLowerCase();
  if (t.includes("recovery") || t.includes("rest")) return ZONES.z1;
  if (t.includes("endurance") || t.includes("base") || t.includes("long")) return ZONES.z2;
  if (t.includes("tempo")) return ZONES.z3;
  if (t.includes("threshold") || t.includes("sweet") || t.includes("ftp")) return ZONES.z4;
  if (t.includes("vo2")) return ZONES.z5;
  if (t.includes("anaerobic")) return ZONES.z6;
  if (t.includes("sprint") || t.includes("neuro")) return ZONES.z7;
  return ZONES.z2;
}
