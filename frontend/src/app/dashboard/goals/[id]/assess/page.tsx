"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { useState } from "react";
import {
  ArrowLeft,
  Check,
  Trophy,
  Frown,
  Ban,
  Target,
  Calendar,
} from "lucide-react";
import { goals as goalsApi } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { formatDate, formatDuration, cn } from "@/lib/utils";
import type { CandidateRide, GoalAssessmentSubmit } from "@/lib/api";
import { Button, Arrow, buttonVariants } from "@/components/ui/button";
import { Kicker } from "@/components/ui/kicker";
import { Input } from "@/components/ui/input";
import { CoachNote } from "@/components/ui/coach-note";

const STATUS_OPTIONS = [
  { value: "completed", label: "Completed", icon: Trophy },
  { value: "dnf", label: "DNF", icon: Frown },
  { value: "dns", label: "DNS", icon: Ban },
];

const selectClasses =
  "flex h-11 w-full rounded-sm border border-vb-border bg-vb-surface px-3 py-2 text-sm text-vb-text focus:border-vb-red focus:outline-none focus:ring-1 focus:ring-vb-red";

const textareaClasses =
  "w-full rounded-sm border border-vb-border bg-vb-surface px-3 py-2 text-sm text-vb-text placeholder:text-vb-text-muted focus:border-vb-red focus:outline-none focus:ring-1 focus:ring-vb-red resize-none";

