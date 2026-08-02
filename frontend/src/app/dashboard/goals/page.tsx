"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, useRef, useEffect, Suspense } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  Plus,
  Target,
  MapPin,
  Mountain,
  ExternalLink,
  Trash2,
  Pencil,
  Upload,
  X,
} from "lucide-react";
import { goals as goalsApi, training as trainingApi } from "@/lib/api";
import { formatDate } from "@/lib/utils";
import type { GoalEvent } from "@/lib/api";
import { Button, Arrow, buttonVariants } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Kicker } from "@/components/ui/kicker";
import { SectionHeader } from "@/components/ui/section-header";
import { EmptyState } from "@/components/ui/empty-state";
import { CoachNote } from "@/components/ui/coach-note";
import { Input } from "@/components/ui/input";

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

interface GoalFormData {
  event_name: string;
  event_date: string;
  event_type: string;
  priority: string;
  target_duration_minutes: string;
  notes: string;
  route_url: string;
}

const emptyGoalForm: GoalFormData = {
  event_name: "",
  event_date: "",
  event_type: "road_race",
  priority: "a_race",
  target_duration_minutes: "",
  notes: "",
  route_url: "",
};

function goalToFormData(goal: GoalEvent): GoalFormData {
  return {
    event_name: goal.event_name,
    event_date: goal.event_date,
    event_type: goal.event_type,
    priority: goal.priority,
    target_duration_minutes: goal.target_duration_minutes?.toString() || "",
    notes: goal.notes || "",
    route_url: goal.route_url || "",
  };
}

function formatDurationMinutes(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (h > 0) return m > 0 ? `${h}h ${m}m` : `${h}h`;
  return `${m}m`;
}

/** Poster date block parts from an ISO date string. */
function eventDateParts(dateStr: string) {
  const d = new Date(dateStr.slice(0, 10) + "T00:00:00");
  return {
    day: String(d.getDate()).padStart(2, "0"),
    month: d.toLocaleString("en-GB", { month: "short" }).toUpperCase(),
    year: String(d.getFullYear()),
  };
}

function priorityBadge(priority: string) {
  const label = priority.replace(/_/g, " ").replace("race", "race");
  if (priority === "a_race") return <Badge variant="flamme">A race</Badge>;
  if (priority === "b_race") return <Badge variant="outline">B race</Badge>;
  if (priority === "c_race") return <Badge variant="outline">C race</Badge>;
  return <Badge variant="outline">{label}</Badge>;
}

const selectClasses =
  "flex h-11 w-full rounded-sm border border-vb-border bg-vb-surface px-3 py-2 text-sm text-vb-text focus:border-vb-red focus:outline-none focus:ring-1 focus:ring-vb-red";

const textareaClasses =
  "w-full rounded-sm border border-vb-border bg-vb-surface px-3 py-2 text-sm text-vb-text placeholder:text-vb-text-muted focus:border-vb-red focus:outline-none focus:ring-1 focus:ring-vb-red resize-none";

