// Coach Forma "Race Radio" — template-based coaching message engine
// Directeur Sportif + spin class instructor voice

export type CoachTrigger =
  | "workout_start"
  | "pre_step_change"
  | "step_start"
  | "step_midpoint"
  | "long_effort_check"
  | "power_too_high"
  | "power_too_low"
  | "workout_complete";

export interface CoachContext {
  stepType: string;
  zoneName: string;
  zoneNumber: number;
  targetWatts: number;
  targetPct: number; // fraction e.g. 0.85
  stepDuration: number;
  stepElapsed: number;
  stepRemaining: number;
  cadenceTarget: number | null;
  intervalIndex?: number;
  intervalTotal?: number;
  totalElapsed: number;
  totalRemaining: number;
  nextStepType?: string;
  nextZoneName?: string;
  nextTargetWatts?: number;
}

interface MessageTemplate {
  text: string;
  /** Only use for specific step types (null = any) */
  stepTypes?: string[];
}

// {variable} placeholders get interpolated with context values
const MESSAGE_POOLS: Record<CoachTrigger, MessageTemplate[]> = {
  workout_start: [
    { text: "Let's go. Easy spin to start, let your legs open up." },
    { text: "Time to ride. Settle in and let the warm-up do its job." },
    { text: "Here we go. Smooth pedalling, relaxed breathing, nothing heroic yet." },
    { text: "Right, to work. Every great ride starts with an easy ten minutes." },
    { text: "Roll out. Easy start, big finish. That's how the classics are won." },
  ],

  pre_step_change: [
    {
      text: "{stepRemaining} seconds, get ready for {nextZoneName} at {nextTargetWatts}W.",
      stepTypes: ["warmup", "steady_state", "interval_off", "cooldown"],
    },
    {
      text: "Heads up, {nextZoneName} interval coming in {stepRemaining}. Build your focus.",
      stepTypes: ["warmup", "steady_state", "interval_off"],
    },
    {
      text: "Almost there. {stepRemaining} seconds and you can recover.",
      stepTypes: ["interval_on", "steady_state"],
    },
    {
      text: "{stepRemaining} to go. Hold your form, the rest is coming.",
      stepTypes: ["interval_on"],
    },
    {
      text: "Get ready to change gear. {nextZoneName} at {nextTargetWatts}W in {stepRemaining} seconds.",
    },
    {
      text: "Transition coming. {stepRemaining} seconds then we shift to {nextZoneName}.",
    },
  ],

  step_start: [
    {
      text: "Go. Settle into {targetWatts} watts. Smooth and strong.",
      stepTypes: ["interval_on"],
    },
    {
      text: "Interval {intervalIndex} of {intervalTotal}. You know what to do. {targetWatts}W, let's hold it.",
      stepTypes: ["interval_on"],
    },
    {
      text: "Hit it. {targetWatts} watts. Don't spike, ramp in over 5 seconds.",
      stepTypes: ["interval_on"],
    },
    {
      text: "This is the one that matters. {zoneName} effort, {targetWatts}W. Hold every second of it.",
      stepTypes: ["interval_on"],
    },
    {
      text: "Good work. Spin easy now, deep breaths. {stepDuration} seconds to recover.",
      stepTypes: ["interval_off"],
    },
    {
      text: "Recovery time. Drop the power, spin your legs out. You've earned this rest.",
      stepTypes: ["interval_off"],
    },
    {
      text: "Easy now. Shake out your hands, relax your shoulders. Recover smart.",
      stepTypes: ["interval_off"],
    },
    {
      text: "That's the hard work done. Easy spin to cool down. Well earned.",
      stepTypes: ["cooldown"],
    },
    {
      text: "Cool down time. Gradually bring it down, let your heart rate settle.",
      stepTypes: ["cooldown"],
    },
    {
      text: "Warm-up phase. Let your legs find their rhythm, don't force it.",
      stepTypes: ["warmup"],
    },
    {
      text: "Building into it now. {zoneName} effort at {targetWatts}W.",
      stepTypes: ["steady_state", "ramp"],
    },
    {
      text: "Settle in at {targetWatts}W. {zoneName} pace, find your breathing rhythm.",
      stepTypes: ["steady_state"],
    },
  ],

  step_midpoint: [
    {
      text: "Halfway through this {zoneName} block. Holding strong at {targetWatts}W.",
      stepTypes: ["interval_on", "steady_state"],
    },
    {
      text: "You're {stepElapsed} seconds in, {stepRemaining} to go. Stay on it.",
      stepTypes: ["interval_on"],
    },
    {
      text: "Past the halfway mark. This is where it counts. Don't fade now.",
      stepTypes: ["interval_on"],
    },
    {
      text: "Halfway. Your legs are talking to you, that's normal. Stay committed.",
      stepTypes: ["interval_on"],
    },
    {
      text: "Half done. Keep your cadence smooth and let the power come from your hips.",
      stepTypes: ["interval_on", "steady_state"],
    },
  ],

  long_effort_check: [
    { text: "Check your form. Drop the shoulders, relax the grip, smooth circles." },
    { text: "This {zoneName} work is building your engine. Every pedal stroke counts." },
    { text: "How's your breathing? Rhythmic and controlled. In through the nose if you can." },
    { text: "Stay present. Don't think about the finish, own this kilometre." },
    { text: "Relax your jaw, unclench your hands. Tension is watts you're not using." },
    { text: "This is exactly the work that wins the last hour of a race." },
    {
      text: "Zone 2 built every engine worth having. Low drama, high value. Keep turning.",
      stepTypes: ["steady_state"],
    },
    { text: "Drink. Small sips, stay ahead of it. The pros drink before they're thirsty." },
    { text: "Think about the pedal stroke. Over the top, through the bottom." },
    { text: "This is the work your rivals are skipping. Bank it." },
  ],

  power_too_high: [
    { text: "Power is running hot. Ease back to {targetWatts}W, save the matches for later." },
    { text: "Above target. Back to {targetWatts}W. Discipline now, fireworks later." },
    { text: "Too hot. {targetWatts}W. Every race is lost by someone who felt great early." },
    { text: "Control it. {targetWatts}W is the number. Harder isn't better here." },
  ],

  power_too_low: [
    { text: "Power is slipping. Dig in, {stepRemaining} seconds. Hold the number." },
    { text: "Bring it back to {targetWatts}W. Find the rhythm again." },
    { text: "Don't let it go. {targetWatts}W. Just the next 30 seconds, nothing else." },
    { text: "I need {targetWatts}W from you. Recommit. This bit is the whole point." },
  ],

  workout_complete: [
    { text: "That's done, and it counts. Recovery drink inside 30 minutes." },
    { text: "Session banked. Stretch, refuel, feet up. I'm already writing the next one." },
    { text: "Done. Every file like that makes the plan smarter. Well ridden." },
    { text: "Good work. The gains happen while you rest, so go do that part properly." },
    { text: "That's another session in the legs. See you at the next one." },
  ],
};

