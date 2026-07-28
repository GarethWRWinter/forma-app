"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, useRef } from "react";
import {
  Plus,
  Trash2,
  RefreshCw,
  FolderSync,
  Pencil,
  Upload,
  ExternalLink,
  MapPin,
  Mountain,
  X,
} from "lucide-react";
import { users, strava, dropbox, goals as goalsApi, metrics } from "@/lib/api";
import { COACH_TONES, normalizeAvatarKey } from "@/lib/coach";
import { useAuth } from "@/lib/auth-context";
import { formatDate, cn } from "@/lib/utils";
import type { GoalEvent, StravaStatus } from "@/lib/api";
import Link from "next/link";
import { Button, Arrow } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Kicker } from "@/components/ui/kicker";
import { Badge } from "@/components/ui/badge";
import { SectionHeader } from "@/components/ui/section-header";
import { ArchiveImport } from "@/components/settings/archive-import";
import { WahooCard } from "@/components/settings/wahoo-card";

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
  { value: "a_race", label: "A race (the big one)" },
  { value: "b_race", label: "B race (matters)" },
  { value: "c_race", label: "C race (training day)" },
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

// f-kicker styled form label
const labelClasses = "f-kicker mb-2 block text-vb-text-muted";
// select / textarea share the kit Input look (Input covers <input> only)
const fieldClasses =
  "w-full rounded-sm border border-vb-border bg-vb-surface px-3 py-2 text-sm text-vb-text placeholder:text-vb-text-muted focus:border-vb-red focus:outline-none focus:ring-1 focus:ring-vb-red";

