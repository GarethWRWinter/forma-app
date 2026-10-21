// Run: node --test src/components/coach/coach-thread.test.mjs
// Node strips the types from the .ts files itself, so no build step.
//
// Live round 1: a safety card can now reach the chat at any point in the
// stream (first, mid-reply or just before done), and a stored reply carries
// its card ("safety_card", {kind, text}). The card must sit at the top of
// that reply whenever it arrives and come back on reload; the hold banner
// must refresh after every reply.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  NO_REPLY_YET,
  foldReplyChunk,
  fromStoredMessage,
  keepStreamedDetail,
  safetyCardFrom,
  showReply,
  storedSafetyCards,
  updateReply,
  withSafetyCard,
} from "./coach-thread.ts";
import { readChatStream } from "../../lib/api.ts";

const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");

const CHEST = { kind: "emergency", text: "Stop riding. Call 999 now if the pain is still there." };
const HEAD = { kind: "emergency", text: "A head injury with these signs needs A&E today." };
const CRISIS = { kind: "crisis", text: "You don't have to carry this on your own." };

/** The page's loop, as page.tsx runs it: fold each chunk into the reply,
    then show the reply on the thread. */
function streamInto(thread, replyKey, chunks) {
  let reply = NO_REPLY_YET;
  for (const chunk of chunks) {
    if (chunk.type === "safety" || chunk.type === "text") {
      reply = foldReplyChunk(reply, chunk);
      thread = showReply(thread, replyKey, reply);
    }
  }
  return { thread, reply };
}

function startThread(key = "reply-1") {
  return [
    { role: "user", content: "earlier", id: "m1" },
    { role: "assistant", content: "earlier reply", id: "m2" },
    { role: "user", content: "My chest went tight on the climb." },
    { role: "assistant", content: "", replyKey: key },
  ];
}

const text = (content) => ({ type: "text", content });
const card = (c) => ({ type: "safety", ...c, card: "chest" });
const DONE = { type: "done" };

// === Which cards the chat shows ===

test("emergency and crisis cards are shown; warnings, blanks and junk are not", () => {
  assert.deepEqual(safetyCardFrom({ type: "safety", ...CHEST, card: "chest" }), CHEST);
  assert.deepEqual(safetyCardFrom(CRISIS), CRISIS);
  assert.equal(safetyCardFrom({ kind: "warning", text: "Fever: no riding." }), null);
  assert.equal(safetyCardFrom({ kind: "emergency", text: "   " }), null);
  assert.equal(safetyCardFrom({ kind: "emergency" }), null);
  assert.equal(safetyCardFrom({ kind: "alarm", text: "x" }), null);
  assert.equal(safetyCardFrom(null), null);
  assert.equal(safetyCardFrom("emergency"), null);
});

test("a card shows once, and emergency sits above crisis whatever the arrival order", () => {
  let cards = withSafetyCard(undefined, CRISIS);
  cards = withSafetyCard(cards, CHEST);
  cards = withSafetyCard(cards, CRISIS);
  assert.deepEqual(cards, [CHEST, CRISIS]);
  // Two emergency cards keep the order they came in.
  cards = withSafetyCard(cards, HEAD);
  assert.deepEqual(cards, [CHEST, HEAD, CRISIS]);
});

// === A card at any point in the stream ===

for (const [where, chunks] of [
  ["first, before any text", [card(CHEST), text("I'm stopping "), text("today's session."), DONE]],
  ["part way through the text", [text("I'm stopping "), card(CHEST), text("today's session."), DONE]],
  ["just before done", [text("I'm stopping "), text("today's session."), card(CHEST), DONE]],
]) {
  test(`a card that arrives ${where} sits at the top of that reply`, () => {
    const { thread } = streamInto(startThread(), "reply-1", chunks);
    assert.equal(thread.length, 4, "no extra message for the card");
    const reply = thread[3];
    assert.equal(reply.role, "assistant");
    assert.equal(reply.content, "I'm stopping today's session.");
    assert.deepEqual(reply.safety, [CHEST]);
    // The earlier messages and the rider's own are untouched.
    assert.deepEqual(thread.slice(0, 3), startThread().slice(0, 3));
  });
}

test("an emergency card first and a crisis card late both show, emergency on top", () => {
  const { thread } = streamInto(startThread(), "reply-1", [
    card(CRISIS),
    text("Words."),
    card(CHEST),
    card(CHEST),
    DONE,
  ]);
  assert.deepEqual(thread[3].safety, [CHEST, CRISIS]);
});

