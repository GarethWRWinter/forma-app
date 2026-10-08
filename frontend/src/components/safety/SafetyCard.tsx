"use client";

import { AlertTriangle, Phone } from "lucide-react";
import { cn } from "@/lib/utils";
import { emergencyNumber, hasSamaritans } from "@/components/safety/safety-rules";

export type SafetyCardKind = "emergency" | "crisis";

/**
 * The fixed card the server sends before the coach's reply when a message
 * hits an emergency or crisis red flag. Its words come from the server, not
 * the model, and it is never shown as chat text: it sits above the reply,
 * louder than anything else in the thread, with the number one tap away.
 */
export function SafetyCard({
  kind,
  text,
  country,
  className,
}: {
  kind: SafetyCardKind;
  text: string;
  country?: string | null;
  className?: string;
}) {
  const emergency = emergencyNumber(country);
  const urgent = kind === "emergency";

  return (
    <div
      role="alert"
      className={cn(
        "rounded-sm border-l-[3px] p-4 sm:p-5",
        urgent
          ? "border border-vb-red border-l-vb-red bg-vb-red text-white"
          : "border border-vb-border-strong border-l-vb-red bg-vb-surface-raised text-vb-text",
        className
      )}
    >
      <p
        className={cn(
          "f-kicker flex items-center gap-2",
          urgent ? "text-white" : "text-vb-red"
        )}
      >
        <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
        {urgent ? "Get help now" : "Please talk to someone"}
      </p>
      <p className="mt-2 text-[15px] leading-relaxed">{text}</p>
      <div className="mt-4 flex flex-wrap gap-2">
        {!urgent && hasSamaritans(country) && (
          <a
            href="tel:116123"
            className="f-press inline-flex h-10 items-center gap-2 rounded-sm bg-vb-text px-4 font-mono text-xs font-semibold uppercase tracking-[0.08em] text-white hover:bg-vb-red"
          >
            <Phone className="h-3.5 w-3.5" aria-hidden="true" />
            Samaritans 116 123
          </a>
        )}
        <a
          href={`tel:${emergency}`}
          className={cn(
            "f-press inline-flex h-10 items-center gap-2 rounded-sm px-4 font-mono text-xs font-semibold uppercase tracking-[0.08em]",
            urgent
              ? "bg-white text-vb-red hover:bg-vb-surface"
              : "border border-vb-border bg-transparent text-vb-text hover:border-vb-border-strong"
          )}
        >
          <Phone className="h-3.5 w-3.5" aria-hidden="true" />
          Call {emergency}
        </a>
      </div>
    </div>
  );
}
