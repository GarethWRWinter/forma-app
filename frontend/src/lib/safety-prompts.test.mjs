// Run: node --test src/lib/safety-prompts.test.mjs
// The re-screen prompt (review finding 6), the sign-up country list (finding
// 11) and the rebuild offer after a clearance. Node strips the types itself.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  EASY_PLAN_FOCUS,
  REBUILD_BUTTON,
  REBUILT_TEXT,
  RESCREEN_AFTER_DAYS,
  allowedCountriesFrom,
  clearedForFullPlan,
  consentsDue,
  countryBlockFor,
  countryOptions,
  planBuiltEasy,
  rebuildOfferText,
  rescreenReason,
  rescreenText,
  rescreenTitle,
  showRebuildOffer,
  showRescreenPrompt,
} from "./safetyPrompts.ts";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

// === Finding 6: riders with no screening, or one due again, are stopped ===

const now = new Date("2026-10-08T12:00:00Z");
const record = (extra) => ({
  version: "screen-v1",
  answered_version: "screen-v1",
  answered_at: "2026-09-01T09:00:00",
  rescreen_due: false,
  ...extra,
});

test("a beta rider who never answered is stopped at the dashboard", () => {
  const r = record({ answered_at: null, answered_version: null, rescreen_due: true });
  assert.equal(showRescreenPrompt({ record: r, termsCurrent: true, pathname: "/dashboard" }), true);
  assert.equal(rescreenReason(r, now), "never");
  assert.equal(rescreenTitle("never"), "A few health questions first");
});

test("rescreen_due from the server stops every dashboard page, with the reason why", () => {
  const due = (extra) => record({ rescreen_due: true, ...extra });
  for (const path of ["/dashboard", "/dashboard/coach", "/dashboard/settings", "/dashboard/training"]) {
    assert.equal(showRescreenPrompt({ record: due({}), termsCurrent: true, pathname: path }), true, path);
  }
  assert.equal(rescreenReason(due({ answered_version: "screen-v0" }), now), "new_questions");
  assert.equal(rescreenReason(due({ answered_at: "2025-09-30T09:00:00" }), now), "a_year");
  assert.equal(rescreenReason(due({}), now), "since_then");
});

test("answers that are current don't stop anyone", () => {
  const r = record({});
  assert.equal(showRescreenPrompt({ record: r, termsCurrent: true, pathname: "/dashboard" }), false);
  assert.equal(rescreenReason(r, now), null);
});

test("never over the terms modal, never mid-ride, never on a failed read", () => {
  const r = record({ answered_at: null, rescreen_due: true });
  assert.equal(showRescreenPrompt({ record: r, termsCurrent: false, pathname: "/dashboard" }), false);
  assert.equal(
    showRescreenPrompt({ record: r, termsCurrent: true, pathname: "/dashboard/training/abc/session" }),
    false
  );
  assert.equal(showRescreenPrompt({ record: undefined, termsCurrent: true, pathname: "/dashboard" }), false);
});

test("the year matches the server's", () => {
  // The holds code owns the number; the onboarding service passes it on.
  const holds = read("../../../app/services/safety_service.py");
  assert.match(holds, new RegExp(`^RESCREEN_AFTER_DAYS = ${RESCREEN_AFTER_DAYS}\\b`, "m"));
  const server = read("../../../app/services/onboarding_service.py");
  assert.match(server, /^RESCREEN_AFTER_DAYS = ss\.RESCREEN_AFTER_DAYS\b/m);
});

test("rescreen_due has a consumer: the dashboard layout opens the health questions", () => {
  const layout = read("../app/dashboard/layout.tsx");
  const prompt = read("../components/dashboard/rescreen-prompt.tsx");
  assert.ok(layout.includes("<RescreenPrompt />"));
  assert.ok(prompt.includes("onboarding.getScreening()"));
  assert.ok(prompt.includes("showRescreenPrompt("));
  assert.ok(prompt.includes("<HealthQuestions"));
  assert.ok(prompt.includes("onboarding.submitScreening("));
  // Blocking, like the terms modal: no close, only log out.
  assert.ok(prompt.includes('aria-modal="true"'));
  assert.ok(!/onClose|Escape|>Close</.test(prompt));
});

// === The rebuild offer ===

const easyPlan = {
  status: "active",
  phases: [{ focus: EASY_PLAN_FOCUS }, { focus: EASY_PLAN_FOCUS }],
};
const fullPlan = { status: "active", phases: [{ focus: "Aerobic base" }] };
const state = (extra) => ({
  allowed: "all",
  hold: null,
  screening: { tier: "easy_only", version: "screen-v1", clearance_confirmed: true, limits: null },
  layoff_gate_until: null,
  ...extra,
});

