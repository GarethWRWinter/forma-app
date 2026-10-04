"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw, AlertTriangle } from "lucide-react";
import Link from "next/link";
import { Sidebar } from "@/components/layout/sidebar";
import { CoachDock, useCoachDockVisible } from "@/components/coach/coach-dock";
import { auth, billing } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useStravaAutoSync } from "@/hooks/useStravaAutoSync";
import { cn } from "@/lib/utils";

function VerifyEmailBanner() {
  const [sent, setSent] = useState(false);
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-vb-border-subtle bg-vb-surface px-4 py-2.5 sm:px-8">
      <p className="text-xs text-vb-text-dim">
        One thing left: confirm your email, so password resets and
        Forma&apos;s check-ins can reach you.
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

/** Shown on every dashboard page once the paywall is on and this rider
    hasn't joined. Without it, an unpaid rider met the paywall as errors:
    the coach said "send that again" and uploads failed with no reason
    (launch audit, 4 Oct 2026). Shares its query with the Membership card, so
    it disappears the moment Stripe confirms the payment. */
function MembershipBanner() {
  const [error, setError] = useState("");
  const [opening, setOpening] = useState(false);
  const { data: status } = useQuery({
    queryKey: ["billing-status"],
    queryFn: () => billing.getStatus(),
  });
  if (!status?.configured || !status.required || status.has_access) return null;
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-vb-border-subtle bg-vb-surface px-4 py-3 sm:px-8">
      <p className="max-w-2xl text-sm text-vb-text-dim">
        Your plan is ready. Join Forma and the coach and your ride uploads
        open up: £14.99 a month for the founding hundred, locked for as long
        as you stay.
        {error && <span className="mt-1 block text-vb-red">{error}</span>}
      </p>
      <button
        disabled={opening}
        onClick={async () => {
          setError("");
          setOpening(true);
          try {
            const { url } = await billing.checkout();
            window.location.href = url;
          } catch (err) {
            setError(err instanceof Error ? err.message : "Stripe didn't answer. Try again in a minute.");
            setOpening(false);
          }
        }}
        className="f-kicker text-vb-red transition-colors hover:text-vb-red-dim disabled:opacity-50"
      >
        {opening ? "Opening checkout" : "Join Forma"}
      </button>
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
  // When the dock holds the bottom corner, the sync toast stacks above it.
  const dockVisible = useCoachDockVisible();

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
    // h-dvh, not h-screen: on iOS Safari 100vh is the *large* viewport (toolbar
    // hidden). Since only the inner main scrolls, the toolbar never collapses,
    // so a 100vh shell puts its own bottom edge permanently behind it. That is
    // where the chat composer lives.
    <div className="flex h-dvh overflow-hidden bg-vb-bg">
      <Sidebar />
      <main className="flex-1 overflow-y-auto bg-vb-bg pt-14 md:pt-0">
        {user.email_verified === false && <VerifyEmailBanner />}
        <MembershipBanner />
        {/* flex column at min-h-full so a page can opt into filling the exact
            remaining height with flex-1, instead of guessing it with a vh calc
            that cannot know about the banner above or this padding. */}
        <div className="mx-auto flex min-h-full max-w-7xl flex-col px-4 py-6 sm:px-8 sm:py-10">
          {children}
        </div>
      </main>

      {/* Forma is always one tap away. Hidden where the coach already owns
          the surface (the coach page, the carbon session player). */}
      <CoachDock />

      {/* Auto-sync toast, editorial chip with red accent on errors. */}
      {(syncing || (lastSyncedCount != null && lastSyncedCount > 0) || lastError) && (
        <div
          className={cn(
            "fixed right-4 z-50 flex items-center gap-2 rounded-md border border-vb-border bg-vb-surface px-4 py-2.5 text-[11px] font-medium uppercase tracking-[0.08em] text-vb-text",
            dockVisible ? "bottom-[4.75rem]" : "bottom-4"
          )}
        >
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
              <span>Strava sync failed → open Settings</span>
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
