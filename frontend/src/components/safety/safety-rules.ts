/**
 * Safety copy and the small rules around it, in one place.
 *
 * The consent wording here is recorded word for word in consent_events, so
 * the label a rider reads and the text the API stores are built from the same
 * strings. Change a sentence here and every surface (and the record) changes
 * with it.
 *
 * No runtime imports: the tests run this file straight under Node.
 */

import type { SafetyHold, SafetyState, ScreeningQuestion } from "@/lib/api";

// === Registration ===

export const SAFETY_PANEL_TITLE = "Before you join";
export const SAFETY_PANEL_TEXT =
  "Forma is an AI coach. It can be wrong, it isn't medical advice, and nobody watches you ride. If you have a heart condition, chest pain, fainting, a long-term condition, or you're pregnant, talk to your doctor first. Stop riding and get help if you feel chest pain, faintness, dizziness or unusual breathlessness.";

/** A consent label with one linked phrase. `text` is what gets recorded. */
export interface ConsentLabel {
  before: string;
  link: string;
  href: string;
  after: string;
  text: string;
}

function label(before: string, link: string, href: string, after: string): ConsentLabel {
  return { before, link, href, after, text: `${before}${link}${after}` };
}

export const TERMS_URL = "https://ridewithforma.com/terms";
export const PRIVACY_URL = "https://ridewithforma.com/privacy";

/** Box 1: terms and risk acknowledgement. */
export const TERMS_BOX = label(
  "I agree to Forma's ",
  "terms",
  TERMS_URL,
  ". I understand the coaching is written by AI and can be wrong, that it isn't medical advice, and that I decide what I ride and stop if something feels wrong."
);

/** Box 2: health data (UK GDPR Art 9), always separate from box 1. */
export const HEALTH_BOX = label(
  "Forma can use the health details I share with it, such as injuries, illness, medication, sleep and my answers to its health questions, to coach me. The ",
  "privacy policy",
  PRIVACY_URL,
  " explains how to withdraw this."
);

export const DOB_HINT = "Forma is for adults, 18 and over.";
export const UNDER_18 =
  "Forma is for adults, so you need to be 18 or over to join. A British Cycling club can put you in touch with a qualified youth coach.";
export const COUNTRY_HINT = "So the coach gives you the right emergency numbers.";
export const US_CA_BLOCKED =
  "Forma isn't available in the US or Canada yet. Leave your email and I'll tell you when it is.";
export const ELSEWHERE_BLOCKED =
  "Forma is only open in the UK for now. Leave your email and I'll tell you when that changes.";
export const TERMS_UNTICKED =
  "Tick the first box to agree to the terms. Forma can't coach you without it.";
export const HEALTH_UNTICKED =
  "Tick the second box too. Forma needs your health details to coach you.";

// === Where riders live ===

export interface Country {
  code: string;
  name: string;
}

/** The pick for "somewhere not on the list". Never sent to the API. */
export const ELSEWHERE = "elsewhere";

/**
 * Countries named only so their riders are told why they can't join. The
 * server enforces an allowlist instead (settings.allowed_countries, sent by
 * GET /auth/config): anything off it is refused, not just these.
 */
export const BLOCKED_COUNTRIES = new Set(["US", "CA"]);

/**
 * The form's own list, used only when GET /auth/config can't be read: the
 * United Kingdom, matching section 3 of the terms and the server's default
 * allowlist (settings.allowed_countries), plus the US and Canada so those
 * riders are told why, not left guessing. When the server opens another
 * country (ALLOWED_COUNTRIES, set on the day the terms add it), the picker
 * takes it from /auth/config and names it with the browser's own region
 * names. The server refuses anything off its list whatever this shows.
 */
export const COUNTRIES: Country[] = [
  { code: "GB", name: "United Kingdom" },
  { code: "US", name: "United States" },
  { code: "CA", name: "Canada" },
];

/** Why this rider can't join from where they live, or null if they can. */
export function countryBlock(code: string): string | null {
  if (!code) return null;
  if (code === ELSEWHERE) return ELSEWHERE_BLOCKED;
  if (BLOCKED_COUNTRIES.has(code.toUpperCase())) return US_CA_BLOCKED;
  return null;
}

