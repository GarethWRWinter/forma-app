"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { palmares, type PalmaresGoal } from "@/lib/api";
import { cn, formatDate } from "@/lib/utils";
import { Kicker } from "@/components/ui/kicker";
import { SectionHeader } from "@/components/ui/section-header";
import { EmptyState } from "@/components/ui/empty-state";
import { buttonVariants } from "@/components/ui/button";
import { useAuth } from "@/lib/auth-context";
import { INKSCAPES } from "@/lib/dailyInkscape";
import { FoundingBadge } from "@/components/palmares/founding-badge";

/**
 * Palmarès — the trophy cabinet. The kite is the trophy: a pennant wall
 * like race pennants in a clubhouse. Attempts honoured: victories carry
 * flamme, hard days stay in honest ink. This page is why riders stay.
 */

const STATUS_LINE: Record<string, string> = {
  completed: "Conquered",
  dnf: "Did not finish. It still counts as a start.",
  dns: "Did not start. The training still happened.",
};

function fmtTime(secs: number): string {
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  return h > 0 ? `${h}h ${m}m` : `${m}:${String(s).padStart(2, "0")}`;
}

/** Render a 1080×1350 share card onto a canvas and download it. */
async function downloadShareCard(goal: PalmaresGoal, riderName: string) {
  const W = 1080;
  const H = 1350;
  const canvas = document.createElement("canvas");
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext("2d")!;

  // Inkscape ground — pick by goal id so each card keeps its own pass.
  let h = 0;
  for (const c of goal.id) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const ink = INKSCAPES[h % INKSCAPES.length];
  const img = new Image();
  img.src = ink.src;
  await new Promise((res, rej) => {
    img.onload = res;
    img.onerror = rej;
  });
  // cover-fit
  const scale = Math.max(W / img.width, H / img.height);
  const iw = img.width * scale;
  const ih = img.height * scale;
  ctx.drawImage(img, (W - iw) / 2, (H - ih) / 2, iw, ih);

  // scrim
  const grad = ctx.createLinearGradient(0, H, 0, 0);
  grad.addColorStop(0, "rgba(0,0,0,0.72)");
  grad.addColorStop(0.5, "rgba(0,0,0,0.28)");
  grad.addColorStop(1, "rgba(0,0,0,0.15)");
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, W, H);

  await (document as Document & { fonts: FontFaceSet }).fonts.ready;

  // FORMA lockup top-left (chalk word + flamme kite — IB2)
  ctx.fillStyle = "#FFFFFF";
  ctx.font = "800 52px Archivo, sans-serif";
  ctx.textBaseline = "top";
  ctx.fillText("FORMA", 72, 72);
  const fw = ctx.measureText("FORMA").width;
  ctx.fillStyle = "#FF3D00";
  ctx.beginPath();
  ctx.moveTo(72 + fw + 12, 100);
  ctx.lineTo(72 + fw + 34, 100);
  ctx.lineTo(72 + fw + 23, 122);
  ctx.closePath();
  ctx.fill();

  // Big flamme kite (the trophy)
  ctx.fillStyle = "#FF3D00";
  ctx.beginPath();
  ctx.moveTo(72, 880);
  ctx.lineTo(180, 880);
  ctx.lineTo(126, 980);
  ctx.closePath();
  ctx.fill();

  // Verdict
  ctx.fillStyle = "#FFFFFF";
  ctx.font = "500 30px 'IBM Plex Mono', monospace";
  ctx.fillText(
    (goal.achieved ? "CONQUERED · " : "RACED · ") +
      new Date(goal.date)
        .toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })
        .toUpperCase(),
    72,
    1024
  );

  // Goal name (wrap, no widows: keep last line ≥ 2 words)
  ctx.font = "800 84px Archivo, sans-serif";
  const words = goal.name.split(" ");
  const lines: string[] = [];
  let line = "";
  for (const w of words) {
    const test = line ? `${line} ${w}` : w;
    if (ctx.measureText(test).width > W - 144 && line) {
      lines.push(line);
      line = w;
    } else {
      line = test;
    }
  }
  if (line) lines.push(line);
  if (lines.length > 1 && lines[lines.length - 1].split(" ").length === 1) {
    const prev = lines[lines.length - 2].split(" ");
    lines[lines.length - 1] = `${prev.pop()} ${lines[lines.length - 1]}`;
    lines[lines.length - 2] = prev.join(" ");
  }
  lines.slice(0, 3).forEach((l, i) => {
    ctx.fillText(l, 72, 1076 + i * 92);
  });

  // Rider
  ctx.fillStyle = "rgba(255,255,255,0.7)";
  ctx.font = "500 26px 'IBM Plex Mono', monospace";
  ctx.fillText(`${riderName.toUpperCase()} · PALMARÈS`, 72, H - 80);

  const a = document.createElement("a");
  a.download = `forma-palmares-${goal.name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}.png`;
  a.href = canvas.toDataURL("image/png");
  a.click();
}

