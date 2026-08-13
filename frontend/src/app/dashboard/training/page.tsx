"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import Link from "next/link";
import {
  ChevronLeft,
  ChevronRight,
  Plus,
  Check,
  X,
  Trophy,
} from "lucide-react";
import { training, goals, users } from "@/lib/api";
import { formatDuration, formatDate, cn } from "@/lib/utils";
import { useAuth } from "@/lib/auth-context";
import { ZONE_BLOCKS, ZONES, SERIES } from "@/lib/palette";
import { restLine } from "@/lib/voice";
import { Button, Arrow, buttonVariants } from "@/components/ui/button";
import { Kicker } from "@/components/ui/kicker";
import { DataTile } from "@/components/ui/data-tile";
import { EmptyState } from "@/components/ui/empty-state";
import { CoachNote } from "@/components/ui/coach-note";
import type { Workout, GoalEvent } from "@/lib/api";

// Plan-phase ramp, cold to hot as the season sharpens toward race day.
// Colours come from the palette only; flamme marks race day itself.
const PHASE_COLORS: Record<string, { bg: string; dark: boolean }> = {
  base:       { bg: ZONES.z1, dark: true },
  build:      { bg: ZONES.z3, dark: false },
  peak:       { bg: ZONES.z4, dark: true },
  taper:      { bg: SERIES.grey, dark: true },
  race:       { bg: SERIES.flamme, dark: true },
  recovery:   { bg: ZONES.z2, dark: true },
  transition: { bg: SERIES.hairline, dark: false },
};

/** Countdown wording for the masthead kicker. */
function daysOutLabel(days: number | null): string {
  if (days == null) return "date to come";
  if (days === 0) return "race day";
  if (days === 1) return "1 day out";
  return `${days} days out`;
}

/** Target time, written the way a rider says it. */
function formatTargetTime(minutes: number): string {
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

/** The check-in the rider hands the coach, prefilled and specific to the goal. */
function checkInAsk(goal: GoalEvent): string {
  const days = goal.days_until;
  const when =
    days === 0
      ? "It's today."
      : days === 1
        ? "It's tomorrow."
        : days != null
          ? `It's ${days} days out.`
          : "";
  return `Let's check in on ${goal.event_name}. ${when} Where am I really against it right now? Look at my recent training, tell me straight what's on track and what isn't, then ask me the one question I need to answer this week.`
    .replace(/\s+/g, " ")
    .trim();
}

/** Ask that sends the rider into goalcraft with the coach. */
const GOALCRAFT_ASK =
  "I want to craft a goal with you. Ask me one question at a time and help me find the goal I would actually love: what it is, why it matters to me, and who it makes me. When we have it, file it for me.";

function coachHref(ask: string): string {
  return `/dashboard/coach?ask=${encodeURIComponent(ask)}`;
}

/** One fact in the masthead detail strip. */
function MastheadStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="f-kicker text-white/40">{label}</dt>
      <dd className="f-data mt-1 truncate text-sm text-white">{value}</dd>
    </div>
  );
}

function getWeekDates(offset: number): { start: Date; dates: Date[] } {
  const today = new Date();
  const start = new Date(today);
  start.setDate(today.getDate() - today.getDay() + 1 + offset * 7); // Monday

  const dates = [];
  for (let i = 0; i < 7; i++) {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    dates.push(d);
  }
  return { start, dates };
}

function formatWeekDate(d: Date): string {
  return d.toISOString().split("T")[0];
}

