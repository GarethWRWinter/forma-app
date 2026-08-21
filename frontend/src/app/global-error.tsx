"use client";

// Last-resort boundary: catches errors in the root layout itself, where
// error.tsx cannot. Renders its own <html> because at this point nothing
// else survived. Kept dependency-free beyond Sentry for the same reason.

import { useEffect } from "react";
import * as Sentry from "@sentry/nextjs";

export default function GlobalError({
  error,
}: {
  error: Error & { digest?: string };
}) {
  useEffect(() => {
    Sentry.captureException(error);
  }, [error]);

  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: "14px",
          background: "#F0F0EC",
          color: "#0B0B0C",
          fontFamily: "system-ui, sans-serif",
          textAlign: "center",
          padding: "24px",
        }}
      >
        <h1 style={{ fontSize: 26, fontWeight: 800, margin: 0 }}>
          Something slipped a gear.
        </h1>
        <p style={{ maxWidth: 400, fontSize: 14, color: "#555", margin: 0 }}>
          Not your fault, and nothing of yours is lost. Reload the page, and
          if it keeps happening it is already on my list to fix.
        </p>
        <button
          onClick={() => window.location.reload()}
          style={{
            marginTop: 8,
            padding: "12px 22px",
            background: "#0B0B0C",
            color: "#FAFAF7",
            border: "none",
            fontSize: 12,
            fontWeight: 600,
            letterSpacing: "0.1em",
            textTransform: "uppercase",
            cursor: "pointer",
          }}
        >
          Reload
        </button>
      </body>
    </html>
  );
}
