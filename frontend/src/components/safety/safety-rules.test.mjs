// Run: node --test src/components/safety/safety-rules.test.mjs
// Node strips the types from safety-rules.ts itself, so no build step.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  ageOn,
  completeAnswers,
  countryBlock,
  emergencyNumber,
  ftpGate,
  hasSamaritans,
  holdView,
  liftedMessage,
  mistakeLiftedMessage,
  MISTAKE_LIFTED,
  STILL_HELD,
  registrationProblems,
  showCheckUpLine,
  aiDisclaimer,
  screeningIntro,
  HEALTH_BOX,
  TERMS_BOX,
  SCREENING_QUESTIONS,
  COUNTRIES,
  CLEARANCE_TEXT,
  FEVER_LIFT_TEXT,
  FEVER_LIFT_BUTTON,
  MINOR_HOLD_TEXT,
  MINOR_HOLD_TITLE,
  clearanceOffer,
  limitsOf,
  liftKindOf,
  ELSEWHERE,
  UNDER_18,
  US_CA_BLOCKED,
  ELSEWHERE_BLOCKED,
  TERMS_UNTICKED,
  HEALTH_UNTICKED,
  HEAD_LIFT_TEXT,
  HEAD_CHECKED_BY_OPTIONS,
  clearanceCopy,
  noRacingLine,
} from "./safety-rules.ts";

const day = (y, m, d) => new Date(y, m - 1, d);

test("ageOn counts whole years and turns on the birthday itself", () => {
  assert.equal(ageOn("2008-10-08", day(2026, 10, 7)), 17);
  assert.equal(ageOn("2008-10-08", day(2026, 10, 8)), 18);
  assert.equal(ageOn("1990-02-28", day(2026, 2, 27)), 35);
  assert.equal(ageOn("1990-02-28", day(2026, 2, 28)), 36);
});

test("ageOn rejects missing, malformed, impossible and future dates", () => {
  assert.equal(ageOn("", day(2026, 10, 8)), null);
  assert.equal(ageOn(null, day(2026, 10, 8)), null);
  assert.equal(ageOn("08/10/1990", day(2026, 10, 8)), null);
  assert.equal(ageOn("2001-02-30", day(2026, 10, 8)), null);
  assert.equal(ageOn("2030-01-01", day(2026, 10, 8)), null);
});

test("emergency numbers follow where the rider lives", () => {
  assert.equal(emergencyNumber("GB"), "999");
  assert.equal(emergencyNumber(null), "999");
  assert.equal(emergencyNumber("us"), "911");
  assert.equal(emergencyNumber("CA"), "911");
  assert.equal(emergencyNumber("FR"), "112");
  assert.equal(emergencyNumber("IE"), "112");
  assert.ok(aiDisclaimer("DE").endsWith("call 112."));
  assert.ok(aiDisclaimer("GB").endsWith("call 999."));
  assert.ok(screeningIntro("US").endsWith("call 911."));
  assert.equal(hasSamaritans("IE"), true);
  assert.equal(hasSamaritans("FR"), false);
});

test("the US, Canada and anywhere off the list are blocked with a reason", () => {
  assert.equal(countryBlock("US"), US_CA_BLOCKED);
  assert.equal(countryBlock("ca"), US_CA_BLOCKED);
  assert.equal(countryBlock(ELSEWHERE), ELSEWHERE_BLOCKED);
  assert.equal(countryBlock("GB"), null);
  assert.equal(countryBlock("ES"), null);
  assert.equal(countryBlock(""), null);
  // Every listed code is a two-letter ISO code the API will accept.
  for (const c of COUNTRIES) assert.match(c.code, /^[A-Z]{2}$/);
});

test("without the server's list, the form opens only the UK, as the terms do", () => {
  // Re-verification new problem 4: the terms say UK only, so the fallback
  // must not offer the EU. The US and Canada stay listed only to be refused.
  const open = COUNTRIES.filter((c) => countryBlock(c.code) === null).map((c) => c.code);
  assert.deepEqual(open, ["GB"]);
  assert.doesNotMatch(ELSEWHERE_BLOCKED, /\bEU\b|European/);
  assert.match(ELSEWHERE_BLOCKED, /only open in the UK/);
});

