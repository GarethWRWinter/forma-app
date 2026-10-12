"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams } from "next/navigation";
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

/* Three journeys, one page. A finished event gets the full race report. A
   DNF is a debrief of a day that stopped early, so the questions are about
   why and where, not time and position. A DNS has no ride in it at all: the
   only honest questions are why, how the rider feels, and whether the goal
   still stands. Asking "happy with the day?" after a DNS was the bug that
   prompted this (12 Oct 2026). */

type Status = "completed" | "dnf" | "dns";

const STATUS_OPTIONS: { value: Status; label: string; hint: string; icon: typeof Trophy }[] = [
  { value: "completed", label: "Completed", hint: "Crossed the line", icon: Trophy },
  { value: "dnf", label: "DNF", hint: "Started, didn't finish", icon: Frown },
  { value: "dns", label: "DNS", hint: "Didn't start", icon: Ban },
];

const DNF_REASONS = [
  { value: "mechanical", label: "Mechanical" },
  { value: "crash", label: "A crash" },
  { value: "illness", label: "Felt ill on the day" },
  { value: "fuelling", label: "Cramps, fuelling or the stomach" },
  { value: "body", label: "The body said no" },
  { value: "head", label: "The head said no" },
  { value: "time_cut", label: "Missed a time cut" },
  { value: "weather", label: "Weather or the course" },
  { value: "other", label: "Something else" },
];

const DNS_REASONS = [
  { value: "illness", label: "Ill" },
  { value: "injury", label: "Injured" },
  { value: "life", label: "Life got in the way" },
  { value: "motivation", label: "Didn't want to, when it came to it" },
  { value: "logistics", label: "Travel, entry or logistics" },
  { value: "weather", label: "Weather or the event was cancelled" },
  { value: "wrong_goal", label: "Decided it wasn't the right goal" },
  { value: "other", label: "Something else" },
];

const DNS_NEXT = [
  { value: "same_event", label: "Same event, next time" },
  { value: "different_goal", label: "A different goal" },
  { value: "let_go", label: "Let this one go" },
  { value: "unsure", label: "Not sure yet" },
];

const selectClasses =
  "flex h-11 w-full rounded-sm border border-vb-border bg-vb-surface px-3 py-2 text-sm text-vb-text focus:border-vb-red focus:outline-none focus:ring-1 focus:ring-vb-red";

const textareaClasses =
  "w-full rounded-sm border border-vb-border bg-vb-surface px-3 py-2 text-sm text-vb-text placeholder:text-vb-text-muted focus:border-vb-red focus:outline-none focus:ring-1 focus:ring-vb-red resize-none";

function Slider({
  label,
  value,
  onChange,
  low,
  high,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  low: string;
  high: string;
}) {
  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <Kicker>{label}</Kicker>
        <span className="f-data text-2xl font-semibold text-vb-text">
          {value}
          <span className="text-sm text-vb-text-muted">/10</span>
        </span>
      </div>
      <input
        type="range"
        min="1"
        max="10"
        value={value}
        onChange={(e) => onChange(parseInt(e.target.value))}
        className="w-full accent-vb-red"
      />
      <div className="mt-1 flex justify-between font-mono text-[10px] uppercase tracking-[0.1em] text-vb-text-muted">
        <span>{low}</span>
        <span>{high}</span>
      </div>
    </div>
  );
}

