// Run: node --test src/components/coach/reply-safety-cards-render.test.mjs
// Live round 1: the cards at the top of a coach reply, rendered rather than
// read. coach-thread.test.mjs checks where a card lands in the thread; this
// renders the real ReplySafetyCards (and the real SafetyCard inside it) to
// HTML and checks what the rider sees: the emergency style above the crisis
// style, the right numbers one tap away, nothing at all without a card.
//
// The TSX is compiled with the SWC that ships with Next, through a loader
// registered below (as in src/lib/account-exits-render.test.mjs), so nothing
// new is installed.
import { test } from "node:test";
import assert from "node:assert/strict";
import { register } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
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
const { ReplySafetyCards } = await import("@/components/coach/reply-safety-cards");
const { storedSafetyCards } = await import("@/components/coach/coach-thread");

const CHEST = { kind: "emergency", text: "Stop riding now and call 999 if the pain is still there." };
const CRISIS = { kind: "crisis", text: "You don't have to carry this on your own. Talk to someone today." };

function render(cards, country = "GB") {
  return renderToString(h(ReplySafetyCards, { cards, country }))
    .replace(/<!-- -->/g, "")
    .replace(/&#x27;/g, "'")
    .replace(/&amp;/g, "&");
}

test("no card, no markup", () => {
  assert.equal(render(undefined), "");
  assert.equal(render([]), "");
});

test("a stored reply's cards render emergency first, each its own alert", () => {
  // Stored crisis-first: the page still shows the emergency card on top.
  const html = render(storedSafetyCards({ safety_card: [CRISIS, CHEST] }));
  assert.equal((html.match(/role="alert"/g) || []).length, 2);
  const urgent = html.indexOf("Get help now");
  const talk = html.indexOf("Please talk to someone");
  assert.ok(urgent >= 0 && talk > urgent, "emergency above crisis");
  assert.ok(html.indexOf(CHEST.text) < html.indexOf(CRISIS.text));
  // The emergency style is the loud one: the red card.
  const emergencyCard = html.slice(0, html.indexOf(CRISIS.text));
  assert.match(emergencyCard, /bg-vb-red text-white/);
});

test("the numbers are one tap away: 999 on both, Samaritans on the crisis card in the UK", () => {
  const html = render([CHEST, CRISIS]);
  assert.equal((html.match(/href="tel:999"/g) || []).length, 2);
  assert.equal((html.match(/href="tel:116123"/g) || []).length, 1);
  assert.match(html, /Call 999/);
});

test("the emergency number follows the rider's country", () => {
  assert.match(render([CHEST], "US"), /href="tel:911"/);
  assert.match(render([CHEST], "DE"), /href="tel:112"/);
  assert.doesNotMatch(render([CRISIS], "DE"), /tel:116123/);
});

test("the card's words render exactly as the server sent them", () => {
  const html = render([CHEST]);
  assert.ok(html.includes(CHEST.text));
});
