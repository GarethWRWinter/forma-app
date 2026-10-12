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
    included, so a reload shows the same thread the rider saw. */
export function fromStoredMessage(m: ChatMessage): ThreadMessage {
  const cards = m.role === "assistant" ? storedSafetyCards(m) : [];
  return {
    role: m.role,
    content: m.content,
    id: m.id,
    created_at: m.created_at,
    ...(cards.length ? { safety: cards } : {}),
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
    reply, and the filenames on a rider's message. A message is the same one
    by id, or by role and words when the page's copy has no id yet. What the
    server stored always wins. */
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
      : { ...m, attachments: found.attachments };
  });
}