test("cleared, with a plan written while riding was kept easy: offer the rebuild", () => {
  assert.equal(planBuiltEasy(easyPlan), true);
  assert.equal(showRebuildOffer(state({}), easyPlan, false), true);
  assert.equal(
    rebuildOfferText(null),
    "Your plan was written while I was keeping your riding easy, and that no longer applies. Rebuild it and the hard sessions come back. The new plan replaces this one."
  );
});

test("only the break left: still offered, and it says when the hard sessions return", () => {
  const s = state({ allowed: "easy", layoff_gate_until: "2026-10-22" });
  assert.equal(clearedForFullPlan(s), true);
  assert.equal(showRebuildOffer(s, easyPlan, false), true);
  assert.ok(rebuildOfferText("22 October").includes("come back from 22 October"));
});

test("not offered while anything still holds the rider back", () => {
  const uncleared = state({
    allowed: "easy",
    screening: { tier: "easy_only", version: "screen-v1", clearance_confirmed: false, limits: null },
  });
  const doctorHold = state({
    allowed: "easy",
    hold: { id: "h", level: "easy_only", reason: "", red_flag: "injury", source: "detector", opened_at: "", lift_kind: "doctor" },
  });
  // The week of easy riding after a fever: a rebuild now would still be easy.
  const feverWeek = state({
    allowed: "easy",
    hold: { id: "h", level: "easy_only", reason: "", red_flag: "fever", source: "detector", opened_at: "", lift_kind: "expires", expires_at: "2026-10-15T09:00:00" },
  });
  for (const s of [uncleared, doctorHold, feverWeek, state({ allowed: "none" }), null]) {
    assert.equal(showRebuildOffer(s, easyPlan, false), false, JSON.stringify(s));
  }
});

test("not offered for a full plan, a finished plan, or once declined", () => {
  assert.equal(showRebuildOffer(state({}), fullPlan, false), false);
  assert.equal(showRebuildOffer(state({}), { ...easyPlan, status: "cancelled" }, false), false);
  assert.equal(showRebuildOffer(state({}), easyPlan, true), false);
  assert.equal(showRebuildOffer(state({}), null, false), false);
});

test("the server's own word on how a plan was built wins over the phases", () => {
  assert.equal(planBuiltEasy({ ...easyPlan, built_level: "all" }), false);
  assert.equal(planBuiltEasy({ ...fullPlan, built_level: "easy" }), true);
});

test("the easy-plan marker matches plan_service", () => {
  const server = read("../../../app/services/plan_service.py");
  assert.ok(server.includes(`EASY_PLAN_FOCUS = "${EASY_PLAN_FOCUS}"`));
});

