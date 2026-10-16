"use client";

import { useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import type { QueryClient } from "@tanstack/react-query";
import { Paperclip } from "lucide-react";
import {
  ApiError,
  chat,
  FILE_LABEL_MAX,
  type ChatAttachmentCard,
  type ChatAttachmentCardChoice,
  type FileChoice,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { Arrow, Button } from "@/components/ui/button";
import { CadenceSpinner } from "@/components/ui/cadence-spinner";

/**
 * The card under the message that shared a ride file.
 *
 * Forma has already checked the file against the rider's rides by the time
 * this shows, so the card leads with what it recognised and asks only when
 * the answer matters. Filing is a button press, never a "yes" read out of
 * the chat: saving changes the rider's training load, so it has to be a
 * deliberate act with its consequence written beside it.
 *
 * The server writes every line on the card, the choice labels included, so
 * this renders them as given. The three choices carry equal weight on
 * purpose: the card should not lean the rider towards filing a ride.
 */

const NBSP = "\u00a0";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** 3 May 2026, the way the server writes dates on the card. The card's
    date is the ride's UTC day, so it is read as one, not as local time. */
function cardDate(day?: string | null): string | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(day || "");
  if (!m) return null;
  const month = MONTHS[Number(m[2]) - 1];
  return month ? `${Number(m[3])}${NBSP}${month}${NBSP}${m[1]}` : null;
}

/** The last two words held together, so a short line never ends on one. */
function hold(text: string): string {
  const at = text.trimEnd().lastIndexOf(" ");
  return at > 0 ? `${text.slice(0, at)}${NBSP}${text.slice(at + 1)}` : text;
}

/** What the card says while the server files the choice. */
const WORKING: Record<FileChoice, string> = {
  save: `Adding it to your${NBSP}rides…`,
  chat_only: `Marking it as${NBSP}yours…`,
  someone_else: `Marking it as someone${NBSP}else’s…`,
};

/**
 * What went wrong, and what to do about it. A refused choice (409) and a
 * name that is too long (400) come back from the server already worded for
 * the rider; everything else gets a plain line and a way forward.
 */
function decisionFailure(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 404) {
      return hold("This file is no longer in the chat. Attach it again to file it.");
    }
    if (error.status === 422) {
      return hold(`That name didn’t go through. Keep it to ${FILE_LABEL_MAX} characters and try again.`);
    }
    if (error.status >= 400 && error.status < 500 && error.message) {
      return hold(error.message);
    }
    return hold("Forma couldn’t file that just now. Try again in a moment.");
  }
  return hold("That didn’t reach Forma. Check your connection and try again.");
}

/** Every query that reads the rider's rides or fitness. A save adds a ride
    and rebuilds fitness on the server, so all of them are behind it. */
const RIDE_DATA_KEYS = [
  "rides",
  "recent-rides",
  "fitness-summary",
  "fitness-summary-quick",
  "pmc",
  "weekly-load",
  "weekly-load-12",
  "power-profile",
  "zones",
  "ftp-history",
  "palmares",
  "workouts-week",
  "today-workouts",
];

export function refreshRideData(queryClient: QueryClient) {
  for (const key of RIDE_DATA_KEYS) {
    queryClient.invalidateQueries({ queryKey: [key] });
  }
}

const focusRing =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-vb-red focus-visible:ring-offset-2 focus-visible:ring-offset-vb-surface-raised";

/** One choice: what it is, and underneath, what it does. */
function ChoiceButton({
  choice,
  onPick,
}: {
  choice: ChatAttachmentCardChoice;
  onPick: (choice: ChatAttachmentCardChoice) => void;
}) {
  const id = useId();
  return (
    <button
      type="button"
      data-choice={choice.choice}
      onClick={() => onPick(choice)}
      aria-describedby={choice.detail ? `${id}-detail` : undefined}
      className={cn(
        "f-press block min-h-11 w-full rounded-sm border border-vb-border bg-vb-surface px-3 py-2.5 text-left transition-colors hover:border-vb-border-strong",
        focusRing
      )}
    >
      <span className="block text-sm font-medium leading-snug text-vb-text">
        {choice.label}
      </span>
      {/* Read as the button's description, not as part of its name */}
      {choice.detail && (
        <span
          id={`${id}-detail`}
          aria-hidden="true"
          className="mt-0.5 block text-pretty text-xs leading-relaxed text-vb-text-dim"
        >
          {choice.detail}
        </span>
      )}
    </button>
  );
}

/** A quiet text control: the ways to change a choice, and to step back.
    `mark` tags it so focus can find its way back to it. */
function QuietButton({
  children,
  onClick,
  mark,
}: {
  children: React.ReactNode;
  onClick: () => void;
  mark?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      data-choice={mark}
      className={cn(
        // A full 44 px tall to touch, though it reads as a quiet text link.
        "inline-flex min-h-11 items-center rounded-sm text-xs text-vb-text-muted underline decoration-vb-border underline-offset-4 transition-colors hover:text-vb-text hover:decoration-vb-text",
        focusRing
      )}
    >
      {children}
    </button>
  );
}

