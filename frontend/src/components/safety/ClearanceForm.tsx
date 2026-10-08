"use client";

import { useState } from "react";
import { safety, type SafetyState } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  CLEARANCE_LIMITS_LABEL,
  clearanceCopy,
  FEVER_LIFT_TEXT,
  FEVER_UNTICKED,
} from "@/components/safety/safety-rules";
import { useApplySafetyState } from "@/components/safety/useSafetyState";

interface Shared {
  onDone?: (state: SafetyState) => void;
  onCancel?: () => void;
  className?: string;
}

/**
 * How the rider lifts a hold themselves, in the form its lift kind asks for.
 *
 * "doctor" (the default): the rider declares a doctor (or midwife, or physio)
 * has assessed them. Forma doesn't verify it, and the terms say so. Anything
 * they were told to avoid goes to the coach as a hard limit.
 *
 * "head_injury": the head injury's own check, never "cleared me for hard
 * training": a doctor has seen them since they hit their head, and they've
 * had no symptoms for 24 hours. Only a doctor can make it. The server turns
 * the hold into easy riding until two weeks after the injury, with no racing
 * or group riding before day 21.
 *
 * "fever_self": the rider's own word that the fever has gone. No clinician is
 * named and nothing says "hard training", because neither is claimed. The
 * server turns the hold into an easy week that ends by itself.
 *
 * Used under any yes at onboarding, on the hold banner and in Settings, then
 * Health. The tick-box words are the ones the server records.
 */
export function ClearanceForm(
  props: Shared &
    (
      | { kind?: "doctor" }
      | { kind: "head_injury"; holdId: string }
      | { kind: "fever_self"; holdId: string }
    )
) {
  if (props.kind === "fever_self") {
    return (
      <FeverLiftForm
        holdId={props.holdId}
        onDone={props.onDone}
        onCancel={props.onCancel}
        className={props.className}
      />
    );
  }
  return (
    <DoctorClearanceForm
      headInjuryHoldId={props.kind === "head_injury" ? props.holdId : undefined}
      onDone={props.onDone}
      onCancel={props.onCancel}
      className={props.className}
    />
  );
}

function TickBox({
  checked,
  onChange,
  text,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  text: string;
}) {
  return (
    <label className="flex items-start gap-3 text-sm leading-relaxed text-vb-text">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-1 h-4 w-4 flex-none accent-[var(--color-vb-red)]"
      />
      <span>{text}</span>
    </label>
  );
}

function Actions({
  saving,
  onSubmit,
  onCancel,
  label,
}: {
  saving: boolean;
  onSubmit: () => void;
  onCancel?: () => void;
  label: string;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <Button size="sm" onClick={onSubmit} disabled={saving}>
        {saving ? "Saving…" : label}
      </Button>
      {onCancel && (
        <Button size="sm" variant="quiet" onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
      )}
    </div>
  );
}

function ErrorLine({ error }: { error: string }) {
  if (!error) return null;
  return <p className="border-l-2 border-vb-red pl-3 text-sm text-vb-text">{error}</p>;
}

function FeverLiftForm({ holdId, onDone, onCancel, className }: Shared & { holdId: string }) {
  const applyState = useApplySafetyState();
  const [ticked, setTicked] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const submit = async () => {
    setError("");
    if (!ticked) {
      setError(FEVER_UNTICKED);
      return;
    }
    setSaving(true);
    try {
      const state = await safety.liftFever(holdId);
      applyState(state);
      onDone?.(state);
    } catch (err) {
      setError(err instanceof Error ? err.message : "That didn't save. Try again.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={cn("space-y-4", className)}>
      <TickBox checked={ticked} onChange={setTicked} text={FEVER_LIFT_TEXT} />
      <p className="text-xs text-vb-text-muted">
        Your first week back is easy riding only, and it ends by itself.
      </p>
      <ErrorLine error={error} />
      <Actions saving={saving} onSubmit={submit} onCancel={onCancel} label="Confirm" />
    </div>
  );
}

/** A doctor's clearance, or (with headInjuryHoldId) a head injury's own check. */
function DoctorClearanceForm({
  headInjuryHoldId,
  onDone,
  onCancel,
  className,
}: Shared & { headInjuryHoldId?: string }) {
  const applyState = useApplySafetyState();
  const copy = clearanceCopy(headInjuryHoldId ? "head_injury" : "doctor");
  const [ticked, setTicked] = useState(false);
  const [by, setBy] = useState("");
  const [limits, setLimits] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const submit = async () => {
    setError("");
    if (!ticked) {
      setError(copy.unticked);
      return;
    }
    if (!by) {
      setError(headInjuryHoldId ? "Tell me which doctor checked you." : "Tell me who cleared you.");
      return;
    }
    setSaving(true);
    try {
      const avoid = limits.trim() || undefined;
      const state = headInjuryHoldId
        ? await safety.liftHeadInjury(headInjuryHoldId, by, avoid)
        : await safety.confirmClearance(by, avoid);
      applyState(state);
      onDone?.(state);
    } catch (err) {
      setError(err instanceof Error ? err.message : "That didn't save. Try again.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={cn("space-y-4", className)}>
      <TickBox checked={ticked} onChange={setTicked} text={copy.tick} />

      <div>
        <p className="mb-2 text-sm font-medium text-vb-text">{copy.byQuestion}</p>
        <div className="flex flex-wrap gap-2" role="radiogroup" aria-label={copy.byQuestion}>
          {copy.byOptions.map((option) => (
            <button
              key={option}
              type="button"
              role="radio"
              aria-checked={by === option}
              onClick={() => setBy(option)}
              className={cn(
                "f-press rounded-sm border px-3 py-2 text-xs font-medium transition-colors",
                by === option
                  ? "border-vb-red bg-vb-surface text-vb-text"
                  : "border-vb-border-subtle bg-vb-surface text-vb-text-dim hover:border-vb-border"
              )}
            >
              {option}
            </button>
          ))}
        </div>
      </div>

      <div>
        <label className="mb-1.5 block text-sm font-medium text-vb-text">
          {CLEARANCE_LIMITS_LABEL}{" "}
          <span className="font-normal text-vb-text-muted">(optional)</span>
        </label>
        <textarea
          value={limits}
          onChange={(e) => setLimits(e.target.value)}
          maxLength={2000}
          rows={2}
          placeholder="For example: no efforts above threshold for six weeks"
          className="w-full rounded-sm border border-vb-border bg-vb-surface px-3 py-2.5 text-sm text-vb-text placeholder:text-vb-text-muted focus:border-vb-red focus:outline-none focus:ring-1 focus:ring-vb-red"
        />
        <p className="mt-1.5 text-xs text-vb-text-muted">
          The coach treats anything you write here as a hard limit.
        </p>
      </div>

      {copy.note && <p className="text-xs leading-relaxed text-vb-text-muted">{copy.note}</p>}

      <ErrorLine error={error} />
      <Actions saving={saving} onSubmit={submit} onCancel={onCancel} label={copy.submit} />
    </div>
  );
}
