"use client";

import { useState } from "react";
import Link from "next/link";
import { safety } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Kicker } from "@/components/ui/kicker";
import { ClearanceForm } from "@/components/safety/ClearanceForm";
import {
  FEVER_LIFT_BUTTON,
  holdView,
  liftedMessage,
  mistakeLiftedMessage,
  type FullSafetyState,
  type Hold,
} from "@/components/safety/safety-rules";
import { useApplySafetyState, useSafetyState } from "@/components/safety/useSafetyState";

/**
 * The hold banner: shown while a safety hold is open.
 *
 * `card` sits at the top of the dashboard; `strip` runs across the top of
 * every other dashboard page. The way out follows the hold's lift kind:
 * "I've been cleared" (a doctor's clearance, or for a head injury its own
 * check, which leaves easy riding and no racing before day 21), "My fever has
 * gone" (the rider's own word, which leaves an easy week) and "This was a
 * mistake" (logged, for
 * the chat detector misreading something like "my chest strap died"). Holds
 * from the rider's own health answers can't be called a mistake: those
 * answers are changed in Settings, then Health. An under-18 hold, the easy
 * week after a fever and a hold set by hand offer no way out at all. A fever
 * or head injury mentioned again during its easy days shows as its own hold;
 * calling that a mistake lifts it alone, and the easy days run on.
 */
export function HoldBanner({ variant = "card" }: { variant?: "card" | "strip" }) {
  const { user } = useAuth();
  const { data: state } = useSafetyState(!!user);
  const applyState = useApplySafetyState();
  const [mode, setMode] = useState<"idle" | "clear" | "fever" | "mistake">("idle");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");

  const hold = (state?.hold ?? null) as Hold | null;

  if (!hold) {
    if (!done) return null;
    return (
      <div
        role="status"
        className={cn(
          variant === "strip"
            ? "border-b border-vb-border-subtle bg-vb-surface px-4 py-3 sm:px-8"
            : "f-rise border border-vb-border-subtle border-l-[3px] border-l-vb-success bg-vb-surface p-5"
        )}
      >
        <p className="text-sm text-vb-text">{done}</p>
      </div>
    );
  }

  const view = holdView(
    hold,
    user?.country,
    (state as FullSafetyState | undefined)?.no_racing_or_group_until
  );
  const severe = hold.level === "hold_all";

  const markMistake = async () => {
    setError("");
    setBusy(true);
    try {
      const next = await safety.markMistake(hold.id);
      applyState(next);
      setMode("idle");
      setDone(mistakeLiftedMessage(next));
    } catch (err) {
      setError(err instanceof Error ? err.message : "That didn't go through. Try again.");
    } finally {
      setBusy(false);
    }
  };

  const hasActions = view.canClear || view.canSelfLift || view.canMistake || view.fromAnswers;
  const open = (next: "clear" | "fever" | "mistake") => {
    setDone("");
    setError("");
    setMode(next);
  };

  const actions = mode === "idle" && hasActions && (
    <div
      className={cn(
        "flex flex-wrap items-center",
        variant === "strip" ? "gap-x-5 gap-y-2" : "mt-5 gap-3"
      )}
    >
      {view.canClear &&
        (variant === "strip" ? (
          <button
            type="button"
            onClick={() => open("clear")}
            className="f-kicker text-vb-red transition-colors hover:text-vb-red-dim"
          >
            I&apos;ve been cleared
          </button>
        ) : (
          <Button size="sm" onClick={() => open("clear")}>
            I&apos;ve been cleared
          </Button>
        ))}
      {view.canSelfLift &&
        (variant === "strip" ? (
          <button
            type="button"
            onClick={() => open("fever")}
            className="f-kicker text-vb-red transition-colors hover:text-vb-red-dim"
          >
            {FEVER_LIFT_BUTTON}
          </button>
        ) : (
          <Button size="sm" onClick={() => open("fever")}>
            {FEVER_LIFT_BUTTON}
          </Button>
        ))}
      {view.canMistake && (
        <button
          type="button"
          onClick={() => open("mistake")}
          className="f-kicker text-vb-text-dim transition-colors hover:text-vb-text"
        >
          This was a mistake
        </button>
      )}
      {view.fromAnswers && (
        <Link
          href="/dashboard/settings#health"
          className="f-kicker text-vb-text-dim transition-colors hover:text-vb-text"
        >
          Change my answers
        </Link>
      )}
    </div>
  );

  const panel = (
    <>
      {mode === "clear" && view.clearKind === "head_injury" && (
        <ClearanceForm
          kind="head_injury"
          holdId={hold.id}
          className="mt-4 max-w-xl"
          onCancel={() => setMode("idle")}
          onDone={(next) => {
            setMode("idle");
            setDone(liftedMessage(next));
          }}
        />
      )}
      {mode === "clear" && view.clearKind === "doctor" && (
        <ClearanceForm
          className="mt-4 max-w-xl"
          onCancel={() => setMode("idle")}
          onDone={(next) => {
            setMode("idle");
            setDone(liftedMessage(next));
          }}
        />
      )}
      {mode === "fever" && (
        <ClearanceForm
          kind="fever_self"
          holdId={hold.id}
          className="mt-4 max-w-xl"
          onCancel={() => setMode("idle")}
          onDone={(next) => {
            setMode("idle");
            setDone(liftedMessage(next));
          }}
        />
      )}
      {mode === "mistake" && (
        <div className="mt-4 max-w-xl space-y-3">
          <p className="text-sm leading-relaxed text-vb-text">
            Only lift it if I misread what you told me. If something is wrong,
            keep it and see a doctor.
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <Button size="sm" variant="ghost" onClick={markMistake} disabled={busy}>
              {busy ? "Lifting…" : "Lift the hold"}
            </Button>
            <Button size="sm" variant="quiet" onClick={() => setMode("idle")} disabled={busy}>
              Keep it
            </Button>
          </div>
        </div>
      )}
      {done && mode === "idle" && (
        <p className="mt-3 text-sm text-vb-text-dim">{done}</p>
      )}
      {error && (
        <p className="mt-3 border-l-2 border-vb-red pl-3 text-sm text-vb-text">{error}</p>
      )}
    </>
  );

  if (variant === "strip") {
    return (
      <div
        role="status"
        className={cn(
          "border-b border-vb-border-subtle border-l-[3px] bg-vb-surface px-4 py-3 sm:px-8",
          severe ? "border-l-vb-red" : "border-l-vb-text"
        )}
      >
        <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2">
          <p className="max-w-2xl text-sm text-vb-text">
            <span className="f-kicker mr-2 text-vb-red">{view.title}</span>
            {view.text}
          </p>
          {actions}
        </div>
        {panel}
      </div>
    );
  }

  return (
    <section
      role="status"
      className={cn(
        "f-rise border border-vb-border-subtle border-l-[3px] bg-vb-surface p-6 md:p-8",
        severe ? "border-l-vb-red" : "border-l-vb-text"
      )}
    >
      <Kicker flamme={severe} dot={severe}>
        {view.title}
      </Kicker>
      <p className="mt-2 max-w-xl text-[15px] leading-relaxed text-vb-text">{view.text}</p>
      {actions}
      {panel}
    </section>
  );
}
