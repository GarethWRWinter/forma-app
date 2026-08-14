"use client";

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { training, type PlanProposal, type PlanProposalChange } from "@/lib/api";
import { Button, Arrow, buttonVariants } from "@/components/ui/button";
import { Kicker } from "@/components/ui/kicker";
import { CoachDot } from "@/components/ui/coach-glyph";
import { CadenceSpinner } from "@/components/ui/cadence-spinner";
import { ZONE_BLOCKS, SERIES } from "@/lib/palette";
import { formatDate, formatDuration, cn } from "@/lib/utils";

/**
 * The coach knocking on the door about the plan it wrote.
 *
 * This is the one place where Forma asks permission. Nothing here is applied
 * until the rider presses the button, so the card has to show the actual
 * edits, not a summary of them: what session, what date, what it becomes, and
 * why. It wears the flamme rail because it is the coach speaking, not the
 * system warning.
 */

/** The proposals query lives here so every surface reads the same cache. */
export function usePlanProposals() {
  return useQuery({
    queryKey: ["plan-proposals"],
    queryFn: () => training.getProposals(),
    staleTime: 5 * 60 * 1000,
    retry: false,
  });
}

/** How each edit reads before it happens, and after.
 *
 * The three canonical actions are what the backend validator actually stores
 * (update_workout, add_workout, skip_workout). Without them the card falls
 * back to humanising the raw key and labels an edit "Update workout", which is
 * the machine's word for it, not the coach's. The looser synonyms stay so an
 * older proposal written before the vocabulary settled still reads properly. */
const ACTION_WORDS: Record<string, { plan: string; done: string }> = {
  update_workout: { plan: "Changed", done: "Changed" },
  add_workout: { plan: "New session", done: "Added" },
  skip_workout: { plan: "Dropped", done: "Dropped" },
  add: { plan: "New session", done: "Added" },
  create: { plan: "New session", done: "Added" },
  replace: { plan: "Replaces", done: "Replaced" },
  swap: { plan: "Swapped for", done: "Swapped" },
  modify: { plan: "Changed", done: "Changed" },
  update: { plan: "Changed", done: "Changed" },
  edit: { plan: "Changed", done: "Changed" },
  move: { plan: "Moved", done: "Moved" },
  reschedule: { plan: "Moved", done: "Moved" },
  remove: { plan: "Dropped", done: "Dropped" },
  delete: { plan: "Dropped", done: "Dropped" },
  skip: { plan: "Dropped", done: "Dropped" },
};

