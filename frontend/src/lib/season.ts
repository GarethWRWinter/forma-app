import type { GoalEvent, TrainingPlan } from "@/lib/api";
import { localISODate } from "@/lib/utils";

/**
 * Between goals, read the way the server's season keeper reads it
 * (app/services/season_service.py), so the plan page, Today and the coach
 * agree on where the rider stands.
 */

/** The plan running today: active, and not past its last day. A plan whose
    date has gone is over even before the keeper marks it so; reading it as
    current is what put "week 7 of 7" over empty days (16 Oct 2026). */
export function currentPlan(
  plans: TrainingPlan[] | undefined
): TrainingPlan | undefined {
  const today = localISODate();
  return plans?.find((p) => p.status === "active" && p.end_date >= today);
}

export interface Season {
  /** The nearest goal still ahead (today counts). */
  futureGoal?: GoalEvent;
  /** A goal is dated today. Race day is chasing, never between goals. */
  raceDay: boolean;
  /** The most recent goal no longer ahead. */
  lastGoal?: GoalEvent;
  /** No goal ahead, not race day, and one behind. */
  betweenGoals: boolean;
  /** The last goal's race report is still owed. */
  needsDebrief: boolean;
  /** Between goals, and the last goal was raced in the past fortnight. */
  freshFromRace: boolean;
  /** Between goals after a DNF or DNS: the debrief leads, and nothing asks
      for the next goal (the debrief law in coach_skills, and the server's
      season note). */
  setback: boolean;
}

/** How long a finished goal keeps the coach's slot on Today for the ask
    about what comes next. After that the next-goal ask lives on Goal. */
const FRESH_DAYS = 14;

/** Whole days from one ISO date to another, calendar days only. */
function daysBetween(fromISO: string, toISO: string): number {
  const [fy, fm, fd] = fromISO.slice(0, 10).split("-").map(Number);
  const [ty, tm, td] = toISO.slice(0, 10).split("-").map(Number);
  return Math.round(
    (Date.UTC(ty, tm - 1, td) - Date.UTC(fy, fm - 1, fd)) / 86_400_000
  );
}

/** Built from the server's days_until, so a rider in a different timezone
    from the server sees the same state the coach is told about. */
export function seasonState(goals: GoalEvent[] | undefined): Season {
  const all = goals ?? [];
  const futureGoal = all
    .filter((g) => g.status === "upcoming" && g.days_until != null)
    .sort((a, b) => (a.days_until ?? 0) - (b.days_until ?? 0))[0];
  const raceDay = all.some((g) => g.days_until === 0);
  const lastGoal = all
    .filter(
      (g) =>
        g.days_until !== 0 && (g.days_until == null || g.status !== "upcoming")
    )
    .sort((a, b) => b.event_date.localeCompare(a.event_date))[0];
  const betweenGoals = !futureGoal && !raceDay && !!lastGoal;
  return {
    futureGoal,
    raceDay,
    lastGoal,
    betweenGoals,
    needsDebrief: betweenGoals && !!lastGoal?.needs_assessment,
    freshFromRace:
      betweenGoals &&
      !!lastGoal &&
      daysBetween(lastGoal.event_date, localISODate()) <= FRESH_DAYS,
    setback:
      betweenGoals && (lastGoal?.status === "dnf" || lastGoal?.status === "dns"),
  };
}

/** The rider's opening line to the coach when choosing what comes next,
    prefilled for them to send. */
export function nextGoalAsk(lastGoal: GoalEvent, justFiled = false): string {
  return justFiled
    ? `I’ve just filed my race report for ${lastGoal.event_name}. Talk it through with me, then propose the next goal you think I should chase. Ask me one question at a time until we have it, then file it for me.`
    : `${lastGoal.event_name} is behind me. From how it went and how I’ve been riding since, propose the next goal you think I should chase. Ask me one question at a time until we have it, then file it for me.`;
}
