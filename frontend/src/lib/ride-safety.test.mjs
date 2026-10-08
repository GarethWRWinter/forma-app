// Run: node --test src/lib/ride-safety.test.mjs
// Node strips the types from the .ts files itself, so no build step.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  ERG_ABSOLUTE_MAX_WATTS,
  clampErgWatts,
  createCommandQueue,
  ergCeilingFor,
  ftmsSetTargetPower,
  getErgCeiling,
  setErgCeiling,
} from "./bluetooth.ts";
import {
  EaseOffDetector,
  HARD_SESSION_LINE,
  RIDE_MODE_ACK_BUTTON,
  RIDE_MODE_ACK_TITLE,
  SPRINT_ERG_OFF,
  commandKey,
  easeOffPct,
  easyVersion,
  easyVersionText,
  isEaseOffStep,
  isHardSession,
  maxErgTargetPct,
  mislabelledText,
  rideGate,
  rideModeAckParagraphs,
  rideModeAckRecord,
  sha256Hex,
  stepsSignature,
  trainerCommand,
  workoutPeakPct,
} from "./rideSafety.ts";
import { selectMessage } from "./coachMessages.ts";

// === Fixtures (shapes from app/core/workout_templates.py) ===

let order = 0;
function step(step_type, pct, extra = {}) {
  order += 1;
  return {
    id: `s${order}`,
    step_order: order,
    step_type,
    duration_seconds: 300,
    power_target_pct: pct,
    power_low_pct: null,
    power_high_pct: null,
    cadence_target: null,
    repeat_count: null,
    notes: null,
    ...extra,
  };
}

const vo2 = () => [
  step("warmup", 0.55),
  step("interval_on", 1.12, { power_low_pct: 1.06, power_high_pct: 1.2, repeat_count: 5 }),
  step("interval_off", 0.5),
  step("cooldown", 0.45),
];
const endurance = () => [
  step("warmup", 0.55, { power_low_pct: 0.45, power_high_pct: 0.65 }),
  step("steady_state", 0.65, { power_low_pct: 0.56, power_high_pct: 0.75 }),
  step("cooldown", 0.5),
];
const sprints = () => [
  step("warmup", 0.55),
  step("steady_state", 0.7),
  step("interval_on", 2.0, { repeat_count: 6, duration_seconds: 10 }),
  step("interval_off", 0.45, { duration_seconds: 290 }),
];

// === ERG cap at the transport layer ===

test("the ERG ceiling is 1.3 x FTP and a looser cap is ignored", () => {
  assert.equal(ergCeilingFor(250), 325);
  assert.equal(ergCeilingFor(250, 2.0), 325);
  assert.equal(ergCeilingFor(250, 1.2), 300);
  assert.equal(ergCeilingFor(287), 373); // floored, never rounded up
  assert.equal(ergCeilingFor(0), 0);
});

test("clamping keeps targets whole, non-negative and under the ceiling", () => {
  assert.equal(clampErgWatts(500, 325), 325);
  assert.equal(clampErgWatts(300.4, 325), 300);
  assert.equal(clampErgWatts(-20, 325), 0);
  assert.equal(clampErgWatts(Number.NaN, 325), 0);
  assert.equal(clampErgWatts(Infinity, 325), 0);
  // No FTP ceiling set: the absolute ceiling still holds.
  assert.equal(clampErgWatts(5000, null), ERG_ABSOLUTE_MAX_WATTS);
  // A ceiling above the absolute one can't loosen it.
  assert.equal(clampErgWatts(5000, 4000), ERG_ABSOLUTE_MAX_WATTS);
});

test("Set Target Power never encodes more than the ceiling", () => {
  const encoded = (bytes) => new DataView(bytes.buffer).getInt16(1, true);
  try {
    setErgCeiling(325.9);
    assert.equal(getErgCeiling(), 325);
    const cmd = ftmsSetTargetPower(500); // a sprint at 200% of a 250W FTP
    assert.equal(cmd[0], 0x05);
    assert.equal(encoded(cmd), 325);
    assert.equal(encoded(ftmsSetTargetPower(200)), 200);
    setErgCeiling(null);
    assert.equal(encoded(ftmsSetTargetPower(30000)), ERG_ABSOLUTE_MAX_WATTS);
    assert.equal(encoded(ftmsSetTargetPower(-50)), 0);
  } finally {
    setErgCeiling(null);
  }
});