/**
 * Select a coaching message for a given trigger and context.
 * Filters by step type, avoids recently used messages, and interpolates placeholders.
 */
export function selectMessage(
  trigger: CoachTrigger,
  context: CoachContext,
  recentMessages: string[]
): string | null {
  const pool = MESSAGE_POOLS[trigger];
  if (!pool || pool.length === 0) return null;

  // Filter by step type
  const eligible = pool.filter(
    (m) => !m.stepTypes || m.stepTypes.includes(context.stepType)
  );
  if (eligible.length === 0) return null;

  // Filter out recently used (by raw template)
  const fresh = eligible.filter((m) => !recentMessages.includes(m.text));
  const candidates = fresh.length > 0 ? fresh : eligible; // fall back to all if all used

  // Pick random
  const template = candidates[Math.floor(Math.random() * candidates.length)];

  // Interpolate placeholders
  return interpolate(template.text, context);
}

function interpolate(template: string, context: CoachContext): string {
  return template.replace(/\{(\w+)\}/g, (_, key) => {
    const value = (context as unknown as Record<string, unknown>)[key];
    if (value === undefined || value === null) return "";

    // Format seconds as readable
    if (key === "stepDuration" || key === "stepRemaining" || key === "stepElapsed") {
      const secs = Number(value);
      if (secs >= 60) {
        const m = Math.floor(secs / 60);
        const s = secs % 60;
        return s > 0 ? `${m}m ${s}s` : `${m} minute${m > 1 ? "s" : ""}`;
      }
      return `${Math.round(secs)}`;
    }

    if (key === "targetPct") {
      return `${Math.round(Number(value) * 100)}%`;
    }

    return String(value);
  });
}