export default function SettingsPage() {
  const { user, refreshUser } = useAuth();
  const queryClient = useQueryClient();
  const gpxInputRef = useRef<HTMLInputElement>(null);

  // Profile form state
  const [form, setForm] = useState({
    full_name: user?.full_name || "",
    weight_kg: user?.weight_kg?.toString() || "",
    height_cm: user?.height_cm?.toString() || "",
    ftp: user?.ftp?.toString() || "",
    max_hr: user?.max_hr?.toString() || "",
    resting_hr: user?.resting_hr?.toString() || "",
    weekly_hours_available: user?.weekly_hours_available?.toString() || "",
    preferred_hard_days: user?.preferred_hard_days ?? [],
    rest_days: user?.rest_days ?? [],
  });
  const [saved, setSaved] = useState(false);

  // Goals state
  const [showGoalForm, setShowGoalForm] = useState(false);
  const [editingGoalId, setEditingGoalId] = useState<string | null>(null);
  const [goalForm, setGoalForm] = useState<GoalFormData>(emptyGoalForm);
  const [gpxUploadingId, setGpxUploadingId] = useState<string | null>(null);

  // FTP test state
  const [ftpTestPower, setFtpTestPower] = useState("");
  const [ftpResult, setFtpResult] = useState<string | null>(null);

  // Dropbox folder edit state
  const [editingFolder, setEditingFolder] = useState(false);
  const [folderPath, setFolderPath] = useState("");

  // Queries
  const { data: stravaStatus } = useQuery({
    queryKey: ["strava-status"],
    queryFn: () => strava.getStatus(),
    refetchInterval: (query) => {
      const data = query.state.data as StravaStatus | undefined;
      const status = data?.backfill?.status;
      // Poll fast while actively running, slow while paused (long waits)
      if (status === "running") return 3000;
      if (status === "paused") return 30_000; // 30s while in 15-min pause
      if (status === "paused_daily") return 5 * 60_000; // 5min while in daily pause
      return false;
    },
  });

  // One live probe of Strava activity access — the ground-truth 403 check that
  // catches a token linked without the "activities" permission (which the
  // stored scope alone can't reveal for older connections).
  const { data: stravaProbe } = useQuery({
    queryKey: ["strava-probe"],
    queryFn: () => strava.getStatus(true),
    enabled: !!stravaStatus?.connected,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

  const stravaNoActivityScope =
    stravaStatus?.connected === true &&
    (stravaStatus?.can_read_activities === false ||
      stravaProbe?.probe?.reason === "missing_activity_scope" ||
      stravaStatus?.backfill?.status === "failed_no_activity_scope");

  const reconnectStrava = async () => {
    const { auth_url } = await strava.getAuthUrl();
    window.location.href = auth_url;
  };

  const { data: dropboxStatus } = useQuery({
    queryKey: ["dropbox-status"],
    queryFn: () => dropbox.getStatus(),
  });

  const { data: goalsData } = useQuery({
    queryKey: ["goals"],
    queryFn: () => goalsApi.list(),
  });

  // Mutations
  const saveProfile = useMutation({
    mutationFn: () => {
      const data: Record<string, string | number | boolean | number[]> = {};
      if (form.full_name) data.full_name = form.full_name;
      if (form.weight_kg) data.weight_kg = parseFloat(form.weight_kg);
      if (form.height_cm) data.height_cm = parseFloat(form.height_cm);
      if (form.ftp) data.ftp = parseInt(form.ftp);
      if (form.max_hr) data.max_hr = parseInt(form.max_hr);
      if (form.resting_hr) data.resting_hr = parseInt(form.resting_hr);
      if (form.weekly_hours_available)
        data.weekly_hours_available = parseFloat(form.weekly_hours_available);
      data.preferred_hard_days = form.preferred_hard_days;
      data.rest_days = form.rest_days;
      return users.updateProfile(data);
    },
    onSuccess: () => {
      refreshUser();
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    },
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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["goals"] });
      setShowGoalForm(false);
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

  const submitFTPTest = useMutation({
    mutationFn: () => metrics.submitFTPTest(parseInt(ftpTestPower)),
    onSuccess: (data) => {
      setFtpResult(`New FTP: ${data.new_ftp}W`);
      refreshUser();
      setFtpTestPower("");
    },
  });

  const syncStrava = useMutation({
    mutationFn: () => strava.sync(),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["rides"] });
      queryClient.invalidateQueries({ queryKey: ["strava-status"] });
      alert(
        data.synced > 0
          ? `${data.synced} rides in from Strava`
          : "Strava answered, but had nothing new since the last sync."
      );
    },
    onError: (err: Error) => {
      alert(err.message || "That sync didn't go through. Try again in a minute.");
    },
  });

  const syncDropbox = useMutation({
    mutationFn: () => dropbox.sync(),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["rides"] });
      queryClient.invalidateQueries({ queryKey: ["dropbox-status"] });
      alert(`${data.synced} ride file(s) in from Dropbox`);
    },
    onError: (err: Error) => {
      alert(err.message || "That sync didn't go through. Try again in a minute.");
    },
  });

  const updateDropboxFolder = useMutation({
    mutationFn: (path: string) => dropbox.updateFolder(path),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["dropbox-status"] });
      setEditingFolder(false);
    },
  });

  function openAddGoal() {
    setEditingGoalId(null);
    setGoalForm(emptyGoalForm);
    setShowGoalForm(true);
  }

  function openEditGoal(goal: GoalEvent) {
    setEditingGoalId(goal.id);
    setGoalForm(goalToFormData(goal));
    setShowGoalForm(true);
  }

  function handleGpxUpload(goalId: string, file: File) {
    setGpxUploadingId(goalId);
    uploadGpx.mutate({ goalId, file });
  }

  return (
    <div className="mx-auto max-w-3xl space-y-10">
      {/* ============ MASTHEAD ============ */}
      <header className="f-rise border-b border-vb-border-subtle pb-5">
        <Kicker flamme className="mb-2">
          The desk
        </Kicker>
        <h1 className="f-display text-5xl leading-[1.04] md:text-6xl">
          Settings.
        </h1>
      </header>

      {/* Your coach */}
      <CoachSection />

      {/* Profile */}
      <section className="rounded-sm border border-vb-border-subtle bg-vb-surface p-6">
        <h2 className="f-display text-2xl text-vb-text">Profile</h2>
        <p className="mt-1 text-sm text-vb-text-dim">
          The engine, roughly. We&apos;ll refine it as you ride.
        </p>
        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          <div>
            <label className={labelClasses}>Full name</label>
            <Input
              value={form.full_name}
              onChange={(e) => setForm({ ...form, full_name: e.target.value })}
            />
          </div>
          <div>
            <label className={labelClasses}>Weight (kg)</label>
            <Input
              type="number"
              step="0.1"
              value={form.weight_kg}
              onChange={(e) => setForm({ ...form, weight_kg: e.target.value })}
            />
          </div>
          <div>
            <label className={labelClasses}>FTP (watts)</label>
            <Input
              type="number"
              value={form.ftp}
              onChange={(e) => setForm({ ...form, ftp: e.target.value })}
            />
          </div>
          <div>
            <label className={labelClasses}>Max HR (bpm)</label>
            <Input
              type="number"
              value={form.max_hr}
              onChange={(e) => setForm({ ...form, max_hr: e.target.value })}
            />
          </div>
          <div>
            <label className={labelClasses}>Resting HR (bpm)</label>
            <Input
              type="number"
              value={form.resting_hr}
              onChange={(e) => setForm({ ...form, resting_hr: e.target.value })}
            />
          </div>
          <div>
            <label className={labelClasses}>Hours a week for the bike</label>
            <Input
              type="number"
              step="0.5"
              value={form.weekly_hours_available}
              onChange={(e) =>
                setForm({ ...form, weekly_hours_available: e.target.value })
              }
            />
          </div>
        </div>

        {/* Training Schedule */}
        <div className="mt-8">
          <h3 className="f-display text-lg text-vb-text">Which days can hurt?</h3>
          <p className="mt-1 mb-4 text-xs leading-relaxed text-vb-text-dim">
            Tap a day to cycle it through easy, rest and hard. Hard days carry
            the intensity, easy days carry the miles.
          </p>
          <div className="grid grid-cols-7 gap-2">
            {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((day, idx) => {
              const isHard = form.preferred_hard_days.includes(idx);
              const isRest = form.rest_days.includes(idx);
              return (
                <div key={day} className="text-center">
                  <p className="f-kicker mb-1.5 text-[10px] text-vb-text-muted">{day}</p>
                  <button
                    type="button"
                    onClick={() => {
                      if (isRest) {
                        // rest → hard
                        setForm({
                          ...form,
                          rest_days: form.rest_days.filter((d: number) => d !== idx),
                          preferred_hard_days: [...form.preferred_hard_days, idx],
                        });
                      } else if (isHard) {
                        // hard → normal
                        setForm({
                          ...form,
                          preferred_hard_days: form.preferred_hard_days.filter((d: number) => d !== idx),
                        });
                      } else {
                        // normal → rest
                        setForm({
                          ...form,
                          rest_days: [...form.rest_days, idx],
                        });
                      }
                    }}
                    className={cn(
                      "f-press w-full rounded-sm py-2 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] transition-colors",
                      isHard
                        ? "border border-vb-text bg-vb-text text-white"
                        : isRest
                          ? "border border-vb-border-subtle bg-vb-sunken text-vb-text-muted"
                          : "border border-vb-border bg-vb-surface text-vb-text-dim hover:border-vb-border-strong"
                    )}
                  >
                    {isHard ? "Hard" : isRest ? "Rest" : "Easy"}
                  </button>
                </div>
              );
            })}
          </div>
          <p className="mt-2 text-[11px] text-vb-text-muted">
            Hard days get the threshold and VO2 work. Easy days build the engine.
          </p>
        </div>

        <div className="mt-6 flex items-center gap-3">
          <Button
            size="sm"
            onClick={() => saveProfile.mutate()}
            disabled={saveProfile.isPending}
          >
            {saveProfile.isPending ? "Saving…" : "Save profile"}
          </Button>
          {saved && <span className="f-kicker text-vb-success">Saved</span>}
        </div>
      </section>

      {/* FTP Test */}
      <section className="rounded-sm border border-vb-border-subtle bg-vb-surface p-6">
        <h2 className="f-display text-2xl text-vb-text">FTP test</h2>
        <p className="mt-2 text-sm text-vb-text-dim">
          Ride 20 minutes as hard as you dare and give Forma the average.
          FTP is 95% of it, the classic test.
        </p>
        <div className="mt-4 flex items-end gap-3">
          <div className="flex-1">
            <label className={labelClasses}>20-minute average power (w)</label>
            <Input
              type="number"
              value={ftpTestPower}
              onChange={(e) => setFtpTestPower(e.target.value)}
              placeholder="e.g. 280"
            />
          </div>
          <Button
            onClick={() => submitFTPTest.mutate()}
            disabled={!ftpTestPower || submitFTPTest.isPending}
          >
            Calculate
          </Button>
        </div>
        {ftpResult && (
          <p className="f-data mt-3 text-sm font-semibold text-vb-text">
            {ftpResult}
          </p>
        )}
      </section>

      {/* Goals */}
      <section className="rounded-sm border border-vb-border-subtle bg-vb-surface p-6">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="f-display text-2xl text-vb-text">Goals</h2>
            <p className="mt-0.5 text-xs text-vb-text-muted">
              <Link
                href="/dashboard/goals"
                className="f-kicker text-vb-text-dim transition-colors hover:text-vb-red"
              >
                Full goals page →
              </Link>
            </p>
          </div>
          <Button size="sm" onClick={openAddGoal}>
            <Plus className="h-3.5 w-3.5" /> Add goal
          </Button>
        </div>

        {/* Goal Form (Add / Edit) */}
        {showGoalForm && (
          <div className="mt-4 rounded-sm border border-vb-border-subtle bg-vb-bg p-4">
            <div className="mb-3 flex items-center justify-between">
              <Kicker>{editingGoalId ? "Edit goal" : "New goal"}</Kicker>
              <button
                onClick={() => {
                  setShowGoalForm(false);
                  setEditingGoalId(null);
                }}
                className="rounded-sm p-1 text-vb-text-muted hover:text-vb-text"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              {/* Event Name */}
              <div>
                <label className={labelClasses}>Event name *</label>
                <Input
                  value={goalForm.event_name}
                  onChange={(e) =>
                    setGoalForm({ ...goalForm, event_name: e.target.value })
                  }
                  placeholder="e.g. Mallorca 312, Tour of Wessex"
                />
              </div>

              {/* Date */}
              <div>
                <label className={labelClasses}>Event date *</label>
                <Input
                  type="date"
                  value={goalForm.event_date}
                  onChange={(e) =>
                    setGoalForm({ ...goalForm, event_date: e.target.value })
                  }
                />
              </div>

              {/* Type */}
              <div>
                <label className={labelClasses}>Event type</label>
                <select
                  value={goalForm.event_type}
                  onChange={(e) =>
                    setGoalForm({ ...goalForm, event_type: e.target.value })
                  }
                  className={fieldClasses}
                >
                  {EVENT_TYPES.map((t) => (
                    <option key={t.value} value={t.value}>
                      {t.label}
                    </option>
                  ))}
                </select>
              </div>

              {/* Priority */}
              <div>
                <label className={labelClasses}>Priority</label>
                <select
                  value={goalForm.priority}
                  onChange={(e) =>
                    setGoalForm({ ...goalForm, priority: e.target.value })
                  }
                  className={fieldClasses}
                >
                  {PRIORITIES.map((p) => (
                    <option key={p.value} value={p.value}>
                      {p.label}
                    </option>
                  ))}
                </select>
              </div>

              {/* Target Duration */}
              <div>
                <label className={labelClasses}>Target duration (minutes)</label>
                <Input
                  type="number"
                  value={goalForm.target_duration_minutes}
                  onChange={(e) =>
                    setGoalForm({
                      ...goalForm,
                      target_duration_minutes: e.target.value,
                    })
                  }
                  placeholder="e.g. 240 (4 hours)"
                />
              </div>

              {/* Route URL */}
              <div>
                <label className={labelClasses}>Route or event URL</label>
                <Input
                  type="url"
                  value={goalForm.route_url}
                  onChange={(e) =>
                    setGoalForm({ ...goalForm, route_url: e.target.value })
                  }
                  placeholder="https://strava.com/routes/... or event website"
                />
              </div>
            </div>

            {/* Notes - full width */}
            <div className="mt-3">
              <label className={labelClasses}>Notes</label>
              <textarea
                value={goalForm.notes}
                onChange={(e) =>
                  setGoalForm({ ...goalForm, notes: e.target.value })
                }
                placeholder="The course, the climbs, what a good day looks like..."
                rows={3}
                className={`${fieldClasses} resize-none`}
              />
            </div>

            <div className="mt-4 flex gap-2">
              <Button
                size="sm"
                onClick={() => saveGoal.mutate()}
                disabled={
                  !goalForm.event_name ||
                  !goalForm.event_date ||
                  saveGoal.isPending
                }
              >
                {saveGoal.isPending
                  ? "Saving…"
                  : editingGoalId
                    ? "Update goal"
                    : "Save goal"}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setShowGoalForm(false);
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
              handleGpxUpload(gpxUploadingId, file);
            }
            e.target.value = "";
          }}
        />

        {/* Goal List */}
        <div className="mt-4 divide-y divide-vb-border-subtle">
          {goalsData?.goals.length === 0 && (
            <p className="py-4 text-center text-sm text-vb-text-dim">
              Nothing on the calendar yet. Give Forma a date to build the
              season backwards from.
            </p>
          )}
          {goalsData?.goals.map((goal) => (
            <div key={goal.id} className="py-3">
              <div className="flex items-start justify-between">
                <div className="min-w-0 flex-1">
                  <Link
                    href={`/dashboard/goals/${goal.id}`}
                    className="text-sm font-medium text-vb-text transition-colors hover:text-vb-red"
                  >
                    {goal.event_name}
                  </Link>
                  <p className="f-data mt-0.5 text-xs text-vb-text-dim">
                    {formatDate(goal.event_date)} &middot;{" "}
                    <span className="capitalize">
                      {goal.event_type.replace(/_/g, " ")}
                    </span>{" "}
                    &middot;{" "}
                    <span className="capitalize">
                      {goal.priority.replace(/_/g, " ")}
                    </span>
                    {goal.target_duration_minutes && (
                      <span>
                        {" "}
                        &middot; {formatDurationMinutes(goal.target_duration_minutes)}
                      </span>
                    )}
                  </p>

                  {/* Route data */}
                  {goal.route_data && !goal.route_data.error && (
                    <p className="f-data mt-0.5 flex items-center gap-1 text-xs text-vb-text-dim">
                      <MapPin className="h-3 w-3" />
                      {goal.route_data.total_distance_km &&
                        `${goal.route_data.total_distance_km}km`}
                      {goal.route_data.elevation_gain_m &&
                        ` · ${goal.route_data.elevation_gain_m}m climbing`}
                      {(goal.route_data.title || goal.route_data.name) &&
                        ` · ${goal.route_data.title || goal.route_data.name}`}
                    </p>
                  )}

                  {/* Route URL link */}
                  {goal.route_url && (
                    <a
                      href={goal.route_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="mt-0.5 inline-flex items-center gap-1 text-xs text-vb-text-muted transition-colors hover:text-vb-red"
                    >
                      <ExternalLink className="h-3 w-3" />
                      Event link
                    </a>
                  )}

                  {/* GPX badge */}
                  {goal.gpx_file_path && (
                    <span className="ml-2 inline-flex items-center gap-1 text-xs text-vb-text-dim">
                      <Mountain className="h-3 w-3" />
                      GPX uploaded
                    </span>
                  )}

                  {/* Notes preview */}
                  {goal.notes && (
                    <p className="mt-1 text-xs text-vb-text-muted line-clamp-2">
                      {goal.notes}
                    </p>
                  )}
                </div>

                <div className="ml-3 flex shrink-0 items-center gap-1.5">
                  {goal.days_until != null && goal.days_until > 0 && (
                    <span className="f-data bg-vb-sunken px-2 py-0.5 text-xs text-vb-text-dim">
                      {goal.days_until}d
                    </span>
                  )}
                  {/* Upload GPX */}
                  <button
                    onClick={() => {
                      setGpxUploadingId(goal.id);
                      gpxInputRef.current?.click();
                    }}
                    disabled={uploadGpx.isPending && gpxUploadingId === goal.id}
                    className="rounded-sm p-1.5 text-vb-text-muted transition-colors hover:bg-vb-sunken hover:text-vb-text"
                    title="Upload GPX file"
                  >
                    <Upload className="h-3.5 w-3.5" />
                  </button>
                  {/* Edit */}
                  <button
                    onClick={() => openEditGoal(goal)}
                    className="rounded-sm p-1.5 text-vb-text-muted transition-colors hover:bg-vb-sunken hover:text-vb-text"
                    title="Edit goal"
                  >
                    <Pencil className="h-3.5 w-3.5" />
                  </button>
                  {/* Delete */}
                  <button
                    onClick={() => {
                      if (confirm(`Delete "${goal.event_name}"?`)) {
                        deleteGoal.mutate(goal.id);
                      }
                    }}
                    className="rounded-sm p-1.5 text-vb-text-muted transition-colors hover:bg-vb-sunken hover:text-vb-red"
                    title="Delete goal"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* ============ DATA IN ============ */}
      <SectionHeader kicker="Data in" title="Where your rides come from" />
      <p className="-mt-4 max-w-2xl text-sm leading-relaxed text-vb-text-dim">
        Your rides are yours. Forma reads them straight from the source, your
        head unit, your files, your archive, so no platform can ever stand
        between you and your coach. Riding Garmin? Import your archive below,
        then use Dropbox for new rides, a bridge like tapiriik moves each
        ride there automatically.
      </p>

      {/* Wahoo: the push door — ride ends, ride arrives */}
      <WahooCard />

      {/* Account archive: the whole history in one zip */}
      <ArchiveImport />

      {/* Strava Integration */}
      <section className="rounded-sm border border-vb-border-subtle bg-vb-surface p-6">
        <div className="flex items-center justify-between gap-3">
          <h2 className="f-display text-2xl text-vb-text">Strava</h2>
          {stravaStatus?.connected && <Badge variant="ink">Linked</Badge>}
        </div>
        {stravaStatus?.connected ? (
          <div className="mt-4 space-y-3">
            <p className="f-data text-xs text-vb-text-muted">
              Athlete #{stravaStatus.athlete_id}
            </p>

            {/* Activity-scope warning: linked, but no permission to read rides */}
            {stravaNoActivityScope && (
              <div className="border border-vb-red/40 bg-vb-surface p-4">
                <Kicker flamme>Reconnect needed</Kicker>
                <p className="mt-2 text-sm text-vb-text-dim">
                  Strava is linked, but it hasn&apos;t given Forma permission to
                  read your rides, so nothing can import. Reconnect and tick
                  <span className="text-vb-text"> View data about your activities</span>{" "}
                  on Strava&apos;s permission screen.
                </p>
                <Button size="sm" variant="flamme" className="mt-3" onClick={reconnectStrava}>
                  Reconnect Strava
                  <Arrow />
                </Button>
              </div>
            )}

            {/* Backfill Progress */}
            {stravaStatus.backfill?.status === "running" && (
              <div className="border border-vb-border-subtle bg-vb-sunken p-4">
                <Kicker dot flamme>
                  Reading your history
                </Kicker>
                {stravaStatus.backfill.total ? (
                  <>
                    <p className="f-data mt-3 text-2xl font-semibold leading-none text-vb-text">
                      {stravaStatus.backfill.progress}
                      <span className="text-vb-text-muted">
                        {" "}/ {stravaStatus.backfill.total}
                      </span>
                    </p>
                    <div className="mt-3 h-1 w-full overflow-hidden bg-vb-border-subtle">
                      <div
                        className="h-full bg-vb-red transition-all duration-500"
                        style={{
                          width: `${Math.round(
                            (stravaStatus.backfill.progress /
                              stravaStatus.backfill.total) *
                              100
                          )}%`,
                        }}
                      />
                    </div>
                    <p className="mt-2 text-xs text-vb-text-dim">
                      rides read, remembered, working for you
                    </p>
                  </>
                ) : (
                  <p className="mt-2 text-xs text-vb-text-dim">
                    Scanning everything you&apos;ve ever logged…
                  </p>
                )}
              </div>
            )}

            {stravaStatus.backfill?.status === "completed" && (
              <div className="border border-vb-border-subtle bg-vb-sunken p-4">
                <p className="text-sm text-vb-text">
                  History in.{" "}
                  <span className="f-data font-semibold">
                    {stravaStatus.backfill.total}
                  </span>{" "}
                  activities read and remembered.
                </p>
                {stravaStatus.backfill.completed_at && (
                  <p className="f-data mt-0.5 text-xs text-vb-text-muted">
                    Finished {formatDate(stravaStatus.backfill.completed_at)}
                  </p>
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  className="mt-3"
                  onClick={async () => {
                    await strava.startBackfill(true);
                    queryClient.invalidateQueries({ queryKey: ["strava-status"] });
                  }}
                >
                  Re-import from Strava
                </Button>
              </div>
            )}

            {stravaStatus.backfill?.status === "paused" && (
              <div className="border border-vb-border bg-vb-sunken p-4">
                <Kicker>
                  <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                  Paused, Strava&apos;s 15-minute limit
                </Kicker>
                <p className="mt-2 text-xs text-vb-text-dim">
                  {stravaStatus.backfill.total
                    ? `${stravaStatus.backfill.progress} of ${stravaStatus.backfill.total} in. Back on it in about 15 minutes.`
                    : "Back on it in about 15 minutes."}
                </p>
              </div>
            )}

            {stravaStatus.backfill?.status === "paused_daily" && (
              <div className="border border-vb-border bg-vb-sunken p-4">
                <Kicker>
                  <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                  Paused, Strava&apos;s daily limit
                </Kicker>
                <p className="mt-2 text-xs text-vb-text-dim">
                  {stravaStatus.backfill.total
                    ? `${stravaStatus.backfill.progress} of ${stravaStatus.backfill.total} in. Strava allows 1,000 requests a day, Forma resumes at midnight UTC.`
                    : "Strava allows 1,000 requests a day, Forma resumes at midnight UTC."}
                </p>
              </div>
            )}

            {stravaStatus.backfill?.status === "failed" && (
              <div className="border border-vb-red/40 bg-vb-surface p-4">
                <Kicker flamme>Import stopped</Kicker>
                <p className="mt-2 text-sm text-vb-text-dim">
                  It got through {stravaStatus.backfill.progress || 0} of{" "}
                  {stravaStatus.backfill.total || "?"} before the error. Not
                  your fault. Retry and it picks up where it left off.
                </p>
                <Button
                  size="sm"
                  variant="ghost"
                  className="mt-3"
                  onClick={async () => {
                    await strava.startBackfill();
                    queryClient.invalidateQueries({ queryKey: ["strava-status"] });
                  }}
                >
                  Retry import
                </Button>
              </div>
            )}

            {!stravaStatus.backfill?.status && (
              <div className="border border-vb-border-subtle bg-vb-bg p-4">
                <p className="text-sm text-vb-text-dim">
                  Give Forma your full history and every ride you&apos;ve ever
                  logged starts working for you.
                </p>
                <Button
                  size="sm"
                  className="mt-3"
                  onClick={async () => {
                    await strava.startBackfill();
                    queryClient.invalidateQueries({ queryKey: ["strava-status"] });
                  }}
                >
                  Import full history
                </Button>
              </div>
            )}

            {stravaStatus.last_sync_at && (
              <p className="f-data text-xs text-vb-text-muted">
                Last synced {formatDate(stravaStatus.last_sync_at)}
              </p>
            )}

            <div className="flex gap-2">
              <Button
                size="sm"
                onClick={() => syncStrava.mutate()}
                disabled={syncStrava.isPending}
              >
                <RefreshCw
                  className={`h-3.5 w-3.5 ${syncStrava.isPending ? "animate-spin" : ""}`}
                />
                {syncStrava.isPending ? "Syncing…" : "Sync recent"}
              </Button>
              <Button
                size="sm"
                variant="quiet"
                onClick={async () => {
                  await strava.disconnect();
                  queryClient.invalidateQueries({
                    queryKey: ["strava-status"],
                  });
                }}
              >
                Disconnect
              </Button>
            </div>
          </div>
        ) : (
          <div className="mt-4 border border-dashed border-vb-border p-5">
            <p className="text-sm leading-relaxed text-vb-text-dim">
              The better way in from Strava is the archive above: one zip,
              your whole history, no ongoing tie to Strava&apos;s platform
              rules. Live linking still works if you prefer it.
            </p>
            <Button
              variant="ghost"
              className="mt-4"
              onClick={async () => {
                const { auth_url } = await strava.getAuthUrl();
                window.location.href = auth_url;
              }}
            >
              Connect Strava
              <Arrow />
            </Button>
          </div>
        )}
      </section>

      {/* Dropbox Integration */}
      <section className="rounded-sm border border-vb-border-subtle bg-vb-surface p-6">
        <div className="flex items-center justify-between gap-3">
          <h2 className="f-display text-2xl text-vb-text">Dropbox</h2>
          {dropboxStatus?.connected && <Badge variant="ink">Linked</Badge>}
        </div>
        {dropboxStatus?.connected ? (
          <div className="mt-4 space-y-3">
            {dropboxStatus.account_id && (
              <p className="f-data text-xs text-vb-text-muted">
                {dropboxStatus.account_id}
              </p>
            )}

            {/* Folder path */}
            <div className="border border-vb-border-subtle bg-vb-bg p-4">
              <div className="flex items-center justify-between">
                <div>
                  <Kicker className="mb-1">Sync folder</Kicker>
                  {editingFolder ? (
                    <div className="mt-1 flex gap-2">
                      <Input
                        value={folderPath}
                        onChange={(e) => setFolderPath(e.target.value)}
                        placeholder="/cycling"
                        className="h-8 text-xs"
                      />
                      <Button
                        size="sm"
                        onClick={() => updateDropboxFolder.mutate(folderPath)}
                      >
                        Save
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setEditingFolder(false)}
                      >
                        Cancel
                      </Button>
                    </div>
                  ) : (
                    <p className="f-data mt-0.5 flex items-center gap-1.5 text-sm text-vb-text">
                      <FolderSync className="h-3.5 w-3.5 text-vb-text-muted" />
                      {dropboxStatus.folder_path || "/cycling"}
                      <button
                        onClick={() => {
                          setFolderPath(
                            dropboxStatus.folder_path || "/cycling"
                          );
                          setEditingFolder(true);
                        }}
                        className="f-kicker ml-2 text-vb-text-muted transition-colors hover:text-vb-red"
                      >
                        Change
                      </button>
                    </p>
                  )}
                </div>
              </div>
              {dropboxStatus.last_sync_at && (
                <p className="f-data mt-2 text-xs text-vb-text-muted">
                  Last synced {formatDate(dropboxStatus.last_sync_at)}
                </p>
              )}
            </div>

            <div className="flex gap-2">
              <Button
                size="sm"
                onClick={() => syncDropbox.mutate()}
                disabled={syncDropbox.isPending}
              >
                <RefreshCw
                  className={`h-3.5 w-3.5 ${syncDropbox.isPending ? "animate-spin" : ""}`}
                />
                {syncDropbox.isPending ? "Syncing…" : "Sync FIT files"}
              </Button>
              <Button
                size="sm"
                variant="quiet"
                onClick={async () => {
                  await dropbox.disconnect();
                  queryClient.invalidateQueries({
                    queryKey: ["dropbox-status"],
                  });
                }}
              >
                Disconnect
              </Button>
            </div>
          </div>
        ) : (
          <div className="mt-4 border border-dashed border-vb-border p-5">
            <p className="text-sm leading-relaxed text-vb-text-dim">
              Drop FIT files into a Dropbox folder and Forma collects them
              from there. Handy for head units that never met Strava.
            </p>
            <Button
              variant="ghost"
              className="mt-4"
              onClick={async () => {
                const { auth_url } = await dropbox.getAuthUrl();
                window.location.href = auth_url;
              }}
            >
              Connect Dropbox
            </Button>
          </div>
        )}
      </section>
    </div>
  );
}

// ============ Your coach, name, face, tone ============

function CoachSection() {
  const { user, refreshUser } = useAuth();
  const [coachName, setCoachName] = useState(user?.coach_name ?? "Forma");
  const [avatar, setAvatar] = useState(normalizeAvatarKey(user?.coach_avatar));
  const [tone, setTone] = useState(user?.coach_tone ?? "balanced");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const dirty =
    coachName !== (user?.coach_name ?? "Forma") ||
    avatar !== normalizeAvatarKey(user?.coach_avatar) ||
    tone !== (user?.coach_tone ?? "balanced");

  async function save() {
    setSaving(true);
    setSaved(false);
    try {
      await users.updateProfile({
        coach_name: coachName.trim() || "Forma",
        coach_avatar: avatar,
        coach_tone: tone,
      });
      await refreshUser();
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="rounded-sm border border-vb-border-subtle bg-vb-surface p-6">
      <h2 className="f-display text-2xl text-vb-text">Your coach</h2>
      <p className="mt-1 text-sm text-vb-text-dim">
        Forma is your coach. Choose the manner, keep the standard. The
        coaching is world-class either way.
      </p>

      {/* Tone, the primary personalisation */}
      <div className="mt-6">
        <label className="f-kicker mb-3 block text-vb-text-muted">
          How Forma talks to you
        </label>
        <div className="f-stagger grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {COACH_TONES.map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => setTone(t.key)}
              className={cn(
                "f-press rounded-sm border p-4 text-left transition-colors",
                tone === t.key
                  ? "border-vb-red bg-vb-sunken"
                  : "border-vb-border-subtle bg-vb-surface hover:border-vb-border"
              )}
            >
              <p className="text-sm font-medium text-vb-text">{t.label}</p>
              <p className="mt-1 text-xs text-vb-text-dim">{t.description}</p>
              <p className="mt-2 text-xs italic leading-relaxed text-vb-text-muted">
                &ldquo;{t.sample}&rdquo;
              </p>
            </button>
          ))}
        </div>
      </div>

      {/* Optional custom name, coach stays Forma by default */}
      <div className="mt-6 max-w-xs">
        <label className="f-kicker mb-2 block text-vb-text-muted">
          Prefer another name? (optional)
        </label>
        <Input
          type="text"
          value={coachName}
          maxLength={30}
          onChange={(e) => setCoachName(e.target.value)}
          placeholder="Forma"
        />
        <p className="mt-1.5 text-[11px] text-vb-text-muted">
          Your coach stays Forma unless you change this.
        </p>
      </div>

      <div className="mt-5 flex items-center gap-3">
        <Button size="sm" onClick={save} disabled={saving || !dirty}>
          {saving ? "Saving…" : "Save coach"}
        </Button>
        {saved && (
          <span className="f-kicker text-vb-success">
            Done, {coachName.trim() || "Forma"} is ready.
          </span>
        )}
      </div>
    </section>
  );
}
