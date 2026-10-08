"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  EaseOffDetector,
  commandKey,
  easeOffPct,
  isEaseOffStep,
  trainerCommand,
  type TrainerCommand,
} from "@/lib/rideSafety";

// === Types ===

export interface SessionStep {
  index: number;
  originalStepId: string;
  stepType: string;
  durationSeconds: number;
  powerTargetPct: number;
  powerLowPct?: number;
  powerHighPct?: number;
  cadenceTarget?: number;
  notes?: string;
  isInterval: boolean;
  repeatIndex?: number;
  repeatTotal?: number;
  /** For an interval: the power its recovery rides at, which is where an
      eased-off rep drops to. */
  recoveryPct?: number;
}

export type SessionStatus = "idle" | "running" | "paused" | "completed";

/** The current step was eased off: power sat below 70% of target for 20
    seconds, so the target dropped to recovery for the rest of it. `id` goes
    up by one each time, so Race Radio can say so once. */
export interface EaseOffEvent {
  id: number;
  stepIndex: number;
  pct: number;
  fromWatts: number;
  toWatts: number;
}

export interface SessionState {
  status: SessionStatus;
  currentStepIndex: number;
  stepElapsedSeconds: number;
  totalElapsedSeconds: number;
  currentTargetWatts: number;
  currentTargetPct: number;
  currentCadenceTarget: number | null;
  steps: SessionStep[];
  totalDurationSeconds: number;
  /** What the trainer should be doing right now (null before the start). */
  trainerCommand: TrainerCommand | null;
  /** The current step is above the ERG cap and runs with ERG off. */
  ergReleased: boolean;
  /** Set while the current step is eased off. */
  easeOff: EaseOffEvent | null;
  /** The current step will ease off by itself if power drops. */
  easeOffEligible: boolean;
}

export interface SessionActions {
  start: () => void;
  pause: () => void;
  resume: () => void;
  stop: () => void;
  skipStep: () => void;
  prevStep: () => void;
  /** Stop pressed: pause and release the trainer before anything is asked. */
  halt: () => void;
  /** Send the current trainer command again, e.g. after a reconnect. */
  resync: () => void;
}

export interface TrainingSessionOptions {
  /** Every change in what the trainer should do. The page clamps and sends. */
  onTrainerCommand?: (cmd: TrainerCommand) => void;
  /** ERG cap as a fraction of FTP: steps above it run with ERG released. */
  ergCapPct: number;
  /** Live power for the ease-off check. Null when nothing measures power. */
  livePower?: number | null;
}

interface WorkoutStepInput {
  id: string;
  step_type: string;
  duration_seconds: number;
  power_target_pct: number | null;
  power_low_pct: number | null;
  power_high_pct: number | null;
  cadence_target: number | null;
  repeat_count: number | null;
  notes: string | null;
  step_order: number;
}

// === Flatten Steps (expand intervals) ===

function flattenSteps(steps: WorkoutStepInput[]): SessionStep[] {
  const sorted = [...steps].sort((a, b) => a.step_order - b.step_order);
  const flat: SessionStep[] = [];
  let globalIndex = 0;

  let i = 0;
  while (i < sorted.length) {
    const step = sorted[i];

    if (step.step_type === "interval_on") {
      const offStep =
        i + 1 < sorted.length && sorted[i + 1].step_type === "interval_off"
          ? sorted[i + 1]
          : null;
      const repeats = step.repeat_count || 1;
      const recoveryPct = offStep ? offStep.power_target_pct || 0.5 : undefined;

      for (let r = 0; r < repeats; r++) {
        // On interval
        flat.push({
          index: globalIndex++,
          originalStepId: step.id,
          stepType: "interval_on",
          durationSeconds: step.duration_seconds,
          powerTargetPct: step.power_target_pct || 1.0,
          cadenceTarget: step.cadence_target || undefined,
          notes: step.notes || undefined,
          isInterval: true,
          repeatIndex: r + 1,
          repeatTotal: repeats,
          recoveryPct,
        });

        // Off interval
        if (offStep) {
          flat.push({
            index: globalIndex++,
            originalStepId: offStep.id,
            stepType: "interval_off",
            durationSeconds: offStep.duration_seconds,
            powerTargetPct: offStep.power_target_pct || 0.5,
            cadenceTarget: offStep.cadence_target || undefined,
            notes: offStep.notes || undefined,
            isInterval: true,
            repeatIndex: r + 1,
            repeatTotal: repeats,
          });
        }
      }

      if (offStep) i += 1; // skip off step
    } else {
      flat.push({
        index: globalIndex++,
        originalStepId: step.id,
        stepType: step.step_type,
        durationSeconds: step.duration_seconds,
        powerTargetPct: step.power_target_pct || 0.5,
        powerLowPct: step.power_low_pct || undefined,
        powerHighPct: step.power_high_pct || undefined,
        cadenceTarget: step.cadence_target || undefined,
        notes: step.notes || undefined,
        isInterval: false,
      });
    }

    i += 1;
  }

  return flat;
}

