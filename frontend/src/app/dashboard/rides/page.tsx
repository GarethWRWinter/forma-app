"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Upload } from "lucide-react";
import Link from "next/link";
import { useRef, useState } from "react";
import { rides, type Ride } from "@/lib/api";
import { formatDate, formatDuration, formatPower, formatTSS } from "@/lib/utils";
import { ZONES } from "@/lib/palette";
import { rideStory, dominantZone } from "@/lib/rideStory";
import { RideShape, ZoneStrip } from "@/components/ui/ride-shape";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Kicker } from "@/components/ui/kicker";
import { EmptyState } from "@/components/ui/empty-state";

const SOURCE_LABELS: Record<string, string> = {
  strava: "Strava",
  fit_upload: "Upload",
  in_app: "In-app",
  dropbox: "Dropbox",
};

const ZONE_LABELS: Record<string, string> = {
  z1: "Recovery",
  z2: "Endurance",
  z3: "Tempo",
  z4: "Threshold",
  z5: "VO2",
  z6: "Anaerobic",
  z7: "Sprint",
};

/** Dominant-zone tag: a small colour block + the zone's name in mono. */
function ZoneTag({ ride }: { ride: Ride }) {
  const dom = dominantZone(ride);
  if (!dom) return null;
  return (
    <span className="flex items-center gap-1.5">
      <span
        className="inline-block h-2.5 w-2.5"
        style={{ background: ZONES[dom as keyof typeof ZONES] }}
      />
      <span className="f-kicker text-vb-text-dim">{ZONE_LABELS[dom]}</span>
    </span>
  );
}