function humanise(value: string): string {
  const words = value.replace(/_/g, " ").trim();
  if (!words) return "";
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function actionWords(action: string): { plan: string; done: string } {
  const key = (action ?? "").toLowerCase();
  const known = ACTION_WORDS[key];
  if (known) return known;
  const fallback = humanise(key) || "Change";
  return { plan: fallback, done: fallback };
}

/** Planned time, written the way a rider says it. The shared formatter keeps
    the zero minutes ("3h 0m"), which reads as a machine talking. */
function plannedTime(seconds: number): string {
  if (seconds >= 3600 && seconds % 3600 === 0) return `${seconds / 3600}h`;
  return formatDuration(seconds);
}

/** One line of hard facts under each edit: zone, time on the bike, load. */
function changeMeta(change: PlanProposalChange): string {
  const zone = change.workout_type
    ? (ZONE_BLOCKS[change.workout_type]?.label ?? humanise(change.workout_type))
    : null;
  const duration = change.planned_duration_seconds
    ? plannedTime(change.planned_duration_seconds)
    : null;
  const tss = change.planned_tss
    ? `${Math.round(change.planned_tss)} TSS`
    : null;
  return [zone, duration, tss].filter(Boolean).join(" · ");
}

function changeColor(change: PlanProposalChange): string {
  if (change.workout_type && ZONE_BLOCKS[change.workout_type]) {
    return ZONE_BLOCKS[change.workout_type].bg;
  }
  return SERIES.hairline;
}

/** The question the rider carries into the conversation, already loaded with
    the coach's own words so the chat opens mid thought rather than cold. */
function talkItThroughAsk(proposal: PlanProposal, coachName: string): string {
  return `You want to change my plan. You said: "${proposal.observation}" Talk me through it before I decide. What does it cost me, what does it buy me for my goal, and what happens if I leave the plan exactly as it is? I want ${coachName} being straight with me, not polite.`
    .replace(/\s+/g, " ")
    .trim();
}

export function PlanProposalCard({
  proposal,
  coachName = "Forma",
  className,
}: {
  proposal: PlanProposal;
  coachName?: string;
  className?: string;
}) {
  const queryClient = useQueryClient();
  const [declined, setDeclined] = useState(false);

  const accept = useMutation({
    mutationFn: () => training.acceptProposal(proposal.id),
    onSuccess: () => {
      // The plan has actually moved, so anything drawn from workouts is stale.
      // The proposals query is deliberately NOT invalidated: refetching it
      // would unmount this card before the rider has read what changed.
      queryClient.invalidateQueries({ queryKey: ["workouts-week"] });
      queryClient.invalidateQueries({ queryKey: ["today-workouts"] });
      queryClient.invalidateQueries({ queryKey: ["today-workout-detail"] });
      queryClient.invalidateQueries({ queryKey: ["plans"] });
      queryClient.invalidateQueries({ queryKey: ["plan-detail"] });
    },
  });

  const decline = useMutation({
    mutationFn: () => training.declineProposal(proposal.id),
    onSuccess: () => {
      setDeclined(true);
      queryClient.invalidateQueries({ queryKey: ["plan-proposals"] });
    },
  });

  // Declining is the rider closing the door. Nothing takes its place.
  if (declined) return null;

  const changes = proposal.changes ?? [];
  const shell = cn(
    "f-rise border border-vb-border-subtle border-l-[3px] border-l-vb-red bg-vb-surface p-5 md:p-8",
    className
  );

  // === Applied: what actually moved ===
  if (accept.isSuccess) {
    return (
      <section className={shell}>
        <div className="flex items-center gap-2.5">
          <CoachDot state="still" size="9px" />
          <Kicker flamme>The plan has moved</Kicker>
        </div>
        <p className="mt-3 max-w-2xl text-lg leading-[1.5] text-vb-text">
          {accept.data?.message ??
            "Done. The sessions below have moved. Ride what is on the sheet and I will keep watching it against your goal."}
        </p>
        <ul className="mt-5 border-t border-vb-border-subtle">
          {changes.map((change, i) => (
            <li
              key={change.workout_id ?? `${change.action}-${i}`}
              className="flex items-baseline gap-3 border-b border-vb-border-subtle py-2.5"
            >
              <span
                aria-hidden="true"
                className="inline-block h-2.5 w-2.5 shrink-0 translate-y-[1px]"
                style={{ background: changeColor(change) }}
              />
              <p className="text-sm leading-relaxed text-vb-text-dim">
                <span className="f-kicker mr-2 text-vb-text">
                  {actionWords(change.action).done}
                </span>
                {change.title ?? "This session"}
                {change.scheduled_date
                  ? ` · ${formatDate(change.scheduled_date)}`
                  : ""}
              </p>
            </li>
          ))}
        </ul>
        <div className="mt-5 flex flex-wrap items-center gap-x-6 gap-y-3">
          <Link
            href="/dashboard/training"
            className={buttonVariants({ variant: "ghost", size: "sm" })}
          >
            See the week <Arrow />
          </Link>
          <p className="f-signature text-xl leading-none">{coachName}</p>
        </div>
      </section>
    );
  }

  // === Pending: the ask ===
  return (
    <section className={shell}>
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="flex items-center gap-2.5">
          <CoachDot state="still" size="9px" />
          <span className="f-kicker text-vb-text-muted">
            {coachName} · a change to your plan
          </span>
        </div>
        {proposal.trigger && (
          <span className="f-kicker text-vb-text-muted">
            {humanise(proposal.trigger)}
          </span>
        )}
      </div>

      <h2 className="mt-3 max-w-2xl text-xl leading-[1.45] text-vb-text md:text-2xl md:leading-[1.4]">
        {proposal.observation}
      </h2>

      <p className="mt-3 max-w-2xl text-[15px] leading-relaxed text-vb-text-dim">
        {proposal.rationale}
      </p>

      {changes.length > 0 && (
        <>
          <Kicker className="mt-6 text-vb-text-muted">
            What changes · {changes.length}{" "}
            {changes.length === 1 ? "session" : "sessions"}
          </Kicker>
          <ul className="mt-2.5 border-t border-vb-border-subtle">
            {changes.map((change, i) => {
              const meta = changeMeta(change);
              return (
                <li
                  key={change.workout_id ?? `${change.action}-${i}`}
                  className="flex gap-3 border-b border-vb-border-subtle py-3.5"
                >
                  <span
                    aria-hidden="true"
                    className="mt-[7px] inline-block h-2.5 w-2.5 shrink-0"
                    style={{ background: changeColor(change) }}
                  />
                  <div className="min-w-0">
                    <p className="f-kicker text-vb-text-muted">
                      {actionWords(change.action).plan}
                      {change.scheduled_date
                        ? ` · ${formatDate(change.scheduled_date)}`
                        : ""}
                    </p>
                    <p className="mt-1 text-[15px] font-medium leading-snug text-vb-text">
                      {change.title ?? "This session"}
                    </p>
                    {meta && (
                      <p className="f-data mt-1 text-xs text-vb-text-dim">
                        {meta}
                      </p>
                    )}
                    {change.description && (
                      <p className="mt-1.5 text-sm leading-relaxed text-vb-text-dim">
                        {change.description}
                      </p>
                    )}
                    {change.why && (
                      <p className="mt-1.5 text-sm leading-relaxed text-vb-text-dim">
                        <span className="f-kicker mr-2 text-vb-red">Why</span>
                        {change.why}
                      </p>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </>
      )}

      <p className="f-signature mt-5 text-2xl leading-none">{coachName}</p>

      {accept.isPending ? (
        <div className="mt-5 flex items-center gap-3">
          <CadenceSpinner
            size={20}
            className="text-vb-red"
            title="Rewriting your plan"
          />
          <span className="text-sm text-vb-text-dim">
            Rewriting your plan&hellip;
          </span>
        </div>
      ) : (
        <div className="mt-5 flex flex-wrap items-center gap-x-4 gap-y-3">
          <Button
            variant="flamme"
            onClick={() => accept.mutate()}
            disabled={decline.isPending}
          >
            Make the change
            <Arrow />
          </Button>
          <Link
            href={`/dashboard/coach?ask=${encodeURIComponent(
              talkItThroughAsk(proposal, coachName)
            )}`}
            className={buttonVariants({ variant: "ghost" })}
          >
            Talk it through
          </Link>
          <button
            type="button"
            onClick={() => decline.mutate()}
            disabled={decline.isPending}
            className="f-kicker text-vb-text-muted transition-colors hover:text-vb-red disabled:opacity-40 sm:ml-auto"
          >
            {decline.isPending ? "Closing it" : "Not now"}
          </button>
        </div>
      )}

      {accept.isError && (
        <p className="f-kicker mt-3 text-vb-text-muted">
          That did not land. Nothing has changed. Try again in a moment.
        </p>
      )}
      {decline.isError && (
        <p className="f-kicker mt-3 text-vb-text-muted">
          I could not close that off. Try again in a moment.
        </p>
      )}
    </section>
  );
}
