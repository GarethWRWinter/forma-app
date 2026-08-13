"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { CoachDot } from "@/components/ui/coach-glyph";
import { StarterChips, useCoachStarters } from "@/components/coach/coach-starters";

/**
 * The coach leaning in, at the top of the day.
 *
 * Conversation is the product, so the dashboard opens with the coach
 * saying something real and a place to answer it. Typing here never
 * sends: it hands the words to /dashboard/coach?ask=... and the rider
 * presses send. The card is never absent, because the invitation to
 * talk is the point; with no line of the day it still opens the door.
 */
export function CoachInvite({
  coach,
  nudge,
  loading = false,
}: {
  coach: string;
  nudge?: string;
  loading?: boolean;
}) {
  const router = useRouter();
  const [draft, setDraft] = useState("");

  // The coach's own line steers which doors lead: the chips answer
  // what was just said, not a generic menu.
  const starters = useCoachStarters(nudge ?? "").slice(0, 3);

  const open = (text: string) => {
    const t = text.trim();
    if (!t) return;
    router.push(`/dashboard/coach?ask=${encodeURIComponent(t)}`);
  };

  const invitation =
    "I am here whenever you want to think out loud. Tell me how the legs feel, what went well this week, or what you are quietly worried about.";

  return (
    <section className="f-rise border border-vb-border-subtle border-l-[3px] border-l-vb-red bg-vb-surface p-5 md:p-8">
      <div className="flex items-center gap-2.5">
        <CoachDot state="still" size="9px" />
        <span className="f-kicker text-vb-text-muted">{coach}</span>
      </div>

      <p className="mt-3 max-w-2xl text-lg leading-[1.55] text-vb-text md:text-xl md:leading-[1.5]">
        {loading ? (
          <span className="text-vb-text-dim">{coach} is reading your week.</span>
        ) : (
          nudge || invitation
        )}
      </p>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          open(draft);
        }}
        className="mt-5 flex max-w-2xl items-stretch gap-2"
      >
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              open(draft);
            }
          }}
          aria-label={`Write back to ${coach}`}
          placeholder={nudge ? `Answer ${coach}` : `Talk to ${coach}`}
          className="h-11 min-w-0 flex-1 rounded-sm border border-vb-border bg-vb-bg px-3.5 text-[15px] text-vb-text placeholder:text-vb-text-muted focus:border-vb-red focus:outline-none"
        />
        <button
          type="submit"
          disabled={!draft.trim()}
          aria-label={`Send to ${coach}`}
          className="f-press f-arrow inline-flex h-11 shrink-0 items-center gap-2 rounded-sm bg-vb-red px-4 font-mono text-xs font-semibold uppercase tracking-[0.08em] text-white transition-colors hover:bg-vb-red-dim focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-vb-red focus-visible:ring-offset-2 focus-visible:ring-offset-vb-bg disabled:pointer-events-none disabled:opacity-40"
        >
          <span className="hidden sm:inline">Send</span>
          <span className="f-arrow-head" aria-hidden="true">
            →
          </span>
        </button>
      </form>

      {starters.length > 0 && (
        <>
          <p className="f-kicker mt-5 text-vb-text-muted">Or start here</p>
          <StarterChips
            starters={starters}
            onPick={open}
            className="mt-2.5 flex flex-wrap gap-2"
          />
        </>
      )}
    </section>
  );
}
