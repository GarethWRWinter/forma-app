"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  selectMessage,
  type CoachTrigger,
  type CoachContext,
} from "@/lib/coachMessages";
import { getZoneFromPct } from "@/lib/trainingZones";
import { chat } from "@/lib/api";
import type {
  EaseOffEvent,
  SessionStep,
  SessionStatus,
} from "@/hooks/useTrainingSession";

interface UseCoachRadioOptions {
  status: SessionStatus;
  currentStepIndex: number;
  stepElapsedSeconds: number;
  totalElapsedSeconds: number;
  currentTargetWatts: number;
  currentTargetPct: number;
  currentStep: SessionStep | undefined;
  nextStep: SessionStep | undefined;
  ftp: number;
  livePower: number;
  enabled: boolean;
  /** Any step at or above 105% of FTP, or a VO2max or sprint session. */
  hardSession: boolean;
  /** The trainer is connected and taking ERG targets, so pausing really
      does take the resistance off. */
  trainerControlled: boolean;
  /** Sprint above the ERG cap: the number is the rider's to chase, so no
      "too hot" or "too low" calls. */
  ergReleased: boolean;
  /** The current step eases off on its own when power drops, so the
      low-power call comes from that instead of the deviation check. */
  easeOffEligible: boolean;
  /** Latest ease-off; a new id plays the low-power cue once. */
  easeOff: EaseOffEvent | null;
}

export interface UseCoachRadioReturn {
  currentMessage: string | null;
  muted: boolean;
  setMuted: (v: boolean) => void;
}

const MESSAGE_DISPLAY_DURATION = 8000; // ms
const MIN_MESSAGE_GAP = 15000; // ms between messages
const POWER_DEVIATION_THRESHOLD_HIGH = 1.1; // 110% of target
const POWER_DEVIATION_THRESHOLD_LOW = 0.85; // 85% of target
const POWER_DEVIATION_HOLD_SECONDS = 10; // how long deviation must persist
const LONG_EFFORT_INTERVAL = 150; // seconds between form checks

