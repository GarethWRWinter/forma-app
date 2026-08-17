"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ArrowLeft,
  Download,
  Clock,
  Zap,
  ChevronDown,
  RefreshCw,
} from "lucide-react";
import { training, exports_ } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { formatDuration, cn } from "@/lib/utils";
import { STEPS, SERIES } from "@/lib/palette";
import { Arrow, buttonVariants } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Kicker } from "@/components/ui/kicker";
import { ZoneChip } from "@/components/ui/zone-chip";
import { CoachNote } from "@/components/ui/coach-note";

const EXPORT_FORMATS = [
  { key: "zwo", label: "ZWO", desc: "Zwift" },
  { key: "erg", label: "ERG", desc: "Wahoo / TrainerRoad" },
  { key: "mrc", label: "MRC", desc: "% FTP format" },
  { key: "fit", label: "FIT", desc: "Garmin / Hammerhead" },
] as const;

export default function WorkoutDetailPage() {
  const params = useParams();
  const workoutId = params.id as string;
  const { user } = useAuth();
  const queryClient = useQueryClient();
  // No FTP means no watts. We never substitute a number and present it as the
  // rider's own: a fabricated FTP turns a guess into an apparent measurement,
  // and the same figure drives ERG resistance and the exported workout files.
  const ftp = user?.ftp ?? null;
  const watts = (pct: number | null | undefined) =>
    pct && ftp ? Math.round(pct * ftp) : null;
  const coachName = user?.coach_name || "Forma";
  const [showExportMenu, setShowExportMenu] = useState(false);
  const [downloading, setDownloading] = useState<string | null>(null);

  const { data: workout, isLoading } = useQuery({
    queryKey: ["workout", workoutId],
    queryFn: () => training.getWorkout(workoutId),
  });

  const hasActualRide = !!workout?.actual_ride_id;

  // Auto-generate assessment the first time we view a workout with a linked ride.
  const { data: assessment, isFetching: assessmentLoading } = useQuery({
    queryKey: ["workout-assessment", workoutId],
    queryFn: () => training.getWorkoutAssessment(workoutId),
    enabled: hasActualRide,
  });

  const regenerateAssessment = useMutation({
    mutationFn: () => training.getWorkoutAssessment(workoutId, true),
    onSuccess: (data) => {
      queryClient.setQueryData(["workout-assessment", workoutId], data);
      queryClient.invalidateQueries({ queryKey: ["workout", workoutId] });
      queryClient.invalidateQueries({ queryKey: ["workouts-week"] });
    },
  });

  const handleDownload = async (format: string) => {
    if (!workout) return;
    setDownloading(format);
    setShowExportMenu(false);
    try {
      const fn =
        format === "zwo"
          ? exports_.downloadZWO
          : format === "erg"
            ? exports_.downloadERG
            : format === "mrc"
              ? exports_.downloadMRC
              : exports_.downloadFIT;
      await fn(workoutId, workout.title);
    } catch (e) {
      console.error("Download failed:", e);
    } finally {
      setDownloading(null);
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-vb-red border-t-transparent" />
      </div>
    );
  }

  if (!workout) {
    return (
      <div className="py-20 text-center text-vb-text-dim">
        That workout isn&apos;t here. Back to the calendar and try again.
      </div>
    );
  }

  // Calculate total duration from steps
  const totalFromSteps =
    workout.steps?.reduce((sum, s) => {
      const repeats = s.repeat_count || 1;
      return sum + s.duration_seconds * repeats;
    }, 0) || 0;

  return (
    <div className="f-rise mx-auto max-w-3xl space-y-6">
      {/* Header */}
      <div>
        <Link
          href="/dashboard/training"
          className="mb-3 inline-flex items-center gap-1 font-mono text-xs uppercase tracking-[0.08em] text-vb-text-dim hover:text-vb-red"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> Back to training
        </Link>
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="f-display text-3xl text-vb-text">{workout.title}</h1>
            <p className="mt-1 text-sm text-vb-text-dim">
              {workout.description}
            </p>
            <div className="mt-3 flex flex-wrap items-center gap-3">
              <ZoneChip zone={workout.workout_type} />
              <Badge
                variant={
                  workout.status === "completed"
                    ? "ink"
                    : workout.status === "skipped"
                      ? "chalk"
                      : "outline"
                }
              >
                {workout.status}
              </Badge>
              <span className="f-data flex items-center gap-1 text-sm text-vb-text-dim">
                <Clock className="h-4 w-4" />
                {workout.planned_duration_seconds
                  ? formatDuration(workout.planned_duration_seconds)
                  : "N/A"}
              </span>
              {workout.planned_tss && (
                <span className="f-data flex items-center gap-1 text-sm text-vb-text-dim">
                  <Zap className="h-4 w-4" />
                  {Math.round(workout.planned_tss)} TSS
                </span>
              )}
            </div>
          </div>

          {/* Action buttons */}
          <div className="flex shrink-0 items-center gap-2">
            {/* Start Session button */}
            {workout.status === "planned" && workout.steps && workout.steps.length > 0 && (
              <Link
                href={`/dashboard/training/${workoutId}/session`}
                className={buttonVariants({ variant: "flamme" })}
              >
                Start Session <Arrow />
              </Link>
            )}

            {/* Export dropdown */}
            {workout.steps && workout.steps.length > 0 && (
              <div className="relative">
                <button
                  onClick={() => setShowExportMenu(!showExportMenu)}
                  className={buttonVariants({ variant: "ghost" })}
                >
                  <Download className="h-4 w-4" />
                  {downloading ? "…" : "Export"}
                  <ChevronDown className="h-3 w-3" />
                </button>

                {showExportMenu && (
                  <>
                    <div
                      className="fixed inset-0 z-10"
                      onClick={() => setShowExportMenu(false)}
                    />
                    <div className="absolute right-0 top-full z-20 mt-1 w-52 rounded-sm border border-vb-border bg-vb-surface py-1">
                      {EXPORT_FORMATS.map((fmt) => (
                        <button
                          key={fmt.key}
                          onClick={() => handleDownload(fmt.key)}
                          disabled={!!downloading}
                          className="flex w-full items-center justify-between px-4 py-2 text-left text-sm transition-colors hover:bg-vb-sunken disabled:opacity-50"
                        >
                          <span className="font-mono text-xs font-semibold text-vb-text">
                            .{fmt.label}
                          </span>
                          <span className="text-xs text-vb-text-dim">
                            {fmt.desc}
                          </span>
                        </button>
                      ))}
                    </div>
                  </>
                )}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Planned vs Actual, shown when a ride has been linked to this workout */}
      {hasActualRide && workout.actual_ride && (
        <div className="rounded-sm border border-vb-border-subtle bg-vb-surface p-5">
          <div className="mb-4 flex items-center justify-between gap-2">
            <Kicker>Plan vs. the ride</Kicker>
            {assessment?.score != null && (
              <span
                className={cn(
                  "f-data text-sm font-semibold",
                  assessment.score >= 8
                    ? "text-vb-success"
                    : assessment.score >= 6
                      ? "text-vb-text-dim"
                      : "text-vb-red"
                )}
              >
                {assessment.score.toFixed(1)}/10
              </span>
            )}
          </div>

          {/* Stat comparison grid */}
          <div className="f-stagger grid grid-cols-2 gap-2 sm:grid-cols-4 sm:gap-3">
            <StatCompare
              label="Duration"
              planned={
                workout.planned_duration_seconds
                  ? formatDuration(workout.planned_duration_seconds)
                  : "·"
              }
              actual={
                workout.actual_ride.moving_time_seconds ||
                workout.actual_ride.duration_seconds
                  ? formatDuration(
                      (workout.actual_ride.moving_time_seconds ??
                        workout.actual_ride.duration_seconds)!
                    )
                  : "·"
              }
            />
            <StatCompare
              label="TSS"
              planned={
                workout.planned_tss != null
                  ? Math.round(workout.planned_tss).toString()
                  : "·"
              }
              actual={
                workout.actual_ride.tss != null
                  ? Math.round(workout.actual_ride.tss).toString()
                  : "·"
              }
            />
            <StatCompare
              label="IF"
              planned={
                workout.planned_if != null
                  ? workout.planned_if.toFixed(2)
                  : "·"
              }
              actual={
                workout.actual_ride.intensity_factor != null
                  ? workout.actual_ride.intensity_factor.toFixed(2)
                  : "·"
              }
            />
            <StatCompare
              label="NP"
              planned="·"
              actual={
                workout.actual_ride.normalized_power != null
                  ? `${Math.round(workout.actual_ride.normalized_power)}W`
                  : "·"
              }
            />
          </div>

          {/* Forma's debrief */}
          <CoachNote
            className="mt-5"
            kicker={`${coachName}'s debrief`}
            coachName={coachName}
            signature={!!assessment?.feedback}
            action={
              <button
                onClick={() => regenerateAssessment.mutate()}
                disabled={
                  regenerateAssessment.isPending || assessmentLoading
                }
                className="flex items-center gap-1 rounded-sm font-mono text-[10px] uppercase tracking-[0.08em] text-vb-text-muted hover:text-vb-red disabled:opacity-50"
                title="Ask again"
              >
                <RefreshCw
                  className={cn(
                    "h-3 w-3",
                    (regenerateAssessment.isPending || assessmentLoading) &&
                      "animate-spin"
                  )}
                />
                Ask again
              </button>
            }
          >
            {assessmentLoading && !assessment ? (
              <p className="text-sm text-vb-text-dim">
                Forma is reading your ride…
              </p>
            ) : assessment?.feedback ? (
              <div className="prose prose-sm max-w-none prose-p:text-vb-text">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {assessment.feedback}
                </ReactMarkdown>
              </div>
            ) : (
              <p className="text-sm text-vb-text-muted">
                Nothing filed yet. Give me a moment with the numbers.
              </p>
            )}

            {assessment?.adjustments && assessment.adjustments.length > 0 && (
              <div className="mt-4 border-t border-vb-border-subtle pt-3">
                <Kicker className="mb-2">What changes</Kicker>
                <ul className="space-y-2">
                  {assessment.adjustments.map((adj, i) => (
                    <li
                      key={i}
                      className="rounded-sm border border-vb-border-subtle bg-vb-bg p-2.5 text-xs text-vb-text-dim"
                    >
                      {adj.date && (
                        <p className="f-data mb-0.5 font-medium text-vb-text">
                          {adj.date}
                        </p>
                      )}
                      {adj.change && <p>{adj.change}</p>}
                      {adj.reason && (
                        <p className="mt-1 text-[10px] text-vb-text-muted">
                          {adj.reason}
                        </p>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </CoachNote>
        </div>
      )}

      {/* Visual Workout Profile */}
      {workout.steps && workout.steps.length > 0 && (
        <div className="rounded-sm border border-vb-border-subtle bg-vb-surface p-5">
          <Kicker className="mb-4">The shape of the session</Kicker>

          {/* Visual bar chart of steps */}
          <div className="relative">
            {/* FTP hairline reference at 100% FTP (80% of chart height) */}
            <div
              className="pointer-events-none absolute inset-x-0 border-t border-dashed"
              style={{ top: "20%", borderColor: SERIES.hairline }}
            />
            <div className="flex h-32 items-end gap-0.5">
              {(() => {
                const bars: React.ReactNode[] = [];
                const steps = workout.steps || [];

                for (let si = 0; si < steps.length; si++) {
                  const step = steps[si];
                  const repeats = step.repeat_count || 1;
                  const powerPct = step.power_target_pct || 0.5;
                  const heightPct = Math.max(20, Math.min(100, powerPct * 80));
                  const widthPct =
                    ((step.duration_seconds * repeats) / totalFromSteps) * 100;

                  // For interval_on with repeats, interleave with the next interval_off step
                  if (
                    step.step_type === "interval_on" &&
                    step.repeat_count &&
                    step.repeat_count > 1
                  ) {
                    // Find the paired interval_off (next step)
                    const offStep = si + 1 < steps.length && steps[si + 1].step_type === "interval_off"
                      ? steps[si + 1]
                      : null;
                    const offPowerPct = offStep?.power_target_pct || 0.4;
                    const offHeightPct = Math.max(20, Math.min(100, offPowerPct * 80));
                    const offWidthPct = offStep
                      ? ((offStep.duration_seconds) / totalFromSteps) * 100
                      : 0;

                    for (let r = 0; r < repeats; r++) {
                      // On bar
                      bars.push(
                        <div
                          key={`${step.id}-on-${r}`}
                          className="transition-all"
                          style={{
                            backgroundColor: STEPS.interval_on,
                            height: `${heightPct}%`,
                            width: `${widthPct / repeats}%`,
                            minWidth: "2px",
                          }}
                          title={`${step.notes || "Interval"}: ${Math.round(powerPct * 100)}% FTP${watts(powerPct) ? ` (${watts(powerPct)}W)` : ""}`}
                        />
                      );
                      // Off bar (between reps, not after the last one)
                      if (offStep && r < repeats - 1) {
                        bars.push(
                          <div
                            key={`${step.id}-off-${r}`}
                            className="transition-all"
                            style={{
                              backgroundColor: STEPS.interval_off,
                              height: `${offHeightPct}%`,
                              width: `${offWidthPct}%`,
                              minWidth: "2px",
                            }}
                            title={`Recovery: ${Math.round(offPowerPct * 100)}% FTP${watts(offPowerPct) ? ` (${watts(offPowerPct)}W)` : ""}`}
                          />
                        );
                      }
                    }

                    // Skip the interval_off step since we already rendered it inline
                    if (offStep) si++;
                    continue;
                  }

                  bars.push(
                    <div
                      key={step.id}
                      className="transition-all"
                      style={{
                        backgroundColor:
                          STEPS[step.step_type] ?? SERIES.chalk,
                        height: `${heightPct}%`,
                        width: `${widthPct}%`,
                        minWidth: "4px",
                      }}
                      title={`${step.notes || step.step_type}: ${Math.round(powerPct * 100)}% FTP${watts(powerPct) ? ` (${watts(powerPct)}W)` : ""}`}
                    />
                  );
                }
                return bars;
              })()}
            </div>
          </div>

          <p className="f-data mt-1 text-right text-[10px] text-vb-text-muted">
            {ftp ? (
              `FTP ${ftp}W`
            ) : (
              <>
                Percentages only.{" "}
                <Link
                  href="/dashboard/settings"
                  className="underline underline-offset-2 hover:text-vb-text"
                >
                  Set your FTP
                </Link>{" "}
                to see watts.
              </>
            )}
          </p>
        </div>
      )}

      {/* Step Details */}
      {workout.steps && workout.steps.length > 0 && (
        <div className="rounded-sm border border-vb-border-subtle bg-vb-surface">
          <div className="border-b border-vb-border-subtle px-5 py-3">
            <Kicker>Step by step</Kicker>
          </div>
          <div className="divide-y divide-vb-border-subtle">
            {workout.steps.map((step) => {
              const powerW = watts(step.power_target_pct);
              const lowW = watts(step.power_low_pct);
              const highW = watts(step.power_high_pct);

              return (
                <div key={step.id} className="flex items-center gap-4 px-5 py-3">
                  <div
                    className="h-8 w-1.5 rounded-full"
                    style={{
                      backgroundColor:
                        STEPS[step.step_type] ?? SERIES.chalk,
                    }}
                  />
                  <div className="flex-1">
                    <div className="flex items-center gap-2">
                      <p className="font-mono text-xs font-semibold uppercase tracking-[0.08em] text-vb-text">
                        {step.step_type.replace("_", " ")}
                      </p>
                      {step.repeat_count && step.repeat_count > 1 && (
                        <span className="f-data rounded-sm bg-vb-sunken px-1.5 py-0.5 text-[10px] text-vb-text-dim">
                          x{step.repeat_count}
                        </span>
                      )}
                    </div>
                    {step.notes && (
                      <p className="text-xs text-vb-text-dim">{step.notes}</p>
                    )}
                  </div>
                  <div className="text-right">
                    <p className="f-data text-sm font-semibold text-vb-text">
                      {formatDuration(step.duration_seconds)}
                    </p>
                    <p className="f-data text-xs text-vb-text-dim">
                      {lowW && highW
                        ? `${lowW}-${highW}W`
                        : powerW
                          ? `${powerW}W`
                          : ""}
                      {step.power_target_pct &&
                        ` (${Math.round(step.power_target_pct * 100)}%)`}
                    </p>
                    {step.cadence_target && (
                      <p className="f-data text-[10px] text-vb-text-muted">
                        {step.cadence_target} rpm
                      </p>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function StatCompare({
  label,
  planned,
  actual,
}: {
  label: string;
  planned: string;
  actual: string;
}) {
  return (
    <div className="rounded-sm border border-vb-border-subtle bg-vb-surface p-3">
      <p className="f-kicker text-vb-text-muted">{label}</p>
      <div className="mt-2 space-y-1">
        <div className="flex items-baseline justify-between gap-2">
          <span className="f-kicker text-[9px] text-vb-text-muted">Plan</span>
          <span className="f-data text-sm text-vb-text-dim">{planned}</span>
        </div>
        <div className="flex items-baseline justify-between gap-2">
          <span className="f-kicker text-[9px] text-vb-red">Did</span>
          <span className="f-data text-sm font-semibold text-vb-text">
            {actual}
          </span>
        </div>
      </div>
    </div>
  );
}
