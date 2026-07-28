"use client";

import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Upload } from "lucide-react";
import { rides } from "@/lib/api";
import {
  scanArchive,
  importArchive,
  type ArchiveScan,
  type ImportCounts,
} from "@/lib/archiveImport";
import { Button, Arrow } from "@/components/ui/button";
import { Kicker } from "@/components/ui/kicker";

type Stage = "idle" | "scanning" | "scoped" | "importing" | "done" | "error";

const SCOPES = [
  { key: "all", label: "Everything" },
  { key: "3y", label: "Last 3 years" },
  { key: "1y", label: "Last 12 months" },
] as const;

type ScopeKey = (typeof SCOPES)[number]["key"];

function scopeCutoff(scope: ScopeKey): Date | null {
  if (scope === "all") return null;
  const d = new Date();
  d.setFullYear(d.getFullYear() - (scope === "3y" ? 3 : 1));
  return d;
}

/** Strava / Garmin account-archive import. The zip stays on the rider's
    machine; only the ride files inside it are uploaded. */
export function ArchiveImport() {
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [stage, setStage] = useState<Stage>("idle");
  const [error, setError] = useState("");
  const [archive, setArchive] = useState<File | null>(null);
  const [scan, setScan] = useState<ArchiveScan | null>(null);
  const [scope, setScope] = useState<ScopeKey>("all");
  const [progress, setProgress] = useState({ done: 0, total: 0 });
  const [counts, setCounts] = useState<ImportCounts>({
    imported: 0,
    duplicates: 0,
    failed: 0,
  });

  const reset = () => {
    setStage("idle");
    setError("");
    setArchive(null);
    setScan(null);
    setScope("all");
    if (fileRef.current) fileRef.current.value = "";
  };

  const handlePick = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setArchive(file);
    setStage("scanning");
    setError("");
    try {
      const result = await scanArchive(file);
      setScan(result);
      if (!result.fileEntries.length) {
        setError(
          result.nestedZips > 0
            ? "This archive holds further zips inside it (Garmin exports do this). Unzip it once on your computer, then choose one of the inner zips here."
            : "No ride files in this zip. Forma looks for .fit, .gpx and .tcx files, including gzipped ones."
        );
        setStage("error");
        return;
      }
      setStage("scoped");
    } catch {
      setError("Couldn't read that zip. Try re-downloading the archive.");
      setStage("error");
    }
  };

  const selectedEntries = (): Set<string> => {
    if (!scan) return new Set();
    // With a manifest we can filter rides by date; without one we take
    // every ride file in the archive.
    if (!scan.hasManifest) return new Set(scan.fileEntries);
    const cutoff = scopeCutoff(scope);
    return new Set(
      scan.rides
        .filter((r) => !cutoff || (r.date && r.date >= cutoff))
        .map((r) => r.filename)
    );
  };

  const selectedCount = stage === "scoped" ? selectedEntries().size : 0;

  const runImport = async () => {
    if (!archive) return;
    const wanted = selectedEntries();
    setStage("importing");
    setProgress({ done: 0, total: wanted.size });
    try {
      const final = await importArchive(archive, wanted, (done, total, c) => {
        setProgress({ done, total });
        setCounts(c);
      });
      setCounts(final);
      await rides.finalizeImport().catch(() => undefined);
      queryClient.invalidateQueries({ queryKey: ["rides"] });
      queryClient.invalidateQueries({ queryKey: ["fitness-summary"] });
      setStage("done");
    } catch {
      setError(
        "The import stopped partway. Everything already in is safe. Run it again and Forma skips straight past what it has."
      );
      setStage("error");
    }
  };

  const yearRange =
    scan?.earliest && scan?.latest
      ? `${scan.earliest.getFullYear()} to ${scan.latest.getFullYear()}`
      : null;

  return (
    <section className="rounded-sm border border-vb-border-subtle bg-vb-surface p-6">
      <div className="flex items-center justify-between gap-3">
        <h2 className="f-display text-2xl text-vb-text">Ride archive</h2>
      </div>

      <input
        ref={fileRef}
        type="file"
        accept=".zip"
        onChange={handlePick}
        className="hidden"
      />

      {stage === "idle" && (
        <div className="mt-4 border border-dashed border-vb-border p-5">
          <p className="text-sm leading-relaxed text-vb-text-dim">
            Bring your whole history with you. Download your archive from
            Strava (Settings, My Account, Download Request) or Garmin, drop
            the zip here, and every ride you&apos;ve ever recorded starts
            working for you. The zip never leaves your machine, Forma reads
            the ride files out of it right here.
          </p>
          <Button className="mt-4" onClick={() => fileRef.current?.click()}>
            <Upload className="h-3.5 w-3.5" />
            Choose the zip
          </Button>
        </div>
      )}

      {stage === "scanning" && (
        <div className="mt-4 border border-vb-border-subtle bg-vb-sunken p-4">
          <Kicker dot flamme>
            Reading the archive
          </Kicker>
          <p className="mt-2 text-xs text-vb-text-dim">
            Nothing is uploading yet, Forma is finding your rides first.
          </p>
        </div>
      )}

      {stage === "scoped" && scan && (
        <div className="mt-4 space-y-4">
          <div className="border border-vb-border-subtle bg-vb-sunken p-4">
            <p className="f-data text-2xl font-semibold leading-none text-vb-text">
              {scan.hasManifest ? scan.rides.length : scan.fileEntries.length}
              <span className="text-sm font-normal text-vb-text-muted"> rides found</span>
            </p>
            {yearRange && (
              <p className="f-data mt-1 text-xs text-vb-text-muted">
                spanning {yearRange}
              </p>
            )}
          </div>

          {scan.hasManifest && (
            <div>
              <Kicker className="mb-2">How far back</Kicker>
              <div className="flex gap-2">
                {SCOPES.map((s) => (
                  <button
                    key={s.key}
                    onClick={() => setScope(s.key)}
                    className={
                      "f-kicker border px-3 py-1.5 transition-colors " +
                      (scope === s.key
                        ? "border-vb-text bg-vb-text text-vb-bg"
                        : "border-vb-border text-vb-text-dim hover:border-vb-border-strong")
                    }
                  >
                    {s.label}
                  </button>
                ))}
              </div>
              <p className="mt-2 text-xs text-vb-text-dim">
                Full history feeds your all-time records in the Palmarès.
                Recent years are enough for training decisions.
              </p>
            </div>
          )}

          <div className="flex items-center gap-3">
            <Button variant="flamme" onClick={runImport} disabled={selectedCount === 0}>
              Import {selectedCount} rides
              <Arrow />
            </Button>
            <Button variant="quiet" onClick={reset}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      {stage === "importing" && (
        <div className="mt-4 border border-vb-border-subtle bg-vb-sunken p-4">
          <Kicker dot flamme>
            Reading your history
          </Kicker>
          <p className="f-data mt-3 text-2xl font-semibold leading-none text-vb-text">
            {progress.done}
            <span className="text-vb-text-muted"> / {progress.total}</span>
          </p>
          <div className="mt-3 h-1 w-full overflow-hidden bg-vb-border-subtle">
            <div
              className="h-full bg-vb-red transition-all duration-300"
              style={{
                width: `${progress.total ? Math.round((progress.done / progress.total) * 100) : 0}%`,
              }}
            />
          </div>
          <p className="mt-2 text-xs text-vb-text-dim">
            rides read, remembered, working for you. Safe to leave this page
            open in the background.
          </p>
        </div>
      )}

      {stage === "done" && (
        <div className="mt-4 space-y-3">
          <div className="border border-vb-border-subtle bg-vb-sunken p-4">
            <p className="text-sm text-vb-text">
              History in.{" "}
              <span className="f-data font-semibold">{counts.imported}</span>{" "}
              rides imported
              {counts.duplicates > 0 && (
                <>
                  ,{" "}
                  <span className="f-data font-semibold">{counts.duplicates}</span>{" "}
                  already on record
                </>
              )}
              {counts.failed > 0 && (
                <>
                  ,{" "}
                  <span className="f-data font-semibold">{counts.failed}</span>{" "}
                  unreadable
                </>
              )}
              .
            </p>
            <p className="mt-1 text-xs text-vb-text-dim">
              Your fitness history is rebuilding now. Titles and stories
              arrive as Forma reads through them.
            </p>
          </div>
          <Button variant="ghost" size="sm" onClick={reset}>
            Import another archive
          </Button>
        </div>
      )}

      {stage === "error" && (
        <div className="mt-4 space-y-3">
          <div className="border border-vb-red/40 bg-vb-surface p-4">
            <Kicker flamme>That didn&apos;t work</Kicker>
            <p className="mt-2 text-sm text-vb-text-dim">{error}</p>
          </div>
          <Button variant="ghost" size="sm" onClick={reset}>
            Try again
          </Button>
        </div>
      )}
    </section>
  );
}
