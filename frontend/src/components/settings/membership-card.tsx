"use client";

import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { billing } from "@/lib/api";
import { formatDate } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button, Arrow } from "@/components/ui/button";
import { Kicker } from "@/components/ui/kicker";

const STATUS_LABELS: Record<string, string> = {
  active: "Active",
  trialing: "Trial",
  past_due: "Payment issue",
  canceled: "Cancelled",
  none: "Not a member yet",
};

/** Membership: status, join, and Stripe's portal for everything money. */
export function MembershipCard() {
  const cardRef = useRef<HTMLElement>(null);
  // Stripe sends the rider back with ?billing=success or ?billing=cancelled.
  // Read once, then take it out of the URL so a reload doesn't repeat it.
  const [returned, setReturned] = useState<"success" | "cancelled" | null>(null);
  const [error, setError] = useState("");
  const [slow, setSlow] = useState(false);

  useEffect(() => {
    const outcome = new URLSearchParams(window.location.search).get("billing");
    if (outcome !== "success" && outcome !== "cancelled") return;
    setReturned(outcome);
    const url = new URL(window.location.href);
    url.searchParams.delete("billing");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
    cardRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, []);

  // If Stripe's confirmation never arrives, say so rather than spin forever.
  useEffect(() => {
    if (returned !== "success") return;
    const timer = setTimeout(() => setSlow(true), 30000);
    return () => clearTimeout(timer);
  }, [returned]);

  const { data: status } = useQuery({
    queryKey: ["billing-status"],
    queryFn: () => billing.getStatus(),
    // Stripe confirms the payment to the server a few seconds after the
    // rider lands back here; keep asking until it has.
    refetchInterval: (query) => {
      const s = query.state.data?.status;
      return returned === "success" && !["active", "trialing"].includes(s ?? "")
        ? 2000
        : false;
    },
  });

  // Invisible until Stripe is configured server-side: never advertise a
  // door that isn't fitted.
  if (!status || !status.configured) return null;

  const goto = async (fn: () => Promise<{ url: string }>) => {
    setError("");
    try {
      const { url } = await fn();
      window.location.href = url;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Stripe didn't answer. Try again in a minute.");
    }
  };

  const isMember = ["active", "trialing", "past_due"].includes(status.status);

  const confirmed = ["active", "trialing"].includes(status.status);

  return (
    <section
      ref={cardRef}
      className="rounded-sm border border-vb-border-subtle bg-vb-surface p-6"
    >
      {returned && (
        <div
          role="status"
          className="mb-5 flex items-start justify-between gap-4 border border-vb-border-subtle bg-vb-sunken p-4"
        >
          <div>
            <Kicker dot={returned === "success"}>
              {returned === "cancelled"
                ? "No payment taken"
                : confirmed
                  ? "You're in"
                  : "Confirming your payment"}
            </Kicker>
            <p className="mt-2 text-sm leading-relaxed text-vb-text-dim">
              {returned === "cancelled"
                ? "Nothing was charged. Join whenever you're ready."
                : confirmed
                  ? "Payment received and your membership is live. Your plan, your coach and everything it remembers carry straight on."
                  : slow
                    ? "This is taking longer than it should. Your payment is safe with Stripe; refresh in a minute, and if it still says this, email gareth@ridewithforma.com and I'll sort it the same day."
                    : "Stripe has the payment and is confirming it with Forma now. This usually takes a few seconds."}
            </p>
          </div>
          <button
            type="button"
            onClick={() => setReturned(null)}
            aria-label="Dismiss"
            className="-m-1 flex-none p-1 text-vb-text-muted hover:text-vb-text"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}
      <div className="flex items-center justify-between gap-3">
        <h2 className="f-display text-2xl text-vb-text">Membership</h2>
        <Badge variant={isMember ? "ink" : "outline"}>
          {STATUS_LABELS[status.status] ?? status.status}
        </Badge>
      </div>

      {isMember ? (
        <div className="mt-4 space-y-3">
          {status.status === "past_due" && (
            <div className="border border-vb-red/40 bg-vb-surface p-4">
              <p className="text-sm text-vb-text-dim">
                Your last payment didn&apos;t go through. Update the card in
                Manage billing and nothing is interrupted; Stripe retries the
                payment for a few days.
              </p>
            </div>
          )}
          {status.period_end && (
            <p className="f-data text-xs text-vb-text-muted">
              Current month runs to {formatDate(status.period_end)}
            </p>
          )}
          <Button size="sm" onClick={() => goto(billing.portal)}>
            Manage billing
          </Button>
          {error && <p className="text-sm text-vb-red">{error}</p>}
        </div>
      ) : (
        <div className="mt-4 border border-dashed border-vb-border p-5">
          <p className="text-sm leading-relaxed text-vb-text-dim">
            The coach and everything it remembers, a plan that changes when
            your week does, a briefing before you ride each day, and a voice
            in your ear on the turbo. A human coach runs about £150 a month
            before the bike fit. As one of the founding hundred you pay
            £14.99 a month, locked for as long as you stay; after the hundred
            the price is £19.99. Cancel any time in Manage billing and you
            keep everything until the end of the month you&apos;ve paid for.
          </p>
          <p className="mt-3 text-sm text-vb-text-dim">
            I read every founding rider&apos;s first month personally.{" "}
            <span className="f-signature text-lg text-vb-red">G</span>
          </p>
          <Button variant="flamme" className="mt-4" onClick={() => goto(billing.checkout)}>
            Join Forma
            <Arrow />
          </Button>
          {error && <p className="mt-3 text-sm text-vb-red">{error}</p>}
        </div>
      )}
    </section>
  );
}
