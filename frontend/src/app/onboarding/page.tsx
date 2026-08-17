"use client";

import { useRouter } from "next/navigation";
import { useState, useRef } from "react";
import { Check, Link2, Upload, MapPin } from "lucide-react";
import { onboarding, users, goals, training } from "@/lib/api";
import { COACH_TONES } from "@/lib/coach";
import { useAuth } from "@/lib/auth-context";
import { cn } from "@/lib/utils";
import { Button, Arrow } from "@/components/ui/button";
import { Kicker } from "@/components/ui/kicker";
import { ProgressSteps } from "@/components/ui/progress-steps";
import { CoachNote } from "@/components/ui/coach-note";
import { Input } from "@/components/ui/input";

const GOALS = [
  {
    value: "build_fitness",
    label: "Get fitter, full stop",
    desc: "A stronger engine and longer days out. No finish line required",
  },
  {
    value: "target_event",
    label: "Aim at an event",
    desc: "A date on the calendar, and a season built backwards from it",
  },
  {
    value: "improve_ftp",
    label: "Raise my FTP",
    desc: "More watts at threshold. The number that moves everything else",
  },
  {
    value: "race",
    label: "Race, properly",
    desc: "Be there when it kicks off at the front, not watching it go",
  },
  {
    value: "learn_skills",
    label: "Ride smarter",
    desc: "Pacing, fuelling, handling. The craft of the sport",
  },
];

const PREFERENCES = [
  { value: "indoor", label: "Indoor (trainer)" },
  { value: "outdoor", label: "Outdoor" },
  { value: "both", label: "Both" },
];

const EVENT_TYPES = [
  { value: "road_race", label: "Road race" },
  { value: "crit", label: "Criterium" },
  { value: "time_trial", label: "Time trial" },
  { value: "gran_fondo", label: "Gran Fondo" },
  { value: "sportive", label: "Sportive" },
  { value: "gravel", label: "Gravel" },
  { value: "mtb", label: "Mountain bike" },
];

const PRIORITIES = [
  { value: "a_race", label: "A race", desc: "The day the whole season points at" },
  { value: "b_race", label: "B race", desc: "Matters, but we won't taper for it" },
  { value: "c_race", label: "C race", desc: "Training with a number on your back" },
];

