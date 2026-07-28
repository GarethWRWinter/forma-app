/**
 * Strava / Garmin account-archive import, entirely browser-side.
 *
 * The zip is never uploaded. It streams through fflate's Unzip so even a
 * multi-gigabyte archive (photos and all) uses a few MB of memory: we only
 * inflate the entries we want. Two passes over the local file:
 *
 *   1. scanArchive  — reads activities.csv (Strava's manifest) and lists
 *      ride-file entries, so the UI can say "2,847 rides, 2014 to 2026"
 *      and offer a scope before a single byte is sent.
 *   2. importArchive — inflates the selected entries and uploads them in
 *      small concurrent batches. Compressed .fit.gz bytes are sent as-is
 *      (the server gunzips), so upload stays small.
 *
 * The server dedupes by start time + duration, so re-running an
 * interrupted import is safe: already-imported rides come back as
 * "duplicate" and the run finishes the remainder.
 */

import { Unzip, UnzipInflate } from "fflate";
import { rides } from "@/lib/api";

const RIDE_FILE_RE = /\.(fit|gpx|tcx)(\.gz)?$/i;

export interface ArchiveActivity {
  date: Date | null;
  name: string;
  type: string;
  /** Entry path inside the zip, e.g. "activities/123456789.fit.gz" */
  filename: string;
}

export interface ArchiveScan {
  /** Manifest rows that are rides (empty when no manifest found). */
  rides: ArchiveActivity[];
  /** Every ride-file entry path found in the zip. */
  fileEntries: string[];
  hasManifest: boolean;
  nestedZips: number;
  earliest: Date | null;
  latest: Date | null;
}

export interface ImportCounts {
  imported: number;
  duplicates: number;
  failed: number;
}

/** Minimal CSV row parser that honours quoted fields (Strava quotes names). */
function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        field += ch;
      }
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === ",") {
      row.push(field);
      field = "";
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(field);
      field = "";
      if (row.length > 1 || row[0] !== "") rows.push(row);
      row = [];
    } else {
      field += ch;
    }
  }
  if (field !== "" || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

function manifestRides(csvText: string): ArchiveActivity[] {
  const rows = parseCsv(csvText);
  if (rows.length < 2) return [];
  const header = rows[0].map((h) => h.trim().toLowerCase());
  const dateIdx = header.indexOf("activity date");
  const nameIdx = header.indexOf("activity name");
  const typeIdx = header.indexOf("activity type");
  const fileIdx = header.indexOf("filename");
  if (fileIdx === -1) return [];

  const out: ArchiveActivity[] = [];
  for (const row of rows.slice(1)) {
    const filename = row[fileIdx]?.trim();
    if (!filename || !RIDE_FILE_RE.test(filename)) continue;
    const type = typeIdx >= 0 ? row[typeIdx]?.trim() || "" : "";
    // Anything Strava calls a ride: Ride, Virtual Ride, Gravel Ride, ...
    if (type && !/ride/i.test(type)) continue;
    let date: Date | null = null;
    if (dateIdx >= 0 && row[dateIdx]) {
      const parsed = new Date(row[dateIdx]);
      if (!isNaN(parsed.getTime())) date = parsed;
    }
    out.push({
      date,
      name: nameIdx >= 0 ? row[nameIdx]?.trim() || "" : "",
      type,
      filename,
    });
  }
  return out;
}

export async function scanArchive(file: File, signal?: AbortSignal): Promise<ArchiveScan> {
  let manifestText: string | null = null;
  const fileEntries: string[] = [];
  let nestedZips = 0;

  await streamZipSimple(
    file,
    (name) => {
      const lower = name.toLowerCase();
      if (lower.endsWith("activities.csv")) return "collect";
      if (RIDE_FILE_RE.test(lower)) {
        fileEntries.push(name);
        return "skip";
      }
      if (lower.endsWith(".zip")) nestedZips++;
      return "skip";
    },
    (name, bytes) => {
      manifestText = new TextDecoder().decode(bytes);
      void name;
    },
    signal
  );

  const rideRows = manifestText ? manifestRides(manifestText) : [];
  const entrySet = new Set(fileEntries);
  // Manifest filenames are relative to the archive root and must exist.
  const confirmed = rideRows.filter((r) => entrySet.has(r.filename));
  const dates = confirmed.map((r) => r.date).filter((d): d is Date => d !== null);

  return {
    rides: confirmed,
    fileEntries,
    hasManifest: manifestText !== null && rideRows.length > 0,
    nestedZips,
    earliest: dates.length ? new Date(Math.min(...dates.map((d) => d.getTime()))) : null,
    latest: dates.length ? new Date(Math.max(...dates.map((d) => d.getTime()))) : null,
  };
}

/**
 * Simpler streaming walk: decide per entry ("collect" inflates and hands the
 * full bytes to onBytes; "skip" costs nothing).
 */
async function streamZipSimple(
  file: File,
  decide: (name: string) => "collect" | "skip",
  onBytes: (name: string, bytes: Uint8Array) => void | Promise<void>,
  signal?: AbortSignal,
  maxInFlight = 8
): Promise<void> {
  const unzipper = new Unzip();
  unzipper.register(UnzipInflate);
  let pending: Promise<void>[] = [];
  let streamError: unknown = null;

  unzipper.onfile = (entry) => {
    if (decide(entry.name) !== "collect") return;
    const chunks: Uint8Array[] = [];
    const done = new Promise<void>((resolve, reject) => {
      entry.ondata = (err, data, final) => {
        if (err) return reject(err);
        if (data) chunks.push(data);
        if (final) {
          const total = chunks.reduce((n, c) => n + c.length, 0);
          const merged = new Uint8Array(total);
          let offset = 0;
          for (const c of chunks) {
            merged.set(c, offset);
            offset += c.length;
          }
          Promise.resolve(onBytes(entry.name, merged)).then(resolve, reject);
        }
      };
    });
    pending.push(
      done.catch((err) => {
        streamError = streamError ?? err;
      })
    );
    entry.start();
  };

  const reader = file.stream().getReader();
  try {
    for (;;) {
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      const { done, value } = await reader.read();
      if (done) {
        unzipper.push(new Uint8Array(0), true);
        break;
      }
      unzipper.push(value, false);
      if (pending.length >= maxInFlight) {
        await Promise.all(pending);
        pending = [];
      }
    }
  } finally {
    reader.cancel().catch(() => undefined);
  }
  await Promise.all(pending);
  if (streamError) throw streamError;
}

export async function importArchive(
  file: File,
  wanted: Set<string>,
  onProgress: (done: number, total: number, counts: ImportCounts) => void,
  signal?: AbortSignal
): Promise<ImportCounts> {
  const counts: ImportCounts = { imported: 0, duplicates: 0, failed: 0 };
  let done = 0;
  const total = wanted.size;

  await streamZipSimple(
    file,
    (name) => (wanted.has(name) ? "collect" : "skip"),
    async (name, bytes) => {
      if (signal?.aborted) return;
      const basename = name.split("/").pop() || name;
      try {
        const result = await rides.importFile(basename, bytes);
        if (result.status === "imported") counts.imported++;
        else if (result.status === "duplicate") counts.duplicates++;
        else counts.failed++;
      } catch {
        counts.failed++;
      }
      done++;
      onProgress(done, total, { ...counts });
    },
    signal,
    4 // concurrency: at most 4 inflated files in flight at once
  );

  return counts;
}
