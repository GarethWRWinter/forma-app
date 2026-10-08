/**
 * The rules behind three safety prompts outside ride mode: the health
 * questions when a re-screen is due, the plan rebuild once nothing holds a
 * rider back, and the countries the sign-up form offers.
 *
 * No runtime imports: the tests run this file straight under Node.
 * Tests: node --test src/lib/safety-prompts.test.mjs
 */

import type { SafetyState, ScreeningRecord } from "@/lib/api";

// === Health questions due ===

/** Mirrors safety_service.RESCREEN_AFTER_DAYS (onboarding_service passes it on). */
export const RESCREEN_AFTER_DAYS = 365;

/** Why the questions are being asked (again). */
export type RescreenReason = "never" | "new_questions" | "a_year" | "since_then";

type RescreenRecord = Pick<
  ScreeningRecord,
  "rescreen_due" | "answered_at" | "answered_version" | "version"
>;

/** Null when nothing is due. The server decides that it is due
    (rescreen_due); this only picks the sentence that says why. */
export function rescreenReason(
  record: RescreenRecord | null | undefined,
  now: Date = new Date()
): RescreenReason | null {
  if (!record || !record.rescreen_due) return null;
  if (!record.answered_at) return "never";
  if (record.answered_version && record.answered_version !== record.version) {
    return "new_questions";
  }
  // The server sends UTC without a zone; read it as UTC.
  const iso = /[zZ]|[+-]\d\d:?\d\d$/.test(record.answered_at)
    ? record.answered_at
    : `${record.answered_at}Z`;
  const answered = Date.parse(iso);
  if (Number.isFinite(answered) && now.getTime() - answered >= RESCREEN_AFTER_DAYS * 86_400_000) {
    return "a_year";
  }
  // Due, recent and on the current questions: a red flag in chat since.
  return "since_then";
}

export function rescreenTitle(reason: RescreenReason): string {
  return reason === "never" ? "A few health questions first" : "Your health questions again";
}

export function rescreenText(reason: RescreenReason): string {
  switch (reason) {
    case "never":
      return "Before I coach you any further, I need your answers to eight health questions. They decide how hard your plan can go.";
    case "new_questions":
      return "The health questions have changed since you last answered them, so I need your answers again before I coach you any further.";
    case "a_year":
      return "It's been a year since you answered the health questions. Things change, so I need your answers as they are today.";
    case "since_then":
      return "Something you've told me since you last answered the health questions means your answers may be out of date. Answer them as things are today.";
  }
}

/** Ride mode is never interrupted, whatever is due. */
export function isRidePath(pathname: string | null | undefined): boolean {
  return (pathname || "").includes("/session");
}

/**
 * The dashboard stops for the health questions while a re-screen is due:
 * never answered (every beta account), a new question set, a year old, or a
 * red flag since. Never on top of the terms modal (that comes first), never
 * mid-ride, and not while the record is still loading or failed to load: the
 * gate itself is enforced on the server, so a failed read never locks a
 * rider out of their own app.
 */
export function showRescreenPrompt(opts: {
  record: RescreenRecord | null | undefined;
  termsCurrent: boolean | undefined;
  /** Box 2. Without it the health questions can't be asked, and the
      consent modal asks for it first. */
  healthConsent?: boolean | undefined;
  pathname: string | null | undefined;
}): boolean {
  if (opts.termsCurrent === false) return false;
  if (opts.healthConsent === false) return false;
  if (isRidePath(opts.pathname)) return false;
  return !!opts.record?.rescreen_due;
}

// === Rebuild at full strength ===

/** Mirrors plan_service.EASY_PLAN_FOCUS: every phase of a plan written
    while the rider was held to easy riding carries it. */
export const EASY_PLAN_FOCUS =
  "Easy and steady riding only, until a doctor clears you for hard training";

export interface PlanLike {
  status?: string | null;
  /** "easy" when the plan was written under an easy-only gate, if the server
      says so. Otherwise the phases' focus tells. */
  built_level?: string | null;
  phases?: { focus: string | null }[] | null;
}

export function planBuiltEasy(plan: PlanLike | null | undefined): boolean {
  if (!plan) return false;
  if (plan.built_level) return plan.built_level === "easy";
  return (plan.phases ?? []).some((p) => p.focus === EASY_PLAN_FOCUS);
}

/**
 * Nothing holds the rider back but, at most, the layoff gate, which ends by
 * itself (and which a new plan writes in for its first weeks): no open hold,
 * and no health answer still waiting on a doctor.
 */
export function clearedForFullPlan(state: SafetyState | null | undefined): boolean {
  if (!state || state.allowed === "none") return false;
  if (state.hold && state.hold.source !== "layoff") return false;
  const s = state.screening;
  if (s && s.tier !== "none" && !s.clearance_confirmed) return false;
  return state.allowed === "all" || !!state.layoff_gate_until;
}

/** After a clearance (or a fever or other hold lifting), a plan written
    while riding was kept easy would otherwise stay easy for ever. */
