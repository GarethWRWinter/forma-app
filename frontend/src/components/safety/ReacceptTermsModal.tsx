"use client";

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { auth } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { Button, Arrow } from "@/components/ui/button";
import { Kicker } from "@/components/ui/kicker";
import { ConsentBox } from "@/components/safety/ConsentBox";
import {
  ProfileConsentFields,
  useProfileConsent,
} from "@/components/account/profile-consent-fields";
import {
  HEALTH_BOX,
  HEALTH_CONSENT_INTRO,
  HEALTH_CONSENT_TITLE,
  HEALTH_UNTICKED,
  REACCEPT_CHANGES,
  REACCEPT_TITLE,
  REACCEPT_UNTICKED,
  TERMS_BOX,
  TERMS_URL,
} from "@/components/safety/safety-rules";
import { consentsDue } from "@/lib/safetyPrompts";
import { AccountExits } from "@/components/account/account-exits";
import {
  ADULTS_ONLY_KICKER,
  ADULTS_ONLY_TITLE,
  adultsOnlyLines,
  adultsOnlyRefusal,
  isMinorHeld,
  serverSentence,
} from "@/lib/accountExits";
import { SAFETY_STATE_KEY, useSafetyState } from "@/components/safety/useSafetyState";

/** A lost connection or a server fault, under the box on screen. */
const SAVE_FAILED = "That didn't save. Try again in a minute.";

/**
 * Blocks the app when the rider last agreed to an older version of the
 * terms, or never agreed at all (every beta account, the first time it
 * opens after this ships), or never gave box 2, the consent to use their
 * health details, or never gave a date of birth and country (the beta
 * riders, needs_profile_consent). It says what changed, then asks for
 * whichever are missing, and the exact words of each box are what the server
 * records. The date of birth and country go with the terms box, and the
 * server refuses an under-18 or a country Forma doesn't serve, shown under
 * that field. The health questions wait behind it.
 *
 * There is no close button on purpose, but it is never a trap: Manage
 * billing (to cancel), Download my data, Delete my account and Log out sit
 * under the form, because Settings, where they otherwise live, is behind it
 * (re-verification round 3, problem 7). Picking somewhere Forma isn't open
 * says so under the country and turns the rider to those instead.
 *
 * An account held as under 18, or a re-acceptance the server refuses for
 * being under 18, gets the adults-only message and the same ways out, and
 * the form stops there: no second try with another date (problem 4).
 */
