"use client";

import * as React from "react";

/**
 * The ride's elevation silhouette.
 *
 * Terrain never wears effort colours: it is drawn in quiet ink so it reads
 * as the shape of the road, not as a performance claim. Plotted against
 * distance rather than time, because a climb is a place, not a moment.
 */
export function RideElevation({
  points,
  height = 150,
}: {
  points: {
    altitude?: number | null;
    distance?: number | null;
    elapsed_seconds?: number | null;
  }[];
  height?: number;
}) {
  const series = React.useMemo(() => {
    const rows = points.filter((p) => p.altitude != null);
    if (rows.length < 2) return null;

    // Distance if the file carries it, otherwise fall back to elapsed time
    // so a turbo ride or a GPS dropout still draws something honest.
    const hasDistance = rows.some((p) => p.distance != null);
    const x = (p: (typeof rows)[number], i: number) =>
      hasDistance ? (p.distance ?? 0) : (p.elapsed_seconds ?? i);

    // Thin to something a browser can draw smoothly.
    const MAX = 420;
    const step = Math.max(1, Math.floor(rows.length / MAX));
    const thinned = rows.filter((_, i) => i % step === 0);

    const xs = thinned.map((p, i) => x(p, i));
    const ys = thinned.map((p) => p.altitude as number);
    const xMin = Math.min(...xs);
    const xMax = Math.max(...xs);
    const yMin = Math.min(...ys);
    const yMax = Math.max(...ys);
    return { xs, ys, xMin, xMax, yMin, yMax, hasDistance };
  }, [points]);

  if (!series) return null;

  const W = 1000;
  const H = height;
  const pad = 2;
  const spanX = series.xMax - series.xMin || 1;
  // A flat ride should look flat, not amplified into drama by autoscaling.
  const spanY = Math.max(series.yMax - series.yMin, 40);

  const px = (v: number) => ((v - series.xMin) / spanX) * W;
  const py = (v: number) =>
    H - pad - ((v - series.yMin) / spanY) * (H - pad * 2);

  const line = series.xs
    .map((xv, i) => `${i === 0 ? "M" : "L"}${px(xv).toFixed(1)},${py(series.ys[i]).toFixed(1)}`)
    .join(" ");
  const area = `${line} L${W},${H} L0,${H} Z`;

  const totalKm = series.hasDistance ? spanX / 1000 : null;

  return (
    <div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="block w-full"
        style={{ height }}
        role="img"
        aria-label="Elevation profile"
      >
        <path d={area} fill="currentColor" className="text-vb-border-subtle" opacity="0.7" />
        <path
          d={line}
          fill="none"
          stroke="currentColor"
          className="text-vb-text-muted"
          strokeWidth="1.5"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <div className="f-data mt-2 flex justify-between text-[11px] text-vb-text-muted">
        <span>{Math.round(series.yMin)} m</span>
        {totalKm != null && <span>{totalKm.toFixed(1)} km</span>}
        <span>{Math.round(series.yMax)} m</span>
      </div>
    </div>
  );
}
