"use client";

import { useMemo, useRef, useState, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { goals as goalsApi } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Conversation starters — the coach's range, signposted. Two kinds of
 * context decide what leads: the rider's goal state (a race in a
 * fortnight, a goal with no why, a report pending) and what the current
 * conversation is already about (keyword-scored against recent messages).
 * Tapping never sends; it hands the rider the words and leaves them
 * holding the pen.
 */

export interface Starter {
  label: string;
  ask: string;
  /** conversation keywords that make this door MORE relevant right now */
  keywords: string[];
  /** base priority: goal-state chips outrank the general pool */
  priority?: number;
}

const GENERAL_STARTERS: Starter[] = [
  {
    label: "I keep falling off the plan",
    ask: "I keep falling off the plan after a couple of weeks. Help me work out why it happens and design a week I can actually keep.",
    keywords: ["missed", "skipped", "fell off", "consistency", "lapse", "behind", "guilt"],
  },
  {
    label: "Fix my sleep",
    ask: "My sleep is a mess and I know it's costing me. Coach me through fixing it like it's training, one change at a time.",
    keywords: ["sleep", "tired", "insomnia", "bed", "exhausted", "knackered", "fatigue"],
  },
  {
    label: "Find me more hours",
    ask: "I can't find enough time to train. Audit my week with me and find where the hours are actually going.",
    keywords: ["time", "busy", "work", "hours", "hectic", "no time", "diary", "calendar"],
  },
  {
    label: "My head gets loud when it hurts",
    ask: "My head gets loud in the hard parts of a ride and talks me into backing off. Help me build a script for those moments.",
    keywords: ["head", "mental", "quit", "backing off", "gave up", "cracked", "doubt"],
  },
  {
    label: "Race nerves are getting to me",
    ask: "I get properly nervous before events and it costs me. Teach me how to handle race nerves like the pros do.",
    keywords: ["nervous", "nerves", "anxious", "race day", "start line", "scared", "worry"],
  },
  {
    label: "Why am I always hungry in the evening?",
    ask: "I'm ravenous every evening and I end up raiding the cupboard. Help me work out what my fuelling is actually doing.",
    keywords: ["hungry", "food", "eating", "fuel", "snack", "craving", "diet", "weight"],
  },
  {
    label: "Make my easy rides actually easy",
    ask: "I know my easy rides are too hard. Help me fix it: the zones, the ego, all of it.",
    keywords: ["easy", "zone 2", "z2", "endurance", "too hard", "recovery ride"],
  },
  {
    label: "I feel guilty on rest days",
    ask: "Rest days make me feel guilty and twitchy, like I'm losing fitness. Talk me through why that's wrong and how to sit with it.",
    keywords: ["rest", "guilt", "day off", "recovery week", "losing fitness", "detrain"],
  },
  {
    label: "Design my ideal training week",
    ask: "Walk me through designing my ideal week: sleep, family, work, and the riding that fits around the life I actually have.",
    keywords: ["week", "plan", "schedule", "routine", "structure", "family"],
  },
  {
    label: "Teach me to pace the last hour",
    ask: "I always fade in the last hour of long rides. Teach me how to pace and fuel so I finish strong.",
    keywords: ["fade", "pacing", "last hour", "blow up", "bonk", "died", "long ride"],
  },
  {
    label: "What would you change about my training?",
    ask: "Look at my recent training with fresh eyes. What's the one thing you'd change first, and why?",
    keywords: ["training", "improve", "faster", "ftp", "progress", "plateau", "stuck"],
  },
  {
    label: "How is my fitness trending?",
    ask: "How is my fitness actually trending? Give me the honest picture, not just the numbers.",
    keywords: ["fitness", "form", "ctl", "tsb", "trend", "progress"],
  },
];

function scoreStarter(s: Starter, recentText: string): number {
  let score = s.priority ?? 0;
  for (const k of s.keywords) {
    if (recentText.includes(k)) score += 2;
  }
  return score;
}

