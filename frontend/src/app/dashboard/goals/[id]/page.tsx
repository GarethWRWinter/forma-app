"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { useState, useRef, useEffect, useMemo } from "react";
import {
  ArrowLeft,
  MapPin,
  Mountain,
  ExternalLink,
  Pencil,
  Trash2,
  Upload,
  TrendingUp,
  X,
} from "lucide-react";
import { goals as goalsApi, metrics, training, rides } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { cn, formatDate } from "@/lib/utils";
import { ElevationProfileChart } from "@/components/charts/elevation-profile-chart";
import { PerformanceCards } from "@/components/race-projection/performance-cards";
import { FitnessTrajectoryChart } from "@/components/charts/fitness-trajectory-chart";
import { Button, Arrow, buttonVariants } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Kicker } from "@/components/ui/kicker";
import { SectionHeader } from "@/components/ui/section-header";
import { DataTile } from "@/components/ui/data-tile";
import { CoachNote } from "@/components/ui/coach-note";
import { Input } from "@/components/ui/input";
import type { GoalEvent } from "@/lib/api";

const EVENT_TYPES = [
  { value: "road_race", label: "Road Race" },
  { value: "crit", label: "Criterium" },
  { value: "time_trial", label: "Time Trial" },
  { value: "gran_fondo", label: "Gran Fondo" },
  { value: "sportive", label: "Sportive" },
  { value: "gravel", label: "Gravel" },
  { value: "mtb", label: "MTB" },
  { value: "hill_climb", label: "Hill Climb" },
  { value: "stage_race", label: "Stage Race" },
  { value: "charity_ride", label: "Charity Ride" },
  { value: "century", label: "Century" },
];

const PRIORITIES = [
  { value: "a_race", label: "A race (the one that matters)" },
  { value: "b_race", label: "B race (a marker on the way)" },
  { value: "c_race", label: "C race (training with a number on)" },
];

function formatDurationMinutes(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (h > 0) return m > 0 ? `${h}h ${m}m` : `${h}h`;
  return `${m}m`;
}

function priorityLabel(priority: string): string {
  if (priority === "a_race") return "A race";
  if (priority === "b_race") return "B race";
  if (priority === "c_race") return "C race";
  return priority.replace(/_/g, " ");
}

function readinessColor(label: string): string {
  switch (label) {
    case "On Track":
      return "text-vb-success";
    case "Needs Work":
      return "text-vb-warning";
    case "At Risk":
      return "text-vb-red";
    default:
      return "text-vb-text-dim";
  }
}

const selectClasses =
  "flex h-11 w-full rounded-sm border border-vb-border bg-vb-surface px-3 py-2 text-sm text-vb-text focus:border-vb-red focus:outline-none focus:ring-1 focus:ring-vb-red";

const textareaClasses =
  "w-full rounded-sm border border-vb-border bg-vb-surface px-3 py-2 text-sm text-vb-text placeholder:text-vb-text-muted focus:border-vb-red focus:outline-none focus:ring-1 focus:ring-vb-red resize-none";

/** Mono tile matching DataTile visuals, for formatted string values. */
function MonoTile({
  label,
  display,
  sub,
  className,
}: {
  label: string;
  display: string;
  sub?: string;
  className?: string;
}) {
  return (
    <div
      className={cn("border border-vb-border-subtle bg-vb-surface p-4", className)}
    >
      <p className="f-kicker text-vb-text-muted">{label}</p>
      <p className="f-data mt-2 text-4xl font-semibold leading-none text-vb-text">
        {display}
      </p>
      {sub && <p className="mt-2 text-xs text-vb-text-dim">{sub}</p>}
    </div>
  );
}