export default function PalmaresPage() {
  const { user } = useAuth();
  const { data } = useQuery({ queryKey: ["palmares"], queryFn: () => palmares.get() });

  const firstName = user?.full_name?.split(" ")[0] || "Rider";
  const goalsByYear = new Map<number, PalmaresGoal[]>();
  for (const g of data?.goals ?? []) {
    const y = g.year ?? 0;
    goalsByYear.set(y, [...(goalsByYear.get(y) ?? []), g]);
  }
  const years = [...goalsByYear.keys()].sort((a, b) => b - a);

  return (
    <div className="space-y-12 md:space-y-14">
      {/* Masthead */}
      <header className="f-rise">
        <Kicker>The record · everything earned</Kicker>
        <h1 className="f-display mt-3 text-5xl leading-[1.02] md:text-6xl">
          Palmarès.
        </h1>
        {data && data.totals.rides > 0 && (
          <p className="f-data mt-3 text-sm text-vb-text-dim">
            {data.totals.rides.toLocaleString()} rides ·{" "}
            {data.totals.km.toLocaleString()} km ·{" "}
            {data.totals.hours.toLocaleString()} hours in the bank
          </p>
        )}
      </header>

      {/* Coach-voiced milestones */}
      {data && data.milestones.length > 0 && (
        <section className="f-rise space-y-2">
          {data.milestones.map((m) => (
            <p
              key={m.key}
              className="border-l-[3px] border-l-vb-red bg-vb-surface px-5 py-3 text-sm text-vb-text-dim"
            >
              {m.text}
            </p>
          ))}
        </section>
      )}

      {/* Founding rider badge studio */}
      <FoundingBadge />

      {/* THE CABINET — raced goals as pennants */}
      <section className="f-rise">
        <SectionHeader kicker="The cabinet" title="Days that counted" />
        {years.length === 0 ? (
          <EmptyState
            title="The cabinet is waiting."
            action={
              <Link
                href="/dashboard/goals"
                className={cn(buttonVariants({ variant: "ghost", size: "sm" }))}
              >
                Set a goal <span className="f-arrow-head">→</span>
              </Link>
            }
          >
            Race a goal and it hangs here forever, hit or missed. The
            attempt is the entry fee; the training is never wasted.
          </EmptyState>
        ) : (
          years.map((year) => (
            <div key={year} className="mb-10">
              <p className="f-kicker mb-4 border-b border-vb-border pb-2 text-vb-text-muted">
                {year || "Undated"} season
              </p>
              <div className="f-stagger grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {goalsByYear.get(year)!.map((g) => (
                  <div
                    key={g.id}
                    className={cn(
                      "relative border bg-vb-surface p-5",
                      g.achieved
                        ? "border-vb-red/50"
                        : "border-vb-border-subtle"
                    )}
                  >
                    {/* the pennant */}
                    <span
                      className="absolute right-5 top-0 inline-block h-9 w-7"
                      style={{
                        clipPath: "polygon(0 0, 100% 0, 50% 100%)",
                        background: g.achieved ? "#FF3D00" : "#D8D8D2",
                      }}
                      aria-hidden
                    />
                    <p className="f-kicker text-vb-text-muted">
                      {formatDate(g.date)} · {g.priority.replace("_", "-")}
                    </p>
                    <p className="f-display mt-2 pr-8 text-xl leading-tight">
                      {g.name}
                    </p>
                    <p
                      className={cn(
                        "f-kicker mt-2",
                        g.achieved ? "text-vb-red" : "text-vb-text-dim"
                      )}
                    >
                      {STATUS_LINE[g.status] || "Raced"}
                    </p>
                    <button
                      onClick={() => downloadShareCard(g, firstName)}
                      className="f-kicker mt-4 text-vb-text-muted transition-colors hover:text-vb-red"
                    >
                      Share card ↓
                    </button>
                  </div>
                ))}
              </div>
            </div>
          ))
        )}
      </section>

      {/* RECORDS */}
      {data && data.records.length > 0 && (
        <section className="f-rise">
          <SectionHeader kicker="The numbers" title="Records" />
          <div className="f-stagger grid grid-cols-2 gap-3 md:grid-cols-3">
            {data.records.map((r) => (
              <div
                key={r.key}
                className="border border-vb-border-subtle bg-vb-surface p-4"
              >
                <p className="f-kicker text-vb-text-muted">{r.label}</p>
                <p className="f-data mt-2 text-3xl font-bold leading-none">
                  {r.value}
                  <span className="ml-1 text-sm font-medium text-vb-text-muted">
                    {r.unit}
                  </span>
                </p>
                {r.detail && (
                  <p className="mt-1.5 truncate text-xs text-vb-text-dim">
                    {r.detail}
                  </p>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {/* THE LOG — segment PRs */}
      {data && data.segment_prs.length > 0 && (
        <section className="f-rise">
          <SectionHeader kicker="The log" title="Segment bests" />
          <ul>
            {data.segment_prs.map((pr, i) => (
              <li
                key={`${pr.name}-${i}`}
                className={cn(
                  "flex items-baseline justify-between gap-4 py-3",
                  i > 0 && "border-t border-vb-border-subtle"
                )}
              >
                <div className="min-w-0">
                  <p className="truncate font-medium text-vb-text">{pr.name}</p>
                  <p className="f-kicker mt-0.5 text-vb-text-muted">
                    {pr.date}
                    {pr.distance_m ? ` · ${(pr.distance_m / 1000).toFixed(1)}km` : ""}
                  </p>
                </div>
                <span className="f-data shrink-0 text-lg font-bold">
                  {fmtTime(pr.time_seconds)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