test("a card on a reply with no words still shows", () => {
  const { thread } = streamInto(startThread(), "reply-1", [card(CHEST), DONE]);
  assert.equal(thread[3].content, "");
  assert.deepEqual(thread[3].safety, [CHEST]);
});

test("a warning card leaves the reply as it was", () => {
  const { thread, reply } = streamInto(startThread(), "reply-1", [
    { type: "safety", kind: "warning", text: "No riding with a fever." },
    text("Rest today."),
  ]);
  assert.equal(thread[3].safety, undefined);
  assert.deepEqual(reply.safety, []);
});

test("the card goes to the streamed reply, not the last message, when the thread moved", () => {
  // Older history paged in above, and a message appended below (any change
  // that used to make 'the last message' the wrong one).
  let thread = startThread();
  thread = [...thread, { role: "assistant", content: "unrelated", id: "x" }];
  const { thread: after } = streamInto(thread, "reply-1", [text("Stop."), card(CHEST)]);
  assert.deepEqual(after[3].safety, [CHEST]);
  assert.equal(after[3].content, "Stop.");
  assert.equal(after[4].safety, undefined);
});

test("a reply the thread lost (history replaced it mid-stream) comes back whole, card on top", () => {
  let reply = NO_REPLY_YET;
  let thread = startThread();
  for (const chunk of [card(CHEST), text("First half. ")]) {
    reply = foldReplyChunk(reply, chunk);
    thread = showReply(thread, "reply-1", reply);
  }
  // A stale copy of the chat lands and replaces the thread.
  thread = [{ role: "user", content: "My chest went tight on the climb.", id: "u9" }];
  reply = foldReplyChunk(reply, text("Second half."));
  thread = showReply(thread, "reply-1", reply);
  assert.equal(thread.length, 2);
  assert.equal(thread[1].content, "First half. Second half.");
  assert.deepEqual(thread[1].safety, [CHEST]);
});

test("showing the same reply twice changes nothing (safe under React's double calls)", () => {
  const reply = foldReplyChunk(foldReplyChunk(NO_REPLY_YET, card(CHEST)), text("Stop."));
  const once = showReply(startThread(), "reply-1", reply);
  const twice = showReply(once, "reply-1", reply);
  assert.deepEqual(twice, once);
});

test("a failed reply keeps its card on top of the failure line", () => {
  let reply = foldReplyChunk(NO_REPLY_YET, card(CHEST));
  reply = foldReplyChunk(reply, text("Partial"));
  const thread = showReply(startThread(), "reply-1", {
    ...reply,
    content: "I lost the connection there. Send that again.",
  });
  assert.deepEqual(thread[3].safety, [CHEST]);
  assert.equal(thread[3].content, "I lost the connection there. Send that again.");
});

test("updateReply starts the reply when the key is missing, and edits it in place otherwise", () => {
  const added = updateReply([], "k", (m) => ({ ...m, content: "x" }));
  assert.deepEqual(added, [{ role: "assistant", content: "x", replyKey: "k" }]);
  const edited = updateReply(added, "k", (m) => ({ ...m, content: "y" }));
  assert.equal(edited.length, 1);
  assert.equal(edited[0].content, "y");
});

// === Reload from history ===

test("a stored reply's safety_card comes back, as one card or a list", () => {
  assert.deepEqual(storedSafetyCards({ safety_card: CHEST }), [CHEST]);
  assert.deepEqual(storedSafetyCards({ safety_card: [CRISIS, CHEST] }), [CHEST, CRISIS]);
  assert.deepEqual(storedSafetyCards({ safety_card: { kind: "warning", text: "x" } }), []);
  assert.deepEqual(storedSafetyCards({ safety_card: null }), []);
  assert.deepEqual(storedSafetyCards({}), []);
  assert.deepEqual(storedSafetyCards({ safety_cards: [CHEST] }), [CHEST]);
});

test("fromStoredMessage puts the stored card on the reply, and only on replies", () => {
  const reply = fromStoredMessage({
    id: "a1",
    role: "assistant",
    content: "Call 999.",
    created_at: "2026-10-09T10:00:00",
    safety_card: { ...CHEST, card: "chest" },
  });
  assert.deepEqual(reply, {
    role: "assistant",
    content: "Call 999.",
    id: "a1",
    created_at: "2026-10-09T10:00:00",
    safety: [CHEST],
  });
  const rider = fromStoredMessage({
    id: "u1",
    role: "user",
    content: "help",
    created_at: "2026-10-09T10:00:00",
    safety_card: CHEST,
  });
  assert.equal("safety" in rider, false);
  const plain = fromStoredMessage({ id: "a2", role: "assistant", content: "Fine.", created_at: "" });
  assert.equal("safety" in plain, false);
});

