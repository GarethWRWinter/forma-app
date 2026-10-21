// Run: node --test src/lib/profile-consent.test.mjs
// Re-verification new problem 11: beta accounts agreed to the new terms
// (18 and over, in the UK) but were never asked a date of birth or a
// country, and no age row was recorded. The re-acceptance modal now asks for
// both when /users/me says needs_profile_consent, sends them with
// auth.reacceptTerms, and shows the server's refusals under the right field.
// Node strips the types itself.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  PROFILE_CONSENT_INTRO,
  PROFILE_COUNTRY_LABEL,
  PROFILE_DOB_LABEL,
  PROFILE_ELSEWHERE,
  PROFILE_NO_COUNTRY,
  PROFILE_NO_DOB,
  profileConsentDue,
  profileErrorField,
  profilePayload,
  profileProblems,
} from "./profileConsent.ts";
import { reacceptTermsBody } from "./api.ts";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const today = new Date(2026, 9, 8, 12, 0, 0);

// === When the modal asks ===

test("only an account the server flags is asked for a date of birth and country", () => {
  assert.equal(profileConsentDue({ needs_profile_consent: true }), true);
  assert.equal(profileConsentDue({ needs_profile_consent: false }), false);
  // An older server that doesn't send the flag asks for nothing new.
  assert.equal(profileConsentDue({}), false);
  assert.equal(profileConsentDue(null), false);
  assert.equal(profileConsentDue(undefined), false);
});

// === What stops it sending ===

test("both fields are needed before the modal sends", () => {
  assert.deepEqual(profileProblems({ dateOfBirth: "", country: "" }, "elsewhere", today), {
    dateOfBirth: PROFILE_NO_DOB,
    country: PROFILE_NO_COUNTRY,
  });
  assert.deepEqual(
    profileProblems({ dateOfBirth: "1984-05-12", country: "GB" }, "elsewhere", today),
    {}
  );
});

test("a date that doesn't exist or hasn't happened yet counts as missing", () => {
  for (const bad of ["2001-02-30", "1990-13-01", "12/05/1984", "2026-10-09", "2031-01-01"]) {
    assert.equal(
      profileProblems({ dateOfBirth: bad, country: "GB" }, "elsewhere", today).dateOfBirth,
      PROFILE_NO_DOB,
      bad
    );
  }
  // Born today is a real date; the server says it is under 18.
  assert.equal(
    profileProblems({ dateOfBirth: "2026-10-08", country: "GB" }, "elsewhere", today).dateOfBirth,
    undefined
  );
});

test("an under-18 date is sent, so the server's age rule decides", () => {
  // The server owns the age rule (auth.check_age), as it does at sign-up,
  // and refuses with its own sentence, which shows under the date field.
  // One rule in one place: the modal never second-guesses it.
  assert.deepEqual(
    profileProblems({ dateOfBirth: "2011-03-01", country: "GB" }, "elsewhere", today),
    {}
  );
  // A country off the list goes to the server too, which names it.
  assert.deepEqual(
    profileProblems({ dateOfBirth: "1984-05-12", country: "US" }, "elsewhere", today),
    {}
  );
});

test("'Somewhere else' is answered here, because it has no code to send", () => {
  assert.equal(
    profileProblems({ dateOfBirth: "1984-05-12", country: "elsewhere" }, "elsewhere", today)
      .country,
    PROFILE_ELSEWHERE
  );
});

// === What reacceptTerms carries ===

test("reacceptTerms sends the date of birth and country only when they were asked for", () => {
  assert.deepEqual(reacceptTermsBody("I agree"), { text_shown: "I agree" });
  assert.ok(!("date_of_birth" in reacceptTermsBody("I agree")));
  assert.ok(!("country" in reacceptTermsBody("I agree")));

  const choices = { dateOfBirth: " 1984-05-12 ", country: "gb " };
  assert.equal(profilePayload(false, choices), undefined);
  const payload = profilePayload(true, choices);
  assert.deepEqual(payload, { dateOfBirth: "1984-05-12", country: "GB" });
  assert.deepEqual(reacceptTermsBody("I agree", payload), {
    text_shown: "I agree",
    date_of_birth: "1984-05-12",
    country: "GB",
  });

  const api = read("./api.ts");
  assert.match(api, /reacceptTerms: \(textShown: string, profile\?: ProfileConsentInput\)/);
  assert.match(api, /JSON\.stringify\(reacceptTermsBody\(textShown, profile\)\)/);
});

// === Server refusals go under the field they are about ===

/** A Python string constant from app/api/v1/auth.py, adjacent literals joined. */
function pyConstant(source, name) {
  const start = source.search(new RegExp(`^${name} = `, "m"));
  assert.ok(start >= 0, `${name} not found in auth.py`);
  const rest = source.slice(start + name.length + 3);
  const span = rest.startsWith("(") ? rest.slice(0, rest.indexOf("\n)")) : rest.split("\n")[0];
  const parts = [...span.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) => m[1]);
  assert.ok(parts.length, `${name} has no string`);
  return parts.join("");
}

