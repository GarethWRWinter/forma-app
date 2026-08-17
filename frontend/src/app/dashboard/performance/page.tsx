"use client";

import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { coachInsights, metrics } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { CoachNote } from "@/components/ui/coach-note";
import { StatCard } from "@/components/ui/stat-card";
import { PMCChart } from "@/components/charts/pmc-chart";
import { PowerCurveChart } from "@/components/charts/power-curve-chart";
import { ZoneDistribution } from "@/components/charts/zone-distribution";
import { WeeklyLoadChart } from "@/components/charts/weekly-load-chart";
import { FTPHistoryChart } from "@/components/charts/ftp-history-chart";
import { PersonalBestsGrid } from "@/components/charts/personal-bests-grid";
import { RiderProfileRadar } from "@/components/charts/rider-profile-radar";

type DateRange = "1m" | "3m" | "6m" | "1y" | "all";

const DATE_RANGE_OPTIONS: { value: DateRange; label: string }[] = [
  { value: "1m", label: "1 Month" },
  { value: "3m", label: "3 Months" },
  { value: "6m", label: "6 Months" },
  { value: "1y", label: "1 Year" },
  { value: "all", label: "All Time" },
];

function getDateRangeCutoff(range: DateRange): Date | null {
  if (range === "all") return null;
  const now = new Date();
  switch (range) {
    case "1m":
      now.setMonth(now.getMonth() - 1);
      break;
    case "3m":
      now.setMonth(now.getMonth() - 3);
      break;
    case "6m":
      now.setMonth(now.getMonth() - 6);
      break;
    case "1y":
      now.setFullYear(now.getFullYear() - 1);
      break;
  }
  return now;
}

function getWeeklyLoadWeeks(range: DateRange): number {
  switch (range) {
    case "1m":
      return 5;
    case "3m":
      return 13;
    case "6m":
      return 26;
    case "1y":
      return 52;
    case "all":
      return 520;
  }
}

