"use client";

import Link from "next/link";
import { Arrow, buttonVariants } from "@/components/ui/button";
import { CoachDot } from "@/components/ui/coach-glyph";
import type { Activation } from "@/lib/api";

/**
 * The one thing standing between this rider and a working plan.
 *
 * Shown only while the rider is still setting up, in the same slot as the
 * coach's proposals and initiatives, because it is the same idea: one ask,
 * never a list. Gone the moment the plan exists.
 */
export function ActivationCard({
  activation,
  coachName,
}: {
  activation: Activation;
  coachName: string;
}) {
  const next = activation.next_action;
  if (!next) return null;
  return (
    <section className="f-rise border border-vb-border-subtle bg-vb-surface p-5 md:p-8">
      <div className="flex items-center gap-2.5">
        <CoachDot state="pulsing" size="9px" />
        <span className="f-kicker text-vb-text-muted">
          {coachName} · one thing first
        </span>
      </div>
      <h2 className="f-display mt-3 text-2xl text-vb-text md:text-3xl">{next.title}</h2>
      <p className="mt-3 max-w-2xl text-base leading-relaxed text-vb-text-dim">
        {next.instruction}
      </p>
      <Link href={next.link} className={buttonVariants({ variant: "flamme", size: "sm" }) + " mt-5"}>
        {next.title}
        <Arrow />
      </Link>
    </section>
  );
}