export function showRebuildOffer(
  state: SafetyState | null | undefined,
  plan: PlanLike | null | undefined,
  dismissed: boolean
): boolean {
  return (
    !dismissed &&
    plan?.status === "active" &&
    planBuiltEasy(plan) &&
    clearedForFullPlan(state)
  );
}

export const REBUILD_BUTTON = "Rebuild my plan at full strength";

/** `gateOpens` is the long date the layoff gate opens, e.g. "22 October". */
export function rebuildOfferText(gateOpens: string | null): string {
  const lead = "Your plan was written while I was keeping your riding easy, and that no longer applies.";
  const back = gateOpens
    ? ` Rebuild it and the hard sessions come back from ${gateOpens}, once you've eased back in after your break.`
    : " Rebuild it and the hard sessions come back.";
  return `${lead}${back} The new plan replaces this one.`;
}

export const REBUILT_TEXT = "Done. Your new plan is on the Training page.";

/** Per plan, so a new easy plan asks afresh. */
export function rebuildDismissKey(planId: string): string {
  return `forma.rebuild-offer.dismissed.${planId}`;
}

// === Where riders can join from ===

export interface CountryOption {
  code: string;
  name: string;
}

/** The two-letter code, upper case; "UK" is what everyone types for GB. */
function normaliseCode(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  let code = raw.trim().toUpperCase();
  if (code === "UK") code = "GB";
  return /^[A-Z]{2}$/.test(code) ? code : null;
}

/**
 * The allowlist from GET /auth/config, as [{ code, name }]. The server may
 * send codes (["GB", "IE"]) or objects ([{ code: "GB", name: "United
 * Kingdom" }]). Null when it sent no list at all, so the form can fall back
 * to its own (the server checks the country again either way).
 */
export function allowedCountriesFrom(
  raw: unknown
): { code: string; name: string | null }[] | null {
  if (!Array.isArray(raw)) return null;
  const out: { code: string; name: string | null }[] = [];
  const seen = new Set<string>();
  for (const item of raw) {
    const obj = item && typeof item === "object" ? (item as { code?: unknown; name?: unknown }) : null;
    const code = normaliseCode(obj ? obj.code : item);
    if (!code || seen.has(code)) continue;
    seen.add(code);
    const name = obj && typeof obj.name === "string" && obj.name.trim() ? obj.name.trim() : null;
    out.push({ code, name });
  }
  return out;
}

/**
 * What the country picker lists: the server's allowlist (the United Kingdom
 * first, then by name), then the blocked countries, so a rider there is told
 * why rather than left guessing. Without a server list, `fallback`.
 */
export function countryOptions(
  allowed: { code: string; name: string | null }[] | null,
  fallback: CountryOption[],
  blocked: Iterable<string>,
  nameFor: (code: string) => string | null = () => null
): CountryOption[] {
  if (!allowed) return fallback;
  const known = new Map(fallback.map((c) => [c.code, c.name]));
  const name = (code: string, given: string | null) =>
    given || known.get(code) || nameFor(code) || code;
  const open = allowed
    .map((c) => ({ code: c.code, name: name(c.code, c.name) }))
    .sort((a, b) =>
      a.code === "GB" ? -1 : b.code === "GB" ? 1 : a.name.localeCompare(b.name, "en-GB")
    );
  const listed = new Set(open.map((c) => c.code));
  const closed: CountryOption[] = [];
  for (const raw of blocked) {
    const code = normaliseCode(raw);
    if (!code || listed.has(code)) continue;
    listed.add(code);
    closed.push({ code, name: name(code, null) });
  }
  return [...open, ...closed];
}

/** Why a rider can't join from here: "us_ca" and "elsewhere" pick the
    sentence; null means they can. Anything off the server's list is refused.
    With no server list, only the blocked countries and "somewhere else" are
    refused here (the server still checks). */
export type CountryBlock = "us_ca" | "elsewhere" | null;

export function countryBlockFor(
  code: string,
  allowed: ReadonlySet<string> | null,
  blocked: ReadonlySet<string>,
  elsewhere: string = "elsewhere"
): CountryBlock {
  if (!code) return null;
  if (code === elsewhere) return "elsewhere";
  const c = normaliseCode(code);
  if (!c) return "elsewhere";
  // The server's list is the rule; the blocked set only picks the words.
  if (allowed) return allowed.has(c) ? null : blocked.has(c) ? "us_ca" : "elsewhere";
  return blocked.has(c) ? "us_ca" : null;
}

// === The consent modal ===

/**
 * Which boxes the blocking consent modal asks for: box 1 when the terms have
 * changed since the rider agreed (or they never did), box 2 when they never
 * gave consent to use their health details. Every beta account needs both.
 */
export function consentsDue(
  user: { terms_current?: boolean; health_consent_current?: boolean } | null | undefined
): { terms: boolean; health: boolean } {
  return {
    terms: user?.terms_current === false,
    health: user?.health_consent_current === false,
  };
}