export default function PerformancePage() {
  const { user } = useAuth();
  const [dateRange, setDateRange] = useState<DateRange>("3m");

  const { data: pmc, isLoading: pmcLoading } = useQuery({
    queryKey: ["pmc"],
    queryFn: () => metrics.getPMC(),
  });

  const { data: fitnessQuick } = useQuery({
    queryKey: ["fitness-summary-quick"],
    queryFn: () => metrics.getFitnessSummary(false),
  });

  const { data: fitnessFull } = useQuery({
    queryKey: ["fitness-summary"],
    queryFn: () => metrics.getFitnessSummary(true),
    staleTime: 5 * 60 * 1000,
  });

  const fitness = fitnessFull ?? fitnessQuick;

  // Until the numbers actually arrive, show nothing rather than a confident 0.
  // A zero here is indistinguishable from a measurement, and a rider reading
  // "Fitness 0" cannot tell a loading state from a dead backend from a real
  // result. Same rule as the dashboard strip and the sidebar.
  const load = (v: number | null | undefined) =>
    typeof v === "number" ? Math.round(v) : "—";

  const { data: zones } = useQuery({
    queryKey: ["zones"],
    queryFn: () => metrics.getZones(),
  });

  const { data: powerProfile } = useQuery({
    queryKey: ["power-profile"],
    queryFn: () => metrics.getPowerProfile(),
  });

  const weeksToFetch = getWeeklyLoadWeeks(dateRange);
  const { data: weeklyLoad } = useQuery({
    queryKey: ["weekly-load", weeksToFetch],
    queryFn: () => metrics.getWeeklyLoad(weeksToFetch),
  });

  const { data: ftpHistory } = useQuery({
    queryKey: ["ftp-history"],
    queryFn: () => metrics.getFTPHistory(),
  });

  // Filter PMC data based on selected date range
  const filteredPMC = useMemo(() => {
    if (!pmc?.data) return [];
    const cutoff = getDateRangeCutoff(dateRange);
    if (!cutoff) return pmc.data;
    return pmc.data.filter((d) => new Date(d.date) >= cutoff);
  }, [pmc, dateRange]);

  const tsbColor =
    (fitness?.current_tsb ?? 0) > 10
      ? "text-vb-forest"
      : (fitness?.current_tsb ?? 0) < -20
        ? "text-vb-clay"
        : "text-vb-text";

  // Calculate weekly load trend
  const weeklyTrend =
    weeklyLoad && weeklyLoad.weeks.length >= 2
      ? (() => {
          const recent = weeklyLoad.weeks.slice(-2);
          const diff = recent[1].total_tss - recent[0].total_tss;
          return diff > 20 ? "up" : diff < -20 ? "down" : undefined;
        })()
      : undefined;

  const currentWeekTSS =
    weeklyLoad?.weeks.length
      ? Math.round(weeklyLoad.weeks[weeklyLoad.weeks.length - 1].total_tss)
      : 0;

  const currentWeekRides =
    weeklyLoad?.weeks.length
      ? weeklyLoad.weeks[weeklyLoad.weeks.length - 1].ride_count
      : 0;

  // Pillar 1 — Forma reads the numbers on the most data-dense page.
  // Keyed on the rounded TSB so the reading refreshes when form moves,
  // not on every visit.
  const tsbNow = Math.round(fitness?.current_tsb ?? 0);
  const { data: reading } = useQuery({
    queryKey: ["performance-reading", tsbNow],
    queryFn: () =>
      coachInsights.explainMetric(
        "my current form and training load",
        `CTL ${Math.round(fitness?.current_ctl ?? 0)}, ATL ${Math.round(
          fitness?.current_atl ?? 0
        )}, TSB ${tsbNow}, this week ${currentWeekTSS} TSS over ${currentWeekRides} rides`
      ),
    enabled: !!fitness,
    staleTime: 30 * 60 * 1000,
    retry: false,
  });

  return (
    <div className="space-y-6">
      <div>
        <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-vb-text-muted">
          Analysis
        </p>
        <h1 className="mt-1.5 font-display text-4xl font-extrabold leading-[1.04] tracking-[-0.01em] text-vb-text md:text-5xl">
          Performance
        </h1>
        <p className="mt-3 text-sm text-vb-text-dim">
          Track your fitness trends and training load
        </p>
      </div>

      {/* Fitness Stats Row */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <StatCard
          label="FTP"
          value={user?.ftp ?? "-"}
          unit="W"
          explainable="FTP"
        />
        <StatCard
          label="Fitness (CTL)"
          value={load(fitness?.current_ctl)}
          explainable="CTL"
          trend={
            (fitness?.ramp_rate ?? 0) > 0
              ? "up"
              : (fitness?.ramp_rate ?? 0) < 0
                ? "down"
                : undefined
          }
        />
        <StatCard
          label="Fatigue (ATL)"
          value={load(fitness?.current_atl)}
          explainable="ATL"
        />
        <div className="rounded-md border border-vb-border-subtle bg-vb-surface p-4">
          <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-vb-text-muted">
            Form (TSB)
          </p>
          <p className={`mt-2 font-display text-2xl font-semibold tracking-[-0.02em] tabular-nums ${tsbColor}`}>
            {load(fitness?.current_tsb)}
          </p>
        </div>
        <StatCard
          label="This Week"
          value={currentWeekTSS}
          unit={`TSS · ${currentWeekRides} rides`}
          trend={weeklyTrend as "up" | "down" | undefined}
          explainable="Weekly TSS"
        />
      </div>

      {/* Forma reads the trends — no data page goes uncoached */}
      {reading?.explanation && (
        <CoachNote kicker="Forma reads the numbers" coachName={user?.coach_name || "Forma"}>
          {reading.explanation}
        </CoachNote>
      )}

      {/* Date Range Selector */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-medium uppercase tracking-[0.16em] text-vb-text-muted">
          Range
        </span>
        <div className="flex flex-wrap gap-1">
          {DATE_RANGE_OPTIONS.map((option) => (
            <button
              key={option.value}
              onClick={() => setDateRange(option.value)}
              className={`rounded-sm px-3 py-1.5 text-xs font-medium transition-colors ${
                dateRange === option.value
                  ? "bg-vb-forest text-white"
                  : "text-vb-text-dim hover:bg-vb-sunken hover:text-vb-text"
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      {/* PMC Chart */}
      <div className="rounded-md border border-vb-border-subtle bg-vb-surface p-5">
        <h2 className="mb-4 font-display text-lg font-semibold tracking-[-0.01em] text-vb-text">
          Performance Management Chart
        </h2>
        {pmcLoading ? (
          <div className="flex h-80 items-center justify-center">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-vb-forest border-t-transparent" />
          </div>
        ) : filteredPMC.length > 0 ? (
          <div className="h-80">
            <PMCChart data={filteredPMC} dateRange={dateRange} />
          </div>
        ) : (
          <div className="flex h-80 items-center justify-center text-sm text-vb-text-muted">
            No PMC data yet. Upload rides with power data to see your chart.
          </div>
        )}
      </div>

      {/* Weekly Training Load */}
      <div className="rounded-md border border-vb-border-subtle bg-vb-surface p-5">
        <h2 className="mb-4 font-display text-lg font-semibold tracking-[-0.01em] text-vb-text">
          Weekly Training Load
        </h2>
        {weeklyLoad && weeklyLoad.weeks.length > 0 ? (
          <WeeklyLoadChart data={weeklyLoad.weeks} />
        ) : (
          <div className="flex h-64 items-center justify-center text-sm text-vb-text-muted">
            No weekly data yet
          </div>
        )}
      </div>

      {/* Personal Bests Grid */}
      {powerProfile && powerProfile.points.length > 0 && (
        <div className="rounded-md border border-vb-border-subtle bg-vb-surface p-5">
          <PersonalBestsGrid points={powerProfile.points} days={powerProfile.days} />
        </div>
      )}

      {/* Power Curve + Rider Profile Radar (2 columns) */}
      <div className="grid gap-6 lg:grid-cols-2">
        {/* Power Duration Curve */}
        <div className="rounded-md border border-vb-border-subtle bg-vb-surface p-5">
          <h2 className="mb-4 font-display text-lg font-semibold tracking-[-0.01em] text-vb-text">
            Power Duration Curve
          </h2>
          {powerProfile && powerProfile.points.some((p) => p.best_power > 0) ? (
            <div className="h-72">
              <PowerCurveChart data={powerProfile.points} days={powerProfile.days} />
            </div>
          ) : (
            <div className="flex h-64 items-center justify-center text-sm text-vb-text-muted">
              No power data yet
            </div>
          )}
        </div>

        {/* Rider Profile Radar */}
        <div className="rounded-md border border-vb-border-subtle bg-vb-surface p-5">
          <h2 className="mb-4 font-display text-lg font-semibold tracking-[-0.01em] text-vb-text">
            Rider Profile
          </h2>
          {fitness && fitness.profile_scores && fitness.profile_scores.length > 0 ? (
            <RiderProfileRadar
              scores={fitness.profile_scores}
              riderType={fitness.rider_type}
              strengths={fitness.strengths}
              weaknesses={fitness.weaknesses}
            />
          ) : (
            <div className="flex h-64 items-center justify-center text-sm text-vb-text-muted">
              Complete rides with power data to see your profile
            </div>
          )}
        </div>
      </div>

      {/* Power Zones */}
      <div className="rounded-md border border-vb-border-subtle bg-vb-surface p-5">
        <h2 className="mb-4 font-display text-lg font-semibold tracking-[-0.01em] text-vb-text">
          Power Zones
        </h2>
        {zones?.power_zones ? (
          <ZoneDistribution zones={zones.power_zones} />
        ) : (
          <div className="flex h-64 items-center justify-center text-sm text-vb-text-muted">
            Set your FTP in settings to see zones
          </div>
        )}
      </div>

      {/* FTP History */}
      <div className="rounded-md border border-vb-border-subtle bg-vb-surface p-5">
        <h2 className="mb-4 font-display text-lg font-semibold tracking-[-0.01em] text-vb-text">
          FTP History
        </h2>
        {ftpHistory && ftpHistory.history.length > 0 ? (
          <FTPHistoryChart
            data={ftpHistory.history}
            currentFTP={ftpHistory.current_ftp}
          />
        ) : (
          <div className="flex h-48 items-center justify-center text-sm text-vb-text-muted">
            No FTP history yet
          </div>
        )}
      </div>

      {/* Fitness Details (ramp rate, fitness level) */}
      {fitness && fitness.rider_type !== "unknown" && (
        <div className="rounded-md border border-vb-border-subtle bg-vb-surface p-5">
          <h2 className="mb-3 font-display text-lg font-semibold tracking-[-0.01em] text-vb-text">
            Training Status
          </h2>
          <div className="grid gap-4 sm:grid-cols-3">
            <div>
              <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-vb-text-muted">
                Fitness Level
              </p>
              <p className="mt-1 font-display text-lg font-semibold capitalize text-vb-text">
                {fitness.fitness_level.replace("_", " ")}
              </p>
            </div>
            <div>
              <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-vb-text-muted">
                W/kg
              </p>
              <p className="mt-1 font-display text-lg font-semibold tabular-nums text-vb-text">
                {fitness.w_per_kg?.toFixed(2) ?? "-"}
              </p>
            </div>
            <div>
              <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-vb-text-muted">
                Ramp Rate
              </p>
              <p className="mt-1 font-display text-lg font-semibold tabular-nums text-vb-text">
                {fitness.ramp_rate.toFixed(1)}{" "}
                <span className="text-sm font-sans text-vb-text-dim">TSS/day/wk</span>
              </p>
              {fitness.ramp_rate > 7 && (
                <p className="mt-0.5 text-xs text-vb-clay">
                  High ramp rate - monitor recovery
                </p>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