type Mode = "idle" | "changing" | "naming";

export function FileCard({
  card,
  onDecided,
  onStale,
  className,
}: {
  card: ChatAttachmentCard;
  /** the card as the server now has it, after the rider's choice */
  onDecided: (next: ChatAttachmentCard, before: ChatAttachmentCard) => void;
  /** a choice was refused, so this card may be behind the server */
  onStale?: () => void;
  className?: string;
}) {
  const id = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const [working, setWorking] = useState<FileChoice | null>(null);
  const [error, setError] = useState<string | null>(null);
  // idle shows the card as it stands; changing opens the quiet changes as
  // full choices; naming asks for the optional name of someone else's file,
  // and steps back to wherever it was opened from.
  const [mode, setMode] = useState<Mode>("idle");
  const [namedFrom, setNamedFrom] = useState<Mode>("idle");
  const [name, setName] = useState("");
  // The control focus goes to once the card has redrawn for a new step.
  const focusNext = useRef<string | null>(null);

  useEffect(() => {
    const target = focusNext.current;
    if (!target) return;
    focusNext.current = null;
    const root = rootRef.current;
    const el =
      target === "card" ? root : root?.querySelector<HTMLElement>(target);
    (el ?? root)?.focus();
  });

  const decide = async (choice: FileChoice, label?: string) => {
    if (working) return;
    // The controls give way to the working line while the server files it,
    // so focus waits on the card rather than dropping to the page.
    focusNext.current = "card";
    setWorking(choice);
    setError(null);
    try {
      const next = await chat.decideAttachment(card.id, choice, label?.trim() || null);
      setMode("idle");
      setName("");
      // Focus stays on the card, and the live line reads out what became of
      // the file.
      focusNext.current = "card";
      onDecided(next, card);
    } catch (err) {
      setError(decisionFailure(err));
      if (err instanceof ApiError && err.status === 409) {
        // Refused because the file has moved on (chosen elsewhere, or by
        // the coach): show the card as it now stands, with the reason.
        setMode("idle");
        focusNext.current = "card";
        onStale?.();
      } else {
        // Nothing changed, so the rider can try the same thing again.
        focusNext.current =
          mode === "naming"
            ? `#${CSS.escape(`${id}-label`)}`
            : `[data-choice="${choice}"]`;
      }
    } finally {
      setWorking(null);
    }
  };

  const pick = (choice: ChatAttachmentCardChoice) => {
    setError(null);
    if (choice.asks_label) {
      setNamedFrom(mode);
      focusNext.current = `#${CSS.escape(`${id}-label`)}`;
      setMode("naming");
      return;
    }
    void decide(choice.choice);
  };

  const step = (to: Mode, focus: string) => {
    focusNext.current = focus;
    setError(null);
    setMode(to);
  };

  const backFromNaming = () => step(namedFrom, '[data-choice="someone_else"]');

  const facts = [
    cardDate(card.date),
    card.duration,
    typeof card.distance_km === "number" && card.distance_km > 0
      ? `${card.distance_km.toFixed(1)} km`
      : null,
  ].filter(Boolean);

  // One quiet change with nothing to explain is offered by name ("Not my
  // ride", "It's mine"). Several, or one whose consequence needs saying,
  // open behind a single "Change it" so the card stays quiet once answered.
  const changes = card.changes;
  const direct = changes.length === 1 && !changes[0].detail ? changes[0] : null;
  const idle = mode === "idle" && !working;

  return (
    <div
      ref={rootRef}
      role="group"
      tabIndex={-1}
      aria-labelledby={`${id}-name`}
      className={cn(
        "w-full rounded-sm border border-vb-border bg-vb-surface-raised text-left outline-none",
        className
      )}
    >
      <header className="flex items-start gap-2 border-b border-vb-border-subtle px-3 py-2.5">
        <Paperclip aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0 text-vb-text-muted" />
        <div className="min-w-0 flex-1">
          <p
            id={`${id}-name`}
            title={card.filename}
            className="truncate text-xs font-medium text-vb-text"
          >
            {card.filename}
          </p>
          {facts.length > 0 && (
            <p className="f-data mt-0.5 text-[11px] text-vb-text-muted">
              {facts.join(" · ")}
            </p>
          )}
        </div>
      </header>

      <div className="space-y-3 px-3 py-3">
        {/* What Forma recognised, or after a choice what became of it.
            Polite, so a choice is read out once it has landed. */}
        <div aria-live="polite" className="space-y-1">
          <p className="break-words text-pretty text-sm leading-snug text-vb-text">
            {card.outcome || card.recognised}
          </p>
          {card.note && (
            <p className="text-pretty text-xs leading-relaxed text-vb-text-dim">{card.note}</p>
          )}
          {card.undo && (
            <p className="text-pretty text-xs leading-relaxed text-vb-text-muted">{card.undo}</p>
          )}
        </div>

        {card.ride && (
          <Link
            href={`/dashboard/rides/${card.ride.id}`}
            className={cn(
              "f-arrow inline-flex min-h-11 items-center gap-1.5 rounded-sm font-mono text-[11px] font-semibold uppercase tracking-[0.08em] text-vb-text transition-colors hover:text-vb-red",
              focusRing
            )}
          >
            Open in Rides <Arrow />
          </Link>
        )}

        {/* The filing question, when there is one to ask */}
        {idle && card.choices.length > 0 && (
          <div className="space-y-2">
            {card.question && (
              <p id={`${id}-question`} className="text-sm font-medium text-vb-text">
                {card.question}
              </p>
            )}
            <div
              role="group"
              aria-labelledby={card.question ? `${id}-question` : `${id}-name`}
              className="space-y-2"
            >
              {card.choices.map((choice) => (
                <ChoiceButton key={choice.choice} choice={choice} onPick={pick} />
              ))}
            </div>
          </div>
        )}

        {/* Changing a choice already made, laid out as full choices */}
        {mode === "changing" && !working && (
          <div className="space-y-2">
            <div role="group" aria-labelledby={`${id}-name`} className="space-y-2">
              {changes.map((choice) => (
                <ChoiceButton key={choice.choice} choice={choice} onPick={pick} />
              ))}
            </div>
            <QuietButton onClick={() => step("idle", '[data-choice="change"]')}>
              Keep it as it&nbsp;is
            </QuietButton>
          </div>
        )}

        {/* Someone else's, with the name the coach will use for it */}
        {mode === "naming" && (
          <form
            className="space-y-2"
            onSubmit={(e) => {
              e.preventDefault();
              void decide("someone_else", name);
            }}
            onKeyDown={(e) => {
              if (e.key === "Escape" && !working) backFromNaming();
            }}
          >
            <label htmlFor={`${id}-label`} className="block text-sm font-medium text-vb-text">
              Who rode&nbsp;it?
            </label>
            <input
              id={`${id}-label`}
              value={name}
              maxLength={FILE_LABEL_MAX}
              onChange={(e) => setName(e.target.value)}
              disabled={!!working}
              placeholder="For example, Tom Pidcock’s KOM"
              aria-describedby={`${id}-hint`}
              autoComplete="off"
              className="h-11 w-full rounded-sm border border-vb-border bg-vb-surface px-3 text-sm text-vb-text placeholder:text-vb-text-muted focus-visible:border-vb-red focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-vb-red disabled:opacity-50"
            />
            <p id={`${id}-hint`} className="flex items-start justify-between gap-3 text-[11px] leading-relaxed text-vb-text-muted">
              <span>Optional. Their ride stays in this chat, for comparison&nbsp;only.</span>
              <span aria-hidden="true" className="f-data shrink-0">
                {name.length}/{FILE_LABEL_MAX}
              </span>
            </p>
            {!working && (
              <div className="flex flex-wrap items-center gap-x-4 gap-y-2 pt-1">
                <Button type="submit" size="sm">
                  Mark as someone else’s
                </Button>
                <QuietButton onClick={backFromNaming}>Back</QuietButton>
              </div>
            )}
          </form>
        )}

        {/* The quiet way to change it, once the card is answered */}
        {idle && changes.length > 0 && (
          <div className="flex flex-wrap gap-x-4">
            {direct ? (
              <QuietButton mark={direct.choice} onClick={() => pick(direct)}>
                {direct.label}
              </QuietButton>
            ) : (
              <QuietButton mark="change" onClick={() => step("changing", "[data-choice]")}>
                {/* Nothing to change before a choice: a file in Rides, or
                    part of a ride, only has other options. */}
                {card.decided ? <>Change&nbsp;it</> : <>Other&nbsp;options</>}
              </QuietButton>
            )}
          </div>
        )}

        {error && (
          <p role="alert" className="border-l-2 border-vb-red pl-3 text-xs leading-relaxed text-vb-text-dim">
            {error}
          </p>
        )}
      </div>

      {/* Saving runs the ride through the whole fitness rebuild, so it can
          take a few seconds: say so while it does. Always in the page, and
          outside the spaced stack above, so it is announced when it fills
          and takes no room while empty. */}
      <div role="status">
        {working && (
          <p className="flex items-center gap-2 px-3 pb-3 text-xs text-vb-text-dim">
            <span aria-hidden="true" className="flex">
              <CadenceSpinner size={16} className="text-vb-red" />
            </span>
            {WORKING[working]}
          </p>
        )}
      </div>
    </div>
  );
}
