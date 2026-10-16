"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

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
}

export type SessionStatus = "idle" | "running" | "paused" | "completed";

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
}

export interface SessionActions {
  start: () => void;
  pause: () => void;
  resume: () => void;
  stop: () => void;
  skipStep: () => void;
  prevStep: () => void;
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
  // Ramp, warmup and cooldown run from low to high. On a steady step the
  // range is a band to sit in, so it holds its target.
  const ramps =
    step.stepType === "ramp" ||
    step.stepType === "warmup" ||
    step.stepType === "cooldown";
  if (ramps && step.powerLowPct !== undefined && step.powerHighPct !== undefined) {
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
  onTargetPowerChange?: (watts: number) => void
): [SessionState, SessionActions] {
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

  const startTime = useRef<number | null>(null);
  const stepStartTime = useRef<number | null>(null);
  const pausedTotalElapsed = useRef(0);
  const pausedStepElapsed = useRef(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastTargetPower = useRef<number | null>(null);
  const onTargetPowerChangeRef = useRef(onTargetPowerChange);
  onTargetPowerChangeRef.current = onTargetPowerChange;

  const currentStep = flatSteps.current[currentStepIndex];
  const target = currentStep
    ? calculateTargetPower(currentStep, stepElapsedSeconds, ftp)
    : { watts: 0, pct: 0 };

  // Send target power changes to trainer
  useEffect(() => {
    if (
      status === "running" &&
      target.watts !== lastTargetPower.current &&
      onTargetPowerChangeRef.current
    ) {
      lastTargetPower.current = target.watts;
      onTargetPowerChangeRef.current(target.watts);
    }
  }, [status, target.watts]);

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
        lastTargetPower.current = null; // Force target power update
        return nextIndex;
      }

      return prevIndex;
    });
  }, [clearTimer]);

  const start = useCallback(() => {
    if (status !== "idle") return;
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

  const resume = useCallback(() => {
    if (status !== "paused") return;
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
      lastTargetPower.current = null;
      return next;
    });
  }, [status, clearTimer]);

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
        lastTargetPower.current = null;
        return prev;
      }

      // Go to previous step
      pausedStepElapsed.current = 0;
      stepStartTime.current = Date.now();
      setStepElapsedSeconds(0);
      lastTargetPower.current = null;
      return prev - 1;
    });
  }, [status]);

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
  };

  const actions: SessionActions = {
    start,
    pause,
    resume,
    stop,
    skipStep,
    prevStep,
  };

  return [state, actions];
}
