"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { coachInsights, type CoachInitiative } from "@/lib/api";
import { Button, Arrow } from "@/components/ui/button";
import { CoachDot } from "@/components/ui/coach-glyph";
import { cn } from "@/lib/utils";

/**
 * The coach tapping the rider on the shoulder.
 *
 * Not a notification. Someone who has been paying attention has noticed one
 * thing, says in plain words what it means, and asks. The question carries the
 * most weight on the card because the question is the entire point: the rider
 * is never expected to arrive with the right thing to ask.
 *
 * It wears the flamme rail so it is unmistakably Forma, but it is deliberately
 * a smaller object than the invite above it: tighter padding, no input, no
 * chips. Two full-scale slabs of coach would read as an app talking over
 * itself rather than a person following a thought through.
 */

export const COACH_INITIATIVE_KEY = ["coach-initiative"] as const;

/** What kind of noticing this was, in the coach's words rather than the
    generator's. An unrecognised kind gets no label at all, because a raw
    slug on the card gives the machine away. */
const KIND_LABEL: Record<string, string> = {
  open_loop: "picking this back up",
  ride_insight: "something in your riding",
  weekly_checkin: "the weekly check in",
};

/**
 * The coach's single pending thought, or null.
 *
 * With nothing pending we ask whether it has anything worth raising, because
 * the premise of the product is that the coach goes first instead of waiting
 * on a rider who may not know what to ask. Every safeguard, one at a time and
 * the cooldowns, lives on the backend, so a null here is the coach choosing
 * silence, and silence is the correct state most days.
 *
 * `enabled` is how a surface says the rider already has something else to
 * answer; the coach should not be composing a second thought meanwhile.
 */
export function useCoachInitiative(enabled = true) {
  return useQuery({
    queryKey: COACH_INITIATIVE_KEY,
    queryFn: async (): Promise<CoachInitiative | null> => {
      const pending = await coachInsights.getInitiative();
      if (pending) return pending;
      try {
        return await coachInsights.generateInitiative();
      } catch {
        // A generator that fell over has to look like a quiet day. The rider
        // should never be told the coach failed to think of something.
        return null;
      }
    },
    enabled,
    staleTime: 30 * 60 * 1000,
    retry: false,
  });
}

export function CoachInitiativeCard({
  initiative,
  coachName = "Forma",
  className,
}: {
  initiative: CoachInitiative;
  coachName?: string;
  className?: string;
}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [dismissed, setDismissed] = useState(false);

  const opened = useMutation({
    mutationFn: () => coachInsights.openInitiative(initiative.id),
  });

  const dismiss = useMutation({
    mutationFn: () => coachInsights.dismissInitiative(initiative.id),
  });

  const question = initiative.question?.trim();
  // With no question the headline is what the coach wanted to talk about, so
  // that is what the conversation opens on. The card is never a dead end.
  const ask = question || initiative.headline;

  const answer = () => {
    // Fire and forget. The rider lands in the conversation instantly, and the
    // fetch outlives this component, so the coach still learns it landed.
    // Whether that POST succeeded is not the rider's problem.
    opened.mutate();
    // Cleared by hand for the same reason as a dismissal. The server has just
    // stopped this being pending, so a rider who wanders back to the dashboard
    // must not meet the question they have already carried into the chat. Set
    // rather than invalidated, because a refetch would compose a fresh thought
    // while they are still mid-answer on this one.
    queryClient.setQueryData(COACH_INITIATIVE_KEY, null);
    router.push(`/dashboard/coach?ask=${encodeURIComponent(ask)}`);
  };

  const letItGo = () => {
    // One tap, no reason asked, gone before the server answers. The cache is
    // cleared by hand rather than invalidated: a refetch here would generate a
    // fresh thought straight into the space the rider has just cleared.
    setDismissed(true);
    queryClient.setQueryData(COACH_INITIATIVE_KEY, null);
    dismiss.mutate();
  };

  if (dismissed) return null;

  const label = KIND_LABEL[initiative.kind];

  return (
    <section
      className={cn(
        "f-rise border border-vb-border-subtle border-l-[3px] border-l-vb-red bg-vb-surface p-5 md:p-6",
        className
      )}
    >
      <div className="flex items-center gap-2.5">
        <CoachDot state="still" size="9px" />
        <span className="f-kicker text-vb-text-muted">
          {coachName}
          {label ? ` · ${label}` : ""}
        </span>
      </div>

      <h2 className="mt-3 max-w-2xl text-base font-medium leading-[1.45] text-vb-text md:text-[17px]">
        {initiative.headline}
      </h2>

      {initiative.body && (
        <p className="mt-2.5 max-w-2xl text-[15px] leading-relaxed text-vb-text-dim">
          {initiative.body}
        </p>
      )}

      {question && (
        <p className="mt-5 max-w-2xl border-t border-vb-border-subtle pt-5 text-lg leading-[1.45] text-vb-text md:text-xl">
          {question}
        </p>
      )}

      <div className="mt-5 flex flex-wrap items-center gap-x-4 gap-y-3">
        <Button variant="flamme" onClick={answer}>
          Answer {coachName}
          <Arrow />
        </Button>
        <button
          type="button"
          onClick={letItGo}
          className="f-kicker text-vb-text-muted transition-colors hover:text-vb-red sm:ml-auto"
        >
          Not now
        </button>
      </div>
    </section>
  );
}
