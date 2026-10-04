"use client";

import { cn } from "@/lib/utils";

interface VoiceIndicatorProps {
  /** Current voice mode state */
  mode: "idle" | "listening" | "processing" | "speaking";
  /** Real-time transcript while user is speaking */
  interimTranscript?: string;
  className?: string;
}

export function VoiceIndicator({
  mode,
  interimTranscript,
  className,
}: VoiceIndicatorProps) {
  if (mode === "idle") return null;

  return (
    <div
      className={cn(
        "mb-3 flex h-10 items-center gap-3 rounded-sm px-4 transition-all",
        mode === "listening" && "bg-vb-sage-tint border border-vb-border-subtle",
        mode === "processing" && "bg-vb-sage-tint border border-vb-border-subtle",
        mode === "speaking" && "bg-vb-sage-tint border border-vb-border-subtle",
        className
      )}
    >
      {/* Waveform / Status icon */}
      <div className="flex items-center gap-0.5">
        {mode === "listening" && (
          <>
            {[0, 1, 2, 3, 4].map((i) => (
              <div
                key={i}
                className="voice-bar w-1 rounded-full bg-vb-forest"
                style={{
                  animationDelay: `${i * 0.1}s`,
                  height: "4px",
                }}
              />
            ))}
          </>
        )}
        {mode === "processing" && (
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-vb-forest border-t-transparent" />
        )}
        {mode === "speaking" && (
          <>
            {[0, 1, 2, 3, 4].map((i) => (
              <div
                key={i}
                className="voice-bar w-1 rounded-full bg-vb-forest"
                style={{
                  animationDelay: `${i * 0.12}s`,
                  height: "4px",
                }}
              />
            ))}
          </>
        )}
      </div>

      {/* Status text */}
      <div className="min-w-0 flex-1">
        {mode === "listening" && (
          <p className="text-xs text-vb-text-dim">
            {interimTranscript ? (
              <span className="italic">{interimTranscript}</span>
            ) : (
              "Listening, go ahead…"
            )}
          </p>
        )}
        {mode === "processing" && (
          <p className="text-xs text-vb-text-dim">Thinking…</p>
        )}
        {mode === "speaking" && (
          <p className="text-xs text-vb-forest">Forma is speaking…</p>
        )}
      </div>
    </div>
  );
}
