/**
 * The coach thread on the chat page: the messages, the reply being streamed,
 * and the emergency and crisis cards that sit at the top of a reply.
 *
 * A card is fixed text from the server, never the model's. It can reach the
 * page at any point in a reply's stream (first, part way through the text,
 * or just before "done") and it comes back with the stored reply when the
 * thread reloads. Wherever it comes from, it sits at the top of the reply it
 * belongs to, and it is never lost: a card for a reply the thread no longer
 * has starts that reply again rather than vanishing.
 *
 * Pure, with no imports at run time, so node can test it as it is
 * (coach-thread.test.mjs).
 */
import type { SafetyCardKind } from "@/components/safety/SafetyCard";
import type { ChatMessage, ChatStreamChunk } from "@/lib/api";

export interface ThreadSafetyCard {
  kind: SafetyCardKind;
  text: string;
}

export interface ThreadMessage {
  role: "user" | "assistant";
  content: string;
  id?: string;
  created_at?: string;
  /** filenames handed over with this message, so the thread shows the swap */
  attachments?: string[];
  /** the same files' ids, in the same order, so each file card sits under it */
  attachmentIds?: string[];
  /** the emergency or crisis cards at the top of this reply, emergency first */
  safety?: ThreadSafetyCard[];
  /** marks the reply this page is streaming, so every chunk finds it */
  replyKey?: string;
}

// Emergency above crisis; otherwise the order they arrived in.
const CARD_RANK: Record<SafetyCardKind, number> = { emergency: 0, crisis: 1 };

/** A card the chat page shows, or null: a malformed chunk, an empty text, or
    a "warning" card (its lines travel in the reply itself). */
export function safetyCardFrom(raw: unknown): ThreadSafetyCard | null {
  if (!raw || typeof raw !== "object") return null;
  const { kind, text } = raw as { kind?: unknown; text?: unknown };
  if (kind !== "emergency" && kind !== "crisis") return null;
  if (typeof text !== "string" || !text.trim()) return null;
  return { kind, text };
}

/** The cards with one more: the same card twice shows once, and an emergency
    card always sits above a crisis card. */
export function withSafetyCard(
  cards: readonly ThreadSafetyCard[] | undefined,
  card: ThreadSafetyCard
): ThreadSafetyCard[] {
  const list = cards ? [...cards] : [];
  if (list.some((c) => c.kind === card.kind && c.text === card.text)) return list;
  // Array sort is stable, so cards of one kind keep their arrival order.
  return [...list, card].sort((a, b) => CARD_RANK[a.kind] - CARD_RANK[b.kind]);
}

/** Every card a stored message carries, as one card or a list. */
export function storedSafetyCards(message: {
  safety_card?: unknown;
  safety_cards?: unknown;
}): ThreadSafetyCard[] {
  const raw = [message.safety_card, message.safety_cards].flatMap((v) =>
    Array.isArray(v) ? v : [v]
  );
  return raw.reduce<ThreadSafetyCard[]>((cards, r) => {
    const card = safetyCardFrom(r);
    return card ? withSafetyCard(cards, card) : cards;
  }, []);
}

/** A message from the history endpoint as the thread holds it, cards
    included (the safety cards on a reply, the ids of the files a rider
    message carried), so a reload shows the same thread the rider saw. */
export function fromStoredMessage(m: ChatMessage): ThreadMessage {
  const cards = m.role === "assistant" ? storedSafetyCards(m) : [];
  const files = m.role === "user" ? m.attachment_ids ?? [] : [];
  return {
    role: m.role,
    content: m.content,
    id: m.id,
    created_at: m.created_at,
    ...(cards.length ? { safety: cards } : {}),
    ...(files.length ? { attachmentIds: files } : {}),
  };
}

/** The thread with the streamed reply changed. A reply the thread no longer
    holds (a history fetch replaced it) is started again at the end, so what
    the stream still has to say is never dropped. */
export function updateReply<M extends ThreadMessage>(
  messages: M[],
  replyKey: string,
  change: (reply: M) => M
): M[] {
  const at = messages.findIndex((m) => m.replyKey === replyKey);
  if (at < 0) {
    return [...messages, change({ role: "assistant", content: "", replyKey } as M)];
  }
  const next = [...messages];
  next[at] = change(next[at]);
  return next;
}

/** A streamed reply as it stands: the words so far and the cards so far. */
export interface StreamedReply {
  content: string;
  safety: ThreadSafetyCard[];
}

export const NO_REPLY_YET: StreamedReply = { content: "", safety: [] };

/** The reply after one stream chunk: a card joins the cards whenever it
    arrives (first, mid-reply or just before done), text joins the words, and
    anything else leaves the reply as it was. */
export function foldReplyChunk(reply: StreamedReply, chunk: ChatStreamChunk): StreamedReply {
  if (chunk.type === "safety") {
    const card = safetyCardFrom(chunk);
    return card ? { ...reply, safety: withSafetyCard(reply.safety, card) } : reply;
  }
  if (chunk.type === "text" && typeof chunk.content === "string") {
    return { ...reply, content: reply.content + chunk.content };
  }
  return reply;
}

/** The thread showing the streamed reply as it stands: its words, and its
    cards at the top. It sets rather than appends, so showing the same reply
    twice changes nothing, and a reply the thread lost comes back whole. */
export function showReply<M extends ThreadMessage>(
  messages: M[],
  replyKey: string,
  reply: StreamedReply
): M[] {
  return updateReply(messages, replyKey, (m) => {
    const safety = reply.safety.reduce(withSafetyCard, m.safety ?? []);
    return {
      ...m,
      role: "assistant",
      content: reply.content,
      ...(safety.length ? { safety } : {}),
    };
  });
}

