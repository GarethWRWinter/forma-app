"use client";

import type { ConsentLabel } from "@/components/safety/safety-rules";

/**
 * An unticked consent box whose label is built from the same strings the
 * server records, so what the rider read and what we stored can't drift.
 * The linked phrase opens in a new tab and doesn't tick the box.
 */
export function ConsentBox({
  label,
  checked,
  onChange,
  error,
  className,
}: {
  label: ConsentLabel;
  checked: boolean;
  onChange: (checked: boolean) => void;
  error?: string;
  className?: string;
}) {
  return (
    <div className={className}>
      <label className="flex items-start gap-3 text-sm leading-relaxed text-vb-text">
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          aria-invalid={!!error}
          className="mt-1 h-4 w-4 flex-none accent-[var(--color-vb-red)]"
        />
        <span>
          {label.before}
          <a
            href={label.href}
            target="_blank"
            rel="noreferrer"
            className="underline underline-offset-2 hover:text-vb-red"
          >
            {label.link}
          </a>
          {label.after}
        </span>
      </label>
      {error && (
        <p className="mt-2 border-l-2 border-vb-red pl-3 text-sm text-vb-text">{error}</p>
      )}
    </div>
  );
}