test("a stored copy without the card keeps the card the stream showed on the same reply", () => {
  const shown = [
    { role: "user", content: "My chest went tight.", attachments: ["ride.fit"] },
    { role: "assistant", content: "Stop  riding.\nCall 999.", safety: [CHEST], replyKey: "reply-1" },
  ];
  const stored = [
    { role: "user", content: "My chest went tight.", id: "u1" },
    { role: "assistant", content: "Stop riding. Call 999.", id: "a1" },
  ];
  const merged = keepStreamedDetail(stored, shown);
  assert.deepEqual(merged[1].safety, [CHEST], "matched by words, whitespace aside");
  assert.equal(merged[1].id, "a1", "the stored copy is the one kept");
  assert.deepEqual(merged[0].attachments, ["ride.fit"], "the filenames stay too");
  assert.equal(merged[0].safety, undefined, "a card never lands on the rider's message");
});

test("the server's stored card wins, ids match ids, and different words get no card", () => {
  const shown = [
    { role: "assistant", content: "Same words.", safety: [CRISIS] },
    { role: "assistant", content: "Old reply", id: "a7", safety: [HEAD] },
  ];
  const stored = [
    { role: "assistant", content: "Same words.", id: "a1", safety: [CHEST] },
    { role: "assistant", content: "Old reply, edited", id: "a7" },
    { role: "assistant", content: "Something else.", id: "a8" },
  ];
  const merged = keepStreamedDetail(stored, shown);
  assert.deepEqual(merged[0].safety, [CHEST]);
  assert.deepEqual(merged[1].safety, [HEAD]);
  assert.equal(merged[2].safety, undefined);
});

test("with nothing to keep, the stored copy comes back as it is", () => {
  const stored = [{ role: "assistant", content: "x", id: "a1" }];
  assert.equal(keepStreamedDetail(stored, [{ role: "assistant", content: "x" }]), stored);
});

// === The stream itself ===

function streamOf(parts) {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const p of parts) controller.enqueue(typeof p === "string" ? enc.encode(p) : p);
      controller.close();
    },
  });
}

async function collect(stream) {
  const out = [];
  for await (const chunk of readChatStream(stream)) out.push(chunk);
  return out;
}

test("readChatStream rebuilds lines split across reads, multi-byte characters included", async () => {
  const card = JSON.stringify({ type: "safety", kind: "crisis", text: "You're not alone. Samaritans: 116 123, free, costs €0." });
  const whole = `data: ${JSON.stringify({ type: "text", content: "Hello " })}\n\ndata: ${card}\n\ndata: {"type":"done"}\n\n`;
  const bytes = new TextEncoder().encode(whole);
  // Split every 7 bytes, which cuts through the three-byte euro sign too.
  const parts = [];
  for (let i = 0; i < bytes.length; i += 7) parts.push(bytes.slice(i, i + 7));
  const chunks = await collect(streamOf(parts));
  assert.deepEqual(
    chunks.map((c) => c.type),
    ["text", "safety", "done"]
  );
  assert.equal(chunks[1].text, "You're not alone. Samaritans: 116 123, free, costs €0.");
});

test("a card just before done, with the stream closing on no newline, still arrives", async () => {
  const chunks = await collect(
    streamOf([
      'data: {"type":"text","content":"Stop."}\n\n',
      `data: ${JSON.stringify({ type: "safety", ...CHEST, card: "chest" })}\n\n`,
      'data: {"type":"done"}',
    ])
  );
  assert.deepEqual(chunks.map((c) => c.type), ["text", "safety", "done"]);

  const lastIsCard = await collect(
    streamOf(['data: {"type":"text","content":"Stop."}\n\n', `data: ${JSON.stringify({ type: "safety", ...CHEST })}`])
  );
  assert.equal(lastIsCard.at(-1).type, "safety");
});

test("readChatStream reads CRLF lines and data: without the space, and skips junk", async () => {
  const chunks = await collect(
    streamOf([
      ": keep-alive\r\n\r\n",
      'data:{"type":"text","content":"a"}\r\n\r\n',
      "data: not json\n\n",
      "data: 42\n\n",
      "event: ping\n\n",
      `data: ${JSON.stringify({ type: "safety", ...CRISIS })}\r\n\r\n`,
    ])
  );
  assert.deepEqual(chunks.map((c) => c.type), ["text", "safety"]);
  assert.equal(chunks[1].text, CRISIS.text);
});