/** The number to call in an emergency. Unknown falls back to the UK's. */
export function emergencyNumber(country?: string | null): string {
  const c = (country || "").toUpperCase();
  if (!c || c === "GB") return "999";
  if (c === "US" || c === "CA") return "911";
  return "112";
}

/** Samaritans answer on 116 123 in the UK and Ireland. */
export function hasSamaritans(country?: string | null): boolean {
  const c = (country || "").toUpperCase();
  return !c || c === "GB" || c === "IE";
}

// === Age ===

/** Whole years old on `today`, from a YYYY-MM-DD date of birth. Null when
    the date is missing, malformed or in the future. */
export function ageOn(dob: string | null | undefined, today: Date = new Date()): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec((dob || "").trim());
  if (!m) return null;
  const y = Number(m[1]);
  const mo = Number(m[2]);
  const d = Number(m[3]);
  const born = new Date(y, mo - 1, d);
  // Rejects 2001-02-30 and the like, which Date quietly rolls forward.
  if (born.getFullYear() !== y || born.getMonth() !== mo - 1 || born.getDate() !== d) {
    return null;
  }
  let age = today.getFullYear() - y;
  const beforeBirthday =
    today.getMonth() < mo - 1 || (today.getMonth() === mo - 1 && today.getDate() < d);
  if (beforeBirthday) age -= 1;
  return age < 0 ? null : age;
}

export interface RegistrationChoices {
  dateOfBirth: string;
  country: string;
  termsAccepted: boolean;
  healthConsent: boolean;
}

export type RegistrationProblems = Partial<
  Record<"dateOfBirth" | "country" | "terms" | "health", string>
>;

/** Everything that stops this sign-up being sent, keyed by field. Empty
    means it can go to the server (which checks all of it again). */
export function registrationProblems(
  input: RegistrationChoices,
  today: Date = new Date()
): RegistrationProblems {
  const problems: RegistrationProblems = {};
  const age = ageOn(input.dateOfBirth, today);
  if (age === null) problems.dateOfBirth = "Add your date of birth.";
  else if (age < 18) problems.dateOfBirth = UNDER_18;
  if (!input.country) problems.country = "Choose where you live.";
  else {
    const block = countryBlock(input.country);
    if (block) problems.country = block;
  }
  if (!input.termsAccepted) problems.terms = TERMS_UNTICKED;
  if (!input.healthConsent) problems.health = HEALTH_UNTICKED;
  return problems;
}

// === Health screening ===

export const SCREENING_TITLE = "A few health questions first";

export function screeningIntro(country?: string | null): string {
  return `Eight yes or no questions. A yes won't stop you joining. It changes how we start. If something is happening right now, such as chest pain, call ${emergencyNumber(country)}.`;
}

export const SCREENING_QUESTIONS: { id: ScreeningQuestion; text: string }[] = [
  {
    id: "q1",
    text: "Has a doctor ever told you that you have a heart condition or high blood pressure?",
  },
  {
    id: "q2",
    text: "Do you ever get pain, pressure or tightness in your chest, whether resting, going about your day or exercising?",
  },
  {
    id: "q3",
    text: "In the past 12 months, have you fainted, blacked out, or been so dizzy that you lost your balance?",
  },
  {
    id: "q4",
    text: "Has a parent, brother or sister died suddenly, or been diagnosed with an inherited heart condition, before the age of 50?",
  },
  {
    id: "q5",
    text: "Do you have a long-term condition such as diabetes, asthma or epilepsy, or take prescribed medicine regularly?",
  },
  {
    id: "q6",
    text: "Do you have an injury, or a bone, joint or muscle problem that more training could make worse, or have you had surgery or a concussion in the past three months?",
  },
  {
    id: "q7",
    text: "Are you pregnant, or have you had a baby in the past 12 months?",
  },
  {
    id: "q8",
    text: "Has a doctor or other health professional told you to avoid hard exercise, or to exercise only under supervision?",
  },
];

export const LONG_BREAK_QUESTION =
  "Have you had four weeks or more off the bike in the past three months?";

export type ScreeningAnswers = Partial<Record<ScreeningQuestion, boolean>>;

