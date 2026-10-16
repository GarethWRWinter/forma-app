"use client";

import Link from "next/link";
import { Arrow, buttonVariants } from "@/components/ui/button";
import { CoachDot } from "@/components/ui/coach-glyph";
import type { GoalEvent, TrainingPlan } from "@/lib/api";
import { nextGoalAsk } from "@/lib/season";

/**
 * Between goals, the one thing on Today.
 *
 * The race is behind the rider and nothing is ahead. The coach asks for the
 * race report first, then proposes the next goal: one ask at a time, never
 * a menu, because the base block keeps every week in sessions meanwhile.
 * It takes the same slot as proposals and initiatives, for a fortnight
 * after the race.
 */
export function SeasonCard({
  lastGoal,
  needsDebrief,
  plan,
  coachName,
}: {
  lastGoal: GoalEvent;
  needsDebrief: boolean;
  plan?: TrainingPlan;
  coachName: string;
}) {
  return (
    <section className="f-rise border border-vb-border-subtle border-l-[3px] border-l-vb-red bg-vb-surface p-5 md:p-8">
      <div className="flex items-center gap-2.5">
        <CoachDot state="pulsing" size="9px" />
        <span className="f-kicker text-vb-text-muted">
          {coachName} · {needsDebrief ? "so, how did it go?" : "what comes next"}
        </span>
      </div>
      {needsDebrief ? (
        <>
          <h2 className="f-display mt-3 line-clamp-2 text-2xl text-vb-text md:text-3xl">
            {lastGoal.event_name}
          </h2>
          <p className="mt-3 max-w-2xl text-base leading-relaxed text-vb-text-dim">
            Tell me the result and how it felt. I’ll propose the next goal
            from&nbsp;it.
          </p>
          <Link
            href={`/dashboard/goals/${lastGoal.id}/assess`}
            className={buttonVariants({ variant: "flamme", size: "sm" }) + " mt-5"}
          >
            File the race report
            <Arrow />
          </Link>
        </>
      ) : (
        <>
          <h2 className="f-display mt-3 text-2xl text-vb-text md:text-3xl">
            What are we chasing&nbsp;next?
          </h2>
          <p className="mt-3 max-w-2xl text-base leading-relaxed text-vb-text-dim">
            {plan && !plan.goal_event_id && (
              <>
                Your base block is running, building the aerobic engine the
                next goal will need, so every week has its sessions.{" "}
              </>
            )}
            I’ll propose the next goal from how you’ve been riding, and build
            the season around it once you say&nbsp;yes.
          </p>
          <div className="mt-5 flex flex-wrap items-center gap-x-6 gap-y-3">
            <Link
              href={`/dashboard/coach?ask=${encodeURIComponent(nextGoalAsk(lastGoal))}`}
              className={buttonVariants({ variant: "flamme", size: "sm" })}
            >
              Find it with {coachName}
              <Arrow />
            </Link>
            <Link
              href="/dashboard/goals?new=1"
              className="f-kicker text-vb-text-muted transition-colors hover:text-vb-red"
            >
              Set the goal myself →
            </Link>
          </div>
        </>
      )}
    </section>
  );
}