test("end to end: the real stream reader into the thread, card just before done", async () => {
  const body = streamOf([
    'data: {"type":"status","content":"Checking your week"}\n\n',
    'data: {"type":"text","content":"I\'ve cleared today. "}\n\n',
    'data: {"type":"plan_updated"}\n\n',
    'data: {"type":"text","content":"Get seen today."}\n\n',
    `data: ${JSON.stringify({ type: "safety", ...CHEST, card: "chest" })}\n\n`,
    'data: {"type":"done"}\n\n',
  ]);
  const chunks = await collect(body);
  const { thread } = streamInto(startThread(), "reply-1", chunks);
  assert.equal(thread[3].content, "I've cleared today. Get seen today.");
  assert.deepEqual(thread[3].safety, [CHEST]);
});

// === The page is wired to all of this ===

test("the coach page streams both send paths through one reply loop", () => {
  const page = read("../../app/dashboard/coach/page.tsx");
  assert.equal((page.match(/await streamReply\(/g) || []).length, 2, "text and voice both use it");
  assert.match(page, /chat\.sendMessage\(sessionId, userMessage, handedOver\.ids, refreshChatList\)/);
  assert.match(page, /chat\.sendVoiceMessage\(sessionId, transcript\.trim\(\), handedOver\.ids, refreshChatList\)/);
  // The old 'last message' writes are gone: they put a card on the wrong
  // message (or nowhere) once the thread moved.
  assert.doesNotMatch(page, /updated\[updated\.length - 1\]/);
  assert.doesNotMatch(page, /safetyFrom\(/);
  // Both chunk kinds go through the fold, then show on the keyed reply.
  assert.equal((page.match(/reply = foldReplyChunk\(reply, chunk\);\s*show\(reply\);/g) || []).length, 2);
  assert.match(page, /showReply\(prev, replyKey, shown\)/);
});

test("the hold banner refreshes after every reply, failed ones included", () => {
  const page = read("../../app/dashboard/coach/page.tsx");
  const loop = page.slice(page.indexOf("const streamReply = async"), page.indexOf("// Voice send handler"));
  assert.ok(loop.length > 0, "streamReply is defined before the voice handler");
  const finallyBlock = loop.slice(loop.indexOf("} finally {"));
  assert.match(finallyBlock, /invalidateQueries\(\{ queryKey: SAFETY_STATE_KEY \}\)/);
  assert.match(finallyBlock, /streamingRef\.current = false/);
  // And at once when a card lands, since the hold may already be open.
  const onCard = loop.slice(loop.indexOf('chunk.type === "safety"'), loop.indexOf('chunk.type === "status"'));
  assert.match(onCard, /invalidateQueries\(\{ queryKey: SAFETY_STATE_KEY \}\)/);
  // The key is the one the banner reads.
  assert.match(page, /import \{ SAFETY_STATE_KEY \} from "@\/components\/safety\/useSafetyState"/);
  const hook = read("../safety/useSafetyState.ts");
  assert.match(hook, /export const SAFETY_STATE_KEY = \["safety-state"\] as const/);
  assert.match(read("../safety/HoldBanner.tsx"), /useSafetyState\(/);
});

test("history loads and pages carry the stored cards, and a mid-reply copy is held back", () => {
  const page = read("../../app/dashboard/coach/page.tsx");
  assert.match(page, /data\.messages\.map\(fromStoredMessage\)/);
  assert.match(page, /older\.messages\.map\(fromStoredMessage\)/);
  assert.match(page, /keepStreamedDetail\(stored, prev\)/);
  assert.match(
    page,
    /if \(streamingRef\.current && sessionData\.id === threadSessionRef\.current\) \{\s*heldHistoryRef\.current = sessionData\.id;\s*return;/
  );
});

test("the cards render at the top of the reply, above its bubble, and on a wordless reply", () => {
  const page = read("../../app/dashboard/coach/page.tsx");
  const cards = page.indexOf("<ReplySafetyCards cards={msg.safety}");
  const bubble = page.indexOf("{!emptyReply && (");
  assert.ok(cards > 0 && bubble > cards, "cards come before the bubble");
  assert.match(page, /if \(emptyReply && !msg\.safety\?\.length\) \{\s*return null;/);
});

test("api.ts types the stored card and reads both streams through readChatStream", () => {
  const api = read("../../lib/api.ts");
  assert.match(api, /safety_card\?: ChatSafetyCard \| ChatSafetyCard\[\] \| null;/);
  assert.equal((api.match(/yield\* readChatStream\(response\.body, onClosed\)/g) || []).length, 2);
});
