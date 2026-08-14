/**
 * The daily inkscape — oil-ink road art of the great passes, one per day.
 *
 * Same deterministic day-hash idea as the daily quote: every rider wakes to
 * the same pass, and tomorrow brings another. The orbs belong to editorial;
 * IN the app, the ink itself is the imagery.
 */

export interface Inkscape {
  src: string;
  label: string;
}

export const INKSCAPES: Inkscape[] = [
  { src: "/inkscapes/ventoux.jpg", label: "MONT VENTOUX" },
  { src: "/inkscapes/stelvio.jpg", label: "PASSO DELLO STELVIO · 48 TORNANTI" },
  { src: "/inkscapes/sa-calobra.jpg", label: "SA CALOBRA · 26 BENDS" },
  { src: "/inkscapes/tourmalet.jpg", label: "COL DU TOURMALET" },
  { src: "/inkscapes/velodrome.jpg", label: "THE VELODROME · THE BOARDS" },
  { src: "/inkscapes/road-art-1.jpg", label: "THE SWITCHBACKS" },
  { src: "/inkscapes/road-art-2.jpg", label: "THE HIGH ROAD" },
];

export function dailyInkscape(dateISO?: string): Inkscape {
  const d = dateISO || new Date().toISOString().slice(0, 10);
  let h = 0;
  for (let i = 0; i < d.length; i++) h = (h * 31 + d.charCodeAt(i)) >>> 0;
  return INKSCAPES[h % INKSCAPES.length];
}

/** Map a workout type to its zone key + colour-ready label. */
export const WORKOUT_ZONE: Record<string, { z: string; name: string }> = {
  recovery: { z: "z1", name: "Recovery" },
  endurance: { z: "z2", name: "Endurance" },
  tempo: { z: "z3", name: "Tempo" },
  sweet_spot: { z: "z3", name: "Sweet spot" },
  threshold: { z: "z4", name: "Threshold" },
  vo2max: { z: "z5", name: "VO2 max" },
  vo2: { z: "z5", name: "VO2 max" },
  anaerobic: { z: "z6", name: "Anaerobic" },
  sprint: { z: "z7", name: "Sprint" },
  neuromuscular: { z: "z7", name: "Sprint" },
};
