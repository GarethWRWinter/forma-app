"use client";

import {
  ResponsiveContainer,
  RadarChart,
  Radar,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
  Tooltip,
} from "recharts";
import { cn } from "@/lib/utils";
import { SERIES } from "@/lib/palette";
import type { RiderProfileScore } from "@/lib/api";

const MONO = "IBM Plex Mono, ui-monospace, SFMono-Regular, monospace";

interface RiderProfileRadarProps {
  scores: RiderProfileScore[];
  riderType: string;
  strengths: string[];
  weaknesses: string[];
  compact?: boolean;
  /** Explicit chart height in px — overrides the compact/full defaults. */
  height?: number;
  className?: string;
}

const RIDER_TYPE_LABELS: Record<string, string> = {
  sprinter: "Sprinter",
  pursuiter: "Pursuiter",
  time_trialist: "Time trialist",
  climber: "Climber",
  all_rounder: "All-rounder",
  unknown: "Unknown",
};

function RadarTooltipContent({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload: { label: string; score: number } }>;
}) {
  if (!active || !payload?.length) return null;
  const data = payload[0].payload;
  return (
    <div className="rounded-sm border border-vb-border bg-vb-surface px-3 py-2">
      <p className="f-kicker text-vb-text-muted">{data.label}</p>
      <p className="f-data mt-1 text-sm font-medium text-vb-text">
        {data.score.toFixed(0)}
        <span className="ml-0.5 text-xs font-normal text-vb-text-muted">/100</span>
      </p>
    </div>
  );
}

export function RiderProfileRadar({
  scores,
  riderType,
  strengths,
  weaknesses,
  compact = false,
  height,
  className,
}: RiderProfileRadarProps) {
  const radarData = scores.map((s) => ({
    category: s.category,
    label: s.label.replace(/ \(.*\)/, ""),
    score: s.score,
    fullMark: 100,
  }));

  const typeLabel = RIDER_TYPE_LABELS[riderType] || RIDER_TYPE_LABELS.unknown;
  const hasData = scores.some((s) => s.score > 0);

  const chartHeight = height ?? (compact ? 200 : 280);
  const outerRadius = compact && !height ? "70%" : "78%";
  const tickFontSize = compact ? 10 : 11;
  const dotRadius = compact ? 2.5 : 3;

  return (
    <div
      className={cn(
        compact
          ? ""
          : "rounded-sm border border-vb-border-subtle bg-vb-surface p-4",
        className
      )}
    >
      {/* Header, hidden in compact mode */}
      {!compact && (
        <div className="mb-2 text-center">
          <h3 className="f-kicker text-vb-text-muted">The rider you are</h3>
          <span className="mt-1.5 inline-block rounded-sm bg-vb-text px-2.5 py-1 font-mono text-[10px] font-semibold uppercase tracking-[0.12em] text-white">
            {typeLabel}
          </span>
        </div>
      )}

      {/* Radar Chart */}
      {hasData ? (
        <ResponsiveContainer width="100%" height={chartHeight}>
          <RadarChart data={radarData} cx="50%" cy="50%" outerRadius={outerRadius}>
            <PolarGrid stroke={SERIES.hairline} />
            <PolarAngleAxis
              dataKey="label"
              tick={{ fontSize: tickFontSize, fill: SERIES.grey, fontFamily: MONO }}
            />
            <PolarRadiusAxis
              domain={[0, 100]}
              tick={{ fontSize: 9, fill: SERIES.grey, fontFamily: MONO }}
              tickCount={compact ? 3 : 5}
              axisLine={false}
            />
            {/* Median reference area */}
            <Radar
              name="Median"
              dataKey="fullMark"
              stroke="none"
              fill="none"
            />
            {/* 50th percentile reference line */}
            <Radar
              name="Average"
              dataKey={() => 50}
              stroke={SERIES.hairline}
              strokeWidth={1}
              strokeDasharray="4 4"
              fill="none"
            />
            {/* Rider's profile — ink stroke, chalk fill */}
            <Radar
              name="You"
              dataKey="score"
              stroke={SERIES.ink}
              strokeWidth={1.5}
              fill={SERIES.chalk}
              fillOpacity={0.85}
              dot={{ r: dotRadius, fill: SERIES.ink, stroke: SERIES.ink }}
            />
            <Tooltip content={<RadarTooltipContent />} />
          </RadarChart>
        </ResponsiveContainer>
      ) : (
        <div className={cn("flex items-center justify-center", compact ? "h-[200px]" : "h-[280px]")}>
          <p className="max-w-[26ch] text-center text-sm text-vb-text-dim">
            Ride with power and the shape of your engine appears here.
          </p>
        </div>
      )}

      {/* Strengths & Weaknesses, hidden in compact mode */}
      {!compact && (strengths.length > 0 || weaknesses.length > 0) && (
        <div className="mt-3 space-y-2">
          {strengths.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {strengths.map((s) => (
                <span
                  key={s}
                  className="rounded-sm border border-vb-border-strong px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-[0.1em] text-vb-text"
                >
                  {s}
                </span>
              ))}
            </div>
          )}
          {weaknesses.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {weaknesses.map((w) => (
                <span
                  key={w}
                  className="rounded-sm border border-vb-border px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-[0.1em] text-vb-text-dim"
                >
                  {w}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