test("registration stops under-18s, blocked countries and unticked boxes", () => {
  const today = day(2026, 10, 8);
  const ok = { dateOfBirth: "1990-05-01", country: "GB", termsAccepted: true, healthConsent: true };
  assert.deepEqual(registrationProblems(ok, today), {});

  assert.equal(registrationProblems({ ...ok, dateOfBirth: "2009-01-01" }, today).dateOfBirth, UNDER_18);
  assert.equal(registrationProblems({ ...ok, dateOfBirth: "" }, today).dateOfBirth, "Add your date of birth.");
  assert.equal(registrationProblems({ ...ok, country: "US" }, today).country, US_CA_BLOCKED);
  assert.equal(registrationProblems({ ...ok, country: "" }, today).country, "Choose where you live.");
  assert.equal(registrationProblems({ ...ok, termsAccepted: false }, today).terms, TERMS_UNTICKED);
  assert.equal(registrationProblems({ ...ok, healthConsent: false }, today).health, HEALTH_UNTICKED);
  // Box 1 and box 2 are judged separately: one ticked never covers the other.
  const onlyTerms = registrationProblems({ ...ok, healthConsent: false }, today);
  assert.equal(onlyTerms.terms, undefined);
  assert.ok(onlyTerms.health);
});

test("the recorded consent text is exactly the label the rider reads", () => {
  assert.equal(
    TERMS_BOX.text,
    "I agree to Forma's terms. I understand the coaching is written by AI and can be wrong, that it isn't medical advice, and that I decide what I ride and stop if something feels wrong."
  );
  assert.equal(
    HEALTH_BOX.text,
    "Forma can use the health details I share with it, such as injuries, illness, medication, sleep and my answers to its health questions, to coach me. The privacy policy explains how to withdraw this."
  );
  assert.equal(TERMS_BOX.text, TERMS_BOX.before + TERMS_BOX.link + TERMS_BOX.after);
  assert.equal(
    CLEARANCE_TEXT,
    "A doctor (or my midwife or physio) has assessed me and cleared me for hard training."
  );
});

test("screening needs a yes or a no for all eight questions", () => {
  assert.equal(SCREENING_QUESTIONS.length, 8);
  const all = Object.fromEntries(SCREENING_QUESTIONS.map((q) => [q.id, false]));
  assert.deepEqual(completeAnswers(all), all);
  const missing = { ...all };
  delete missing.q8;
  assert.equal(completeAnswers(missing), null);
  assert.equal(completeAnswers({}), null);
});

const hold = (over) => ({
  id: "h1",
  level: "easy_only",
  reason: "Screening",
  red_flag: null,
  source: "screening",
  opened_at: "2026-10-08T09:00:00",
  ...over,
});

test("hold wording and buttons follow the level and the source", () => {
  const easy = holdView(hold({}), "GB");
  assert.match(easy.text, /^Hard sessions are on hold until you tell me a doctor has cleared you\./);
  assert.equal(easy.canClear, true);
  assert.equal(easy.canMistake, false); // the server refuses it for screening holds
  assert.equal(easy.fromAnswers, true);

  const all = holdView(hold({ level: "hold_all", source: "detector" }), "FR");
  assert.equal(
    all.text,
    "Riding is on hold until a doctor has checked you over. If symptoms come back, call 112."
  );
  assert.equal(all.canMistake, true);

  assert.equal(holdView(hold({ source: "admin" })).canMistake, false);
  const layoff = holdView(hold({ source: "layoff" }));
  assert.equal(layoff.canClear, false);
});

const state = (over) => ({
  allowed: "all",
  hold: null,
  screening: null,
  layoff_gate_until: null,
  ride_mode_ack: false,
  ftp: 250,
  erg_cap: 1.3,
  ceilings: {},
  ...over,
});

