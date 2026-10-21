// Run: node --test src/lib/membership-ending.test.mjs
// A membership the rider has cancelled that hasn't ended yet. From Stripe's
// dahlia API versions a rider who cancels in the portal keeps status
// "active" until the paid month ends, with the date in cancel_at, and the
// server sends it as billing/status ends_at (found live on 13 Oct 2026).
// Before, Settings said the month "runs to" that date, as if it would renew.
// Node strips the types itself.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { ENDING_KEEP, endingBadge, endingLines, membershipEndsAt } from "./membershipEnding.ts";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

const ENDS = "2026-11-13T12:00:00";
const ON = "13 Nov 2026";

test("a cancelled membership is still a membership, with an end date", () => {
  for (const status of ["active", "trialing", "past_due"]) {
    assert.equal(membershipEndsAt({ status, ends_at: ENDS }), ENDS, status);
    assert.equal(membershipEndsAt({ status, ends_at: ENDS, period_end: ENDS }), ENDS, status);
    assert.equal(membershipEndsAt({ status, ends_at: null }), null, status);
    assert.equal(membershipEndsAt({ status }), null, `${status}, an older server with no ends_at`);
  }
  // Ended, or never paid: nothing pending, whatever a stale field says.
  for (const status of ["none", "canceled", "unpaid"]) {
    assert.equal(membershipEndsAt({ status, ends_at: ENDS }), null, status);
  }
  assert.equal(membershipEndsAt(undefined), null);
  assert.equal(membershipEndsAt(null), null);
});

test("an end date before the period end is an ending; one after it is not", () => {
  // Stripe ends it early and credits the rest: still no further charge.
  const earlier = "2026-11-03T12:00:00";
  assert.equal(membershipEndsAt({ status: "active", ends_at: earlier, period_end: ENDS }), earlier);
  // After the period end, Stripe renews once more and charges for the days
  // up to it. The server never sends that as ends_at; if one ever did, the
  // card must not promise "you won't be charged again".
  const later = "2026-12-13T12:00:00";
  assert.equal(membershipEndsAt({ status: "active", ends_at: later, period_end: ENDS }), null);
});

test("Settings says it is cancelled, when it ends, that it won't renew, and how to keep it", () => {
  assert.equal(endingBadge(ON), "Ends 13 Nov 2026");
  assert.deepEqual(endingLines(ON, { pastDue: false }), [
    "Your membership is cancelled. It ends on 13 Nov 2026 and won't renew, so you won't be charged again.",
    "Everything works as normal until then. To keep your membership, open Manage billing and press Renew before it ends.",
  ]);
  assert.equal(endingLines(ON, { pastDue: false })[1], ENDING_KEEP);
  // A payment still overdue may yet be retried: no promise about charges.
  const pastDue = endingLines(ON, { pastDue: true });
  assert.equal(pastDue[0], "Your membership is cancelled. It ends on 13 Nov 2026 and won't renew.");
  assert.doesNotMatch(pastDue.join(" "), /charged/);
});

test("the words follow the house rules", () => {
  const dash = new RegExp(`[${String.fromCharCode(0x2013, 0x2014)}]`);
  const lines = [endingBadge(ON), ...endingLines(ON, { pastDue: false }), ...endingLines(ON, { pastDue: true })];
  for (const line of lines) {
    assert.ok(!dash.test(line), line);
    assert.ok(!line.includes("!"), line);
    assert.ok(!/\b(color|center|organize|license|canceled)\b/i.test(line), line);
  }
  for (const file of [
    "./membershipEnding.ts",
    "./membership-ending.test.mjs",
    "../components/settings/membership-card.tsx",
    "../components/settings/membership-card-render.test.mjs",
  ]) {
    assert.ok(!dash.test(read(file)), `${file} has a dash`);
  }
});

test("the Settings card reads the ending from here", () => {
  const card = read("../components/settings/membership-card.tsx");
  assert.match(card, /from "@\/lib\/membershipEnding"/);
  assert.match(card, /const endsAt = membershipEndsAt\(status\);/);
  // The renewing line only when nothing is ending.
  assert.match(card, /Current month runs to \{formatDate\(status\.period_end\)\}/);
});
