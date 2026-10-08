// Run: node --test src/lib/ride-exit.test.mjs
// Leaving ride mode (review finding 15) and the fresh safety read it starts
// from (finding 19). Node strips the types from the .ts files itself.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { ftmsSetTargetPower, ftmsStartOrResume, ftmsStop, setErgCeiling } from "./bluetooth.ts";
import { TrainerControl, safetyReadStatus } from "./rideSafety.ts";

const codec = {
  setTargetPower: ftmsSetTargetPower,
  stop: ftmsStop,
  startOrResume: ftmsStartOrResume,
};
const FAST = { writeTimeoutMs: 30, queueWaitMs: 60 };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Reads a write back as what it asks the trainer to do. */
function decode(bytes) {
  const b = Array.from(bytes);
  if (b[0] === 0x05) return `target ${b[1] | (b[2] << 8)}`;
  if (b[0] === 0x08 && b[1] === 0x01) return "stop";
  if (b[0] === 0x07) return "start";
  return `op ${b[0]}`;
}

/**
 * A fake control point that, like Web Bluetooth, rejects a write while
 * another is in flight. `delays` sets how long each write takes, in order;
 * "hang" never settles.
 */
function fakeTrainer({ delays = [], fail = () => false } = {}) {
  const log = [];
  let inFlight = false;
  let n = 0;
  const write = (bytes) => {
    const what = decode(bytes);
    if (inFlight) return Promise.reject(new Error(`GATT operation already in progress (${what})`));
    const delay = delays[n++] ?? 0;
    if (delay === "hang") {
      inFlight = true;
      log.push(`${what} (hung)`);
      return new Promise(() => {});
    }
    inFlight = true;
    return new Promise((resolve, reject) => {
      setTimeout(() => {
        inFlight = false;
        if (fail(what)) {
          log.push(`${what} (failed)`);
          reject(new Error("write failed"));
          return;
        }
        log.push(what);
        resolve();
      }, delay);
    });
  };
  return { log, write };
}

setErgCeiling(null);

// === Finding 15: leaving mid-ride lets the trainer go before disconnecting ===

test("leaving mid-interval sends 40%, then the FTMS stop, and only then disconnects", async () => {
  const t = new TrainerControl(codec, FAST);
  const trainer = fakeTrainer();
  t.attach(trainer.write);
  assert.equal(await t.setTarget(300), true);
  await t.releaseThenDisconnect(100, () => trainer.log.push("disconnect"));
  assert.deepEqual(trainer.log, ["target 300", "target 100", "stop", "disconnect"]);
});

test("a target still being written when the rider leaves goes first, with no collision", async () => {
  const t = new TrainerControl(codec, FAST);
  const trainer = fakeTrainer({ delays: [20, 0, 0] });
  t.attach(trainer.write);
  void t.setTarget(300); // in flight while the page unmounts
  await t.releaseThenDisconnect(100, () => trainer.log.push("disconnect"));
  assert.deepEqual(trainer.log, ["target 300", "target 100", "stop", "disconnect"]);
});

test("a trainer already let go (a sprint, Stop, the end) is simply disconnected", async () => {
  const t = new TrainerControl(codec, FAST);
  const trainer = fakeTrainer();
  t.attach(trainer.write);
  await t.setTarget(300);
  await t.stop();
  await t.releaseThenDisconnect(100, () => trainer.log.push("disconnect"));
  assert.deepEqual(trainer.log, ["target 300", "stop", "disconnect"]);
});

test("without an FTP to work from, the stop still goes before the disconnect", async () => {
  const t = new TrainerControl(codec, FAST);
  const trainer = fakeTrainer();
  t.attach(trainer.write);
  await t.releaseThenDisconnect(null, () => trainer.log.push("disconnect"));
  assert.deepEqual(trainer.log, ["stop", "disconnect"]);
});

test("a failed release write never stops the disconnect", async () => {
  const t = new TrainerControl(codec, FAST);
  const trainer = fakeTrainer({ fail: (what) => what === "target 100" });
  t.attach(trainer.write);
  await t.releaseThenDisconnect(100, () => trainer.log.push("disconnect"));
  assert.deepEqual(trainer.log, ["target 100 (failed)", "stop", "disconnect"]);
});

