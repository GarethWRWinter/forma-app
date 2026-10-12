"use client";

import { SafetyCard } from "@/components/safety/SafetyCard";
import type { ThreadSafetyCard } from "@/components/coach/coach-thread";

/**
 * The emergency and crisis cards at the top of one coach reply: fixed words
 * from the server, never the model's, emergency first. They show the moment
 * the stream delivers them, and again when the thread reloads from history.
 */
export function ReplySafetyCards({
  cards,
  country,
}: {
  cards?: readonly ThreadSafetyCard[];
  country?: string | null;
}) {
  if (!cards?.length) return null;
  return (
    <div className="ml-10 max-w-[90%] space-y-2 sm:max-w-[80%]">
      {cards.map((card) => (
        <SafetyCard
          key={`${card.kind}:${card.text}`}
          kind={card.kind}
          text={card.text}
          country={country}
        />
      ))}
    </div>
  );
}
