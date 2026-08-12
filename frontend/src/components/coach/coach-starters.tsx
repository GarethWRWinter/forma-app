"use client";

import { useQuery } from "@tanstack/react-query";
import { goals as goalsApi } from "@/lib/api";

/**
 * Conversation starters — the coach's range, signposted. Context-aware:
 * a goal in race week, a goal with no why, or a race report pending each
 * open a different door. Tapping never sends; it hands the rider the
 * words and leaves them holding the pen.
 */

export interface Starter {
  label: string;
  ask: string;
}

const HABIT_STARTERS: Starter[] = [
  {
    label: "I keep falling off the plan",
    ask: "I keep falling off the plan after a couple of weeks. Help me work out why it happens and design a week I can actually keep.",
  },
  {
    label: "Fix my sleep",
    ask: "My sleep is a mess and I know it's costing me. Coach me through fixing it like it's training, one change at a time.",
  },
  {
    label: "Find me more hours",
    ask: "I can't find enough time to train. Audit my week with me and find where the hours are actually going.",
  },
  {
    label: "My head gets loud when it hurts",
    ask: "My head gets loud in the hard parts of a ride and talks me into backing off. Help me build a script for those moments.",
  },
];

export function useCoachStarters(max = 5): Starter[] {
  const { data } = useQuery({
    queryKey: ["goals"],
    queryFn: () => goalsApi.list(),
    staleTime: 60_000,
  });

  const starters: Starter[] = [];
  const list = data?.goals ?? [];
  const upcoming = list.filter((g) => g.status === "upcoming");

  const pending = list.find((g) => g.needs_assessment);
  if (pending) {
    starters.push({
      label: `Debrief ${pending.event_name}`,
      ask: `Let's talk about how ${pending.event_name} went, beyond the numbers. Help me see how far I've come and what the pursuit made of me. Be honest with me.`,
    });
  }

  const raceSoon = upcoming.find(
    (g) => g.days_until != null && g.days_until <= 14
  );
  if (raceSoon) {
    starters.push({
      label: `Get my head ready for ${raceSoon.event_name}`,
      ask: `${raceSoon.event_name} is close and my head is getting louder than my legs. Walk me through getting race ready: the plan, the nerves, the what-ifs.`,
    });
  }

  const noWhy = upcoming.find((g) => !g.why);
  if (noWhy) {
    starters.push({
      label: `Give ${noWhy.event_name} its why`,
      ask: `Let's talk about my goal "${noWhy.event_name}". Ask me one question at a time and help me find why this one matters to me and who it's turning me into. Then save it to the goal.`,
    });
  }

  if (upcoming.length === 0) {
    starters.push({
      label: "Craft my next goal",
      ask: "I want to craft a new goal with you. Ask me one question at a time and help me find the goal I would actually love: what it is, why it matters to me, and who it makes me. When we have it, file it for me.",
    });
  }

  for (const s of HABIT_STARTERS) {
    if (starters.length >= max) break;
    starters.push(s);
  }
  return starters.slice(0, max);
}

export function StarterChips({
  starters,
  onPick,
  className,
}: {
  starters: Starter[];
  onPick: (ask: string) => void;
  className?: string;
}) {
  if (starters.length === 0) return null;
  return (
    <div className={className}>
      {starters.map((s) => (
        <button
          key={s.label}
          onClick={() => onPick(s.ask)}
          className="shrink-0 rounded-full border border-vb-border px-3 py-1.5 text-xs text-vb-text-dim transition-colors hover:border-vb-red hover:text-vb-red"
        >
          {s.label}
        </button>
      ))}
    </div>
  );
}
