"use client";

import { useQuery } from "@tanstack/react-query";
import { coachInsights } from "@/lib/api";
import { Kicker } from "@/components/ui/kicker";

/** The team car before the stage: today's pre-ride briefing.
    Daily = the word through the car window; goal day = the full talk. */
export function BriefingCard() {
  const { data: briefing, isLoading } = useQuery({
    queryKey: ["coach-briefing"],
    queryFn: () => coachInsights.getBriefing(),
    staleTime: 1000 * 60 * 60, // cached server-side per day anyway
    retry: 1,
  });

  if (isLoading) {
    return (
      <section className="f-rise border border-vb-border-subtle bg-vb-surface p-5 md:p-6">
        <Kicker dot flamme>Pre-ride briefing</Kicker>
        <p className="mt-3 text-sm text-vb-text-dim">
          Forma is reading the sky and today&apos;s plan…
        </p>
      </section>
    );
  }

  if (!briefing) return null;

  const isGoalDay = briefing.kind === "goal";
  const c = briefing.conditions;

  return (
    <section
      className={
        "f-rise border-l-[3px] border-l-vb-red bg-vb-surface p-5 md:p-6 " +
        (isGoalDay
          ? "border border-vb-border-strong"
          : "border-y border-r border-vb-border-subtle")
      }
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <Kicker dot flamme>
          {isGoalDay ? "Race day · the team car" : "Pre-ride briefing"}
        </Kicker>
        {c && (
          <span className="f-data text-xs text-vb-text-dim">
            {c.temp_c != null && `${Math.round(c.temp_c)}°C`}
            {c.wind_kph != null && ` · ${c.wind_dir ?? ""} ${Math.round(c.wind_kph)} km/h`}
            {c.rain_chance != null && c.rain_chance > 0 && ` · ${c.rain_chance}% rain`}
          </span>
        )}
      </div>
      <div
        className={
          "mt-3 whitespace-pre-line leading-relaxed text-vb-text " +
          (isGoalDay ? "text-[15px]" : "text-sm")
        }
      >
        {briefing.content}
      </div>
      <p className="f-signature mt-4 text-xl">Forma</p>
    </section>
  );
}