// === Calculate Target Power ===

function calculateTargetPower(
  step: SessionStep,
  stepElapsed: number,
  ftp: number
): { watts: number; pct: number } {
  // For ramp/warmup/cooldown: linearly interpolate
  if (step.powerLowPct !== undefined && step.powerHighPct !== undefined) {
    const progress = Math.min(stepElapsed / step.durationSeconds, 1);
    const pct = step.powerLowPct + (step.powerHighPct - step.powerLowPct) * progress;
    return { watts: Math.round(pct * ftp), pct };
  }

  // Steady state
  return {
    watts: Math.round(step.powerTargetPct * ftp),
    pct: step.powerTargetPct,
  };
}

// === Hook ===

export function useTrainingSession(
  workoutSteps: WorkoutStepInput[],
  ftp: number,
  options: TrainingSessionOptions
): [SessionState, SessionActions] {
  const { ergCapPct, livePower = null } = options;
  const steps = useMemo(() => flattenSteps(workoutSteps), [workoutSteps]);
  const flatSteps = useRef(steps);
  flatSteps.current = steps;

  const totalDuration = flatSteps.current.reduce(
    (sum, s) => sum + s.durationSeconds,
    0
  );

  const [status, setStatus] = useState<SessionStatus>("idle");
  const [currentStepIndex, setCurrentStepIndex] = useState(0);
  const [stepElapsedSeconds, setStepElapsedSeconds] = useState(0);
  const [totalElapsedSeconds, setTotalElapsedSeconds] = useState(0);
  const [halted, setHalted] = useState(false);
  const [easeOff, setEaseOff] = useState<EaseOffEvent | null>(null);

  const startTime = useRef<number | null>(null);
  const stepStartTime = useRef<number | null>(null);
  const pausedTotalElapsed = useRef(0);
  const pausedStepElapsed = useRef(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastCommandKey = useRef<string | null>(null);
  const onTrainerCommandRef = useRef(options.onTrainerCommand);
  onTrainerCommandRef.current = options.onTrainerCommand;
  const detector = useRef(new EaseOffDetector());
  const easeOffCount = useRef(0);

  // Called wherever a step starts afresh (advance, skip, back, restart).
  const resetStepTrackers = useCallback(() => {
    lastCommandKey.current = null; // Force a fresh command to the trainer
    detector.current.reset();
    setEaseOff(null);
  }, []);

  const currentStep = flatSteps.current[currentStepIndex];
  const eased = easeOff && easeOff.stepIndex === currentStepIndex ? easeOff : null;
  const target = !currentStep
    ? { watts: 0, pct: 0 }
    : eased
      ? { watts: eased.toWatts, pct: eased.pct }
      : calculateTargetPower(currentStep, stepElapsedSeconds, ftp);

  // What the trainer should be doing: the target in ERG, 40% of FTP while
  // paused, released for sprints, Stop and the end.
  const command = trainerCommand({
    status,
    halted,
    targetWatts: target.watts,
    targetPct: target.pct,
    ftp,
    ergCapPct,
  });
  const key = commandKey(command);
  const commandRef = useRef(command);
  commandRef.current = command;

  // Send each change to the trainer once
  useEffect(() => {
    if (!commandRef.current || key === lastCommandKey.current) return;
    lastCommandKey.current = key;
    onTrainerCommandRef.current?.(commandRef.current);
  }, [key]);

  const resync = useCallback(() => {
    if (!commandRef.current) return;
    lastCommandKey.current = commandKey(commandRef.current);
    onTrainerCommandRef.current?.(commandRef.current);
  }, []);

  // Ease off instead of pushing: on a work step, power below 70% of target
  // for 20 seconds drops the target to the step's recovery for the rest of
  // it. In ERG that is what stops a tiring rider grinding to a halt.
  const easeOffEligible =
    !!currentStep && !eased && isEaseOffStep(currentStep, ergCapPct);
  useEffect(() => {
    if (status !== "running" || livePower === null || !currentStep) {
      detector.current.reset();
      return;
    }
    if (!easeOffEligible) return;
    if (detector.current.update(Date.now(), livePower, target.watts)) {
      const pct = easeOffPct(currentStep);
      easeOffCount.current += 1;
      setEaseOff({
        id: easeOffCount.current,
        stepIndex: currentStepIndex,
        pct,
        fromWatts: target.watts,
        toWatts: Math.round(pct * ftp),
      });
    }
    // stepElapsedSeconds keeps the check running while power holds steady.
  }, [
    livePower,
    stepElapsedSeconds,
    status,
    currentStep,
    currentStepIndex,
    easeOffEligible,
    target.watts,
    ftp,
  ]);

  const clearTimer = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const tick = useCallback(() => {
    if (!startTime.current || !stepStartTime.current) return;

    const now = Date.now();
    const newTotal = pausedTotalElapsed.current + (now - startTime.current) / 1000;
    const newStepElapsed =
      pausedStepElapsed.current + (now - stepStartTime.current) / 1000;

    setTotalElapsedSeconds(Math.floor(newTotal));
    setStepElapsedSeconds(Math.floor(newStepElapsed));

    // Check step completion
    setCurrentStepIndex((prevIndex) => {
      const step = flatSteps.current[prevIndex];
      if (!step) return prevIndex;

      if (newStepElapsed >= step.durationSeconds) {
        const nextIndex = prevIndex + 1;

        if (nextIndex >= flatSteps.current.length) {
          // Workout complete
          clearTimer();
          setStatus("completed");
          return prevIndex;
        }

        // Advance to next step
        pausedStepElapsed.current = 0;
        stepStartTime.current = Date.now();
        setStepElapsedSeconds(0);
        resetStepTrackers();
        return nextIndex;
      }

      return prevIndex;
    });
  }, [clearTimer, resetStepTrackers]);

  const start = useCallback(() => {
    if (status !== "idle") return;
    setHalted(false);
    startTime.current = Date.now();
    stepStartTime.current = Date.now();
    pausedTotalElapsed.current = 0;
    pausedStepElapsed.current = 0;
    setStatus("running");
    timerRef.current = setInterval(tick, 250); // 4Hz for smooth updates
  }, [status, tick]);

  const pause = useCallback(() => {
    if (status !== "running") return;
    clearTimer();
    if (startTime.current) {
      pausedTotalElapsed.current += (Date.now() - startTime.current) / 1000;
    }
    if (stepStartTime.current) {
      pausedStepElapsed.current += (Date.now() - stepStartTime.current) / 1000;
    }
    startTime.current = null;
    stepStartTime.current = null;
    setStatus("paused");
  }, [status, clearTimer]);

  // Stop pressed. Release first, ask second: the trainer lets go before the
  // confirmation appears, and nothing re-engages it until the rider resumes.
  const halt = useCallback(() => {
    setHalted(true);
    pause();
  }, [pause]);

  const resume = useCallback(() => {
    if (status !== "paused") return;
    setHalted(false);
    startTime.current = Date.now();
    stepStartTime.current = Date.now();
    setStatus("running");
    timerRef.current = setInterval(tick, 250);
  }, [status, tick]);

  const stop = useCallback(() => {
    clearTimer();
    setStatus("completed");
    startTime.current = null;
    stepStartTime.current = null;
  }, [clearTimer]);

  const skipStep = useCallback(() => {
    if (status !== "running" && status !== "paused") return;

    setCurrentStepIndex((prev) => {
      const next = prev + 1;
      if (next >= flatSteps.current.length) {
        clearTimer();
        setStatus("completed");
        return prev;
      }
      pausedStepElapsed.current = 0;
      stepStartTime.current = Date.now();
      setStepElapsedSeconds(0);
      resetStepTrackers();
      return next;
    });
  }, [status, clearTimer, resetStepTrackers]);

  const prevStep = useCallback(() => {
    if (status !== "running" && status !== "paused") return;

    setCurrentStepIndex((prev) => {
      // If we're more than 3 seconds into the current step, restart it
      // Otherwise go to the previous step
      const stepElapsed =
        pausedStepElapsed.current +
        (stepStartTime.current
          ? (Date.now() - stepStartTime.current) / 1000
          : 0);

      if (stepElapsed > 3 || prev === 0) {
        // Restart current step
        pausedStepElapsed.current = 0;
        stepStartTime.current = Date.now();
        setStepElapsedSeconds(0);
        resetStepTrackers();
        return prev;
      }

      // Go to previous step
      pausedStepElapsed.current = 0;
      stepStartTime.current = Date.now();
      setStepElapsedSeconds(0);
      resetStepTrackers();
      return prev - 1;
    });
  }, [status, resetStepTrackers]);

  // Cleanup
  useEffect(() => {
    return () => clearTimer();
  }, [clearTimer]);

  const state: SessionState = {
    status,
    currentStepIndex,
    stepElapsedSeconds,
    totalElapsedSeconds,
    currentTargetWatts: target.watts,
    currentTargetPct: target.pct,
    currentCadenceTarget: currentStep?.cadenceTarget ?? null,
    steps: flatSteps.current,
    totalDurationSeconds: totalDuration,
    trainerCommand: command,
    ergReleased: command?.kind === "release" && command.reason === "sprint",
    easeOff: eased,
    easeOffEligible,
  };

  const actions: SessionActions = {
    start,
    pause,
    resume,
    stop,
    skipStep,
    prevStep,
    halt,
    resync,
  };

  return [state, actions];
}
