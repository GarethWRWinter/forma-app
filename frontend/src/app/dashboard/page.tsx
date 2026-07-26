"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { metrics, rides, training, goals as goalsApi, coachInsights, inspiration } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { formatDuration, formatDate, cn } from "@/lib/utils";
import { RiderProfileRadar } from "@/components/charts/rider-profile-radar";
import { Kicker } from "@/components/ui/kicker";
import { SectionHeader } from "@/components/ui/section-header";
import { DataTile } from "@/components/ui/data-tile";
import { ZoneChip } from "@/components/ui/seated-banner";
import { zoneFromIF } from "@/lib/zones";
import { ZONES } from "@/lib/palette";
import { dailyInkscape, WORKOUT_ZONE } from "@/lib/dailyInkscape";
import { EmptyState } from "@/components/ui/empty-state";
import { Card, CardBody } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";

/**
 * FORMA dashboard, the daily face of the product.
 * Paper ground, ink structure, one flamme accent, mono data.
 * Hairline editorial rows, huge numbers, Forma speaking from the rail.
 */

// One-line verdict per rider type: the carbon band's insight. Teach,
// never condescend; no dashes (typography law).
const RIDER_TYPE_VERDICT: Record<string, string> = {
  all_rounder:
    "No single spike, you're competitive across every system. Your edge is versatility; your ceiling comes from sharpening the tails.",
  sprinter:
    "Explosive top end, the final 200 metres belong to you. The work is carrying that weapon over the climbs to the finish.",
  climber:
    "You go up better than you go along. Your races are decided where the road tilts; protect the watts per kilo.",
  time_trialist:
    "A diesel engine that holds monstrous steady power. Your race is against the clock, and the clock is losing.",
  puncheur:
    "Short, sharp and vicious. Five-minute power is your weapon; pick finishes with a wall in the last kilometre.",
  pursuiter:
    "Big engine in the three-to-eight-minute range. Track DNA on the road; break away and stay away.",
  rouleur:
    "Strong everywhere the road is flat and hard. Wind, cobbles and long ranges are where you collect victims.",
};

// Physiological system → its TRUE zone ink. Data colours mean the data:
// endurance is Z2 work, threshold Z4, VO2 Z5, anaerobic Z6, sprint Z7.
const SYSTEM_ZONE_COLOR: Record<string, string> = {
  endurance: "#4A72AE",
  tempo: "#439D7C",
  sustained: "#439D7C",
  threshold: "#D9AC34",
  vo2max: "#E86F22",
  vo2: "#E86F22",
  anaerobic: "#D92420",
  sprint: "#B81743",
  neuromuscular: "#B81743",
};

