"use client";

import type { Ride } from "@/lib/api";
import { ZONES } from "@/lib/palette";
import { zoneSeconds } from "@/lib/rideStory";

const KEYS = ["z1", "z2", "z3", "z4", "z5", "z6", "z7"] as const;

/** The ride's fingerprint: power over time as zone-coloured bars.
    `compact` renders a small inline version for list rows and rails. */
export function RideShape({
  ride,
  compact = false,
  className = "",
}: {
  ride: Ride;
  compact?: boolean;
  className?: string;
}) {
  const shape = ride.zone_summary?.shape;
  if (!shape || !shape.length) return null;
  return (
    <span
      className={`flex items-end gap-px ${
        compact ? "h-6 w-28" : "h-8 w-full max-w-[280px]"
      } ${className}`}
      aria-label="Ride power shape"
    >
      {shape.map(([h, z], i) => (
        <span
          key={i}
          className="flex-1"
          style={{
            height: `${h}%`,
            background: ZONES[KEYS[(z || 1) - 1]],
          }}
        />
      ))}
    </span>
  );
}

/** Time-in-zone proportions as a thin strip. */
export function ZoneStrip({
  ride,
  className = "",
}: {
  ride: Ride;
  className?: string;
}) {
  const secs = zoneSeconds(ride);
  if (!secs) return null;
  const total = secs.reduce((a, b) => a + b, 0);
  if (total <= 0) return null;
  return (
    <span
      className={`flex h-1.5 w-full max-w-[280px] overflow-hidden ${className}`}
      aria-label="Time in zones"
    >
      {secs.map((s, i) =>
        s / total >= 0.02 ? (
          <span
            key={KEYS[i]}
            style={{ width: `${(s / total) * 100}%`, background: ZONES[KEYS[i]] }}
          />
        ) : null
      )}
    </span>
  );
}
