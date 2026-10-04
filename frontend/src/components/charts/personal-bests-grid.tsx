"use client";

import Link from "next/link";
import { Trophy, Zap, Flame, Wind, Mountain } from "lucide-react";
import { cn } from "@/lib/utils";

interface PBPoint {
  duration_seconds: number;
  duration_label: string;
  best_power: number;
  watts_per_kg: number | null;
  ride_id: string | null;
  ride_date: string | null;
  all_time_power: number | null;
  all_time_watts_per_kg: number | null;
  all_time_ride_date: string | null;
}

interface PersonalBestsGridProps {
  points: PBPoint[];
  days?: number | null;
  className?: string;
}

function durationBand(seconds: number): {
  color: string;
  bgColor: string;
  borderColor: string;
  icon: React.ReactNode;
} {
  if (seconds <= 30) {
    return {
      color: "text-[#E86F22]",
      bgColor: "bg-[#E86F22]/10",
      borderColor: "border-vb-border-subtle",
      icon: <Zap className="h-3.5 w-3.5 text-[#E86F22]" />,
    };
  }
  if (seconds <= 300) {
    return {
      color: "text-[#D92420]",
      bgColor: "bg-[#D92420]/10",
      borderColor: "border-vb-border-subtle",
      icon: <Flame className="h-3.5 w-3.5 text-[#D92420]" />,
    };
  }
  if (seconds <= 1800) {
    return {
      color: "text-[#4A72AE]",
      bgColor: "bg-[#4A72AE]/10",
      borderColor: "border-vb-border-subtle",
      icon: <Wind className="h-3.5 w-3.5 text-[#4A72AE]" />,
    };
  }
  return {
    color: "text-vb-forest",
    bgColor: "bg-vb-forest/10",
    borderColor: "border-vb-border-subtle",
    icon: <Mountain className="h-3.5 w-3.5 text-vb-forest" />,
  };
}

function formatPBDate(dateStr: string): string {
  const d = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 30) return `${diffDays}d ago`;
  if (diffDays < 365) return `${Math.floor(diffDays / 30)}mo ago`;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "2-digit" });
}

export function PersonalBestsGrid({ points, days, className }: PersonalBestsGridProps) {
  const validPoints = points.filter(
    (p) => p.best_power > 0 || (p.all_time_power && p.all_time_power > 0)
  );

  if (validPoints.length === 0) return null;

  const label = days ? `Last ${days} Days` : "All Time";

  return (
    <div className={cn("rounded-md border border-vb-border-subtle bg-vb-surface p-4", className)}>
      <div className="mb-4 flex items-center gap-2">
        <Trophy className="h-4 w-4 text-vb-clay" />
        <h3 className="text-[11px] font-medium uppercase tracking-[0.16em] text-vb-text-muted">Personal Bests</h3>
        {days && (
          <span className="rounded-full bg-vb-forest/15 px-2 py-0.5 text-[10px] font-medium text-vb-forest">
            Current Form ({label})
          </span>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
        {validPoints.map((point) => {
          const band = durationBand(point.duration_seconds);
          const hasAllTime = point.all_time_power != null && point.all_time_power > 0;
          const isAtPB =
            hasAllTime &&
            point.best_power > 0 &&
            Math.round(point.best_power) >= Math.round(point.all_time_power!);
          const pctOfPB =
            hasAllTime && point.best_power > 0
              ? Math.round((point.best_power / point.all_time_power!) * 100)
              : null;

          const card = (
            <div
              key={point.duration_seconds}
              className={cn(
                "rounded-sm border p-3 transition-colors",
                band.borderColor,
                band.bgColor,
                point.ride_id && "hover:border-vb-border cursor-pointer"
              )}
            >
              <div className="mb-2 flex items-center justify-between">
                <span className="text-[11px] font-medium uppercase tracking-wider text-vb-text-dim">
                  {point.duration_label}
                </span>
                {isAtPB ? (
                  <Trophy className="h-3.5 w-3.5 text-vb-clay" />
                ) : (
                  band.icon
                )}
              </div>
              {/* Current form power */}
              <div className={cn("text-xl font-bold tabular-nums", band.color)}>
                {point.best_power > 0 ? Math.round(point.best_power) : ", "}
                <span className="ml-0.5 text-xs font-normal text-vb-text-muted">W</span>
              </div>
              {point.watts_per_kg !== null && point.best_power > 0 && (
                <div className="mt-0.5 text-xs tabular-nums text-vb-text-dim">
                  {point.watts_per_kg.toFixed(2)} W/kg
                </div>
              )}
              {/* All-time PB comparison */}
              {hasAllTime && !isAtPB && (
                <div className="mt-1.5 border-t border-vb-border-subtle pt-1.5">
                  <div className="text-[10px] text-vb-text-muted">
                    All-time: {Math.round(point.all_time_power!)}W
                    {point.all_time_watts_per_kg != null && (
                      <span className="ml-1">
                        ({point.all_time_watts_per_kg.toFixed(1)} W/kg)
                      </span>
                    )}
                  </div>
                  {pctOfPB !== null && (
                    <div className={cn(
                      "text-[10px] font-medium",
                      pctOfPB >= 95 ? "text-vb-success" :
                      pctOfPB >= 85 ? "text-vb-ochre" : "text-vb-text-muted"
                    )}>
                      {pctOfPB}% of PB
                    </div>
                  )}
                </div>
              )}
              {isAtPB && (
                <div className="mt-1.5 text-[10px] font-medium text-vb-clay">
                  All-time PB
                </div>
              )}
              {point.ride_date && (
                <div className="mt-1 text-[10px] text-vb-text-muted">
                  {formatPBDate(point.ride_date)}
                </div>
              )}
            </div>
          );

          if (point.ride_id) {
            return (
              <Link
                key={point.duration_seconds}
                href={`/dashboard/rides/${point.ride_id}`}
              >
                {card}
              </Link>
            );
          }
          return card;
        })}
      </div>

      {/* Legend */}
      <div className="mt-3 flex flex-wrap items-center gap-4 text-[10px] text-vb-text-muted">
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-[#E86F22]" />
          Neuromuscular
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-[#D92420]" />
          Anaerobic / VO2max
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-[#4A72AE]" />
          Threshold
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-vb-forest" />
          Endurance
        </span>
        {days && (
          <span className="ml-auto flex items-center gap-1">
            <Trophy className="h-2.5 w-2.5 text-vb-clay" />
            = matches all-time PB
          </span>
        )}
      </div>
    </div>
  );
}
