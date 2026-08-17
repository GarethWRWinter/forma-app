"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { memory, type MemoryEntity } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { cn } from "@/lib/utils";
import { BRAIN } from "@/lib/palette";

/**
 * Your Brain — the memory graph rendered as a living organ (Pillar 2 flagship).
 *
 * Force-directed canvas in the ALMANAC palette: continuous motion (breathing
 * nodes, swaying threads), life-area clustering, type filters, hover threads,
 * a time scrubber that replays the graph's growth, and a click-through detail
 * card with the memory's story + hide-not-delete.
 */

// The reference space the layout was tuned in. The sim itself runs in CSS
// pixels; every tuned distance rides on a scale derived from the measured box.
const W = 1240;
const H = 1320;

const TYPE_STYLE: Record<string, { c: string; r: number; label: string }> = {
  user: { c: BRAIN.user.c, r: 15, label: "You" },
  value: { c: BRAIN.value.c, r: 8.5, label: "Value" },
  goal: { c: BRAIN.goal.c, r: 9.5, label: "Goal" },
  gap: { c: BRAIN.gap.c, r: 7.5, label: "Gap" },
  insight: { c: BRAIN.insight.c, r: 6.5, label: "Insight" },
  habit: { c: BRAIN.habit.c, r: 6.5, label: "Habit" },
  person: { c: BRAIN.person.c, r: 7, label: "Person" },
  life_event: { c: BRAIN.life_event.c, r: 5.5, label: "Life" },
  ride_memory: { c: BRAIN.ride_memory.c, r: 6.5, label: "Ride" },
  health_signal: { c: BRAIN.health_signal.c, r: 6.5, label: "Health" },
  procedural: { c: BRAIN.procedural.c, r: 6, label: "Coaching rule" },
};

// Cluster anchors as shares of the measured canvas, so the quadrants hold
// their shape on any box.
const AREAS: Record<string, { label: string; ax: number; ay: number }> = {
  training: { label: "TRAINING & PERFORMANCE", ax: 0.27, ay: 0.3 },
  body: { label: "BODY & HEALTH", ax: 0.73, ay: 0.3 },
  mind: { label: "MIND", ax: 0.27, ay: 0.73 },
  life: { label: "LIFE & PEOPLE", ax: 0.73, ay: 0.73 },
};

const FILTERS: { key: string; label: string; color?: string }[] = [
  { key: "all", label: "All" },
  { key: "value", label: "Values", color: BRAIN.value.c },
  { key: "goal", label: "Goals", color: BRAIN.goal.c },
  { key: "habit", label: "Habits", color: BRAIN.habit.c },
  { key: "insight", label: "Insights", color: BRAIN.insight.c },
  { key: "gap", label: "Gaps", color: BRAIN.gap.c },
  { key: "person", label: "People", color: BRAIN.person.c },
  { key: "life_event", label: "Life", color: BRAIN.life_event.c },
];

const EDGE_WORDS: Record<string, string> = {
  grounds: "grounds",
  serves: "serves",
  surfaces: "surfaced",
  addressed_by: "addressed by",
  became: "became",
  involves: "involves",
  constrains: "shapes",
  about: "relates to",
};

interface SimNode {
  id: string;
  type: string;
  area: string;
  label: string;
  birth: number; // days since first memory
  x: number;
  y: number;
  vx: number;
  vy: number;
  ph: number;
}

