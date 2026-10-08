// Run: node --test src/lib/account-exits.test.mjs
// Re-verification round 3.
// Problem 7: the re-acceptance modal covered every dashboard page and had no
// way out but Log out, so a beta rider outside the UK (or anyone who wouldn't
// agree) couldn't cancel their membership, take a copy of their data or
// delete their account: all three live in Settings, behind the modal.
// Problem 4: a beta rider who gave an under-18 date of birth was just
// refused, and could try again with another date.
// Node strips the types itself.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  ADULTS_ONLY_HELD,
  ADULTS_ONLY_KICKER,
  ADULTS_ONLY_MISTAKE,
  ADULTS_ONLY_REASON,
  ADULTS_ONLY_TITLE,
  BILLING_BUTTON,
  BILLING_FAILED,
  BILLING_OPENING,
  DELETE_BUTTON,
  DELETE_CANCEL,
  DELETE_CONFIRM_KICKER,
  DELETE_EMAIL_LABEL,
  DELETE_EXPLAINER,
  DELETE_FAILED,
  DELETE_GO,
  DELETE_WORKING,
  EXITS_KICKER,
  EXPORT_BUTTON,
  EXPORT_DONE,
  EXPORT_FAILED,
  EXPORT_WORKING,
  FORMA_EMAIL,
  LOGOUT_BUTTON,
  adultsOnlyLines,
  adultsOnlyRefusal,
  countryClosedText,
  deleteArmed,
  exitsLead,
  exportFilename,
  isMinorHeld,
  offerBilling,
  serverSentence,
} from "./accountExits.ts";
import { refreshesOn401 } from "./api.ts";
import { PROFILE_ELSEWHERE, profileErrorField } from "./profileConsent.ts";
import { countryBlockFor } from "./safetyPrompts.ts";
import { BLOCKED_COUNTRIES, ELSEWHERE } from "../components/safety/safety-rules.ts";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

/** A Python string constant, adjacent literals joined, f-string fields filled. */
function pyConstant(source, name, fields = {}) {
  const start = source.search(new RegExp(`^${name} = `, "m"));
  assert.ok(start >= 0, `${name} not found`);
  const rest = source.slice(start + name.length + 3);
  const span = rest.startsWith("(") ? rest.slice(0, rest.indexOf("\n)")) : rest.split("\n")[0];
  const parts = [...span.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) => m[1]);
  assert.ok(parts.length, `${name} has no string`);
  return parts.join("").replace(/\{([^}]+)\}/g, (_, field) => {
    assert.ok(field in fields, `${name}: no value for {${field}}`);
    return fields[field];
  });
}

const authPy = read("../../../app/api/v1/auth.py");
const billingPy = read("../../../app/api/v1/billing.py");
const safetyPy = read("../../../app/api/v1/safety.py");
const coachPy = read("../../../app/services/coach_service.py");
const emailFields = { "safety_service.FORMA_EMAIL": FORMA_EMAIL, FORMA_EMAIL };

const err = (message, status = 400) => Object.assign(new Error(message), { status });

// === Problem 4: an under-18 refusal ends the form ===

test("the server's under-18 refusals are read as adults-only, word for word", () => {
  const sentences = [
    // What the fixed re-acceptance answers (403) for a date under 18, and
    // for every later try.
    pyConstant(authPy, "MINOR_ACCOUNT"),
    pyConstant(authPy, "UNDER_18"),
    pyConstant(authPy, "MINOR_HEALTH_CONSENT_REFUSAL", emailFields),
    pyConstant(billingPy, "MINOR_BILLING_REFUSAL"),
    pyConstant(safetyPy, "MINOR_REFUSAL", emailFields),
  ];
  for (const sentence of sentences) {
    for (const status of [400, 403]) {
      assert.equal(adultsOnlyRefusal(err(sentence, status)), sentence, `${status}: ${sentence}`);
    }
  }
  // However the fixed re-acceptance words it, an adults-only line is caught.
  for (const sentence of [
    "Forma is for adults, 18 and over, so this account is now on hold.",
    "You need to be 18 or over to use Forma.",
    "This account is held as under 18.",
  ]) {
    assert.equal(adultsOnlyRefusal(err(sentence)), sentence, sentence);
  }
  // A 403 "on hold" with no age in it is the held account too.
  assert.equal(
    adultsOnlyRefusal(err("This account is on hold.", 403)),
    "This account is on hold."
  );
});