test("the FTP test gate fails closed and names the right reason", () => {
  assert.equal(ftpGate(undefined), "unknown");
  assert.equal(ftpGate(state({})), "open");
  assert.equal(ftpGate(state({ allowed: "none" })), "doctor");
  assert.equal(ftpGate(state({ allowed: "easy", hold: hold({}) })), "doctor");
  assert.equal(
    ftpGate(
      state({
        allowed: "easy",
        screening: { tier: "easy_only", version: "screen-v1", clearance_confirmed: false, limits: null },
      })
    ),
    "doctor"
  );
  // Cleared by a doctor, but back from a long break: the layoff message.
  assert.equal(
    ftpGate(
      state({
        allowed: "easy",
        layoff_gate_until: "2026-10-20",
        screening: { tier: "easy_only", version: "screen-v1", clearance_confirmed: true, limits: null },
      })
    ),
    "layoff"
  );
});

test("after a clearance the rider is told what still applies", () => {
  assert.equal(liftedMessage(state({})), "Thanks. The hold is lifted.");
  assert.equal(liftedMessage(state({ hold: hold({ source: "detector" }) })), STILL_HELD);
  assert.equal(
    liftedMessage(state({ allowed: "easy", layoff_gate_until: "2026-10-20" })),
    "Thanks. The hold is lifted. You've had a break, so I'll keep things steady until 20 October."
  );
  assert.match(
    liftedMessage(
      state({ screening: { tier: "easy_only", version: "screen-v1", clearance_confirmed: true, limits: "No sprints" } })
    ),
    /hard limit\.$/
  );
});

test("the check-up line shows from 35, and when the age is unknown", () => {
  const today = day(2026, 10, 8);
  assert.equal(showCheckUpLine("1991-10-08", today), true);
  assert.equal(showCheckUpLine("1991-10-09", today), false);
  assert.equal(showCheckUpLine(null, today), true);
});

test("no dashes in any of the copy", () => {
  const strings = [
    TERMS_BOX.text,
    HEALTH_BOX.text,
    UNDER_18,
    US_CA_BLOCKED,
    ELSEWHERE_BLOCKED,
    TERMS_UNTICKED,
    HEALTH_UNTICKED,
    aiDisclaimer("GB"),
    screeningIntro("GB"),
    ...SCREENING_QUESTIONS.map((q) => q.text),
  ];
  for (const s of strings) assert.doesNotMatch(s, /[\u2013\u2014]/);
});

// === Lift kinds: under 18, fever, the easy week ===

test("an under-18 hold says the account will be closed and offers no way out", () => {
  for (const source of ["detector", "coach_tool"]) {
    const minor = hold({ level: "hold_all", source, red_flag: "minor", lift_kind: "admin_only" });
    const view = holdView(minor, "GB");
    assert.equal(view.title, "Account on hold");
    assert.equal(MINOR_HOLD_TITLE, "Account on hold");
    assert.equal(
      view.text,
      "Forma is for adults, 18 and over. This account is on hold and will be closed, with anything you've paid refunded."
    );
    assert.equal(view.text, MINOR_HOLD_TEXT);
    assert.equal(view.canClear, false);
    assert.equal(view.canMistake, false);
    assert.equal(view.canSelfLift, false);
    assert.equal(view.fromAnswers, false);
    // Even without lift_kind from an older server.
    assert.equal(holdView({ ...minor, lift_kind: undefined }).canClear, false);
    assert.equal(clearanceOffer(state({ allowed: "none", hold: minor })), null);
    // Not even under an uncleared screening yes.
    assert.equal(
      clearanceOffer(
        state({
          allowed: "none",
          hold: minor,
          screening: { tier: "easy_only", version: "screen-v1", clearance_confirmed: false, limits: null },
        })
      ),
      null
    );
    assert.equal(ftpGate(state({ allowed: "none", hold: minor })), "closed");
  }
});

