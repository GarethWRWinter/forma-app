import * as React from "react";
import { cn } from "@/lib/utils";
import { Kicker } from "@/components/ui/kicker";

/**
 * FORMA section header — kicker, bold Archivo title, ink rule that
 * draws in. `number` prefixes an editorial index ("01").
 * `action` renders right-aligned (a link or button).
 * `id` makes the section a place another page can link to.
 */
export function SectionHeader({
  kicker,
  title,
  number,
  action,
  id,
  className,
}: {
  kicker?: string;
  title: React.ReactNode;
  number?: string;
  action?: React.ReactNode;
  id?: string;
  className?: string;
}) {
  return (
    <div id={id} className={cn("mb-5", id && "scroll-mt-20", className)}>
      <hr className="f-rule f-draw mb-4" />
      <div className="flex items-end justify-between gap-4">
        <div>
          {kicker && (
            <Kicker className="mb-1.5">
              {number && <span className="text-vb-red">{number} ·</span>}
              {kicker}
            </Kicker>
          )}
          <h2 className="f-display text-2xl text-vb-text md:text-3xl">
            {title}
          </h2>
        </div>
        {action && <div className="shrink-0 pb-1">{action}</div>}
      </div>
    </div>
  );
}
