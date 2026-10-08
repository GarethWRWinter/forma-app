/**
 * Ride mode's safety rules: which sessions it refuses, the easy version of a
 * held hard session, what the trainer is told at each moment, and when to
 * ease off a rider who can't hold the number.
 *
 * The server owns the gate (GET /users/me/safety-state); these rules apply it
 * to the steps a trainer is about to be given. The ERG power cap itself lives
 * in lib/bluetooth.ts, at the transport layer.
 *
 * No runtime imports: the tests run this file straight under Node.
 * Tests: node --test src/lib/ride-safety.test.mjs
 */

// === Limits (mirror app/services/safety_service.py) ===

/** Highest step allowed per session type, as a fraction of FTP. The server
    sends its own copy in safety-state; these fill any gap. */
export const DEFAULT_CEILINGS: Record<string, number> = {
  recovery: 0.6,
  endurance: 0.8,
  tempo: 0.92,
  sweet_spot: 0.97,
  threshold: 1.1,
  vo2max: 1.3,
  sprint: 2.5,
};

/** Session types an "easy" day still rides as written. */
export const EASY_TYPES = new Set(["recovery", "endurance"]);

/** Under "easy", every step is capped here, as plan generation and the
    coach are (safety_service.EASY_CAP). The server sends its own copy. */
export const EASY_CAP_PCT = 0.75;

/** A step at or above this, or a session of a HARD_TYPES type, is a hard
    session and gets the warning on the ready screen. */
export const HARD_STEP_PCT = 1.05;
export const HARD_TYPES = new Set(["vo2max", "sprint"]);

/** Pause holds the trainer here when it can't be released outright. */
export const PAUSE_RELEASE_PCT = 0.4;

/** Ease off: below 70% of target for 20 seconds on a work step. */
export const EASE_OFF_RATIO = 0.7;
export const EASE_OFF_SECONDS = 20;
/** Readings are smoothed over this window so one dropped sample can't
    trigger (or cancel) an ease-off. */
export const EASE_OFF_SMOOTHING_MS = 3000;
/** Steps above this are work, so they can ease off even outside intervals. */
export const WORK_STEP_PCT = 0.8;
/** Easing off drops to the step's recovery, never above this. An over-under
    "recovers" at 90%, which is no help to a rider who is cooked. */
export const EASE_OFF_MAX_PCT = 0.55;
export const DEFAULT_RECOVERY_PCT = 0.5;

const EPS = 1e-6;

// === Steps ===

/** A workout step as the API sends it (lib/api.ts WorkoutStep). */
export interface RawStep {
  id: string;
  step_order: number;
  step_type: string;
  duration_seconds: number;
  power_target_pct: number | null;
  power_low_pct: number | null;
  power_high_pct: number | null;
  cadence_target: number | null;
  repeat_count: number | null;
  notes: string | null;
}

/** The target a step really rides at. Mirrors flattenSteps in
    useTrainingSession: a missing interval target means 100% of FTP. */
export function stepTargetPct(step: RawStep): number {
  return step.power_target_pct || (step.step_type === "interval_on" ? 1.0 : 0.5);
}

export function rawStepPeakPct(step: RawStep): number {
  return Math.max(stepTargetPct(step), step.power_low_pct || 0, step.power_high_pct || 0);
}

/** The most any step asks for, as a fraction of FTP. */
export function workoutPeakPct(steps: RawStep[]): number {
  return steps.reduce((peak, s) => Math.max(peak, rawStepPeakPct(s)), 0);
}

/** Unknown types and rest days are held to the recovery ceiling, so a
    mystery label fails closed (same rule as the server). */
export function ceilingFor(workoutType: string, ceilings: Record<string, number>): number {
  return ceilings[workoutType] ?? ceilings.recovery ?? DEFAULT_CEILINGS.recovery;
}

export function isHardSession(workoutType: string, steps: RawStep[]): boolean {
  return HARD_TYPES.has(workoutType) || workoutPeakPct(steps) >= HARD_STEP_PCT - EPS;
}

