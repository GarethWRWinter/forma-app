"use client";

import { useEffect, useState } from "react";
import { onboarding, type ScreeningResult, type SafetyState } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { Button, Arrow } from "@/components/ui/button";
import { ClearanceForm } from "@/components/safety/ClearanceForm";
import {
  HealthQuestions,
  LongBreakQuestion,
  ScreeningResultNote,
} from "@/components/safety/HealthQuestions";
import {
  clearanceOffer,
  completeAnswers,
  FEVER_LIFT_BUTTON,
  ftpGate,
  isMinorHold,
  liftedMessage,
  limitsOf,
  MINOR_HOLD_TEXT,
  noRacingLine,
  screeningIntro,
  type FullSafetyState,
  type Hold,
  type ScreeningAnswers,
} from "@/components/safety/safety-rules";
import { useApplySafetyState, useSafetyState } from "@/components/safety/useSafetyState";

function longDate(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "long",
  });
}

/** Where the rider stands today, in plain sentences. */
function statusLines(state: FullSafetyState): string[] {
  const lines: string[] = [];
  if (isMinorHold(state.hold)) return [MINOR_HOLD_TEXT];
  if (state.allowed === "all") lines.push("Your plan has the full range of sessions.");
  else if (state.allowed === "easy")
    lines.push(
      "Your plan is easy and steady riding for now: recovery and endurance, with no hard intervals and no FTP test."
    );
  else lines.push("Every session is on hold for now.");

  if (state.layoff_gate_until && state.allowed !== "none") {
    const until = longDate(state.layoff_gate_until);
    lines.push(
      ftpGate(state) === "doctor"
        ? `You've also had a break, so even once you're cleared, hard sessions wait until ${until}.`
        : `You've had a break, so the hard sessions come back on ${until}.`
    );
  }
  const noRacing = noRacingLine(state.no_racing_or_group_until);
  if (noRacing) lines.push(noRacing);
  if (state.screening?.clearance_confirmed) lines.push("You told me a doctor cleared you.");
  const limits = limitsOf(state);
  if (limits.length === 1) {
    lines.push(`I treat what they told you to avoid as a hard rule: ${limits[0]}`);
  } else if (limits.length > 1) {
    lines.push(`I treat each thing you were told to avoid as a hard rule: ${limits.join("; ")}`);
  }
  return lines;
}

/**
 * Settings, then Health: where the rider stands, the clearance flow, and the
 * eight questions again. Answers start blank on purpose: a re-screen asks
 * about today, not about whatever was true at sign-up.
 */