test("a fever hold offers the rider's own word, never the doctor form", () => {
  const fever = hold({ level: "hold_all", source: "detector", red_flag: "fever", lift_kind: "fever_self" });
  const view = holdView(fever, "GB");
  assert.equal(view.canClear, false);
  assert.equal(view.canSelfLift, true);
  assert.equal(view.canMistake, true);
  assert.match(view.text, /tap My fever has gone/);
  assert.doesNotMatch(view.text, /doctor|hard training/);
  assert.ok(view.text.endsWith("call 999."));
  assert.equal(FEVER_LIFT_BUTTON, "My fever has gone");
  assert.equal(
    FEVER_LIFT_TEXT,
    "My fever has been gone for 24 hours without paracetamol or ibuprofen, and my chest has cleared."
  );
  assert.equal(clearanceOffer(state({ allowed: "none", hold: fever })), "fever_self");
  assert.equal(ftpGate(state({ allowed: "none", hold: fever })), "layoff");
  // Worked out from the fields when lift_kind isn't sent.
  assert.equal(liftKindOf({ ...fever, lift_kind: undefined }), "fever_self");
});

test("the easy week after a fever ends by itself and can't be waved away", () => {
  const week = hold({
    level: "easy_only",
    source: "detector",
    red_flag: "fever",
    lift_kind: "expires",
    expires_at: "2026-10-15T10:00:00",
  });
  const view = holdView(week, "FR");
  assert.equal(view.title, "Easy riding only");
  assert.equal(
    view.text,
    "Easy riding only until 15 October, while you get over the fever. If it comes back, stop riding and tell me. If you get chest pain or struggle to breathe, call 112."
  );
  assert.equal(view.canClear || view.canSelfLift || view.canMistake, false);
  assert.equal(clearanceOffer(state({ allowed: "easy", hold: week })), null);
  assert.equal(ftpGate(state({ allowed: "easy", hold: week })), "layoff");
  assert.equal(
    liftedMessage(state({ allowed: "easy", hold: week })),
    "Thanks. Your first week back is easy riding only, until 15 October."
  );
  assert.equal(liftKindOf({ ...week, lift_kind: undefined }), "expires");
});

test("doctor holds still offer clearance; hand-set ones offer nothing", () => {
  const chest = hold({ level: "hold_all", source: "detector", red_flag: "chest_pain", lift_kind: "doctor" });
  assert.equal(clearanceOffer(state({ allowed: "none", hold: chest })), "doctor");
  assert.equal(ftpGate(state({ allowed: "none", hold: chest })), "doctor");
  const byHand = holdView(hold({ source: "admin", lift_kind: "admin_only" }));
  assert.equal(byHand.canClear || byHand.canMistake || byHand.canSelfLift, false);
});

test("every limit a clinician set is shown, not just the latest screening's", () => {
  const limits = [
    { id: "a", text: "No sprints", by: "My GP", recorded_at: "2026-09-01T09:00:00" },
    { id: "b", text: "No standing climbs", by: "My physio", recorded_at: "2026-10-01T09:00:00" },
  ];
  assert.deepEqual(limitsOf(state({ limits })), ["No sprints", "No standing climbs"]);
  assert.deepEqual(
    limitsOf(state({ screening: { tier: "none", version: "screen-v1", clearance_confirmed: true, limits: "Old" } })),
    ["Old"]
  );
  assert.match(liftedMessage(state({ limits })), /hard limit\.$/);
});

test("no dashes or exclamation marks in the hold copy", () => {
  const holds = [
    hold({ red_flag: "minor", source: "detector", level: "hold_all" }),
    hold({ red_flag: "fever", source: "detector", level: "hold_all" }),
    hold({ red_flag: "fever", source: "detector", expires_at: "2026-10-15T10:00:00" }),
    hold({ source: "layoff", red_flag: "layoff" }),
    hold({ source: "admin" }),
    hold({ source: "detector", level: "hold_all" }),
    hold({}),
  ];
  const strings = [FEVER_LIFT_TEXT, MINOR_HOLD_TEXT, ...holds.flatMap((h) => {
    const v = holdView(h, "GB");
    return [v.title, v.text];
  })];
  for (const s of strings) {
    assert.doesNotMatch(s, /[\u2013\u2014!]/);
    assert.doesNotMatch(s, / - /);
  }
});