export function ReacceptTermsModal() {
  const { user, refreshUser } = useAuth();
  const queryClient = useQueryClient();
  const [ticked, setTicked] = useState(false);
  const [healthTicked, setHealthTicked] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [healthError, setHealthError] = useState("");
  // The server's sentence when it refused the rider for being under 18.
  const [adultsOnly, setAdultsOnly] = useState<string | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);

  const due = consentsDue(user);
  const profile = useProfileConsent(user);
  // The date of birth and country travel with the terms, so the terms box
  // shows whenever they are asked for.
  const termsDue = due.terms || profile.due;
  const open = termsDue || due.health;
  // The hold banner's own cache entry: an account already held as under 18
  // is never asked for a date of birth, the terms or its health details.
  const { data: safetyState } = useSafetyState(open);
  const held = adultsOnly !== null || isMinorHeld(safetyState);
  const countryClosed = !held && termsDue && profile.countryClosed !== null;

  useEffect(() => {
    if (open) headingRef.current?.focus();
  }, [open, held]);

  if (!open) return null;

  const agree = async () => {
    setError("");
    setHealthError("");
    const termsMissing = termsDue && !ticked;
    const healthMissing = due.health && !healthTicked;
    const profileReady = profile.check();
    if (termsMissing) setError(REACCEPT_UNTICKED);
    if (healthMissing) setHealthError(HEALTH_UNTICKED);
    if (termsMissing || healthMissing || !profileReady) return;
    setSaving(true);
    let termsSaved = false;
    try {
      if (termsDue) {
        await auth.reacceptTerms(TERMS_BOX.text, profile.payload);
        termsSaved = true;
      }
      if (due.health) await auth.giveHealthConsent(HEALTH_BOX.text);
      await refreshUser();
    } catch (err) {
      // Under 18, or already held as under 18: the adults-only message, and
      // the form stops there. The safety state is read again, so the hold
      // the server opens shows here and on the banner.
      const refusal = adultsOnlyRefusal(err);
      if (refusal) {
        setAdultsOnly(refusal);
        queryClient.invalidateQueries({ queryKey: SAFETY_STATE_KEY });
        return;
      }
      // The server's sentence for a refusal it explains; a lost connection
      // or a server fault gets a plain line, never "Failed to fetch".
      const message = serverSentence(err, SAVE_FAILED);
      // A date the server can't read, or a country Forma doesn't serve: under that field.
      if (!termsSaved && profile.showServerError(message)) return;
      // Otherwise under the box whose call failed: box 2 once the terms
      // have saved, or when they were already current.
      if (termsDue && !termsSaved) setError(message);
      else setHealthError(message);
    } finally {
      setSaving(false);
    }
  };

  if (held) {
    return (
      <ModalShell>
        <AdultsOnly
          message={adultsOnly}
          minorHeld={isMinorHeld(safetyState)}
          headingRef={headingRef}
        />
      </ModalShell>
    );
  }

  return (
    <ModalShell>
      <Kicker flamme className="mb-2">
        {termsDue ? "Updated terms" : "Your health details"}
      </Kicker>
      <h2
        id="reaccept-title"
        ref={headingRef}
        tabIndex={-1}
        className="f-display text-3xl leading-tight text-vb-text focus:outline-none"
      >
        {termsDue ? REACCEPT_TITLE : HEALTH_CONSENT_TITLE}
      </h2>
      {termsDue && (
        <>
          <p className="mt-4 text-sm text-vb-text-dim">Here&apos;s what changed:</p>
          <ul className="mt-3 space-y-2.5">
            {REACCEPT_CHANGES.map((line) => (
              <li key={line} className="flex gap-3 text-sm leading-relaxed text-vb-text">
                <span aria-hidden="true" className="mt-2 h-1.5 w-1.5 shrink-0 bg-vb-red" />
                <span>{line}</span>
              </li>
            ))}
          </ul>
          <a
            href={TERMS_URL}
            target="_blank"
            rel="noreferrer"
            className="f-kicker mt-4 inline-block text-vb-text-dim underline-offset-2 hover:text-vb-red"
          >
            Read the full terms →
          </a>

          {profile.due && (
            <ProfileConsentFields
              {...profile.fieldProps}
              disabled={saving}
              className="mt-6 border-t border-vb-border-subtle pt-5"
            />
          )}

          {/* Nothing to agree to from somewhere Forma isn't open: the
              sentence under the country points to the ways out below. */}
          {!countryClosed && (
            <ConsentBox
              className={
                profile.due ? "mt-5" : "mt-6 border-t border-vb-border-subtle pt-5"
              }
              label={TERMS_BOX}
              checked={ticked}
              onChange={(v) => {
                setTicked(v);
                if (v) setError("");
              }}
              error={error}
            />
          )}
        </>
      )}

      {due.health && !countryClosed && (
        <>
          <p className="mt-4 text-sm leading-relaxed text-vb-text-dim">
            {HEALTH_CONSENT_INTRO}
          </p>
          <ConsentBox
            className={
              termsDue ? "mt-4" : "mt-6 border-t border-vb-border-subtle pt-5"
            }
            label={HEALTH_BOX}
            checked={healthTicked}
            onChange={(v) => {
              setHealthTicked(v);
              if (v) setHealthError("");
            }}
            error={healthError}
          />
        </>
      )}

      {!countryClosed && (
        <div className="mt-6 flex justify-end">
          <Button variant="flamme" onClick={agree} disabled={saving}>
            {saving ? "Saving…" : <>Agree and carry on <Arrow /></>}
          </Button>
        </div>
      )}

      <AccountExits
        mode={countryClosed ? "closed" : "agree"}
        minorHeld={false}
        disabled={saving}
        className="mt-6 border-t border-vb-border-subtle pt-5"
      />
    </ModalShell>
  );
}

/** The blocking card: no close button, scrolls on a short phone. */
function ModalShell({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="fixed inset-0 z-[70] overflow-y-auto bg-vb-text/40 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="reaccept-title"
    >
      {/* min-h-full centring, not flex on the scroller: a phone shorter than
          the card can still scroll to its top. */}
      <div className="flex min-h-full items-center justify-center px-4 py-8">
        <div className="f-rise w-full max-w-lg rounded-sm border border-vb-border-strong bg-vb-surface p-6 sm:p-8">
          {children}
        </div>
      </div>
    </div>
  );
}

/** Forma is for adults: the server's refusal, or the held account's line,
    the way back for an adult it misread, and the ways out. Nothing to fill
    in and nothing to agree to. */
function AdultsOnly({
  message,
  minorHeld,
  headingRef,
}: {
  message: string | null;
  /** The hold is open: the portal refuses the account, so it isn't offered. */
  minorHeld: boolean;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  return (
    <>
      <Kicker flamme className="mb-2">
        {ADULTS_ONLY_KICKER}
      </Kicker>
      <h2
        id="reaccept-title"
        ref={headingRef}
        tabIndex={-1}
        className="f-display text-3xl leading-tight text-vb-text focus:outline-none"
      >
        {ADULTS_ONLY_TITLE}
      </h2>
      <div role="alert">
        {adultsOnlyLines(message).map((line) => (
          <p key={line} className="mt-4 text-sm leading-relaxed text-vb-text">
            {line}
          </p>
        ))}
      </div>
      <AccountExits
        mode="held"
        minorHeld={minorHeld}
        className="mt-6 border-t border-vb-border-subtle pt-5"
      />
    </>
  );
}
