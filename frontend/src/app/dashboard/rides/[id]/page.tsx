"use client";

import * as React from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import Link from "next/link";
import { ArrowLeft, Download, Pencil, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { rides, exports_, metrics, coachInsights } from "@/lib/api";
import type { SegmentEffort } from "@/lib/api";
import { formatDuration, formatDate, formatPower } from "@/lib/utils";
import { useAuth } from "@/lib/auth-context";
import { cn } from "@/lib/utils";
import { RideChart } from "@/components/charts/ride-chart";
import dynamic from "next/dynamic";

// Leaflet touches `window` at import time; load the map client-side only.
const RouteWindMap = dynamic(
  () => import("@/components/charts/route-wind-map").then((m) => m.RouteWindMap),
  { ssr: false }
);
const RideMap = dynamic(
  () => import("@/components/charts/ride-map").then((m) => m.RideMap),
  { ssr: false }
);
import { PowerCurveChart } from "@/components/charts/power-curve-chart";
import { buildWindRoute, windTotals } from "@/lib/windSegments";
import { RideElevation } from "@/components/charts/ride-elevation";
import { RideZonesChart } from "@/components/charts/ride-zones-chart";
import { DataTile } from "@/components/ui/data-tile";
import { Badge } from "@/components/ui/badge";
import { Kicker } from "@/components/ui/kicker";
import { Button } from "@/components/ui/button";
import { CoachNote } from "@/components/ui/coach-note";

export default function RideDetailPage() {
  const params = useParams();
  const { user } = useAuth();
  const rideId = params.id as string;
  const coachName = user?.coach_name || "Forma";

  const queryClient = useQueryClient();
  const [editingTitle, setEditingTitle] = React.useState(false);
  const [titleDraft, setTitleDraft] = React.useState("");

  const { data: ride, isLoading } = useQuery({
    queryKey: ["ride", rideId],
    queryFn: () => rides.get(rideId),
  });

  const { data: rideData } = useQuery({
    queryKey: ["ride-data", rideId],
    queryFn: () => rides.getData(rideId, "5s"),
    enabled: !!ride,
  });

  const { data: powerCurve } = useQuery({
    queryKey: ["ride-power-curve", rideId],
    queryFn: () => rides.getPowerCurve(rideId),
    enabled: !!ride,
  });

  const { data: rideZones } = useQuery({
    queryKey: ["ride-zones", rideId],
    queryFn: () => metrics.getRideZones(rideId),
    enabled: !!ride,
  });

  const { data: segments } = useQuery({
    queryKey: ["ride-segments", rideId],
    queryFn: () => rides.getSegments(rideId),
    enabled: !!ride,
  });

  const renameRide = useMutation({
    mutationFn: (title: string) => rides.update(rideId, { title }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["ride", rideId], updated);
      queryClient.invalidateQueries({ queryKey: ["rides"] });
      setEditingTitle(false);
    },
  });

  const { data: debrief, isLoading: debriefLoading } = useQuery({
    queryKey: ["ride-debrief", rideId],
    queryFn: () => coachInsights.getRideDebrief(rideId),
    enabled: !!ride,
    staleTime: 1000 * 60 * 60, // 1 hour
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-vb-red border-t-transparent" />
      </div>
    );
  }

  if (!ride) {
    return (
      <div className="py-20 text-center text-vb-text-dim">Ride not found</div>
    );
  }

  return (
    <div className="space-y-8">
      {/* ============ MASTHEAD ============ */}
      <header className="f-rise flex items-start justify-between gap-6 border-b-2 border-vb-border-strong pb-5">
        <div>
          <Link
            href="/dashboard/rides"
            className="mb-3 inline-flex items-center gap-1 font-mono text-[11px] uppercase tracking-[0.08em] text-vb-text-dim transition-colors hover:text-vb-red"
          >
            <ArrowLeft className="h-3.5 w-3.5" /> The training log
          </Link>
          <Kicker className="mb-2">
            {formatDate(ride.ride_date)}
            {ride.location_name && ` · ${ride.location_name}`}
            {ride.source && ` · ${ride.source.replace("_", " ")}`}
            {ride.ftp_at_time && ` · FTP ${ride.ftp_at_time}W`}
          </Kicker>
          {editingTitle ? (
            <form
              className="flex flex-wrap items-center gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                const next = titleDraft.trim();
                if (!next || next === ride.title) return setEditingTitle(false);
                renameRide.mutate(next);
              }}
            >
              <input
                autoFocus
                value={titleDraft}
                onChange={(e) => setTitleDraft(e.target.value)}
                onKeyDown={(e) => e.key === "Escape" && setEditingTitle(false)}
                maxLength={255}
                aria-label="Ride name"
                className="f-display min-w-0 flex-1 border-b-2 border-vb-red bg-transparent text-4xl outline-none md:text-5xl"
              />
              <Button type="submit" size="sm" disabled={renameRide.isPending}>
                {renameRide.isPending ? "Saving" : "Save"}
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setEditingTitle(false)}
              >
                Cancel
              </Button>
            </form>
          ) : (
            <div className="group flex flex-wrap items-center gap-3">
              <h1 className="f-display text-4xl md:text-5xl">{ride.title}</h1>
              <button
                onClick={() => {
                  setTitleDraft(ride.title);
                  setEditingTitle(true);
                }}
                aria-label="Rename this ride"
                title="Rename this ride"
                className="f-kicker text-vb-text-muted transition-colors hover:text-vb-red"
              >
                <Pencil className="h-3.5 w-3.5" />
              </button>
            </div>
          )}
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => exports_.downloadGPX(rideId, ride.title)}
          className="shrink-0"
        >
          <Download className="h-3.5 w-3.5" /> GPX
        </Button>
      </header>

      {/* ============ STATS GRID ============ */}
      <div className="f-stagger grid grid-cols-2 gap-px md:grid-cols-4 lg:grid-cols-6">
        <StatTile
          label="Duration"
          display={
            ride.duration_seconds ? formatDuration(ride.duration_seconds) : null
          }
        />
        <StatTile
          label="Distance"
          value={ride.distance_meters ? ride.distance_meters / 1000 : null}
          unit="km"
          decimals={1}
        />
        <StatTile
          label="TSS"
          value={ride.tss != null ? Math.round(ride.tss) : null}
          explainable="TSS"
        />
        <StatTile
          label="NP"
          value={
            ride.normalized_power != null
              ? Math.round(ride.normalized_power)
              : null
          }
          unit="W"
          explainable="Normalized Power"
        />
        <StatTile
          label="Avg Power"
          value={
            ride.average_power != null ? Math.round(ride.average_power) : null
          }
          unit="W"
          explainable="Average Power"
        />
        <StatTile
          label="IF"
          value={ride.intensity_factor ?? null}
          decimals={2}
          explainable="Intensity Factor"
        />
        <StatTile label="Avg HR" value={ride.average_hr ?? null} unit="bpm" />
        <StatTile label="Max HR" value={ride.max_hr ?? null} unit="bpm" />
        <StatTile
          label="Avg Cadence"
          value={ride.average_cadence ?? null}
          unit="rpm"
        />
        <StatTile
          label="Elevation"
          value={
            ride.elevation_gain_meters != null
              ? Math.round(ride.elevation_gain_meters)
              : null
          }
          unit="m"
        />
        <StatTile label="Calories" value={ride.calories ?? null} unit="kcal" />
        <StatTile label="Max Power" value={ride.max_power ?? null} unit="W" />
      </div>

      {/* ============ ACHIEVEMENTS ============ */}
      {segments &&
        (segments.achievement_count ||
          segments.pr_count ||
          segments.kudos_count) && (
          <div className="flex flex-wrap items-center gap-2">
            {!!segments.achievement_count && (
              <Badge variant="ink">
                {segments.achievement_count} achievement
                {segments.achievement_count !== 1 ? "s" : ""}
              </Badge>
            )}
            {!!segments.pr_count && (
              <Badge variant="outline">
                {segments.pr_count} PR{segments.pr_count !== 1 ? "s" : ""}
              </Badge>
            )}
            {!!segments.kudos_count && (
              <Badge variant="chalk">
                {segments.kudos_count} kudo
                {segments.kudos_count !== 1 ? "s" : ""}
              </Badge>
            )}
          </div>
        )}

      {/* ============ THE DEBRIEF ============ */}
      {debriefLoading && (
        <CoachNote kicker="Post-ride debrief" signature={false}>
          <span className="flex items-center gap-2 text-vb-text-dim">
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-vb-red border-t-transparent" />
            {coachName} is reading your ride…
          </span>
        </CoachNote>
      )}
      {debrief && (
        <CoachNote kicker="Post-ride debrief" coachName={coachName}>
          <div className="prose prose-sm max-w-none text-vb-text-dim prose-headings:font-display prose-headings:font-extrabold prose-headings:text-vb-text prose-p:my-1.5 prose-p:leading-relaxed prose-strong:font-semibold prose-strong:text-vb-text prose-em:not-italic prose-em:text-vb-red prose-ul:my-1.5">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {debrief.debrief}
            </ReactMarkdown>
          </div>
        </CoachNote>
      )}

      {/* ============ CONDITIONS ============ */}
      {ride.weather && !ride.weather.none && (
        <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2 border border-vb-border-subtle bg-vb-surface px-5 py-3">
          <span className="f-kicker text-vb-text-muted">Conditions</span>
          {ride.weather.temp_c != null && (
            <span className="f-data text-sm text-vb-text">
              {Math.round(ride.weather.temp_c)}°C
              {ride.weather.feels_c != null &&
                Math.round(ride.weather.feels_c) !== Math.round(ride.weather.temp_c) && (
                  <span className="text-vb-text-dim"> feels {Math.round(ride.weather.feels_c)}°</span>
                )}
            </span>
          )}
          {ride.weather.wind_kph != null && (
            <span className="f-data text-sm text-vb-text">
              {ride.weather.wind_dir ?? ""} {Math.round(ride.weather.wind_kph)} km/h
              {ride.weather.gust_kph != null && (
                <span className="text-vb-text-dim"> g{Math.round(ride.weather.gust_kph)}</span>
              )}
            </span>
          )}
          {ride.weather.humidity != null && (
            <span className="f-data text-sm text-vb-text-dim">{ride.weather.humidity}% hum</span>
          )}
          {ride.weather.pressure_hpa != null && (
            <span className="f-data text-sm text-vb-text-dim">{ride.weather.pressure_hpa} hPa</span>
          )}
          {ride.weather.description && (
            <span className="f-kicker text-vb-text-dim">{ride.weather.description}</span>
          )}
        </div>
      )}

      {/* ============ THE ROUTE ============ */}
      {rideData && rideData.data_points.some((p) => p.latitude != null) && (
        <div className="border border-vb-border-subtle bg-vb-surface p-5">
          <Kicker className="mb-4">
            The route{ride.location_name ? ` · ${ride.location_name}` : ""}
          </Kicker>
          <RideMap data={rideData.data_points} />
        </div>
      )}

      {/* ============ ELEVATION ============ */}
      {rideData && rideData.data_points.some((p) => p.altitude != null) && (
        <div className="border border-vb-border-subtle bg-vb-surface p-5">
          <Kicker className="mb-4">
            Elevation
            {ride.elevation_gain_meters != null
              ? ` · ${Math.round(ride.elevation_gain_meters)} m climbed`
              : ""}
          </Kicker>
          <RideElevation points={rideData.data_points} />
        </div>
      )}

      {/* ============ WIND ============ */}
      {(() => {
        const w = ride.weather as Record<string, number | null> | null;
        if (!rideData || !w || w.wind_deg == null) return null;
        const route = buildWindRoute(
          rideData.data_points,
          w.wind_deg as number,
          (w.wind_kph as number) ?? null
        );
        if (!route || route.segments.length === 0) return null;
        const t = windTotals(route.segments);
        const total = t.head + t.tail + t.cross || 1;
        const pct = (v: number) => Math.round((v / total) * 100);
        return (
          <div className="border border-vb-border-subtle bg-vb-surface p-5">
            <Kicker className="mb-4">
              The wind · {Math.round((w.wind_kph as number) ?? 0)} km/h from{" "}
              {(ride.weather as Record<string, string>).wind_dir ?? "?"}
            </Kicker>
            <RouteWindMap route={route} />
            <div className="f-data mt-4 flex flex-wrap gap-x-6 gap-y-2 text-xs text-vb-text-dim">
              <span>
                <span className="mr-1.5 inline-block h-2 w-2 rounded-full align-middle" style={{ background: "#D92420" }} />
                Headwind {pct(t.head)}%
              </span>
              <span>
                <span className="mr-1.5 inline-block h-2 w-2 rounded-full align-middle" style={{ background: "#439D7C" }} />
                Tailwind {pct(t.tail)}%
              </span>
              <span>
                <span className="mr-1.5 inline-block h-2 w-2 rounded-full align-middle" style={{ background: "#D9AC34" }} />
                Crosswind {pct(t.cross)}%
              </span>
            </div>
            <p className="mt-3 max-w-xl text-sm text-vb-text-dim">
              {t.head > t.tail
                ? "More of this ride was paid for into the wind than given back by it. Worth remembering before you read anything into the average speed."
                : t.tail > t.head
                  ? "The wind was on your side for more of this than it was against you, which flatters the average a little."
                  : "Wind roughly balanced out across the ride."}
            </p>
          </div>
        );
      })()}

      {/* ============ RIDE CHART ============ */}
      {/* RideChart draws its own card (border, heading, legend); wrapping it
          in a second, shorter card was the overflow in the 11 Aug review. */}
      {rideData && rideData.data_points.length > 0 && (
        <RideChart data={rideData.data_points} />
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        {/* Power Zone Distribution */}
        {rideZones && rideZones.zones.length > 0 && (
          <div className="border border-vb-border-subtle bg-vb-surface p-5">
            <Kicker className="mb-1">Where the time went</Kicker>
            <p className="f-data mb-4 text-xs text-vb-text-muted">
              Zones set from FTP {rideZones.ftp}W on the day
            </p>
            <RideZonesChart
              zones={rideZones.zones}
              totalSeconds={rideZones.total_seconds}
            />
          </div>
        )}

        {/* Power Curve */}
        {powerCurve && powerCurve.points.length > 0 && (
          <div className="border border-vb-border-subtle bg-vb-surface p-5">
            <Kicker className="mb-4">Power curve</Kicker>
            <div className="h-64">
              <PowerCurveChart data={powerCurve.points} />
            </div>
          </div>
        )}
      </div>

      {/* ============ SEGMENTS ============ */}
      {segments && segments.segment_efforts.length > 0 && (
        <div className="border border-vb-border-subtle bg-vb-surface p-5">
          <Kicker className="mb-4">
            Segments · {segments.segment_efforts.length}
          </Kicker>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-vb-border-subtle text-left">
                  <th className="f-kicker pb-2 pr-4 font-medium text-vb-text-muted">
                    Segment
                  </th>
                  <th className="f-kicker pb-2 pr-4 font-medium text-vb-text-muted">
                    Time
                  </th>
                  <th className="f-kicker pb-2 pr-4 font-medium text-vb-text-muted">
                    Avg Power
                  </th>
                  <th className="f-kicker pb-2 pr-4 font-medium text-vb-text-muted">
                    Grade
                  </th>
                  <th className="f-kicker pb-2 pr-4 font-medium text-vb-text-muted">
                    Category
                  </th>
                  <th className="f-kicker pb-2 font-medium text-vb-text-muted">
                    Achievements
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-vb-border-subtle">
                {segments.segment_efforts.map((effort) => (
                  <SegmentRow key={effort.id} effort={effort} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Stat tile: DataTile for numbers, a matching mono tile for formatted
 * strings and missing values. `explainable` keeps the tap-to-ask-Forma
 * behaviour: tap a metric and the coach explains what it means.
 */
function StatTile({
  label,
  value,
  display,
  unit,
  decimals = 0,
  explainable,
}: {
  label: string;
  value?: number | null;
  display?: string | null;
  unit?: string;
  decimals?: number;
  explainable?: string;
}) {
  const [showExplain, setShowExplain] = React.useState(false);
  const [explanation, setExplanation] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(false);

  const hasValue = display != null || value != null;

  const handleTap = async () => {
    if (!explainable || value == null) return;
    if (showExplain) {
      setShowExplain(false);
      return;
    }
    setShowExplain(true);
    if (!explanation) {
      setLoading(true);
      try {
        const result = await coachInsights.explainMetric(explainable, value);
        setExplanation(result.explanation);
      } catch {
        setExplanation("Couldn't load that right now. Try again in a moment.");
      } finally {
        setLoading(false);
      }
    }
  };

  const tile =
    value != null && display == null ? (
      <DataTile
        label={label}
        value={value}
        unit={unit}
        decimals={decimals}
        className={cn(
          explainable && "cursor-pointer transition-colors hover:border-vb-border-strong"
        )}
      />
    ) : (
      <div className="border border-vb-border-subtle bg-vb-surface p-4">
        <p className="f-kicker text-vb-text-muted">{label}</p>
        <p
          className={cn(
            "f-data mt-2 text-4xl font-semibold leading-none",
            hasValue ? "text-vb-text" : "text-vb-text-muted"
          )}
        >
          {display ?? "-"}
        </p>
      </div>
    );

  if (!explainable) return tile;

  return (
    <div
      className="relative"
      onClick={handleTap}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") handleTap();
      }}
    >
      {tile}
      {showExplain && (
        <div className="absolute left-0 right-0 top-full z-20 mt-1 border border-vb-border-subtle border-l-[3px] border-l-vb-red bg-vb-surface p-3">
          <div className="mb-1.5 flex items-center justify-between">
            <Kicker flamme>Forma explains</Kicker>
            <button
              onClick={(e) => {
                e.stopPropagation();
                setShowExplain(false);
              }}
              className="text-vb-text-muted hover:text-vb-text"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
          {loading ? (
            <div className="flex items-center gap-2 text-xs text-vb-text-dim">
              <div className="h-3 w-3 animate-spin rounded-full border border-vb-red border-t-transparent" />
              Forma is thinking…
            </div>
          ) : (
            <p className="text-xs leading-relaxed text-vb-text-dim">
              {explanation}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function formatSegmentTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function climbCategoryLabel(cat: number | null): string | null {
  if (cat === null || cat === undefined) return null;
  if (cat === 0) return null;
  if (cat === 5) return "HC";
  return `Cat ${cat}`;
}

function SegmentRow({ effort }: { effort: SegmentEffort }) {
  const hasPR = effort.pr_rank !== null && effort.pr_rank > 0;
  const hasKOM = effort.kom_rank !== null && effort.kom_rank > 0;
  const catLabel = climbCategoryLabel(effort.climb_category);

  return (
    <tr className="text-vb-text-dim">
      <td className="py-2.5 pr-4">
        <span className={hasPR || hasKOM ? "font-medium text-vb-text" : ""}>
          {effort.segment_name}
        </span>
        {effort.distance_meters && (
          <span className="f-data ml-2 text-xs text-vb-text-muted">
            {(effort.distance_meters / 1000).toFixed(1)}km
          </span>
        )}
      </td>
      <td className="f-data py-2.5 pr-4">
        {formatSegmentTime(effort.elapsed_time_seconds)}
      </td>
      <td className="f-data py-2.5 pr-4">
        {effort.average_watts ? `${Math.round(effort.average_watts)}W` : "-"}
      </td>
      <td className="f-data py-2.5 pr-4">
        {effort.average_grade !== null
          ? `${effort.average_grade.toFixed(1)}%`
          : "-"}
      </td>
      <td className="py-2.5 pr-4">
        {catLabel && <Badge variant="chalk">{catLabel}</Badge>}
      </td>
      <td className="py-2.5">
        <div className="flex gap-1.5">
          {hasKOM && (
            <Badge variant="flamme">
              {effort.kom_rank === 1 ? "KOM" : `${effort.kom_rank}nd`}
            </Badge>
          )}
          {hasPR && (
            <Badge variant="outline">
              {effort.pr_rank === 1
                ? "PR"
                : `${effort.pr_rank}${effort.pr_rank === 2 ? "nd" : effort.pr_rank === 3 ? "rd" : "th"} best`}
            </Badge>
          )}
        </div>
      </td>
    </tr>
  );
}