// === The easy start after a break ends by itself ===

test("a break hold names the day it ends and stays a break, not a doctor's hold", () => {
  const brk = hold({
    level: "easy_only",
    source: "layoff",
    red_flag: "layoff",
    lift_kind: "layoff",
    expires_at: "2026-11-05T10:00:00",
  });
  const view = holdView(brk, "GB");
  assert.equal(view.title, "Easing back in");
  assert.equal(
    view.text,
    "Hard sessions are on hold until 5 November while you ease back in after a break. Easy riding is fine if you feel well."
  );
  assert.equal(view.canClear, false);
  assert.equal(view.canMistake, true);
  assert.equal(clearanceOffer(state({ allowed: "easy", hold: brk })), null);
  assert.equal(ftpGate(state({ allowed: "easy", hold: brk })), "layoff");
  // Worked out the server's way when lift_kind isn't sent: a break first.
  assert.equal(liftKindOf({ ...brk, lift_kind: undefined }), "layoff");
  // An older break hold with no end date reads as before.
  assert.match(holdView({ ...brk, expires_at: null }).text, /^Hard sessions are on hold while you ease back in/);
});

// === Head injury: its own check, then a graded return (reverify S3) ===

const headHold = (over) =>
  hold({ level: "hold_all", source: "detector", red_flag: "head_injury", lift_kind: "head_injury", ...over });

test("a head injury opens its own form, never the hard-training clearance", () => {
  const view = holdView(headHold({}), "GB", "2026-10-22");
  assert.equal(view.title, "Riding on hold");
  assert.equal(view.canClear, true);
  assert.equal(view.clearKind, "head_injury");
  assert.equal(view.canSelfLift, false);
  assert.equal(view.canMistake, true);
  assert.equal(
    view.text,
    "No riding, training or racing until a doctor has checked you and you've had no symptoms for at least 24 hours. Then easy riding only until two weeks after the injury. No racing or group riding before 22 October, three weeks after your head injury. If symptoms get worse, call 999."
  );
  // Worked out from the fields when lift_kind isn't sent.
  assert.equal(liftKindOf({ ...headHold({}), lift_kind: undefined }), "head_injury");
  assert.equal(clearanceOffer(state({ allowed: "none", hold: headHold({}) })), "head_injury");
  assert.equal(ftpGate(state({ allowed: "none", hold: headHold({}) })), "doctor");

  const copy = clearanceCopy("head_injury");
  assert.equal(
    copy.tick,
    "A doctor has checked me since I hit my head, and I've had no symptoms for at least 24 hours."
  );
  assert.equal(copy.tick, HEAD_LIFT_TEXT);
  assert.doesNotMatch(copy.tick, /hard training/);
  assert.deepEqual(copy.byOptions, HEAD_CHECKED_BY_OPTIONS);
  assert.ok(!copy.byOptions.some((o) => /physio|midwife/i.test(o)));
  assert.match(copy.note, /no racing or group riding before day 21/);

  // Every other doctor's hold keeps the usual form.
  const chest = holdView(hold({ level: "hold_all", source: "detector", red_flag: "chest_pain" }));
  assert.equal(chest.clearKind, "doctor");
  assert.equal(clearanceCopy().tick, CLEARANCE_TEXT);
  assert.equal(clearanceCopy("doctor").submit, "Confirm I'm cleared");
});

