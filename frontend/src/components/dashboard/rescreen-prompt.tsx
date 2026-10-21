"use client";

import { useEffect, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { onboarding, type ScreeningRecord, type ScreeningResult } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { Button, Arrow } from "@/components/ui/button";
import { Kicker } from "@/components/ui/kicker";
import { ClearanceForm } from "@/components/safety/ClearanceForm";
import {
  HealthQuestions,
  LongBreakQuestion,
  ScreeningResultNote,
} from "@/components/safety/HealthQuestions";
import {
  completeAnswers,
  liftedMessage,
  screeningIntro,
  type ScreeningAnswers,
} from "@/components/safety/safety-rules";
import { useApplySafetyState } from "@/components/safety/useSafetyState";
import { AccountExits } from "@/components/account/account-exits";
import {
  rescreenReason,
  rescreenText,
  rescreenTitle,
  showRescreenPrompt,
} from "@/lib/safetyPrompts";

export const SCREENING_RECORD_KEY = ["screening-record"] as const;

/**
 * Blocks the dashboard while the health questions are due: a rider who has
 * never answered them (every beta account, at its next login), a new
 * question set, answers a year old, or a red flag in chat since. Like the
 * terms modal, there is no close button. A rider who won't answer can still
 * manage billing, download their data, delete their account or log out
 * (AccountExits), because the modal covers Settings. It waits for the terms
 * modal, and never opens over ride mode. Never shown to an account held as
 * under 18: the server never marks the questions due for one.
 *
 * Read once per visit: a red flag raised in chat doesn't throw this over the
 * conversation that raised it. It asks at the next visit.
 */
export function RescreenPrompt() {
  const { user } = useAuth();
  const pathname = usePathname();
  const queryClient = useQueryClient();
  const applySafetyState = useApplySafetyState();

  const { data: record } = useQuery({
    queryKey: SCREENING_RECORD_KEY,
    queryFn: () => onboarding.getScreening(),
    enabled: !!user && user.terms_current !== false && user.health_consent_current !== false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    retry: 1,
  });

  const [answers, setAnswers] = useState<ScreeningAnswers>({});
  const [longBreak, setLongBreak] = useState<boolean | undefined>(undefined);
  const [result, setResult] = useState<ScreeningResult | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [clearing, setClearing] = useState(false);
  const [cleared, setCleared] = useState("");
  const headingRef = useRef<HTMLHeadingElement>(null);

  const due = showRescreenPrompt({
    record,
    termsCurrent: user?.terms_current,
    healthConsent: user?.health_consent_current,
    pathname,
  });
  // Once answered, the result stays up until the rider has read it.
  const open = due || result !== null;
  const reason = rescreenReason(record) ?? "never";

  useEffect(() => {
    if (open) headingRef.current?.focus();
  }, [open]);

  if (!open) return null;

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
      applySafetyState(next.safety);
      setResult(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Your answers didn't save. Try again.");
    } finally {
      setSaving(false);
    }
  };

  const carryOn = () => {
    // Answered: closed now, and the server's word on it fetched behind.
    queryClient.setQueryData<ScreeningRecord>(SCREENING_RECORD_KEY, (r) =>
      r ? { ...r, rescreen_due: false, tier: result?.tier ?? r.tier } : r
    );
    setResult(null);
    setAnswers({});
    setLongBreak(undefined);
    setClearing(false);
    setCleared("");
    void queryClient.invalidateQueries({ queryKey: SCREENING_RECORD_KEY });
  };

  return (
    <div
      className="fixed inset-0 z-[69] overflow-y-auto bg-vb-text/40 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="rescreen-title"
    >
      {/* min-h-full centring, not flex on the scroller: a phone shorter than
          the card can still scroll to its top. */}
      <div className="flex min-h-full items-center justify-center px-4 py-8">
        <div className="f-rise w-full max-w-2xl rounded-sm border border-vb-border-strong bg-vb-surface p-6 sm:p-8">
          <Kicker flamme className="mb-2">
            Your health
          </Kicker>
          <h2
            id="rescreen-title"
            ref={headingRef}
            tabIndex={-1}
            className="f-display text-3xl leading-tight text-vb-text focus:outline-none"
          >
            {rescreenTitle(reason)}
          </h2>

          {result ? (
            <>
              <ScreeningResultNote
                result={result}
                coachName={user?.coach_name || "Forma"}
                className="mt-6"
              />
              {/* Under any yes: a rider already cleared can say so now. */}
              {result.tier !== "none" && (
                <div className="mt-4 rounded-sm border border-vb-border-subtle bg-vb-bg p-4">
                  {cleared ? (
                    <p className="text-sm text-vb-text">{cleared}</p>
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
                        Already seen a doctor about this?
                      </p>
                      <Button size="sm" variant="ghost" onClick={() => setClearing(true)}>
                        I&apos;ve been cleared
                      </Button>
                    </div>
                  )}
                </div>
              )}
              <div className="mt-6 flex justify-end">
                <Button variant="flamme" onClick={carryOn} disabled={clearing}>
                  Carry on <Arrow />
                </Button>
              </div>
            </>
          ) : (
            <>
              <p className="mt-4 text-sm leading-relaxed text-vb-text">{rescreenText(reason)}</p>
              <p className="mt-3 text-sm leading-relaxed text-vb-text-dim">
                {screeningIntro(user?.country)}
              </p>
              <HealthQuestions
                answers={answers}
                onAnswer={(id, v) => setAnswers((prev) => ({ ...prev, [id]: v }))}
                className="mt-4"
              />
              <div className="border-t border-vb-border-subtle">
                <LongBreakQuestion value={longBreak} onChange={setLongBreak} />
              </div>
              {error && (
                <p className="mt-3 border-l-2 border-vb-red pl-3 text-sm text-vb-text">{error}</p>
              )}
              <div className="mt-6 flex flex-wrap items-center justify-end gap-3">
                <Button
                  variant="flamme"
                  onClick={save}
                  disabled={saving || !complete || longBreak === undefined}
                >
                  {saving ? "Saving…" : <>Save my answers <Arrow /></>}
                </Button>
              </div>
              <AccountExits
                mode="answer"
                minorHeld={false}
                disabled={saving}
                className="mt-6 border-t border-vb-border-subtle pt-5"
              />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