export default function GoalDetailPage() {
  const params = useParams();
  const router = useRouter();
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const goalId = params.id as string;
  const gpxInputRef = useRef<HTMLInputElement>(null);
  const coachName = user?.coach_name || "Forma";

  const [showEdit, setShowEdit] = useState(false);
  const [editForm, setEditForm] = useState({
    event_name: "",
    event_date: "",
    event_type: "road_race",
    priority: "a_race",
    target_duration_minutes: "",
    notes: "",
    route_url: "",
  });

  const { data: goal, isLoading } = useQuery({
    queryKey: ["goal", goalId],
    queryFn: () => goalsApi.get(goalId),
  });

  const { data: readiness } = useQuery({
    queryKey: ["goal-readiness", goalId],
    queryFn: () => goalsApi.getReadiness(goalId),
    enabled: !!goal && goal.days_until != null && goal.days_until > 0,
    retry: false,
  });

  const { data: fitness } = useQuery({
    queryKey: ["fitness-summary-quick"],
    queryFn: () => metrics.getFitnessSummary(false),
  });

  const { data: projection } = useQuery({
    queryKey: ["race-projection", goalId],
    queryFn: () => goalsApi.getRaceProjection(goalId),
    enabled:
      !!goal?.route_data?.elevation_profile &&
      goal.route_data.elevation_profile.length > 0,
    retry: false,
  });

  // Fetch actual ride data for completed goals with route data
  const { data: actualRideData } = useQuery({
    queryKey: ["ride-data", goal?.actual_ride_id],
    queryFn: () => rides.getData(goal!.actual_ride_id!, "30s"),
    enabled: !!goal?.actual_ride_id && (goal?.route_data?.elevation_profile?.length ?? 0) > 0,
  });

  const actualForChart = useMemo(() => {
    if (!actualRideData?.data_points) return undefined;
    return actualRideData.data_points
      .filter((dp) => dp.distance != null)
      .map((dp) => ({
        distance_km: dp.distance! / 1000,
        power: dp.power,
        speed: dp.speed ? dp.speed * 3.6 : null, // m/s → km/h
      }));
  }, [actualRideData]);

  const [showTargetPower, setShowTargetPower] = useState(true);
  const [showActualPower, setShowActualPower] = useState(true);

  const updateGoal = useMutation({
    mutationFn: () => {
      const data: Record<string, unknown> = {
        event_name: editForm.event_name,
        event_date: editForm.event_date,
        event_type: editForm.event_type,
        priority: editForm.priority,
      };
      if (editForm.target_duration_minutes) {
        data.target_duration_minutes = parseInt(editForm.target_duration_minutes);
      } else {
        data.target_duration_minutes = null;
      }
      data.notes = editForm.notes || null;
      data.route_url = editForm.route_url || null;
      return goalsApi.update(goalId, data as Parameters<typeof goalsApi.update>[1]);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["goal", goalId] });
      queryClient.invalidateQueries({ queryKey: ["goals"] });
      setShowEdit(false);
    },
  });

  const deleteGoalMutation = useMutation({
    mutationFn: () => goalsApi.delete(goalId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["goals"] });
      router.push("/dashboard/goals");
    },
  });

  const uploadGpx = useMutation({
    mutationFn: (file: File) => goalsApi.uploadGpx(goalId, file),
    onSuccess: (data) => {
      // Set the cache directly with the response (which now includes parsed route_data)
      queryClient.setQueryData(["goal", goalId], data);
      queryClient.invalidateQueries({ queryKey: ["goals"] });
    },
  });

  const generatePlan = useMutation({
    mutationFn: () =>
      training.generatePlan({ goal_event_id: goalId }),
    onSuccess: () => {
      router.push("/dashboard/training");
    },
  });

  const deleteGpx = useMutation({
    mutationFn: () => goalsApi.deleteGpx(goalId),
    onSuccess: (data) => {
      queryClient.setQueryData(["goal", goalId], data);
      queryClient.invalidateQueries({ queryKey: ["goals"] });
    },
  });

  const reparseGpx = useMutation({
    mutationFn: () => goalsApi.reparseGpx(goalId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["goal", goalId] });
    },
  });

  const refetchRoute = useMutation({
    mutationFn: () => goalsApi.refetchRoute(goalId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["goal", goalId] });
    },
  });

  // Auto-reparse GPX if file exists but elevation profile is missing
  const [autoRefreshed, setAutoRefreshed] = useState(false);
  useEffect(() => {
    if (!goal || autoRefreshed) return;

    // Case 1: GPX file exists but elevation profile is missing → reparse
    if (
      goal.gpx_file_path &&
      (!goal.route_data?.elevation_profile ||
        goal.route_data.elevation_profile.length === 0) &&
      !reparseGpx.isPending
    ) {
      setAutoRefreshed(true);
      reparseGpx.mutate();
      return;
    }

    // Case 2: Route URL exists but route_data is missing distance/elevation → refetch
    if (
      goal.route_url &&
      !goal.gpx_file_path &&
      goal.route_data &&
      !goal.route_data.total_distance_km &&
      !refetchRoute.isPending
    ) {
      setAutoRefreshed(true);
      refetchRoute.mutate();
      return;
    }
  }, [goal, autoRefreshed, reparseGpx.isPending, refetchRoute.isPending]);

  function openEdit() {
    if (!goal) return;
    setEditForm({
      event_name: goal.event_name,
      event_date: goal.event_date,
      event_type: goal.event_type,
      priority: goal.priority,
      target_duration_minutes: goal.target_duration_minutes?.toString() || "",
      notes: goal.notes || "",
      route_url: goal.route_url || "",
    });
    setShowEdit(true);
  }

  function buildCoachMessage(): string {
    if (!goal) return "";
    const parts = [
      `I'd like your help preparing for ${goal.event_name} on ${formatDate(goal.event_date)}.`,
      `It's a ${goal.event_type.replace(/_/g, " ")} (${goal.priority.replace(/_/g, " ")}).`,
    ];
    if (goal.route_data && !goal.route_data.error) {
      if (goal.route_data.total_distance_km) {
        parts.push(`The route is ${goal.route_data.total_distance_km}km`);
      }
      if (goal.route_data.elevation_gain_m) {
        parts.push(`with ${goal.route_data.elevation_gain_m}m of climbing.`);
      }
    }
    if (goal.target_duration_minutes) {
      parts.push(
        `My target duration is ${formatDurationMinutes(goal.target_duration_minutes)}.`
      );
    }
    if (goal.days_until != null && goal.days_until > 0) {
      parts.push(`I have ${goal.days_until} days to prepare.`);
    }
    parts.push("What should my preparation look like?");
    return parts.join(" ");
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="h-8 w-8 animate-spin rounded-full border border-vb-border-subtle border-t-vb-red" />
      </div>
    );
  }

  if (!goal) {
    return (
      <div className="py-20 text-center text-vb-text-dim">Goal not found</div>
    );
  }

  return (
    <div className="space-y-8">
      {/* ============ MASTHEAD ============ */}
      <header className="f-rise border-b-2 border-vb-border-strong pb-5">
        <Link
          href="/dashboard/goals"
          className="mb-3 inline-flex items-center gap-1 font-mono text-[11px] uppercase tracking-[0.08em] text-vb-text-dim transition-colors hover:text-vb-red"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> The calendar
        </Link>
        <div className="flex flex-wrap items-end justify-between gap-6">
          <div className="min-w-0">
            <div className="mb-2 flex flex-wrap items-center gap-3">
              <Kicker>{goal.event_type.replace(/_/g, " ")}</Kicker>
              <Badge
                variant={goal.priority === "a_race" ? "flamme" : "outline"}
              >
                {priorityLabel(goal.priority)}
              </Badge>
            </div>
            <h1 className="f-display text-4xl text-vb-text md:text-5xl">
              {goal.event_name}
            </h1>
            <p className="f-kicker mt-3 text-vb-text-muted">
              {formatDate(goal.event_date)}
            </p>
          </div>
          <div className="flex items-end gap-5">
            {/* Countdown */}
            {goal.days_until != null && goal.days_until >= 0 && (
              <div className="text-right">
                <p
                  className={cn(
                    "f-data text-5xl font-semibold leading-none",
                    goal.days_until < 14 ? "text-vb-red" : "text-vb-text"
                  )}
                >
                  {goal.days_until}
                </p>
                <Kicker className="mt-1.5 justify-end">
                  {goal.days_until === 0 ? "Race day" : "Days to go"}
                </Kicker>
              </div>
            )}
            <div className="flex items-center gap-2">
              <Button variant="ghost" size="sm" onClick={openEdit}>
                <Pencil className="h-3.5 w-3.5" /> Edit
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  if (confirm(`Delete "${goal.event_name}"?`)) {
                    deleteGoalMutation.mutate();
                  }
                }}
                className="hover:border-vb-red hover:text-vb-red"
              >
                <Trash2 className="h-3.5 w-3.5" /> Delete
              </Button>
            </div>
          </div>
        </div>
      </header>

      {/* The soul of the goal, written at goalcraft */}
      {(goal.why || goal.becoming) && (
        <div className="border-l-[3px] border-l-vb-red bg-vb-surface px-5 py-4">
          <Kicker className="mb-2">Why this one</Kicker>
          {goal.why && (
            <p className="max-w-2xl text-sm leading-relaxed text-vb-text">
              {goal.why}
            </p>
          )}
          {goal.becoming && (
            <p className="f-data mt-2 text-xs uppercase tracking-wider text-vb-text-muted">
              Becoming · {goal.becoming}
            </p>
          )}
        </div>
      )}
      {!goal.why && !goal.becoming && goal.status === "upcoming" && (
        <div className="border border-dashed border-vb-border px-5 py-4">
          <p className="text-sm text-vb-text-dim">
            This goal has a date and a route, but no why yet. The why is the
            fuel that survives February.{" "}
            <Link
              href={`/dashboard/coach?ask=${encodeURIComponent(
                `Let's talk about my goal "${goal.event_name}". Ask me one question at a time and help me find why this one matters to me and who it's turning me into. Then save it to the goal.`
              )}`}
              className="text-vb-red underline-offset-4 hover:underline"
            >
              Craft it with Forma <span className="f-arrow-head">→</span>
            </Link>
          </p>
        </div>
      )}

      {/* Assessment banner, needs assessment */}
      {goal.needs_assessment && (
        <CoachNote
          kicker="Race report pending"
          signature={false}
          action={
            <Link
              href={`/dashboard/goals/${goalId}/assess`}
              className={buttonVariants({ variant: "flamme", size: "sm" })}
            >
              File the report
              <Arrow />
            </Link>
          }
        >
          So, how did {goal.event_name} go? Give me the result, link the ride
          file, and this race starts working for the next one.
        </CoachNote>
      )}

      {/* Assessment results, completed goals */}
      {goal.assessment_completed_at && (
        <div className="border border-vb-border-subtle bg-vb-surface p-5">
          <div className="flex items-center justify-between">
            <Kicker>The result</Kicker>
            <Badge variant={goal.status === "completed" ? "ink" : "outline"}>
              {goal.status === "dns"
                ? "DNS"
                : goal.status === "dnf"
                  ? "DNF"
                  : "Completed"}
            </Badge>
          </div>
          <div className="f-stagger mt-4 grid grid-cols-2 gap-px sm:grid-cols-4">
            {goal.finish_time_seconds && (
              <MonoTile
                label="Finish time"
                display={`${Math.floor(goal.finish_time_seconds / 3600)}:${String(
                  Math.floor((goal.finish_time_seconds % 3600) / 60)
                ).padStart(2, "0")}:${String(goal.finish_time_seconds % 60).padStart(2, "0")}`}
              />
            )}
            {goal.finish_position && (
              <MonoTile
                label="Position"
                display={`${goal.finish_position}`}
                sub={
                  goal.finish_position_total
                    ? `of ${goal.finish_position_total}`
                    : undefined
                }
              />
            )}
            {goal.overall_satisfaction && (
              <DataTile
                label="Satisfaction"
                value={goal.overall_satisfaction}
                unit="/10"
              />
            )}
            {goal.perceived_exertion && (
              <DataTile
                label="RPE"
                value={goal.perceived_exertion}
                unit="/10"
              />
            )}
          </div>

          {/* Takeaways */}
          {goal.assessment_data && (() => {
            const ad = goal.assessment_data as Record<string, unknown>;
            return (
              <div className="mt-4 grid gap-3 sm:grid-cols-2">
                {ad.went_well ? (
                  <div className="border border-vb-border-subtle bg-vb-sunken p-3">
                    <Kicker className="mb-1">What went well</Kicker>
                    <p className="text-sm text-vb-text-dim">
                      {String(ad.went_well)}
                    </p>
                  </div>
                ) : null}
                {ad.to_improve ? (
                  <div className="border border-vb-border-subtle border-l-[3px] border-l-vb-red bg-vb-surface p-3">
                    <Kicker flamme className="mb-1">
                      What to change
                    </Kicker>
                    <p className="text-sm text-vb-text-dim">
                      {String(ad.to_improve)}
                    </p>
                  </div>
                ) : null}
              </div>
            );
          })()}

          {/* The finish line gets coached like the start line */}
          <p className="mt-4 border-t border-vb-border-subtle pt-4 text-sm text-vb-text-dim">
            The result is filed. The bigger question is what the pursuit made
            of you.{" "}
            <Link
              href={`/dashboard/coach?ask=${encodeURIComponent(
                `Let's talk about how ${goal.event_name} went, beyond the numbers. Help me see how far I've come since I set it, what the pursuit made of me, and whether I'm ready to think about what comes next. Be honest with me.`
              )}`}
              className="text-vb-red underline-offset-4 hover:underline"
            >
              Talk it through with Forma <span className="f-arrow-head">→</span>
            </Link>
          </p>
        </div>
      )}

      {/* Edit Form */}
      {showEdit && (
        <div className="f-rise border border-vb-border-subtle bg-vb-surface p-5">
          <div className="mb-4 flex items-start justify-between">
            <div>
              <Kicker className="mb-1.5">Edit goal</Kicker>
              <h2 className="f-display text-2xl text-vb-text">
                Change the target
              </h2>
            </div>
            <button
              onClick={() => setShowEdit(false)}
              className="rounded-sm p-1 text-vb-text-muted hover:text-vb-text"
            >
              <X className="h-5 w-5" />
            </button>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <Kicker className="mb-2">What&apos;s the event?</Kicker>
              <Input
                value={editForm.event_name}
                onChange={(e) =>
                  setEditForm({ ...editForm, event_name: e.target.value })
                }
              />
            </div>
            <div>
              <Kicker className="mb-2">When is race day?</Kicker>
              <Input
                type="date"
                value={editForm.event_date}
                onChange={(e) =>
                  setEditForm({ ...editForm, event_date: e.target.value })
                }
              />
            </div>
            <div>
              <Kicker className="mb-2">What kind of race?</Kicker>
              <select
                value={editForm.event_type}
                onChange={(e) =>
                  setEditForm({ ...editForm, event_type: e.target.value })
                }
                className={selectClasses}
              >
                {EVENT_TYPES.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <Kicker className="mb-2">How much does it matter?</Kicker>
              <select
                value={editForm.priority}
                onChange={(e) =>
                  setEditForm({ ...editForm, priority: e.target.value })
                }
                className={selectClasses}
              >
                {PRIORITIES.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <Kicker className="mb-2">Target time, in minutes</Kicker>
              <Input
                type="number"
                value={editForm.target_duration_minutes}
                onChange={(e) =>
                  setEditForm({
                    ...editForm,
                    target_duration_minutes: e.target.value,
                  })
                }
                placeholder="e.g. 240"
              />
            </div>
            <div>
              <Kicker className="mb-2">Where does the route live?</Kicker>
              <Input
                type="url"
                value={editForm.route_url}
                onChange={(e) =>
                  setEditForm({ ...editForm, route_url: e.target.value })
                }
              />
            </div>
          </div>
          <div className="mt-4">
            <Kicker className="mb-2">Anything I should know?</Kicker>
            <textarea
              value={editForm.notes}
              onChange={(e) =>
                setEditForm({ ...editForm, notes: e.target.value })
              }
              rows={3}
              className={textareaClasses}
            />
          </div>
          <div className="mt-4 flex gap-2">
            <Button
              onClick={() => updateGoal.mutate()}
              disabled={!editForm.event_name || !editForm.event_date || updateGoal.isPending}
            >
              {updateGoal.isPending ? "Saving…" : "Save changes"}
            </Button>
            <Button variant="ghost" onClick={() => setShowEdit(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      {/* ============ STATS ROW ============ */}
      <div className="f-stagger grid grid-cols-2 gap-px md:grid-cols-4">
        {goal.target_duration_minutes && (
          <MonoTile
            label="Target time"
            display={formatDurationMinutes(goal.target_duration_minutes)}
          />
        )}
        {goal.days_until != null && goal.days_until > 0 && (
          <DataTile label="Days to go" value={goal.days_until} unit="days" />
        )}
        {goal.route_data?.total_distance_km && (
          <DataTile
            label="Distance"
            value={goal.route_data.total_distance_km}
            unit="km"
            decimals={Number.isInteger(goal.route_data.total_distance_km) ? 0 : 1}
          />
        )}
        {goal.route_data?.elevation_gain_m && (
          <DataTile
            label="Climbing"
            value={Math.round(goal.route_data.elevation_gain_m)}
            unit="m"
          />
        )}
      </div>

      {/* ============ RACE DAY PROJECTION ============ */}
      {projection && (
        <div>
          <SectionHeader kicker="Race day" title="The projection" />
          <div className="space-y-4">
            <PerformanceCards projection={projection} daysUntil={goal.days_until} />
            {projection.fitness_trajectory.length > 2 && (
              <div className="border border-vb-border-subtle bg-vb-surface p-4">
                <Kicker className="mb-2">Where your form is going</Kicker>
                <FitnessTrajectoryChart trajectory={projection.fitness_trajectory} />
              </div>
            )}
          </div>
        </div>
      )}

      {/* Course profile, elevation chart or GPX upload CTA */}
      {goal.route_data?.elevation_profile &&
        goal.route_data.elevation_profile.length > 0 ? (
          <div className="border border-vb-border-subtle bg-vb-surface p-5">
            <div className="mb-3 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Kicker>Course profile</Kicker>
                {goal.gpx_file_path && <Badge variant="chalk">GPX</Badge>}
              </div>
              <div className="flex items-center gap-1.5">
                <button
                  onClick={() => gpxInputRef.current?.click()}
                  disabled={uploadGpx.isPending}
                  className="flex items-center gap-1 rounded-sm border border-vb-border px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.08em] text-vb-text-dim transition-colors hover:border-vb-border-strong hover:text-vb-text"
                >
                  <Upload className="h-3 w-3" />
                  {uploadGpx.isPending ? "Uploading…" : "Replace GPX"}
                </button>
                {goal.gpx_file_path && (
                  <button
                    onClick={() => {
                      if (confirm("Remove GPX file and route data? This will clear the elevation profile and pacing analysis.")) {
                        deleteGpx.mutate();
                      }
                    }}
                    disabled={deleteGpx.isPending}
                    className="flex items-center gap-1 rounded-sm border border-vb-border px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.08em] text-vb-text-dim transition-colors hover:border-vb-red hover:text-vb-red"
                  >
                    <Trash2 className="h-3 w-3" />
                    {deleteGpx.isPending ? "Removing…" : "Remove"}
                  </button>
                )}
              </div>
            </div>
            {/* Toggle pills when actual ride data is available */}
            {actualForChart && actualForChart.length > 0 && (
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <span className="f-kicker mr-1 text-vb-text-muted">Show</span>
                {projection?.pacing_strategy && (
                  <button
                    onClick={() => setShowTargetPower((v) => !v)}
                    className={cn(
                      "rounded-sm border px-3 py-1 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] transition-colors",
                      showTargetPower
                        ? "border-vb-border-strong bg-vb-sunken text-vb-text"
                        : "border-vb-border bg-vb-surface text-vb-text-muted"
                    )}
                  >
                    Target power
                  </button>
                )}
                <button
                  onClick={() => setShowActualPower((v) => !v)}
                  className={cn(
                    "rounded-sm border px-3 py-1 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] transition-colors",
                    showActualPower
                      ? "border-vb-red text-vb-red"
                      : "border-vb-border bg-vb-surface text-vb-text-muted"
                  )}
                >
                  Actual power
                </button>
              </div>
            )}
            <ElevationProfileChart
              data={goal.route_data.elevation_profile}
              pacingStrategy={projection?.pacing_strategy}
              actualRideData={actualForChart}
              showTargetPower={showTargetPower}
              showActualPower={showActualPower}
            />
          </div>
        ) : (
          <div className="border border-dashed border-vb-border bg-vb-surface p-5">
            <input
              ref={gpxInputRef}
              type="file"
              accept=".gpx"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) uploadGpx.mutate(file);
                e.target.value = "";
              }}
            />
            <div className="flex flex-col items-center gap-3 py-4 text-center">
              <Mountain className="h-8 w-8 text-vb-text-muted" />
              <div>
                <p className="f-display text-lg text-vb-text">
                  {goal.gpx_file_path
                    ? "Re-upload the GPX to redraw the profile"
                    : "One GPX file and Forma studies every climb"}
                </p>
                <p className="mx-auto mt-1 max-w-sm text-xs text-vb-text-muted">
                  {goal.route_data?.source === "strava"
                    ? "Strava gives summary numbers only. Upload a GPX export for the full course profile and pacing plan."
                    : "Export the route from Strava, RideWithGPS or Komoot as a .gpx file."}
                </p>
              </div>
              <Button
                onClick={() => gpxInputRef.current?.click()}
                disabled={uploadGpx.isPending}
                className="w-full sm:w-auto"
              >
                <Upload className="h-4 w-4" />
                {uploadGpx.isPending ? "Reading the route…" : "Upload GPX file"}
              </Button>
            </div>
          </div>
        )}

      <div className="grid gap-6 lg:grid-cols-2">
        {/* Route & Event Details */}
        <div className="border border-vb-border-subtle bg-vb-surface p-5">
          <Kicker className="mb-4">Event details</Kicker>

          <div className="space-y-3">
            {/* Route data */}
            {goal.route_data && !goal.route_data.error && (
              <div className="border border-vb-border-subtle bg-vb-bg p-3">
                <div className="flex items-center justify-between">
                  <Kicker>The route</Kicker>
                  {goal.route_data.source && (
                    <Badge variant="chalk">{goal.route_data.source}</Badge>
                  )}
                </div>
                {(goal.route_data.title || goal.route_data.name) && (
                  <p className="mt-1.5 text-sm font-medium text-vb-text">
                    {goal.route_data.title || goal.route_data.name}
                  </p>
                )}
                {goal.route_data.description && (
                  <p className="mt-1 text-xs text-vb-text-dim">
                    {goal.route_data.description}
                  </p>
                )}
                {(goal.route_data.total_distance_km || goal.route_data.elevation_gain_m) && (
                  <div className="mt-3 grid grid-cols-2 gap-3">
                    {goal.route_data.total_distance_km && (
                      <div className="flex items-center gap-2">
                        <MapPin className="h-4 w-4 text-vb-text-muted" />
                        <div>
                          <p className="f-data text-sm font-semibold text-vb-text">
                            {goal.route_data.total_distance_km}km
                          </p>
                          <p className="f-kicker text-[9px] text-vb-text-muted">
                            Distance
                          </p>
                        </div>
                      </div>
                    )}
                    {goal.route_data.elevation_gain_m && (
                      <div className="flex items-center gap-2">
                        <Mountain className="h-4 w-4 text-vb-text-muted" />
                        <div>
                          <p className="f-data text-sm font-semibold text-vb-text">
                            {goal.route_data.elevation_gain_m}m
                          </p>
                          <p className="f-kicker text-[9px] text-vb-text-muted">
                            Climbing
                          </p>
                        </div>
                      </div>
                    )}
                    {goal.route_data.avg_gradient_pct != null && (
                      <div className="flex items-center gap-2">
                        <TrendingUp className="h-4 w-4 text-vb-text-muted" />
                        <div>
                          <p className="f-data text-sm font-semibold text-vb-text">
                            {goal.route_data.avg_gradient_pct}%
                          </p>
                          <p className="f-kicker text-[9px] text-vb-text-muted">
                            Avg gradient
                          </p>
                        </div>
                      </div>
                    )}
                    {goal.route_data.max_elevation_m != null && (
                      <div>
                        <p className="f-data text-sm font-semibold text-vb-text">
                          {goal.route_data.max_elevation_m}m
                        </p>
                        <p className="f-kicker text-[9px] text-vb-text-muted">
                          Max elevation
                        </p>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}

            {/* Links */}
            {goal.route_url && (
              <div className="flex items-center gap-2">
                <ExternalLink className="h-4 w-4 text-vb-text-muted" />
                <a
                  href={goal.route_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-sm text-vb-text transition-colors hover:text-vb-red"
                >
                  {goal.route_url.length > 60
                    ? goal.route_url.slice(0, 60) + "..."
                    : goal.route_url}
                </a>
              </div>
            )}

            {/* GPX status */}
            {goal.gpx_file_path && (
              <div className="flex items-center gap-2">
                <span className="inline-flex items-center gap-1.5 font-mono text-xs uppercase tracking-[0.08em] text-vb-text-dim">
                  <Mountain className="h-4 w-4" />
                  GPX file uploaded
                </span>
              </div>
            )}

            {/* Notes */}
            {goal.notes && (
              <div className="mt-3">
                <Kicker className="mb-1">Notes</Kicker>
                <p className="whitespace-pre-wrap text-sm text-vb-text-dim">
                  {goal.notes}
                </p>
              </div>
            )}
          </div>
        </div>

        {/* Fitness Readiness */}
        <div className="border border-vb-border-subtle bg-vb-surface p-5">
          <Kicker className="mb-4">Fitness readiness</Kicker>

          {readiness ? (
            <div className="space-y-4">
              {/* Readiness score */}
              <div className="text-center">
                <p
                  className={`f-display mt-1 text-3xl ${readinessColor(readiness.readiness_label)}`}
                >
                  {readiness.readiness_label}
                </p>
                <p className="f-data mt-1 text-sm text-vb-text-muted">
                  {readiness.readiness_score}/100
                </p>
              </div>

              {/* Metrics */}
              <div className="grid grid-cols-2 gap-px">
                <div className="border border-vb-border-subtle bg-vb-surface p-3">
                  <p className="f-kicker text-vb-text-muted">Current CTL</p>
                  <p className="f-data mt-1 text-2xl font-semibold text-vb-text">
                    {Math.round(readiness.current_ctl)}
                  </p>
                  <p className="f-data text-xs text-vb-text-muted">
                    Target {Math.round(readiness.target_ctl)}
                  </p>
                </div>
                <div className="border border-vb-border-subtle bg-vb-surface p-3">
                  <p className="f-kicker text-vb-text-muted">Current TSB</p>
                  <p className="f-data mt-1 text-2xl font-semibold text-vb-text">
                    {Math.round(readiness.current_tsb)}
                  </p>
                  {readiness.projected_tsb_on_event != null && (
                    <p className="f-data text-xs text-vb-text-muted">
                      Race day ~{Math.round(readiness.projected_tsb_on_event)}
                    </p>
                  )}
                </div>
                {readiness.current_ftp && (
                  <div className="border border-vb-border-subtle bg-vb-surface p-3">
                    <p className="f-kicker text-vb-text-muted">FTP</p>
                    <p className="f-data mt-1 text-2xl font-semibold text-vb-text">
                      {readiness.current_ftp}W
                    </p>
                  </div>
                )}
                {readiness.w_per_kg && (
                  <div className="border border-vb-border-subtle bg-vb-surface p-3">
                    <p className="f-kicker text-vb-text-muted">W/kg</p>
                    <p className="f-data mt-1 text-2xl font-semibold text-vb-text">
                      {readiness.w_per_kg}
                    </p>
                  </div>
                )}
              </div>

              {/* Recommendations */}
              {readiness.recommendations.length > 0 && (
                <div>
                  <Kicker>From the coach</Kicker>
                  <ul className="mt-2 space-y-1.5">
                    {readiness.recommendations.map((rec, i) => (
                      <li
                        key={i}
                        className="flex items-start gap-2 text-sm text-vb-text-dim"
                      >
                        <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-vb-red" />
                        {rec}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          ) : fitness ? (
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-px">
                <div className="border border-vb-border-subtle bg-vb-surface p-3">
                  <p className="f-kicker text-vb-text-muted">Fitness</p>
                  <p className="f-data mt-1 text-2xl font-semibold text-vb-text">
                    {Math.round(fitness.current_ctl)}
                    <span className="ml-1 text-sm font-medium text-vb-text-muted">
                      CTL
                    </span>
                  </p>
                </div>
                <div className="border border-vb-border-subtle bg-vb-surface p-3">
                  <p className="f-kicker text-vb-text-muted">Form</p>
                  <p className="f-data mt-1 text-2xl font-semibold text-vb-text">
                    {Math.round(fitness.current_tsb)}
                    <span className="ml-1 text-sm font-medium text-vb-text-muted">
                      TSB
                    </span>
                  </p>
                </div>
                {user?.ftp && (
                  <div className="border border-vb-border-subtle bg-vb-surface p-3">
                    <p className="f-kicker text-vb-text-muted">FTP</p>
                    <p className="f-data mt-1 text-2xl font-semibold text-vb-text">
                      {user.ftp}W
                    </p>
                  </div>
                )}
                {fitness.w_per_kg && (
                  <div className="border border-vb-border-subtle bg-vb-surface p-3">
                    <p className="f-kicker text-vb-text-muted">W/kg</p>
                    <p className="f-data mt-1 text-2xl font-semibold text-vb-text">
                      {fitness.w_per_kg}
                    </p>
                  </div>
                )}
              </div>
              <p className="text-xs text-vb-text-muted">
                The full readiness read arrives once the event is on the horizon.
              </p>
            </div>
          ) : (
            <p className="py-8 text-center text-sm text-vb-text-muted">
              This panel is waiting for your first ride with power. One file
              and it starts moving.
            </p>
          )}
        </div>
      </div>

      {/* ============ ACTIONS ============ */}
      <div className="flex flex-wrap gap-3">
        {goal.needs_assessment ? (
          <Link
            href={`/dashboard/goals/${goalId}/assess`}
            className={buttonVariants({ variant: "flamme" })}
          >
            File the race report
            <Arrow />
          </Link>
        ) : goal.assessment_completed_at ? (
          <Link
            href={`/dashboard/coach?goal_id=${goalId}&debrief=true`}
            className={buttonVariants({ variant: "flamme" })}
          >
            Debrief with {coachName}
            <Arrow />
          </Link>
        ) : (
          <>
            <Link
              href={`/dashboard/coach?goal_id=${goalId}`}
              className={buttonVariants({ variant: "flamme" })}
            >
              Ask {coachName} about this goal
              <Arrow />
            </Link>
            <Button
              variant="ghost"
              onClick={() => generatePlan.mutate()}
              disabled={generatePlan.isPending}
            >
              {generatePlan.isPending ? "Building your season…" : "Build my season"}
            </Button>
          </>
        )}
      </div>
    </div>
  );
}
