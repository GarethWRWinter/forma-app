/**
 * The daily pass: one of Gareth's own photographs of the climbs, one per day.
 *
 * Same deterministic day-hash idea as the daily quote: every rider wakes to
 * the same road, and tomorrow brings another. Shot by Gareth Winter on his
 * Fujifilm in Mallorca's Serra de Tramuntana, all in one monochrome grade
 * (scripts/brand/build_passes.py), so the flamme accent stays the only
 * colour. No people on the road: the rider puts themselves there. Replaced
 * the ink road art on 13 Oct 2026.
 */

export interface Pass {
  /** 1280 px wide: the default <img> source */
  src: string;
  /** 2000 px wide: for canvases (share cards are 1080 x 1350) */
  large: string;
  /** 1280 and 2000 px widths for a full-bleed <img srcSet> */
  srcSet: string;
  /** Where the road is, as a CSS object-position, so every crop keeps it */
  focus: string;
  label: string;
  alt: string;
}

function pass(slug: string, focus: string, label: string, alt: string): Pass {
  return {
    src: `/passes/${slug}-1280.webp`,
    large: `/passes/${slug}-2000.webp`,
    srcSet: `/passes/${slug}-1280.webp 1280w, /passes/${slug}-2000.webp 2000w`,
    focus,
    label,
    alt,
  };
}

export const PASSES: Pass[] = [
  pass(
    "sa-calobra-rock",
    "40% 78%",
    "SA CALOBRA · MALLORCA",
    "Sa Calobra's road threading along the foot of a sheer limestone face"
  ),
  pass(
    "formentor-road",
    "50% 70%",
    "FORMENTOR · MALLORCA",
    "An empty road bending towards the mountains of Formentor"
  ),
  pass(
    "tramuntana-switchbacks",
    "50% 62%",
    "SERRA DE TRAMUNTANA · MALLORCA",
    "Switchbacks climbing beneath a rocky summit in the Serra de Tramuntana"
  ),
  pass(
    "coast-road-sunset",
    "45% 62%",
    "THE COAST ROAD · MALLORCA",
    "A coast road at dusk, a headland and a rock island out at sea"
  ),
  pass(
    "sa-calobra-knot",
    "55% 62%",
    "SA CALOBRA · THE KNOT",
    "Sa Calobra's road looping under itself beneath a rocky peak"
  ),
  pass(
    "femenia",
    "45% 70%",
    "COLL DE FEMENIA · MALLORCA",
    "The road up the Coll de Femenia, under pines and limestone"
  ),
  pass(
    "formentor-cloud",
    "55% 80%",
    "FORMENTOR · ABOVE THE CLOUD",
    "A mountain peak rising out of a sea of cloud"
  ),
  pass(
    "tramuntana-descent",
    "45% 62%",
    "SERRA DE TRAMUNTANA · MALLORCA",
    "A mountain road bending above hairpins that drop towards the sea"
  ),
  pass(
    "formentor-cliffs",
    "35% 62%",
    "FORMENTOR · THE CLIFF ROAD",
    "A cliff road curving above the sea on the Formentor peninsula"
  ),
  pass(
    "sa-calobra-valley",
    "35% 70%",
    "SA CALOBRA · MALLORCA",
    "Sa Calobra's road winding through a limestone valley above the cloud"
  ),
  pass(
    "sunset-bend",
    "50% 66%",
    "THE LAST BEND · MALLORCA",
    "A mountain road bending into the low sun"
  ),
];

export function dailyPass(dateISO?: string): Pass {
  const d = dateISO || new Date().toISOString().slice(0, 10);
  let h = 0;
  for (let i = 0; i < d.length; i++) h = (h * 31 + d.charCodeAt(i)) >>> 0;
  return PASSES[h % PASSES.length];
}

/** Load a pass for drawing onto a canvas. */
export function loadPass(p: Pass): Promise<HTMLImageElement> {
  return new Promise((res, rej) => {
    const img = new Image();
    img.onload = () => res(img);
    img.onerror = rej;
    img.src = p.large;
  });
}

/** Cover-fit an image into a W x H canvas, aligned the way CSS
 *  object-position aligns it (so "40% 78%" frames a share card the same way
 *  it frames the hero), which keeps the road in a portrait crop. */
export function drawCover(
  ctx: CanvasRenderingContext2D,
  img: HTMLImageElement,
  W: number,
  H: number,
  focus = "50% 50%"
): void {
  const [fx, fy] = focus.split(" ").map((v) => parseFloat(v) / 100);
  const scale = Math.max(W / img.width, H / img.height);
  const iw = img.width * scale;
  const ih = img.height * scale;
  ctx.drawImage(img, (W - iw) * fx, (H - ih) * fy, iw, ih);
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