export default function OnboardingPage() {
  const router = useRouter();
  const { refreshUser } = useAuth();
  const gpxInputRef = useRef<HTMLInputElement>(null);

  // Wizard step (0=goal, 1=event details if target_event, 2=experience, 3=physical)
  const [step, setStep] = useState(0);
  const [goal, setGoal] = useState("");

  // Event details (only shown when goal === "target_event")
  const [eventName, setEventName] = useState("");
  const [eventDate, setEventDate] = useState("");
  const [eventType, setEventType] = useState("");
  const [eventPriority, setEventPriority] = useState("a_race");
  const [eventDuration, setEventDuration] = useState("");
  const [eventNotes, setEventNotes] = useState("");
  const [routeUrl, setRouteUrl] = useState("");
  const [gpxFile, setGpxFile] = useState<File | null>(null);

  // Experience details
  const [weeklyHours, setWeeklyHours] = useState("6");
  const [yearsCycling, setYearsCycling] = useState("1");
  const [preference, setPreference] = useState("both");

  // Training schedule
  const [hardDays, setHardDays] = useState<number[]>([5, 6]); // Sat/Sun default
  const [restDays, setRestDays] = useState<number[]>([]);

  // Physical stats
  const [ftp, setFtp] = useState("");
  const [weight, setWeight] = useState("");

  // Coach is Forma; the rider shapes manner (tone) + face. Name is optional
  // and defaults to Forma.
  const [coachAvatar, setCoachAvatar] = useState("");
  const [coachName, setCoachName] = useState("");
  const [coachTone, setCoachTone] = useState("balanced");

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  // The plan is the one thing Forma promises out loud on the last step, so a
  // failed write gets said to the rider instead of hidden behind the
  // dashboard. The goal id is held so a retry writes the plan again without
  // re-submitting the quiz or creating a second goal.
  const [planFailed, setPlanFailed] = useState(false);
  const [planRetried, setPlanRetried] = useState(false);
  const [savedGoalId, setSavedGoalId] = useState<string | null>(null);

  // Whether event step is needed
  const isEventGoal = goal === "target_event";

  // Total steps: 4 normally (goal, experience, physical, coach), 5 with event details
  const totalSteps = isEventGoal ? 5 : 4;

  // Map display step to actual step index
  const getActualStep = (displayStep: number) => {
    if (!isEventGoal) return displayStep;
    return displayStep;
  };

  const handleNext = () => {
    if (step === 0 && isEventGoal) {
      setStep(1); // Go to event details
    } else if (step === 0) {
      setStep(2); // Skip event details
    } else {
      setStep(step + 1);
    }
  };

  const handleBack = () => {
    if (step === 2 && !isEventGoal) {
      setStep(0);
    } else {
      setStep(step - 1);
    }
  };

  const handleGpxSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file && file.name.toLowerCase().endsWith(".gpx")) {
      setGpxFile(file);
    }
  };

  // A rider who named a goal meets their coach by crafting it: the
  // first conversation is the why, not the watts.
  const leaveOnboarding = (goalId: string | null) => {
    if (goalId && eventName) {
      router.push(
        `/dashboard/coach?ask=${encodeURIComponent(
          `I've just set my goal: ${eventName}. Before we talk training, help me craft it properly. Ask me one question at a time: why this one matters to me, and who it's turning me into. Then save that to the goal.`
        )}`
      );
    } else {
      router.push("/dashboard");
    }
  };

  // Write the plan, then leave. A failure keeps the rider on this step: by
  // now the account exists and the answers are stored, so the honest move is
  // to say the plan is missing rather than run the success animation.
  const writePlan = async (goalId: string | null) => {
    try {
      await training.generatePlan({
        goal_event_id: goalId ?? undefined,
        periodization_model: "traditional",
      });
      leaveOnboarding(goalId);
    } catch (planErr) {
      console.error("Plan generation failed during onboarding:", planErr);
      setPlanFailed(true);
    }
  };

  const handleRetryPlan = async () => {
    setLoading(true);
    setPlanFailed(false);
    setPlanRetried(true);
    await writePlan(savedGoalId);
    setLoading(false);
  };

  const handleFinish = async () => {
    setLoading(true);
    setError("");
    try {
      // 1. Submit onboarding quiz
      await onboarding.submitQuiz({
        primary_goal: goal,
        current_weekly_volume_hours: parseFloat(weeklyHours),
        years_cycling: parseInt(yearsCycling),
        indoor_outdoor_preference: preference,
      });

      // 2. Update profile with FTP, weight, and schedule preferences
      const profileData: Record<string, number | number[] | string> = {};
      if (ftp) profileData.ftp = parseInt(ftp);
      if (weight) profileData.weight_kg = parseFloat(weight);
      profileData.preferred_hard_days = hardDays;
      profileData.rest_days = restDays;
      // Coach choice: only persist what the rider actually chose.
      const coachData: Record<string, string> = {};
      if (coachAvatar) coachData.coach_avatar = coachAvatar;
      if (coachName.trim()) coachData.coach_name = coachName.trim();
      if (coachTone) coachData.coach_tone = coachTone;
      Object.assign(profileData, coachData);
      await users.updateProfile(profileData);

      // 3. If training for event, create the goal event
      let createdGoalId: string | null = null;
      if (isEventGoal && eventName && eventDate && eventType) {
        const createdGoal = await goals.create({
          event_name: eventName,
          event_date: eventDate,
          event_type: eventType,
          priority: eventPriority,
          target_duration_minutes: eventDuration
            ? parseInt(eventDuration)
            : undefined,
          notes: eventNotes || undefined,
          route_url: routeUrl || undefined,
        });
        createdGoalId = createdGoal?.id ?? null;

        // 4. If GPX file was selected, upload it
        if (gpxFile && createdGoal?.id) {
          try {
            await goals.uploadGpx(createdGoal.id, gpxFile);
          } catch (gpxErr) {
            console.error("GPX upload failed:", gpxErr);
            // Don't block onboarding for GPX upload failure
          }
        }
      }

      setSavedGoalId(createdGoalId);
      // Everything the rider typed is stored by this point, so refresh before
      // the plan attempt: they can still leave if the plan never writes.
      await refreshUser();

      // 5. Auto-generate a training plan
      //    - If the user has a target event, build it around that goal so the
      //      plan peaks on race day.
      //    - Otherwise still generate a default 12-week plan so the user lands
      //      on the dashboard with something to do.
      await writePlan(createdGoalId);
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Something went wrong";
      setError(message);
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  // Progress bar calculation
  const currentProgress = (() => {
    if (!isEventGoal) {
      return step === 0 ? 0 : step === 2 ? 1 : step === 3 ? 2 : 3;
    }
    return step;
  })();

  const fieldLabel = "mb-1.5 block text-sm font-medium text-vb-text";
  const chip = (selected: boolean) =>
    cn(
      "f-press rounded-sm border px-3 py-2 text-xs font-medium transition-colors",
      selected
        ? "border-vb-red bg-vb-surface text-vb-text"
        : "border-vb-border-subtle bg-vb-surface text-vb-text-dim hover:border-vb-border"
    );

  return (
    <div className="flex min-h-screen items-center justify-center bg-vb-bg px-4 py-12">
      <div className="w-full max-w-lg">
        {/* Progress */}
        <ProgressSteps total={totalSteps} current={currentProgress} className="mb-10" />

        {/* Step 0: Goal Selection */}
        {step === 0 && (
          <div className="f-rise">
            <Kicker>First things first</Kicker>
            <h2 className="f-display mt-2 text-3xl text-vb-text">
              What are we aiming at?
            </h2>
            <p className="mt-2 text-sm text-vb-text-dim">
              Everything Forma builds starts from this answer.
            </p>

            <div className="f-stagger mt-8 space-y-3">
              {GOALS.map((g) => (
                <button
                  key={g.value}
                  onClick={() => setGoal(g.value)}
                  className={cn(
                    "f-lift f-press flex w-full items-center gap-4 rounded-sm border bg-vb-surface p-4 text-left transition-colors",
                    goal === g.value
                      ? "border-vb-red"
                      : "border-vb-border-subtle"
                  )}
                >
                  <span
                    aria-hidden="true"
                    className={cn(
                      "h-2.5 w-2.5 shrink-0 rounded-full transition-colors",
                      goal === g.value ? "bg-vb-red" : "border border-vb-border"
                    )}
                  />
                  <span>
                    <span className="block text-sm font-medium text-vb-text">
                      {g.label}
                    </span>
                    <span className="block text-xs text-vb-text-dim">
                      {g.desc}
                    </span>
                  </span>
                </button>
              ))}
            </div>

            <Button
              onClick={handleNext}
              disabled={!goal}
              className="mt-8 w-full"
            >
              Continue <Arrow />
            </Button>
          </div>
        )}

        {/* Step 1: Event Details (only when target_event selected) */}
        {step === 1 && isEventGoal && (
          <div className="f-rise">
            <Kicker>The target</Kicker>
            <h2 className="f-display mt-2 text-3xl text-vb-text">
              Tell me about race day
            </h2>
            <p className="mt-2 text-sm text-vb-text-dim">
              The more Forma knows about the day, the sharper the plan gets.
            </p>

            <div className="mt-8 space-y-5">
              {/* Event Name */}
              <div>
                <label className={fieldLabel}>What&apos;s it called?</label>
                <Input
                  type="text"
                  value={eventName}
                  onChange={(e) => setEventName(e.target.value)}
                  placeholder="e.g. Etape du Tour, London to Brighton"
                />
              </div>

              {/* Event Date */}
              <div>
                <label className={fieldLabel}>When is it?</label>
                <Input
                  type="date"
                  value={eventDate}
                  onChange={(e) => setEventDate(e.target.value)}
                />
              </div>

              {/* Event Type */}
              <div>
                <label className={fieldLabel}>What kind of day is it?</label>
                <div className="grid grid-cols-2 gap-2">
                  {EVENT_TYPES.map((t) => (
                    <button
                      key={t.value}
                      onClick={() => setEventType(t.value)}
                      className={chip(eventType === t.value)}
                    >
                      {t.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* Priority */}
              <div>
                <label className={fieldLabel}>How much does it matter?</label>
                <div className="flex gap-2">
                  {PRIORITIES.map((p) => (
                    <button
                      key={p.value}
                      onClick={() => setEventPriority(p.value)}
                      className={cn(chip(eventPriority === p.value), "flex-1 text-center")}
                      title={p.desc}
                    >
                      {p.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* Expected Duration */}
              <div>
                <label className={fieldLabel}>
                  How long will you be out there?{" "}
                  <span className="font-normal text-vb-text-muted">
                    (minutes, optional)
                  </span>
                </label>
                <Input
                  type="number"
                  value={eventDuration}
                  onChange={(e) => setEventDuration(e.target.value)}
                  placeholder="e.g. 180"
                />
              </div>

              {/* Route / Challenge Info Section */}
              <div className="rounded-sm border border-vb-border-subtle bg-vb-surface p-4">
                <h3 className="mb-1 flex items-center gap-2 text-sm font-medium text-vb-text">
                  <MapPin className="h-4 w-4 text-vb-red" />
                  Show Forma the course
                </h3>
                <p className="mb-4 text-xs text-vb-text-dim">
                  A route link or a GPX file, and Forma reads the climbs before
                  you do.
                </p>

                {/* Route URL */}
                <div className="mb-3">
                  <label className="mb-1.5 flex items-center gap-2 text-xs font-medium text-vb-text-dim">
                    <Link2 className="h-3.5 w-3.5" />
                    Strava route, segment or race website
                  </label>
                  <Input
                    type="url"
                    value={routeUrl}
                    onChange={(e) => setRouteUrl(e.target.value)}
                    placeholder="e.g. https://www.strava.com/routes/123456"
                    className="bg-vb-sunken"
                  />
                </div>

                {/* GPX Upload */}
                <div>
                  <label className="mb-1.5 flex items-center gap-2 text-xs font-medium text-vb-text-dim">
                    <Upload className="h-3.5 w-3.5" />
                    Or a GPX file of the route
                  </label>
                  <input
                    ref={gpxInputRef}
                    type="file"
                    accept=".gpx"
                    onChange={handleGpxSelect}
                    className="hidden"
                  />
                  <button
                    onClick={() => gpxInputRef.current?.click()}
                    className={cn(
                      "f-press w-full rounded-sm border border-dashed py-3 text-center text-xs transition-colors",
                      gpxFile
                        ? "border-vb-red text-vb-red"
                        : "border-vb-border text-vb-text-dim hover:border-vb-border-strong hover:text-vb-text"
                    )}
                  >
                    {gpxFile ? (
                      <span className="flex items-center justify-center gap-2">
                        <Check className="h-3.5 w-3.5" />
                        {gpxFile.name}
                      </span>
                    ) : (
                      "Choose a .gpx file"
                    )}
                  </button>
                </div>
              </div>

              {/* Notes */}
              <div>
                <label className={fieldLabel}>
                  Anything else Forma should know?{" "}
                  <span className="font-normal text-vb-text-muted">(optional)</span>
                </label>
                <textarea
                  value={eventNotes}
                  onChange={(e) => setEventNotes(e.target.value)}
                  placeholder="e.g. 3 big climbs in the last 50km, expect headwinds on the coast..."
                  rows={2}
                  className="w-full rounded-sm border border-vb-border bg-vb-surface px-3 py-2.5 text-sm text-vb-text placeholder:text-vb-text-muted focus:border-vb-red focus:outline-none focus:ring-1 focus:ring-vb-red"
                />
              </div>
            </div>

            <div className="mt-8 flex gap-3">
              <Button variant="ghost" onClick={handleBack} className="px-6">
                Back
              </Button>
              <Button
                onClick={() => setStep(2)}
                disabled={!eventName || !eventDate || !eventType}
                className="flex-1"
              >
                Continue <Arrow />
              </Button>
            </div>
          </div>
        )}

        {/* Step 2: Experience */}
        {step === 2 && (
          <div className="f-rise">
            <Kicker>Your riding life</Kicker>
            <h2 className="f-display mt-2 text-3xl text-vb-text">
              How much bike is in your life?
            </h2>
            <p className="mt-2 text-sm text-vb-text-dim">
              Honest answers build honest plans. Forma works with the week you
              actually have.
            </p>

            <div className="mt-8 space-y-6">
              <div>
                <label className={fieldLabel}>
                  How many hours a week can you give it?
                </label>
                <div className="flex items-center gap-4">
                  <input
                    type="range"
                    min="2"
                    max="20"
                    step="1"
                    value={weeklyHours}
                    onChange={(e) => setWeeklyHours(e.target.value)}
                    className="flex-1 accent-vb-red"
                  />
                  <span className="f-data w-16 text-right text-xl font-semibold text-vb-text">
                    {weeklyHours}h
                  </span>
                </div>
              </div>

              <div>
                <label className={fieldLabel}>
                  How long have you been riding?
                </label>
                <div className="flex items-center gap-4">
                  <input
                    type="range"
                    min="0"
                    max="20"
                    step="1"
                    value={yearsCycling}
                    onChange={(e) => setYearsCycling(e.target.value)}
                    className="flex-1 accent-vb-red"
                  />
                  <span className="f-data w-16 text-right text-xl font-semibold text-vb-text">
                    {yearsCycling}y
                  </span>
                </div>
              </div>

              <div>
                <label className={fieldLabel}>Where do you ride?</label>
                <div className="flex gap-3">
                  {PREFERENCES.map((p) => (
                    <button
                      key={p.value}
                      onClick={() => setPreference(p.value)}
                      className={cn(chip(preference === p.value), "flex-1 py-2.5 text-sm")}
                    >
                      {p.label}
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <label className={fieldLabel}>Which days can hurt?</label>
                <p className="mb-3 text-xs text-vb-text-dim">
                  Tap each day. Hard days carry the intensity, rest days stay
                  empty, easy days soak up the rest.
                </p>
                <div className="grid grid-cols-7 gap-2">
                  {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((day, idx) => {
                    const isHard = hardDays.includes(idx);
                    const isRest = restDays.includes(idx);
                    return (
                      <div key={day} className="text-center">
                        <p className="f-data mb-1 text-[10px] uppercase tracking-[0.12em] text-vb-text-muted">
                          {day}
                        </p>
                        <button
                          type="button"
                          onClick={() => {
                            if (isRest) {
                              setRestDays(restDays.filter(d => d !== idx));
                              setHardDays([...hardDays, idx]);
                            } else if (isHard) {
                              setHardDays(hardDays.filter(d => d !== idx));
                            } else {
                              setRestDays([...restDays, idx]);
                            }
                          }}
                          className={cn(
                            "f-press w-full rounded-sm border py-2.5 text-xs font-medium transition-colors",
                            isHard
                              ? "border-vb-red bg-vb-red text-white"
                              : isRest
                                ? "border-vb-border-subtle bg-vb-bg text-vb-text-muted"
                                : "border-vb-border-subtle bg-vb-sunken text-vb-text"
                          )}
                        >
                          {isHard ? "Hard" : isRest ? "Rest" : "Easy"}
                        </button>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>

            <div className="mt-8 flex gap-3">
              <Button variant="ghost" onClick={handleBack} className="px-6">
                Back
              </Button>
              <Button onClick={() => setStep(3)} className="flex-1">
                Continue <Arrow />
              </Button>
            </div>
          </div>
        )}

        {/* Step 3: Physical stats */}
        {step === 3 && (
          <div className="f-rise">
            <Kicker>The numbers</Kicker>
            <h2 className="f-display mt-2 text-3xl text-vb-text">
              The engine, roughly.
            </h2>
            <p className="mt-2 text-sm text-vb-text-dim">
              Skip anything you don&apos;t know. We&apos;ll measure the rest.
            </p>

            <div className="mt-8 space-y-5">
              <div>
                <label className={fieldLabel}>FTP, if you know it</label>
                <Input
                  type="number"
                  value={ftp}
                  onChange={(e) => setFtp(e.target.value)}
                  placeholder="e.g. 250"
                />
                <p className="mt-1.5 text-xs text-vb-text-muted">
                  No idea? Fine. Forma will find it on the road.
                </p>
              </div>
              <div>
                <label className={fieldLabel}>Weight (kg)</label>
                <Input
                  type="number"
                  step="0.1"
                  value={weight}
                  onChange={(e) => setWeight(e.target.value)}
                  placeholder="e.g. 75"
                />
                <p className="mt-1.5 text-xs text-vb-text-muted">
                  Watts per kilo is the sport&apos;s honest currency.
                </p>
              </div>
            </div>

            {error && (
              <p className="mt-4 text-sm text-vb-red">{error}</p>
            )}

            <div className="mt-8 flex gap-3">
              <Button variant="ghost" onClick={() => setStep(2)} className="px-6">
                Back
              </Button>
              <Button onClick={() => setStep(4)} className="flex-1">
                Next, meet Forma <Arrow />
              </Button>
            </div>
          </div>
        )}

        {/* Step 4: Meet your coach, any face, any name, any tone */}
        {step === 4 && (
          <div className="f-rise">
            <Kicker dot flamme>Your coach</Kicker>
            <h2 className="f-display mt-2 text-3xl text-vb-text">
              Meet Forma.
            </h2>
            <p className="mt-2 text-sm text-vb-text-dim">
              Forma is your coach, and adapts to you. Shape how Forma shows up,
              the coaching is world-class either way. You can fine-tune this
              any time.
            </p>

            <div className="mt-8">
              <Kicker className="mb-3">How Forma coaches you</Kicker>
              <div className="f-stagger grid grid-cols-2 gap-2">
                {COACH_TONES.map((t) => (
                  <button
                    key={t.key}
                    type="button"
                    onClick={() => setCoachTone(t.key)}
                    className={cn(
                      "f-lift f-press rounded-sm border bg-vb-surface p-3 text-left transition-colors",
                      coachTone === t.key
                        ? "border-vb-red"
                        : "border-vb-border-subtle"
                    )}
                  >
                    <p className="flex items-center gap-1.5 text-xs font-medium text-vb-text">
                      {coachTone === t.key && (
                        <span
                          aria-hidden="true"
                          className="inline-block h-1.5 w-1.5 rounded-full bg-vb-red"
                        />
                      )}
                      {t.label}
                    </p>
                    <p className="mt-0.5 text-[11px] leading-snug text-vb-text-dim">{t.description}</p>
                  </button>
                ))}
              </div>
            </div>

            <details className="mt-8">
              <summary className="f-kicker cursor-pointer list-none text-vb-text-muted hover:text-vb-text-dim">
                Prefer another name? (optional)
              </summary>
              <Input
                type="text"
                value={coachName}
                maxLength={30}
                onChange={(e) => setCoachName(e.target.value)}
                placeholder="Forma"
                className="mt-2"
              />
              <p className="mt-1.5 text-[11px] text-vb-text-muted">
                Your coach stays Forma unless you change this.
              </p>
            </details>

            {planFailed ? (
              <CoachNote
                className="mt-8"
                kicker={planRetried ? "Still no plan" : "The plan didn't write"}
                coachName={coachName.trim() || "Forma"}
              >
                {planRetried ? (
                  <>
                    Twice now, and it still won&apos;t write. Your account and
                    your answers are safe, but you&apos;re going through
                    without week one. Open Training on the dashboard, hit Build
                    my season, and I&apos;ll get it done there.
                  </>
                ) : (
                  <>
                    Your account is made and every answer you gave me is saved.
                    The plan itself didn&apos;t write. That one&apos;s on me,
                    so let me have another go at it.
                  </>
                )}
              </CoachNote>
            ) : (
              <CoachNote
                className="mt-8"
                kicker="Before you go"
                coachName={coachName.trim() || "Forma"}
              >
                Right. Give me these answers and I&apos;ll write week one before
                you&apos;ve closed the laptop.
              </CoachNote>
            )}

            {error && (
              <p className="mt-4 text-sm text-vb-red">{error}</p>
            )}

            {/* Once the answers are saved, Back and Start training would
                re-submit the quiz and create a second goal, so the failure
                state offers exactly one way onward. */}
            {planFailed ? (
              <Button
                variant="flamme"
                size="lg"
                onClick={
                  planRetried
                    ? () => router.push("/dashboard")
                    : handleRetryPlan
                }
                disabled={loading}
                className="mt-8 w-full"
              >
                {loading ? "Writing week one…" : planRetried ? (
                  <>
                    Go to my dashboard <Arrow />
                  </>
                ) : (
                  <>
                    Write the plan again <Arrow />
                  </>
                )}
              </Button>
            ) : (
              <div className="mt-8 flex gap-3">
                <Button variant="ghost" onClick={() => setStep(3)} className="px-6">
                  Back
                </Button>
                <Button
                  variant="flamme"
                  size="lg"
                  onClick={handleFinish}
                  disabled={loading}
                  className="flex-1"
                >
                  {loading ? "Writing week one…" : (
                    <>
                      Start training <Arrow />
                    </>
                  )}
                </Button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
