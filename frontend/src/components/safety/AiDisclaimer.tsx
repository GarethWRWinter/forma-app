"use client";

import { useAuth } from "@/lib/auth-context";
import { cn } from "@/lib/utils";
import { aiDisclaimer } from "@/components/safety/safety-rules";

/** The permanent line under every place a rider talks to the coach. The
    emergency number follows where they live: 999, 112 or 911. */
export function AiDisclaimer({ className }: { className?: string }) {
  const { user } = useAuth();
  return (
    <p className={cn("text-[11px] leading-snug text-vb-text-muted", className)}>
      {aiDisclaimer(user?.country)}
    </p>
  );
}