export function HealthSettings() {
  const { user } = useAuth();
  const { data: state, isPending, isError, refetch } = useSafetyState(!!user);
  const applyState = useApplySafetyState();

  const [editing, setEditing] = useState(false);
  const [answers, setAnswers] = useState<ScreeningAnswers>({});
  const [longBreak, setLongBreak] = useState<boolean | undefined>(undefined);
  const [result, setResult] = useState<ScreeningResult | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [clearing, setClearing] = useState(false);
  const [cleared, setCleared] = useState("");

  // Links from the hold banner land here as #health. The section renders
  // before the page's data arrives, so bring it into view on a fresh load.
  useEffect(() => {
    if (window.location.hash === "#health") {
      document.getElementById("health")?.scrollIntoView({ block: "start" });
    }
  }, []);

  const complete = completeAnswers(answers);

  const save = async () => {
    if (!complete || longBreak === undefined) {
      setError("Answer every question with a yes or a no.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const next = await onboarding.submitScreening({ answers: complete, long_break: longBreak });
      applyState(next.safety);
      setResult(next);
      setEditing(false);
      setCleared("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Your answers didn't save. Try again.");
    } finally {
      setSaving(false);
    }
  };

  const startEditing = () => {
    setAnswers({});
    setLongBreak(undefined);
    setResult(null);
    setError("");
    setEditing(true);
  };

  const fullState = (state ?? null) as FullSafetyState | null;
  const hold = (fullState?.hold ?? null) as Hold | null;
  const screening = fullState?.screening ?? null;
  // Which way out to offer: a doctor's clearance, the fever form, or none
  // (an account held as under 18 has nothing to lift here).
  const offer = clearanceOffer(fullState, user?.country);
  // An account held as under 18 is never asked the health questions: the
  // server refuses the answers (403), and age says nothing about health.
  const minor = isMinorHold(hold);

  return (
    <section
      id="health"
      className="scroll-mt-20 rounded-sm border border-vb-border-subtle bg-vb-surface p-6"
    >
      <h2 className="f-display text-2xl text-vb-text">Health</h2>
      <p className="mt-1 text-sm text-vb-text-dim">
        Your answers to the health questions decide how hard your plan can go.
        If anything changes, update them here.
      </p>

      <div className="mt-5 space-y-2 border-l-2 border-vb-border-strong pl-4">
        {isPending ? (
          <p className="text-sm text-vb-text-dim">Checking…</p>
        ) : isError || !state ? (
          <p className="text-sm text-vb-text">
            I couldn&apos;t load your health answers.{" "}
            <button
              type="button"
              onClick={() => refetch()}
              className="underline underline-offset-2 hover:text-vb-red"
            >
              Try again
            </button>
          </p>
        ) : (
          <>
            {!screening && !minor && (
              <p className="text-sm text-vb-text">
                You haven&apos;t answered the health questions yet. There are
                eight, and they take a minute.
              </p>
            )}
            {statusLines(state as FullSafetyState).map((line) => (
              <p key={line} className="text-sm leading-relaxed text-vb-text">
                {line}
              </p>
            ))}
          </>
        )}
      </div>

      {result && !editing && (
        <ScreeningResultNote
          result={result}
          coachName={user?.coach_name || "Forma"}
          className="mt-5"
        />
      )}

      {/* Clearance: under any yes, for any hold a doctor can lift, and the
          rider's own word for a fever. */}
      {state && !editing && (offer || cleared) && (
        <div className="mt-5 rounded-sm border border-vb-border-subtle bg-vb-bg p-4">
          {cleared ? (
            <p className="text-sm text-vb-text">{cleared}</p>
          ) : clearing && offer === "fever_self" && hold ? (
            <ClearanceForm
              kind="fever_self"
              holdId={hold.id}
              onCancel={() => setClearing(false)}
              onDone={(next) => {
                setClearing(false);
                setCleared(liftedMessage(next));
              }}
            />
          ) : clearing && offer === "head_injury" && hold ? (
            <ClearanceForm
              kind="head_injury"
              holdId={hold.id}
              onCancel={() => setClearing(false)}
              onDone={(next) => {
                setClearing(false);
                setCleared(liftedMessage(next));
              }}
            />
          ) : clearing ? (
            <ClearanceForm
              onCancel={() => setClearing(false)}
              onDone={(next) => {
                setClearing(false);
                setCleared(liftedMessage(next));
              }}
            />
          ) : (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="text-sm text-vb-text-dim">
                {offer === "fever_self"
                  ? "Has your fever been gone for 24 hours, without paracetamol or ibuprofen?"
                  : offer === "head_injury"
                    ? "Has a doctor checked you since you hit your head?"
                    : "Has a doctor, midwife or physio cleared you for hard training?"}
              </p>
              <Button size="sm" variant="ghost" onClick={() => setClearing(true)}>
                {offer === "fever_self" ? FEVER_LIFT_BUTTON : <>I&apos;ve been cleared</>}
              </Button>
            </div>
          )}
        </div>
      )}

      {minor ? null : editing ? (
        <div className="mt-6">
          <p className="text-sm text-vb-text-dim">{screeningIntro(user?.country)}</p>
          <p className="mt-1 text-sm text-vb-text-dim">Answer them as things are today.</p>
          <HealthQuestions
            answers={answers}
            onAnswer={(id, v) => setAnswers((prev) => ({ ...prev, [id]: v }))}
            className="mt-2"
          />
          <div className="mt-2 border-t border-vb-border-subtle">
            <LongBreakQuestion value={longBreak} onChange={setLongBreak} />
          </div>
          {error && (
            <p className="mt-3 border-l-2 border-vb-red pl-3 text-sm text-vb-text">{error}</p>
          )}
          <div className="mt-5 flex flex-wrap items-center gap-3">
            <Button
              size="sm"
              onClick={save}
              disabled={saving || !complete || longBreak === undefined}
            >
              {saving ? "Saving…" : <>Save my answers <Arrow /></>}
            </Button>
            <Button size="sm" variant="quiet" onClick={() => setEditing(false)} disabled={saving}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <div className="mt-5">
          <Button size="sm" variant="ghost" onClick={startEditing}>
            {screening ? "Answer the questions again" : "Answer the health questions"}
          </Button>
        </div>
      )}
    </section>
  );
}