const plain = (s: string) => s.replace(/\s+/g, " ").trim();

/** Stored history for the thread on screen, keeping what the page showed
    that the stored copy doesn't carry: the cards the stream delivered on a
    reply, and the filenames (and file ids, where the stored copy has none)
    on a rider's message. A message is the same one by id, or by role and
    words when the page's copy has no id yet. What the server stored always
    wins. */
export function keepStreamedDetail<M extends ThreadMessage>(
  stored: M[],
  shown: readonly ThreadMessage[]
): M[] {
  const extra = shown.filter((m) =>
    m.role === "assistant" ? !!m.safety?.length : !!m.attachments?.length
  );
  if (!extra.length) return stored;
  const used = new Set<number>();
  return stored.map((m) => {
    const missing = m.role === "assistant" ? !m.safety?.length : !m.attachments?.length;
    if (!missing) return m;
    const at = extra.findIndex(
      (c, i) =>
        !used.has(i) &&
        c.role === m.role &&
        ((!!c.id && c.id === m.id) ||
          (!c.id && !!plain(c.content) && plain(c.content) === plain(m.content)))
    );
    if (at < 0) return m;
    used.add(at);
    const found = extra[at];
    return m.role === "assistant"
      ? { ...m, safety: found.safety }
      : {
          ...m,
          attachments: found.attachments,
          attachmentIds: m.attachmentIds?.length ? m.attachmentIds : found.attachmentIds,
        };
  });
}

// === The debrief opening ===

/** The parts of a goal the debrief opening reads. */
export interface DebriefGoal {
  event_name: string;
  status: string;
  finish_time_seconds: number | null;
  overall_satisfaction: number | null;
  perceived_exertion: number | null;
  assessment_data: Record<string, unknown> | null;
}

// The race report's reasons (goals/[id]/assess), in the rider's own voice.
const DNF_WHY: Record<string, string> = {
  mechanical: "The bike let me down.",
  crash: "I crashed.",
  illness: "I wasn’t well on the day.",
  fuelling: "Cramps, fuelling or my stomach stopped me.",
  body: "My body said no.",
  head: "My head said no.",
  time_cut: "The time cut got me.",
  weather: "The weather or the course stopped me.",
};
const DNS_WHY: Record<string, string> = {
  illness: "I was ill.",
  injury: "I was injured.",
  life: "Life needed me elsewhere.",
  motivation: "When it came to it, I didn’t want to.",
  logistics: "Travel, entry or logistics got in the way.",
  weather: "The weather got in the way, or the event was cancelled.",
  wrong_goal: "I realised it wasn’t the right goal.",
};
const RIGHT_CALL: Record<string, string> = {
  yes: "Stopping was the right call.",
  probably: "Stopping was probably the right call.",
  no: "I could have carried on.",
  no_choice: "There was no choice.",
};

/** A free-text answer without its closing stop, or null when blank. */
function answer(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const text = value.trim().replace(/[.!?…\s]+$/, "");
  return text || null;
}

/**
 * What the rider sends to open the debrief, filled in from their race report.
 * A DNF or DNS never says they completed it, and a DNS carries no ratings:
 * its one slider is how they feel about it, not how the race went.
 */
export function debriefOpening(goal: DebriefGoal): string {
  const ad = goal.assessment_data ?? {};
  const parts: string[] = [];
  const add = (label: string, key: string) => {
    const text = answer(ad[key]);
    if (text) parts.push(`${label}: ${text}.`);
  };

  if (goal.status === "dnf") {
    parts.push(`I didn’t finish ${goal.event_name}.`);
    add("Where it ended", "dnf_where");
    const why = DNF_WHY[String(ad.dnf_reason ?? "")];
    if (why) parts.push(why);
    const call = RIGHT_CALL[String(ad.dnf_right_call ?? "")];
    if (call) parts.push(call);
    if (goal.overall_satisfaction) {
      parts.push(`How I feel about stopping: ${goal.overall_satisfaction}/10.`);
    }
    add("What was going right before it stopped", "went_well");
    add("What I’d change", "to_improve");
    parts.push("Can you help me talk it through?");
    return parts.join(" ");
  }

  if (goal.status === "dns") {
    parts.push(`I didn’t start ${goal.event_name}.`);
    const why = DNS_WHY[String(ad.dns_reason ?? "")];
    if (why) parts.push(why);
    const story = answer(ad.dns_story);
    if (story) parts.push(`${story}.`);
    add("What I’d say to a riding mate in my shoes", "to_improve");
    parts.push("Can you help me talk it through?");
    return parts.join(" ");
  }

  parts.push(`I just completed ${goal.event_name}.`);
  if (goal.finish_time_seconds) {
    const h = Math.floor(goal.finish_time_seconds / 3600);
    const m = Math.floor((goal.finish_time_seconds % 3600) / 60);
    parts.push(`I finished in ${h}h ${m}m.`);
  }
  const satisfaction = goal.overall_satisfaction;
  const effort = goal.perceived_exertion;
  if (satisfaction && effort) {
    parts.push(`I rated my satisfaction ${satisfaction}/10 and effort ${effort}/10.`);
  } else if (satisfaction) {
    parts.push(`I rated my satisfaction ${satisfaction}/10.`);
  } else if (effort) {
    parts.push(`I rated my effort ${effort}/10.`);
  }
  add("What went well", "went_well");
  add("What to improve", "to_improve");
  parts.push("Can you help me debrief this event?");
  return parts.join(" ");
}
