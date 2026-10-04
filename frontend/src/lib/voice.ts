/**
 * FORMA shared voice — the small lines that make the app feel coached.
 * One home for rest-day copy, loading lines and empty-state one-liners
 * so the voice stays consistent and editable in one place.
 *
 * Voice rules live in .claude/skills/forma-voice. No em or en dashes.
 */

/** Deterministic pick so a given date always shows the same line. */
export function stableIndex(seed: string, len: number): number {
  let h = 0;
  for (let i = 0; i < seed.length; i++)
    h = (Math.imul(h, 31) + seed.charCodeAt(i)) >>> 0;
  return len > 0 ? h % len : 0;
}

/** Rest-day cells on the training calendar. */
export const REST_LINES = [
  "Rest. It counts as training.",
  "Feet up. Even Coppi took rest weeks.",
  "Rest day. Resist the urge.",
  "Recover like you mean it.",
  "This is when the training sinks in.",
  "Do nothing, brilliantly.",
];

export function restLine(dateStr: string): string {
  return REST_LINES[stableIndex(dateStr, REST_LINES.length)];
}

/** Forma-at-work loading lines. */
export const LOADING_LINES = {
  thinking: "Forma is thinking…",
  readingRide: "Forma is reading your ride…",
  writingPlan: "Forma is writing your season…",
  readingHistory: "Forma is reading your history…",
} as const;