test("trainer commands run one at a time, in order, past a failure", async () => {
  const queue = createCommandQueue();
  const log = [];
  let inFlight = 0;
  let maxInFlight = 0;
  const write = (name, ms, fail = false) => async () => {
    inFlight += 1;
    maxInFlight = Math.max(maxInFlight, inFlight);
    await new Promise((r) => setTimeout(r, ms));
    inFlight -= 1;
    log.push(name);
    if (fail) throw new Error("GATT operation already in progress");
  };
  const realError = console.error;
  console.error = () => {};
  try {
    void queue(write("target 300W", 20));
    void queue(write("pause 100W", 5, true));
    await queue(write("FTMS stop", 1));
  } finally {
    console.error = realError;
  }
  assert.deepEqual(log, ["target 300W", "pause 100W", "FTMS stop"]);
  assert.equal(maxInFlight, 1);
});

// === The gate ===

test("riding on hold refuses every session", () => {
  assert.deepEqual(rideGate("recovery", endurance(), "none"), { kind: "hold" });
});

test("a session above its type's ceiling is refused, whatever the day allows", () => {
  for (const allowed of ["all", "easy"]) {
    const gate = rideGate("recovery", vo2(), allowed);
    assert.equal(gate.kind, "mislabelled");
    assert.equal(gate.peakPct, 1.2); // the band's top counts, not just the target
    assert.equal(gate.ceilingPct, 0.6);
  }
  // Unknown types fail closed at the recovery ceiling.
  assert.equal(rideGate("mystery", endurance(), "all").kind, "mislabelled");
  // A missing interval target rides at 100%, so it counts as 100%.
  const bare = [step("interval_on", null, { repeat_count: 3 })];
  assert.equal(workoutPeakPct(bare), 1.0);
  assert.equal(rideGate("endurance", bare, "all").kind, "mislabelled");
});

test("the server's ceilings win over the built-in copy", () => {
  assert.equal(rideGate("tempo", vo2(), "all", { tempo: 1.3 }).kind, "ride");
  assert.equal(rideGate("vo2max", vo2(), "all", { vo2max: 1.1 }).kind, "mislabelled");
});

test("a normal day rides the session as written", () => {
  const steps = vo2();
  const gate = rideGate("vo2max", steps, "all");
  assert.equal(gate.kind, "ride");
  assert.equal(gate.easy, false);
  assert.equal(gate.steps, steps);
});

test("an easy day rides a hard session only as its easy version", () => {
  const steps = vo2();
  const gate = rideGate("vo2max", steps, "easy");
  assert.equal(gate.kind, "ride");
  assert.equal(gate.easy, true);
  assert.equal(gate.capPct, 0.75);
  assert.ok(workoutPeakPct(gate.steps) <= 0.75);
  assert.deepEqual(
    gate.steps.map((s) => s.id),
    steps.map((s) => s.id)
  );
  // The original is untouched.
  assert.equal(steps[1].power_target_pct, 1.12);
  // Easy steps keep their shape under the cap.
  assert.equal(gate.steps[0].power_target_pct, 0.55);
  assert.equal(gate.steps[2].power_target_pct, 0.5);
});

test("the server's easy cap wins over the built-in copy", () => {
  const gate = rideGate("vo2max", vo2(), "easy", null, 0.7);
  assert.equal(gate.capPct, 0.7);
  assert.ok(workoutPeakPct(gate.steps) <= 0.7);
});

test("an easy day rides endurance and recovery as written", () => {
  const steps = endurance();
  const gate = rideGate("endurance", steps, "easy");
  assert.equal(gate.kind, "ride");
  assert.equal(gate.easy, false);
  assert.equal(gate.steps, steps);
});

test("easyVersion fills a missing interval target before capping it", () => {
  const capped = easyVersion([step("interval_on", null)], 0.8);
  assert.equal(capped[0].power_target_pct, 0.8);
});

test("hard sessions: a step at 105% or more, or VO2max and sprint days", () => {
  const overUnder = [step("interval_on", 1.05), step("interval_off", 0.9)];
  const threshold = [step("interval_on", 0.97, { power_high_pct: 1.0 })];
  assert.equal(isHardSession("threshold", overUnder), true);
  assert.equal(isHardSession("threshold", threshold), false);
  assert.equal(isHardSession("vo2max", endurance()), true);
  assert.equal(isHardSession("sprint", endurance()), true);
  assert.equal(isHardSession("endurance", endurance()), false);
});

// === What the trainer is told ===

const cmd = (over) =>
  trainerCommand({
    status: "running",
    halted: false,
    targetWatts: 280,
    targetPct: 1.12,
    ftp: 250,
    ergCapPct: 1.3,
    ...over,
  });

