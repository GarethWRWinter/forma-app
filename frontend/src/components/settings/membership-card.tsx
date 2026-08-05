"use client";

import { useQuery } from "@tanstack/react-query";
import { billing } from "@/lib/api";
import { formatDate } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button, Arrow } from "@/components/ui/button";

const STATUS_LABELS: Record<string, string> = {
  active: "Active",
  trialing: "Trial",
  past_due: "Payment issue",
  canceled: "Cancelled",
  none: "Not a member yet",
};

/** Membership: status, join, and Stripe's portal for everything money. */
export function MembershipCard() {
  const { data: status } = useQuery({
    queryKey: ["billing-status"],
    queryFn: () => billing.getStatus(),
  });

  // Invisible until Stripe is configured server-side: never advertise a
  // door that isn't fitted.
  if (!status || !status.configured) return null;

  const goto = async (fn: () => Promise<{ url: string }>) => {
    const { url } = await fn();
    window.location.href = url;
  };

  const isMember = ["active", "trialing", "past_due"].includes(status.status);

  return (
    <section className="rounded-sm border border-vb-border-subtle bg-vb-surface p-6">
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
                Your last payment didn&apos;t go through. Update the card and
                nothing is interrupted, Stripe retries for a few days.
              </p>
            </div>
          )}
          {status.period_end && (
            <p className="f-data text-xs text-vb-text-muted">
              Renews {formatDate(status.period_end)}
            </p>
          )}
          <Button size="sm" onClick={() => goto(billing.portal)}>
            Manage billing
          </Button>
        </div>
      ) : (
        <div className="mt-4 border border-dashed border-vb-border p-5">
          <p className="text-sm leading-relaxed text-vb-text-dim">
            Full membership: the coach, the memory, the plan that bends
            around your life. Founding riders keep their price for as long
            as they stay.
          </p>
          <Button variant="flamme" className="mt-4" onClick={() => goto(billing.checkout)}>
            Join Forma
            <Arrow />
          </Button>
        </div>
      )}
    </section>
  );
}