test("every other refusal stays on the form, so the rider can put it right", () => {
  const fixable = [
    // Mentions adults, but only means the date is missing.
    pyConstant(authPy, "NO_DATE_OF_BIRTH"),
    pyConstant(authPy, "BAD_DATE_OF_BIRTH"),
    pyConstant(authPy, "NO_COUNTRY"),
    pyConstant(authPy, "BAD_COUNTRY"),
    "Forma isn't available in the US or Canada yet. Join the list at ridewithforma.com and I'll tell you when it is.",
    "Forma isn't available in your country yet. Join the list at ridewithforma.com and I'll tell you when it is.",
    "Refresh the page and tick the box again.",
    "date_of_birth: Input should be a valid date",
    "country: String should have at most 64 characters",
    "Failed to fetch",
    "Something went wrong. Try again.",
    "",
  ];
  for (const message of fixable) {
    assert.equal(adultsOnlyRefusal(err(message)), null, message);
  }
  // "On hold" without a 403 is not the account being held.
  assert.equal(adultsOnlyRefusal(err("This account is on hold.", 400)), null);
  for (const odd of [null, undefined, "text", 42, {}]) {
    assert.equal(adultsOnlyRefusal(odd), null, String(odd));
  }
});

test("the adults-only panel says what the coach says to a held account", () => {
  // coach_service's lines, word for word.
  assert.equal(ADULTS_ONLY_REASON, pyConstant(coachPy, "_ADULTS_ONLY"));
  assert.equal(ADULTS_ONLY_HELD, pyConstant(coachPy, "MINOR_CLOSING_FACT"));
  assert.equal(ADULTS_ONLY_MISTAKE, pyConstant(coachPy, "MINOR_CLOSING_MISTAKE"));

  // Held, with nothing from the server: why, what happens, and the way back.
  const held = [`${ADULTS_ONLY_REASON} ${ADULTS_ONLY_HELD}`, ADULTS_ONLY_MISTAKE];
  assert.deepEqual(adultsOnlyLines(null), held);
  assert.deepEqual(adultsOnlyLines("  "), held);
  // The fixed server's refusal says all of it, the address included: shown alone.
  const minorAccount = pyConstant(authPy, "MINOR_ACCOUNT");
  assert.deepEqual(adultsOnlyLines(minorAccount), [minorAccount]);
  // The heading never repeats the refusal's opening words.
  assert.ok(!minorAccount.startsWith(ADULTS_ONLY_TITLE.replace(/\.$/, "")));
  assert.ok(!ADULTS_ONLY_REASON.startsWith(ADULTS_ONLY_TITLE.replace(/\.$/, "")));
  // The server's refusal first, then the way back for an adult it misread.
  const under18 = pyConstant(authPy, "UNDER_18");
  assert.deepEqual(adultsOnlyLines(under18), [under18, ADULTS_ONLY_MISTAKE]);
  // Never the address twice.
  const box2 = pyConstant(authPy, "MINOR_HEALTH_CONSENT_REFUSAL", emailFields);
  assert.deepEqual(adultsOnlyLines(box2), [box2]);
});

test("an account held as under 18 is read from the safety state", () => {
  assert.equal(isMinorHeld({ hold: { red_flag: "minor" } }), true);
  assert.equal(isMinorHeld({ hold: { red_flag: "head_injury" } }), false);
  assert.equal(isMinorHeld({ hold: null }), false);
  assert.equal(isMinorHeld(null), false);
  assert.equal(isMinorHeld(undefined), false);
});