test("the trainer holds the target, lets go for sprints, and eases on pause", () => {
  assert.equal(cmd({ status: "idle" }), null);
  assert.deepEqual(cmd({}), { kind: "erg", watts: 280, reason: "target" });
  assert.deepEqual(cmd({ targetPct: 1.3, targetWatts: 325 }), {
    kind: "erg",
    watts: 325,
    reason: "target",
  });
  assert.deepEqual(cmd({ targetPct: 2.0, targetWatts: 500 }), {
    kind: "release",
    reason: "sprint",
  });
  // Pause: 40% of FTP.
  assert.deepEqual(cmd({ status: "paused" }), { kind: "erg", watts: 100, reason: "pause" });
});

test("Stop releases before the question, and the end releases too", () => {
  assert.deepEqual(cmd({ status: "paused", halted: true }), { kind: "release", reason: "stop" });
  assert.deepEqual(cmd({ halted: true }), { kind: "release", reason: "stop" });
  assert.deepEqual(cmd({ status: "completed" }), { kind: "release", reason: "end" });
});

test("command keys send each trainer state once", () => {
  assert.equal(commandKey(null), null);
  assert.equal(commandKey(cmd({})), "erg:280");
  assert.equal(
    commandKey(cmd({ status: "paused", halted: true })),
    commandKey(cmd({ status: "completed" }))
  );
  assert.notEqual(commandKey(cmd({})), commandKey(cmd({ status: "paused" })));
});

// === Ease off ===

test("ease off fires after 20 seconds below 70% of target, not before", () => {
  const d = new EaseOffDetector();
  const target = 300;
  let fired = -1;
  for (let t = 0; t <= 25; t++) {
    if (d.update(t * 1000, 150, target) && fired < 0) fired = t;
  }
  assert.equal(fired, 20);
});

test("holding 70% or more resets the ease-off clock", () => {
  const d = new EaseOffDetector();
  for (let t = 0; t < 15; t++) assert.equal(d.update(t * 1000, 150, 300), false);
  // Back up to target for a few seconds: the smoothed power recovers.
  for (let t = 15; t < 20; t++) d.update(t * 1000, 300, 300);
  // Another 19 seconds low is still not 20.
  let fired = false;
  for (let t = 20; t < 39; t++) fired = fired || d.update(t * 1000, 150, 300);
  assert.equal(fired, false);
});

test("one dropped reading doesn't start the ease-off clock", () => {
  const d = new EaseOffDetector();
  let fired = false;
  for (let t = 0; t < 60; t++) {
    const watts = t % 10 === 0 ? 0 : 300; // a dropout every 10 seconds
    fired = fired || d.update(t * 1000, watts, 300);
  }
  assert.equal(fired, false);
});

test("ease off applies to intervals and work above endurance, never to a released sprint", () => {
  assert.equal(isEaseOffStep({ stepType: "interval_on", powerTargetPct: 0.9 }, 1.3), true);
  assert.equal(isEaseOffStep({ stepType: "steady_state", powerTargetPct: 0.95 }, 1.3), true);
  assert.equal(
    isEaseOffStep({ stepType: "steady_state", powerTargetPct: 0.65, powerHighPct: 0.75 }, 1.3),
    false
  );
  assert.equal(isEaseOffStep({ stepType: "interval_on", powerTargetPct: 2.0 }, 1.3), false);
});

test("easing off drops to the step's recovery, never above 55%", () => {
  assert.equal(easeOffPct({ stepType: "interval_on", powerTargetPct: 1.12, recoveryPct: 0.5 }), 0.5);
  // Over-unders "recover" at 90%: no help to a rider who is cooked.
  assert.equal(easeOffPct({ stepType: "interval_on", powerTargetPct: 1.05, recoveryPct: 0.9 }), 0.55);
  assert.equal(easeOffPct({ stepType: "steady_state", powerTargetPct: 0.95 }), 0.5);
});

// === The record ===

test("the steps signature ignores input order and the hash is SHA-256", async () => {
  const steps = vo2();
  assert.equal(stepsSignature(steps), stepsSignature([...steps].reverse()));
  assert.notEqual(stepsSignature(steps), stepsSignature(easyVersion(steps, 0.8)));
  assert.equal(
    await sha256Hex("abc"),
    "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
  );
  assert.equal((await sha256Hex(stepsSignature(steps))).length, 64);
});

test("the highest ERG target leaves out released sprints", () => {
  const flat = (steps) =>
    steps.map((s) => ({
      stepType: s.step_type,
      powerTargetPct: s.power_target_pct,
      powerLowPct: s.power_low_pct ?? undefined,
      powerHighPct: s.power_high_pct ?? undefined,
    }));
  assert.equal(maxErgTargetPct(flat(sprints()), 1.3), 0.7);
  // A ramp that crosses the cap is held up to it.
  const ramp = [{ stepType: "ramp", powerTargetPct: 0.5, powerLowPct: 0.5, powerHighPct: 1.5 }];
  assert.equal(maxErgTargetPct(ramp, 1.3), 1.3);
});

