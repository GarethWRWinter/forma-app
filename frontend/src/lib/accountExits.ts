/**
 * The ways out of the blocking consent modal, and where it stops.
 *
 * The modal covers every dashboard page until the rider agrees, and Manage
 * billing, the data download and account deletion all live in Settings,
 * behind it. A beta rider outside the UK, or anyone who won't agree, could
 * only log out, while Stripe went on renewing (re-verification round 3,
 * problem 7). The modal now carries all four itself.
 *
 * A re-acceptance refused because the rider is under 18 ends the form
 * there: the adults-only message and the same ways out, never a second try
 * with another date (problem 4).
 *
 * No runtime imports: the tests run this file straight under Node.
 * Tests: node --test src/lib/account-exits.test.mjs
 */

import type { BillingStatus, SafetyState } from "@/lib/api";

export const FORMA_EMAIL = "gareth@ridewithforma.com";

// === Held as under 18 ===

/** The account has an open under-18 hold. safety_service.safety_state puts
    that hold first whenever there is one. */
export function isMinorHeld(state: Pick<SafetyState, "hold"> | null | undefined): boolean {
  return state?.hold?.red_flag === "minor";
}

/**
 * The server's own sentence when POST /auth/reaccept-terms (or box 2) turned
 * the rider away for being under 18, or because the account is already held
 * as under 18. Null for every other refusal, which the form shows under its
 * field and lets the rider put right: a date left out or mistyped, a
 * country, a lost connection.
 */
export function adultsOnlyRefusal(err: unknown): string | null {
  const e = (err && typeof err === "object" ? err : {}) as { message?: unknown; status?: unknown };
  const text = typeof e.message === "string" ? e.message.trim() : "";
  if (!text) return null;
  // FastAPI's own field errors ("date_of_birth: Input should be a valid
  // date"), and auth.NO_DATE_OF_BIRTH, which mentions adults but only means
  // the date is missing.
  if (/^(date_of_birth|country)\b/i.test(text)) return null;
  if (/^add your date of birth/i.test(text)) return null;
  if (/\b(18 or over|18 and over|under[ -]18|adults?)\b/i.test(text)) return text;
  // An account held as under 18 is refused with 403 and an "on hold" line.
  if (e.status === 403 && /\bon hold\b/i.test(text)) return text;
  return null;
}

export const ADULTS_ONLY_KICKER = "18 and over";
/** Not "Forma is for adults": the server's refusal (auth.MINOR_ACCOUNT)
    opens with that, and the heading shouldn't say it twice. */
export const ADULTS_ONLY_TITLE = "We have to stop here.";
/** coach_service._ADULTS_ONLY, word for word: why. */
export const ADULTS_ONLY_REASON =
  "Forma is for adults, 18 and over, so I can't coach you or build you a plan.";
/** coach_service.MINOR_CLOSING_FACT, word for word: what every reply to an
    account held as under 18 says happens next. */
export const ADULTS_ONLY_HELD =
  "This account is on hold and will be closed, and anything you've paid will be refunded.";
/** coach_service.MINOR_CLOSING_MISTAKE, word for word: the way back for an
    adult the check misread. */
export const ADULTS_ONLY_MISTAKE =
  "If you're 18 or over and this was a mistake, email gareth@ridewithforma.com and I'll sort it out.";

/** What the adults-only panel says: the server's refusal when there is one,
    else why and what happens to a held account, then the way back for an
    adult, unless the server's sentence already gave the address. */
export function adultsOnlyLines(serverMessage: string | null | undefined): string[] {
  const first = (serverMessage || "").trim() || `${ADULTS_ONLY_REASON} ${ADULTS_ONLY_HELD}`;
  return first.includes(FORMA_EMAIL) ? [first] : [first, ADULTS_ONLY_MISTAKE];
}

// === Membership ===

const MEMBER_STATUSES = new Set(["active", "trialing", "past_due"]);

/**
 * Whether the modal offers Manage billing, Stripe's portal, where the rider
 * can cancel. Yes for a membership Stripe could still renew, and yes when the
 * status didn't load: a button that explains itself beats a rider with no way
 * to cancel. Not while the status is still loading, not when nobody pays, and
 * never for an account held as under 18, which the portal refuses
 * (billing.MINOR_BILLING_REFUSAL): Forma ends that membership itself.
 */