/** The full answer set once every question has a yes or a no, else null. */
export function completeAnswers(
  answers: ScreeningAnswers
): Record<ScreeningQuestion, boolean> | null {
  const out = {} as Record<ScreeningQuestion, boolean>;
  for (const q of SCREENING_QUESTIONS) {
    const v = answers[q.id];
    if (typeof v !== "boolean") return null;
    out[q.id] = v;
  }
  return out;
}

// === Clearance ===

/** Same words as safety_service.CLEARANCE_TEXT, which the server records. */
export const CLEARANCE_TEXT =
  "A doctor (or my midwife or physio) has assessed me and cleared me for hard training.";
export const CLEARANCE_LIMITS_LABEL = "Anything they told you to avoid?";
export const CLEARED_BY_OPTIONS = ["My GP", "Another doctor", "My midwife", "My physio"];

/**
 * A fever lifts on the rider's own word, with no doctor named. Same words as
 * safety_service.FEVER_LIFT_TEXT, which the server records.
 */
export const FEVER_LIFT_TEXT =
  "My fever has been gone for 24 hours without paracetamol or ibuprofen, and my chest has cleared.";
export const FEVER_LIFT_BUTTON = "My fever has gone";
export const FEVER_UNTICKED = "Tick the box to confirm your fever has gone.";

/**
 * A head injury has its own declaration, never "cleared me for hard
 * training". Same words as safety_service.HEAD_LIFT_TEXT, which the server
 * records. Only a doctor can make it.
 */
export const HEAD_LIFT_TEXT =
  "A doctor has checked me since I hit my head, and I've had no symptoms for at least 24 hours.";
export const HEAD_CHECKED_BY_OPTIONS = ["My GP", "Another doctor"];

/** The tick box, the question and the buttons for each clearance form. */
export interface ClearanceCopy {
  tick: string;
  unticked: string;
  byQuestion: string;
  byOptions: string[];
  /** What happens next, under the form. Empty when there's nothing to add. */
  note: string;
  submit: string;
}

/** The clearance form's words for a hold's lift kind: a doctor's clearance
    (the default) or a head injury's own check. */
export function clearanceCopy(kind: "doctor" | "head_injury" = "doctor"): ClearanceCopy {
  if (kind === "head_injury") {
    return {
      tick: HEAD_LIFT_TEXT,
      unticked:
        "Tick the box to confirm a doctor has checked you and you've had no symptoms for at least 24 hours.",
      byQuestion: "Which doctor checked you?",
      byOptions: HEAD_CHECKED_BY_OPTIONS,
      note: "Riding stays easy until two weeks after the injury, building back gradually, and there's no racing or group riding before day 21. If any symptom comes back, stop riding and tell me.",
      submit: "Confirm",
    };
  }
  return {
    tick: CLEARANCE_TEXT,
    unticked: "Tick the box to confirm a doctor, midwife or physio has cleared you.",
    byQuestion: "Who cleared you?",
    byOptions: CLEARED_BY_OPTIONS,
    note: "",
    submit: "Confirm I'm cleared",
  };
}

// === Holds ===

/**
 * How a hold lifts, from safety_service.lift_kind. "doctor": a declared
 * clearance. "fever_self": the rider says the fever has gone, which leaves an
 * easy week. "head_injury": its own check (a doctor has seen them since, and
 * no symptoms for 24 hours), which leaves easy riding until two weeks after
 * the injury. "expires": ends by itself on expires_at. "layoff": the easy
 * start after a break. "admin_only": an under-18 account or a hold set by hand.
 */
export type LiftKind =
  | "doctor"
  | "fever_self"
  | "head_injury"
  | "expires"
  | "layoff"
  | "admin_only";

/** A hold as the server now sends it: lift_kind and expires_at included. */
export type Hold = Omit<SafetyHold, "lift_kind" | "expires_at"> & {
  lift_kind?: LiftKind;
  expires_at?: string | null;
};

/** One limit a clinician set, which the coach treats as a hard rule. */
export interface ClearanceLimitInfo {
  id: string;
  text: string;
  by: string | null;
  recorded_at: string;
}

/** The safety state with the fields added for lift kinds and limits. */
export type FullSafetyState = Omit<SafetyState, "hold" | "no_racing_or_group_until"> & {
  hold: Hold | null;
  limits?: ClearanceLimitInfo[];
  /** ISO date: after a head injury, no racing or group riding until this day. */
  no_racing_or_group_until?: string | null;
};

