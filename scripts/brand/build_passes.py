"""Build Forma's mountain photography from Gareth's own masters.

Every photograph here was taken by Gareth Winter on his Fujifilm, so Forma
owns them outright (the Adobe Stock set tried first on 13 Oct 2026 was
licensed to Mindvalley and was never published).

They are published in Gareth's own colour grade, as shot: one camera and one
eye already make them a consistent set, and the colour carries the light and
warmth that make a rider want to be on that road (decided 15 Oct 2026, after
a week in monochrome).

    .venv/bin/python scripts/brand/build_passes.py [--mono]

--mono builds the monochrome grade instead (levels set per image, a gentle S
curve, a hint of warmth in the whites). Writes
WebP at 1280 and 2000 px wide into the app (frontend/public/passes) and the
landing site (~/forma-landing/img/passes), with metadata stripped.
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

MASTERS = Path("/Users/garethwinter/Documents/Forma /Brand Images /Mountains/Gareth Licenced")
APP = Path(__file__).resolve().parents[2] / "frontend" / "public" / "passes"
LANDING = Path.home() / "forma-landing" / "img" / "passes"
WIDTHS = (1280, 2000)

# master file -> published slug
SOURCES = {
    "2022_Gareth Winter Mallorca Sa Calobra 4.jpg": "sa-calobra-rock",
    "2022_Gareth Winter Mallorca Sa Calobra 8.jpg": "sa-calobra-valley",
    "2022_Gareth Winter Mallorca Sa Calobra 10.jpg": "sa-calobra-knot",
    "DSCF3029.jpg": "tramuntana-switchbacks",
    "DSCF3031.jpg": "tramuntana-descent",
    "2022_Gareth Winter Mallorca Formentor 6.jpg": "formentor-cloud",
    "2022_Gareth Winter Mallorca Formentor 4.jpg": "formentor-cliffs",
    "2022_Gareth Winter Mallorca Formentor 1.jpg": "formentor-road",
    "2022_Gareth Winter Mallorca Coll de Femenia 2.jpg": "femenia",
    "DSCF3300.jpg": "coast-road-sunset",
    "DSCF3299.jpg": "sunset-bend",
}
ON_LANDING = {"sa-calobra-valley", "formentor-cloud"}


def grade_mono(rgb: np.ndarray) -> np.ndarray:
    lum = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    lo, hi = np.percentile(lum, [0.5, 99.6])
    a = np.clip((lum - lo) / max(hi - lo, 1e-3), 0, 1)
    # bring every picture to the same middle grey before the curve
    med = float(np.median(a))
    a = a ** (np.log(0.47) / np.log(max(min(med, 0.95), 0.05)))
    a = 0.5 + (a - 0.5) * 1.12          # gentle S
    a = 0.035 + a * (0.965 - 0.035)     # deep, not crushed; bright, not clipped
    out = np.stack([a, a * 0.993, a * 0.978], -1)   # a hint of warmth
    return np.clip(out, 0, 1)


def main() -> None:
    mono = "--mono" in sys.argv
    grade = grade_mono if mono else (lambda a: a)
    APP.mkdir(parents=True, exist_ok=True)
    LANDING.mkdir(parents=True, exist_ok=True)
    for name, slug in SOURCES.items():
        src = ImageOps.exif_transpose(Image.open(MASTERS / name)).convert("RGB")
        # grade at 2400 wide: plenty for the largest output, quick to process
        base = src.resize((2400, round(src.height * 2400 / src.width)), Image.LANCZOS)
        graded = Image.fromarray(
            (grade(np.asarray(base, dtype=np.float32) / 255) * 255).round().astype(np.uint8)
        )
        for w in WIDTHS:
            im = graded.resize((w, round(graded.height * w / 2400)), Image.LANCZOS)
            targets = [APP] + ([LANDING] if slug in ON_LANDING else [])
            for t in targets:
                out = t / f"{slug}-{w}.webp"
                im.save(out, "WEBP", quality=74 if w == 1280 else 70, method=6)
                print(f"{out.relative_to(out.parents[2])}: {im.width}x{im.height}, {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