export default function RidesPage() {
  const queryClient = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [page, setPage] = useState(1);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");

  const { data, isLoading } = useQuery({
    queryKey: ["rides", page],
    queryFn: () => rides.list(page, 20),
  });

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    setUploading(true);
    setError("");
    try {
      await rides.upload(file);
      queryClient.invalidateQueries({ queryKey: ["rides"] });
      queryClient.invalidateQueries({ queryKey: ["fitness-summary"] });
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Upload failed";
      setError(msg);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const totalPages = data ? Math.ceil(data.total / 20) : 0;

  return (
    <div className="space-y-10">
      {/* ============ MASTHEAD ============ */}
      <header className="f-rise flex items-end justify-between gap-6 border-b-2 border-vb-border-strong pb-5">
        <div>
          <Kicker className="mb-2">The training log</Kicker>
          <h1 className="f-display text-5xl leading-[0.95] md:text-6xl">
            Rides.
          </h1>
          <p className="f-data mt-3 text-xs text-vb-text-muted">
            {data?.total ?? "-"} rides on record · page {page}
            {totalPages > 0 && ` of ${totalPages}`}
          </p>
        </div>

        <div className="shrink-0">
          <input
            ref={fileInputRef}
            type="file"
            accept=".fit"
            onChange={handleUpload}
            className="hidden"
          />
          <Button
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
          >
            <Upload className="h-3.5 w-3.5" />
            {uploading ? "Reading the file…" : "Upload FIT"}
          </Button>
        </div>
      </header>

      {error && (
        <div className="border border-vb-border-subtle border-l-[3px] border-l-vb-red bg-vb-surface px-4 py-3 text-sm text-vb-text">
          {error}
        </div>
      )}

      {/* ============ RIDES LIST ============ */}
      {isLoading ? (
        <div className="border border-vb-border-subtle px-5 py-10 text-center text-sm text-vb-text-dim">
          Forma is fetching your rides…
        </div>
      ) : data?.rides.length === 0 ? (
        <EmptyState
          kicker="The log is open"
          title="No rides yet, and I'd love to see what you can do."
          action={
            <Button
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading}
            >
              <Upload className="h-3.5 w-3.5" />
              Upload a FIT file
            </Button>
          }
        >
          Upload a FIT file, or connect Strava in Settings and Forma reads
          every ride you have ever logged, overnight. Each one sharpens your
          power profile.
        </EmptyState>
      ) : (
        <>
          {/* Column rubric, visible on lg+ only, gives editorial structure */}
          <div className="hidden border-b border-vb-border-subtle pb-2 lg:grid lg:grid-cols-[1fr_90px_90px_90px_90px_70px_60px] lg:gap-6">
            <span className="f-kicker text-vb-text-muted">Ride</span>
            <span className="f-kicker text-right text-vb-text-muted">
              Time
            </span>
            <span className="f-kicker text-right text-vb-text-muted">
              Distance
            </span>
            <span className="f-kicker text-right text-vb-text-muted">Avg</span>
            <span className="f-kicker text-right text-vb-text-muted">NP</span>
            <span className="f-kicker text-right text-vb-text-muted">TSS</span>
            <span className="f-kicker text-right text-vb-text-muted">IF</span>
          </div>

          <ul className="f-stagger">
            {data?.rides.map((ride, idx) => (
              <li
                key={ride.id}
                className={
                  idx > 0 ? "border-t border-vb-border-subtle" : undefined
                }
              >
                <Link
                  href={`/dashboard/rides/${ride.id}`}
                  className="block py-4 transition-colors hover:-mx-3 hover:bg-vb-surface hover:px-3 lg:grid lg:grid-cols-[1fr_90px_90px_90px_90px_70px_60px] lg:items-baseline lg:gap-6"
                >
                  {/* Title + rubric + the ride's story */}
                  <div className="min-w-0">
                    <div className="mb-1 flex items-center gap-2.5">
                      <span className="f-kicker text-vb-text-muted">
                        {formatDate(ride.ride_date)}
                      </span>
                      {ride.source && SOURCE_LABELS[ride.source] && (
                        <Badge variant="outline">
                          {SOURCE_LABELS[ride.source]}
                        </Badge>
                      )}
                      <ZoneTag ride={ride} />
                    </div>
                    <p className="truncate text-base font-medium text-vb-text">
                      {ride.forma_title || ride.title}
                    </p>
                    {(ride.story || rideStory(ride)) && (
                      <p className="mt-1 max-w-xl text-xs leading-relaxed text-vb-text-dim">
                        {ride.story || rideStory(ride)}
                      </p>
                    )}
                    <RideShape ride={ride} className="mt-2.5" />
                    <ZoneStrip ride={ride} className="mt-1" />
                  </div>

                  {/* Compact numbers row, visible always, repositioned on lg */}
                  <div className="f-data mt-2 flex flex-wrap gap-x-5 gap-y-1 text-sm text-vb-text lg:hidden">
                    {ride.duration_seconds != null && (
                      <NumWithLabel
                        n={formatDuration(ride.duration_seconds)}
                        label="Time"
                      />
                    )}
                    {ride.distance_meters != null && (
                      <NumWithLabel
                        n={`${(ride.distance_meters / 1000).toFixed(1)} km`}
                        label="Dist"
                      />
                    )}
                    {ride.normalized_power != null && (
                      <NumWithLabel
                        n={`${Math.round(ride.normalized_power)}W`}
                        label="NP"
                      />
                    )}
                    {ride.tss != null && (
                      <NumWithLabel n={Math.round(ride.tss)} label="TSS" />
                    )}
                  </div>

                  {/* Desktop columns */}
                  <span className="f-data hidden text-right text-sm text-vb-text lg:inline">
                    {ride.duration_seconds
                      ? formatDuration(ride.duration_seconds)
                      : "-"}
                  </span>
                  <span className="f-data hidden text-right text-sm text-vb-text lg:inline">
                    {ride.distance_meters
                      ? `${(ride.distance_meters / 1000).toFixed(1)} km`
                      : "-"}
                  </span>
                  <span className="f-data hidden text-right text-sm text-vb-text lg:inline">
                    {ride.average_power
                      ? `${Math.round(ride.average_power)}`
                      : "-"}
                  </span>
                  <span className="f-data hidden text-right text-sm text-vb-text lg:inline">
                    {ride.normalized_power
                      ? `${Math.round(ride.normalized_power)}`
                      : "-"}
                  </span>
                  <span className="f-data hidden text-right text-sm font-semibold text-vb-text lg:inline">
                    {ride.tss ? Math.round(ride.tss) : "-"}
                  </span>
                  <span className="f-data hidden text-right text-sm text-vb-text lg:inline">
                    {ride.intensity_factor
                      ? ride.intensity_factor.toFixed(2)
                      : "-"}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </>
      )}

      {/* ============ PAGINATION ============ */}
      {data && data.total > 20 && (
        <div className="flex items-center justify-between gap-4 border-t border-vb-border-subtle pt-5">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
          >
            ← Previous
          </Button>
          <span className="f-data text-xs text-vb-text-muted">
            Page {page} of {totalPages}
          </span>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setPage((p) => p + 1)}
            disabled={page * 20 >= data.total}
          >
            Next →
          </Button>
        </div>
      )}
    </div>
  );
}

// Small mobile-friendly "value + label" inline helper.
function NumWithLabel({ n, label }: { n: string | number; label: string }) {
  return (
    <span className="f-data">
      {n}
      <span className="ml-0.5 text-[10px] uppercase tracking-[0.12em] text-vb-text-dim">
        {label}
      </span>
    </span>
  );
}
