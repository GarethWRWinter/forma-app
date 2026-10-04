"use client";

import * as React from "react";
import { TrendingUp, TrendingDown, Bot, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { coachInsights } from "@/lib/api";

interface StatCardProps extends React.HTMLAttributes<HTMLDivElement> {
  label: string;
  value: string | number;
  unit?: string;
  trend?: "up" | "down";
  /** Pass a metric name to enable tap-to-explain (e.g. "CTL", "TSS", "NP") */
  explainable?: string;
}

const StatCard = React.forwardRef<HTMLDivElement, StatCardProps>(
  ({ className, label, value, unit, trend, explainable, ...props }, ref) => {
    const [showExplain, setShowExplain] = React.useState(false);
    const [explanation, setExplanation] = React.useState<string | null>(null);
    const [loading, setLoading] = React.useState(false);

    const handleTap = async () => {
      if (!explainable || value === "-") return;
      if (showExplain) {
        setShowExplain(false);
        return;
      }
      setShowExplain(true);
      if (!explanation) {
        setLoading(true);
        try {
          const result = await coachInsights.explainMetric(explainable, value);
          setExplanation(result.explanation);
        } catch {
          setExplanation("Forma couldn't explain that just now. Tap the number again in a minute.");
        } finally {
          setLoading(false);
        }
      }
    };

    return (
      <div className="relative">
        <div
          ref={ref}
          className={cn(
            "rounded-md border border-vb-border-subtle bg-vb-surface p-4",
            explainable && value !== "-" && "cursor-pointer transition-colors hover:border-vb-forest/40",
            showExplain && "border-vb-forest/40",
            className
          )}
          onClick={handleTap}
          role={explainable ? "button" : undefined}
          tabIndex={explainable ? 0 : undefined}
          {...props}
        >
          <div className="flex items-center justify-between">
            <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-vb-text-muted">
              {label}
            </p>
            {explainable && value !== "-" && (
              <Bot className="h-3.5 w-3.5 text-vb-text-muted" />
            )}
          </div>
          <div className="mt-2 flex items-baseline gap-2">
            <span className="font-display text-2xl font-semibold tracking-[-0.02em] tabular-nums text-vb-text">
              {value}
            </span>
            {unit && <span className="text-sm text-vb-text-muted">{unit}</span>}
            {trend && (
              <span
                className={cn(
                  "ml-auto flex items-center text-sm font-medium",
                  trend === "up" ? "text-vb-forest" : "text-vb-clay"
                )}
              >
                {trend === "up" ? (
                  <TrendingUp className="h-4 w-4" />
                ) : (
                  <TrendingDown className="h-4 w-4" />
                )}
              </span>
            )}
          </div>
        </div>
        {showExplain && (
          <div className="absolute left-0 right-0 top-full z-20 mt-1 rounded-md border border-vb-border bg-vb-surface p-3">
            <div className="mb-1.5 flex items-center justify-between">
              <div className="flex items-center gap-1.5">
                <Bot className="h-3.5 w-3.5 text-vb-forest" />
                <span className="text-xs font-medium text-vb-forest">Coach Forma</span>
              </div>
              <button
                onClick={(e) => { e.stopPropagation(); setShowExplain(false); }}
                className="text-vb-text-muted hover:text-vb-text"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
            {loading ? (
              <div className="flex items-center gap-2 text-xs text-vb-text-dim">
                <div className="h-3 w-3 animate-spin rounded-full border border-vb-forest border-t-transparent" />
                Thinking…
              </div>
            ) : (
              <p className="text-xs leading-relaxed text-vb-text-dim">{explanation}</p>
            )}
          </div>
        )}
      </div>
    );
  }
);
StatCard.displayName = "StatCard";

export { StatCard };
