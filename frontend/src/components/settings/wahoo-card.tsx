"use client";

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, X } from "lucide-react";
import { wahoo, type WahooStatus } from "@/lib/api";
import { formatDate } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button, Arrow } from "@/components/ui/button";
import { Kicker } from "@/components/ui/kicker";

/** What Wahoo's OAuth round trip sends us back with: ?wahoo=connected, or
    ?wahoo=error&reason=... The page used to ignore both, so a refused
    reconnect looked like the button doing nothing, and the founder clicked it
    three times before giving up. A customer gives up after one. */
type ReturnNotice = {
  tone: "ok" | "bad";
  title: string;
  body: React.ReactNode;
};

function readReturnNotice(search: string): ReturnNotice | null {
  const params = new URLSearchParams(search);
  const outcome = params.get("wahoo");
  if (!outcome) return null;
  if (outcome === "connected") {
    return {
      tone: "ok",
      title: "Wahoo linked",
      body: "Any rides from while it was disconnected are on their way. Give it a minute, then look in Rides.",
    };
  }
  switch (params.get("reason")) {
    case "token_cap":
      return {
        tone: "bad",
        title: "Wahoo refused the reconnect",
        body: (
          <>
            Forma still has ten keys in your Wahoo account, and Wahoo will not
            issue another until they are cleared. In the Wahoo app: Settings,
            then Authorized Apps, then Forma, then Deauthorize (or remove Forma
            at{" "}
            <a
              href="https://www.wahooligan.com/profile"
              target="_blank"
              rel="noreferrer"
              className="underline underline-offset-2 hover:text-vb-text"
            >
              wahooligan.com/profile
            </a>
            ). Then reconnect below. Nothing is lost.
          </>
        ),
      };
    case "denied":
      return {
        tone: "bad",
        title: "Nothing changed",
        body: "You cancelled on Wahoo's side, so Forma has no access. Reconnect whenever you like.",
      };
    case "invalid_state":
      return {
        tone: "bad",
        title: "That link had expired",
        body: "Wahoo took too long to send you back. Try Reconnect once more.",
      };
    default:
      return {
        tone: "bad",
        title: "Wahoo did not accept the connection",
        body: (
          <>
            Try once more in a minute. If it keeps happening, email{" "}
            <a
              href="mailto:gareth@ridewithforma.com?subject=Wahoo%20connection"
              className="underline underline-offset-2 hover:text-vb-text"
            >
              gareth@ridewithforma.com
            </a>{" "}
            and I will look at it the same day.
          </>
        ),
      };
  }
}

/** Wahoo Cloud link: ride ends, ELEMNT syncs, the ride is in Forma before
    the bike is racked. The premium door for ride data. */
export function WahooCard() {
  const queryClient = useQueryClient();
  const cardRef = useRef<HTMLElement>(null);
  const [notice, setNotice] = useState<ReturnNotice | null>(null);

  // Read the OAuth outcome on mount, then take it out of the URL so a reload
  // does not repeat it. window.location rather than useSearchParams: this
  // card is one client component among many on a page that prerenders, and
  // a bare useSearchParams there fails the build for want of a Suspense
  // boundary.
  useEffect(() => {
    const found = readReturnNotice(window.location.search);
    if (!found) return;
    setNotice(found);
    // The status the card is showing may predate the round trip.
    queryClient.invalidateQueries({ queryKey: ["wahoo-status"] });
    queryClient.invalidateQueries({ queryKey: ["rides"] });
    const url = new URL(window.location.href);
    url.searchParams.delete("wahoo");
    url.searchParams.delete("reason");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
    // The card sits below Membership; a rider bounced back to the top of
    // Settings otherwise sees nothing change.
    cardRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [queryClient]);

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
    <section
      ref={cardRef}
      className="rounded-sm border border-vb-border-subtle bg-vb-surface p-6"
    >
      {notice && (
        <div
          role="status"
          className={`mb-5 flex items-start justify-between gap-4 border p-4 ${
            notice.tone === "bad"
              ? "border-vb-red/40 bg-vb-surface"
              : "border-vb-border-subtle bg-vb-sunken"
          }`}
        >
          <div>
            <Kicker flamme={notice.tone === "bad"} dot={notice.tone === "ok"}>
              {notice.title}
            </Kicker>
            <p className="mt-2 text-sm leading-relaxed text-vb-text-dim">{notice.body}</p>
          </div>
          <button
            type="button"
            onClick={() => setNotice(null)}
            aria-label="Dismiss"
            className="-m-1 flex-none p-1 text-vb-text-muted hover:text-vb-text"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}
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
          {status.reauth_reason === "token_cap" ? (
            /* Wahoo allows an app ten keys per rider. Reconnect asks for an
               eleventh and is refused, so the rider has to clear the old ones
               first; sending them straight to Reconnect is a loop. */
            <div className="border border-vb-red/40 bg-vb-surface p-4 space-y-3">
              <p className="text-sm text-vb-text-dim">
                Wahoo allows an app ten keys per rider and Forma has used them
                all, which is our fault, not yours. Reconnect on its own
                won&apos;t clear it, so one step first:
              </p>
              <ol className="list-decimal space-y-2 pl-5 text-sm text-vb-text-dim">
                <li>
                  In the Wahoo app: Settings, then Authorized Apps, then Forma,
                  then Deauthorize. Or remove Forma at{" "}
                  <a
                    href="https://www.wahooligan.com/profile"
                    target="_blank"
                    rel="noreferrer"
                    className="underline underline-offset-2 hover:text-vb-text"
                  >
                    wahooligan.com/profile
                  </a>
                  .
                </li>
                <li>
                  Then reconnect below. Every ride you did in the meantime comes
                  back with it.
                </li>
              </ol>
            </div>
          ) : (
            <div className="border border-vb-red/40 bg-vb-surface p-4">
              <p className="text-sm text-vb-text-dim">
                Wahoo stopped accepting our connection, which happens from time
                to time with their tokens, so new rides have not been arriving.
                Reconnect and everything picks up where it left off, including
                the rides you did in the meantime.
              </p>
            </div>
          )}
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