export function useCoachRadio(options: UseCoachRadioOptions): UseCoachRadioReturn {
  const {
    status,
    currentStepIndex,
    stepElapsedSeconds,
    totalElapsedSeconds,
    currentTargetWatts,
    currentTargetPct,
    currentStep,
    nextStep,
    ftp,
    livePower,
    enabled,
    hardSession,
    trainerControlled,
    ergReleased,
    easeOffEligible,
    easeOff,
  } = options;

  const [currentMessage, setCurrentMessage] = useState<string | null>(null);
  const [muted, setMuted] = useState(false);

  // Refs for tracking state between renders
  const lastMessageTime = useRef(0);
  const lastStepIndex = useRef(-1);
  const preStepFired = useRef(false);
  const midpointFired = useRef(false);
  const workoutStartFired = useRef(false);
  const workoutCompleteFired = useRef(false);
  const longEffortLastCheck = useRef(0);
  const recentTemplates = useRef<string[]>([]);
  const messageTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const prevStatus = useRef<SessionStatus>("idle");
  const lastEaseOffId = useRef(0);

  // Power deviation tracking (3-second rolling average)
  const powerReadings = useRef<number[]>([]);
  const deviationStart = useRef<number | null>(null);
  const lastDeviationTrigger = useRef(0);

  const clearMessage = useCallback(() => {
    if (messageTimer.current) {
      clearTimeout(messageTimer.current);
      messageTimer.current = null;
    }
    setCurrentMessage(null);
  }, []);

  const showMessage = useCallback(
    (trigger: CoachTrigger, context: CoachContext, priority = false) => {
      const now = Date.now();

      // Anti-spam: minimum gap between messages. Safety calls (pause, ease
      // off) explain what the trainer just did, so they never wait.
      if (!priority && now - lastMessageTime.current < MIN_MESSAGE_GAP) return;

      const msg = selectMessage(trigger, context, recentTemplates.current);
      if (!msg) return;

      // Track recent messages (keep last 10)
      recentTemplates.current = [...recentTemplates.current.slice(-9), msg];
      lastMessageTime.current = now;

      setCurrentMessage(msg);

      // TTS via ElevenLabs (same voice as Coach Forma chat)
      if (!muted) {
        // Stop any currently playing audio
        if (audioRef.current) {
          audioRef.current.pause();
          audioRef.current = null;
        }

        // Fire-and-forget: fetch TTS audio from backend
        chat
          .textToSpeech(msg)
          .then((blob) => {
            if (!blob || blob.size === 0) return;
            const url = URL.createObjectURL(blob);
            const audio = new Audio(url);
            audio.volume = 0.9;
            audioRef.current = audio;
            audio.onended = () => {
              URL.revokeObjectURL(url);
              audioRef.current = null;
            };
            audio.play().catch(() => {
              // Browser may block autoplay before user interaction
              URL.revokeObjectURL(url);
              audioRef.current = null;
            });
          })
          .catch(() => {
            // ElevenLabs unavailable: fail silently, text still shows
          });
      }

      // Auto-clear
      if (messageTimer.current) clearTimeout(messageTimer.current);
      messageTimer.current = setTimeout(() => {
        setCurrentMessage(null);
        messageTimer.current = null;
      }, MESSAGE_DISPLAY_DURATION);
    },
    [muted]
  );

  const buildContext = useCallback((): CoachContext => {
    const zone = getZoneFromPct(currentTargetPct);
    const nextZone = nextStep
      ? getZoneFromPct(nextStep.powerTargetPct)
      : undefined;
    const stepRemaining = currentStep
      ? Math.max(0, currentStep.durationSeconds - stepElapsedSeconds)
      : 0;

    return {
      stepType: currentStep?.stepType || "steady_state",
      zoneName: zone.name,
      zoneNumber: zone.zone,
      targetWatts: currentTargetWatts,
      targetPct: currentTargetPct,
      stepDuration: currentStep?.durationSeconds || 0,
      stepElapsed: stepElapsedSeconds,
      stepRemaining,
      cadenceTarget: currentStep?.cadenceTarget ?? null,
      intervalIndex: currentStep?.repeatIndex,
      intervalTotal: currentStep?.repeatTotal,
      totalElapsed: totalElapsedSeconds,
      totalRemaining: 0, // not critical for messages
      nextStepType: nextStep?.stepType,
      nextZoneName: nextZone?.name,
      nextTargetWatts: nextStep
        ? Math.round(nextStep.powerTargetPct * ftp)
        : undefined,
    };
  }, [
    currentTargetPct,
    currentTargetWatts,
    currentStep,
    nextStep,
    stepElapsedSeconds,
    totalElapsedSeconds,
    ftp,
  ]);

  // === Trigger: workout_start (hard sessions get their own pool), pause ===
  useEffect(() => {
    if (
      status === "running" &&
      prevStatus.current === "idle" &&
      !workoutStartFired.current &&
      enabled
    ) {
      workoutStartFired.current = true;
      const trigger: CoachTrigger = hardSession ? "workout_start_hard" : "workout_start";
      // Small delay so UI settles
      setTimeout(() => showMessage(trigger, buildContext()), 1500);
    }
    if (
      status === "paused" &&
      prevStatus.current === "running" &&
      trainerControlled &&
      enabled
    ) {
      showMessage("pause", buildContext(), true);
    }
    prevStatus.current = status;
  }, [status, enabled, hardSession, trainerControlled, showMessage, buildContext]);

  // === Trigger: ease off (power below 70% of target for 20s on a work step) ===
  useEffect(() => {
    if (!enabled || !easeOff || easeOff.id === lastEaseOffId.current) return;
    lastEaseOffId.current = easeOff.id;
    // Name the number they couldn't hold, not the recovery it dropped to.
    showMessage("power_too_low", { ...buildContext(), targetWatts: easeOff.fromWatts }, true);
  }, [easeOff, enabled, showMessage, buildContext]);

  // === Trigger: workout_complete ===
  useEffect(() => {
    if (status === "completed" && !workoutCompleteFired.current && enabled) {
      workoutCompleteFired.current = true;
      showMessage("workout_complete", buildContext());
    }
  }, [status, enabled, showMessage, buildContext]);

  // === Trigger: step_start (when step index changes) ===
  useEffect(() => {
    if (!enabled || status !== "running") return;

    if (currentStepIndex !== lastStepIndex.current && lastStepIndex.current >= 0) {
      // Reset per-step trackers
      preStepFired.current = false;
      midpointFired.current = false;
      longEffortLastCheck.current = 0;
      deviationStart.current = null;
      powerReadings.current = [];

      // Fire step_start (skip for the very first step: workout_start covers it)
      if (workoutStartFired.current) {
        showMessage("step_start", buildContext());
      }
    }
    lastStepIndex.current = currentStepIndex;
  }, [currentStepIndex, status, enabled, showMessage, buildContext]);

  // === Periodic triggers: pre_step_change, midpoint, long_effort_check, power deviation ===
  useEffect(() => {
    if (!enabled || status !== "running" || !currentStep) return;

    const stepRemaining = Math.max(
      0,
      currentStep.durationSeconds - stepElapsedSeconds
    );

    // pre_step_change: 30s before step ends (only for steps > 45s)
    if (
      !preStepFired.current &&
      stepRemaining <= 30 &&
      stepRemaining > 25 &&
      currentStep.durationSeconds > 45 &&
      nextStep
    ) {
      preStepFired.current = true;
      showMessage("pre_step_change", buildContext());
    }

    // step_midpoint: halfway through intervals > 180s
    if (
      !midpointFired.current &&
      currentStep.durationSeconds > 180 &&
      stepElapsedSeconds >= currentStep.durationSeconds / 2 &&
      stepElapsedSeconds < currentStep.durationSeconds / 2 + 5
    ) {
      midpointFired.current = true;
      showMessage("step_midpoint", buildContext());
    }

    // long_effort_check: every ~2.5min during steady efforts
    if (
      currentStep.durationSeconds > 300 &&
      stepElapsedSeconds - longEffortLastCheck.current >= LONG_EFFORT_INTERVAL &&
      stepElapsedSeconds > 60
    ) {
      longEffortLastCheck.current = stepElapsedSeconds;
      showMessage("long_effort_check", buildContext());
    }

    // Power deviation tracking (not on a released sprint: it's all theirs)
    if (!ergReleased && livePower > 0 && currentTargetWatts > 0) {
      powerReadings.current.push(livePower);
      // Keep last 12 readings (~3s at 4Hz update rate)
      if (powerReadings.current.length > 12) {
        powerReadings.current = powerReadings.current.slice(-12);
      }

      const avgPower =
        powerReadings.current.reduce((a, b) => a + b, 0) /
        powerReadings.current.length;
      const ratio = avgPower / currentTargetWatts;

      const now = Date.now();
      if (
        ratio > POWER_DEVIATION_THRESHOLD_HIGH ||
        ratio < POWER_DEVIATION_THRESHOLD_LOW
      ) {
        if (deviationStart.current === null) {
          deviationStart.current = now;
        } else if (
          (now - deviationStart.current) / 1000 >= POWER_DEVIATION_HOLD_SECONDS &&
          now - lastDeviationTrigger.current > 60000 // max once per minute
        ) {
          lastDeviationTrigger.current = now;
          deviationStart.current = null;
          const trigger: CoachTrigger =
            ratio > 1 ? "power_too_high" : "power_too_low";
          // Low power on a work step is the ease-off's call, made once, with
          // the trainer already backing off. No nagging before it.
          if (trigger === "power_too_high" || !easeOffEligible) {
            showMessage(trigger, buildContext());
          }
        }
      } else {
        deviationStart.current = null;
      }
    }
  }, [
    stepElapsedSeconds,
    status,
    enabled,
    currentStep,
    nextStep,
    livePower,
    currentTargetWatts,
    ergReleased,
    easeOffEligible,
    showMessage,
    buildContext,
  ]);

  // Cleanup
  useEffect(() => {
    return () => {
      if (messageTimer.current) clearTimeout(messageTimer.current);
      if (audioRef.current) {
        audioRef.current.pause();
        audioRef.current = null;
      }
    };
  }, []);

  return { currentMessage, muted, setMuted };
}
