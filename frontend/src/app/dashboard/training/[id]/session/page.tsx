"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import {
  ArrowLeft,
  Play,
  Pause,
  Square,
  SkipForward,
  SkipBack,
  Bluetooth,
  BluetoothConnected,
  Heart,
  Zap,
  Activity,
  Gauge,
  X,
  Check,
  AlertTriangle,
  List,
  ChevronRight,
  Volume2,
  VolumeX,
  Radio,
} from "lucide-react";
import { rides, training } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { formatDuration, cn } from "@/lib/utils";
import { getZoneFromPct } from "@/lib/trainingZones";
import { ZONES, SERIES, ZONE_BLOCKS } from "@/lib/palette";
import { useBluetooth } from "@/hooks/useBluetooth";
import { useCoachRadio } from "@/hooks/useCoachRadio";
import {
  useTelemetryBuffer,
  SESSION_BUFFER_PREFIX,
} from "@/hooks/useTelemetryBuffer";
import {
  useTrainingSession,
  type SessionStep,
} from "@/hooks/useTrainingSession";
import { Button } from "@/components/ui/button";
import { ZoneChip } from "@/components/ui/zone-chip";

/** Zone number to palette hex (the only source of zone colour on carbon). */
function zoneHex(pct: number): string {
  const z = getZoneFromPct(pct).zone;
  return ZONES[`z${z}` as keyof typeof ZONES] ?? ZONES.z1;
}

/** Zone number to the ZoneChip key used by the kit. */
const ZONE_CHIP_KEYS: Record<number, string> = {
  1: "recovery",
  2: "endurance",
  3: "tempo",
  4: "threshold",
  5: "vo2max",
  6: "sprint",
  7: "sprint",
};

