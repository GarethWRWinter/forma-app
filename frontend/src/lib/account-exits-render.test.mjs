// Run: node --test src/lib/account-exits-render.test.mjs
// Re-verification round 3, problems 7 and 4, rendered rather than read.
// account-exits.test.mjs checks the words and the wiring in the source; this
// renders the real ReacceptTermsModal to HTML (react-dom/server) in each
// state a rider can open it in, and checks what they would see: the form
// with all four ways out, no Manage billing when nobody pays, the adults-only
// panel with no form for an account held as under 18, and somewhere Forma
// isn't open turning the form to the ways out.
//
// The TSX is compiled with the SWC that ships with Next, through a loader
// registered below, so nothing new is installed. The auth context is
// stubbed, and the react-query cache is seeded with what the server would
// answer. Server rendering can't click, so the country the rider picked is
// forced through a thin wrapper around useProfileConsent.
import { test } from "node:test";
import assert from "node:assert/strict";
import { register } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const FRONTEND = path.dirname(SRC);

const loader = `
import { createRequire } from "node:module";
import { existsSync, readFileSync, statSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const SRC = ${JSON.stringify(SRC)};
const PKG = pathToFileURL(${JSON.stringify(path.join(FRONTEND, "package.json"))}).href;
const swc = createRequire(PKG)("next/dist/build/swc/index.js");
let ready = null;

const AUTH_STUB = "data:text/javascript," + encodeURIComponent(
  "export function useAuth() { return globalThis.__FORMA_TEST_AUTH__; }"
);
const PROFILE_WRAP = "data:text/javascript," + encodeURIComponent(\`
  import * as real from "@/components/account/profile-consent-fields";
  import { countryClosedText } from "@/lib/accountExits";
  export const ProfileConsentFields = real.ProfileConsentFields;
  export function useProfileConsent(user) {
    const out = real.useProfileConsent(user);
    const kind = globalThis.__FORMA_TEST_COUNTRY_CLOSED__;
    if (!kind || !out.due) return out;
    return {
      ...out,
      countryClosed: kind,
      fieldProps: {
        ...out.fieldProps,
        value: { ...out.fieldProps.value, country: kind === "us_ca" ? "US" : "elsewhere" },
        problems: { country: countryClosedText(kind) },
      },
    };
  }
\`);

function file(base) {
  for (const f of [base, base + ".ts", base + ".tsx", path.join(base, "index.ts"), path.join(base, "index.tsx")]) {
    if (existsSync(f) && statSync(f).isFile()) return pathToFileURL(f).href;
  }
  return null;
}

export async function resolve(specifier, context, next) {
  const parent = context.parentURL || "";
  if (specifier === "@/lib/auth-context") return { url: AUTH_STUB, shortCircuit: true };
  if (specifier === "@/components/account/profile-consent-fields" && parent.includes("ReacceptTermsModal")) {
    return { url: PROFILE_WRAP, shortCircuit: true };
  }
  if (specifier.startsWith("@/")) {
    const url = file(path.join(SRC, specifier.slice(2)));
    if (url) return { url, shortCircuit: true };
  }
  if (/^\\.\\.?\\//.test(specifier) && parent.startsWith("file:") && fileURLToPath(parent).startsWith(SRC)) {
    const url = file(path.resolve(path.dirname(fileURLToPath(parent)), specifier));
    if (url) return { url, shortCircuit: true };
  }
  if (parent.startsWith("data:")) return next(specifier, { ...context, parentURL: PKG });
  try {
    return await next(specifier, context);
  } catch {
    return next(specifier, { ...context, parentURL: PKG });
  }
}

export async function load(url, context, next) {
  if (url.startsWith("file:") && /\\.tsx?$/.test(url) && fileURLToPath(url).startsWith(SRC)) {
    ready ??= swc.loadBindings();
    await ready;
    const filename = fileURLToPath(url);
    const out = await swc.transform(readFileSync(filename, "utf8"), {
      filename,
      jsc: {
        parser: { syntax: "typescript", tsx: filename.endsWith(".tsx") },
        transform: { react: { runtime: "automatic" } },
        target: "es2022",
      },
      module: { type: "es6" },
      sourceMaps: false,
    });
    return { format: "module", source: out.code, shortCircuit: true };
  }
  return next(url, context);
}
`;
register("data:text/javascript," + encodeURIComponent(loader));

const { createElement: h } = await import("react");
const { renderToString } = await import("react-dom/server");
const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { ReacceptTermsModal } = await import("@/components/safety/ReacceptTermsModal");
const exits = await import("@/lib/accountExits");

