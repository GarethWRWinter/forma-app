/**
 * A membership the rider has cancelled that hasn't ended yet.
 *
 * From Stripe's dahlia API versions a rider who cancels in the portal keeps
 * status "active" until the paid month ends, with the date in cancel_at, and
 * the server sends it as billing/status ends_at (found live on 13 Oct 2026).
 * Before, Settings said the month "runs to" that date, as if it would renew,
 * and riders worried they would be charged again.
 *
 * Nothing here depends on the safety system, so the same file ships with or
 * without it. No runtime imports: the tests run this file straight under
 * Node. Tests: node --test src/lib/membership-ending.test.mjs
 */

import type { BillingStatus } from "@/lib/api";

// The statuses that are still a membership (accountExits.ts keeps the same
// set; this file can't import it at runtime).
const MEMBER_STATUSES = new Set(["active", "trialing", "past_due"]);

/**
 * When a membership the rider has cancelled ends, as the server sent it
 * (billing/status ends_at), or null for one that renews and for no
 * membership. Stripe keeps a cancelled membership active to the end of what
 * was paid for, so the status alone still reads "active".
 *
 * The server sends ends_at only when no further payment is due before it.
 * A date after the current period end would mean one more renewal first,
 * so it is never read as an ending here: the card must not say "you won't
 * be charged again" to a rider who will be.
 */
export function membershipEndsAt(
  status:
    | (Pick<BillingStatus, "status"> & Partial<Pick<BillingStatus, "ends_at" | "period_end">>)
    | null
    | undefined
): string | null {
  if (!status || !MEMBER_STATUSES.has(status.status)) return null;
  const ends = status.ends_at || null;
  if (!ends) return null;
  if (status.period_end && Date.parse(ends) > Date.parse(status.period_end)) return null;
  return ends;
}

/** The Settings card's badge in place of "Active". `on` is the date as the
    screen shows it (formatDate). */
export function endingBadge(on: string): string {
  return `Ends ${on}`;
}

/** How to keep it: the second line on the Settings card. */
export const ENDING_KEEP =
  "Everything works as normal until then. To keep your membership, open Manage billing and press Renew before it ends.";

/**
 * What the Settings card says: it is cancelled, when it ends, that it won't
 * renew, and, unless a payment is overdue (Stripe may yet retry it), that no
 * more will be taken; then that everything works until then and how to keep
 * it.
 */
export function endingLines(on: string, opts: { pastDue: boolean }): string[] {
  const charge = opts.pastDue ? "" : ", so you won't be charged again";
  return [`Your membership is cancelled. It ends on ${on} and won't renew${charge}.`, ENDING_KEEP];
}
