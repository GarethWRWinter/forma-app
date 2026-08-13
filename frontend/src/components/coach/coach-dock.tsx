"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ArrowUp, X } from "lucide-react";
import { CoachDot } from "@/components/ui/coach-glyph";
import { StarterChips, useCoachStarters } from "@/components/coach/coach-starters";
import { coachInsights } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { cn } from "@/lib/utils";

/**
 * The coach dock — Forma's ambient presence.
 *
 * Every surface of the app keeps the coach within reach, not one nav item
 * away. Collapsed, the dock is a person waiting with something to say: the
 * dot, the name, and a glimpse of the question already on the table. Open,
 * it is a doorway, never the conversation itself. Whatever the rider types
 * is handed to /dashboard/coach as ?ask= so the words arrive prefilled and
 * the rider still presses send.
 */

/** Where the coach already owns the surface, the dock stands down. */
export function useCoachDockVisible(): boolean {
  const pathname = usePathname();
  // The coach page IS the conversation; the session player has Race Radio.
  return !(
    pathname.startsWith("/dashboard/coach") || pathname.includes("/session")
  );
}

/** One line of the nudge, cut on a word so it reads like a held thought. */
function previewOf(text: string, max = 40): string {
  const clean = text.replace(/\s+/g, " ").trim();
  if (clean.length <= max) return clean;
  const cut = clean.slice(0, max);
  const space = cut.lastIndexOf(" ");
  const body = space > max * 0.6 ? cut.slice(0, space) : cut;
  return `${body.replace(/[,.;:]+$/, "")}…`;
}

export function CoachDock() {
  const visible = useCoachDockVisible();
  const router = useRouter();
  const { user } = useAuth();
  const coach = user?.coach_name || "Forma";

  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const pillRef = useRef<HTMLButtonElement>(null);

  // Shares the dashboard's cache entry, so the dock costs no extra call.
  const { data: nudge } = useQuery({
    queryKey: ["coach-nudge"],
    queryFn: () => coachInsights.getNudge(),
    staleTime: 30 * 60 * 1000,
    retry: false,
    enabled: visible,
  });

  const line = nudge?.nudge?.trim() || "";

  const close = useCallback((returnFocus = true) => {
    setOpen(false);
    if (returnFocus) pillRef.current?.focus();
  }, []);

  // Hand the words over to the coach page, prefilled and unsent.
  const handOff = useCallback(
    (text: string) => {
      const ask = text.trim();
      if (!ask) return;
      setOpen(false);
      router.push(`/dashboard/coach?ask=${encodeURIComponent(ask)}`);
    },
    [router]
  );

  // Escape closes, and a click anywhere else lets the panel go quietly.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        close();
      }
    };
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [open, close]);

  // Route changes should not leave the panel hanging open behind the page.
  const pathname = usePathname();
  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  if (!visible) return null;

  return (
    <div
      ref={rootRef}
      className="fixed bottom-4 right-4 z-40 flex flex-col items-end"
    >
      {open && (
        <DockPanel
          coach={coach}
          nudge={line}
          onClose={() => close()}
          onSubmit={handOff}
        />
      )}

      <button
        ref={pillRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls="coach-dock-panel"
        aria-label={
          line
            ? `${coach} has something to say: ${line}`
            : `Talk to ${coach}, your coach`
        }
        className={cn(
          "f-lift flex max-w-[264px] items-center gap-3 border border-vb-border-strong bg-vb-surface-raised py-2.5 pl-3.5 pr-4 text-left shadow-[0_14px_34px_-22px_rgba(11,11,12,0.55)]",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-vb-red focus-visible:ring-offset-2 focus-visible:ring-offset-vb-bg"
        )}
      >
        <CoachDot state="still" size="9px" />
        <span className="min-w-0">
          <span className="block font-mono text-[11px] font-semibold uppercase tracking-[0.12em] text-vb-text">
            {coach}
          </span>
          <span className="mt-0.5 block truncate text-[12px] leading-snug text-vb-text-dim">
            {line ? previewOf(line) : "Here when you want to talk"}
          </span>
        </span>
      </button>
    </div>
  );
}

function DockPanel({
  coach,
  nudge,
  onClose,
  onSubmit,
}: {
  coach: string;
  nudge: string;
  onClose: () => void;
  onSubmit: (text: string) => void;
}) {
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  // Mounted only while open, so the goal lookup waits for real intent.
  const starters = useCoachStarters(nudge).slice(0, 3);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  return (
    <div
      id="coach-dock-panel"
      role="dialog"
      aria-label={`Talk to ${coach}`}
      className="f-rise mb-2 w-[min(360px,calc(100vw-2rem))] border border-vb-border-strong bg-vb-surface-raised shadow-[0_24px_60px_-30px_rgba(11,11,12,0.6)]"
    >
      <div className="flex items-center justify-between gap-3 border-b border-vb-border-subtle px-4 py-3">
        <span className="flex items-center gap-2.5">
          <CoachDot state="still" size="9px" />
          <span className="font-mono text-[11px] font-semibold uppercase tracking-[0.12em] text-vb-text">
            {coach}
          </span>
        </span>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close the coach dock"
          className="-mr-1 rounded-sm p-1 text-vb-text-muted transition-colors hover:text-vb-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-vb-red"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="space-y-3 px-4 py-3.5">
        {nudge && (
          <p className="text-[13px] leading-relaxed text-vb-text">{nudge}</p>
        )}

        <div className="flex items-center gap-2">
          <input
            ref={inputRef}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                onSubmit(value);
              }
            }}
            placeholder={`Tell ${coach}…`}
            aria-label={`Tell ${coach} something`}
            className="h-[38px] min-w-0 flex-1 rounded-sm border border-vb-border bg-vb-surface px-3 text-[13px] text-vb-text placeholder-vb-text-muted focus:border-vb-red focus:outline-none"
          />
          <button
            type="button"
            onClick={() => onSubmit(value)}
            disabled={!value.trim()}
            aria-label={`Take this to ${coach}`}
            className="flex h-[38px] w-[38px] shrink-0 items-center justify-center rounded-sm bg-vb-red text-white transition-colors hover:bg-vb-red-dim disabled:pointer-events-none disabled:opacity-30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-vb-red focus-visible:ring-offset-2 focus-visible:ring-offset-vb-bg"
          >
            <ArrowUp className="h-4 w-4" />
          </button>
        </div>

        <StarterChips
          starters={starters}
          onPick={onSubmit}
          className="flex flex-wrap gap-1.5"
        />
      </div>
    </div>
  );
}