/** The hold's lift kind, worked out the server's way if it wasn't sent. */
export function liftKindOf(hold: Hold): LiftKind {
  if (hold.lift_kind) return hold.lift_kind;
  if (hold.red_flag === "minor" || hold.source === "admin") return "admin_only";
  // A break ends by itself too, but stays a break the rider can call a mistake.
  if (hold.source === "layoff" || hold.red_flag === "layoff") return "layoff";
  if (hold.expires_at) return "expires";
  if (hold.red_flag === "fever") return "fever_self";
  if (hold.red_flag === "head_injury") return "head_injury";
  return "doctor";
}

export function isMinorHold(hold: Hold | null | undefined): boolean {
  return !!hold && hold.red_flag === "minor";
}

export const MINOR_HOLD_TITLE = "Account on hold";
export const MINOR_HOLD_TEXT =
  "Forma is for adults, 18 and over. This account is on hold and will be closed, with anything you've paid refunded.";

function dayMonth(iso: string): string {
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00` : iso);
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "long" });
}

/** The graded return after a head injury, as one sentence, or "" when the
    date has passed or wasn't sent. */
export function noRacingLine(until: string | null | undefined): string {
  if (!until) return "";
  return `No racing or group riding before ${dayMonth(until)}, three weeks after your head injury.`;
}

export interface HoldView {
  title: string;
  text: string;
  /** "I've been cleared" makes sense: a doctor can lift this one. */
  canClear: boolean;
  /** Which form "I've been cleared" opens: a doctor's clearance, or the
      head injury's own check. */
  clearKind: "doctor" | "head_injury";
  /** "My fever has gone": the rider lifts it on their own word. */
  canSelfLift: boolean;
  /** "This was a mistake": the server refuses it for screening, admin,
      under-18 holds and the easy week after a fever. */
  canMistake: boolean;
  /** The hold comes from the rider's own answers, which they can change. */
  fromAnswers: boolean;
}

export function holdView(
  hold: Hold,
  country?: string | null,
  noRacingUntil?: string | null
): HoldView {
  const kind = liftKindOf(hold);
  const n = emergencyNumber(country);
  const none = {
    canClear: false,
    clearKind: "doctor" as const,
    canSelfLift: false,
    canMistake: false,
    fromAnswers: false,
  };
  const noRacing = noRacingLine(noRacingUntil);

  // Before anything else: an account held as under 18 has no way out here.
  if (isMinorHold(hold)) {
    return { ...none, title: MINOR_HOLD_TITLE, text: MINOR_HOLD_TEXT };
  }
  if (kind === "admin_only") {
    return {
      ...none,
      title: hold.level === "hold_all" ? "Riding on hold" : "Easy riding only",
      text: "Forma has put this hold on by hand. If you think it's wrong, email gareth@ridewithforma.com.",
    };
  }

  const fromAnswers = hold.source === "screening";
  const canMistake =
    !fromAnswers &&
    (kind === "doctor" || kind === "fever_self" || kind === "head_injury" || kind === "layoff");
  if (kind === "expires") {
    const until = hold.expires_at ? dayMonth(hold.expires_at) : "";
    let text = `Hard sessions are on hold until ${until}. Easy riding is fine if you feel well.`;
    if (hold.red_flag === "fever") {
      text = `Easy riding only until ${until}, while you get over the fever. If it comes back, stop riding and tell me. If you get chest pain or struggle to breathe, call ${n}.`;
    } else if (hold.red_flag === "head_injury") {
      text = [
        `Easy riding only until ${until} while you build back after your head injury.`,
        noRacing,
        `If a headache, dizziness or any other symptom comes back, stop riding and tell me. If it gets worse, call ${n}.`,
      ]
        .filter(Boolean)
        .join(" ");
    }
    return { ...none, title: "Easy riding only", text };
  }
  if (kind === "head_injury") {
    return {
      ...none,
      title: "Riding on hold",
      text: [
        "No riding, training or racing until a doctor has checked you and you've had no symptoms for at least 24 hours. Then easy riding only until two weeks after the injury.",
        noRacing,
        `If symptoms get worse, call ${n}.`,
      ]
        .filter(Boolean)
        .join(" "),
      canClear: true,
      clearKind: "head_injury",
      canMistake,
    };
  }
  if (kind === "layoff") {
    return {
      ...none,
      title: "Easing back in",
      text: hold.expires_at
        ? `Hard sessions are on hold until ${dayMonth(hold.expires_at)} while you ease back in after a break. Easy riding is fine if you feel well.`
        : "Hard sessions are on hold while you ease back in after a break. Easy riding is fine if you feel well.",
      canMistake,
    };
  }
  if (kind === "fever_self") {
    return {
      ...none,
      title: "Riding on hold",
      text: `No training while you have a fever. Once it has been gone for 24 hours without paracetamol or ibuprofen and your chest has cleared, tap ${FEVER_LIFT_BUTTON}, and your first week back is easy riding only. If you get chest pain or struggle to breathe, call ${n}.`,
      canSelfLift: true,
      canMistake,
    };
  }
  if (hold.level === "hold_all") {
    return {
      ...none,
      title: "Riding on hold",
      text: `Riding is on hold until a doctor has checked you over. If symptoms come back, call ${n}.`,
      canClear: true,
      canMistake,
      fromAnswers,
    };
  }
  return {
    ...none,
    title: "Easy riding only",
    text: "Hard sessions are on hold until you tell me a doctor has cleared you. Easy riding is fine if you feel well.",
    canClear: true,
    canMistake,
    fromAnswers,
  };
}

/**
 * Which way out to offer outside the banner (Settings, then Health): a
 * doctor's clearance, the head injury's own check, the fever form, or
 * nothing. Nothing at all while the account is held as under 18: the server
 * refuses all of them.
 */
export function clearanceOffer(
  state: FullSafetyState | SafetyState | null | undefined,
  country?: string | null
): "doctor" | "head_injury" | "fever_self" | null {
  if (!state) return null;
  const hold = (state.hold ?? null) as Hold | null;
  if (hold) {
    if (isMinorHold(hold)) return null;
    const view = holdView(hold, country);
    if (view.canClear) return view.clearKind;
    if (view.canSelfLift) return "fever_self";
  }
  const screen = state.screening;
  if (screen && screen.tier !== "none" && !screen.clearance_confirmed) return "doctor";
  return null;
}

/** Every limit a clinician set, oldest first, falling back to the
    screening's own field for a server that hasn't sent the list. */
export function limitsOf(state: FullSafetyState | SafetyState): string[] {
  const list = (state as FullSafetyState).limits;
  if (Array.isArray(list)) return list.map((l) => l.text).filter(Boolean);
  return state.screening?.limits ? [state.screening.limits] : [];
}

export const STILL_HELD = "Noted. Another hold is still open, so I'm keeping things as they are.";
export const MISTAKE_LIFTED = "Hold lifted. If anything changes, tell me straight away.";

/** What the rider reads once a clearance (or the fever form) goes through,
    from the state the server sent back. */
export function liftedMessage(state: FullSafetyState | SafetyState): string {
  const hold = (state.hold ?? null) as Hold | null;
  const noRacing = noRacingLine((state as FullSafetyState).no_racing_or_group_until);
  if (hold) {
    if (liftKindOf(hold) === "expires" && hold.expires_at) {
      if (hold.red_flag === "head_injury") {
        return [
          `Thanks. Easy riding only until ${dayMonth(hold.expires_at)}, then build back gradually.`,
          noRacing,
        ]
          .filter(Boolean)
          .join(" ");
      }
      return `Thanks. Your first week back is easy riding only, until ${dayMonth(hold.expires_at)}.`;
    }
    return STILL_HELD;
  }
  if (noRacing) {
    return `Thanks. The hold is lifted. ${noRacing}`;
  }
  if (state.allowed === "easy" && state.layoff_gate_until) {
    return `Thanks. The hold is lifted. You've had a break, so I'll keep things steady until ${dayMonth(state.layoff_gate_until)}.`;
  }
  if (limitsOf(state).length) {
    return "Thanks. The hold is lifted, and I'll treat what they told you to avoid as a hard limit.";
  }
  return "Thanks. The hold is lifted.";
}

/** What the rider reads once "This was a mistake" goes through, from the
    state the server sent back. A fever or head injury mentioned again during
    its easy days is lifted on its own and the easy days run on, so say so
    rather than "Thanks". */
export function mistakeLiftedMessage(state: FullSafetyState | SafetyState): string {
  const hold = (state.hold ?? null) as Hold | null;
  if (!hold) return MISTAKE_LIFTED;
  if (liftKindOf(hold) === "expires" && hold.expires_at) {
    const until = dayMonth(hold.expires_at);
    if (hold.red_flag === "fever") {
      return `Hold lifted. Your easy week after the fever still runs until ${until}.`;
    }
    if (hold.red_flag === "head_injury") {
      return [
        `Hold lifted. Easy riding after your head injury still runs until ${until}.`,
        noRacingLine((state as FullSafetyState).no_racing_or_group_until),
      ]
        .filter(Boolean)
        .join(" ");
    }
    return `Hold lifted. Easy riding still runs until ${until}.`;
  }
  return STILL_HELD;
}

// === FTP test ===

export const FTP_TEST_WARNING =
  "This is a maximal test: 20 minutes as hard as you can hold. Only do it if you're well, rested and have been riding regularly for the past few weeks. Ride it on a trainer or a safe, traffic-free stretch, never in traffic. Stop at once if you feel chest pain, faintness or unusual breathlessness.";
export const FTP_TEST_OVER_35 = "If you haven't had a check-up recently, see your GP first.";
export const FTP_NOT_YET_LAYOFF = "Not yet. Let's get some steady riding in your legs first.";
export const FTP_NOT_YET_DOCTOR = "Not until a doctor has cleared you.";

/**
 * Whether the FTP test entry shows. "unknown" (state not loaded, or the read
 * failed) keeps it hidden: the gate fails closed. "closed": the account is
 * held as under 18 (show MINOR_HOLD_TEXT, never "see a doctor").
 */
export function ftpGate(
  state: FullSafetyState | SafetyState | null | undefined
): "open" | "layoff" | "doctor" | "closed" | "unknown" {
  if (!state) return "unknown";
  const hold = (state.hold ?? null) as Hold | null;
  if (isMinorHold(hold)) return "closed";
  if (state.allowed === "all") return "open";
  const screen = state.screening;
  const kind = hold ? liftKindOf(hold) : null;
  // A fever, the easy week after it and a break all end without a doctor,
  // so they get the "not yet" line rather than "see a doctor".
  const needsDoctor =
    kind === "doctor" ||
    kind === "head_injury" ||
    kind === "admin_only" ||
    (!hold && state.allowed === "none") ||
    (!!screen && screen.tier !== "none" && !screen.clearance_confirmed);
  return needsDoctor ? "doctor" : "layoff";
}

/** Age 35 and over sees the check-up line. So does a rider whose age we
    don't know: the line costs nothing and the risk sits with not saying it. */
export function showCheckUpLine(dob: string | null | undefined, today: Date = new Date()): boolean {
  const age = ageOn(dob, today);
  return age === null || age >= 35;
}

// === Coach chat ===

export function aiDisclaimer(country?: string | null): string {
  return `Forma is AI and can be wrong. It isn't medical advice. In an emergency, call ${emergencyNumber(country)}.`;
}

// === Re-acceptance ===

export const REACCEPT_TITLE = "We've updated the terms.";
export const REACCEPT_CHANGES = [
  "Forma is an AI coach. The terms now say plainly that it can be wrong, it isn't medical advice, and nobody watches you ride.",
  "A new section on health: when to see a doctor before you train, and when to stop riding and get help.",
  "Forma is for adults, 18 and over, and isn't available in the US or Canada yet.",
  "Your contract is with Forma Cycling Ltd, and the terms set out what we are and aren't responsible for.",
];
export const REACCEPT_UNTICKED =
  "Tick the box to agree to the terms. Forma can't coach you without it.";
/** Box 2 again, for an account made before it was asked (the beta riders). */
export const HEALTH_CONSENT_TITLE = "Can Forma use your health details?";
export const HEALTH_CONSENT_INTRO =
  "Forma asks a few health questions and remembers what you tell it about injuries and illness, so it can coach you well. Health details need your clear agreement, so tick this box once.";