/** Every step capped at `capPct`. Ramps keep their shape below the cap. */
export function easyVersion<S extends RawStep>(steps: S[], capPct: number): S[] {
  return steps.map((s) => ({
    ...s,
    power_target_pct: Math.min(stepTargetPct(s), capPct),
    power_low_pct: s.power_low_pct ? Math.min(s.power_low_pct, capPct) : s.power_low_pct,
    power_high_pct: s.power_high_pct ? Math.min(s.power_high_pct, capPct) : s.power_high_pct,
  }));
}

// === The gate ===

export type SafetyAllowed = "none" | "easy" | "all";

export type RideGate<S extends RawStep = RawStep> =
  /** Ride these steps. `easy` means they were capped at `capPct`. */
  | { kind: "ride"; steps: S[]; easy: boolean; capPct: number | null }
  /** Riding is on hold: nothing goes on the trainer. */
  | { kind: "hold" }
  /** A step asks for more than the session's type allows. */
  | { kind: "mislabelled"; peakPct: number; ceilingPct: number };

/**
 * What ride mode may do with this workout today. "none" refuses everything.
 * A session above its type's ceiling is refused whatever the day allows (it
 * means the plan is wrong, not the rider). Under "easy", anything harder than
 * endurance rides only as its easy version.
 */
export function rideGate<S extends RawStep>(
  workoutType: string,
  steps: S[],
  allowed: SafetyAllowed,
  serverCeilings?: Record<string, number> | null,
  serverEasyCap?: number | null
): RideGate<S> {
  if (allowed === "none") return { kind: "hold" };
  const ceilings = { ...DEFAULT_CEILINGS, ...(serverCeilings || {}) };
  const peakPct = workoutPeakPct(steps);
  const ceilingPct = ceilingFor(workoutType, ceilings);
  if (peakPct > ceilingPct + EPS) return { kind: "mislabelled", peakPct, ceilingPct };
  if (allowed === "easy") {
    const capPct = serverEasyCap ?? EASY_CAP_PCT;
    if (!EASY_TYPES.has(workoutType) || peakPct > capPct + EPS) {
      return { kind: "ride", steps: easyVersion(steps, capPct), easy: true, capPct };
    }
  }
  return { kind: "ride", steps, easy: false, capPct: null };
}

// === What the trainer is told ===

export type TrainerCommand =
  /** Hold this power in ERG. "pause" is the 40% fallback while paused. */
  | { kind: "erg"; watts: number; reason: "target" | "pause" }
  /** Let go: ERG off, the rider rides against their own resistance. */
  | { kind: "release"; reason: "sprint" | "stop" | "end" };

export interface TrainerCommandInput {
  status: "idle" | "running" | "paused" | "completed";
  /** Stop was pressed: release before the confirmation is answered. */
  halted: boolean;
  targetWatts: number;
  targetPct: number;
  ftp: number;
  /** ERG cap as a fraction of FTP. Anything above it runs released. */
  ergCapPct: number;
}

export function trainerCommand(input: TrainerCommandInput): TrainerCommand | null {
  const { status, halted, targetWatts, targetPct, ftp, ergCapPct } = input;
  if (status === "idle") return null;
  if (status === "completed") return { kind: "release", reason: "end" };
  if (halted) return { kind: "release", reason: "stop" };
  if (status === "paused") {
    return { kind: "erg", watts: Math.round(PAUSE_RELEASE_PCT * ftp), reason: "pause" };
  }
  if (targetPct > ergCapPct + EPS) return { kind: "release", reason: "sprint" };
  return { kind: "erg", watts: targetWatts, reason: "target" };
}

/** Two commands with the same key would leave the trainer in the same state. */
export function commandKey(cmd: TrainerCommand | null): string | null {
  if (!cmd) return null;
  return cmd.kind === "release" ? "release" : `erg:${cmd.watts}`;
}

// === Ease off ===

/** The parts of a flattened session step these rules read. */
export interface FlatStepLike {
  stepType: string;
  powerTargetPct: number;
  powerLowPct?: number;
  powerHighPct?: number;
  recoveryPct?: number;
}

