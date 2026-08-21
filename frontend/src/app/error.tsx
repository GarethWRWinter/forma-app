"use client";

// Route-level error boundary. Without this file any client exception showed
// Next's unstyled "Application error" white screen, which is the least
// Forma-shaped surface a rider could possibly meet.

import { useEffect } from "react";
import * as Sentry from "@sentry/nextjs";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    Sentry.captureException(error);
  }, [error]);

  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 px-6 text-center">
      <p className="font-mono text-[11px] font-semibold uppercase tracking-[0.16em] text-vb-red">
        Mechanical
      </p>
      <h1 className="font-display text-3xl font-extrabold tracking-[-0.02em] text-vb-text">
        Something slipped a gear.
      </h1>
      <p className="max-w-sm text-sm text-vb-text-dim">
        Not your fault, and nothing of yours is lost. Try again, and if it
        keeps happening it is already on my list to fix.
      </p>
      <button
        onClick={reset}
        className="mt-2 border border-vb-text bg-vb-text px-5 py-2.5 font-mono text-xs font-semibold uppercase tracking-[0.1em] text-vb-bg hover:bg-vb-red hover:border-vb-red hover:text-white"
      >
        Try again
      </button>
    </div>
  );
}