// === Words ===

test("the ride-mode acknowledgement is recorded as shown", () => {
  const lines = rideModeAckParagraphs("999");
  assert.ok(lines[2].includes("call 999 if it doesn't settle quickly"));
  assert.ok(rideModeAckParagraphs("112")[2].includes("call 112"));
  const record = rideModeAckRecord("999");
  assert.ok(record.startsWith(`${RIDE_MODE_ACK_TITLE}\n\n`));
  assert.ok(record.endsWith(`\n\n${RIDE_MODE_ACK_BUTTON}`));
  for (const line of lines) assert.ok(record.includes(line));
  assert.equal(RIDE_MODE_ACK_BUTTON, "Understood, let's ride");
});

test("ride-mode copy matches section D", () => {
  assert.equal(
    HARD_SESSION_LINE,
    "Hard session today. If you feel unwell or you're carrying an injury, skip it and tell me. Stop if you feel chest pain, faintness or unusual breathlessness."
  );
  assert.equal(SPRINT_ERG_OFF, "Sprint: ERG off, it's all you");
  assert.equal(
    mislabelledText("sweet_spot", 1.12, 0.97),
    "This session is labelled sweet spot, but a step asks for 112% of your FTP. Sweet spot sessions top out at 97%, so I won't put it on your trainer."
  );
  const doctor = easyVersionText({ doctor: true, capPct: 0.8, capWatts: 200 });
  assert.ok(doctor.startsWith("Hard sessions are on hold until you tell me a doctor has cleared you"));
  assert.ok(doctor.includes("80% of your FTP (200W)"));
  const layoff = easyVersionText({ doctor: false, capPct: 0.8, capWatts: 200, gateOpens: "22 October" });
  assert.ok(layoff.endsWith("Hard sessions come back on 22 October."));
  assert.ok(!easyVersionText({ doctor: false, capPct: 0.8, capWatts: 200 }).includes("come back"));
});

// === Race Radio ===

const ctx = {
  stepType: "interval_on",
  zoneName: "VO2max",
  zoneNumber: 5,
  targetWatts: 300,
  targetPct: 1.2,
  stepDuration: 240,
  stepElapsed: 120,
  stepRemaining: 120,
  cadenceTarget: null,
  totalElapsed: 1200,
  totalRemaining: 0,
};

test("hard sessions open with the hard-session line", () => {
  assert.equal(
    selectMessage("workout_start_hard", ctx, []),
    "Hard one today. You're in charge: if anything feels wrong, stop."
  );
});

test("pause says the resistance is off", () => {
  assert.equal(selectMessage("pause", ctx, []), "Resistance off. Take your time.");
});

test("low power never pushes, and doesn't repeat itself", () => {
  const allowed = new Set([
    "Power's slipping. If the legs are gone, ease off. It still counts.",
    "Can't hold 300W today? That's fine. Ride what you can.",
    "Below target. Settle at a pace you can hold, or stop if you feel unwell.",
  ]);
  const said = [];
  for (let i = 0; i < 3; i++) said.push(selectMessage("power_too_low", ctx, said));
  assert.equal(new Set(said).size, 3);
  for (const line of said) assert.ok(allowed.has(line), line);
});

test("the halfway call names the warning signs", () => {
  const seen = new Set();
  for (let i = 0; i < 200; i++) seen.add(selectMessage("step_midpoint", ctx, []));
  assert.ok(
    seen.has("Halfway. Heavy legs are normal here. Chest pain or dizziness are not: stop if you feel either.")
  );
  for (const line of seen) assert.ok(!line.includes("legs are talking"), line);
});

test("no em or en dashes, and no push-through lines, anywhere in ride copy", () => {
  for (const file of [
    "./coachMessages.ts",
    "./rideSafety.ts",
    "./bluetooth.ts",
    "../hooks/useCoachRadio.ts",
    "../hooks/useTrainingSession.ts",
    "../app/dashboard/training/[id]/session/page.tsx",
  ]) {
    const text = readFileSync(new URL(file, import.meta.url), "utf8");
    const dashes = new RegExp(`[${String.fromCharCode(0x2013, 0x2014)}]`);
    assert.ok(!dashes.test(text), `${file} has a dash`);
  }
  const radio = readFileSync(new URL("./coachMessages.ts", import.meta.url), "utf8");
  for (const banned of ["Dig in", "I need {targetWatts}W", "Recommit", "Don't let it go"]) {
    assert.ok(!radio.includes(banned), banned);
  }
});