export default function AssessPage() {
  const params = useParams();
  const router = useRouter();
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const goalId = params.id as string;
  const coachName = user?.coach_name || "Forma";

  const [status, setStatus] = useState("completed");
  const [selectedRideId, setSelectedRideId] = useState<string | null>(null);
  const [finishHours, setFinishHours] = useState("");
  const [finishMinutes, setFinishMinutes] = useState("");
  const [finishSeconds, setFinishSeconds] = useState("");
  const [finishPosition, setFinishPosition] = useState("");
  const [finishPositionTotal, setFinishPositionTotal] = useState("");
  const [satisfaction, setSatisfaction] = useState<number>(7);
  const [exertion, setExertion] = useState<number>(7);
  const [submitted, setSubmitted] = useState(false);

  // Self-assessment sections
  const [legsRating, setLegsRating] = useState<number>(5);
  const [fatigueOnset, setFatigueOnset] = useState("last_30min");
  const [followedPlan, setFollowedPlan] = useState("mostly");
  const [fuelingWorked, setFuelingWorked] = useState("yes");
  const [preRaceConfidence, setPreRaceConfidence] = useState<number>(7);
  const [wentWell, setWentWell] = useState("");
  const [toImprove, setToImprove] = useState("");

  const { data: goal, isLoading } = useQuery({
    queryKey: ["goal", goalId],
    queryFn: () => goalsApi.get(goalId),
  });

  const { data: candidateRidesData } = useQuery({
    queryKey: ["candidate-rides", goalId],
    queryFn: () => goalsApi.getCandidateRides(goalId),
    enabled: !!goal,
  });

  const submitMutation = useMutation({
    mutationFn: (data: GoalAssessmentSubmit) =>
      goalsApi.submitAssessment(goalId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["goal", goalId] });
      queryClient.invalidateQueries({ queryKey: ["goals"] });
      setSubmitted(true);
    },
  });

  function handleSubmit() {
    let finishTimeSecs: number | null = null;
    if (finishHours || finishMinutes || finishSeconds) {
      finishTimeSecs =
        (parseInt(finishHours || "0") * 3600) +
        (parseInt(finishMinutes || "0") * 60) +
        parseInt(finishSeconds || "0");
    }

    const assessmentData: Record<string, unknown> = {};
    if (legsRating) assessmentData.legs_rating = legsRating;
    if (fatigueOnset) assessmentData.fatigue_onset = fatigueOnset;
    if (followedPlan) assessmentData.followed_plan = followedPlan;
    if (fuelingWorked) assessmentData.fueling_worked = fuelingWorked;
    if (preRaceConfidence) assessmentData.pre_race_confidence = preRaceConfidence;
    if (wentWell.trim()) assessmentData.went_well = wentWell.trim();
    if (toImprove.trim()) assessmentData.to_improve = toImprove.trim();

    submitMutation.mutate({
      status,
      actual_ride_id: selectedRideId,
      finish_time_seconds: finishTimeSecs,
      finish_position: finishPosition ? parseInt(finishPosition) : null,
      finish_position_total: finishPositionTotal ? parseInt(finishPositionTotal) : null,
      overall_satisfaction: satisfaction,
      perceived_exertion: exertion,
      assessment_data: Object.keys(assessmentData).length > 0 ? assessmentData : null,
    });
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="h-8 w-8 animate-spin rounded-full border border-vb-border-subtle border-t-vb-red" />
      </div>
    );
  }

  if (!goal) {
    return <div className="py-20 text-center text-vb-text-dim">Goal not found</div>;
  }

  // Success screen
  if (submitted) {
    return (
      <div className="mx-auto max-w-lg space-y-6 py-8">
        <CoachNote
          kicker="Race report filed"
          coachName={coachName}
          className="f-rise"
          action={
            <Link
              href={`/dashboard/coach?goal_id=${goalId}&debrief=true`}
              className={buttonVariants({ variant: "flamme", size: "sm" })}
            >
              Debrief
              <Arrow />
            </Link>
          }
        >
          Filed. Everything you told me about {goal.event_name} now shapes
          how I build what comes next. When you&apos;re ready, let&apos;s
          talk it through.
        </CoachNote>

        {/* What's next, invite user to plan their next block */}
        <div className="border border-vb-border-subtle bg-vb-surface p-5">
          <Kicker className="mb-1.5">What&apos;s next</Kicker>
          <h2 className="f-display text-xl text-vb-text">
            Keep the wheels turning
          </h2>
          <p className="mt-1 text-xs text-vb-text-dim">
            Pick your next target and the season rebuilds around it, or start
            a fresh block and keep the fitness you just banked.
          </p>
          <div className="f-stagger mt-4 grid gap-2 sm:grid-cols-2">
            <Link
              href="/dashboard/goals?new=1"
              className="f-lift flex items-center gap-3 border border-vb-border-subtle bg-vb-surface p-3"
            >
              <Target className="h-5 w-5 shrink-0 text-vb-red" />
              <div>
                <p className="text-sm font-medium text-vb-text">Name the next race</p>
                <p className="text-[11px] text-vb-text-muted">
                  The season builds backwards from it
                </p>
              </div>
            </Link>
            <Link
              href="/dashboard/training"
              className="f-lift flex items-center gap-3 border border-vb-border-subtle bg-vb-surface p-3"
            >
              <Calendar className="h-5 w-5 shrink-0 text-vb-text" />
              <div>
                <p className="text-sm font-medium text-vb-text">New training block</p>
                <p className="text-[11px] text-vb-text-muted">
                  Keep building while it decides
                </p>
              </div>
            </Link>
          </div>
          <Link
            href={`/dashboard/goals/${goalId}`}
            className="mt-4 block text-center font-mono text-[11px] uppercase tracking-[0.08em] text-vb-text-muted transition-colors hover:text-vb-red"
          >
            Or review the goal
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-2xl space-y-8">
      {/* ============ HEADER ============ */}
      <div className="f-rise">
        <Link
          href={`/dashboard/goals/${goalId}`}
          className="mb-3 inline-flex items-center gap-1 font-mono text-[11px] uppercase tracking-[0.08em] text-vb-text-dim transition-colors hover:text-vb-red"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> Back to the goal
        </Link>
        <Kicker className="mb-2">The debrief</Kicker>
        <h1 className="f-display text-4xl text-vb-text md:text-5xl">
          How did {goal.event_name} go?
        </h1>
        <p className="f-kicker mt-3 text-vb-text-muted">
          {formatDate(goal.event_date)} ·{" "}
          {goal.event_type.replace(/_/g, " ")}
        </p>
      </div>

      {/* Section 1: Result Status */}
      <div className="border border-vb-border-subtle bg-vb-surface p-5">
        <Kicker className="mb-4">01 · The result</Kicker>
        <div className="grid grid-cols-3 gap-3">
          {STATUS_OPTIONS.map((opt) => {
            const Icon = opt.icon;
            const isSelected = status === opt.value;
            return (
              <button
                key={opt.value}
                onClick={() => setStatus(opt.value)}
                className={cn(
                  "f-lift f-press flex flex-col items-center gap-2 rounded-sm border p-4",
                  isSelected
                    ? "border-vb-red bg-vb-surface text-vb-text"
                    : "border-vb-border-subtle bg-vb-surface text-vb-text-dim hover:border-vb-border"
                )}
              >
                <Icon
                  className={cn("h-6 w-6", isSelected && "text-vb-red")}
                />
                <span className="font-mono text-xs font-semibold uppercase tracking-[0.08em]">
                  {opt.label}
                </span>
              </button>
            );
          })}
        </div>

        {/* Finish time + position, only for completed */}
        {status === "completed" && (
          <div className="mt-5 grid gap-4 sm:grid-cols-2">
            <div>
              <Kicker className="mb-2">Finish time</Kicker>
              <div className="flex items-center gap-1.5">
                <Input
                  type="number"
                  value={finishHours}
                  onChange={(e) => setFinishHours(e.target.value)}
                  placeholder="h"
                  min="0"
                  className="f-data w-16 text-center"
                />
                <span className="text-vb-text-muted">:</span>
                <Input
                  type="number"
                  value={finishMinutes}
                  onChange={(e) => setFinishMinutes(e.target.value)}
                  placeholder="m"
                  min="0"
                  max="59"
                  className="f-data w-16 text-center"
                />
                <span className="text-vb-text-muted">:</span>
                <Input
                  type="number"
                  value={finishSeconds}
                  onChange={(e) => setFinishSeconds(e.target.value)}
                  placeholder="s"
                  min="0"
                  max="59"
                  className="f-data w-16 text-center"
                />
              </div>
            </div>
            <div>
              <Kicker className="mb-2">Position, if you placed</Kicker>
              <div className="flex items-center gap-2">
                <Input
                  type="number"
                  value={finishPosition}
                  onChange={(e) => setFinishPosition(e.target.value)}
                  placeholder="Place"
                  min="1"
                  className="f-data w-20"
                />
                <span className="text-vb-text-muted">/</span>
                <Input
                  type="number"
                  value={finishPositionTotal}
                  onChange={(e) => setFinishPositionTotal(e.target.value)}
                  placeholder="Total"
                  min="1"
                  className="f-data w-20"
                />
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Section 2: Link Ride */}
      {candidateRidesData && candidateRidesData.rides.length > 0 && (
        <div className="border border-vb-border-subtle bg-vb-surface p-5">
          <Kicker className="mb-1">02 · The ride file</Kicker>
          <p className="mb-4 text-xs text-vb-text-dim">
            Point Forma at the ride from race day and the numbers join the story.
          </p>
          <div className="space-y-2">
            {candidateRidesData.rides.map((ride: CandidateRide) => (
              <button
                key={ride.id}
                onClick={() =>
                  setSelectedRideId(selectedRideId === ride.id ? null : ride.id)
                }
                className={cn(
                  "f-lift flex w-full items-center justify-between rounded-sm border p-3 text-left",
                  selectedRideId === ride.id
                    ? "border-vb-red bg-vb-surface"
                    : "border-vb-border-subtle bg-vb-surface hover:border-vb-border"
                )}
              >
                <div>
                  <p className="text-sm font-medium text-vb-text">{ride.title}</p>
                  <p className="f-data text-xs text-vb-text-dim">
                    {ride.ride_date}
                    {ride.duration_seconds && ` · ${formatDuration(ride.duration_seconds)}`}
                  </p>
                </div>
                <div className="flex items-center gap-3 text-right">
                  {ride.normalized_power && (
                    <div>
                      <p className="f-data text-sm font-semibold text-vb-text">
                        {Math.round(ride.normalized_power)}W
                      </p>
                      <p className="f-kicker text-[9px] text-vb-text-muted">NP</p>
                    </div>
                  )}
                  {ride.tss && (
                    <div>
                      <p className="f-data text-sm font-semibold text-vb-text">
                        {ride.tss}
                      </p>
                      <p className="f-kicker text-[9px] text-vb-text-muted">TSS</p>
                    </div>
                  )}
                  {selectedRideId === ride.id && (
                    <Check className="h-5 w-5 text-vb-red" />
                  )}
                </div>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Section 3: Ratings */}
      <div className="border border-vb-border-subtle bg-vb-surface p-5">
        <Kicker className="mb-4">03 · Gut check</Kicker>
        <div className="space-y-5">
          {/* Satisfaction */}
          <div>
            <div className="mb-2 flex items-center justify-between">
              <Kicker>Happy with the day?</Kicker>
              <span className="f-data text-2xl font-semibold text-vb-text">
                {satisfaction}
                <span className="text-sm text-vb-text-muted">/10</span>
              </span>
            </div>
            <input
              type="range"
              min="1"
              max="10"
              value={satisfaction}
              onChange={(e) => setSatisfaction(parseInt(e.target.value))}
              className="w-full accent-vb-red"
            />
            <div className="mt-1 flex justify-between font-mono text-[10px] uppercase tracking-[0.1em] text-vb-text-muted">
              <span>Disappointed</span>
              <span>Delighted</span>
            </div>
          </div>

          {/* RPE */}
          <div>
            <div className="mb-2 flex items-center justify-between">
              <Kicker>How hard did it feel?</Kicker>
              <span className="f-data text-2xl font-semibold text-vb-text">
                {exertion}
                <span className="text-sm text-vb-text-muted">/10</span>
              </span>
            </div>
            <input
              type="range"
              min="1"
              max="10"
              value={exertion}
              onChange={(e) => setExertion(parseInt(e.target.value))}
              className="w-full accent-vb-red"
            />
            <div className="mt-1 flex justify-between font-mono text-[10px] uppercase tracking-[0.1em] text-vb-text-muted">
              <span>Very easy</span>
              <span>Maximal</span>
            </div>
          </div>
        </div>
      </div>

      {/* Section 4: Self-Assessment */}
      <div className="border border-vb-border-subtle bg-vb-surface p-5">
        <Kicker className="mb-4">04 · Under the skin</Kicker>
        <div className="space-y-5">
          {/* Physical */}
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <Kicker className="mb-2">How were the legs?</Kicker>
              <input
                type="range"
                min="1"
                max="10"
                value={legsRating}
                onChange={(e) => setLegsRating(parseInt(e.target.value))}
                className="w-full accent-vb-red"
              />
              <div className="mt-0.5 flex justify-between font-mono text-[10px] uppercase tracking-[0.1em] text-vb-text-muted">
                <span>Heavy</span>
                <span className="f-data text-xs font-semibold text-vb-text">
                  {legsRating}
                </span>
                <span>Flying</span>
              </div>
            </div>
            <div>
              <Kicker className="mb-2">When did fatigue arrive?</Kicker>
              <select
                value={fatigueOnset}
                onChange={(e) => setFatigueOnset(e.target.value)}
                className={selectClasses}
              >
                <option value="early">Early (first third)</option>
                <option value="middle">Middle</option>
                <option value="last_30min">Last 30 minutes</option>
                <option value="never">Never really fatigued</option>
              </select>
            </div>
          </div>

          {/* Pacing + Nutrition */}
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <Kicker className="mb-2">Did the pacing plan hold?</Kicker>
              <select
                value={followedPlan}
                onChange={(e) => setFollowedPlan(e.target.value)}
                className={selectClasses}
              >
                <option value="yes">Yes, nailed it</option>
                <option value="mostly">Mostly</option>
                <option value="no">No, went off plan</option>
                <option value="no_plan">Had no plan</option>
              </select>
            </div>
            <div>
              <Kicker className="mb-2">Did the fuelling work?</Kicker>
              <select
                value={fuelingWorked}
                onChange={(e) => setFuelingWorked(e.target.value)}
                className={selectClasses}
              >
                <option value="yes">Worked well</option>
                <option value="some_issues">Some issues</option>
                <option value="no">Didn&apos;t work</option>
              </select>
            </div>
          </div>

          {/* Mental */}
          <div>
            <Kicker className="mb-2">How confident were you on the start line?</Kicker>
            <input
              type="range"
              min="1"
              max="10"
              value={preRaceConfidence}
              onChange={(e) => setPreRaceConfidence(parseInt(e.target.value))}
              className="w-full accent-vb-red"
            />
            <div className="mt-0.5 flex justify-between font-mono text-[10px] uppercase tracking-[0.1em] text-vb-text-muted">
              <span>Very nervous</span>
              <span className="f-data text-xs font-semibold text-vb-text">
                {preRaceConfidence}
              </span>
              <span>Very confident</span>
            </div>
          </div>
        </div>
      </div>

      {/* Section 5: Takeaways */}
      <div className="border border-vb-border-subtle bg-vb-surface p-5">
        <Kicker className="mb-4">05 · For the record</Kicker>
        <div className="space-y-4">
          <div>
            <Kicker className="mb-2">What went well?</Kicker>
            <textarea
              value={wentWell}
              onChange={(e) => setWentWell(e.target.value)}
              placeholder="e.g. Held 250W on the final climb, fuelled every 20 minutes, never panicked…"
              rows={2}
              className={textareaClasses}
            />
          </div>
          <div>
            <Kicker className="mb-2">What would you change?</Kicker>
            <textarea
              value={toImprove}
              onChange={(e) => setToImprove(e.target.value)}
              placeholder="e.g. Went out too hot in the first hour, need more long climbs in training, forgot the gels…"
              rows={2}
              className={textareaClasses}
            />
          </div>
        </div>
      </div>

      {/* Submit */}
      <div className="flex gap-3">
        <Button
          variant="flamme"
          size="lg"
          onClick={handleSubmit}
          disabled={submitMutation.isPending}
          className="flex-1"
        >
          {submitMutation.isPending ? "Filing…" : "File the race report"}
          <Arrow />
        </Button>
        <Link
          href={`/dashboard/goals/${goalId}`}
          className={buttonVariants({ variant: "ghost", size: "lg" })}
        >
          Cancel
        </Link>
      </div>
    </div>
  );
}