test("the server's own refusal sentences land under the right field", () => {
  const authPy = read("../../../app/api/v1/auth.py");
  for (const name of ["UNDER_18", "NO_DATE_OF_BIRTH", "BAD_DATE_OF_BIRTH"]) {
    const sentence = pyConstant(authPy, name);
    assert.equal(profileErrorField(sentence), "dateOfBirth", `${name}: ${sentence}`);
  }
  for (const name of ["NO_COUNTRY", "BAD_COUNTRY"]) {
    const sentence = pyConstant(authPy, name);
    assert.equal(profileErrorField(sentence), "country", `${name}: ${sentence}`);
  }
  // _not_available(code), for the US and Canada and for anywhere else.
  assert.match(authPy, /Forma isn't available in \{where\} yet/);
  for (const where of ["the US or Canada", "your country"]) {
    const sentence = `Forma isn't available in ${where} yet. Join the list at ridewithforma.com and I'll tell you when it is.`;
    assert.equal(profileErrorField(sentence), "country", sentence);
  }
});

test("FastAPI's own field errors land under the field, anything else under the box", () => {
  assert.equal(profileErrorField("date_of_birth: Input should be a valid date"), "dateOfBirth");
  assert.equal(profileErrorField("country: String should have at most 64 characters"), "country");
  for (const other of [
    "Refresh the page and tick the box again.",
    "That didn't save. Try again.",
    "Something went wrong. Try again.",
    "",
    null,
    undefined,
  ]) {
    assert.equal(profileErrorField(other), null, String(other));
  }
});

// === The dashboard waits for it ===

test("the health questions wait until the date of birth and country are in", () => {
  const layout = read("../app/dashboard/layout.tsx");
  assert.match(layout, /<ReacceptTermsModal \/>/);
  assert.match(layout, /\{!profileConsentDue\(user\) && <RescreenPrompt \/>\}/);
  // Never unconditionally.
  assert.doesNotMatch(layout, /^\s*<RescreenPrompt \/>/m);
});

// === Types the app reads ===

test("api.ts carries the new fields the server sends", () => {
  const api = read("./api.ts");
  assert.match(api, /needs_profile_consent\?: boolean;/);
  assert.match(api, /no_racing_or_group_until\?: string \| null;/);
  const kinds = /export type SafetyLiftKind =([^;]+);/.exec(api);
  assert.ok(kinds, "SafetyLiftKind not found");
  for (const kind of ["doctor", "fever_self", "head_injury", "admin_only"]) {
    assert.match(kinds[1], new RegExp(`"${kind}"`), kind);
  }
});

// === The words ===

test("the new words follow the house rules", () => {
  const lines = [
    PROFILE_CONSENT_INTRO,
    PROFILE_DOB_LABEL,
    PROFILE_COUNTRY_LABEL,
    PROFILE_NO_DOB,
    PROFILE_NO_COUNTRY,
    PROFILE_ELSEWHERE,
  ];
  const dash = new RegExp(`[${String.fromCharCode(0x2013, 0x2014)}]`);
  for (const line of lines) {
    assert.ok(!dash.test(line), line);
    assert.ok(!line.includes("!"), line);
    assert.ok(!/kicker|\bsafe\b|guarantee/i.test(line), line);
    assert.ok(!/\b(color|center|organize|license)\b/i.test(line), line);
  }
  for (const file of [
    "./profileConsent.ts",
    "./profile-consent.test.mjs",
    "../components/account/profile-consent-fields.tsx",
    "../app/dashboard/layout.tsx",
  ]) {
    assert.ok(!dash.test(read(file)), `${file} has a dash`);
  }
  const fields = read("../components/account/profile-consent-fields.tsx");
  assert.match(fields, />Somewhere else</);
});

// === The modal itself ===

const modal = read("../components/safety/ReacceptTermsModal.tsx");

test(
  "the re-acceptance modal asks for both and sends them with the terms",
  () => {
    const has = (re, what) => assert.ok(re.test(modal), `ReacceptTermsModal: ${what}`);
    has(/useProfileConsent\(user\)/, "calls useProfileConsent(user)");
    has(/<ProfileConsentFields\b/, "renders <ProfileConsentFields />");
    // Opens for the profile alone, and the terms box shows with it.
    has(/profile\.due/, "opens on profile.due");
    // The fields travel with the terms, and the server's refusals go under them.
    has(
      /auth\.reacceptTerms\(TERMS_BOX\.text, profile\.payload\)/,
      "sends profile.payload with the terms"
    );
    has(/profile\.showServerError\(/, "routes server refusals to the fields");
    has(/profile\.check\(\)/, "checks both fields before sending");
  }
);