function ChoiceGrid({
  options,
  value,
  onChange,
}: {
  options: { value: string; label: string }[];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          onClick={() => onChange(opt.value)}
          className={cn(
            "f-press rounded-sm border px-3 py-2.5 text-left text-sm",
            value === opt.value
              ? "border-vb-red bg-vb-surface text-vb-text"
              : "border-vb-border-subtle bg-vb-surface text-vb-text-dim hover:border-vb-border"
          )}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

export default function AssessPage() {
  const params = useParams();
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const goalId = params.id as string;
  const coachName = user?.coach_name || "Forma";

  const [status, setStatus] = useState<Status>("completed");
  const [selectedRideId, setSelectedRideId] = useState<string | null>(null);
  const [finishHours, setFinishHours] = useState("");
  const [finishMinutes, setFinishMinutes] = useState("");
  const [finishSeconds, setFinishSeconds] = useState("");
  const [finishPosition, setFinishPosition] = useState("");
  const [finishPositionTotal, setFinishPositionTotal] = useState("");
  const [satisfaction, setSatisfaction] = useState<number>(7);
  const [exertion, setExertion] = useState<number>(7);
  const [submitted, setSubmitted] = useState(false);

  // Completed: under the skin
  const [legsRating, setLegsRating] = useState<number>(5);
  const [fatigueOnset, setFatigueOnset] = useState("last_30min");
  const [followedPlan, setFollowedPlan] = useState("mostly");
  const [fuelingWorked, setFuelingWorked] = useState("yes");
  const [preRaceConfidence, setPreRaceConfidence] = useState<number>(7);
  const [wentWell, setWentWell] = useState("");
  const [toImprove, setToImprove] = useState("");

  // DNF
  const [dnfReason, setDnfReason] = useState("");
  const [dnfWhere, setDnfWhere] = useState("");
  const [dnfRightCall, setDnfRightCall] = useState("yes");
  const [dnfStory, setDnfStory] = useState("");

  // DNS
  const [dnsReason, setDnsReason] = useState("");
  const [dnsNext, setDnsNext] = useState("unsure");
  const [dnsFeeling, setDnsFeeling] = useState<number>(5);
  const [dnsStory, setDnsStory] = useState("");
  const [dnsPastSelf, setDnsPastSelf] = useState("");

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

  const canSubmit =
    status === "completed" ||
    (status === "dnf" && dnfReason !== "") ||
    (status === "dns" && dnsReason !== "");

  function handleSubmit() {
    const assessmentData: Record<string, unknown> = {};

    if (status === "completed") {
      let finishTimeSecs: number | null = null;
      if (finishHours || finishMinutes || finishSeconds) {
        finishTimeSecs =
          parseInt(finishHours || "0") * 3600 +
          parseInt(finishMinutes || "0") * 60 +
          parseInt(finishSeconds || "0");
      }
      assessmentData.legs_rating = legsRating;
      assessmentData.fatigue_onset = fatigueOnset;
      assessmentData.followed_plan = followedPlan;
      assessmentData.fueling_worked = fuelingWorked;
      assessmentData.pre_race_confidence = preRaceConfidence;
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
        assessment_data: assessmentData,
      });
      return;
    }

    if (status === "dnf") {
      assessmentData.dnf_reason = dnfReason;
      if (dnfWhere.trim()) assessmentData.dnf_where = dnfWhere.trim();
      assessmentData.dnf_right_call = dnfRightCall;
      assessmentData.legs_rating = legsRating;
      if (dnfStory.trim()) assessmentData.went_well = dnfStory.trim();
      if (toImprove.trim()) assessmentData.to_improve = toImprove.trim();
      submitMutation.mutate({
        status,
        actual_ride_id: selectedRideId,
        finish_time_seconds: null,
        finish_position: null,
        finish_position_total: null,
        // "How do you feel about stopping?" The coach reads it as the
        // rider's feeling about the day, which is what it is.
        overall_satisfaction: satisfaction,
        perceived_exertion: exertion,
        assessment_data: assessmentData,
      });
      return;
    }

    // DNS: no ride, no effort, no file.
    assessmentData.dns_reason = dnsReason;
    assessmentData.dns_next = dnsNext;
    if (dnsStory.trim()) assessmentData.dns_story = dnsStory.trim();
    if (dnsPastSelf.trim()) assessmentData.to_improve = dnsPastSelf.trim();
    submitMutation.mutate({
      status,
      actual_ride_id: null,
      finish_time_seconds: null,
      finish_position: null,
      finish_position_total: null,
      overall_satisfaction: dnsFeeling,
      perceived_exertion: null,
      assessment_data: assessmentData,
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

  // Success screen, in the voice the result deserves.
  if (submitted) {
    const filed = {
      completed: {
        kicker: "Race report filed",
        body: `Filed. Everything you told me about ${goal.event_name} now shapes how I build what comes next. When you're ready, let's talk it through.`,
        cta: "Debrief",
        heading: "Keep the wheels turning",
        sub: "Pick your next target and the season rebuilds around it, or start a fresh block and keep the fitness you just banked.",
      },
      dnf: {
        kicker: "Logged, and that took something",
        body: `Stopping is a decision, and most riders never make it well. I've got what happened at ${goal.event_name} on the record. When you're ready, let's work out what it was actually telling us.`,
        cta: "Talk it through",
        heading: "The fitness didn't go anywhere",
        sub: "A DNF costs you the day, not the block. Pick the next target, or keep building while it decides.",
      },
      dns: {
        kicker: "Logged. No ride, no judgement.",
        body:
          dnsNext === "let_go"
            ? `${goal.event_name} is off the list, and the training you did for it is still in your legs. When you're ready, tell me what you'd rather aim at.`
            : `Thanks for telling me straight. The work you put in for ${goal.event_name} is still yours, and the next start line is the one that counts. When you're ready, let's choose it.`,
        cta: "Talk it through",
        heading: dnsNext === "same_event" ? "Same event, next time" : "The fitness is still yours",
        sub:
          dnsNext === "same_event"
            ? "Put the next edition in as the goal and the season builds backwards from it, with what we've learned."
            : "Nothing you trained for has been lost. Name the next target when you're ready, or keep the block ticking over.",
      },
    }[status];

    return (
      <div className="mx-auto max-w-lg space-y-6 py-8">
        <CoachNote
          kicker={filed.kicker}
          coachName={coachName}
          className="f-rise"
          action={
            <Link
              href={`/dashboard/coach?goal_id=${goalId}&debrief=true`}
              className={buttonVariants({ variant: "flamme", size: "sm" })}
            >
              {filed.cta}
              <Arrow />
            </Link>
          }
        >
          {filed.body}
        </CoachNote>

        <div className="border border-vb-border-subtle bg-vb-surface p-5">
          <Kicker className="mb-1.5">What&apos;s next</Kicker>
          <h2 className="f-display text-xl text-vb-text">{filed.heading}</h2>
          <p className="mt-1 text-xs text-vb-text-dim">{filed.sub}</p>
          <div className="f-stagger mt-4 grid gap-2 sm:grid-cols-2">
            <Link
              href="/dashboard/goals?new=1"
              className="f-lift flex items-center gap-3 border border-vb-border-subtle bg-vb-surface p-3"
            >
              <Target className="h-5 w-5 shrink-0 text-vb-red" />
              <div>
                <p className="text-sm font-medium text-vb-text">
                  {status === "dns" && dnsNext === "same_event" ? "Enter the next edition" : "Name the next race"}
                </p>
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

  const heading = {
    completed: `How did ${goal.event_name} go?`,
    dnf: `What happened at ${goal.event_name}?`,
    dns: `${goal.event_name} didn't happen. What got in the way?`,
  }[status];

  const hasRides = !!candidateRidesData && candidateRidesData.rides.length > 0;

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
        <h1 className="f-display text-4xl text-vb-text md:text-5xl">{heading}</h1>
        <p className="f-kicker mt-3 text-vb-text-muted">
          {formatDate(goal.event_date)} · {goal.event_type.replace(/_/g, " ")}
        </p>
      </div>

      {/* Section 1: Result */}
      <div className="border border-vb-border-subtle bg-vb-surface p-5">
        <Kicker className="mb-4">01 · The result</Kicker>
        <div className="grid grid-cols-3 gap-3">
          {STATUS_OPTIONS.map((opt) => {
            const Icon = opt.icon;
            const isSelected = status === opt.value;
            return (
              <button
                key={opt.value}
                type="button"
                onClick={() => setStatus(opt.value)}
                className={cn(
                  "f-lift f-press flex flex-col items-center gap-2 rounded-sm border p-4",
                  isSelected
                    ? "border-vb-red bg-vb-surface text-vb-text"
                    : "border-vb-border-subtle bg-vb-surface text-vb-text-dim hover:border-vb-border"
                )}
              >
                <Icon className={cn("h-6 w-6", isSelected && "text-vb-red")} />
                <span className="font-mono text-xs font-semibold uppercase tracking-[0.08em]">
                  {opt.label}
                </span>
                <span className="text-[11px] text-vb-text-muted">{opt.hint}</span>
              </button>
            );
          })}
        </div>

        {status === "completed" && (
          <div className="mt-5 grid gap-4 sm:grid-cols-2">
            <div>
              <Kicker className="mb-2">Finish time</Kicker>
              <div className="flex items-center gap-1.5">
                <Input type="number" value={finishHours} onChange={(e) => setFinishHours(e.target.value)} placeholder="h" min="0" className="f-data w-16 text-center" />
                <span className="text-vb-text-muted">:</span>
                <Input type="number" value={finishMinutes} onChange={(e) => setFinishMinutes(e.target.value)} placeholder="m" min="0" max="59" className="f-data w-16 text-center" />
                <span className="text-vb-text-muted">:</span>
                <Input type="number" value={finishSeconds} onChange={(e) => setFinishSeconds(e.target.value)} placeholder="s" min="0" max="59" className="f-data w-16 text-center" />
              </div>
            </div>
            <div>
              <Kicker className="mb-2">Position, if you placed</Kicker>
              <div className="flex items-center gap-2">
                <Input type="number" value={finishPosition} onChange={(e) => setFinishPosition(e.target.value)} placeholder="Place" min="1" className="f-data w-20" />
                <span className="text-vb-text-muted">/</span>
                <Input type="number" value={finishPositionTotal} onChange={(e) => setFinishPositionTotal(e.target.value)} placeholder="Total" min="1" className="f-data w-20" />
              </div>
            </div>
          </div>
        )}

        {status === "dnf" && (
          <div className="mt-5 space-y-5">
            <div>
              <Kicker className="mb-2">What stopped the ride?</Kicker>
              <ChoiceGrid options={DNF_REASONS} value={dnfReason} onChange={setDnfReason} />
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Kicker className="mb-2">Where did it end?</Kicker>
                <Input
                  value={dnfWhere}
                  onChange={(e) => setDnfWhere(e.target.value)}
                  placeholder="e.g. 65 km in, or 2 h 40 on the second climb"
                />
              </div>
              <div>
                <Kicker className="mb-2">Was stopping the right call?</Kicker>
                <select value={dnfRightCall} onChange={(e) => setDnfRightCall(e.target.value)} className={selectClasses}>
                  <option value="yes">Yes, no doubt</option>
                  <option value="probably">Probably</option>
                  <option value="no">No, I could have carried on</option>
                  <option value="no_choice">There was no choice</option>
                </select>
              </div>
            </div>
          </div>
        )}

        {status === "dns" && (
          <div className="mt-5 space-y-5">
            <div>
              <Kicker className="mb-2">What got in the way?</Kicker>
              <ChoiceGrid options={DNS_REASONS} value={dnsReason} onChange={setDnsReason} />
            </div>
            <div>
              <Kicker className="mb-2">In your own words, if you want</Kicker>
              <textarea
                value={dnsStory}
                onChange={(e) => setDnsStory(e.target.value)}
                placeholder="e.g. Picked up a chest infection ten days out and it never cleared…"
                rows={2}
                className={textareaClasses}
              />
            </div>
          </div>
        )}
      </div>

      {/* Section 2: The ride file. There is none for a DNS. */}
      {status !== "dns" && hasRides && (
        <div className="border border-vb-border-subtle bg-vb-surface p-5">
          <Kicker className="mb-1">02 · The ride file</Kicker>
          <p className="mb-4 text-xs text-vb-text-dim">
            {status === "dnf"
              ? "Point Forma at the file from the day. A ride that stopped early still has a story in it."
              : "Point Forma at the ride from race day and the numbers join the story."}
          </p>
          <div className="space-y-2">
            {candidateRidesData!.rides.map((ride: CandidateRide) => (
              <button
                key={ride.id}
                type="button"
                onClick={() => setSelectedRideId(selectedRideId === ride.id ? null : ride.id)}
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
                      <p className="f-data text-sm font-semibold text-vb-text">{Math.round(ride.normalized_power)}W</p>
                      <p className="f-kicker text-[9px] text-vb-text-muted">NP</p>
                    </div>
                  )}
                  {ride.tss && (
                    <div>
                      <p className="f-data text-sm font-semibold text-vb-text">{ride.tss}</p>
                      <p className="f-kicker text-[9px] text-vb-text-muted">TSS</p>
                    </div>
                  )}
                  {selectedRideId === ride.id && <Check className="h-5 w-5 text-vb-red" />}
                </div>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Section 3: Gut check */}
      <div className="border border-vb-border-subtle bg-vb-surface p-5">
        <Kicker className="mb-4">{status === "dns" ? "02" : hasRides ? "03" : "02"} · Gut check</Kicker>
        <div className="space-y-5">
          {status === "completed" && (
            <>
              <Slider label="Happy with the day?" value={satisfaction} onChange={setSatisfaction} low="Disappointed" high="Delighted" />
              <Slider label="How hard did it feel?" value={exertion} onChange={setExertion} low="Very easy" high="Maximal" />
            </>
          )}
          {status === "dnf" && (
            <>
              <Slider label="How do you feel about stopping?" value={satisfaction} onChange={setSatisfaction} low="Gutted" high="At peace with it" />
              <Slider label="How hard was it up to that point?" value={exertion} onChange={setExertion} low="Very easy" high="Maximal" />
              <Slider label="How were the legs before it went wrong?" value={legsRating} onChange={setLegsRating} low="Heavy" high="Flying" />
            </>
          )}
          {status === "dns" && (
            <>
              <Slider label="How do you feel about missing it?" value={dnsFeeling} onChange={setDnsFeeling} low="Gutted" high="Relieved" />
              <div>
                <Kicker className="mb-2">Does the goal still stand?</Kicker>
                <ChoiceGrid options={DNS_NEXT} value={dnsNext} onChange={setDnsNext} />
              </div>
            </>
          )}
        </div>
      </div>

      {/* Section 4: Under the skin, only for a ride that happened in full */}
      {status === "completed" && (
        <div className="border border-vb-border-subtle bg-vb-surface p-5">
          <Kicker className="mb-4">{hasRides ? "04" : "03"} · Under the skin</Kicker>
          <div className="space-y-5">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Kicker className="mb-2">How were the legs?</Kicker>
                <input type="range" min="1" max="10" value={legsRating} onChange={(e) => setLegsRating(parseInt(e.target.value))} className="w-full accent-vb-red" />
                <div className="mt-0.5 flex justify-between font-mono text-[10px] uppercase tracking-[0.1em] text-vb-text-muted">
                  <span>Heavy</span>
                  <span className="f-data text-xs font-semibold text-vb-text">{legsRating}</span>
                  <span>Flying</span>
                </div>
              </div>
              <div>
                <Kicker className="mb-2">When did fatigue arrive?</Kicker>
                <select value={fatigueOnset} onChange={(e) => setFatigueOnset(e.target.value)} className={selectClasses}>
                  <option value="early">Early (first third)</option>
                  <option value="middle">Middle</option>
                  <option value="last_30min">Last 30 minutes</option>
                  <option value="never">Never really fatigued</option>
                </select>
              </div>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Kicker className="mb-2">Did the pacing plan hold?</Kicker>
                <select value={followedPlan} onChange={(e) => setFollowedPlan(e.target.value)} className={selectClasses}>
                  <option value="yes">Yes, nailed it</option>
                  <option value="mostly">Mostly</option>
                  <option value="no">No, went off plan</option>
                  <option value="no_plan">Had no plan</option>
                </select>
              </div>
              <div>
                <Kicker className="mb-2">Did the fuelling work?</Kicker>
                <select value={fuelingWorked} onChange={(e) => setFuelingWorked(e.target.value)} className={selectClasses}>
                  <option value="yes">Worked well</option>
                  <option value="some_issues">Some issues</option>
                  <option value="no">Didn&apos;t work</option>
                </select>
              </div>
            </div>
            <div>
              <Kicker className="mb-2">How confident were you on the start line?</Kicker>
              <input type="range" min="1" max="10" value={preRaceConfidence} onChange={(e) => setPreRaceConfidence(parseInt(e.target.value))} className="w-full accent-vb-red" />
              <div className="mt-0.5 flex justify-between font-mono text-[10px] uppercase tracking-[0.1em] text-vb-text-muted">
                <span>Very nervous</span>
                <span className="f-data text-xs font-semibold text-vb-text">{preRaceConfidence}</span>
                <span>Very confident</span>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Section 5: For the record */}
      <div className="border border-vb-border-subtle bg-vb-surface p-5">
        <Kicker className="mb-4">
          {status === "completed" ? (hasRides ? "05" : "04") : status === "dnf" ? (hasRides ? "04" : "03") : "03"} · For the record
        </Kicker>
        <div className="space-y-4">
          {status === "completed" && (
            <>
              <div>
                <Kicker className="mb-2">What went well?</Kicker>
                <textarea value={wentWell} onChange={(e) => setWentWell(e.target.value)} placeholder="e.g. Held 250W on the final climb, fuelled every 20 minutes, never panicked…" rows={2} className={textareaClasses} />
              </div>
              <div>
                <Kicker className="mb-2">What would you change?</Kicker>
                <textarea value={toImprove} onChange={(e) => setToImprove(e.target.value)} placeholder="e.g. Went out too hot in the first hour, need more long climbs in training, forgot the gels…" rows={2} className={textareaClasses} />
              </div>
            </>
          )}
          {status === "dnf" && (
            <>
              <div>
                <Kicker className="mb-2">What was going right before it stopped?</Kicker>
                <textarea value={dnfStory} onChange={(e) => setDnfStory(e.target.value)} placeholder="e.g. Pacing was spot on through the first two climbs, fuelling was going to plan…" rows={2} className={textareaClasses} />
              </div>
              <div>
                <Kicker className="mb-2">Knowing what you know now, what would you change?</Kicker>
                <textarea value={toImprove} onChange={(e) => setToImprove(e.target.value)} placeholder="e.g. Check the bike properly the night before, carry a second tube, back off when the stomach first turns…" rows={2} className={textareaClasses} />
              </div>
            </>
          )}
          {status === "dns" && (
            <div>
              <Kicker className="mb-2">What would you tell yourself three weeks ago?</Kicker>
              <textarea value={dnsPastSelf} onChange={(e) => setDnsPastSelf(e.target.value)} placeholder="e.g. Rest the week you got the cold instead of training through it, or don't put a race the weekend after a work trip…" rows={2} className={textareaClasses} />
            </div>
          )}
        </div>
      </div>

      {/* Submit */}
      <div className="flex gap-3">
        <Button
          variant="flamme"
          size="lg"
          onClick={handleSubmit}
          disabled={submitMutation.isPending || !canSubmit}
          className="flex-1"
        >
          {submitMutation.isPending
            ? "Filing…"
            : status === "completed"
              ? "File the race report"
              : status === "dnf"
                ? "Log the DNF"
                : "Log it and move on"}
          <Arrow />
        </Button>
        <Link href={`/dashboard/goals/${goalId}`} className={buttonVariants({ variant: "ghost", size: "lg" })}>
          Cancel
        </Link>
      </div>
      {!canSubmit && (
        <p className="-mt-4 text-xs text-vb-text-muted">
          {status === "dnf" ? "Pick what stopped the ride first." : "Pick what got in the way first."}
        </p>
      )}
    </div>
  );
}
