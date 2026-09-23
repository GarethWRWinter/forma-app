/**
 * rideStory — one human line per ride: what you did, and what it did for you.
 *
 * Remove the thinking, keep the teaching: use the real term (tempo, TSS),
 * gloss it in plain English, never condescend. Deterministic, no LLM cost.
 */

import type { Ride } from "@/lib/api";

// What each zone IS (the jargon) and what it DOES for you (the teaching).
const ZONE_STORY: Record<
  string,
  { name: string; means: string; does: string }
> = {
  z1: {
    name: "Recovery",
    means: "the gentlest gear",
    does: "lets the body absorb the hard work",
  },
  z2: {
    name: "Endurance",
    means: "all-day pace",
    does: "builds the aerobic engine everything else sits on",
  },
  z3: {
    name: "Tempo",
    means: "comfortably hard",
    does: "teaches your legs to hold power when it starts to bite",
  },
  z4: {
    name: "Threshold",
    means: "right on the red line",
    does: "raises the power you can hold when it matters",
  },
  z5: {
    name: "VO2 max",
    means: "deep in the red",
    does: "grows the size of your engine itself",
  },
  z6: {
    name: "Anaerobic",
    means: "short and savage",
    does: "sharpens your attacks and surges",
  },
  z7: {
    name: "Sprint",
    means: "everything at once",
    does: "trains pure explosive speed",
  },
};

/** The ride's dominant zone key ("z1".."z7") or null. */
export function dominantZone(ride: Ride): string | null {
  const zs = ride.zone_summary;
  if (!zs || zs.none || !zs.dom) return null;
  return zs.dom as string;
}

/** Zone seconds array (7 entries) or null. */
export function zoneSeconds(ride: Ride): number[] | null {
  const zs = ride.zone_summary;
  if (!zs || zs.none || !Array.isArray(zs.z)) return null;
  return zs.z as number[];
}

/** One sentence: what was achieved, in human language that teaches. */
export function rideStory(ride: Ride): string | null {
  const dom = dominantZone(ride);
  const km = ride.distance_meters ? ride.distance_meters / 1000 : null;
  const mins = ride.duration_seconds
    ? Math.round(ride.duration_seconds / 60)
    : null;
  const tss = ride.tss ? Math.round(ride.tss) : null;

  if (dom && ZONE_STORY[dom]) {
    const z = ZONE_STORY[dom];
    const load =
      tss && tss >= 60
        ? ` ${tss} TSS banked, enough to move your fitness on.`
        : tss
          ? ` ${tss} TSS banked.`
          : "";
    return `Mostly ${z.name.toLowerCase()}, ${z.means}. This ${z.does}.${load}`;
  }

  // No power data: still say something true and warm.
  if (km && mins) {
    const easy = ride.intensity_factor && ride.intensity_factor < 0.6;
    return easy
      ? `${km.toFixed(0)}km of easy miles. Gentle days like this are what make the hard days possible.`
      : `${km.toFixed(0)}km in ${mins} minutes. Every ride Forma sees makes the next plan sharper.`;
  }
  return null;
}
