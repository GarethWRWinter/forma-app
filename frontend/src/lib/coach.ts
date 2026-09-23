/**
 * Coach personalisation — avatars and tones.
 *
 * Avatars are hand-drawn expressive line portraits (public/coaches/*.webp).
 * Deliberately unlabelled: no archetypes, no descriptors — the rider decides
 * for themselves what they see in each face. `defaultName` is internal
 * metadata only (drives the default name suggestion), never displayed.
 */

export interface CoachAvatar {
  key: string;
  src: string;
  /** Internal: name suggested when this face is picked. Never shown as a label. */
  defaultName: string;
}

export const COACH_AVATARS: CoachAvatar[] = [
  { key: "coach_01", src: "/coaches/coach_01.webp", defaultName: "Forma" },
  { key: "coach_02", src: "/coaches/coach_02.webp", defaultName: "Forma" },
  { key: "coach_03", src: "/coaches/coach_03.webp", defaultName: "Forma" },
  { key: "coach_04", src: "/coaches/coach_04.webp", defaultName: "Forma" },
  { key: "coach_05", src: "/coaches/coach_05.webp", defaultName: "Forma" },
  { key: "coach_11", src: "/coaches/coach_11.webp", defaultName: "Forma" },
  { key: "coach_12", src: "/coaches/coach_12.webp", defaultName: "Forma" },
  { key: "coach_13", src: "/coaches/coach_13.webp", defaultName: "Forma" },
  { key: "coach_14", src: "/coaches/coach_14.webp", defaultName: "Forma" },
  { key: "coach_15", src: "/coaches/coach_15.webp", defaultName: "Forma" },
  { key: "coach_06", src: "/coaches/coach_06.webp", defaultName: "Forma" },
  { key: "coach_07", src: "/coaches/coach_07.webp", defaultName: "Forma" },
  { key: "coach_08", src: "/coaches/coach_08.webp", defaultName: "Forma" },
  { key: "coach_09", src: "/coaches/coach_09.webp", defaultName: "Forma" },
  { key: "coach_10", src: "/coaches/coach_10.webp", defaultName: "Forma" },
  { key: "coach_16", src: "/coaches/coach_16.webp", defaultName: "Forma" },
  { key: "coach_17", src: "/coaches/coach_17.webp", defaultName: "Forma" },
  { key: "coach_18", src: "/coaches/coach_18.webp", defaultName: "Forma" },
  { key: "coach_19", src: "/coaches/coach_19.webp", defaultName: "Forma" },
  { key: "coach_20", src: "/coaches/coach_20.webp", defaultName: "Forma" },
];

// Legacy keys (pre-neutralisation) → new keys, so stored preferences keep working.
const LEGACY_KEYS: Record<string, string> = {
  m1_climber: "coach_01",
  m2_sprinter: "coach_02",
  m3_tt: "coach_03",
  m4_ds: "coach_04",
  m5_allrounder: "coach_05",
  f1_hardwoman: "coach_06",
  f2_sprinter: "coach_07",
  f3_climber: "coach_08",
  f4_professor: "coach_09",
  f5_endurance: "coach_10",
};

export function normalizeAvatarKey(key: string | undefined | null): string {
  if (!key) return COACH_AVATARS[0].key;
  return LEGACY_KEYS[key] ?? key;
}

export function coachAvatarSrc(key: string | undefined | null): string {
  const k = normalizeAvatarKey(key);
  const found = COACH_AVATARS.find((a) => a.key === k);
  return (found ?? COACH_AVATARS[0]).src;
}

export interface CoachTone {
  key: string;
  label: string;
  description: string;
  sample: string;
}

export const COACH_TONES: CoachTone[] = [
  {
    key: "balanced",
    label: "Balanced",
    description: "Warm and direct in equal measure, the classic coach.",
    sample: "Solid week. Thursday's the session that matters, let's arrive fresh.",
  },
  {
    key: "empathetic",
    label: "Empathetic & nurturing",
    description: "Leads with feelings, celebrates generously, softens hard truths.",
    sample: "You've carried a heavy week and still showed up, I'm proud of you. Let's be kind to your legs today.",
  },
  {
    key: "stoic",
    label: "Stoic & calm",
    description: "Spare, steady, unflappable. Facts, then one action.",
    sample: "TSB is −27. The work is done. Rest today.",
  },
  {
    key: "direct",
    label: "Direct & no-nonsense",
    description: "Blunt, honest, zero fluff. Tough love, fairly applied.",
    sample: "You started too hard and paid for it. Cap the first 10 minutes at 250W. That's the whole fix.",
  },
  {
    key: "analytical",
    label: "Analytical & data-deep",
    description: "Numbers first, mechanisms explained, depth welcome.",
    sample: "Decoupling was 8.4% after 2,100kJ, fuelling, not fitness. At 80g/hr I'd expect under 5%.",
  },
  {
    key: "playful",
    label: "Playful & witty",
    description: "Light, funny, banter-forward, serious training, unserious delivery.",
    sample: "Three PBs in one ride? Leave some watts for the rest of us. Easy spin tomorrow, hotshot.",
  },
];