function formatTime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0)
    return `${h}:${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

// Auto-pause/resume timeout (seconds with no power before auto-pausing)
const AUTO_PAUSE_DELAY = 15;
// Minimum power reading to consider "riding"
const MIN_POWER_THRESHOLD = 10;

export default function TrainingSessionPage() {
  const params = useParams();
  const router = useRouter();
  const workoutId = params.id as string;
  const { user } = useAuth();
  // Ride mode commands real resistance on a real trainer. An invented FTP here
  // is not a cosmetic error: it locks the rider to watts we made up. No FTP
  // means no ride mode, and we say so instead of guessing.
  const ftp = user?.ftp ?? null;
  const coachName = user?.coach_name || "Forma";
  const [showDevicePanel, setShowDevicePanel] = useState(false);
  const [showConfirmStop, setShowConfirmStop] = useState(false);
  const [showStepList, setShowStepList] = useState(true);
  const [autoPaused, setAutoPaused] = useState(false);
  const [deviceSetupDone, setDeviceSetupDone] = useState(false);
  const [autoPauseCountdown, setAutoPauseCountdown] = useState<number | null>(
    null
  );

  // Save flow state
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [resumePrompt, setResumePrompt] = useState<{
    startedAt: string;
    dataPointCount: number;
  } | null>(null);
  const startedSavingRef = useRef(false);

  const { data: workout, isLoading } = useQuery({
    queryKey: ["workout", workoutId],
    queryFn: () => training.getWorkout(workoutId),
  });

  const [btState, btActions] = useBluetooth();

  const handleTargetPowerChange = useCallback(
    async (watts: number) => {
      // Belt and braces: the page refuses to start without an FTP, but never
      // let a target derived from a missing one reach the trainer.
      if (!ftp) return;
      if (btState.trainer.connected && btState.trainer.ergMode) {
        await btActions.setTargetPower(watts);
      }
    },
    [ftp, btState.trainer.connected, btState.trainer.ergMode, btActions]
  );

  const [session, sessionActions] = useTrainingSession(
    workout?.steps || [],
    ftp ?? 0,
    handleTargetPowerChange
  );

  // === Telemetry buffering ===
  const buffer = useTelemetryBuffer({
    workoutId,
    elapsedSeconds: session.totalElapsedSeconds,
    btState,
    running: session.status === "running",
  });

  // Initialise buffer when the session leaves "idle" for the first time.
  // The buffer hook also lazy-inits on first sample, but doing it here keeps
  // startedAt tied to the user's actual "Start" click rather than the first
  // BLE tick.
  const sessionStartedRef = useRef(false);
  useEffect(() => {
    if (session.status !== "idle" && !sessionStartedRef.current) {
      sessionStartedRef.current = true;
      buffer.start();
    }
  }, [session.status, buffer]);

  // Check for a resumable buffered session on mount.
  useEffect(() => {
    if (!buffer.hasStored()) return;
    if (session.status !== "idle") return;
    const stored = window.localStorage.getItem(
      `${SESSION_BUFFER_PREFIX}${workoutId}`
    );
    if (!stored) return;
    try {
      const parsed = JSON.parse(stored) as {
        startedAt: string;
        dataPoints: unknown[];
      };
      setResumePrompt({
        startedAt: parsed.startedAt,
        dataPointCount: parsed.dataPoints?.length ?? 0,
      });
    } catch {
      // ignore: corrupted entry, will be overwritten on next start
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // === Save flow ===
  const saveSession = useCallback(async () => {
    if (startedSavingRef.current) return;
    startedSavingRef.current = true;
    setSaving(true);
    setSaveError(null);

    try {
      buffer.flush();
      const dataPoints = buffer.getBuffer();
      const startedAt = buffer.getStartTime() ?? new Date();

      if (dataPoints.length === 0) {
        // Nothing to save: discard quietly and bail
        buffer.clear();
        router.push(`/dashboard/training/${workoutId}`);
        return;
      }

      const ride = await rides.record({
        title: workout?.title || "Indoor Session",
        ride_date: startedAt.toISOString(),
        workout_id: workoutId,
        data_points: dataPoints,
      });

      buffer.clear();
      router.push(`/dashboard/rides/${ride.id}`);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Unknown error";
      setSaveError(msg);
      startedSavingRef.current = false;
    } finally {
      setSaving(false);
    }
  }, [buffer, router, workout?.title, workoutId]);

  // Auto-save when the session naturally completes.
  useEffect(() => {
    if (session.status === "completed" && !startedSavingRef.current) {
      void saveSession();
    }
  }, [session.status, saveSession]);

  // === Coach Radio ===
  const currentStep_ = session.steps[session.currentStepIndex];
  const nextStep_ = session.steps[session.currentStepIndex + 1];
  const coach = useCoachRadio({
    status: session.status,
    currentStepIndex: session.currentStepIndex,
    stepElapsedSeconds: session.stepElapsedSeconds,
    totalElapsedSeconds: session.totalElapsedSeconds,
    currentTargetWatts: session.currentTargetWatts,
    currentTargetPct: session.currentTargetPct,
    currentStep: currentStep_,
    nextStep: nextStep_,
    ftp: ftp ?? 0,
    livePower: btState.power.value ?? 0,
    enabled: true,
  });

  // === Auto-pause when no power detected ===
  const lastPowerTime = useRef<number>(Date.now());
  const autoPauseTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  // Track when power is being recorded
  useEffect(() => {
    if (btState.power.value && btState.power.value > MIN_POWER_THRESHOLD) {
      lastPowerTime.current = Date.now();
      setAutoPauseCountdown(null);

      // Auto-resume if we were auto-paused
      if (autoPaused && session.status === "paused") {
        sessionActions.resume();
        setAutoPaused(false);
      }
    }
  }, [btState.power.value, autoPaused, session.status, sessionActions]);

  // Check for power dropout periodically
  useEffect(() => {
    if (
      session.status !== "running" ||
      !btState.power.connected
    ) {
      if (autoPauseTimer.current) {
        clearInterval(autoPauseTimer.current);
        autoPauseTimer.current = null;
      }
      setAutoPauseCountdown(null);
      return;
    }

    autoPauseTimer.current = setInterval(() => {
      const elapsed = (Date.now() - lastPowerTime.current) / 1000;
      if (elapsed >= AUTO_PAUSE_DELAY) {
        // Auto-pause
        sessionActions.pause();
        setAutoPaused(true);
        setAutoPauseCountdown(null);
      } else if (elapsed >= 5) {
        // Show countdown
        setAutoPauseCountdown(Math.ceil(AUTO_PAUSE_DELAY - elapsed));
      } else {
        setAutoPauseCountdown(null);
      }
    }, 1000);

    return () => {
      if (autoPauseTimer.current) {
        clearInterval(autoPauseTimer.current);
        autoPauseTimer.current = null;
      }
    };
  }, [session.status, btState.power.connected, sessionActions]);

  // Prevent accidental navigation
  useEffect(() => {
    if (session.status === "running" || session.status === "paused") {
      const handleBeforeUnload = (e: BeforeUnloadEvent) => {
        e.preventDefault();
      };
      window.addEventListener("beforeunload", handleBeforeUnload);
      return () =>
        window.removeEventListener("beforeunload", handleBeforeUnload);
    }
  }, [session.status]);

  // Cleanup trainer on unmount
  useEffect(() => {
    return () => {
      btActions.disconnectAll();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Scroll active step into view in step list
  const activeStepRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (activeStepRef.current) {
      activeStepRef.current.scrollIntoView({
        behavior: "smooth",
        block: "center",
      });
    }
  }, [session.currentStepIndex]);

  if (isLoading) {
    return (
      <div className="f-carbon flex h-screen items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-vb-red border-t-transparent" />
      </div>
    );
  }

  if (!workout || !workout.steps || workout.steps.length === 0) {
    return (
      <div className="f-carbon flex h-screen flex-col items-center justify-center text-vb-chalk-dim">
        <p>That workout has no steps to ride.</p>
        <Link
          href={`/dashboard/training/${workoutId}`}
          className="mt-4 font-mono text-xs uppercase tracking-[0.08em] text-vb-red"
        >
          Back to workout
        </Link>
      </div>
    );
  }

  if (!ftp) {
    return (
      <div className="f-carbon flex h-screen flex-col items-center justify-center px-8 text-center text-vb-chalk-dim">
        <p className="max-w-sm text-vb-chalk">
          Ride mode sets the resistance on your trainer, so it needs your real
          FTP. I will not guess a number and then hold you to it.
        </p>
        <p className="mt-3 max-w-sm text-sm">
          Add it in settings, or ride the session on feel and upload the file
          afterwards. Either way I will find your FTP from the road soon enough.
        </p>
        <div className="mt-6 flex flex-wrap items-center justify-center gap-4">
          <Link
            href="/dashboard/settings"
            className="font-mono text-xs uppercase tracking-[0.08em] text-vb-red"
          >
            Set your FTP
          </Link>
          <Link
            href={`/dashboard/training/${workoutId}`}
            className="font-mono text-xs uppercase tracking-[0.08em] text-vb-chalk-dim"
          >
            Back to workout
          </Link>
        </div>
      </div>
    );
  }

  const currentStep = session.steps[session.currentStepIndex];
  const nextStep = session.steps[session.currentStepIndex + 1];
  const stepRemaining = currentStep
    ? Math.max(0, currentStep.durationSeconds - session.stepElapsedSeconds)
    : 0;
  const totalRemaining = Math.max(
    0,
    session.totalDurationSeconds - session.totalElapsedSeconds
  );

  // Calculate progress
  const elapsedBeforeCurrentStep = session.steps
    .slice(0, session.currentStepIndex)
    .reduce((sum, s) => sum + s.durationSeconds, 0);
  const workoutProgress =
    ((elapsedBeforeCurrentStep + session.stepElapsedSeconds) /
      session.totalDurationSeconds) *
    100;

  // Live metrics
  const livePower = btState.power.value ?? 0;
  const liveHR = btState.heartRate.value ?? 0;
  const liveCadence = btState.cadence.value ?? 0;

  const connectedDeviceCount = [
    btState.heartRate.connected,
    btState.power.connected,
    btState.cadence.connected,
    btState.trainer.connected,
  ].filter(Boolean).length;

  const isActive =
    session.status === "running" || session.status === "paused";

  const currentHex = currentStep ? zoneHex(currentStep.powerTargetPct) : ZONES.z1;

  return (
    <div className="f-carbon fixed inset-0 z-50 flex flex-col">
      {/* Top Bar. On a phone this row holds four controls plus two clocks, which
          will not fit in 375px, so below md the button labels drop to icons and
          the clocks stand down (the metrics strip below already carries them). */}
      <div className="flex items-center justify-between gap-2 border-b border-white/10 px-3 py-3 md:px-6">
        <div className="flex min-w-0 items-center gap-2 md:gap-4">
          {session.status === "idle" && (
            <Link
              href={`/dashboard/training/${workoutId}`}
              className="text-vb-chalk-dim hover:text-vb-red"
            >
              <ArrowLeft className="h-5 w-5" />
            </Link>
          )}
          {session.status === "running" && (
            <span className="flex items-center gap-2">
              <span
                aria-hidden="true"
                className="f-pulse-dot h-2 w-2 rounded-full bg-vb-red"
              />
              <span className="f-kicker text-vb-red">Live</span>
            </span>
          )}
          <div className="min-w-0">
            <h1 className="f-display truncate text-base text-vb-chalk md:text-lg">
              {workout.title}
            </h1>
            <p className="hidden truncate text-xs text-vb-chalk-dim md:block">
              {workout.description}
            </p>
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-1.5 md:gap-3">
          {/* Step list toggle */}
          {isActive && (
            <button
              onClick={() => setShowStepList(!showStepList)}
              className={cn(
                "f-press flex items-center gap-1.5 rounded-sm border px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-[0.08em] transition-colors",
                showStepList
                  ? "border-white/20 bg-vb-carbon-raised text-vb-chalk"
                  : "border-white/10 text-vb-chalk-dim hover:text-vb-chalk"
              )}
            >
              <List className="h-4 w-4" />
              <span className="hidden md:inline">Steps</span>
            </button>
          )}

          {/* Coach Radio mute toggle */}
          {isActive && (
            <button
              onClick={() => coach.setMuted(!coach.muted)}
              className={cn(
                "f-press flex items-center gap-1.5 rounded-sm border px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-[0.08em] transition-colors",
                coach.muted
                  ? "border-white/10 text-vb-chalk-dim hover:text-vb-chalk"
                  : "border-vb-red/40 bg-vb-carbon-raised text-vb-red"
              )}
              title={coach.muted ? "Unmute Race Radio" : "Mute Race Radio"}
            >
              {coach.muted ? (
                <VolumeX className="h-4 w-4" />
              ) : (
                <Volume2 className="h-4 w-4" />
              )}
              <span className="hidden md:inline">Radio</span>
            </button>
          )}

          {/* Device connection button */}
          <button
            onClick={() => setShowDevicePanel(!showDevicePanel)}
            className={cn(
              "f-press flex items-center gap-1.5 rounded-sm border px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-[0.08em] transition-colors",
              connectedDeviceCount > 0
                ? "border-white/20 bg-vb-carbon-raised text-vb-chalk"
                : "border-white/10 text-vb-chalk-dim hover:text-vb-chalk"
            )}
          >
            {connectedDeviceCount > 0 ? (
              <BluetoothConnected className="h-4 w-4" />
            ) : (
              <Bluetooth className="h-4 w-4" />
            )}
            <span className="hidden md:inline">
              {connectedDeviceCount > 0
                ? `${connectedDeviceCount} device${connectedDeviceCount > 1 ? "s" : ""}`
                : "Connect"}
            </span>
            {connectedDeviceCount > 0 && (
              <span className="md:hidden">{connectedDeviceCount}</span>
            )}
          </button>

          {/* Elapsed / Remaining time. Hidden on phones: the metrics strip
              below carries both clocks, and this row has no width to spare. */}
          <div className="hidden items-center gap-4 text-sm md:flex">
            <div className="text-center">
              <p className="f-kicker text-[9px] text-vb-chalk-dim">Elapsed</p>
              <p className="f-data font-semibold text-vb-chalk">
                {formatTime(session.totalElapsedSeconds)}
              </p>
            </div>
            <div className="text-center">
              <p className="f-kicker text-[9px] text-vb-chalk-dim">Remaining</p>
              <p className="f-data text-vb-chalk-dim">
                {formatTime(totalRemaining)}
              </p>
            </div>
          </div>
        </div>
      </div>

      {/* Workout Progress Bar */}
      <div className="relative h-12 border-b border-white/10">
        <div className="flex h-full">
          {session.steps.map((step, idx) => {
            const widthPct =
              (step.durationSeconds / session.totalDurationSeconds) * 100;
            const isActiveStep = idx === session.currentStepIndex;
            const isDone = idx < session.currentStepIndex;
            const hex = zoneHex(step.powerTargetPct);

            return (
              <div
                key={idx}
                className="relative flex items-center justify-center overflow-hidden border-r border-white/10 transition-all"
                style={{ width: `${widthPct}%`, minWidth: "2px" }}
              >
                <div
                  className="absolute inset-0"
                  style={{
                    backgroundColor: hex,
                    opacity: isDone ? 0.35 : isActiveStep ? 0.25 : 0.12,
                  }}
                />
                {/* Active step progress fill */}
                {isActiveStep && session.status !== "idle" && (
                  <div
                    className="absolute inset-y-0 left-0"
                    style={{
                      backgroundColor: hex,
                      opacity: 0.85,
                      width: `${Math.min(100, (session.stepElapsedSeconds / step.durationSeconds) * 100)}%`,
                    }}
                  />
                )}
                {widthPct > 3 && (
                  <span
                    className={cn(
                      "relative z-10 truncate px-0.5 font-mono text-[9px] font-semibold uppercase tracking-[0.08em]",
                      isDone || isActiveStep ? "text-white" : "text-vb-chalk-dim"
                    )}
                  >
                    {step.stepType === "interval_on"
                      ? "ON"
                      : step.stepType === "interval_off"
                        ? "OFF"
                        : widthPct > 6
                          ? `Z${getZoneFromPct(step.powerTargetPct).zone}`
                          : ""}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Main Content */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left Panel: Step List (visible during session) */}
        {isActive && showStepList && (
          <div className="w-72 overflow-y-auto border-r border-white/10">
            <div className="border-b border-white/10 px-4 py-3">
              <h3 className="f-kicker text-vb-chalk-dim">The steps</h3>
            </div>
            <div className="divide-y divide-white/5">
              {session.steps.map((step, idx) => {
                const isCurrentStep = idx === session.currentStepIndex;
                const isDone = idx < session.currentStepIndex;
                const targetW = Math.round(step.powerTargetPct * ftp);
                const stepZone = getZoneFromPct(step.powerTargetPct);
                const hex = zoneHex(step.powerTargetPct);

                return (
                  <div
                    key={idx}
                    ref={isCurrentStep ? activeStepRef : undefined}
                    className={cn(
                      "flex items-center gap-3 px-4 py-2.5 transition-colors",
                      isCurrentStep
                        ? "border-l-2 border-l-vb-red bg-white/5"
                        : isDone
                          ? "opacity-40"
                          : "opacity-70"
                    )}
                  >
                    {/* Step colour indicator */}
                    <div
                      className={cn(
                        "h-6 w-1 shrink-0 rounded-full",
                        isDone && "opacity-50"
                      )}
                      style={{ backgroundColor: hex }}
                    />

                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <span
                          className={cn(
                            "font-mono text-[11px] font-semibold uppercase tracking-[0.08em]",
                            isCurrentStep ? "text-vb-chalk" : "text-vb-chalk-dim"
                          )}
                        >
                          {step.stepType.replace("_", " ")}
                        </span>
                        {step.isInterval && (
                          <span className="f-data text-[9px] text-vb-chalk-dim">
                            {step.repeatIndex}/{step.repeatTotal}
                          </span>
                        )}
                        <span
                          className="rounded-sm px-1 py-0.5 font-mono text-[9px] font-semibold text-white"
                          style={{ backgroundColor: hex }}
                        >
                          Z{stepZone.zone}
                        </span>
                      </div>
                      {step.notes && (
                        <p className="truncate text-[10px] text-vb-chalk-dim">
                          {step.notes}
                        </p>
                      )}
                    </div>

                    <div className="shrink-0 text-right">
                      <p
                        className={cn(
                          "f-data text-xs font-semibold",
                          isCurrentStep ? "text-vb-chalk" : "text-vb-chalk-dim"
                        )}
                      >
                        {targetW}W
                      </p>
                      <p className="f-data text-[10px] text-vb-chalk-dim">
                        {formatTime(step.durationSeconds)}
                      </p>
                    </div>

                    {/* Current step indicator */}
                    {isCurrentStep && (
                      <ChevronRight className="h-3 w-3 shrink-0 text-vb-red" />
                    )}
                    {isDone && (
                      <Check className="h-3 w-3 shrink-0 text-vb-chalk-dim" />
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Center: Current Step & Controls */}
        {/* The running stack is taller than a phone screen. Centring it inside
            an overflow-hidden parent pushed the stop/pause controls off both
            ends with no way to reach them, so on small screens this column
            starts at the top and scrolls. It centres again from md up. */}
        <div className="flex flex-1 flex-col items-center justify-start gap-4 overflow-y-auto p-4 md:justify-center md:gap-8 md:overflow-visible md:p-8">
          {/* Auto-pause banner */}
          {autoPaused && session.status === "paused" && (
            <div className="flex items-center gap-3 rounded-sm border border-vb-warning/40 bg-vb-carbon-raised px-5 py-3">
              <Pause className="h-5 w-5 shrink-0 text-vb-warning" />
              <div>
                <p className="text-sm font-medium text-vb-warning">
                  Auto-paused, no power detected
                </p>
                <p className="text-xs text-vb-chalk-dim">
                  Start pedalling and we pick up where we left off
                </p>
              </div>
            </div>
          )}

          {/* Auto-pause countdown */}
          {autoPauseCountdown !== null && session.status === "running" && (
            <div className="flex items-center gap-2 font-mono text-xs text-vb-warning">
              <AlertTriangle className="h-3 w-3" />
              <span>
                No power, auto-pausing in {autoPauseCountdown}s
              </span>
            </div>
          )}

          {/* Current Step Info */}
          {session.status === "idle" && !deviceSetupDone ? (
            /* === Device Setup Screen === */
            <div className="f-rise w-full max-w-lg text-center">
              <h2 className="f-display mb-1 text-3xl text-vb-chalk">
                {workout.title}
              </h2>
              <p className="f-data mb-8 text-sm text-vb-chalk-dim">
                {formatDuration(session.totalDurationSeconds)} &middot;{" "}
                {session.steps.length} steps
              </p>

              <div className="mb-6 overflow-hidden rounded-sm border border-white/10 bg-vb-carbon-raised">
                <div className="border-b border-white/10 px-5 py-3">
                  <h3 className="f-kicker flex items-center gap-2 text-vb-chalk-dim">
                    <Bluetooth className="h-4 w-4 text-vb-red" />
                    Connect your devices
                  </h3>
                </div>

                {!btState.isSupported ? (
                  <div className="p-5">
                    <div className="flex items-center gap-3 rounded-sm border border-vb-warning/40 bg-vb-warning/10 p-4">
                      <AlertTriangle className="h-5 w-5 shrink-0 text-vb-warning" />
                      <div className="text-left">
                        <p className="text-sm font-medium text-vb-warning">
                          Bluetooth isn&apos;t available in this browser
                        </p>
                        <p className="mt-1 text-xs text-vb-chalk-dim">
                          Use <strong>Chrome</strong>, <strong>Edge</strong>, or
                          <strong> Brave</strong> on a Mac, PC, or Android.{" "}
                          <strong>iPhone and iPad aren&apos;t supported</strong>:
                          every iOS browser (including Chrome) runs on Safari&apos;s
                          engine, which doesn&apos;t have Web Bluetooth. Native
                          iOS app coming soon.
                        </p>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="divide-y divide-white/5">
                    <DeviceRow
                      icon={<Zap className="h-5 w-5 text-vb-chalk" />}
                      label="Power Meter"
                      connected={btState.power.connected}
                      name={btState.power.name}
                      value={btState.power.value ? `${btState.power.value}W` : undefined}
                      onConnect={btActions.connectPower}
                      onDisconnect={() => btActions.disconnectDevice("power")}
                    />
                    <DeviceRow
                      icon={<Heart className="h-5 w-5 text-vb-chalk" />}
                      label="Heart Rate Monitor"
                      connected={btState.heartRate.connected}
                      name={btState.heartRate.name}
                      value={btState.heartRate.value ? `${btState.heartRate.value} bpm` : undefined}
                      onConnect={btActions.connectHeartRate}
                      onDisconnect={() => btActions.disconnectDevice("heartRate")}
                    />
                    <DeviceRow
                      icon={<Gauge className="h-5 w-5 text-vb-chalk" />}
                      label="Smart Trainer (ERG)"
                      description="Holds the target watts for you"
                      connected={btState.trainer.connected}
                      name={btState.trainer.name}
                      value={btState.trainer.connected ? (btState.trainer.ergMode ? "ERG Mode" : "Connected") : undefined}
                      onConnect={btActions.connectTrainer}
                      onDisconnect={() => btActions.disconnectDevice("trainer")}
                    />
                    <DeviceRow
                      icon={<Activity className="h-5 w-5 text-vb-chalk" />}
                      label="Cadence Sensor"
                      description={btState.trainer.connected ? "Available from trainer" : undefined}
                      connected={btState.cadence.connected}
                      name={btState.cadence.name}
                      value={btState.cadence.value ? `${btState.cadence.value} rpm` : undefined}
                      onConnect={btActions.connectCadence}
                      onDisconnect={() => btActions.disconnectDevice("cadence")}
                      fromTrainer={btState.cadence.connected && !!btState.cadence.name?.includes("(cadence)")}
                    />
                  </div>
                )}
              </div>

              <Button
                variant="carbon"
                size="lg"
                className="mx-auto border border-white/20"
                onClick={() => setDeviceSetupDone(true)}
              >
                <Play className="h-5 w-5" />
                {connectedDeviceCount > 0 ? "Continue to start" : "Continue without devices"}
              </Button>

              {connectedDeviceCount === 0 && btState.isSupported && (
                <p className="mt-3 text-xs text-vb-chalk-dim">
                  Connect at least a power meter and every second counts
                </p>
              )}
            </div>
          ) : session.status === "idle" && deviceSetupDone ? (
            <div className="f-rise text-center">
              <p className="f-kicker mb-3 text-vb-chalk-dim">Ready when you are</p>
              <h2 className="f-display mb-1 text-4xl text-vb-chalk">
                {workout.title}
              </h2>
              <p className="f-data mb-2 text-sm text-vb-chalk-dim">
                {formatDuration(session.totalDurationSeconds)} &middot;{" "}
                {session.steps.length} steps
              </p>
              {connectedDeviceCount > 0 && (
                <p className="mb-4 font-mono text-xs text-vb-chalk-dim">
                  <BluetoothConnected className="mr-1 inline h-4 w-4 text-vb-red" />
                  {connectedDeviceCount} device{connectedDeviceCount > 1 ? "s" : ""} connected
                </p>
              )}
            </div>
          ) : session.status === "completed" ? (
            <div className="f-rise text-center">
              <div className="mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-full bg-vb-carbon-raised">
                <Check className="h-8 w-8 text-vb-red" />
              </div>
              <h2 className="f-display mb-2 text-4xl text-vb-chalk">
                That&apos;s done. And it counts.
              </h2>
              <p className="f-data mb-6 text-sm text-vb-chalk-dim">
                Total time {formatTime(session.totalElapsedSeconds)}
              </p>
              {coach.currentMessage && (
                <div className="mx-auto mb-6 max-w-md">
                  <div className="rounded-sm border border-white/10 border-l-[3px] border-l-vb-red bg-vb-carbon-raised px-6 py-4 text-left">
                    <div className="mb-2 flex items-center gap-2">
                      <Radio className="h-3 w-3 text-vb-red" />
                      <span className="f-kicker text-vb-red">
                        {coachName} · Race Radio
                      </span>
                    </div>
                    <p className="text-sm italic text-vb-chalk">
                      &ldquo;{coach.currentMessage}&rdquo;
                    </p>
                    <p className="mt-3">
                      <span className="f-signature text-xl leading-none">
                        {coachName}
                      </span>
                    </p>
                  </div>
                </div>
              )}
              <Link
                href={`/dashboard/training/${workoutId}`}
                className="f-press mt-2 inline-flex h-11 items-center gap-2 rounded-sm border border-white/20 px-5 font-mono text-xs font-semibold uppercase tracking-[0.08em] text-vb-chalk transition-colors hover:border-vb-red hover:text-vb-red"
              >
                Back to workout
              </Link>
            </div>
          ) : currentStep ? (
            <>
              {/* Step type label with zone */}
              <div className="text-center">
                <div className="flex items-center justify-center gap-2">
                  <span className="font-mono text-xs font-semibold uppercase tracking-[0.12em] text-vb-chalk-dim">
                    {currentStep.stepType.replace("_", " ")}
                    {currentStep.isInterval &&
                      ` ${currentStep.repeatIndex}/${currentStep.repeatTotal}`}
                  </span>
                  <ZoneChip
                    zone={
                      ZONE_CHIP_KEYS[
                        getZoneFromPct(currentStep.powerTargetPct).zone
                      ]
                    }
                  />
                </div>
                {currentStep.notes && (
                  <p className="mt-2 text-sm text-vb-chalk-dim">
                    {currentStep.notes}
                  </p>
                )}
              </div>

              {/* Target Power: the number is the screen */}
              <div className="text-center">
                <p
                  className="f-data text-7xl font-semibold leading-none sm:text-8xl md:text-9xl"
                  style={{ color: currentHex }}
                >
                  {session.currentTargetWatts}
                  <span className="ml-2 text-3xl font-medium text-vb-chalk-dim">W</span>
                </p>
                <p className="f-data mt-3 text-lg text-vb-chalk-dim">
                  {Math.round(session.currentTargetPct * 100)}% FTP
                </p>
              </div>

              {/* Interval Progress Bar */}
              <div className="w-full max-w-lg">
                {(() => {
                  const progressPct = Math.min(
                    100,
                    (session.stepElapsedSeconds / currentStep.durationSeconds) * 100
                  );
                  return (
                    <div>
                      {/* Main progress bar */}
                      <div className="relative h-6 overflow-hidden rounded-sm bg-white/10">
                        <div
                          className="absolute inset-y-0 left-0 transition-all duration-500"
                          style={{
                            width: `${progressPct}%`,
                            backgroundColor: currentHex,
                          }}
                        />
                        <div className="absolute inset-0 flex items-center justify-between px-3 font-mono text-[11px] font-semibold text-white">
                          <span>{formatTime(session.stepElapsedSeconds)}</span>
                          <span>{formatTime(stepRemaining)}</span>
                        </div>
                      </div>

                      {/* Upcoming steps preview */}
                      {(() => {
                        const upcoming = session.steps.slice(
                          session.currentStepIndex + 1,
                          session.currentStepIndex + 4
                        );
                        if (upcoming.length === 0) return null;
                        const totalUpcoming = upcoming.reduce(
                          (s, st) => s + st.durationSeconds,
                          0
                        );
                        return (
                          <div className="mt-2 flex h-5 gap-1 overflow-hidden">
                            {upcoming.map((s, i) => {
                              const sz = getZoneFromPct(s.powerTargetPct);
                              const shex = zoneHex(s.powerTargetPct);
                              const w = (s.durationSeconds / totalUpcoming) * 100;
                              return (
                                <div
                                  key={i}
                                  className="relative flex items-center justify-center overflow-hidden rounded-sm"
                                  style={{ width: `${w}%`, minWidth: "30px" }}
                                >
                                  <div
                                    className="absolute inset-0"
                                    style={{ backgroundColor: shex, opacity: 0.3 }}
                                  />
                                  <span className="relative z-10 truncate px-1 font-mono text-[9px] font-medium text-vb-chalk">
                                    Z{sz.zone} {formatTime(s.durationSeconds)}
                                  </span>
                                </div>
                              );
                            })}
                          </div>
                        );
                      })()}
                    </div>
                  );
                })()}
              </div>

              {/* Live metrics strip: rider numbers front and centre */}
              <div className="w-full max-w-lg rounded-sm border border-white/10 bg-vb-carbon-raised px-5 py-4">
                <div className="grid grid-cols-5 gap-2">
                  <div className="text-center">
                    <p className="f-kicker text-[9px] text-vb-chalk-dim">
                      Power
                    </p>
                    <p
                      className={cn(
                        "f-data mt-1 text-2xl font-semibold",
                        livePower === 0 && "text-vb-chalk-dim"
                      )}
                      style={
                        livePower > 0
                          ? {
                              color:
                                livePower > session.currentTargetWatts * 1.05
                                  ? SERIES.flamme
                                  : livePower < session.currentTargetWatts * 0.95
                                    ? ZONES.z1
                                    : currentHex,
                            }
                          : undefined
                      }
                    >
                      {livePower}
                      <span className="ml-0.5 text-xs font-normal text-vb-chalk-dim">
                        W
                      </span>
                    </p>
                  </div>
                  <div className="text-center">
                    <p className="f-kicker text-[9px] text-vb-chalk-dim">
                      Heart Rate
                    </p>
                    <p
                      className={cn(
                        "f-data mt-1 text-2xl font-semibold",
                        liveHR === 0 && "text-vb-chalk-dim"
                      )}
                      style={liveHR > 0 ? { color: SERIES.amber } : undefined}
                    >
                      {liveHR}
                      <span className="ml-0.5 text-xs font-normal text-vb-chalk-dim">
                        bpm
                      </span>
                    </p>
                  </div>
                  <div className="text-center">
                    <p className="f-kicker text-[9px] text-vb-chalk-dim">
                      Cadence
                    </p>
                    <p
                      className={cn(
                        "f-data mt-1 text-2xl font-semibold",
                        liveCadence > 0 ? "text-vb-chalk" : "text-vb-chalk-dim"
                      )}
                    >
                      {liveCadence}
                      <span className="ml-0.5 text-xs font-normal text-vb-chalk-dim">
                        rpm
                      </span>
                    </p>
                  </div>
                  <div className="text-center">
                    <p className="f-kicker text-[9px] text-vb-chalk-dim">
                      Elapsed
                    </p>
                    <p className="f-data mt-1 text-2xl font-semibold text-vb-chalk">
                      {formatTime(session.totalElapsedSeconds)}
                    </p>
                  </div>
                  <div className="text-center">
                    <p className="f-kicker text-[9px] text-vb-chalk-dim">
                      Remaining
                    </p>
                    <p className="f-data mt-1 text-2xl font-semibold text-vb-chalk-dim">
                      {formatTime(totalRemaining)}
                    </p>
                  </div>
                </div>
                <div className="mt-3 h-1 rounded-full bg-white/10">
                  <div
                    className="h-full rounded-full bg-vb-red transition-all"
                    style={{ width: `${Math.min(100, workoutProgress)}%` }}
                  />
                </div>
                {btState.trainer.connected && (
                  <p className="f-kicker mt-2.5 text-center text-[9px] text-vb-chalk-dim">
                    ERG active · {btState.trainer.name} · holding{" "}
                    {session.currentTargetWatts}W
                  </p>
                )}
              </div>

              {/* Race Radio message */}
              {coach.currentMessage && (
                <div className="f-rise w-full max-w-lg">
                  <div className="rounded-sm border border-white/10 border-l-[3px] border-l-vb-red bg-vb-carbon-raised px-6 py-3">
                    <div className="mb-1 flex items-center gap-2">
                      <Radio className="h-3 w-3 text-vb-red" />
                      <span className="f-kicker text-vb-red">
                        {coachName} · Race Radio
                      </span>
                    </div>
                    <p className="text-sm italic text-vb-chalk">
                      &ldquo;{coach.currentMessage}&rdquo;
                    </p>
                  </div>
                </div>
              )}

              {/* Next step preview */}
              {nextStep && (
                <div className="flex items-center gap-2 font-mono text-xs text-vb-chalk-dim">
                  <span className="uppercase tracking-[0.08em]">Next</span>
                  {(() => {
                    const nz = getZoneFromPct(nextStep.powerTargetPct);
                    const nhex = zoneHex(nextStep.powerTargetPct);
                    return (
                      <>
                        <span
                          className="rounded-sm px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-white"
                          style={{ backgroundColor: nhex }}
                        >
                          Z{nz.zone} {nextStep.stepType.replace("_", " ")}
                        </span>
                        <span className="f-data">
                          {Math.round(nextStep.powerTargetPct * ftp)}W &middot;{" "}
                          {formatTime(nextStep.durationSeconds)}
                        </span>
                      </>
                    );
                  })()}
                </div>
              )}
            </>
          ) : null}

          {/* Controls */}
          <div className="flex items-center gap-4">
            {session.status === "idle" && (
              <Button variant="flamme" size="lg" onClick={sessionActions.start}>
                <Play className="h-5 w-5" /> Start workout
              </Button>
            )}

            {session.status === "running" && (
              <>
                <button
                  onClick={sessionActions.prevStep}
                  className="f-press flex h-12 w-12 items-center justify-center rounded-sm border border-white/10 bg-vb-carbon-raised text-vb-chalk transition-colors hover:border-white/30"
                  title="Previous step / restart"
                >
                  <SkipBack className="h-5 w-5" />
                </button>
                <button
                  onClick={sessionActions.pause}
                  className="f-press flex h-14 w-14 items-center justify-center rounded-sm border border-white/10 bg-vb-carbon-raised text-vb-chalk transition-colors hover:border-white/30"
                  title="Pause"
                >
                  <Pause className="h-6 w-6" />
                </button>
                <button
                  onClick={sessionActions.skipStep}
                  className="f-press flex h-12 w-12 items-center justify-center rounded-sm border border-white/10 bg-vb-carbon-raised text-vb-chalk transition-colors hover:border-white/30"
                  title="Skip step"
                >
                  <SkipForward className="h-5 w-5" />
                </button>
                <button
                  onClick={() => setShowConfirmStop(true)}
                  className="f-press flex h-12 w-12 items-center justify-center rounded-sm border border-vb-red/40 bg-vb-carbon-raised text-vb-red transition-colors hover:bg-vb-red hover:text-white"
                  title="Stop"
                >
                  <Square className="h-5 w-5" />
                </button>
              </>
            )}

            {session.status === "paused" && (
              <>
                <button
                  onClick={sessionActions.prevStep}
                  className="f-press flex h-12 w-12 items-center justify-center rounded-sm border border-white/10 bg-vb-carbon-raised text-vb-chalk transition-colors hover:border-white/30"
                  title="Previous step / restart"
                >
                  <SkipBack className="h-5 w-5" />
                </button>
                <Button
                  variant="flamme"
                  size="lg"
                  onClick={() => {
                    sessionActions.resume();
                    setAutoPaused(false);
                  }}
                >
                  <Play className="h-5 w-5" /> Resume
                </Button>
                <button
                  onClick={sessionActions.skipStep}
                  className="f-press flex h-12 w-12 items-center justify-center rounded-sm border border-white/10 bg-vb-carbon-raised text-vb-chalk transition-colors hover:border-white/30"
                  title="Skip step"
                >
                  <SkipForward className="h-5 w-5" />
                </button>
                <button
                  onClick={() => setShowConfirmStop(true)}
                  className="f-press flex h-12 w-12 items-center justify-center rounded-sm border border-vb-red/40 bg-vb-carbon-raised text-vb-red transition-colors hover:bg-vb-red hover:text-white"
                  title="Stop"
                >
                  <Square className="h-5 w-5" />
                </button>
              </>
            )}
          </div>
        </div>

      </div>

      {/* Device Panel Modal */}
      {showDevicePanel && (
        <div className="fixed inset-0 z-60 flex items-center justify-center bg-black/70 backdrop-blur-sm">
          <div className="w-full max-w-md rounded-sm border border-white/10 bg-vb-carbon-raised">
            <div className="flex items-center justify-between border-b border-white/10 px-5 py-4">
              <h2 className="f-display text-lg text-vb-chalk">
                Connect devices
              </h2>
              <button
                onClick={() => setShowDevicePanel(false)}
                className="text-vb-chalk-dim hover:text-vb-red"
              >
                <X className="h-5 w-5" />
              </button>
            </div>

            {!btState.isSupported ? (
              <div className="p-5">
                <div className="flex items-center gap-3 rounded-sm border border-vb-warning/40 bg-vb-warning/10 p-4">
                  <AlertTriangle className="h-5 w-5 shrink-0 text-vb-warning" />
                  <div>
                    <p className="text-sm font-medium text-vb-warning">
                      Bluetooth isn&apos;t available in this browser
                    </p>
                    <p className="mt-1 text-xs text-vb-chalk-dim">
                      Use <strong>Chrome</strong>, <strong>Edge</strong>, or
                      <strong> Brave</strong> on a Mac, PC, or Android.{" "}
                      <strong>iPhone and iPad aren&apos;t supported</strong>:
                      every iOS browser (including Chrome) runs on Safari&apos;s
                      engine, which doesn&apos;t have Web Bluetooth. Native iOS
                      app coming soon.
                    </p>
                  </div>
                </div>
              </div>
            ) : (
              <div className="divide-y divide-white/5">
                <DeviceRow
                  icon={<Heart className="h-5 w-5 text-vb-chalk" />}
                  label="Heart Rate Monitor"
                  connected={btState.heartRate.connected}
                  name={btState.heartRate.name}
                  value={
                    btState.heartRate.value
                      ? `${btState.heartRate.value} bpm`
                      : undefined
                  }
                  onConnect={btActions.connectHeartRate}
                  onDisconnect={() => btActions.disconnectDevice("heartRate")}
                />
                <DeviceRow
                  icon={<Zap className="h-5 w-5 text-vb-chalk" />}
                  label="Power Meter"
                  connected={btState.power.connected}
                  name={btState.power.name}
                  value={
                    btState.power.value
                      ? `${btState.power.value}W`
                      : undefined
                  }
                  onConnect={btActions.connectPower}
                  onDisconnect={() => btActions.disconnectDevice("power")}
                  fromTrainer={
                    btState.power.connected &&
                    !!btState.power.name?.includes("(power)")
                  }
                />
                <DeviceRow
                  icon={<Activity className="h-5 w-5 text-vb-chalk" />}
                  label="Cadence Sensor"
                  description={
                    btState.trainer.connected &&
                    !btState.cadence.name?.includes("Cadence")
                      ? "Available from trainer"
                      : undefined
                  }
                  connected={btState.cadence.connected}
                  name={btState.cadence.name}
                  value={
                    btState.cadence.value
                      ? `${btState.cadence.value} rpm`
                      : undefined
                  }
                  onConnect={btActions.connectCadence}
                  onDisconnect={() => btActions.disconnectDevice("cadence")}
                  fromTrainer={
                    btState.cadence.connected &&
                    !!btState.cadence.name?.includes("(cadence)")
                  }
                />
                <DeviceRow
                  icon={<Gauge className="h-5 w-5 text-vb-chalk" />}
                  label="Smart Trainer (ERG)"
                  description="Holds the target watts for you"
                  connected={btState.trainer.connected}
                  name={btState.trainer.name}
                  value={
                    btState.trainer.connected
                      ? btState.trainer.ergMode
                        ? "ERG Mode"
                        : "Connected"
                      : undefined
                  }
                  onConnect={btActions.connectTrainer}
                  onDisconnect={() => btActions.disconnectDevice("trainer")}
                />
              </div>
            )}

            <div className="border-t border-white/10 px-5 py-3">
              <button
                onClick={() => setShowDevicePanel(false)}
                className="f-press w-full rounded-sm border border-white/20 px-4 py-2 font-mono text-xs font-semibold uppercase tracking-[0.08em] text-vb-chalk transition-colors hover:border-vb-red hover:text-vb-red"
              >
                Done
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Confirm Stop Modal */}
      {showConfirmStop && (
        <div className="fixed inset-0 z-60 flex items-center justify-center bg-black/70 backdrop-blur-sm">
          <div className="w-full max-w-sm rounded-sm border border-white/10 bg-vb-carbon-raised p-6">
            <h3 className="f-display mb-2 text-xl text-vb-chalk">
              End the session?
            </h3>
            <p className="mb-2 text-sm text-vb-chalk-dim">
              You&apos;ve recorded{" "}
              <span className="f-data text-vb-chalk">
                {buffer.getBuffer().length}
              </span>{" "}
              seconds of data so far.
            </p>
            <p className="mb-6 text-sm text-vb-chalk-dim">
              Save the ride, or discard and exit?
            </p>
            {saveError && (
              <div className="mb-4 rounded-sm border border-vb-warning/40 bg-vb-warning/10 px-3 py-2 text-xs text-vb-warning">
                Save failed: {saveError}. Your data is still buffered, try
                again.
              </div>
            )}
            <div className="flex flex-col gap-2">
              <Button
                variant="flamme"
                disabled={saving}
                onClick={async () => {
                  sessionActions.stop();
                  await btActions.stopTrainer();
                  await saveSession();
                  setShowConfirmStop(false);
                }}
              >
                {saving ? "Saving…" : "Save ride & end"}
              </Button>
              <button
                disabled={saving}
                onClick={() => {
                  setShowConfirmStop(false);
                  sessionActions.stop();
                  btActions.stopTrainer();
                  buffer.clear();
                  router.push(`/dashboard/training/${workoutId}`);
                }}
                className="f-press rounded-sm border border-vb-red/40 px-4 py-2 font-mono text-xs font-semibold uppercase tracking-[0.08em] text-vb-red transition-colors hover:bg-vb-red hover:text-white disabled:opacity-50"
              >
                Discard & end
              </button>
              <button
                disabled={saving}
                onClick={() => setShowConfirmStop(false)}
                className="f-press rounded-sm border border-white/20 px-4 py-2 font-mono text-xs font-semibold uppercase tracking-[0.08em] text-vb-chalk-dim transition-colors hover:text-vb-chalk disabled:opacity-50"
              >
                Keep going
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Resume previous session prompt */}
      {resumePrompt && session.status === "idle" && (
        <div className="fixed inset-0 z-60 flex items-center justify-center bg-black/70 backdrop-blur-sm">
          <div className="w-full max-w-sm rounded-sm border border-white/10 bg-vb-carbon-raised p-6">
            <h3 className="f-display mb-2 text-xl text-vb-chalk">
              Unfinished session found
            </h3>
            <p className="mb-2 text-sm text-vb-chalk-dim">
              You have a buffered session for this workout from{" "}
              {new Date(resumePrompt.startedAt).toLocaleString()} with{" "}
              <span className="f-data text-vb-chalk">
                {resumePrompt.dataPointCount}
              </span>{" "}
              seconds of data.
            </p>
            <p className="mb-6 text-sm text-vb-chalk-dim">
              Save what you recorded, or discard and start fresh?
            </p>
            {saveError && (
              <div className="mb-4 rounded-sm border border-vb-warning/40 bg-vb-warning/10 px-3 py-2 text-xs text-vb-warning">
                Save failed: {saveError}
              </div>
            )}
            <div className="flex flex-col gap-2">
              <Button
                variant="flamme"
                disabled={saving}
                onClick={async () => {
                  buffer.restore();
                  await saveSession();
                  setResumePrompt(null);
                }}
              >
                {saving ? "Saving…" : "Save buffered ride"}
              </Button>
              <button
                disabled={saving}
                onClick={() => {
                  buffer.clear();
                  setResumePrompt(null);
                }}
                className="f-press rounded-sm border border-vb-red/40 px-4 py-2 font-mono text-xs font-semibold uppercase tracking-[0.08em] text-vb-red transition-colors hover:bg-vb-red hover:text-white disabled:opacity-50"
              >
                Discard & start fresh
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// === Sub-Components ===

function DeviceRow({
  icon,
  label,
  description,
  connected,
  name,
  value,
  onConnect,
  onDisconnect,
  fromTrainer,
}: {
  icon: React.ReactNode;
  label: string;
  description?: string;
  connected: boolean;
  name: string | null;
  value?: string;
  onConnect: () => Promise<void>;
  onDisconnect: () => void;
  fromTrainer?: boolean;
}) {
  const [connecting, setConnecting] = useState(false);

  const handleConnect = async () => {
    setConnecting(true);
    try {
      await onConnect();
    } catch (e) {
      console.error(`Failed to connect ${label}:`, e);
    } finally {
      setConnecting(false);
    }
  };

  return (
    <div className="flex items-center gap-4 px-5 py-4 text-left">
      <div className="flex h-10 w-10 items-center justify-center rounded-full bg-white/5">
        {icon}
      </div>
      <div className="flex-1">
        <p className="text-sm font-medium text-vb-chalk">{label}</p>
        {connected && name ? (
          <p className="f-data text-xs text-vb-red">
            {name} {value && `· ${value}`}
          </p>
        ) : description ? (
          <p className="text-xs text-vb-chalk-dim">{description}</p>
        ) : null}
      </div>
      {connected ? (
        fromTrainer ? (
          <span className="rounded-sm bg-white/10 px-3 py-1.5 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-vb-chalk-dim">
            Via trainer
          </span>
        ) : (
          <button
            onClick={onDisconnect}
            className="f-press rounded-sm border border-white/10 px-3 py-1.5 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-vb-chalk-dim transition-colors hover:text-vb-chalk"
          >
            Disconnect
          </button>
        )
      ) : (
        <button
          onClick={handleConnect}
          disabled={connecting}
          className="f-press rounded-sm border border-white/20 px-3 py-1.5 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-vb-chalk transition-colors hover:border-vb-red hover:text-vb-red disabled:opacity-50"
        >
          {connecting ? "Scanning…" : "Connect"}
        </button>
      )}
    </div>
  );
}