export function useCoachStarters(recentText = ""): Starter[] {
  const { data } = useQuery({
    queryKey: ["goals"],
    queryFn: () => goalsApi.list(),
    staleTime: 60_000,
  });

  return useMemo(() => {
    const text = recentText.toLowerCase();
    const starters: Starter[] = [];
    const list = data?.goals ?? [];
    const upcoming = list.filter((g) => g.status === "upcoming");

    const pending = list.find((g) => g.needs_assessment);
    if (pending) {
      starters.push({
        label: `Debrief ${pending.event_name}`,
        ask: `Let's talk about how ${pending.event_name} went, beyond the numbers. Help me see how far I've come and what the pursuit made of me. Be honest with me.`,
        keywords: ["race", "result", "finished", pending.event_name.toLowerCase()],
        priority: 6,
      });
    }

    const raceSoon = upcoming.find(
      (g) => g.days_until != null && g.days_until <= 14
    );
    if (raceSoon) {
      starters.push({
        label: `Get my head ready for ${raceSoon.event_name}`,
        ask: `${raceSoon.event_name} is close and my head is getting louder than my legs. Walk me through getting race ready: the plan, the nerves, the what-ifs.`,
        keywords: ["race", "nervous", "taper", "event", raceSoon.event_name.toLowerCase()],
        priority: 5,
      });
    }

    const noWhy = upcoming.find((g) => !g.why);
    if (noWhy) {
      starters.push({
        label: `Give ${noWhy.event_name} its why`,
        ask: `Let's talk about my goal "${noWhy.event_name}". Ask me one question at a time and help me find why this one matters to me and who it's turning me into. Then save it to the goal.`,
        keywords: ["goal", "why", "motivation", noWhy.event_name.toLowerCase()],
        priority: 4,
      });
    }

    const challengeable = upcoming.find((g) => g.why);
    if (challengeable) {
      starters.push({
        label: `Challenge ${challengeable.event_name}`,
        ask: `Challenge my goal "${challengeable.event_name}". Is it the right size for me? Too safe, too far, or about right? Use my numbers and push back where I need it.`,
        keywords: ["goal", "challenge", "realistic", "ambitious", challengeable.event_name.toLowerCase()],
        priority: 2,
      });
    }

    if (upcoming.length === 0) {
      starters.push({
        label: "Craft my next goal",
        ask: "I want to craft a new goal with you. Ask me one question at a time and help me find the goal I would actually love: what it is, why it matters to me, and who it makes me. When we have it, file it for me.",
        keywords: ["goal", "target", "event", "season", "next"],
        priority: 5,
      });
    }

    starters.push(...GENERAL_STARTERS);
    return starters
      .map((s) => ({ s, score: scoreStarter(s, text) }))
      .sort((a, b) => b.score - a.score)
      .map((x) => x.s);
  }, [data, recentText]);
}

export function StarterChips({
  starters,
  onPick,
  className,
  scrollable = false,
}: {
  starters: Starter[];
  onPick: (ask: string) => void;
  className?: string;
  scrollable?: boolean;
}) {
  const railRef = useRef<HTMLDivElement>(null);
  const [canLeft, setCanLeft] = useState(false);
  const [canRight, setCanRight] = useState(false);

  const updateArrows = () => {
    const el = railRef.current;
    if (!el) return;
    setCanLeft(el.scrollLeft > 4);
    setCanRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 4);
  };

  useEffect(() => {
    updateArrows();
    // Re-check when the starter set changes size.
  }, [starters.length]);

  if (starters.length === 0) return null;

  const chips = starters.map((s) => (
    <button
      key={s.label}
      onClick={() => onPick(s.ask)}
      className="shrink-0 snap-start rounded-full border border-vb-border px-3 py-1.5 text-xs text-vb-text-dim transition-colors hover:border-vb-red hover:text-vb-red"
    >
      {s.label}
    </button>
  ));

  if (!scrollable) {
    return <div className={className}>{chips}</div>;
  }

  const nudge = (dir: 1 | -1) => {
    railRef.current?.scrollBy({ left: dir * 260, behavior: "smooth" });
  };

  return (
    <div className={cn("relative", className)}>
      {canLeft && (
        <button
          aria-label="Scroll starters left"
          onClick={() => nudge(-1)}
          className="absolute -left-1 top-1/2 z-10 -translate-y-1/2 rounded-full border border-vb-border bg-vb-surface p-1 text-vb-text-dim shadow-sm hover:text-vb-text"
        >
          <ChevronLeft className="h-3.5 w-3.5" />
        </button>
      )}
      <div
        ref={railRef}
        onScroll={updateArrows}
        className="scrollbar-none flex snap-x gap-2 overflow-x-auto scroll-smooth px-1"
        style={{
          maskImage:
            "linear-gradient(to right, transparent, black 16px, black calc(100% - 16px), transparent)",
        }}
      >
        {chips}
      </div>
      {canRight && (
        <button
          aria-label="Scroll starters right"
          onClick={() => nudge(1)}
          className="absolute -right-1 top-1/2 z-10 -translate-y-1/2 rounded-full border border-vb-border bg-vb-surface p-1 text-vb-text-dim shadow-sm hover:text-vb-text"
        >
          <ChevronRight className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );
}