test("the offer calls the plan generator for the rider's goal, on the dashboard and every other page", () => {
  const offer = read("../components/dashboard/rebuild-plan-offer.tsx");
  assert.ok(/training\.generatePlan\(\{\s*goal_event_id: active\?\.goal_event_id/.test(offer));
  assert.ok(read("../app/dashboard/page.tsx").includes('<RebuildPlanOffer variant="card" />'));
  assert.ok(read("../app/dashboard/layout.tsx").includes('<RebuildPlanOffer variant="strip" />'));
});

// === Finding 11: sign-up offers the server's countries, not a fixed list ===

const fallback = [
  { code: "GB", name: "United Kingdom" },
  { code: "IE", name: "Ireland" },
  { code: "FR", name: "France" },
  { code: "US", name: "United States" },
  { code: "CA", name: "Canada" },
];
const BLOCKED = new Set(["US", "CA"]);

test("the allowlist is read as codes or as code and name", () => {
  assert.deepEqual(allowedCountriesFrom(["gb", " IE ", "UK", "ZZZ", 5, null]), [
    { code: "GB", name: null },
    { code: "IE", name: null },
  ]);
  assert.deepEqual(allowedCountriesFrom([{ code: "fr", name: "France" }, { code: "GB" }]), [
    { code: "FR", name: "France" },
    { code: "GB", name: null },
  ]);
  assert.equal(allowedCountriesFrom(undefined), null);
});

test("the picker lists the server's countries, the UK first, then the blocked ones", () => {
  const allowed = allowedCountriesFrom(["IE", "MT", "GB"]);
  const names = { MT: "Malta" };
  const options = countryOptions(allowed, fallback, BLOCKED, (c) => names[c] ?? null);
  assert.deepEqual(
    options.map((c) => c.code),
    ["GB", "IE", "MT", "US", "CA"]
  );
  assert.equal(options[2].name, "Malta");
  // France is on the form's old list but not the server's, so it's gone.
  assert.ok(!options.some((c) => c.code === "FR"));
});

test("anything off the server's list is refused, with the right sentence", () => {
  const allowed = new Set(["GB", "IE"]);
  assert.equal(countryBlockFor("GB", allowed, BLOCKED), null);
  assert.equal(countryBlockFor("US", allowed, BLOCKED), "us_ca");
  // US territories and made-up codes used to pass on two letters alone.
  assert.equal(countryBlockFor("PR", allowed, BLOCKED), "elsewhere");
  assert.equal(countryBlockFor("ZZ", allowed, BLOCKED), "elsewhere");
  assert.equal(countryBlockFor("elsewhere", allowed, BLOCKED), "elsewhere");
  assert.equal(countryBlockFor("", allowed, BLOCKED), null);
  // The server's list is the rule: if it opens a country, so does the form.
  assert.equal(countryBlockFor("US", new Set(["GB", "US"]), BLOCKED), null);
});

test("with no list from the server, the form falls back and the server decides", () => {
  assert.equal(countryOptions(null, fallback, BLOCKED), fallback);
  assert.equal(countryBlockFor("CA", null, BLOCKED), "us_ca");
  assert.equal(countryBlockFor("FR", null, BLOCKED), null);
});

test("the register page reads allowed_countries from /auth/config", () => {
  const page = read("../app/register/page.tsx");
  assert.ok(page.includes("config?.allowed_countries"));
  assert.ok(!page.includes("COUNTRIES.map("), "the select still maps the fixed list");
  assert.ok(page.includes("countries.map("));
  assert.ok(page.includes("countryBlockFor("));
});

// === Words ===

test("the new copy keeps the house rules", () => {
  const lines = [
    rescreenTitle("never"),
    rescreenTitle("a_year"),
    ...["never", "new_questions", "a_year", "since_then"].map(rescreenText),
    rebuildOfferText(null),
    rebuildOfferText("22 October"),
    REBUILD_BUTTON,
    REBUILT_TEXT,
  ];
  const dash = new RegExp(`[${String.fromCharCode(0x2013, 0x2014)}]`);
  for (const line of lines) {
    assert.ok(!dash.test(line), line);
    assert.ok(!line.includes("!"), line);
    assert.ok(!/kicker|injury-proof|prevents injury|\bsafe\b/i.test(line), line);
  }
  for (const file of [
    "./safetyPrompts.ts",
    "../components/dashboard/rescreen-prompt.tsx",
    "../components/dashboard/rebuild-plan-offer.tsx",
    "../app/register/page.tsx",
  ]) {
    assert.ok(!dash.test(read(file)), `${file} has a dash`);
  }
});

// === Box 2 for the beta riders, before any health question ===

test("a rider who never gave box 2 is asked for it before the health questions", () => {
  assert.deepEqual(consentsDue({ terms_current: false, health_consent_current: false }), {
    terms: true,
    health: true,
  });
  assert.deepEqual(consentsDue({ terms_current: true, health_consent_current: false }), {
    terms: false,
    health: true,
  });
  // An older server that doesn't send the flag asks for nothing new.
  assert.deepEqual(consentsDue({ terms_current: true }), { terms: false, health: false });
  assert.deepEqual(consentsDue(null), { terms: false, health: false });

  const due = record({ answered_at: null, answered_version: null, rescreen_due: true });
  assert.equal(
    showRescreenPrompt({ record: due, termsCurrent: true, healthConsent: false, pathname: "/dashboard" }),
    false
  );
  assert.equal(
    showRescreenPrompt({ record: due, termsCurrent: true, healthConsent: true, pathname: "/dashboard" }),
    true
  );
});

test("the consent modal records box 2 word for word, and the health questions wait for it", () => {
  const modal = read("../components/safety/ReacceptTermsModal.tsx");
  assert.match(modal, /consentsDue\(user\)/);
  assert.match(modal, /auth\.giveHealthConsent\(HEALTH_BOX\.text\)/);
  const prompt = read("../components/dashboard/rescreen-prompt.tsx");
  assert.match(prompt, /healthConsent: user\?\.health_consent_current/);
  assert.match(prompt, /user\.health_consent_current !== false/);
  const api = read("./api.ts");
  assert.match(api, /"\/auth\/health-consent"/);
});

test("Settings shows the adults-only words when the FTP gate is closed", () => {
  const page = read("../app/dashboard/settings/page.tsx");
  assert.match(page, /ftpTestGate === "closed" \? \(\s*MINOR_HOLD_TEXT/);
});

test("onboarding asks for any missing box before its health questions", () => {
  const page = read("../app/onboarding/page.tsx");
  assert.match(page, /<ReacceptTermsModal \/>/);
});

test("the terms and privacy pages show the published text the consent rows are stamped with", () => {
  for (const [path, doc] of [
    ["../app/terms/page.tsx", "terms"],
    ["../app/privacy/page.tsx", "privacy"],
  ]) {
    assert.match(read(path), new RegExp(`<LegalPage doc="${doc}" />`));
  }
  const page = read("../components/legal/legal-page.tsx");
  assert.match(page, /legalDocument\(doc\)/);
  assert.match(read("./api.ts"), /`\/auth\/legal\/\$\{doc\}`/);
});
