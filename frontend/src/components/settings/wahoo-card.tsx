"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { wahoo, type WahooStatus } from "@/lib/api";
import { formatDate } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button, Arrow } from "@/components/ui/button";
import { Kicker } from "@/components/ui/kicker";

/** Wahoo Cloud link: ride ends, ELEMNT syncs, the ride is in Forma before
    the bike is racked. The premium door for ride data. */
export function WahooCard() {
  const queryClient = useQueryClient();

  const { data: status } = useQuery({
    queryKey: ["wahoo-status"],
    queryFn: () => wahoo.getStatus(),
    refetchInterval: (query) => {
      const data = query.state.data as WahooStatus | undefined;
      return data?.backfill?.status === "running" ? 4000 : false;
    },
  });

  const sync = useMutation({
    mutationFn: () => wahoo.sync(),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["wahoo-status"] });
      queryClient.invalidateQueries({ queryKey: ["rides"] });
      alert(
        data.synced > 0
          ? `${data.synced} rides in from Wahoo`
          : "Wahoo answered, but had nothing new since the last sync."
      );
    },
    onError: (err: Error) => alert(err.message),
  });

  const connect = async () => {
    const { auth_url } = await wahoo.getAuthUrl();
    window.location.href = auth_url;
  };

  // The integration is dormant until the Wahoo app credentials exist
  // server-side; don't advertise a door that isn't fitted yet.
  if (status && !status.configured && !status.connected) return null;

  return (
    <section className="rounded-sm border border-vb-border-subtle bg-vb-surface p-6">
      <div className="flex items-center justify-between gap-3">
        <h2 className="f-display text-2xl text-vb-text">Wahoo</h2>
        {status?.connected &&
          (status.needs_reauth ? (
            <Badge variant="outline">Needs reconnecting</Badge>
          ) : (
            <Badge variant="ink">Linked</Badge>
          ))}
      </div>

      {status?.connected && status.needs_reauth ? (
        <div className="mt-4 space-y-3">
          <div className="border border-vb-red/40 bg-vb-surface p-4">
            <p className="text-sm text-vb-text-dim">
              Wahoo stopped accepting our connection, which happens from time
              to time with their tokens, so new rides have not been arriving.
              Reconnect and everything picks up where it left off, including
              the rides you did in the meantime.
            </p>
          </div>
          <Button size="sm" onClick={connect}>
            Reconnect Wahoo
          </Button>
        </div>
      ) : status?.connected ? (
        <div className="mt-4 space-y-3">
          <p className="text-sm text-vb-text-dim">
            Finish the ride, and it&apos;s here before the bike is racked.
            Wahoo pushes every ride to Forma the moment your ELEMNT syncs.
          </p>

          {status.backfill?.status === "running" && (
            <div className="border border-vb-border-subtle bg-vb-sunken p-4">
              <Kicker dot flamme>
                Reading your history
              </Kicker>
              <p className="f-data mt-3 text-2xl font-semibold leading-none text-vb-text">
                {status.backfill.progress}
                {status.backfill.total ? (
                  <span className="text-vb-text-muted"> / {status.backfill.total}</span>
                ) : null}
              </p>
              <p className="mt-2 text-xs text-vb-text-dim">
                workouts read from your Wahoo account
              </p>
            </div>
          )}

          {status.backfill?.status === "failed" && (
            <div className="border border-vb-red/40 bg-vb-surface p-4">
              <Kicker flamme>Import stopped</Kicker>
              <p className="mt-2 text-sm text-vb-text-dim">
                Not your fault. Retry and it picks up where it left off.
              </p>
              <Button
                size="sm"
                variant="ghost"
                className="mt-3"
                onClick={async () => {
                  await wahoo.startBackfill();
                  queryClient.invalidateQueries({ queryKey: ["wahoo-status"] });
                }}
              >
                Retry import
              </Button>
            </div>
          )}

          {status.backfill?.status !== "running" && (
            <div className="border border-vb-border-subtle bg-vb-bg p-4">
              <p className="text-sm text-vb-text-dim">
                Pull your full Wahoo history. Already-imported rides are
                skipped, so this is always safe to run.
              </p>
              <Button
                size="sm"
                className="mt-3"
                onClick={async () => {
                  await wahoo.startBackfill();
                  queryClient.invalidateQueries({ queryKey: ["wahoo-status"] });
                }}
              >
                Import full history
              </Button>
            </div>
          )}

          {status.last_sync_at && (
            <p className="f-data text-xs text-vb-text-muted">
              Last synced {formatDate(status.last_sync_at)}
            </p>
          )}

          {/* Two buttons with one explanation between them is a guess, and the
              founder guessed wrong on his own product. Say what each does. */}
          <p className="text-sm text-vb-text-dim">
            You should never need this. Rides arrive on their own. It is here
            for the day one looks missing: it fetches your last few rides
            straight from Wahoo, and skips anything already here.
          </p>

          <div className="flex gap-2">
            <Button size="sm" onClick={() => sync.mutate()} disabled={sync.isPending}>
              <RefreshCw
                className={`h-3.5 w-3.5 ${sync.isPending ? "animate-spin" : ""}`}
              />
              {sync.isPending ? "Fetching…" : "Fetch missing rides"}
            </Button>
            <Button
              size="sm"
              variant="quiet"
              onClick={async () => {
                await wahoo.disconnect();
                queryClient.invalidateQueries({ queryKey: ["wahoo-status"] });
              }}
            >
              Disconnect
            </Button>
          </div>
        </div>
      ) : (
        <div className="mt-4 border border-dashed border-vb-border p-5">
          <p className="text-sm leading-relaxed text-vb-text-dim">
            Link your Wahoo account and every ride arrives on its own, straight
            off the head unit, the moment your ELEMNT syncs. Your history
            imports overnight.
          </p>
          <Button variant="flamme" className="mt-4" onClick={connect}>
            Connect Wahoo
            <Arrow />
          </Button>
        </div>
      )}
    </section>
  );
}
