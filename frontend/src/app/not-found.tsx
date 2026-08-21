// A wrong URL got Next's default 404 before this. Riders hit it from stale
// links in old letters or a mistyped ride id; give them a way home.

import Link from "next/link";

export default function NotFound() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 px-6 text-center">
      <p className="font-mono text-[11px] font-semibold uppercase tracking-[0.16em] text-vb-text-muted">
        Off course
      </p>
      <h1 className="font-display text-3xl font-extrabold tracking-[-0.02em] text-vb-text">
        This road does not exist.
      </h1>
      <p className="max-w-sm text-sm text-vb-text-dim">
        The page you are after has moved or never was. The dashboard knows the
        way back.
      </p>
      <Link
        href="/dashboard"
        className="mt-2 border border-vb-text bg-vb-text px-5 py-2.5 font-mono text-xs font-semibold uppercase tracking-[0.1em] text-vb-bg hover:border-vb-red hover:bg-vb-red hover:text-white"
      >
        Back to the dashboard
      </Link>
    </div>
  );
}