// === Problem 7: the ways out ===

test("Manage billing is offered to anyone Stripe could still charge", () => {
  const ok = { loadFailed: false, minorHeld: false };
  for (const status of ["active", "trialing", "past_due"]) {
    assert.equal(offerBilling({ configured: true, status }, ok), true, status);
  }
  for (const status of ["none", "canceled"]) {
    assert.equal(offerBilling({ configured: true, status }, ok), false, status);
  }
  // Stripe not set up: there is no portal to open.
  assert.equal(offerBilling({ configured: false, status: "active" }, ok), false);
  // Still loading: not yet. Failed to load: offered, never a rider who can't cancel.
  assert.equal(offerBilling(undefined, ok), false);
  assert.equal(offerBilling(undefined, { loadFailed: true, minorHeld: false }), true);
  // Held as under 18: the portal refuses the account (billing.MINOR_BILLING_REFUSAL).
  assert.equal(
    offerBilling({ configured: true, status: "active" }, { loadFailed: false, minorHeld: true }),
    false
  );
  assert.equal(offerBilling(undefined, { loadFailed: true, minorHeld: true }), false);
  assert.match(billingPy, /except billing_service\.AccountOnHold:\s+raise ForbiddenException\(detail=MINOR_BILLING_REFUSAL\)/);
});

test("the sentence above the buttons names only what is on offer", () => {
  assert.equal(
    exitsLead("agree", true),
    "If you'd rather not agree, you can still cancel your membership in Manage billing, download your data or delete your account here."
  );
  assert.equal(
    exitsLead("agree", false),
    "If you'd rather not agree, you can still download your data or delete your account here."
  );
  assert.equal(
    exitsLead("closed", true),
    "Until Forma opens where you live, you can still cancel your membership in Manage billing, download your data or delete your account here."
  );
  assert.equal(
    exitsLead("held", false),
    "You can still download your data or delete your account here."
  );
  assert.equal(
    exitsLead("answer", true),
    "If you'd rather not answer, you can still cancel your membership in Manage billing, download your data or delete your account here."
  );
  for (const mode of ["agree", "answer", "closed", "held"]) {
    assert.doesNotMatch(exitsLead(mode, false), /Manage billing|membership/, mode);
    assert.match(exitsLead(mode, true), new RegExp(BILLING_BUTTON), mode);
  }
});

test("somewhere Forma isn't open says so and points to the ways out", () => {
  assert.equal(
    countryClosedText("us_ca"),
    "Forma isn't available in the US or Canada yet, so this account can't carry on for now. What you can do instead is below."
  );
  assert.equal(countryClosedText("elsewhere"), PROFILE_ELSEWHERE);
  // With the server's list (the UK only), anything else is closed the moment
  // it is picked, "Somewhere else" included.
  const gb = new Set(["GB"]);
  assert.equal(countryBlockFor("GB", gb, BLOCKED_COUNTRIES, ELSEWHERE), null);
  assert.equal(countryBlockFor("US", gb, BLOCKED_COUNTRIES, ELSEWHERE), "us_ca");
  assert.equal(countryBlockFor("CA", gb, BLOCKED_COUNTRIES, ELSEWHERE), "us_ca");
  assert.equal(countryBlockFor("FR", gb, BLOCKED_COUNTRIES, ELSEWHERE), "elsewhere");
  assert.equal(countryBlockFor(ELSEWHERE, gb, BLOCKED_COUNTRIES, ELSEWHERE), "elsewhere");
  // Without it, the US, Canada and "Somewhere else" still are.
  assert.equal(countryBlockFor("US", null, BLOCKED_COUNTRIES, ELSEWHERE), "us_ca");
  assert.equal(countryBlockFor(ELSEWHERE, null, BLOCKED_COUNTRIES, ELSEWHERE), "elsewhere");
  // And the server's own refusal still lands under the country.
  assert.equal(
    profileErrorField("Forma isn't available in your country yet. Join the list at ridewithforma.com and I'll tell you when it is."),
    "country"
  );
});