export function offerBilling(
  status: Pick<BillingStatus, "configured" | "status"> | null | undefined,
  opts: { loadFailed: boolean; minorHeld: boolean }
): boolean {
  if (opts.minorHeld) return false;
  if (!status) return opts.loadFailed;
  return status.configured && MEMBER_STATUSES.has(status.status);
}

// === What the ways out say ===

/** Why the ways out are showing: the rider hasn't agreed yet, has picked
    somewhere Forma isn't open, or is held as under 18. */
export type ExitsMode = "agree" | "answer" | "closed" | "held";

export const EXITS_KICKER = "Your account";

/** The sentence above the buttons. */
export function exitsLead(mode: ExitsMode, billing: boolean): string {
  const things = billing
    ? "cancel your membership in Manage billing, download your data or delete your account"
    : "download your data or delete your account";
  switch (mode) {
    case "closed":
      return `Until Forma opens where you live, you can still ${things} here.`;
    case "held":
      return `You can still ${things} here.`;
    case "agree":
      return `If you'd rather not agree, you can still ${things} here.`;
    case "answer":
      return `If you'd rather not answer, you can still ${things} here.`;
  }
}

export const BILLING_BUTTON = "Manage billing";
export const BILLING_OPENING = "Opening Stripe";
export const BILLING_FAILED = "Stripe didn't answer. Try again in a minute.";

// The download and the deletion say what Settings says, word for word.
export const EXPORT_BUTTON = "Download my data";
export const EXPORT_WORKING = "Gathering…";
export const EXPORT_DONE = "In your downloads";
export const EXPORT_FAILED = "The file didn't come through. Give it a minute and ask again.";

export const DELETE_BUTTON = "Delete my account";
export const DELETE_EXPLAINER =
  "Your access ends the moment you confirm, and your rides, goals, plans and everything Forma remembers about you are deleted for good 30 days later. It can't be undone, so take the download above first if you want a copy. If you're a member, your membership ends at the same moment and no further payments are taken.";
export const DELETE_CONFIRM_KICKER = "Confirm";
export const DELETE_EMAIL_LABEL = "Your email address";
export const DELETE_GO = "Delete it";
export const DELETE_WORKING = "Deleting…";
export const DELETE_CANCEL = "Cancel";
export const DELETE_FAILED =
  "That didn't go through, and nothing has changed. Try again in a minute.";

export const LOGOUT_BUTTON = "Log out";

/**
 * What to show when a call from the modal fails: the server's own sentence
 * when it refused for a reason it explains (a 4xx, such as "I couldn't end
 * your membership just now, so nothing has been deleted"), else `fallback`.
 * Never the browser's "Failed to fetch" for a lost connection, or a bare
 * "Internal Server Error".
 */
export function serverSentence(err: unknown, fallback: string): string {
  const e = (err && typeof err === "object" ? err : {}) as { message?: unknown; status?: unknown };
  const text = typeof e.message === "string" ? e.message.trim() : "";
  const status = typeof e.status === "number" ? e.status : 0;
  return text && status >= 400 && status < 500 ? text : fallback;
}

/** Only the rider's own address arms the delete button, so a stray tap
    can't end an account. */
export function deleteArmed(typed: string, email: string | null | undefined): boolean {
  return !!email && typed.trim().toLowerCase() === email.trim().toLowerCase();
}

/** The download's file name, as Settings names it. */
export function exportFilename(now: Date = new Date()): string {
  return `forma-export-${now.toISOString().slice(0, 10)}.json`;
}

// === Somewhere Forma isn't open ===

/** Under the country field when the rider picks somewhere off the list. It
    says why, then points to the ways out rather than leaving a dead end. */
export function countryClosedText(kind: "us_ca" | "elsewhere"): string {
  const where = kind === "us_ca" ? "the US or Canada" : "your country";
  return `Forma isn't available in ${where} yet, so this account can't carry on for now. What you can do instead is below.`;
}