export default function DashboardPage() {
  const { user } = useAuth();

  const { data: fitnessQuick } = useQuery({
    queryKey: ["fitness-summary-quick"],
    queryFn: () => metrics.getFitnessSummary(false),
  });

  const { data: fitnessFull } = useQuery({
    queryKey: ["fitness-summary"],
    queryFn: () => metrics.getFitnessSummary(true),
    staleTime: 5 * 60 * 1000,
  });

  const fitness = fitnessFull ?? fitnessQuick;

  const { data: recentRides } = useQuery({
    queryKey: ["recent-rides"],
    queryFn: () => rides.list(1, 5),
  });

  const { data: plans } = useQuery({
    queryKey: ["plans"],
    queryFn: () => training.getPlans(),
  });

  const { data: goalsData } = useQuery({
    queryKey: ["goals"],
    queryFn: () => goalsApi.list(),
  });

  const { data: daily } = useQuery({
    queryKey: ["daily-inspiration"],
    queryFn: () => inspiration.today(),
    staleTime: 60 * 60 * 1000, // it only changes at midnight
  });

  const { data: nudge } = useQuery({
    queryKey: ["coach-nudge"],
    queryFn: () => coachInsights.getNudge(),
    staleTime: 30 * 60 * 1000,
    retry: false,
  });

  const todayISO = new Date().toISOString().slice(0, 10);
  const { data: todayWorkouts } = useQuery({
    queryKey: ["today-workouts", todayISO],
    queryFn: () => training.getWorkouts(todayISO),
    staleTime: 5 * 60 * 1000,
    retry: false,
  });

  const { data: weeklyLoad } = useQuery({
    queryKey: ["weekly-load-12"],
    queryFn: () => metrics.getWeeklyLoad(12),
    staleTime: 30 * 60 * 1000,
    retry: false,
  });

  const latestRide = recentRides?.rides?.[0];
  const { data: latestDebrief } = useQuery({
    queryKey: ["ride-debrief", latestRide?.id],
    queryFn: () => coachInsights.getRideDebrief(latestRide!.id),
    enabled: !!latestRide?.id,
    staleTime: 60 * 60 * 1000,
    retry: false,
  });

  const coach = user?.coach_name || "Forma";

  const tsb = Math.round(fitness?.current_tsb ?? 0);
  const tsbNotable = tsb > 10 || tsb < -20;
  const tsbLabel =
    tsb > 10
      ? "Fresh. A good day to go hard."
      : tsb < -20
        ? "Deep fatigue. Absorb it."
        : "Productive strain";

  const ramp = fitness?.ramp_rate ?? 0;
  const ctlSub = ramp > 0 ? "Building" : ramp < 0 ? "Easing" : "Holding steady";

  const today = new Date().toLocaleDateString("en-GB", {
    weekday: "long",
    day: "numeric",
    month: "long",
  });

  const hour = new Date().getHours();
  const greeting =
    hour < 5 ? "Up early" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";

  const nextGoal = goalsData?.goals
    ?.filter((g) => g.status === "upcoming" && g.days_until != null && g.days_until > 0)
    ?.sort((a, b) => (a.days_until ?? 0) - (b.days_until ?? 0))?.[0];

  const firstName = user?.full_name?.split(" ")[0] || "Rider";

  // Today's pass: the hero inkscape rotates daily with the quote.
  const ink = dailyInkscape(daily?.date);

  // Week X of Y from the active plan
  const activePlan = plans?.plans.find((p) => p.status === "active");
  let weekOf: string | null = null;
  if (activePlan) {
    const start = new Date(activePlan.start_date).getTime();
    const week = Math.max(1, Math.ceil((Date.now() - start) / (7 * 86400000)));
    if (week <= activePlan.total_weeks) weekOf = `Week ${week} of ${activePlan.total_weeks}`;
  }

  // Today's session (first planned workout today)
  const todaySession = todayWorkouts?.find((w) => w.status !== "skipped");
  const sessionZone = todaySession
    ? WORKOUT_ZONE[todaySession.workout_type] || { z: "z2", name: todaySession.workout_type }
    : null;

  // The climb: 12 weeks of work. Height is HOW MUCH (TSS), colour is HOW
  // HARD (the week's average intensity, mapped to its true training zone).
  // Zone inks are data colours: they must mean the data, never decoration.
  const climbWeeks = weeklyLoad?.weeks ?? [];
  const maxTss = Math.max(1, ...climbWeeks.map((w) => w.total_tss || 0));

  return (
    <div className="space-y-12 md:space-y-14">
      {/* ============ HERO — today's pass + the daily line ============ */}
      <section className="f-rise relative -mx-4 -mt-4 overflow-hidden md:-mx-8 md:-mt-8">
        <img
          src={ink.src}
          alt={`Ink road art — ${ink.label}`}
          className="h-[380px] w-full object-cover sm:h-[420px] md:h-[460px]"
        />
        {/* scrim for legibility, heavier at the base where the words sit */}
        <div className="absolute inset-0 bg-gradient-to-t from-black/60 via-black/20 to-black/5" />
        {/* Lockup only on md+ — the mobile drawer bar already carries it
            (one kite moment per screen). */}
        <div className="absolute inset-x-0 top-0 hidden items-center justify-between p-5 md:flex md:p-8">
          <span className="f-display flex items-baseline gap-1 text-xl tracking-tight text-white">
            FORMA
            <span
              className="inline-block h-[9px] w-[9px] bg-vb-red"
              style={{ clipPath: "polygon(0 0, 100% 0, 50% 100%)" }}
            />
          </span>
        </div>
        {daily && (
          <blockquote className="absolute inset-x-0 bottom-0 p-5 md:p-8">
            <span className="f-display block text-5xl leading-[0.6] text-vb-red" aria-hidden>
              &ldquo;
            </span>
            <p className="f-display max-w-2xl text-2xl leading-snug text-white md:text-4xl">
              {daily.text}
            </p>
            <p className="f-kicker mt-4 flex items-center gap-2 text-white/70">
              <span
                className="inline-block h-[7px] w-[7px] shrink-0 bg-vb-red"
                style={{ clipPath: "polygon(0 0, 100% 0, 50% 100%)" }}
              />
              {daily.author} · {ink.label} ·{" "}
              {daily.tag === "wisdom" ? "Daily wisdom" : "Daily quote"}
            </p>
          </blockquote>
        )}
      </section>

      {/* ============ MASTHEAD — the briefing ============ */}
      <header className="f-rise">
        <Kicker>
          {today}
          {weekOf && <span> · {weekOf}</span>}
          {nextGoal && (
            <span className="text-vb-red">
              {" "}· {nextGoal.days_until} days to {nextGoal.event_name}
            </span>
          )}
        </Kicker>
        <h1 className="f-display mt-3 text-4xl leading-[1.04] md:text-6xl">
          {greeting}, {firstName}.
        </h1>
        {nudge && (
          <p className="mt-3 max-w-2xl text-base leading-relaxed text-vb-text-dim md:text-lg">
            {nudge.nudge}{" "}
            <Link
              href="/dashboard/coach"
              className="f-kicker whitespace-nowrap text-vb-text-muted transition-colors hover:text-vb-red"
            >
              Reply <span className="f-arrow-head">→</span>
            </Link>
          </p>
        )}
      </header>

      {/* ============ GOAL BAND (carbon) ============ */}
      {nextGoal && (
        <section className="f-rise flex items-center gap-4 bg-[#101012] px-5 py-5 text-white md:gap-6 md:px-8">
          <div
            className="flex h-14 w-16 shrink-0 items-start justify-center bg-vb-red pt-2.5 sm:h-16 sm:w-[74px]"
            style={{ clipPath: "polygon(0 0, 100% 0, 50% 100%)" }}
          >
            <span className="f-data text-xl font-bold leading-none sm:text-2xl">
              {nextGoal.days_until}
            </span>
          </div>
          <div className="min-w-0 flex-1">
            <p className="f-kicker text-vb-red">Your goal · {nextGoal.days_until} days out</p>
            <p className="f-display mt-1 line-clamp-2 text-lg leading-tight sm:text-xl md:text-2xl">
              {nextGoal.event_name}
            </p>
            <p className="f-kicker mt-1 text-white/50">
              {nextGoal.priority?.replace("_", "-")} · {formatDate(nextGoal.event_date)}
            </p>
          </div>
          <div className="hidden shrink-0 border-l border-white/15 pl-6 text-right sm:block">
            <p className="f-kicker text-white/50">Form · TSB</p>
            <p className={cn("f-data mt-1 text-3xl font-bold leading-none", tsb >= 0 ? "text-vb-red" : "text-white")}>
              {tsb > 0 ? `+${tsb}` : tsb}
            </p>
            <p className="f-kicker mt-1 text-white/50">
              {tsb > 10 ? "Fresh" : tsb < -20 ? "Deep fatigue" : "On track"}
            </p>
          </div>
        </section>
      )}

      {/* ============ TODAY'S SESSION ============ */}
      {todaySession && sessionZone ? (
        <section className="f-rise flex flex-col gap-4 border border-vb-border bg-vb-surface p-5 sm:flex-row sm:items-center md:px-8 md:py-6">
          <div className="min-w-0 flex-1">
            <p className="f-kicker flex items-center gap-2">
              <span
                className="inline-block h-2.5 w-2.5"
                style={{ background: ZONES[sessionZone.z as keyof typeof ZONES] }}
              />
              <span className="text-vb-text-muted">
                Today · {sessionZone.z.toUpperCase()} · {sessionZone.name}
              </span>
            </p>
            <p className="f-display mt-2 text-2xl leading-tight md:text-3xl">
              {todaySession.title}
            </p>
            <p className="f-data mt-1.5 text-xs text-vb-text-dim">
              {todaySession.planned_duration_seconds
                ? formatDuration(todaySession.planned_duration_seconds)
                : ""}
              {todaySession.planned_tss ? ` · ${Math.round(todaySession.planned_tss)} TSS` : ""}
              {todaySession.status === "completed" ? " · done ✓" : ""}
            </p>
          </div>
          {todaySession.status !== "completed" && (
            <Link
              href={`/dashboard/training/${todaySession.id}/session`}
              className={cn(buttonVariants({ variant: "flamme" }), "shrink-0 self-start sm:self-center")}
            >
              Start ride <span className="f-arrow-head">→</span>
            </Link>
          )}
        </section>
      ) : (
        <section className="f-rise border border-vb-border-subtle bg-vb-surface px-5 py-5 md:px-8">
          <p className="f-kicker text-vb-text-muted">Today</p>
          <p className="mt-1.5 text-base text-vb-text-dim">
            Nothing on the sheet. Feet up, or spin easy. Even Coppi took rest days.
          </p>
        </section>
      )}

      {/* ============ FITNESS STAT STRIP ============ */}
      <section className="f-stagger grid grid-cols-2 gap-3 md:grid-cols-4">
        <DataTile
          label="Fitness · CTL"
          value={Math.round(fitness?.current_ctl ?? 0)}
          sub={ctlSub}
        />
        <DataTile
          label="Fatigue · ATL"
          value={Math.round(fitness?.current_atl ?? 0)}
          sub="Rolling 7 days"
        />
        <DataTile label="Form · TSB" value={tsb} sub={tsbLabel} hot={tsbNotable} />
        <DataTile
          label="FTP"
          value={user?.ftp ?? 0}
          unit="w"
          sub={
            user?.weight_kg
              ? `${((user.ftp ?? 0) / user.weight_kg).toFixed(2)} W/kg`
              : "watts"
          }
        />
      </section>

      {/* ============ THE CLIMB — 12 weeks of work ============ */}
      {climbWeeks.length > 1 && (
        <section className="f-rise">
          <SectionHeader
            kicker="The climb · 12 weeks"
            title="How far you've come"
            action={
              fitness?.ramp_rate != null && fitness.ramp_rate !== 0 ? (
                <span className="f-data text-sm text-vb-text-dim">
                  {fitness.ramp_rate > 0 ? "+" : ""}
                  {Math.round(fitness.ramp_rate)} CTL/wk
                </span>
              ) : undefined
            }
          />
          <div className="flex h-36 items-end gap-1.5 md:h-44 md:gap-2">
            {climbWeeks.map((w, i) => {
              const isNow = i === climbWeeks.length - 1;
              const hasWork = (w.total_tss || 0) > 0;
              const zone = hasWork ? zoneFromIF(w.avg_intensity_factor) : null;
              return (
                <div
                  key={w.week_start}
                  className="relative flex h-full flex-1 flex-col items-center justify-end"
                  title={
                    hasWork
                      ? `${w.week_start} · ${Math.round(w.total_tss || 0)} TSS · ${zone?.name ?? ""}`
                      : `${w.week_start} · no riding logged`
                  }
                >
                  {isNow && (
                    <span
                      className="mb-1 inline-block h-[7px] w-[9px] bg-vb-red"
                      style={{ clipPath: "polygon(0 0, 100% 0, 50% 100%)" }}
                      aria-hidden
                    />
                  )}
                  <div
                    className="w-full"
                    style={{
                      height: hasWork
                        ? `${Math.max(4, ((w.total_tss || 0) / maxTss) * 100)}%`
                        : "2px",
                      background: zone ? zone.color : "#E7E7E1",
                    }}
                  />
                </div>
              );
            })}
          </div>
          <div className="mt-2 flex items-center justify-between">
            <span className="f-kicker text-vb-text-muted">12 weeks ago</span>
            <span className="f-kicker text-vb-red">You are here</span>
          </div>
          <p className="mt-2 text-xs leading-relaxed text-vb-text-muted">
            Height is how much work each week held. Colour is how hard it was:
            blue and green weeks build the engine, gold and orange weeks
            sharpen it.
          </p>
        </section>
      )}

      {/* ============ GOALS NEEDING ASSESSMENT ============ */}
      {goalsData?.goals
        .filter((g) => g.needs_assessment)
        .map((goal) => (
          <Link
            key={goal.id}
            href={`/dashboard/goals/${goal.id}/assess`}
            className="f-lift f-arrow block border border-vb-border-subtle border-l-[3px] border-l-vb-red bg-vb-surface px-6 py-5"
          >
            <div className="flex items-center justify-between gap-6">
              <div>
                <Kicker flamme className="mb-1.5">
                  So, how did it go?
                </Kicker>
                <p className="f-display text-xl leading-tight">{goal.event_name}</p>
                <p className="mt-1 text-sm text-vb-text-dim">
                  Debrief with {coach}. What you share shapes the next block.
                </p>
              </div>
              <span className="f-kicker shrink-0 text-vb-text">
                Write report <span className="f-arrow-head">→</span>
              </span>
            </div>
          </Link>
        ))}

      {/* ============ LATEST DEBRIEF (rail card — Forma speaks) ============ */}
      {latestDebrief && latestRide && (() => {
        const zone = zoneFromIF(latestRide.intensity_factor);
        return (
          <section className="f-rise border border-vb-border-subtle border-l-[3px] border-l-vb-red bg-vb-surface p-6 md:p-8">
            <ZoneChip color={zone.color}>
              {zone.name} · {zone.key.toUpperCase()}
            </ZoneChip>
            <h2 className="f-display mt-3 text-3xl leading-tight md:text-4xl">
              {latestRide.title}
            </h2>
            <p className="f-kicker mt-2 text-vb-text-muted">
              {coach}&apos;s debrief · {formatDate(latestRide.ride_date)}
            </p>
            <div className="prose mt-3 max-w-md text-vb-text-dim prose-p:my-2.5 prose-p:text-[16px] prose-p:leading-[1.65] prose-strong:font-semibold prose-strong:text-vb-text prose-em:not-italic prose-em:text-vb-text">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {latestDebrief.debrief}
              </ReactMarkdown>
            </div>
            <Link
              href={`/dashboard/rides/${latestRide.id}`}
              className="f-kicker f-arrow mt-4 inline-block text-vb-text transition-colors hover:text-vb-red"
            >
              Full ride <span className="f-arrow-head">→</span>
            </Link>
          </section>
        );
      })()}

      {/* ============ RIDER PROFILE — the verdict ============ */}
      {fitness &&
        fitness.rider_type !== "unknown" &&
        fitness.profile_scores &&
        fitness.profile_scores.length > 0 && (
          <section className="f-rise">
            <SectionHeader
              kicker="Profile"
              title="Rider type"
              action={
                <Link
                  href="/dashboard/performance"
                  className="f-kicker f-arrow text-vb-text-muted transition-colors hover:text-vb-red"
                >
                  Full performance <span className="f-arrow-head">→</span>
                </Link>
              }
            />
            {/* Carbon verdict band */}
            <div className="flex flex-col gap-3 bg-[#101012] px-6 py-6 text-white sm:flex-row sm:items-center sm:gap-8 md:px-8">
              <div className="flex shrink-0 items-baseline gap-5">
                <span className="f-display text-3xl capitalize leading-none md:text-4xl">
                  {fitness.rider_type.replace("_", " ")}
                </span>
                {fitness.w_per_kg && (
                  <span className="f-data whitespace-nowrap text-2xl font-bold leading-none text-vb-red md:text-3xl">
                    {fitness.w_per_kg.toFixed(2)}
                    <span className="ml-1 text-xs font-medium text-white/60">W/kg</span>
                  </span>
                )}
              </div>
              <p className="max-w-xl border-white/15 text-sm leading-relaxed text-white/80 sm:border-l sm:pl-8">
                {RIDER_TYPE_VERDICT[fitness.rider_type] ||
                  "Every ride teaches Forma more about the shape of your engine."}
              </p>
            </div>

            {/* Radar + per-system bars */}
            <div className="grid gap-8 border border-t-0 border-vb-border-subtle bg-vb-surface p-6 md:grid-cols-[minmax(240px,1fr)_1.4fr] md:gap-10 md:p-8">
              <div>
                <Kicker className="mb-3 text-vb-text-muted">
                  Power profile · Pentagon
                </Kicker>
                <RiderProfileRadar
                  scores={fitness.profile_scores}
                  riderType={fitness.rider_type ?? "unknown"}
                  strengths={fitness.strengths}
                  weaknesses={fitness.weaknesses}
                  compact
                />
                <p className="f-kicker mt-2 text-vb-text-muted">
                  Rings · 25 / 50 / 75 / 100 percentile
                </p>
              </div>
              <div>
                <Kicker className="mb-4 text-vb-text-muted">
                  By system · Percentile vs field
                </Kicker>
                <ul className="space-y-3.5">
                  {fitness.profile_scores.map((s) => {
                    const color =
                      SYSTEM_ZONE_COLOR[s.category] ||
                      SYSTEM_ZONE_COLOR[s.label.toLowerCase()] ||
                      "#9A9A94";
                    return (
                      <li key={s.category} className="flex items-center gap-4">
                        <span className="f-kicker w-24 shrink-0 text-vb-text-dim">
                          {s.label.replace(/ \(.*\)/, "")}
                        </span>
                        <span className="h-4 flex-1 bg-vb-sunken">
                          <span
                            className="block h-full"
                            style={{
                              width: `${Math.max(2, Math.min(100, s.score))}%`,
                              background: color,
                            }}
                          />
                        </span>
                        <span className="f-data w-9 shrink-0 text-right text-lg font-bold">
                          {Math.round(s.score)}
                        </span>
                      </li>
                    );
                  })}
                </ul>
                {fitness.weaknesses.length > 0 && (
                  <div className="mt-6 border-t border-vb-border-subtle pt-5">
                    <Kicker flamme className="mb-1.5">
                      To work on
                    </Kicker>
                    <p className="mb-3 text-xs leading-relaxed text-vb-text-dim">
                      Your lowest systems, where the next gains are hiding.
                    </p>
                    <div className="flex flex-wrap gap-2">
                      {fitness.weaknesses.map((w) => (
                        <span
                          key={w}
                          className="f-kicker flex items-center gap-1.5 border border-vb-border px-2.5 py-1.5 text-vb-text"
                        >
                          <span
                            className="inline-block h-2 w-2"
                            style={{
                              background:
                                SYSTEM_ZONE_COLOR[w.toLowerCase()] || "#9A9A94",
                            }}
                          />
                          {w}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          </section>
        )}

      {/* ============ TWO-COLUMN: RECENT RIDES + GOALS / PLAN ============ */}
      <div className="grid gap-12 md:grid-cols-2">
        {/* Recent rides */}
        <section>
          <SectionHeader
            kicker="Latest"
            title="Recent rides"
            action={
              <Link
                href="/dashboard/rides"
                className="f-kicker f-arrow text-vb-text-muted transition-colors hover:text-vb-red"
              >
                All <span className="f-arrow-head">→</span>
              </Link>
            }
          />
          {recentRides?.rides.length === 0 ? (
            <EmptyState
              title="Show me what you can do."
              action={
                <Link
                  href="/dashboard/rides"
                  className={cn(buttonVariants({ variant: "ghost", size: "sm" }))}
                >
                  Upload a ride <span className="f-arrow-head">→</span>
                </Link>
              }
            >
              No rides yet. Upload one or connect Strava and I&apos;ll start
              building your power profile.
            </EmptyState>
          ) : (
            <ul className="f-stagger">
              {recentRides?.rides.map((ride, idx) => (
                <li
                  key={ride.id}
                  className={idx > 0 ? "border-t border-vb-border-subtle" : undefined}
                >
                  <Link
                    href={`/dashboard/rides/${ride.id}`}
                    className="f-lift -mx-3 flex items-baseline justify-between gap-4 px-3 py-4 transition-colors hover:bg-vb-surface"
                  >
                    <div className="min-w-0">
                      <Kicker className="mb-0.5">
                        {formatDate(ride.ride_date)}
                        {ride.duration_seconds &&
                          ` · ${formatDuration(ride.duration_seconds)}`}
                      </Kicker>
                      <p className="truncate font-medium text-vb-text">{ride.title}</p>
                    </div>
                    <div className="f-data flex shrink-0 items-baseline gap-6 text-vb-text">
                      {ride.normalized_power != null && (
                        <span className="text-sm">
                          {Math.round(ride.normalized_power)}
                          <span className="ml-0.5 text-[10px] text-vb-text-muted">w</span>
                        </span>
                      )}
                      {ride.tss != null && (
                        <span className="text-sm">
                          {Math.round(ride.tss)}
                          <span className="ml-0.5 text-[10px] text-vb-text-muted">TSS</span>
                        </span>
                      )}
                    </div>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* Goals + Plan */}
        <div className="space-y-12">
          <section>
            <SectionHeader
              kicker="Calendar"
              title="Active goals"
              action={
                <Link
                  href="/dashboard/goals"
                  className="f-kicker f-arrow text-vb-text-muted transition-colors hover:text-vb-red"
                >
                  All <span className="f-arrow-head">→</span>
                </Link>
              }
            />
            {!goalsData ||
            goalsData.goals.filter((g) => g.status === "upcoming").length === 0 ? (
              <EmptyState
                title="Nothing on the calendar."
                action={
                  <Link
                    href="/dashboard/goals"
                    className={cn(buttonVariants({ variant: "ghost", size: "sm" }))}
                  >
                    Set a goal <span className="f-arrow-head">→</span>
                  </Link>
                }
              >
                Give me a race to aim you at and the whole plan bends around it.
              </EmptyState>
            ) : (
              <ul className="f-stagger">
                {goalsData.goals
                  .filter((g) => g.status === "upcoming")
                  .slice(0, 3)
                  .map((goal, idx) => (
                    <li
                      key={goal.id}
                      className={cn(
                        "flex items-baseline justify-between gap-4 py-4",
                        idx > 0 && "border-t border-vb-border-subtle"
                      )}
                    >
                      <div className="min-w-0">
                        <Kicker className="mb-0.5">
                          {formatDate(goal.event_date)} ·{" "}
                          <span>{goal.priority.replace("_", "-")}</span>
                        </Kicker>
                        <p className="truncate font-medium text-vb-text">
                          {goal.event_name}
                        </p>
                      </div>
                      {goal.days_until != null && goal.days_until > 0 && (
                        <span className="f-data shrink-0 bg-vb-sunken px-2.5 py-1 text-xs text-vb-text-dim">
                          {goal.days_until}d
                        </span>
                      )}
                    </li>
                  ))}
              </ul>
            )}
          </section>

          {plans && plans.plans.length > 0 && (
            <section>
              <SectionHeader kicker="In flight" title="Active plan" />
              {plans.plans
                .filter((p) => p.status === "active")
                .slice(0, 1)
                .map((plan) => (
                  <Card key={plan.id} className="border-l-[3px] border-l-vb-text">
                    <CardBody>
                      <p className="f-display mb-2 text-xl leading-tight">{plan.name}</p>
                      <p className="f-data text-xs leading-relaxed text-vb-text-dim">
                        {formatDate(plan.start_date)}, {formatDate(plan.end_date)}
                        <br />
                        {plan.total_weeks} weeks · {plan.phase_count} phases
                      </p>
                      <Link
                        href="/dashboard/training"
                        className="f-kicker f-arrow mt-4 inline-block text-vb-text transition-colors hover:text-vb-red"
                      >
                        View calendar <span className="f-arrow-head">→</span>
                      </Link>
                    </CardBody>
                  </Card>
                ))}
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