test("only the rider's own address arms the delete button", () => {
  assert.equal(deleteArmed("rider@example.com", "rider@example.com"), true);
  assert.equal(deleteArmed("  Rider@Example.com ", "rider@example.com"), true);
  assert.equal(deleteArmed("rider@example.co", "rider@example.com"), false);
  assert.equal(deleteArmed("", "rider@example.com"), false);
  assert.equal(deleteArmed("", ""), false);
  assert.equal(deleteArmed("x", null), false);
  assert.equal(deleteArmed("x", undefined), false);
});

test("the download is named as Settings names it", () => {
  assert.equal(exportFilename(new Date("2026-10-08T09:30:00Z")), "forma-export-2026-10-08.json");
});

test("the download and the deletion say what Settings says", () => {
  // Settings' JSX, with entities and line breaks undone.
  const settings = read("../app/dashboard/settings/page.tsx")
    .replace(/&apos;/g, "'")
    .replace(/\s+/g, " ");
  for (const words of [
    EXPORT_BUTTON,
    EXPORT_WORKING,
    EXPORT_DONE,
    EXPORT_FAILED,
    DELETE_BUTTON,
    DELETE_EXPLAINER,
    DELETE_CONFIRM_KICKER,
    DELETE_EMAIL_LABEL,
    DELETE_GO,
    DELETE_WORKING,
    DELETE_CANCEL,
    DELETE_FAILED,
    "below and the button goes live.",
  ]) {
    assert.ok(settings.includes(words), `Settings no longer says: ${words}`);
  }
  const membership = read("../components/settings/membership-card.tsx");
  assert.ok(membership.includes(BILLING_FAILED), BILLING_FAILED);
  assert.ok(membership.includes(`>\n            ${BILLING_BUTTON}\n`), BILLING_BUTTON);
});

// === Wiring ===

const modal = read("../components/safety/ReacceptTermsModal.tsx");
const exits = read("../components/account/account-exits.tsx");
const fields = read("../components/account/profile-consent-fields.tsx");
const layout = read("../app/dashboard/layout.tsx");
const api = read("./api.ts");

test("the modal carries the ways out, on the form and on the adults-only panel", () => {
  const has = (src, re, what) => assert.ok(re.test(src), what);
  // Both the form and the adults-only panel end with the ways out.
  assert.equal((modal.match(/<AccountExits\b/g) || []).length, 2, "two <AccountExits />");
  has(modal, /mode=\{countryClosed \? "closed" : "agree"\}/, "form: agree or closed");
  has(modal, /mode="held"/, "adults-only panel: held");
  // Still no close button.
  assert.doesNotMatch(modal, /aria-label="Close"|onClose|<X\b/);
  // The old lone Log out is gone from the modal; the ways out carry it.
  assert.doesNotMatch(modal, /onClick=\{logout\}/);

  has(exits, /billing\.portal\(\)/, "Manage billing opens Stripe's portal");
  has(exits, /window\.location\.href = url/, "and goes there");
  has(exits, /users\.saveMyData\(exportFilename\(\)\)/, "Download my data");
  has(exits, /users\.deleteMyAccount\(\)/, "Delete my account");
  has(exits, /disabled=\{!deleteArmed\(typedEmail, user\?\.email\) \|\| deleting\}/, "armed by the email");
  has(exits, /logout\(\); \/\/ clears the tokens/, "deletion logs out");
  has(exits, /onClick=\{logout\}/, "Log out");
  has(exits, /offerBilling\(status, \{ loadFailed: statusFailed, minorHeld \}\)/, "billing only when it can help");

  has(api, /saveMyData: async \(filename: string\): Promise<void> =>/, "api.saveMyData");
  has(api, /request<Record<string, unknown>>\("\/users\/me\/export"\);\s+const blob/, "saves the GDPR export");
});