export default function TrainingPage() {
  const queryClient = useQueryClient();
  const { user, refreshUser } = useAuth();
  const coachName = user?.coach_name || "Forma";
  const [weekOffset, setWeekOffset] = useState(0);
  const [showGenerate, setShowGenerate] = useState(false);
  const [genModel, setGenModel] = useState("traditional");
  const [showSchedule, setShowSchedule] = useState(false);
  const [hardDays, setHardDays] = useState<number[]>(
    user?.preferred_hard_days ?? []
  );
  const [restDays, setRestDays] = useState<number[]>(
    user?.rest_days ?? []
  );
  const [scheduleSaving, setScheduleSaving] = useState(false);

  const { start, dates } = getWeekDates(weekOffset);
  const weekStartStr = formatWeekDate(start);

  const { data: plans } = useQuery({
    queryKey: ["plans"],
    queryFn: () => training.getPlans(),
  });

  const { data: weekWorkouts, isLoading } = useQuery({
    queryKey: ["workouts-week", weekStartStr],
    queryFn: () => training.getWorkouts(undefined, weekStartStr),
  });

  const { data: goalsData } = useQuery({
    queryKey: ["goals"],
    queryFn: () => goals.list(),
  });

  // The plans list omits phases; fetch the active plan's detail for the timeline.
  const activePlanId = plans?.plans.find((p) => p.status === "active")?.id;
  const { data: planDetail } = useQuery({
    queryKey: ["plan-detail", activePlanId],
    queryFn: () => training.getPlan(activePlanId!),
    enabled: !!activePlanId,
  });

  const generateMutation = useMutation({
    mutationFn: (data: { periodization_model: string; goal_event_id?: string }) =>
      training.generatePlan(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["plans"] });
      queryClient.invalidateQueries({ queryKey: ["workouts-week"] });
      setShowGenerate(false);
    },
  });

  // Find the next upcoming goal (if any) — used to auto-link newly generated
  // plans to the goal so the plan peaks on race day.
  const nextUpcomingGoal = goalsData?.goals
    ?.filter((g) => g.status === "upcoming" && g.days_until != null && g.days_until >= 0)
    ?.sort((a, b) => (a.days_until ?? 0) - (b.days_until ?? 0))?.[0];

  const activePlan = plans?.plans.find((p) => p.status === "active");
  const hasActivePlan = !!activePlan;

  // Forma's honest read on the goal, requested from the masthead.
  const coachReadMutation = useMutation({
    mutationFn: (goalId: string) => goals.coachRead(goalId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["goals"] });
    },
  });

  // Masthead detail, resolved once so the JSX stays readable.
  const goalRoute =
    nextUpcomingGoal?.route_data && !nextUpcomingGoal.route_data.error
      ? nextUpcomingGoal.route_data
      : null;
  const goalCheckInHref = nextUpcomingGoal
    ? coachHref(checkInAsk(nextUpcomingGoal))
    : "";
  const goalWhyHref = nextUpcomingGoal
    ? coachHref(
        `Let's talk about my goal "${nextUpcomingGoal.event_name}". Ask me one question at a time and help me find why this one matters to me and who it's turning me into. Then save it to the goal.`
      )
    : "";

  const statusMutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      training.updateWorkout(id, { status }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workouts-week"] });
    },
  });

  // Group workouts by date
  const workoutsByDate: Record<string, Workout[]> = {};
  weekWorkouts?.forEach((w) => {
    const d = w.scheduled_date;
    if (!workoutsByDate[d]) workoutsByDate[d] = [];
    workoutsByDate[d].push(w);
  });

  // Group goal events by date
  const goalsByDate: Record<string, GoalEvent[]> = {};
  goalsData?.goals?.forEach((g) => {
    const d = g.event_date;
    if (!goalsByDate[d]) goalsByDate[d] = [];
    goalsByDate[d].push(g);
  });

  const today = new Date().toISOString().split("T")[0];
  const dayLabels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  // Where are we in the plan? Drives the header kicker and the timeline.
  const planPhases = planDetail?.phases ?? activePlan?.phases ?? [];
  const currentPhase = planPhases.find(
    (ph) => today >= ph.start_date && today <= ph.end_date
  );
  const planWeek = activePlan
    ? Math.max(
        1,
        Math.min(
          activePlan.total_weeks,
          Math.floor(
            (new Date(today).getTime() -
              new Date(activePlan.start_date).getTime()) /
              (7 * 86400000)
          ) + 1
        )
      )
    : null;

  // This week's shape: totals + an intensity-mix bar above the grid.
  const weekActive = (weekWorkouts ?? [])
    .filter((w) => w.status !== "skipped")
    .sort((a, b) => a.scheduled_date.localeCompare(b.scheduled_date));
  const weekSeconds = weekActive.reduce(
    (s, w) =>
      s +
      (w.actual_ride?.moving_time_seconds ??
        w.planned_duration_seconds ??
        0),
    0
  );
  const weekTss = Math.round(
    weekActive.reduce(
      (s, w) => s + (w.actual_ride?.tss ?? w.planned_tss ?? 0),
      0
    )
  );
  const weekDone = weekActive.filter(
    (w) => w.status === "completed" || !!w.actual_ride
  ).length;

  return (
    <div className="f-rise space-y-6">
      {/* ============ THE GOAL HOLDS — the plan's masthead ============ */}
      {nextUpcomingGoal ? (
        <section className="space-y-4 bg-[#101012] px-5 py-5 text-white md:px-8 md:py-6">
          {/* The name and the countdown */}
          <div className="flex items-start gap-4 md:gap-6">
            <div
              className="flex h-14 w-16 shrink-0 items-start justify-center bg-vb-red pt-2.5 sm:h-16 sm:w-[74px]"
              style={{ clipPath: "polygon(0 0, 100% 0, 50% 100%)" }}
            >
              <span className="f-data text-xl font-bold leading-none sm:text-2xl">
                {nextUpcomingGoal.days_until}
              </span>
            </div>
            <div className="min-w-0 flex-1">
              <p className="f-kicker text-vb-red">
                The goal holds · {daysOutLabel(nextUpcomingGoal.days_until)}
              </p>
              <Link
                href={`/dashboard/goals/${nextUpcomingGoal.id}`}
                className="f-display mt-1 line-clamp-2 block text-lg leading-tight transition-colors hover:text-vb-red sm:text-xl md:text-2xl"
              >
                {nextUpcomingGoal.event_name}
              </Link>
              <p className="f-kicker mt-1 text-white/50">
                {priorityLabel(nextUpcomingGoal.priority)}
              </p>
            </div>
          </div>

          {/* The detail, on the page rather than three taps away */}
          <dl className="grid grid-cols-2 gap-x-6 gap-y-3 border-t border-white/10 pt-4 sm:flex sm:flex-wrap sm:gap-x-10">
            <MastheadStat
              label="Date"
              value={formatDate(nextUpcomingGoal.event_date)}
            />
            <MastheadStat
              label="Discipline"
              value={nextUpcomingGoal.event_type.replace(/_/g, " ")}
            />
            {goalRoute?.total_distance_km != null && (
              <MastheadStat
                label="Distance"
                value={`${Math.round(goalRoute.total_distance_km)} km`}
              />
            )}
            {goalRoute?.elevation_gain_m != null && (
              <MastheadStat
                label="Climbing"
                value={`${Math.round(goalRoute.elevation_gain_m)} m`}
              />
            )}
            {nextUpcomingGoal.target_duration_minutes != null && (
              <MastheadStat
                label="Target time"
                value={formatTargetTime(
                  nextUpcomingGoal.target_duration_minutes
                )}
              />
            )}
          </dl>

          {/* The soul of it, written at goalcraft */}
          {nextUpcomingGoal.why || nextUpcomingGoal.becoming ? (
            <figure className="border-l-2 border-vb-red pl-4">
              <p className="f-kicker text-white/40">Why this one</p>
              {nextUpcomingGoal.why && (
                <blockquote className="mt-1.5 max-w-2xl text-sm leading-relaxed text-white/85">
                  &ldquo;{nextUpcomingGoal.why}&rdquo;
                </blockquote>
              )}
              {nextUpcomingGoal.becoming && (
                <figcaption className="f-kicker mt-2 text-white/50">
                  Becoming · {nextUpcomingGoal.becoming}
                </figcaption>
              )}
            </figure>
          ) : (
            <div className="border-l-2 border-white/15 pl-4">
              <p className="f-kicker text-white/40">No why yet</p>
              <p className="mt-1.5 max-w-xl text-sm text-white/60">
                This one has a date but nothing behind it. The why is what gets
                you out of the door in February.
              </p>
              <Link
                href={goalWhyHref}
                className="f-kicker mt-2 inline-block text-vb-red transition-colors hover:text-white"
              >
                Find it with {coachName} →
              </Link>
            </div>
          )}

          {/* The coach's read, compact, with the door to the full verdict */}
          {nextUpcomingGoal.coach_read ? (
            <div className="border-t border-white/10 pt-4">
              <p className="f-kicker text-white/40">{coachName}&apos;s read</p>
              <p className="mt-1.5 line-clamp-3 max-w-3xl text-sm leading-relaxed text-white/75">
                {nextUpcomingGoal.coach_read}
              </p>
              <Link
                href={`/dashboard/goals/${nextUpcomingGoal.id}`}
                className="f-kicker mt-2 inline-block text-white/60 transition-colors hover:text-white"
              >
                Read it in full →
              </Link>
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-x-5 gap-y-3 border-t border-white/10 pt-4">
              <p className="max-w-md text-sm text-white/60">
                {coachName} has not read this goal yet. The right size, the
                missing piece, the one change that would make it sing.
              </p>
              <Button
                variant="carbon"
                size="sm"
                onClick={() => coachReadMutation.mutate(nextUpcomingGoal.id)}
                disabled={coachReadMutation.isPending}
              >
                {coachReadMutation.isPending
                  ? "Reading your numbers…"
                  : "Get the read"}
              </Button>
              {coachReadMutation.isError && (
                <p className="f-kicker w-full text-white/50">
                  That one did not land. Try again in a moment.
                </p>
              )}
            </div>
          )}

          {/* The conversation, the whole point of having a coach */}
          <div className="flex flex-wrap items-center gap-x-6 gap-y-3 border-t border-white/10 pt-4">
            <Link
              href={goalCheckInHref}
              className={buttonVariants({
                variant: "flamme",
                size: "lg",
                className: "w-full sm:w-auto",
              })}
            >
              Check in on this goal
              <Arrow />
            </Link>
            <div className="flex items-center gap-5 sm:ml-auto">
              <Link
                href={`/dashboard/goals/${nextUpcomingGoal.id}`}
                className="f-kicker text-white/60 transition-colors hover:text-white"
              >
                Goal detail →
              </Link>
              <Link
                href="/dashboard/goals"
                className="f-kicker text-white/60 transition-colors hover:text-white"
              >
                All goals →
              </Link>
            </div>
          </div>
        </section>
      ) : (
        <section className="bg-[#101012] px-5 py-5 text-white md:px-8 md:py-6">
          <p className="f-kicker text-vb-red">The plan bends. The goal holds.</p>
          <p className="f-display mt-1 text-lg leading-tight sm:text-xl md:text-2xl">
            What are we chasing?
          </p>
          <p className="mt-2 max-w-md text-sm text-white/60">
            An event, a number, or simply the engine rebuilt. Give the plan its
            why and every week bends towards it.
          </p>
          <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-3">
            <Link
              href={coachHref(GOALCRAFT_ASK)}
              className={buttonVariants({
                variant: "flamme",
                size: "lg",
                className: "w-full sm:w-auto",
              })}
            >
              Find it with {coachName}
              <Arrow />
            </Link>
            <Link
              href="/dashboard/goals"
              className="f-kicker text-white/60 transition-colors hover:text-white"
            >
              Set the goal myself →
            </Link>
          </div>
        </section>
      )}

      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          {activePlan && (
            <Kicker className="mb-2">
              <span className="text-vb-red">
                {currentPhase
                  ? `${currentPhase.phase_type.replace(/_/g, " ")} phase`
                  : activePlan.name}
              </span>
              {planWeek ? ` · week ${planWeek} of ${activePlan.total_weeks}` : ""}
            </Kicker>
          )}
          <h1 className="f-display text-3xl text-vb-text md:text-4xl">
            {hasActivePlan ? activePlan?.name : "Training"}
          </h1>
          {!hasActivePlan && (
            <p className="mt-1 text-sm text-vb-text-dim">
              No plan yet. Let&apos;s build one around your goal.
            </p>
          )}
        </div>
        <Button onClick={() => setShowGenerate(!showGenerate)}>
          <Plus className="h-4 w-4" /> Build my season
        </Button>
      </div>

      {/* Prompt: user has an upcoming goal but no active plan */}
      {!hasActivePlan && nextUpcomingGoal && !showGenerate && (
        <CoachNote
          kicker={`A plan for ${nextUpcomingGoal.event_name}`}
          coachName={coachName}
          action={
            <Button
              variant="flamme"
              size="sm"
              onClick={() =>
                generateMutation.mutate({
                  periodization_model: "traditional",
                  // Let backend auto-detect primary goal from all upcoming goals
                })
              }
              disabled={generateMutation.isPending}
            >
              {generateMutation.isPending ? "Building…" : "Build my season"}
            </Button>
          }
        >
          {formatDate(nextUpcomingGoal.event_date)}
          {nextUpcomingGoal.days_until != null &&
            nextUpcomingGoal.days_until > 0 && (
              <> &middot; {nextUpcomingGoal.days_until} days away</>
            )}
          . I&apos;ll build the season backwards from race day and peak you
          right on it.
        </CoachNote>
      )}

      {/* Prompt: user has no active plan and no upcoming goal */}
      {!hasActivePlan && !nextUpcomingGoal && !showGenerate && (
        <EmptyState
          kicker="No plan yet"
          title="What are we aiming at?"
          action={
            <div className="flex flex-wrap justify-center gap-2">
              <Link
                href="/dashboard/goals"
                className={buttonVariants({ variant: "flamme" })}
              >
                Add a goal
              </Link>
              <Button variant="ghost" onClick={() => setShowGenerate(true)}>
                Build a 12-week block
              </Button>
            </div>
          }
        >
          Give me a date and I&apos;ll build the season backwards from it. Or
          start a 12-week block now and we&apos;ll aim it later.
        </EmptyState>
      )}

      {/* Generate Plan Dialog */}
      {showGenerate && (
        <div className="f-rise rounded-sm border border-vb-border-subtle bg-vb-surface p-5">
          <Kicker>Build my season</Kicker>
          <p className="mt-2 text-sm text-vb-text-dim">
            {nextUpcomingGoal
              ? `A periodised season peaking on ${nextUpcomingGoal.event_name}, ${formatDate(nextUpcomingGoal.event_date)}.`
              : "A 12-week periodised block, built around your profile."}
          </p>
          <div className="mt-4 flex flex-wrap items-end gap-3 sm:gap-4">
            <div className="min-w-[180px] flex-1">
              <Kicker className="mb-1.5">Periodisation model</Kicker>
              <select
                value={genModel}
                onChange={(e) => setGenModel(e.target.value)}
                className="h-11 w-full rounded-sm border border-vb-border bg-vb-surface px-3 py-2 text-sm text-vb-text focus-visible:border-vb-red focus-visible:outline-none"
              >
                <option value="traditional">Traditional</option>
                <option value="polarized">Polarized</option>
                <option value="sweet_spot">Sweet Spot</option>
              </select>
            </div>
            <Button
              variant="flamme"
              onClick={() =>
                generateMutation.mutate({
                  periodization_model: genModel,
                  // Let backend auto-detect primary goal from all upcoming goals
                })
              }
              disabled={generateMutation.isPending}
            >
              {generateMutation.isPending ? "Building…" : "Generate"}
            </Button>
            <Button variant="ghost" onClick={() => setShowGenerate(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      {/* Training Schedule */}
      <div className="flex items-center gap-3">
        <button
          onClick={() => setShowSchedule(!showSchedule)}
          className="f-kicker text-vb-text-muted transition-colors hover:text-vb-red"
        >
          {showSchedule ? "Hide schedule" : "Adjust availability"}
        </button>
      </div>
      {showSchedule && (
        <div className="f-rise rounded-sm border border-vb-border-subtle bg-vb-surface p-4">
          <p className="mb-3 text-xs text-vb-text-dim">
            Which days can hurt? Hard days take the intensity, rest days stay
            clear. Click to cycle: Easy, Rest, Hard.
          </p>
          <div className="grid grid-cols-7 gap-2">
            {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((day, idx) => {
              const isHard = hardDays.includes(idx);
              const isRest = restDays.includes(idx);
              return (
                <div key={day} className="text-center">
                  <p className="f-kicker mb-1.5 text-vb-text-muted">{day}</p>
                  <button
                    type="button"
                    onClick={() => {
                      if (isRest) {
                        setRestDays(restDays.filter(d => d !== idx));
                        setHardDays([...hardDays, idx]);
                      } else if (isHard) {
                        setHardDays(hardDays.filter(d => d !== idx));
                      } else {
                        setRestDays([...restDays, idx]);
                      }
                    }}
                    className={cn(
                      "f-press w-full rounded-sm py-2 font-mono text-[11px] font-semibold uppercase tracking-[0.08em] transition-colors",
                      isHard
                        ? "border border-vb-red bg-vb-red text-white"
                        : isRest
                          ? "border border-vb-border-subtle bg-vb-bg text-vb-text-muted"
                          : "border border-vb-border bg-vb-sunken text-vb-text-dim"
                    )}
                  >
                    {isHard ? "Hard" : isRest ? "Rest" : "Easy"}
                  </button>
                </div>
              );
            })}
          </div>
          <div className="mt-4 flex items-center gap-3">
            <Button
              size="sm"
              onClick={async () => {
                setScheduleSaving(true);
                try {
                  await users.updateProfile({
                    preferred_hard_days: hardDays,
                    rest_days: restDays,
                  });
                  await refreshUser();
                  setShowSchedule(false);
                } finally {
                  setScheduleSaving(false);
                }
              }}
              disabled={scheduleSaving}
            >
              {scheduleSaving ? "Saving…" : "Save schedule"}
            </Button>
            <p className="text-[10px] text-vb-text-muted">
              Rebuild your plan after saving and the week reshapes around it
            </p>
          </div>
        </div>
      )}

      {/* Week Navigation */}
      <div className="flex items-center justify-between">
        <button
          onClick={() => setWeekOffset((w) => w - 1)}
          className="f-press rounded-sm border border-vb-border-subtle p-2 text-vb-text-dim hover:border-vb-border-strong"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <div className="text-center">
          <p className="f-data text-sm font-semibold text-vb-text">
            {dates[0].toLocaleDateString("en-GB", {
              day: "numeric",
              month: "short",
            })}{" "}
            to{" "}
            {dates[6].toLocaleDateString("en-GB", {
              day: "numeric",
              month: "short",
              year: "numeric",
            })}
          </p>
          {weekOffset !== 0 && (
            <button
              onClick={() => setWeekOffset(0)}
              className="text-xs text-vb-red hover:text-vb-red-dim"
            >
              Back to this week
            </button>
          )}
        </div>
        <button
          onClick={() => setWeekOffset((w) => w + 1)}
          className="f-press rounded-sm border border-vb-border-subtle p-2 text-vb-text-dim hover:border-vb-border-strong"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>

      {/* Week summary: the shape of the week at a glance */}
      {weekActive.length > 0 && (
        <div>
          <div className="f-stagger grid grid-cols-3 gap-2 sm:gap-3">
            <DataTile
              label="Sessions"
              value={weekActive.length}
              sub={`${weekDone} of ${weekActive.length} done`}
            />
            <DataTile
              label="On the bike"
              value={weekSeconds / 3600}
              decimals={1}
              unit="h"
            />
            <DataTile label="TSS" value={weekTss} />
          </div>
          {(() => {
            const segs = weekActive.map((w) => ({
              w,
              dur:
                w.actual_ride?.moving_time_seconds ??
                w.planned_duration_seconds ??
                1800,
            }));
            const total = segs.reduce((s, x) => s + x.dur, 0) || 1;
            return (
              <div className="mt-2 flex h-2 gap-[3px]">
                {segs.map(({ w, dur }) => {
                  const zz = ZONE_BLOCKS[w.workout_type] ?? ZONE_BLOCKS.rest;
                  const done = w.status === "completed" || !!w.actual_ride;
                  return (
                    <div
                      key={w.id}
                      className="transition-opacity"
                      title={w.title}
                      style={{
                        width: `${(dur / total) * 100}%`,
                        backgroundColor: zz.bg,
                        opacity: done ? 1 : 0.45,
                      }}
                    />
                  );
                })}
              </div>
            );
          })()}
        </div>
      )}

      {/* Week Grid */}
      <div className="-mx-4 overflow-x-auto px-4 pb-2 sm:mx-0 sm:px-0 sm:pb-0">
      <div className="f-stagger grid min-w-[700px] grid-cols-7 gap-2 sm:min-w-0">
        {dates.map((date, i) => {
          const dateStr = formatWeekDate(date);
          const isToday = dateStr === today;
          const dayWorkouts = workoutsByDate[dateStr] || [];

          const dayGoals = goalsByDate[dateStr] || [];
          const activeWorkouts = dayWorkouts.filter(
            (w) => w.status !== "skipped"
          );
          const primary = activeWorkouts[0];
          const z = primary
            ? ZONE_BLOCKS[primary.workout_type] ?? ZONE_BLOCKS.rest
            : null;
          const colored = !!z;
          // Light text means a dark block; ZONE_BLOCKS.recovery.fg is white.
          const zDark = z?.fg === ZONE_BLOCKS.recovery.fg;
          const chipBg = zDark
            ? "rgba(255,255,255,0.22)"
            : "rgba(0,0,0,0.09)";

          return (
            <div
              key={dateStr}
              className={cn(
                "flex min-h-[176px] flex-col rounded-sm p-3",
                colored && primary && "f-lift",
                !colored &&
                  (isToday
                    ? "border border-vb-border-strong bg-vb-surface"
                    : "border border-vb-border-subtle bg-vb-surface")
              )}
              style={colored ? { backgroundColor: z!.bg, color: z!.fg } : {}}
            >
              {/* Day header */}
              <div className="mb-2 flex items-center justify-between">
                <span
                  className={cn(
                    "f-kicker flex items-center gap-1.5",
                    !colored && (isToday ? "text-vb-text" : "text-vb-text-muted")
                  )}
                  style={colored ? { color: z!.fg, opacity: 0.85 } : undefined}
                >
                  {isToday && (
                    <span
                      aria-hidden="true"
                      className={cn(
                        "inline-block h-1.5 w-1.5 rounded-full",
                        colored ? "bg-white" : "bg-vb-red"
                      )}
                    />
                  )}
                  {isToday ? "Today" : dayLabels[i]}
                </span>
                <span
                  className={cn(
                    "f-data text-xs",
                    !colored &&
                      (isToday
                        ? "font-semibold text-vb-text"
                        : "text-vb-text-muted")
                  )}
                  style={colored ? { opacity: 0.85 } : undefined}
                >
                  {date.getDate()}
                </span>
              </div>

              {/* Goal events */}
              {dayGoals.map((goal) => (
                <Link
                  key={goal.id}
                  href={`/dashboard/goals/${goal.id}`}
                  className={cn(
                    "mb-1.5 block rounded-sm p-2",
                    !colored &&
                      "border border-vb-border-subtle border-l-[3px] border-l-vb-red bg-vb-surface"
                  )}
                  style={colored ? { backgroundColor: chipBg } : undefined}
                >
                  <div className="flex items-center gap-1.5">
                    <Trophy
                      className={cn("h-3 w-3 shrink-0", !colored && "text-vb-red")}
                    />
                    <p
                      className={cn(
                        "truncate text-xs font-medium",
                        !colored && "text-vb-text"
                      )}
                    >
                      {goal.event_name}
                    </p>
                  </div>
                  <p
                    className={cn(
                      "mt-1 font-mono text-[9px] uppercase tracking-[0.1em]",
                      !colored && "text-vb-text-muted"
                    )}
                    style={colored ? { opacity: 0.8 } : undefined}
                  >
                    {goal.priority.replace(/_/g, " ").toUpperCase()} ·{" "}
                    {goal.event_type.replace(/_/g, " ")}
                  </p>
                </Link>
              ))}

              {/* Primary workout fills the coloured cell */}
              {primary ? (
                <div className="mt-auto">
                  <Link
                    href={`/dashboard/training/${primary.id}`}
                    className="block"
                  >
                    <div className="flex items-start justify-between gap-1.5">
                      <div className="min-w-0">
                        <p
                          className="font-mono text-[9px] font-semibold uppercase tracking-[0.12em]"
                          style={{ opacity: 0.72 }}
                        >
                          {z!.label}
                        </p>
                        <p className="f-display mt-0.5 text-[15px] leading-[1.12]">
                          {primary.title}
                        </p>
                      </div>
                      {primary.execution_score != null && (
                        <span
                          className="f-data shrink-0 rounded-sm px-1.5 py-0.5 text-[9px] font-semibold"
                          style={{ backgroundColor: chipBg }}
                        >
                          {primary.execution_score.toFixed(1)}
                        </span>
                      )}
                    </div>
                  </Link>

                  {!primary.actual_ride ? (
                    (primary.planned_duration_seconds ||
                      primary.planned_tss) && (
                      <p
                        className="f-data mt-1.5 text-[11px] font-medium"
                        style={{ opacity: 0.85 }}
                      >
                        {primary.planned_duration_seconds
                          ? formatDuration(primary.planned_duration_seconds)
                          : ""}
                        {primary.planned_duration_seconds && primary.planned_tss
                          ? " · "
                          : ""}
                        {primary.planned_tss
                          ? `${Math.round(primary.planned_tss)} TSS`
                          : ""}
                      </p>
                    )
                  ) : (
                    <p
                      className="f-data mt-1.5 text-[11px] font-medium"
                      style={{ opacity: 0.95 }}
                    >
                      ✓{" "}
                      {formatDuration(
                        primary.actual_ride.moving_time_seconds ??
                          primary.actual_ride.duration_seconds ??
                          0
                      )}
                      {primary.actual_ride.tss != null
                        ? ` · ${Math.round(primary.actual_ride.tss)} TSS`
                        : ""}
                    </p>
                  )}

                  {primary.status === "planned" && (
                    <div className="mt-2.5 flex gap-1.5">
                      <button
                        onClick={() =>
                          statusMutation.mutate({
                            id: primary.id,
                            status: "completed",
                          })
                        }
                        title="Mark completed"
                        className="f-press flex h-[22px] w-[22px] items-center justify-center rounded-sm"
                        style={{ backgroundColor: chipBg, color: z!.fg }}
                      >
                        <Check className="h-3 w-3" />
                      </button>
                      <button
                        onClick={() =>
                          statusMutation.mutate({
                            id: primary.id,
                            status: "skipped",
                          })
                        }
                        title="Skip"
                        className="f-press flex h-[22px] w-[22px] items-center justify-center rounded-sm"
                        style={{ backgroundColor: chipBg, color: z!.fg }}
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </div>
                  )}
                  {primary.status === "completed" && !primary.actual_ride && (
                    <p
                      className="mt-1.5 text-[10px] font-medium"
                      style={{ opacity: 0.85 }}
                    >
                      Done ✓
                    </p>
                  )}

                  {/* Additional workouts (rare) */}
                  {activeWorkouts.slice(1).map((w) => {
                    const wz = ZONE_BLOCKS[w.workout_type] ?? ZONE_BLOCKS.rest;
                    return (
                      <Link
                        key={w.id}
                        href={`/dashboard/training/${w.id}`}
                        className="f-lift mt-2 block truncate rounded-sm px-2 py-1.5 text-[11px] font-medium"
                        style={{ backgroundColor: wz.bg, color: wz.fg }}
                      >
                        {wz.label.split(" · ")[0]} · {w.title}
                      </Link>
                    );
                  })}
                </div>
              ) : dayGoals.length === 0 ? (
                <p className="my-auto text-center text-[11px] italic text-vb-text-muted">
                  {restLine(dateStr)}
                </p>
              ) : null}
            </div>
          );
        })}
      </div>
      </div>

      {/* Plan timeline: proportional phases with a you-are-here marker */}
      {activePlan && planPhases.length > 0 && (
        <div className="rounded-sm border border-vb-border-subtle bg-vb-surface p-5">
          <div className="mb-5 flex flex-wrap items-baseline justify-between gap-2">
            <div>
              <Kicker className="mb-1">The season</Kicker>
              <h2 className="f-display text-xl text-vb-text">
                The road to {nextUpcomingGoal?.event_name ?? "race day"}
              </h2>
            </div>
            <p className="f-data text-xs text-vb-text-muted">
              {formatDate(activePlan.start_date)} to{" "}
              {formatDate(activePlan.end_date)}
            </p>
          </div>
          {(() => {
            const planStart = new Date(activePlan.start_date).getTime();
            const planEnd =
              new Date(activePlan.end_date).getTime() + 86400000;
            const span = Math.max(1, planEnd - planStart);
            const todayPct = Math.min(
              99.5,
              Math.max(0, ((new Date(today).getTime() - planStart) / span) * 100)
            );
            const phases = [...planPhases].sort(
              (a, b) => a.sort_order - b.sort_order
            );
            return (
              <div className="relative">
                <div className="flex h-9 gap-[2px] overflow-hidden">
                  {phases.map((ph) => {
                    const w =
                      ((new Date(ph.end_date).getTime() -
                        new Date(ph.start_date).getTime() +
                        86400000) /
                        span) *
                      100;
                    const pc =
                      PHASE_COLORS[ph.phase_type] ?? PHASE_COLORS.recovery;
                    const isCur =
                      today >= ph.start_date && today <= ph.end_date;
                    const isPast = today > ph.end_date;
                    return (
                      <div
                        key={ph.id}
                        className="flex items-center justify-center overflow-hidden"
                        style={{
                          width: `${w}%`,
                          backgroundColor: pc.bg,
                          opacity: isCur ? 1 : isPast ? 0.45 : 0.75,
                        }}
                      >
                        <span
                          className={cn(
                            "truncate px-2 font-mono text-[9px] font-semibold uppercase tracking-[0.12em]",
                            pc.dark ? "text-white" : "text-vb-text"
                          )}
                        >
                          {ph.phase_type.replace(/_/g, " ")}
                        </span>
                      </div>
                    );
                  })}
                </div>
                {/* You are here */}
                <div
                  className="pointer-events-none absolute -top-1.5 bottom-[-6px] w-[2px] bg-vb-red"
                  style={{ left: `${todayPct}%` }}
                />
              </div>
            );
          })()}
          {currentPhase && (
            <p className="mt-4 text-xs text-vb-text-dim">
              <span className="font-mono font-semibold uppercase tracking-[0.08em] text-vb-text">
                {currentPhase.phase_type.replace(/_/g, " ")}
              </span>
              {" · "}
              {formatDate(currentPhase.start_date)} to{" "}
              {formatDate(currentPhase.end_date)}
              {currentPhase.focus ? <> · {currentPhase.focus}</> : null}
              {" · "}
              {currentPhase.workout_count} workouts
            </p>
          )}
        </div>
      )}
    </div>
  );
}