function render({ user, billing, safety, countryClosed }) {
  globalThis.__FORMA_TEST_AUTH__ = { user, refreshUser: async () => {}, logout: () => {} };
  globalThis.__FORMA_TEST_COUNTRY_CLOSED__ = countryClosed;
  const client = new QueryClient();
  if (billing !== undefined) client.setQueryData(["billing-status"], billing);
  if (safety !== undefined) client.setQueryData(["safety-state"], safety);
  client.setQueryData(["auth-config"], { allowed_countries: ["GB"] });
  try {
    return renderToString(h(QueryClientProvider, { client }, h(ReacceptTermsModal)))
      .replace(/<!-- -->/g, "")
      .replace(/&#x27;/g, "'")
      .replace(/&quot;/g, '"')
      .replace(/&amp;/g, "&");
  } finally {
    globalThis.__FORMA_TEST_COUNTRY_CLOSED__ = undefined;
  }
}
const words = (html) => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
const shows = (html, list, what) => {
  const t = words(html);
  for (const s of list) assert.ok(t.includes(s), `${what}: missing "${s}"`);
};
const hides = (html, list, what) => {
  const t = words(html);
  for (const s of list) assert.ok(!t.includes(s), `${what}: should not show "${s}"`);
};

// A beta account: never agreed, never gave box 2, no date of birth or country.
const beta = {
  email: "rider@example.com",
  terms_current: false,
  health_consent_current: false,
  needs_profile_consent: true,
};
const member = { configured: true, required: true, status: "active", period_end: null, has_access: true };
const unpaid = { configured: true, required: true, status: "none", period_end: null, has_access: false };
const noHold = { hold: null };
const minorHold = { hold: { red_flag: "minor" } };
const allFour = [
  exits.BILLING_BUTTON,
  exits.EXPORT_BUTTON,
  exits.DELETE_BUTTON,
  exits.LOGOUT_BUTTON,
];

test("nothing due: no modal", () => {
  const user = { ...beta, terms_current: true, health_consent_current: true, needs_profile_consent: false };
  assert.equal(render({ user, billing: member, safety: noHold }), "");
});

test("a beta member gets the form and all four ways out under it", () => {
  const html = render({ user: beta, billing: member, safety: noHold });
  shows(html, ["Date of birth", "Where you live", "Somewhere else", "Agree and carry on"], "form");
  shows(html, [exits.EXITS_KICKER, exits.exitsLead("agree", true), ...allFour], "ways out");
  hides(html, [exits.ADULTS_ONLY_TITLE], "form");
  assert.equal((html.match(/type="checkbox"/g) || []).length, 2, "both boxes");
  // The ways out come after the Agree button, inside the one dialog.
  const t = words(html);
  assert.ok(t.indexOf("Agree and carry on") < t.indexOf(exits.LOGOUT_BUTTON));
  assert.equal((html.match(/role="dialog"/g) || []).length, 1);
  // Still nothing that closes it.
  assert.doesNotMatch(html, /aria-label="Close"/);
});

test("nobody pays: no Manage billing, and the sentence doesn't mention it", () => {
  const html = render({ user: beta, billing: unpaid, safety: noHold });
  hides(html, [exits.BILLING_BUTTON, "membership"], "unpaid");
  shows(html, [exits.exitsLead("agree", false), exits.EXPORT_BUTTON, exits.DELETE_BUTTON, exits.LOGOUT_BUTTON], "unpaid");
});

test("terms alone, or box 2 alone, still carry the ways out", () => {
  const termsOnly = { ...beta, needs_profile_consent: false, health_consent_current: true };
  const termsHtml = render({ user: termsOnly, billing: member, safety: noHold });
  hides(termsHtml, ["Date of birth"], "terms only");
  shows(termsHtml, ["Agree and carry on", ...allFour], "terms only");

  const box2Only = { ...beta, terms_current: true, needs_profile_consent: false };
  const box2Html = render({ user: box2Only, billing: member, safety: noHold });
  shows(box2Html, ["Your health details", "Agree and carry on", ...allFour], "box 2 only");
});

test("held as under 18: the adults-only message and the ways out, nothing to fill in", () => {
  const html = render({ user: beta, billing: member, safety: minorHold });
  shows(
    html,
    [
      exits.ADULTS_ONLY_KICKER,
      exits.ADULTS_ONLY_TITLE,
      ...exits.adultsOnlyLines(null),
      exits.exitsLead("held", false),
      exits.EXPORT_BUTTON,
      exits.DELETE_BUTTON,
      exits.LOGOUT_BUTTON,
    ],
    "held"
  );
  // No second try with another date, nothing to agree to, and no portal,
  // which refuses the account (billing.MINOR_BILLING_REFUSAL).
  hides(html, ["Date of birth", "Where you live", "Agree and carry on", exits.BILLING_BUTTON], "held");
  assert.doesNotMatch(html, /type="checkbox"|type="date"|<select/);
  assert.match(html, /aria-labelledby="reaccept-title"/);
  assert.match(html, /id="reaccept-title"/);
});

test("somewhere Forma isn't open: why, then the ways out, and nothing to agree to", () => {
  for (const kind of ["elsewhere", "us_ca"]) {
    const html = render({ user: beta, billing: member, safety: noHold, countryClosed: kind });
    shows(html, [exits.countryClosedText(kind), exits.exitsLead("closed", true), ...allFour], kind);
    hides(html, ["Agree and carry on", "If you'd rather not agree"], kind);
    assert.doesNotMatch(html, /type="checkbox"/, `${kind}: no boxes`);
    // The date of birth stays, so a rider who picked by mistake can pick again.
    shows(html, ["Date of birth", "Where you live"], kind);
  }
});

test("what the modal shows has no dashes and no exclamation marks", () => {
  const dash = new RegExp(`[${String.fromCharCode(0x2013, 0x2014)}]`);
  for (const args of [
    { user: beta, billing: member, safety: noHold },
    { user: beta, billing: member, safety: minorHold },
    { user: beta, billing: member, safety: noHold, countryClosed: "elsewhere" },
  ]) {
    const t = words(render(args));
    assert.ok(!dash.test(t), t.match(new RegExp(`.{0,30}${dash.source}.{0,30}`))?.[0]);
    assert.ok(!t.includes("!"), t);
  }
});