export function flatStepPeakPct(step: FlatStepLike): number {
  return Math.max(step.powerTargetPct, step.powerLowPct ?? 0, step.powerHighPct ?? 0);
}

/** Intervals and anything above endurance, but never a released sprint:
    there is no ERG target to drop. */
export function isEaseOffStep(step: FlatStepLike, ergCapPct: number): boolean {
  const peak = flatStepPeakPct(step);
  if (peak > ergCapPct + EPS) return false;
  return step.stepType === "interval_on" || peak > WORK_STEP_PCT + EPS;
}

/** Where an eased step drops to: its own recovery, capped at 55% of FTP. */
export function easeOffPct(step: FlatStepLike): number {
  const recovery = Math.min(step.recoveryPct ?? DEFAULT_RECOVERY_PCT, EASE_OFF_MAX_PCT);
  return Math.min(recovery, step.powerTargetPct);
}

/**
 * Watches live power against the target. `update` returns true once, when
 * the smoothed power has stayed below 70% of target for 20 seconds; it then
 * starts afresh. Zero readings count as below: a rider who stops pedalling
 * is the clearest case for letting go.
 */
export class EaseOffDetector {
  private samples: { t: number; w: number }[] = [];
  private belowSince: number | null = null;

  reset(): void {
    this.samples = [];
    this.belowSince = null;
  }

  update(nowMs: number, watts: number, targetWatts: number): boolean {
    if (!(targetWatts > 0)) {
      this.reset();
      return false;
    }
    this.samples.push({ t: nowMs, w: Math.max(0, watts || 0) });
    this.samples = this.samples.filter((s) => nowMs - s.t <= EASE_OFF_SMOOTHING_MS);
    const avg = this.samples.reduce((sum, s) => sum + s.w, 0) / this.samples.length;
    if (avg >= EASE_OFF_RATIO * targetWatts) {
      this.belowSince = null;
      return false;
    }
    if (this.belowSince === null) this.belowSince = nowMs;
    if (nowMs - this.belowSince >= EASE_OFF_SECONDS * 1000) {
      this.reset();
      return true;
    }
    return false;
  }
}

// === The record of what the trainer was told ===

/** Canonical form of the steps, for ride_session_starts.steps_hash. */
export function stepsSignature(steps: RawStep[]): string {
  const rows = [...steps]
    .sort((a, b) => a.step_order - b.step_order)
    .map((s) => [
      s.step_order,
      s.step_type,
      s.duration_seconds,
      s.power_target_pct ?? null,
      s.power_low_pct ?? null,
      s.power_high_pct ?? null,
      s.repeat_count ?? null,
      s.cadence_target ?? null,
    ]);
  return JSON.stringify(rows);
}

export async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** The most ERG will be asked to hold: steps above the cap run released, a
    ramp that crosses it is held up to it. */
export function maxErgTargetPct(steps: FlatStepLike[], ergCapPct: number): number {
  let peak = 0;
  for (const s of steps) {
    const p = flatStepPeakPct(s);
    if (p <= ergCapPct + EPS) peak = Math.max(peak, p);
    else if ((s.powerLowPct ?? s.powerTargetPct) <= ergCapPct + EPS) peak = Math.max(peak, ergCapPct);
  }
  return peak;
}

// === Words ===

export const RIDE_MODE_ACK_TITLE = "Before your first ride with Forma";
export const RIDE_MODE_ACK_BUTTON = "Understood, let's ride";

export function rideModeAckParagraphs(emergencyNumber: string): string[] {
  return [
    "In ERG mode your trainer holds the power for you. You're always in charge: ease off, pause or stop whenever you need to.",
    "Power targets and Race Radio come from software and can lag or be wrong, so trust your body over the screen.",
    `Stop at once if you feel chest pain, faintness, dizziness or unusual breathlessness, and call ${emergencyNumber} if it doesn't settle quickly.`,
    "Set the trainer up as its maker says, on a stable floor with a fan and a drink in reach.",
  ];
}

/** Exactly what the rider read and the button they pressed, for the
    consent_events row. */
