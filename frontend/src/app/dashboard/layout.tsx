"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { RefreshCw, AlertTriangle } from "lucide-react";
import Link from "next/link";
import { Sidebar } from "@/components/layout/sidebar";
import { auth } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useStravaAutoSync } from "@/hooks/useStravaAutoSync";

function TalkToForma() {
  const pathname = usePathname();
  // The coach page IS the conversation; the session player has Race Radio.
  if (pathname.startsWith("/dashboard/coach") || pathname.includes("/session")) {
    return null;
  }
  return (
    <Link
      href="/dashboard/coach"
      aria-label="Talk to Forma"
      className="fixed bottom-5 right-5 z-40 flex items-center gap-2.5 border border-vb-border-strong bg-vb-surface-raised py-2.5 pl-3 pr-4 transition-transform hover:-translate-y-0.5"
    >
      <span className="f-pulse-dot inline-block h-2.5 w-2.5 rounded-full bg-vb-red" />
      <span className="font-mono text-[11px] uppercase tracking-[0.12em] text-vb-text">
        Talk to Forma
      </span>
    </Link>
  );
}

function VerifyEmailBanner() {
  const [sent, setSent] = useState(false);
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-vb-border-subtle bg-vb-surface px-4 py-2.5 sm:px-8">
      <p className="text-xs text-vb-text-dim">
        One thing left: confirm your email so password resets can always
        reach you.
      </p>
      {sent ? (
        <span className="f-kicker text-vb-text-dim">Sent, check your inbox</span>
      ) : (
        <button
          onClick={async () => {
            try {
              await auth.resendVerification();
            } finally {
              setSent(true);
            }
          }}
          className="f-kicker text-vb-red transition-colors hover:text-vb-red-dim"
        >
          Resend the link
        </button>
      )}
    </div>
  );
}

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !user) {
      router.push("/login");
    }
  }, [user, loading, router]);

  // Auto-sync Strava once per session (and at most every 15 minutes) so new
  // rides appear without the user having to click anything. Runs in the
  // background; any errors are swallowed inside the hook.
  const { syncing, lastSyncedCount, lastError } = useStravaAutoSync({
    enabled: !!user && !loading,
  });

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center bg-vb-bg">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-vb-forest border-t-transparent" />
      </div>
    );
  }

  if (!user) return null;

  return (
    <div className="flex h-screen overflow-hidden bg-vb-bg">
      <Sidebar />
      <main className="flex-1 overflow-y-auto bg-vb-bg pt-14 md:pt-0">
        {user.email_verified === false && <VerifyEmailBanner />}
        <div className="mx-auto max-w-7xl px-4 py-6 sm:px-8 sm:py-10">
          {children}
        </div>
      </main>

      {/* Forma is always one tap away. Hidden where the coach already owns
          the surface (the coach page, the carbon session player). */}
      <TalkToForma />

      {/* Auto-sync toast, editorial chip with red accent on errors. */}
      {(syncing || (lastSyncedCount != null && lastSyncedCount > 0) || lastError) && (
        <div className="fixed bottom-4 right-4 z-50 flex items-center gap-2 rounded-md border border-vb-border bg-vb-surface px-4 py-2.5 text-[11px] font-medium uppercase tracking-[0.08em] text-vb-text">
          {syncing ? (
            <>
              <RefreshCw className="h-3.5 w-3.5 animate-spin" />
              <span>Syncing Strava…</span>
            </>
          ) : lastError ? (
            <Link
              href="/dashboard/settings"
              className="flex items-center gap-2 text-vb-clay hover:opacity-80"
              title={lastError}
            >
              <AlertTriangle className="h-3.5 w-3.5" />
              <span>Strava sync failed → open settings</span>
            </Link>
          ) : (
            <>
              <RefreshCw className="h-3.5 w-3.5" />
              <span>
                +{lastSyncedCount} new ride
                {lastSyncedCount === 1 ? "" : "s"}
              </span>
            </>
          )}
        </div>
      )}
    </div>
  );
}
