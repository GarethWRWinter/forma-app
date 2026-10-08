"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { training } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Kicker } from "@/components/ui/kicker";
import { useSafetyState } from "@/components/safety/useSafetyState";
import {
  REBUILD_BUTTON,
  REBUILT_TEXT,
  rebuildDismissKey,
  rebuildOfferText,
  showRebuildOffer,
} from "@/lib/safetyPrompts";

function longDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "long" });
}

function readDismissed(planId: string): boolean {
  try {
    return window.localStorage.getItem(rebuildDismissKey(planId)) === "1";
  } catch {
    return false;
  }
}

/**
 * "Rebuild my plan at full strength". A plan written while riding was kept
 * easy (an uncleared health answer, an easy-only hold) stays easy until it is
 * rebuilt, so once a clearance or a lifted hold leaves nothing holding the
 * rider back, this offers to write it again for the same goal. It reads the
 * shared safety state, so it appears the moment a lift goes through, wherever
 * it happened, and again on any later visit until the rider answers.
 *
 * `card` sits under the hold card on the dashboard; `strip` runs across the
 * top of the other dashboard pages.
 */
export function RebuildPlanOffer({ variant = "card" }: { variant?: "card" | "strip" }) {
  const { user } = useAuth();
  const pathname = usePathname();
  const queryClient = useQueryClient();
  const { data: state } = useSafetyState(!!user);
  // Same keys as the training page, so the cache is shared.
  const { data: plans } = useQuery({
    queryKey: ["plans"],
    queryFn: () => training.getPlans(),
    enabled: !!user,
  });
  const active = plans?.plans.find((p) => p.status === "active");
  const { data: plan } = useQuery({
    queryKey: ["plan-detail", active?.id],
    queryFn: () => training.getPlan(active!.id),
    enabled: !!active?.id,
  });

  const [dismissed, setDismissed] = useState(true);
  // The "done" line stays on the page it was rebuilt from, and goes after.
  const [donePath, setDonePath] = useState<string | null>(null);
  const done = donePath !== null && donePath === pathname;
  useEffect(() => {
    setDismissed(active?.id ? readDismissed(active.id) : true);
  }, [active?.id]);

  const rebuild = useMutation({
    mutationFn: () =>
      training.generatePlan({
        goal_event_id: active?.goal_event_id ?? undefined,
        periodization_model: active?.periodization_model || undefined,
      }),
    onSuccess: () => {
      setDonePath(pathname);
      for (const key of [
        "plans",
        "plan-detail",
        "workouts-week",
        "today-workouts",
        "today-workout-detail",
        "workout",
        "plan-proposals",
      ]) {
        void queryClient.invalidateQueries({ queryKey: [key] });
      }
    },
  });

  const show = done || showRebuildOffer(state, plan, dismissed);
  if (!show) return null;

  const dismiss = () => {
    if (active?.id) {
      try {
        window.localStorage.setItem(rebuildDismissKey(active.id), "1");
      } catch {
        // Private window or blocked storage: it just asks again next visit.
      }
    }
    setDismissed(true);
  };

  const error =
    rebuild.error instanceof Error
      ? rebuild.error.message
      : rebuild.isError
        ? "That didn't go through. Try again."
        : "";

  const body = done ? (
    <p className="text-sm text-vb-text">
      {REBUILT_TEXT}{" "}
      <Link
        href="/dashboard/training"
        className="underline underline-offset-2 hover:text-vb-red"
      >
        See the plan
      </Link>
    </p>
  ) : (
    <>
      <p
        className={cn(
          "max-w-2xl text-sm leading-relaxed text-vb-text",
          variant === "card" && "text-[15px]"
        )}
      >
        {rebuildOfferText(longDate(state?.layoff_gate_until))}
      </p>
      <div
        className={cn(
          "flex flex-wrap items-center gap-3",
          variant === "card" ? "mt-5" : "mt-3"
        )}
      >
        <Button
          size="sm"
          variant="flamme"
          onClick={() => rebuild.mutate()}
          disabled={rebuild.isPending}
        >
          {rebuild.isPending ? "Rebuilding…" : REBUILD_BUTTON}
        </Button>
        <Button size="sm" variant="quiet" onClick={dismiss} disabled={rebuild.isPending}>
          Not now
        </Button>
      </div>
      {error && (
        <p className="mt-3 border-l-2 border-vb-red pl-3 text-sm text-vb-text">{error}</p>
      )}
    </>
  );

  if (variant === "strip") {
    return (
      <div
        role="status"
        className="border-b border-vb-border-subtle border-l-[3px] border-l-vb-success bg-vb-surface px-4 py-3 sm:px-8"
      >
        {body}
      </div>
    );
  }

  return (
    <section
      role="status"
      className="f-rise border border-vb-border-subtle border-l-[3px] border-l-vb-success bg-vb-surface p-6 md:p-8"
    >
      <Kicker className="mb-2">Your plan</Kicker>
      {body}
    </section>
  );
}