export function rideModeAckRecord(emergencyNumber: string): string {
  return [RIDE_MODE_ACK_TITLE, ...rideModeAckParagraphs(emergencyNumber), RIDE_MODE_ACK_BUTTON].join(
    "\n\n"
  );
}

export const HARD_SESSION_LINE =
  "Hard session today. If you feel unwell or you're carrying an injury, skip it and tell me. Stop if you feel chest pain, faintness or unusual breathlessness.";

export const SPRINT_ERG_OFF = "Sprint: ERG off, it's all you";

const TYPE_LABELS: Record<string, string> = {
  recovery: "recovery",
  endurance: "endurance",
  tempo: "tempo",
  sweet_spot: "sweet spot",
  threshold: "threshold",
  vo2max: "VO2max",
  sprint: "sprint",
  rest: "rest",
};

function typeLabel(workoutType: string): string {
  return TYPE_LABELS[workoutType] ?? (workoutType.replace(/_/g, " ") || "this kind of");
}

function capitalise(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

const pctText = (p: number) => `${Math.round(p * 100)}%`;

/** Why a mislabelled session stays off the trainer. */
export function mislabelledText(workoutType: string, peakPct: number, ceilingPct: number): string {
  const label = typeLabel(workoutType);
  return `This session is labelled ${label}, but a step asks for ${pctText(peakPct)} of your FTP. ${capitalise(label)} sessions top out at ${pctText(ceilingPct)}, so I won't put it on your trainer.`;
}

/** Said whenever a session rides as its easy version. */
export function easyVersionText(opts: {
  /** True when a doctor's clearance lifts it; false for a return from a break. */
  doctor: boolean;
  capPct: number;
  capWatts: number;
  /** Long date the layoff gate opens, e.g. "22 October". */
  gateOpens?: string | null;
}): string {
  const cap = `Every step is capped at ${pctText(opts.capPct)} of your FTP (${opts.capWatts}W).`;
  if (opts.doctor) {
    return `Hard sessions are on hold until you tell me a doctor has cleared you, so this is the easy version. ${cap} Easy riding is fine if you feel well.`;
  }
  const back = opts.gateOpens ? ` Hard sessions come back on ${opts.gateOpens}.` : "";
  return `You're easing back in after a break, so this is the easy version. ${cap}${back}`;
}

// === Reading the gate ===

/** The parts of a react-query result the freshness rule reads. */
export interface SafetyReadLike {
  hasData: boolean;
  isError: boolean;
  isFetching: boolean;
  /** When the data was last written, in ms (0 if never). */
  dataUpdatedAt: number;
}

export type SafetyReadStatus = "fresh" | "loading" | "failed";

/**
 * Whether ride mode may act on the safety state it holds. Only a read made
 * since the page opened counts: the hold banner's cached copy can be minutes
 * old, and a refetch that fails keeps it. "loading" while that read is under
 * way; "failed" when it errored, or finished with nothing newer than the
 * mount. Ride mode refuses to start on anything but "fresh".
 */
export function safetyReadStatus(read: SafetyReadLike, openedAt: number): SafetyReadStatus {
  if (read.hasData && !read.isError && read.dataUpdatedAt >= openedAt) return "fresh";
  if (read.isFetching) return "loading";
  return "failed";
}

// === The trainer's control point ===

/** The bytes a trainer is sent. lib/bluetooth.ts builds them; they are passed
    in so these rules run under Node without it. */
export interface TrainerCodec {
  setTargetPower(watts: number): Uint8Array;
  stop(): Uint8Array;
  startOrResume(): Uint8Array;
}

export interface TrainerControlTimings {
  /** Longest one write may take while letting go before we move on. */
  writeTimeoutMs: number;
  /** Longest a write already in flight may hold up letting go. */
  queueWaitMs: number;
}

export const TRAINER_RELEASE_TIMINGS: TrainerControlTimings = {
  writeTimeoutMs: 1500,
  queueWaitMs: 2000,
};

/** Settles when `p` does or after `ms`, whichever is first, and never rejects. */
function settleWithin(p: Promise<unknown>, ms: number): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    p.then(
      () => {
        clearTimeout(timer);
        resolve();
      },
      (e) => {
        clearTimeout(timer);
        console.error("Trainer release write failed:", e);
        resolve();
      }
    );
  });
}

