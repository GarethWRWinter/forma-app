// Browser-side error reporting. Off unless the DSN exists, so local dev and
// preview builds never phone home. Setting NEXT_PUBLIC_SENTRY_DSN on Vercel
// is the whole rollout.
import * as Sentry from "@sentry/nextjs";

if (process.env.NEXT_PUBLIC_SENTRY_DSN) {
  Sentry.init({
    dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
    environment: "production",
    // Errors are the point; tracing is a cost decision for another day.
    tracesSampleRate: 0,
    // Riders' coach conversations must never ride along in an error event.
    sendDefaultPii: false,
  });
}

export const onRouterTransitionStart = Sentry.captureRouterTransitionStart;
