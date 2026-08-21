// Server/edge-side init. Same rule as the client: no DSN, no reporting.
import * as Sentry from "@sentry/nextjs";

export async function register() {
  if (process.env.NEXT_PUBLIC_SENTRY_DSN) {
    Sentry.init({
      dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
      environment: "production",
      tracesSampleRate: 0,
      sendDefaultPii: false,
    });
  }
}

export const onRequestError = Sentry.captureRequestError;