/**
 * The trainer's control point, one write at a time. Web Bluetooth rejects a
 * write while another is in flight on the same characteristic, so every
 * command (targets, stops, letting go) goes through one queue, in order.
 */
export class TrainerControl {
  private readonly codec: TrainerCodec;
  private readonly timings: TrainerControlTimings;
  private write: ((bytes: Uint8Array) => Promise<void>) | null = null;
  private stopped = false;
  private tail: Promise<void> = Promise.resolve();

  // Plain fields, not parameter properties: Node strips types from this
  // file for the tests, and it can't strip those.
  constructor(codec: TrainerCodec, timings: TrainerControlTimings = TRAINER_RELEASE_TIMINGS) {
    this.codec = codec;
    this.timings = timings;
  }

  /** A trainer has taken control and started: commands go to `write`. */
  attach(write: (bytes: Uint8Array) => Promise<void>): void {
    this.write = write;
    this.stopped = false;
  }

  /** The trainer has gone: later commands are dropped. */
  detach(): void {
    this.write = null;
  }

  get attached(): boolean {
    return this.write !== null;
  }

  private enqueue(cmd: () => Promise<void>): Promise<void> {
    const run = this.tail.then(cmd).catch((e) => {
      console.error("Trainer command failed:", e);
    });
    this.tail = run;
    return run;
  }

  /** Hold `watts` in ERG. After a stop, Start/Resume goes first, or many
      trainers ignore Set Target Power and ERG never comes back. Resolves true
      once the target is written. */
  setTarget(watts: number): Promise<boolean> {
    let written = false;
    return this.enqueue(async () => {
      const write = this.write;
      if (!write) return;
      if (this.stopped) {
        try {
          await write(this.codec.startOrResume());
          this.stopped = false;
        } catch (e) {
          console.error("Failed to resume trainer:", e);
        }
      }
      await write(this.codec.setTargetPower(watts));
      written = true;
    }).then(() => written);
  }

  /** FTMS stop: ERG off, the rider rides against their own resistance. */
  stop(): Promise<void> {
    return this.enqueue(async () => {
      const write = this.write;
      if (!write) return;
      await write(this.codec.stop());
      this.stopped = true;
    });
  }

  /**
   * Let go of the trainer, then `disconnect`. Some trainers (a Wahoo KICKR,
   * for one) keep holding the last ERG target after Bluetooth drops, so a
   * bare disconnect mid-interval can leave the rider grinding against it.
   * Unless the trainer is already stopped, it is sent 40% of FTP first (a
   * trainer that ignores the FTMS stop still drops a hard target), then the
   * stop, and only then disconnected.
   *
   * Waits behind a write already in flight, but never for long, and a write
   * that fails or hangs never stops `disconnect` being called.
   */
  releaseThenDisconnect(releaseWatts: number | null, disconnect: () => void): Promise<void> {
    const { writeTimeoutMs, queueWaitMs } = this.timings;
    let started: Promise<void> | null = null;
    const letGo = () =>
      (started ??= (async () => {
        try {
          const write = this.write;
          if (write && !this.stopped) {
            if (releaseWatts !== null && releaseWatts > 0) {
              await settleWithin(write(this.codec.setTargetPower(releaseWatts)), writeTimeoutMs);
            }
            await settleWithin(
              write(this.codec.stop()).then(() => {
                this.stopped = true;
              }),
              writeTimeoutMs
            );
          }
        } finally {
          this.write = null;
          try {
            disconnect();
          } catch (e) {
            console.error("Trainer disconnect failed:", e);
          }
        }
      })());
    return new Promise<void>((resolve) => {
      // A write stuck in the queue can't hold the release up for ever.
      const timer = setTimeout(() => void letGo().then(resolve), queueWaitMs);
      void this.enqueue(() => {
        clearTimeout(timer);
        return letGo();
      }).then(resolve);
    });
  }
}
