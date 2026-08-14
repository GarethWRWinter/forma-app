"use client";

import { useRef } from "react";
import { Paperclip, X } from "lucide-react";
import type { ChatAttachmentSummary } from "@/lib/api";
import { cn } from "@/lib/utils";
import { CadenceSpinner } from "@/components/ui/cadence-spinner";

/**
 * Handing a ride file to the coach, from the composer.
 *
 * The chip is the promise: the rider should be able to see, before they
 * press send, that the file arrived and that Forma read the right ride.
 * A filename alone proves nothing, so a settled chip carries a fact or
 * two off the parsed summary.
 */

/** Three per message. Past that the coach is reading a folder, not a ride. */
export const MAX_ATTACHMENTS = 3;

export const ATTACHMENT_ACCEPT = ".gpx,.fit,.tcx,.gz";

/**
 * A file with no words is still a question, but the coach needs one asked.
 * This lands in the composer where the rider can edit it or write over it,
 * which keeps the send button theirs to press.
 */
export const ATTACHMENT_ASK = "Take a look at this ride and tell me what you see.";

export interface ComposerAttachment {
  /** local handle, stable from the moment the rider picks the file */
  key: string;
  filename: string;
  status: "uploading" | "ready";
  /** server id, and only once the file has landed and parsed */
  id?: string;
  summary?: ChatAttachmentSummary | null;
}

function firstNumber(
  summary: ChatAttachmentSummary,
  keys: string[]
): number | null {
  for (const key of keys) {
    const value = summary[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

/**
 * Two facts, no more. Parsers spell their fields differently depending on
 * the file format, so read every plausible key rather than betting on one,
 * and say nothing at all when the summary has nothing to say.
 */
export function attachmentFacts(
  summary?: ChatAttachmentSummary | null
): string[] {
  if (!summary || typeof summary !== "object") return [];
  const facts: string[] = [];

  const km = firstNumber(summary, ["distance_km", "total_distance_km"]);
  const metres = firstNumber(summary, [
    "distance_m",
    "distance_meters",
    "total_distance",
  ]);
  const distanceKm = km ?? (metres !== null ? metres / 1000 : null);
  if (distanceKm !== null && distanceKm > 0) {
    facts.push(`${distanceKm.toFixed(1)} km`);
  }

  const climb = firstNumber(summary, [
    "elevation_gain_m",
    "elevation_gain_meters",
    "elevation_gain",
    "total_ascent",
  ]);
  if (climb !== null && climb > 0) {
    facts.push(`${Math.round(climb)} m up`);
  }

  return facts;
}

export function AttachButton({
  disabled,
  onFiles,
  className,
}: {
  disabled?: boolean;
  onFiles: (files: File[]) => void;
  className?: string;
}) {
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        multiple
        accept={ATTACHMENT_ACCEPT}
        className="hidden"
        onChange={(e) => {
          const picked = Array.from(e.target.files ?? []);
          // Clear the input before handing the files on, otherwise picking the
          // same file again after a removal fires no change event at all.
          e.target.value = "";
          if (picked.length) onFiles(picked);
        }}
      />
      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        disabled={disabled}
        title="Attach a ride file (GPX, FIT or TCX)"
        aria-label="Attach a ride file"
        className={cn(
          "flex h-[42px] w-[42px] shrink-0 items-center justify-center rounded-sm border border-vb-border bg-vb-surface text-vb-text-dim transition-colors hover:text-vb-text disabled:pointer-events-none disabled:opacity-40",
          className
        )}
      >
        <Paperclip className="h-4 w-4" />
      </button>
    </>
  );
}

export function AttachmentChips({
  attachments,
  onRemove,
  className,
}: {
  attachments: ComposerAttachment[];
  onRemove: (key: string) => void;
  className?: string;
}) {
  if (attachments.length === 0) return null;

  return (
    <div className={cn("mb-3 flex flex-wrap gap-2", className)}>
      {attachments.map((attachment) => {
        const facts = attachmentFacts(attachment.summary);
        const settled = attachment.status === "ready";
        return (
          <div
            key={attachment.key}
            className="flex max-w-full items-center gap-2 rounded-sm border border-vb-border bg-vb-sunken py-1.5 pl-2.5 pr-1.5"
          >
            {settled ? (
              <Paperclip className="h-3.5 w-3.5 shrink-0 text-vb-text-muted" />
            ) : (
              <CadenceSpinner
                size={14}
                className="text-vb-red"
                title="Reading the file"
              />
            )}
            <div className="min-w-0">
              <span className="block max-w-[220px] truncate text-xs text-vb-text">
                {attachment.filename}
              </span>
              {settled && facts.length > 0 ? (
                <span className="f-data block text-[11px] text-vb-text-muted">
                  {facts.join("  ·  ")}
                </span>
              ) : (
                <span className="f-kicker block text-[10px] text-vb-text-muted">
                  {settled ? "Ready" : "Reading"}
                </span>
              )}
            </div>
            <button
              type="button"
              onClick={() => onRemove(attachment.key)}
              aria-label={`Remove ${attachment.filename}`}
              className="shrink-0 rounded-sm p-1 text-vb-text-muted transition-colors hover:text-vb-red"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
