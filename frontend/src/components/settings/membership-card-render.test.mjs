// Run: node --test src/components/settings/membership-card-render.test.mjs
// The Settings membership card rendered to HTML (react-dom/server) for each
// state a member can be in, and checked for what they would read. A rider
// who cancelled in Stripe's portal keeps status "active" until the paid
// month ends (found live on 13 Oct 2026); the card used to say the month
// "runs to" that date, as if it would renew.
//
// The TSX is compiled with the SWC that ships with Next, through a loader
// registered below, so nothing new is installed. The react-query cache is
// seeded with what the server would answer.
import { test } from "node:test";
import assert from "node:assert/strict";
import { register } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
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

function file(base) {
  for (const f of [base, base + ".ts", base + ".tsx", path.join(base, "index.ts"), path.join(base, "index.tsx")]) {
    if (existsSync(f) && statSync(f).isFile()) return pathToFileURL(f).href;
  }
  return null;
}

export async function resolve(specifier, context, next) {
  const parent = context.parentURL || "";
  if (specifier.startsWith("@/")) {
    const url = file(path.join(SRC, specifier.slice(2)));
    if (url) return { url, shortCircuit: true };
  }
  if (/^\\.\\.?\\//.test(specifier) && parent.startsWith("file:") && fileURLToPath(parent).startsWith(SRC)) {
    const url = file(path.resolve(path.dirname(fileURLToPath(parent)), specifier));
    if (url) return { url, shortCircuit: true };
  }
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
const { MembershipCard } = await import("@/components/settings/membership-card");
const ends = await import("@/lib/membershipEnding");
const { formatDate } = await import("@/lib/utils");

/** The server's HTML with the entities and React's text markers undone. */
const clean = (html) =>
  html
    .replace(/<!-- -->/g, "")
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, "&");

/** Visible text only. */
const words = (html) => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");

function renderCard(billing) {
  const client = new QueryClient();
  client.setQueryData(["billing-status"], billing);
  return clean(renderToString(h(QueryClientProvider, { client }, h(MembershipCard))));
}

function shows(html, texts, label) {
  for (const t of texts) assert.ok(html.includes(t), `${label}: missing ${JSON.stringify(t)}`);
}
function hides(html, texts, label) {
  for (const t of texts) assert.ok(!html.includes(t), `${label}: unexpected ${JSON.stringify(t)}`);
}

const member = { configured: true, required: true, status: "active", period_end: null, ends_at: null, has_access: true };
// Cancelled in Stripe's portal, paid to 13 November: Stripe still says active.
const ENDS = "2026-11-13T12:00:00";
const ending = { ...member, period_end: ENDS, ends_at: ENDS };

test("a renewing member sees Active and when the month runs to", () => {
  const html = renderCard({ ...member, period_end: ENDS });
  shows(html, ["Membership", "Active", `Current month runs to ${formatDate(ENDS)}`, "Manage billing"], "renewing");
  hides(html, ["cancelled", "won't renew", "Ends "], "renewing");
});

test("a member who cancelled is told it ends, won't renew, and how to keep it", () => {
  const html = renderCard(ending);
  const on = formatDate(ENDS);
  shows(html, [ends.endingBadge(on), ...ends.endingLines(on, { pastDue: false }), "Manage billing"], "ending");
  shows(html, ["13 Nov 2026"], "the date, as a rider reads it");
  // Not the renewing line, and not the bare Active badge beside "cancelled".
  hides(html, ["Current month runs to", "Active"], "ending");
  // The join panel is for riders with no membership.
  hides(html, ["Join Forma"], "ending");
});

test("an overdue payment on a cancelled membership makes no promise about charges", () => {
  const html = renderCard({ ...ending, status: "past_due" });
  const on = formatDate(ENDS);
  shows(html, ["didn't go through", ...ends.endingLines(on, { pastDue: true })], "past due");
  hides(html, ["won't be charged again", "Current month runs to"], "past due");
});

test("a cancel date after the paid month is never read as no more charges", () => {
  // Stripe renews once more first. The server doesn't send this as ends_at;
  // if it ever did, the card keeps to what it knows: it renews.
  const html = renderCard({ ...member, period_end: ENDS, ends_at: "2026-12-13T12:00:00" });
  shows(html, ["Active", `Current month runs to ${formatDate(ENDS)}`], "later");
  hides(html, ["won't be charged again", "won't renew", "cancelled", "Ends "], "later");
});

test("an older server with no ends_at still shows the renewing card", () => {
  const { ends_at, ...older } = { ...member, period_end: ENDS };
  const html = renderCard(older);
  shows(html, ["Active", `Current month runs to ${formatDate(ENDS)}`], "older server");
});

test("what the card shows has no dashes and no exclamation marks", () => {
  const dash = new RegExp(`[${String.fromCharCode(0x2013, 0x2014)}]`);
  for (const billing of [ending, { ...ending, status: "past_due" }, { ...member, period_end: ENDS }]) {
    const t = words(renderCard(billing));
    assert.ok(!dash.test(t), t.match(new RegExp(`.{0,30}${dash.source}.{0,30}`))?.[0]);
    assert.ok(!t.includes("!"), t);
  }
});