test("after the head injury check, easy riding ends by itself and racing waits for day 21", () => {
  const easy = hold({
    level: "easy_only",
    source: "detector",
    red_flag: "head_injury",
    lift_kind: "expires",
    expires_at: "2026-10-15T18:00:00",
  });
  const view = holdView(easy, "FR", "2026-10-22");
  assert.equal(view.title, "Easy riding only");
  assert.equal(
    view.text,
    "Easy riding only until 15 October while you build back after your head injury. No racing or group riding before 22 October, three weeks after your head injury. If a headache, dizziness or any other symptom comes back, stop riding and tell me. If it gets worse, call 112."
  );
  assert.equal(view.canClear || view.canSelfLift || view.canMistake, false);
  assert.equal(clearanceOffer(state({ allowed: "easy", hold: easy })), null);
  assert.equal(
    liftedMessage(state({ allowed: "easy", hold: easy, no_racing_or_group_until: "2026-10-22" })),
    "Thanks. Easy riding only until 15 October, then build back gradually. No racing or group riding before 22 October, three weeks after your head injury."
  );
  // Checked after two weeks: no easy hold left, but racing still waits.
  assert.equal(
    liftedMessage(state({ no_racing_or_group_until: "2026-10-22" })),
    "Thanks. The hold is lifted. No racing or group riding before 22 October, three weeks after your head injury."
  );
  assert.equal(noRacingLine(null), "");
});

test("no dashes or exclamation marks in the head injury and clearance copy", () => {
  const strings = [
    HEAD_LIFT_TEXT,
    noRacingLine("2026-10-22"),
    ...["doctor", "head_injury"].flatMap((k) => {
      const c = clearanceCopy(k);
      return [c.tick, c.unticked, c.byQuestion, c.note, c.submit];
    }),
    ...[
      headHold({}),
      hold({ red_flag: "head_injury", source: "detector", lift_kind: "expires", expires_at: "2026-10-15T10:00:00" }),
    ].flatMap((h) => {
      const v = holdView(h, "GB", "2026-10-22");
      return [v.title, v.text];
    }),
  ];
  for (const s of strings) {
    assert.doesNotMatch(s, /[\u2013\u2014!]/);
    assert.doesNotMatch(s, / - /);
    assert.doesNotMatch(s, /kicker/i);
  }
});

test("an account held as under 18 is never shown the health questions in settings", () => {
  // Re-verification new problem 3: the server refuses the answers with a 403,
  // so Settings, then Health must not offer the questions at all.
  const page = readFileSync(new URL("./HealthSettings.tsx", import.meta.url), "utf8");
  assert.match(page, /const minor = isMinorHold\(hold\);/);
  assert.match(page, /\{minor \? null : editing \?/);
  assert.match(page, /!screening && !minor &&/);
});

test("a mistake on a re-mention says the easy days run on", () => {
  // Reverify round 3, problem 1: the same fever or head injury mentioned
  // again during its easy days is its own hold. Calling it a mistake lifts
  // only that one, and the banner says what still stands.
  const easy = (red_flag) =>
    hold({ level: "easy_only", source: "detector", red_flag, lift_kind: "expires", expires_at: "2026-10-15T10:00:00" });
  assert.equal(
    mistakeLiftedMessage(state({ allowed: "easy", hold: easy("fever") })),
    "Hold lifted. Your easy week after the fever still runs until 15 October."
  );
  assert.equal(
    mistakeLiftedMessage(
      state({ allowed: "easy", hold: easy("head_injury"), no_racing_or_group_until: "2026-10-22" })
    ),
    "Hold lifted. Easy riding after your head injury still runs until 15 October. No racing or group riding before 22 October, three weeks after your head injury."
  );
  assert.equal(mistakeLiftedMessage(state({})), MISTAKE_LIFTED);
  assert.equal(mistakeLiftedMessage(state({ hold: hold({ source: "detector" }) })), STILL_HELD);
  for (const s of [
    mistakeLiftedMessage(state({ hold: easy("fever") })),
    mistakeLiftedMessage(state({ hold: easy("head_injury") })),
    mistakeLiftedMessage(state({ hold: easy("other") })),
  ]) {
    assert.doesNotMatch(s, /[\u2013\u2014!]/);
    assert.doesNotMatch(s, /^Thanks/);
  }
  const banner = readFileSync(new URL("./HoldBanner.tsx", import.meta.url), "utf8");
  assert.match(banner, /setDone\(mistakeLiftedMessage\(next\)\)/);
});