test("a write that never settles can't hold the disconnect up for ever", async () => {
  const t = new TrainerControl(codec, FAST);
  const trainer = fakeTrainer({ delays: ["hang"] });
  t.attach(trainer.write);
  void t.setTarget(300); // hangs, and with it the queue
  const started = Date.now();
  await t.releaseThenDisconnect(100, () => trainer.log.push("disconnect"));
  assert.ok(Date.now() - started < 500, "released within the time limits");
  assert.equal(trainer.log.at(-1), "disconnect");
});

test("nothing reaches the trainer once it has been let go", async () => {
  const t = new TrainerControl(codec, FAST);
  const trainer = fakeTrainer();
  t.attach(trainer.write);
  await t.releaseThenDisconnect(100, () => trainer.log.push("disconnect"));
  assert.equal(await t.setTarget(300), false);
  await t.stop();
  assert.deepEqual(trainer.log, ["target 100", "stop", "disconnect"]);
});

test("a target after a stop wakes the trainer with Start/Resume first", async () => {
  const t = new TrainerControl(codec, FAST);
  const trainer = fakeTrainer();
  t.attach(trainer.write);
  await t.stop();
  await t.setTarget(200);
  assert.deepEqual(trainer.log, ["stop", "start", "target 200"]);
});

test("useBluetooth lets the trainer go on unmount, and the page doesn't race it", () => {
  const hook = readFileSync(new URL("../hooks/useBluetooth.ts", import.meta.url), "utf8");
  const page = readFileSync(
    new URL("../app/dashboard/training/[id]/session/page.tsx", import.meta.url),
    "utf8"
  );
  // The old cleanup disconnected the trainer with the sensors, bare.
  assert.ok(!/cadenceConnection,\s*trainerConnection\]/.test(hook), "bare trainer disconnect on unmount");
  // Unmount and the device panel's disconnect both let go first.
  assert.equal(hook.match(/trainer\.releaseThenDisconnect\(releaseWatts\.current/g)?.length, 2);
  assert.ok(!page.includes("btActions.disconnectAll()"), "page disconnects on unmount");
  assert.ok(/useBluetooth\(\{\s*releaseWatts\s*\}\)/.test(page), "page passes the release level");
});

// === Finding 19: ride mode starts only from a read made since it opened ===

const opened = 1_000_000;

test("the hold banner's older copy, with the refetch failed, refuses", () => {
  assert.equal(
    safetyReadStatus({ hasData: true, isError: true, isFetching: false, dataUpdatedAt: opened - 60_000 }, opened),
    "failed"
  );
});

test("the older copy while the refetch is under way waits", () => {
  assert.equal(
    safetyReadStatus({ hasData: true, isError: false, isFetching: true, dataUpdatedAt: opened - 60_000 }, opened),
    "loading"
  );
});

test("the older copy with no refetch at all refuses", () => {
  assert.equal(
    safetyReadStatus({ hasData: true, isError: false, isFetching: false, dataUpdatedAt: opened - 1 }, opened),
    "failed"
  );
});

test("a read made since the page opened is the only one that counts", () => {
  assert.equal(
    safetyReadStatus({ hasData: true, isError: false, isFetching: false, dataUpdatedAt: opened + 250 }, opened),
    "fresh"
  );
});

test("a fresh read followed by a failed one refuses", () => {
  assert.equal(
    safetyReadStatus({ hasData: true, isError: true, isFetching: false, dataUpdatedAt: opened + 250 }, opened),
    "failed"
  );
});

test("nothing yet: loading while it fetches, refused once it fails", () => {
  assert.equal(safetyReadStatus({ hasData: false, isError: false, isFetching: true, dataUpdatedAt: 0 }, opened), "loading");
  assert.equal(safetyReadStatus({ hasData: false, isError: true, isFetching: false, dataUpdatedAt: 0 }, opened), "failed");
});

test("the session page gates on the fresh read, not the cache", () => {
  const page = readFileSync(
    new URL("../app/dashboard/training/[id]/session/page.tsx", import.meta.url),
    "utf8"
  );
  assert.ok(!page.includes("safetyQuery.data ?? null;"), "page reads the cache directly");
  assert.ok(page.includes('refetchOnMount: "always"'));
  assert.ok(page.includes("safetyReadStatus("));
  assert.ok(page.includes('safetyRead === "failed"'));
  assert.ok(/handleStart[\s\S]{0,400}safetyRead !== "fresh"/.test(page), "Start checks the read too");
});