export default function BrainPage() {
  const { user } = useAuth();
  const coach = user?.coach_name || "Forma";
  const queryClient = useQueryClient();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const scrubRef = useRef<HTMLInputElement>(null);
  const tlabelRef = useRef<HTMLSpanElement>(null);

  const { data: graph } = useQuery({
    queryKey: ["memory-graph"],
    queryFn: () => memory.getGraph(),
    staleTime: 60 * 1000,
  });

  const { data: reading } = useQuery({
    queryKey: ["memory-reading"],
    queryFn: () => memory.getReading(),
    staleTime: 30 * 60 * 1000,
    retry: false,
    enabled: !!graph && graph.entities.length > 0,
  });

  const hideMutation = useMutation({
    mutationFn: (id: string) => memory.hide(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memory-graph"] });
      setSelectedId(null);
    },
  });

  const [filter, setFilter] = useState("all");
  const [mode, setMode] = useState<"organic" | "areas">("areas");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Refs mirrored for the animation loop (no re-renders at 60fps).
  const filterRef = useRef(filter);
  const modeRef = useRef(mode);
  const hoverRef = useRef<number>(-1);
  const tDayRef = useRef<number>(9999);
  const playingRef = useRef(false);
  const nodesRef = useRef<SimNode[]>([]);
  const linksRef = useRef<[number, number][]>([]);
  const spanRef = useRef(1);
  // Measured box in CSS pixels, plus the scale the tuned distances ride on.
  const sizeRef = useRef({ w: 900, h: 640, sc: Math.min(900 / W, 640 / H) });

  useEffect(() => {
    filterRef.current = filter;
  }, [filter]);
  useEffect(() => {
    modeRef.current = mode;
  }, [mode]);

  // Days since epoch for a date string.
  const dayOf = (iso: string | null) =>
    iso ? Math.floor(new Date(iso).getTime() / 86400000) : 0;

  // Size the buffer from the measured box. A fixed buffer stretched to a
  // different display aspect drew every node as an egg on a phone.
  useEffect(() => {
    const cv = canvasRef.current;
    if (!cv) return;
    const fit = () => {
      const w = cv.clientWidth;
      const h = cv.clientHeight;
      if (!w || !h) return;
      const dpr = window.devicePixelRatio || 1;
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
      // Resizing the buffer wipes the transform, so re-apply it: the device
      // ratio lives here and everything else draws in CSS pixels.
      cv.getContext("2d")?.setTransform(dpr, 0, 0, dpr, 0, 0);
      const was = sizeRef.current;
      sizeRef.current = { w, h, sc: Math.min(w / W, h / H) };
      // Carry positions across the resize so nothing piles into a corner.
      nodesRef.current.forEach((n) => {
        n.x *= w / was.w;
        n.y *= h / was.h;
      });
    };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(cv);
    return () => ro.disconnect();
    // Keyed on graph like the sim below: the canvas only mounts once there is
    // something to draw, so the observer has to attach then.
  }, [graph]);

  // Rebuild sim nodes when data arrives (preserve positions where possible).
  useEffect(() => {
    if (!graph) return;
    const prev = new Map(nodesRef.current.map((n) => [n.id, n]));
    const bornOn = (e: MemoryEntity) => dayOf(e.observed_at ?? e.created_at);
    const d0 = graph.entities.length
      ? Math.min(...graph.entities.map(bornOn))
      : 0;
    const span = Math.max(
      1,
      (graph.entities.length ? Math.max(...graph.entities.map(bornOn)) : 0) - d0
    );
    spanRef.current = span;
    tDayRef.current = span;
    if (scrubRef.current) {
      scrubRef.current.max = String(span);
      scrubRef.current.value = String(span);
    }
    if (tlabelRef.current) tlabelRef.current.textContent = "Today";

    const { w: cw, h: ch, sc } = sizeRef.current;
    const nodes: SimNode[] = [
      prev.get("__you__") ?? {
        id: "__you__",
        type: "user",
        area: "",
        label: "You",
        birth: 0,
        x: cw / 2,
        y: ch / 2,
        vx: 0,
        vy: 0,
        ph: 0,
      },
    ];
    graph.entities.forEach((e) => {
      const old = prev.get(e.id);
      nodes.push(
        old
          ? { ...old, label: e.label, type: e.type, area: e.life_area }
          : {
              id: e.id,
              type: e.type,
              area: e.life_area,
              label: e.label,
              birth: bornOn(e) - d0,
              x: cw / 2 + (Math.random() - 0.5) * 320 * sc,
              y: ch / 2 + (Math.random() - 0.5) * 320 * sc,
              vx: 0,
              vy: 0,
              ph: Math.random() * 6.28,
            }
      );
    });
    const idx = new Map(nodes.map((n, i) => [n.id, i]));
    const links: [number, number][] = [];
    graph.edges.forEach((g) => {
      const a = idx.get(g.from_id);
      const b = idx.get(g.to_id);
      if (a != null && b != null) links.push([a, b]);
    });
    // Thread loose entities to YOU so nothing floats unanchored.
    const linked = new Set(links.flat());
    nodes.forEach((n, i) => {
      if (i > 0 && !linked.has(i)) links.push([0, i]);
    });
    nodesRef.current = nodes;
    linksRef.current = links;
  }, [graph]);

  // The living simulation.
  useEffect(() => {
    const cv = canvasRef.current;
    if (!cv) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    let raf = 0;
    let last = performance.now();

    const alive = (n: SimNode) => n.birth <= tDayRef.current;
    const visible = (n: SimNode) =>
      alive(n) &&
      (filterRef.current === "all" ||
        n.type === "user" ||
        n.type === filterRef.current);

    const neighbors = (i: number) => {
      const s = new Set([i]);
      linksRef.current.forEach(([a, b]) => {
        if (a === i) s.add(b);
        if (b === i) s.add(a);
      });
      return s;
    };

    function tick(time: number) {
      const N = nodesRef.current;
      const L = linksRef.current;
      const { w: cw, h: ch, sc } = sizeRef.current;
      const sc2 = sc * sc;
      for (let i = 0; i < N.length; i++) {
        const n = N[i];
        if (!alive(n)) continue;
        n.vx *= 0.6;
        n.vy *= 0.6;
        let gx = cw / 2;
        let gy = ch / 2;
        if (modeRef.current === "areas" && n.type !== "user" && AREAS[n.area]) {
          gx = AREAS[n.area].ax * cw;
          gy = AREAS[n.area].ay * ch;
        }
        n.vx += (gx - n.x) * 0.012;
        n.vy += (gy - n.y) * 0.012;
        n.vx += Math.sin(time * 0.0006 + n.ph) * 0.05 * sc;
        n.vy += Math.cos(time * 0.0005 + n.ph * 1.3) * 0.05 * sc;
        for (let j = i + 1; j < N.length; j++) {
          const m = N[j];
          if (!alive(m)) continue;
          const dx = n.x - m.x;
          const dy = n.y - m.y;
          const d2 = dx * dx + dy * dy + 60 * sc2;
          if (d2 < 36000 * sc2) {
            const f = (440 * sc2) / d2;
            n.vx += dx * f;
            n.vy += dy * f;
            m.vx -= dx * f;
            m.vy -= dy * f;
          }
        }
      }
      L.forEach(([a, b]) => {
        const n = N[a];
        const m = N[b];
        if (!alive(n) || !alive(m)) return;
        const dx = m.x - n.x;
        const dy = m.y - n.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 1;
        const want = (n.type === "user" || m.type === "user" ? 170 : 90) * sc;
        const f = (d - want) * 0.012;
        const ux = (dx / d) * f;
        const uy = (dy / d) * f;
        n.vx += ux;
        n.vy += uy;
        m.vx -= ux;
        m.vy -= uy;
      });
      N.forEach((n) => {
        if (!alive(n)) return;
        // Margins are shares of the box: they exist to keep nodes off the
        // chips and the scrubber, which sit at fixed insets.
        n.x = Math.max(cw * 0.045, Math.min(cw * 0.955, n.x + n.vx));
        n.y = Math.max(ch * 0.053, Math.min(ch * 0.932, n.y + n.vy));
      });
    }

    function draw(time: number) {
      const N = nodesRef.current;
      const L = linksRef.current;
      const { w: cw, h: ch, sc } = sizeRef.current;
      ctx!.clearRect(0, 0, cw, ch);
      const hv = hoverRef.current;
      const hoverSet = hv >= 0 && N[hv] && alive(N[hv]) ? neighbors(hv) : null;

      if (modeRef.current === "areas") {
        ctx!.font = "600 11px Schibsted Grotesk, sans-serif";
        ctx!.textAlign = "center";
        ctx!.fillStyle = "rgba(148,141,128,.45)";
        for (const k in AREAS) {
          const a = AREAS[k];
          // Headings ride with their cluster but never run off a narrow box.
          const half = ctx!.measureText(a.label).width / 2 + 6;
          ctx!.fillText(
            a.label,
            Math.max(half, Math.min(cw - half, a.ax * cw)),
            a.ay * ch + (k === "training" || k === "body" ? -230 : 250) * sc
          );
        }
      }

      L.forEach(([a, b], idx2) => {
        const n = N[a];
        const m = N[b];
        if (!alive(n) || !alive(m)) return;
        const inHover = hoverSet && hoverSet.has(a) && hoverSet.has(b);
        const dim =
          (hoverSet && !inHover) ||
          (filterRef.current !== "all" && !(visible(n) && visible(m)));
        const midx = (n.x + m.x) / 2;
        const midy = (n.y + m.y) / 2;
        const dx = m.x - n.x;
        const dy = m.y - n.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 1;
        const bow =
          (idx2 % 2 ? 1 : -1) *
          Math.min(24 * sc, d * 0.14) *
          (1 + 0.1 * Math.sin(time * 0.001 + idx2));
        const cxp = midx - (dy / d) * bow;
        const cyp = midy + (dx / d) * bow;
        ctx!.beginPath();
        ctx!.moveTo(n.x, n.y);
        ctx!.quadraticCurveTo(cxp, cyp, m.x, m.y);
        ctx!.strokeStyle = inHover
          ? "rgba(54,81,63,.75)"
          : dim
            ? "rgba(150,140,120,.07)"
            : "rgba(150,140,120,.26)";
        ctx!.lineWidth = inHover ? 1.8 : 1;
        ctx!.stroke();
      });

      N.forEach((n, i) => {
        if (!alive(n)) return;
        const s = TYPE_STYLE[n.type] ?? TYPE_STYLE.life_event;
        const breathe = 1 + 0.07 * Math.sin(time * 0.0012 + n.ph);
        const r = s.r * breathe;
        const dim = (hoverSet && !hoverSet.has(i)) || !visible(n);
        ctx!.globalAlpha = dim ? 0.13 : 1;
        ctx!.beginPath();
        ctx!.arc(n.x, n.y, r, 0, 7);
        ctx!.fillStyle = s.c;
        ctx!.fill();
        if (n.type === "user") {
          ctx!.lineWidth = 3;
          ctx!.strokeStyle = "#FAFAF7";
          ctx!.stroke();
          ctx!.fillStyle = "#fff";
          ctx!.font = "600 10px Schibsted Grotesk, sans-serif";
          ctx!.textAlign = "center";
          ctx!.textBaseline = "middle";
          ctx!.fillText("YOU", n.x, n.y);
        }
        ctx!.globalAlpha = 1;
      });

      if (hv >= 0 && N[hv] && alive(N[hv]) && N[hv].label && N[hv].type !== "user") {
        const n = N[hv];
        ctx!.font = "500 13px Schibsted Grotesk, sans-serif";
        const w = ctx!.measureText(n.label).width + 22;
        const lx = Math.min(cw - 8 - w, Math.max(8, n.x - w / 2));
        const ly = n.y - 34;
        ctx!.fillStyle = "#0B0B0C";
        ctx!.beginPath();
        ctx!.roundRect(lx, ly - 13, w, 26, 13);
        ctx!.fill();
        ctx!.fillStyle = "#fff";
        ctx!.textAlign = "left";
        ctx!.textBaseline = "middle";
        ctx!.fillText(n.label, lx + 11, ly + 1);
      }
    }

    function loop(now: number) {
      const dt = now - last;
      last = now;
      if (playingRef.current) {
        tDayRef.current = Math.min(
          spanRef.current,
          tDayRef.current + (dt * spanRef.current) / 8000
        );
        if (scrubRef.current) scrubRef.current.value = String(tDayRef.current);
        if (tlabelRef.current)
          tlabelRef.current.textContent =
            tDayRef.current >= spanRef.current
              ? "Today"
              : `Day ${Math.floor(tDayRef.current)}`;
        if (tDayRef.current >= spanRef.current) playingRef.current = false;
      }
      tick(now);
      draw(now);
      raf = requestAnimationFrame(loop);
    }
    // Warm-up so the first frame is already organic.
    for (let i = 0; i < 200; i++) tick(i * 16);
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [graph]);

  // Pointer interactions.
  useEffect(() => {
    const cv = canvasRef.current;
    if (!cv) return;
    // Nodes live in CSS pixels now, so pointer offsets map straight across.
    const toWorld = (e: MouseEvent) => {
      const r = cv.getBoundingClientRect();
      return [e.clientX - r.left, e.clientY - r.top];
    };
    const move = (e: MouseEvent) => {
      const [x, y] = toWorld(e);
      let best = -1;
      let bd = 26 * 26;
      nodesRef.current.forEach((n, i) => {
        if (n.birth > tDayRef.current) return;
        const dx = n.x - x;
        const dy = n.y - y;
        const d = dx * dx + dy * dy;
        if (d < bd) {
          bd = d;
          best = i;
        }
      });
      hoverRef.current = best;
    };
    const leave = () => {
      hoverRef.current = -1;
    };
    const click = () => {
      const hv = hoverRef.current;
      const n = hv >= 0 ? nodesRef.current[hv] : null;
      setSelectedId(n && n.type !== "user" ? n.id : null);
    };
    cv.addEventListener("mousemove", move);
    cv.addEventListener("mouseleave", leave);
    cv.addEventListener("click", click);
    return () => {
      cv.removeEventListener("mousemove", move);
      cv.removeEventListener("mouseleave", leave);
      cv.removeEventListener("click", click);
    };
  }, []);

  const selected: MemoryEntity | null = useMemo(
    () => graph?.entities.find((e) => e.id === selectedId) ?? null,
    [graph, selectedId]
  );

  const selectedEdges = useMemo(() => {
    if (!graph || !selected) return [];
    const byId = new Map(graph.entities.map((e) => [e.id, e]));
    return graph.edges
      .filter((g) => g.from_id === selected.id || g.to_id === selected.id)
      .map((g) => {
        const otherId = g.from_id === selected.id ? g.to_id : g.from_id;
        const other = byId.get(otherId);
        return other
          ? {
              id: g.id,
              word: EDGE_WORDS[g.edge_type] ?? g.edge_type,
              other,
              outgoing: g.from_id === selected.id,
            }
          : null;
      })
      .filter(Boolean) as {
      id: string;
      word: string;
      other: MemoryEntity;
      outgoing: boolean;
    }[];
  }, [graph, selected]);

  const empty = graph && graph.entities.length === 0;

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-vb-text-muted">
            What {coach} knows about you
          </p>
          <h1 className="mt-2 font-display text-4xl font-extrabold tracking-[-0.02em]">
            Your brain.
          </h1>
          <p className="mt-2 max-w-xl text-sm text-vb-text-dim">
            Every conversation, ride, goal and life moment becomes one connected,
            living memory. Hover to follow a thread. Scrub to replay its growth.
          </p>
        </div>
        {graph && !empty && (
          <div className="text-right text-[13px] text-vb-text-muted">
            <span className="font-medium text-vb-text">
              {graph.entities.length}
            </span>{" "}
            memories ·{" "}
            <span className="font-medium text-vb-text">{graph.edges.length}</span>{" "}
            connections
          </div>
        )}
      </div>

      {empty ? (
        <div className="rounded-md border border-vb-border-subtle bg-vb-surface px-8 py-16 text-center">
          <svg
            className="mx-auto mb-5 block text-vb-forest/50"
            width="180"
            height="16"
            viewBox="0 0 180 16"
            fill="none"
          >
            <path
              d="M0,11 C17,11 23,5 36,6 C51,7 56,12 72,11 C87,10 92,3 108,5 C123,7 130,11 146,10 C160,9 168,9 180,7"
              stroke="currentColor"
              strokeWidth="1.4"
              strokeLinecap="round"
            />
            <circle cx="180" cy="7" r="2.4" fill="#FF3D00" />
          </svg>
          <p className="font-display text-xl font-semibold">
            Your brain starts with your first conversation.
          </p>
          <p className="mx-auto mt-2 max-w-sm text-sm text-vb-text-dim">
            Talk to {coach} about your goals, your week, your life, every durable
            thing you share becomes part of a memory that grows with you.
          </p>
          <Link
            href="/dashboard/coach"
            className="mt-6 inline-block rounded-sm bg-vb-forest px-5 py-2.5 text-sm font-medium text-white hover:bg-vb-forest-soft"
          >
            Talk to {coach}
          </Link>
        </div>
      ) : (
        <div className="grid gap-5 lg:grid-cols-[1fr_320px]">
          <div className="relative overflow-hidden rounded-md border border-vb-border-subtle bg-vb-surface">
            {/* Below md the controls sit in flow: absolutely positioned they
                overlapped, and the last chip sat under the toggle, untappable. */}
            <div className="flex flex-wrap gap-1.5 px-3 pt-3 md:absolute md:left-3 md:top-3 md:z-10 md:max-w-[78%] md:px-0 md:pt-0">
              {FILTERS.map((f) => (
                <button
                  key={f.key}
                  onClick={() => setFilter(f.key)}
                  className={cn(
                    "flex items-center gap-1.5 rounded-full border px-3 py-1 text-[11.5px] font-medium transition-colors pointer-coarse:min-h-11",
                    filter === f.key
                      ? "border-vb-text bg-vb-text text-white"
                      : "border-vb-border-subtle bg-vb-bg text-vb-text-dim hover:text-vb-text"
                  )}
                >
                  {f.color && (
                    <span
                      className="h-2 w-2 rounded-full"
                      style={{ backgroundColor: f.color }}
                    />
                  )}
                  {f.label}
                </button>
              ))}
            </div>
            <div className="ml-auto mr-3 mt-2 flex w-fit overflow-hidden rounded-md border border-vb-border-subtle md:absolute md:right-3 md:top-3 md:z-10 md:ml-0 md:mr-0 md:mt-0">
              {(["organic", "areas"] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className={cn(
                    "px-3 py-1.5 text-[11.5px] font-medium",
                    mode === m
                      ? "bg-vb-forest text-white"
                      : "bg-vb-bg text-vb-text-dim hover:text-vb-text"
                  )}
                >
                  {m === "organic" ? "Organic" : "Life areas"}
                </button>
              ))}
            </div>

            <canvas
              ref={canvasRef}
              className="mt-3 block h-[72vh] min-h-[560px] w-full cursor-crosshair md:mt-0"
            />

            <div className="absolute bottom-3 left-3 right-3 z-10 flex items-center gap-3 rounded-sm border border-vb-border-subtle bg-vb-bg/95 px-3.5 py-2">
              <button
                onClick={() => {
                  playingRef.current = !playingRef.current;
                  if (playingRef.current && tDayRef.current >= spanRef.current)
                    tDayRef.current = 0;
                }}
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-vb-forest text-[10px] text-white"
                title="Replay growth"
              >
                ▶
              </button>
              <input
                ref={scrubRef}
                type="range"
                min={0}
                defaultValue={9999}
                onChange={(e) => {
                  playingRef.current = false;
                  tDayRef.current = +e.target.value;
                  if (tlabelRef.current)
                    tlabelRef.current.textContent =
                      tDayRef.current >= spanRef.current
                        ? "Today"
                        : `Day ${Math.floor(tDayRef.current)}`;
                }}
                className="flex-1 accent-vb-red"
              />
              <span
                ref={tlabelRef}
                className="w-20 text-right text-[11.5px] tabular-nums text-vb-text-dim"
              >
                Today
              </span>
            </div>
          </div>

          <div className="flex flex-col gap-4">
            {selected ? (
              <div className="rounded-md border border-vb-border-subtle bg-vb-surface p-5">
                <span
                  className="inline-flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.12em]"
                  style={{
                    color: (TYPE_STYLE[selected.type] ?? TYPE_STYLE.life_event).c,
                  }}
                >
                  <span
                    className="h-2 w-2 rounded-full"
                    style={{
                      backgroundColor: (
                        TYPE_STYLE[selected.type] ?? TYPE_STYLE.life_event
                      ).c,
                    }}
                  />
                  {(TYPE_STYLE[selected.type] ?? TYPE_STYLE.life_event).label}
                  {selected.kind ? ` · ${selected.kind.replace(/_/g, " ")}` : ""}
                </span>
                <h3 className="mt-2.5 font-display text-lg font-normal leading-snug tracking-[-0.01em]">
                  {selected.label}
                </h3>
                {selected.summary && (
                  <p className="mt-2 text-[13px] leading-relaxed text-vb-text-dim">
                    {selected.summary}
                  </p>
                )}
                {selected.type === "insight" && (
                  <div className="mt-3.5 flex">
                    {["noted", "applied", "became_habit"].map((s, i) => (
                      <span
                        key={s}
                        className={cn(
                          "border border-vb-border-subtle px-2.5 py-1 text-[9.5px] font-medium uppercase tracking-[0.06em]",
                          i === 0 && "rounded-l",
                          i === 2 && "rounded-r",
                          selected.status === s
                            ? "border-vb-forest bg-vb-forest text-white"
                            : "bg-vb-bg text-vb-text-muted"
                        )}
                      >
                        {s.replace("_", " ")}
                      </span>
                    ))}
                  </div>
                )}
                {selectedEdges.length > 0 && (
                  <div className="mt-4 flex flex-col gap-2">
                    {selectedEdges.slice(0, 6).map((e) => (
                      <button
                        key={e.id}
                        onClick={() => setSelectedId(e.other.id)}
                        className="flex items-center gap-2.5 text-left text-[12.5px] text-vb-text-dim hover:text-vb-text"
                      >
                        <span
                          className="h-2 w-2 shrink-0 rounded-full"
                          style={{
                            backgroundColor: (
                              TYPE_STYLE[e.other.type] ?? TYPE_STYLE.life_event
                            ).c,
                          }}
                        />
                        <span>
                          <span className="text-[9.5px] uppercase tracking-[0.08em] text-vb-text-muted">
                            {e.outgoing ? e.word : `is ${e.word}`}
                          </span>{" "}
                          {e.other.label}
                        </span>
                      </button>
                    ))}
                  </div>
                )}
                <div className="mt-5 flex gap-2">
                  <Link
                    href="/dashboard/coach"
                    className="rounded-sm bg-vb-forest px-3.5 py-2 text-[12.5px] font-medium text-white hover:bg-vb-forest-soft"
                  >
                    Ask {coach} about this
                  </Link>
                  <button
                    onClick={() => hideMutation.mutate(selected.id)}
                    disabled={hideMutation.isPending}
                    className="rounded-sm border border-vb-border px-3.5 py-2 text-[12.5px] font-medium text-vb-text-dim hover:text-vb-text disabled:opacity-50"
                  >
                    Hide
                  </button>
                </div>
              </div>
            ) : (
              <div className="rounded-md border border-vb-border-subtle bg-vb-surface p-5 text-[13px] leading-relaxed text-vb-text-dim">
                Click any memory to see its story, where it came from, what it
                connects to, and what it became.
              </div>
            )}

            {reading?.reading && (
              <div className="rounded-md border border-vb-border-subtle bg-vb-surface p-5">
                <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-vb-forest">
                  {coach}&apos;s reading of your brain
                </p>
                <p className="mt-2.5 text-[13.5px] leading-relaxed text-vb-text-dim">
                  {reading.reading}
                </p>
                <p className="mt-2.5 font-script text-2xl leading-none text-vb-forest">
                  {coach}
                </p>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