test("an under-18 refusal stops the form, and a held account never sees it", () => {
  const catchAt = modal.indexOf("const refusal = adultsOnlyRefusal(err);");
  const fieldAt = modal.indexOf("profile.showServerError(message)");
  assert.ok(catchAt > 0, "the refusal is checked");
  assert.ok(catchAt < fieldAt, "before it could land under the date field and invite a retry");
  assert.match(modal, /setAdultsOnly\(refusal\);\s+queryClient\.invalidateQueries\(\{ queryKey: SAFETY_STATE_KEY \}\);\s+return;/);
  assert.match(modal, /const held = adultsOnly !== null \|\| isMinorHeld\(safetyState\);/);
  // The adults-only panel returns before the form is built.
  const heldAt = modal.indexOf("if (held) {");
  const formAt = modal.indexOf("<ProfileConsentFields");
  assert.ok(heldAt > 0 && heldAt < formAt, "held returns first");
  assert.match(modal, /\{ADULTS_ONLY_TITLE\}/);
  assert.match(modal, /adultsOnlyLines\(message\)/);
});

test("somewhere Forma isn't open turns the form to the ways out", () => {
  assert.match(fields, /countryBlockFor\(country, allowedCodes, BLOCKED_COUNTRIES, ELSEWHERE\)/);
  assert.match(fields, /closed \? countryClosedText\(closed\) : undefined/, "said the moment it is picked");
  assert.match(fields, /countryClosed,\n/);
  // No box to tick and no Agree button from there.
  assert.match(modal, /const countryClosed = !held && termsDue && profile\.countryClosed !== null;/);
  assert.match(modal, /\{!countryClosed && \(\s+<ConsentBox/);
  assert.match(modal, /\{due\.health && !countryClosed && \(/);
  assert.match(modal, /\{!countryClosed && \(\s+<div className="mt-6 flex justify-end">\s+<Button variant="flamme" onClick=\{agree\}/);
});

test("a held account isn't asked to join", () => {
  assert.match(layout, /if \(isMinorHeld\(safetyState\)\) return null;/);
  assert.match(layout, /<ReacceptTermsModal \/>/);
});

// === Failures say something a rider can act on ===

test("a failed call shows the server's reason, never the browser's", () => {
  const stripeDown = "I couldn't end your membership just now, so nothing has been deleted. Try again in a minute.";
  assert.equal(serverSentence(err(stripeDown, 400), DELETE_FAILED), stripeDown);
  assert.equal(serverSentence(err(pyConstant(billingPy, "MINOR_BILLING_REFUSAL"), 403), BILLING_FAILED),
    pyConstant(billingPy, "MINOR_BILLING_REFUSAL"));
  // A lost connection: fetch throws a TypeError with no status.
  assert.equal(serverSentence(new TypeError("Failed to fetch"), DELETE_FAILED), DELETE_FAILED);
  assert.equal(serverSentence(new TypeError("Load failed"), BILLING_FAILED), BILLING_FAILED);
  // A server fault, whatever its body says.
  assert.equal(serverSentence(err("Internal Server Error", 500), DELETE_FAILED), DELETE_FAILED);
  assert.equal(serverSentence(err("", 400), DELETE_FAILED), DELETE_FAILED);
  for (const odd of [null, undefined, "text", 42, {}]) {
    assert.equal(serverSentence(odd, BILLING_FAILED), BILLING_FAILED, String(odd));
  }
  // Both ways out that call the server, and the modal's own Agree, use it.
  assert.match(exits, /setBillingError\(serverSentence\(err, BILLING_FAILED\)\)/);
  assert.match(exits, /setDeleteError\(serverSentence\(err, DELETE_FAILED\)\)/);
  assert.match(modal, /const message = serverSentence\(err, SAVE_FAILED\);/);
  assert.doesNotMatch(exits + modal, /err instanceof Error \? err\.message|err\.message : /);
});

test("box 2 failing after the terms saved is shown under box 2", () => {
  assert.match(modal, /await auth\.reacceptTerms\(TERMS_BOX\.text, profile\.payload\);\s+termsSaved = true;/);
  assert.match(modal, /if \(termsDue && !termsSaved\) setError\(message\);\s+else setHealthError\(message\);/);
});

// === The modal outlasting the 30-minute access token ===

test("the modal's own calls refresh an expired session instead of failing", () => {
  // Every /auth/ route that takes get_current_user, read from auth.py.
  const routes = [...authPy.matchAll(/^@router\.(?:post|get|put|patch|delete)\("([^"]+)"/gm)].map((m) => {
    const def = authPy.indexOf("def ", m.index);
    const end = authPy.slice(def).search(/\)(?: -> [^\n]+)?:\n/);
    return { path: `/auth${m[1]}`, signature: authPy.slice(def, def + end) };
  });
  assert.ok(routes.length >= 10, `found ${routes.length} auth routes`);
  const signedIn = routes.filter((r) => /get_current_user/.test(r.signature)).map((r) => r.path);
  for (const path of ["/auth/reaccept-terms", "/auth/health-consent", "/auth/resend-verification"]) {
    assert.ok(signedIn.includes(path), `${path} takes a session`);
  }
  for (const path of signedIn) assert.equal(refreshesOn401(path), true, path);
  // Where a 401 is the answer itself, it is shown, not refreshed away.
  for (const path of ["/auth/login", "/auth/register", "/auth/forgot-password", "/auth/reset-password", "/auth/verify-email"]) {
    assert.ok(!signedIn.includes(path), `${path} takes no session`);
    assert.equal(refreshesOn401(path), false, path);
  }
  // Everything else refreshes, as before.
  for (const path of ["/users/me", "/users/me/export", "/billing/portal", "/users/me/safety-state"]) {
    assert.equal(refreshesOn401(path), true, path);
  }
  assert.match(api, /if \(response\.status === 401 && !refreshesOn401\(path\)\)/);
});

// === The words ===

test("the new words follow the house rules", () => {
  const lines = [
    ADULTS_ONLY_KICKER,
    ADULTS_ONLY_TITLE,
    ADULTS_ONLY_REASON,
    ADULTS_ONLY_HELD,
    ADULTS_ONLY_MISTAKE,
    EXITS_KICKER,
    BILLING_BUTTON,
    BILLING_OPENING,
    BILLING_FAILED,
    LOGOUT_BUTTON,
    countryClosedText("us_ca"),
    countryClosedText("elsewhere"),
    ...["agree", "answer", "closed", "held"].flatMap((m) => [exitsLead(m, true), exitsLead(m, false)]),
  ];
  const dash = new RegExp(`[${String.fromCharCode(0x2013, 0x2014)}]`);
  for (const line of lines) {
    assert.ok(!dash.test(line), line);
    assert.ok(!line.includes("!"), line);
    assert.ok(!/kicker|\bsafe\b|guarantee/i.test(line), line);
    assert.ok(!/\b(color|center|organize|license|canceled)\b/i.test(line), line);
  }
  for (const file of [
    "./accountExits.ts",
    "./account-exits.test.mjs",
    "./profileConsent.ts",
    "../components/account/account-exits.tsx",
    "../components/account/profile-consent-fields.tsx",
    "../components/safety/ReacceptTermsModal.tsx",
    "../app/dashboard/layout.tsx",
    "./account-exits-render.test.mjs",
  ]) {
    assert.ok(!dash.test(read(file)), `${file} has a dash`);
  }
});

test("the health questions prompt offers the same ways out, not just Log out", () => {
  // Re-verification round 3, problem 7, the same trap: the re-screen also
  // blocks every dashboard page, and a rider who won't answer could only log
  // out, with billing, export and deletion behind it in Settings.
  const prompt = read("../components/dashboard/rescreen-prompt.tsx");
  assert.match(prompt, /<AccountExits\s+mode="answer"\s+minorHeld=\{false\}/);
  assert.doesNotMatch(prompt, /onClick=\{logout\}/);
});
