"use client";

/**
 * The cadence spinner: the flamme kite riding the crank circle.
 *
 * Two things make it Forma's rather than a generic spinner. The kite sits
 * tangential to the path, nose leading the direction of travel, the way a
 * rider banks round a velodrome. And it does not turn at a constant rate:
 * it quickens through the downstroke and eases over the dead spots at top
 * and bottom, which is what a real pedal stroke feels like.
 *
 * Brand note: the kite is otherwise reserved for the wordmark. This is a
 * deliberate exception, documented in the brand guide, on the grounds that
 * here it is a moving part rather than a mark.
 */
export function CadenceSpinner({
  size = 14,
  className = "",
  title = "Working",
}: {
  size?: number;
  className?: string;
  title?: string;
}) {
  return (
    <svg
      viewBox="0 0 40 40"
      width={size}
      height={size}
      className={className}
      role="img"
      aria-label={title}
      style={{ display: "block", flexShrink: 0 }}
    >
      {/* the crank circle the kite rides */}
      <circle
        cx="20"
        cy="20"
        r="14"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.2"
        opacity="0.22"
      />
      <g className="f-cadence">
        {/* nose to the right: tangential at twelve o'clock, so the kite
            leads the way round rather than pointing at the hub */}
        <polygon points="17,1.5 17,10.5 25,6" fill="currentColor" />
      </g>
    </svg>
  );
}