function GoalsPageInner() {
  const queryClient = useQueryClient();
  const gpxInputRef = useRef<HTMLInputElement>(null);
  const searchParams = useSearchParams();

  const [showForm, setShowForm] = useState(false);
  const [editingGoalId, setEditingGoalId] = useState<string | null>(null);
  const [goalForm, setGoalForm] = useState<GoalFormData>(emptyGoalForm);
  const [gpxUploadingId, setGpxUploadingId] = useState<string | null>(null);
  const [pendingGpxFile, setPendingGpxFile] = useState<File | null>(null);
  const [planGenerating, setPlanGenerating] = useState(false);
  const formGpxInputRef = useRef<HTMLInputElement>(null);

  // Auto-open the add-goal form when navigated with ?new=1 (e.g. from the
  // post-assessment "What's next?" prompt).
  useEffect(() => {
    if (searchParams.get("new") === "1") {
      setEditingGoalId(null);
      setGoalForm(emptyGoalForm);
      setShowForm(true);
    }
  }, [searchParams]);

  const { data: goalsData, isLoading } = useQuery({
    queryKey: ["goals"],
    queryFn: () => goalsApi.list(),
  });

  const saveGoal = useMutation({
    mutationFn: () => {
      const data: Record<string, unknown> = {
        event_name: goalForm.event_name,
        event_date: goalForm.event_date,
        event_type: goalForm.event_type,
        priority: goalForm.priority,
      };
      if (goalForm.target_duration_minutes) {
        data.target_duration_minutes = parseInt(goalForm.target_duration_minutes);
      }
      if (goalForm.notes) data.notes = goalForm.notes;
      if (goalForm.route_url) data.route_url = goalForm.route_url;

      if (editingGoalId) {
        return goalsApi.update(editingGoalId, data as Parameters<typeof goalsApi.update>[1]);
      }
      return goalsApi.create(data as Parameters<typeof goalsApi.create>[0]);
    },
    onSuccess: async (goal) => {
      // Upload pending GPX file if one was selected
      if (pendingGpxFile && goal?.id) {
        await goalsApi.uploadGpx(goal.id, pendingGpxFile);
        setPendingGpxFile(null);
      }

      // Auto-regenerate a unified training plan that considers ALL goals.
      // The backend auto-detects the primary (highest-priority A-race),
      // builds the macro periodization toward it, and accommodates B/C races.
      if (!editingGoalId && goal?.id) {
        try {
          setPlanGenerating(true);
          await trainingApi.generatePlan({
            periodization_model: "traditional",
          });
          queryClient.invalidateQueries({ queryKey: ["plans"] });
          queryClient.invalidateQueries({ queryKey: ["workouts-week"] });
        } catch (planErr) {
          console.error("Auto plan generation failed:", planErr);
        } finally {
          setPlanGenerating(false);
        }
      }

      queryClient.invalidateQueries({ queryKey: ["goals"] });
      setShowForm(false);
      setEditingGoalId(null);
      setGoalForm(emptyGoalForm);
    },
  });

  const deleteGoal = useMutation({
    mutationFn: (id: string) => goalsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["goals"] });
    },
  });

  const uploadGpx = useMutation({
    mutationFn: ({ goalId, file }: { goalId: string; file: File }) =>
      goalsApi.uploadGpx(goalId, file),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["goals"] });
      setGpxUploadingId(null);
    },
    onError: () => {
      setGpxUploadingId(null);
    },
  });

  // Three-category split
  const upcomingGoals =
    goalsData?.goals.filter((g) => g.days_until != null && g.days_until >= 0) || [];
  const needsAssessmentGoals =
    goalsData?.goals.filter((g) => g.needs_assessment) || [];
  const completedGoals =
    goalsData?.goals.filter(
      (g) => (g.days_until == null || g.days_until < 0) && !g.needs_assessment
    ) || [];

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="h-8 w-8 animate-spin rounded-full border border-vb-border-subtle border-t-vb-red" />
      </div>
    );
  }

  return (
    <div className="space-y-10">
      {/* ============ MASTHEAD ============ */}
      <header className="f-rise flex items-end justify-between gap-6 border-b-2 border-vb-border-strong pb-5">
        <div>
          <Link
            href="/dashboard/training"
            className="f-kicker mb-3 inline-block text-vb-text-dim transition-colors hover:text-vb-red"
          >
            ← The plan
          </Link>
          <Kicker className="mb-2">The calendar</Kicker>
          <h1 className="f-display text-5xl leading-[0.95] md:text-6xl">
            Goals.
          </h1>
          <p className="mt-3 max-w-md text-sm text-vb-text-dim">
            The races your season is built backwards from. Give Forma the
            route and it studies every climb before you do.
          </p>
        </div>
        <Button
          onClick={() => {
            setEditingGoalId(null);
            setGoalForm(emptyGoalForm);
            setShowForm(true);
          }}
          className="shrink-0"
        >
          <Plus className="h-3.5 w-3.5" /> Add goal
        </Button>
      </header>

      {/* Goal Form */}
      {showForm && (
        <div className="f-rise border border-vb-border-subtle bg-vb-surface p-6">
          <div className="mb-5 flex items-start justify-between">
            <div>
              <Kicker className="mb-1.5">
                {editingGoalId ? "Edit goal" : "New goal"}
              </Kicker>
              <h2 className="f-display text-2xl text-vb-text">
                {editingGoalId ? "Change the target" : "Where and when?"}
              </h2>
            </div>
            <button
              onClick={() => {
                setShowForm(false);
                setEditingGoalId(null);
              }}
              className="rounded-sm p-1 text-vb-text-muted hover:text-vb-text"
            >
              <X className="h-5 w-5" />
            </button>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <Kicker className="mb-2">What&apos;s the event? *</Kicker>
              <Input
                value={goalForm.event_name}
                onChange={(e) =>
                  setGoalForm({ ...goalForm, event_name: e.target.value })
                }
                placeholder="e.g. Mallorca 312, Tour of Wessex"
              />
            </div>
            <div>
              <Kicker className="mb-2">When is race day? *</Kicker>
              <Input
                type="date"
                value={goalForm.event_date}
                onChange={(e) =>
                  setGoalForm({ ...goalForm, event_date: e.target.value })
                }
              />
            </div>
            <div>
              <Kicker className="mb-2">What kind of race?</Kicker>
              <select
                value={goalForm.event_type}
                onChange={(e) =>
                  setGoalForm({ ...goalForm, event_type: e.target.value })
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
                value={goalForm.priority}
                onChange={(e) =>
                  setGoalForm({ ...goalForm, priority: e.target.value })
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
                value={goalForm.target_duration_minutes}
                onChange={(e) =>
                  setGoalForm({
                    ...goalForm,
                    target_duration_minutes: e.target.value,
                  })
                }
                placeholder="e.g. 240 for four hours"
              />
            </div>
            <div>
              <Kicker className="mb-2">Where does the route live?</Kicker>
              <Input
                type="url"
                value={goalForm.route_url}
                onChange={(e) =>
                  setGoalForm({ ...goalForm, route_url: e.target.value })
                }
                placeholder="https://strava.com/routes/... or the event site"
              />
            </div>
          </div>

          <div className="mt-4">
            <Kicker className="mb-2">Anything I should know?</Kicker>
            <textarea
              value={goalForm.notes}
              onChange={(e) =>
                setGoalForm({ ...goalForm, notes: e.target.value })
              }
              placeholder="Course details, a time you're chasing, how the day needs to go..."
              rows={3}
              className={textareaClasses}
            />
          </div>

          {/* GPX Upload */}
          <div className="mt-4">
            <Kicker className="mb-2">The route file (.gpx)</Kicker>
            <input
              ref={formGpxInputRef}
              type="file"
              accept=".gpx"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) {
                  if (editingGoalId) {
                    // Editing — upload immediately
                    uploadGpx.mutate({ goalId: editingGoalId, file });
                  } else {
                    // Creating — store for upload after save
                    setPendingGpxFile(file);
                  }
                }
                e.target.value = "";
              }}
            />
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={() => formGpxInputRef.current?.click()}
                className="flex items-center gap-1.5 rounded-sm border border-dashed border-vb-border px-3 py-2 font-mono text-xs uppercase tracking-[0.08em] text-vb-text-dim transition-colors hover:border-vb-border-strong hover:text-vb-text"
              >
                <Upload className="h-4 w-4" />
                {pendingGpxFile
                  ? "Change file"
                  : editingGoalId
                    ? "Upload / replace GPX"
                    : "Upload GPX"}
              </button>
              {pendingGpxFile && (
                <div className="flex items-center gap-2">
                  <span className="flex items-center gap-1 font-mono text-xs text-vb-text">
                    <Mountain className="h-3 w-3 text-vb-red" />
                    {pendingGpxFile.name}
                  </span>
                  <button
                    type="button"
                    onClick={() => setPendingGpxFile(null)}
                    className="rounded-sm p-0.5 text-vb-text-muted hover:text-vb-red"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              )}
              {editingGoalId && !pendingGpxFile && (() => {
                const editGoal = goalsData?.goals.find((g) => g.id === editingGoalId);
                if (editGoal?.gpx_file_path) {
                  return (
                    <span className="flex items-center gap-1 font-mono text-xs text-vb-text-dim">
                      <Mountain className="h-3 w-3 text-vb-red" />
                      GPX uploaded
                    </span>
                  );
                }
                return null;
              })()}
            </div>
            <p className="mt-1 text-[10px] text-vb-text-muted">
              Export from Strava, RideWithGPS or Komoot and Forma reads the
              full profile, climb by climb.
            </p>
          </div>

          {!editingGoalId && (
            <p className="mt-3 text-xs text-vb-text-muted">
              Save this goal and Forma builds a plan that peaks on race day.
            </p>
          )}

          <div className="mt-5 flex flex-wrap gap-2">
            <Button
              onClick={() => saveGoal.mutate()}
              disabled={
                !goalForm.event_name ||
                !goalForm.event_date ||
                saveGoal.isPending ||
                planGenerating
              }
            >
              {saveGoal.isPending
                ? "Saving…"
                : planGenerating
                  ? "Building your plan…"
                  : editingGoalId
                    ? "Save changes"
                    : "Create goal"}
              <Arrow />
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                setShowForm(false);
                setEditingGoalId(null);
              }}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}

      {/* Hidden GPX file input */}
      <input
        ref={gpxInputRef}
        type="file"
        accept=".gpx"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file && gpxUploadingId) {
            uploadGpx.mutate({ goalId: gpxUploadingId, file });
          }
          e.target.value = "";
        }}
      />

      {/* Empty state */}
      {goalsData?.goals.length === 0 && !showForm && (
        <EmptyState
          kicker="Empty calendar"
          title="What are we aiming at?"
          action={
            <Button
              variant="flamme"
              onClick={() => {
                setEditingGoalId(null);
                setGoalForm(emptyGoalForm);
                setShowForm(true);
              }}
            >
              <Plus className="h-3.5 w-3.5" /> Add your first goal
              <Arrow />
            </Button>
          }
        >
          A sportive, a road race, a number you&apos;ve been chasing. Give
          Forma the date and it builds the season backwards from it. Add the
          route&apos;s GPX and it studies every climb.
        </EmptyState>
      )}

      {/* Upcoming Goals */}
      {upcomingGoals.length > 0 && (
        <div>
          <SectionHeader
            kicker={`${upcomingGoals.length} on the calendar`}
            title="Upcoming"
          />
          <div className="f-stagger space-y-3">
            {upcomingGoals.map((goal) => (
              <GoalCard
                key={goal.id}
                goal={goal}
                onEdit={() => {
                  setEditingGoalId(goal.id);
                  setGoalForm(goalToFormData(goal));
                  setShowForm(true);
                }}
                onDelete={() => {
                  if (confirm(`Delete "${goal.event_name}"?`)) {
                    deleteGoal.mutate(goal.id);
                  }
                }}
                onUploadGpx={() => {
                  setGpxUploadingId(goal.id);
                  gpxInputRef.current?.click();
                }}
                gpxUploading={uploadGpx.isPending && gpxUploadingId === goal.id}
              />
            ))}
          </div>
        </div>
      )}

      {/* Needs Assessment */}
      {needsAssessmentGoals.length > 0 && (
        <div>
          <SectionHeader
            kicker={`${needsAssessmentGoals.length} waiting on you`}
            title="Needs assessment"
          />
          <div className="f-stagger space-y-3">
            {needsAssessmentGoals.map((goal) => (
              <div key={goal.id} className="relative">
                <GoalCard
                  goal={goal}
                  onEdit={() => {
                    setEditingGoalId(goal.id);
                    setGoalForm(goalToFormData(goal));
                    setShowForm(true);
                  }}
                  onDelete={() => {
                    if (confirm(`Delete "${goal.event_name}"?`)) {
                      deleteGoal.mutate(goal.id);
                    }
                  }}
                  onUploadGpx={() => {
                    setGpxUploadingId(goal.id);
                    gpxInputRef.current?.click();
                  }}
                  gpxUploading={uploadGpx.isPending && gpxUploadingId === goal.id}
                  assessmentBanner
                />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Completed Goals */}
      {completedGoals.length > 0 && (
        <div>
          <SectionHeader
            kicker={`${completedGoals.length} in the bank`}
            title="Completed"
          />
          <div className="f-stagger space-y-3">
            {completedGoals.map((goal) => (
              <GoalCard
                key={goal.id}
                goal={goal}
                onEdit={() => {
                  setEditingGoalId(goal.id);
                  setGoalForm(goalToFormData(goal));
                  setShowForm(true);
                }}
                onDelete={() => {
                  if (confirm(`Delete "${goal.event_name}"?`)) {
                    deleteGoal.mutate(goal.id);
                  }
                }}
                onUploadGpx={() => {
                  setGpxUploadingId(goal.id);
                  gpxInputRef.current?.click();
                }}
                gpxUploading={uploadGpx.isPending && gpxUploadingId === goal.id}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default function GoalsPage() {
  return (
    <Suspense
      fallback={
        <div className="flex items-center justify-center py-20">
          <div className="h-8 w-8 animate-spin rounded-full border border-vb-border-subtle border-t-vb-red" />
        </div>
      }
    >
      <GoalsPageInner />
    </Suspense>
  );
}

function GoalCard({
  goal,
  onEdit,
  onDelete,
  onUploadGpx,
  gpxUploading,
  assessmentBanner,
}: {
  goal: GoalEvent;
  onEdit: () => void;
  onDelete: () => void;
  onUploadGpx: () => void;
  gpxUploading: boolean;
  assessmentBanner?: boolean;
}) {
  const dateParts = eventDateParts(goal.event_date);
  const imminent =
    goal.days_until != null && goal.days_until >= 0 && goal.days_until < 14;

  return (
    <div>
      {/* ===== Event poster ===== */}
      <div className="f-lift border border-vb-border-subtle bg-vb-surface">
        <div className="flex items-stretch">
          {/* Mono date block */}
          <div className="flex w-24 shrink-0 flex-col items-center justify-center border-r border-vb-border-subtle px-3 py-5 text-center">
            <span className="f-kicker text-vb-text-muted">
              {dateParts.month}
            </span>
            <span className="f-data mt-1 text-4xl font-semibold leading-none text-vb-text">
              {dateParts.day}
            </span>
            <span className="f-data mt-1 text-[10px] text-vb-text-muted">
              {dateParts.year}
            </span>
          </div>

          {/* Poster body */}
          <div className="min-w-0 flex-1 p-5">
            {/* Kicker row: type + countdown */}
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <Kicker>{goal.event_type.replace(/_/g, " ")}</Kicker>
              {goal.days_until != null && goal.days_until >= 0 && (
                <Kicker flamme={imminent} dot={imminent}>
                  {goal.days_until === 0
                    ? "Race day"
                    : `${goal.days_until} days`}
                </Kicker>
              )}
            </div>

            {/* Event name */}
            <Link
              href={`/dashboard/goals/${goal.id}`}
              className="f-display mt-1.5 block truncate text-2xl text-vb-text transition-colors hover:text-vb-red"
            >
              {goal.event_name}
            </Link>

            {/* Meta row */}
            <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5">
              {priorityBadge(goal.priority)}
              <span className="f-kicker text-vb-text-muted">
                {formatDate(goal.event_date)}
              </span>
              {goal.target_duration_minutes && (
                <span className="f-data text-xs text-vb-text-dim">
                  Target {formatDurationMinutes(goal.target_duration_minutes)}
                </span>
              )}
            </div>

            {/* Route data */}
            {goal.route_data && !goal.route_data.error && (
              <div className="f-data mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-vb-text">
                {goal.route_data.total_distance_km && (
                  <span className="flex items-center gap-1">
                    <MapPin className="h-3.5 w-3.5 shrink-0 text-vb-text-muted" />
                    {goal.route_data.total_distance_km} km
                  </span>
                )}
                {goal.route_data.elevation_gain_m && (
                  <span className="flex items-center gap-1">
                    <Mountain className="h-3.5 w-3.5 shrink-0 text-vb-text-muted" />
                    {goal.route_data.elevation_gain_m} m
                  </span>
                )}
                {(goal.route_data.title || goal.route_data.name) && (
                  <span className="truncate font-sans text-xs text-vb-text-dim">
                    {goal.route_data.title || goal.route_data.name}
                  </span>
                )}
              </div>
            )}

            {/* Notes */}
            {goal.notes && (
              <p className="mt-2 line-clamp-2 text-xs text-vb-text-muted">
                {goal.notes}
              </p>
            )}

            {/* Result inline, for assessed goals */}
            {goal.assessment_completed_at && (
              <div className="mt-2.5 flex flex-wrap items-center gap-3">
                <Badge variant={goal.status === "completed" ? "ink" : "outline"}>
                  {goal.status === "dns"
                    ? "DNS"
                    : goal.status === "dnf"
                      ? "DNF"
                      : "Completed"}
                </Badge>
                {goal.finish_time_seconds && (
                  <span className="f-data text-xs text-vb-text-dim">
                    {Math.floor(goal.finish_time_seconds / 3600)}h{" "}
                    {Math.floor((goal.finish_time_seconds % 3600) / 60)}m
                  </span>
                )}
                {goal.overall_satisfaction && (
                  <span className="f-data text-xs text-vb-text-dim">
                    {goal.overall_satisfaction}/10 satisfied
                  </span>
                )}
              </div>
            )}

            {/* Tags / indicators */}
            {(goal.route_url || goal.gpx_file_path) && (
              <div className="mt-2 flex items-center gap-3">
                {goal.route_url && (
                  <a
                    href={goal.route_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 font-mono text-[10px] uppercase tracking-[0.1em] text-vb-text-muted transition-colors hover:text-vb-red"
                  >
                    <ExternalLink className="h-3 w-3" />
                    Event link
                  </a>
                )}
                {goal.gpx_file_path && (
                  <span className="inline-flex items-center gap-1 font-mono text-[10px] uppercase tracking-[0.1em] text-vb-text-dim">
                    <Mountain className="h-3 w-3" />
                    GPX
                  </span>
                )}
              </div>
            )}
          </div>

          {/* Actions */}
          <div className="flex shrink-0 flex-col gap-1 p-3">
            <Link
              href={`/dashboard/goals/${goal.id}`}
              className="rounded-sm p-1.5 text-vb-text-muted transition-colors hover:bg-vb-sunken hover:text-vb-text"
              title="View details"
            >
              <Target className="h-4 w-4" />
            </Link>
            <button
              onClick={onUploadGpx}
              disabled={gpxUploading}
              className="rounded-sm p-1.5 text-vb-text-muted transition-colors hover:bg-vb-sunken hover:text-vb-text"
              title="Upload GPX"
            >
              <Upload className="h-4 w-4" />
            </button>
            <button
              onClick={onEdit}
              className="rounded-sm p-1.5 text-vb-text-muted transition-colors hover:bg-vb-sunken hover:text-vb-text"
              title="Edit"
            >
              <Pencil className="h-4 w-4" />
            </button>
            <button
              onClick={onDelete}
              className="rounded-sm p-1.5 text-vb-text-muted transition-colors hover:bg-vb-sunken hover:text-vb-red"
              title="Delete"
            >
              <Trash2 className="h-4 w-4" />
            </button>
          </div>
        </div>
      </div>

      {/* Forma asks for the race report */}
      {assessmentBanner && (
        <CoachNote
          kicker="Race report pending"
          signature={false}
          className="mt-2"
          action={
            <Link
              href={`/dashboard/goals/${goal.id}/assess`}
              className={buttonVariants({ variant: "ink", size: "sm" })}
            >
              File the report
              <Arrow />
            </Link>
          }
        >
          So, how did it go? Ten minutes on {goal.event_name} and this race
          starts working for the next one.
        </CoachNote>
      )}
    </div>
  );
}
