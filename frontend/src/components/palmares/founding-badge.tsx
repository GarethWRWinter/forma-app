"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { Button } from "@/components/ui/button";
import { SectionHeader } from "@/components/ui/section-header";
import { INKSCAPES } from "@/lib/dailyInkscape";

/**
 * The founding badge — rider number as a badge of honour, designed by the
 * rider, shared on their feed. The photo never leaves the browser: it is
 * drawn straight onto a local canvas and downloaded from there.
 */

const W = 1080;
const H = 1350;

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((res, rej) => {
    const img = new Image();
    img.onload = () => res(img);
    img.onerror = rej;
    img.src = src;
  });
}

async function drawBadge(
  canvas: HTMLCanvasElement,
  opts: { number: number; name: string; photo: HTMLImageElement | null }
) {
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext("2d")!;

  // Ground: the rider's photo, or their number's own inkscape.
  let ground = opts.photo;
  if (!ground) {
    ground = await loadImage(INKSCAPES[(opts.number - 1) % INKSCAPES.length].src);
  }
  const scale = Math.max(W / ground.width, H / ground.height);
  const iw = ground.width * scale;
  const ih = ground.height * scale;
  ctx.drawImage(ground, (W - iw) / 2, (H - ih) / 2, iw, ih);

  // Scrim: heavy at the foot where the type lives, light above.
  const grad = ctx.createLinearGradient(0, H, 0, 0);
  grad.addColorStop(0, "rgba(0,0,0,0.78)");
  grad.addColorStop(0.45, "rgba(0,0,0,0.30)");
  grad.addColorStop(1, "rgba(0,0,0,0.18)");
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, W, H);

  await (document as Document & { fonts: FontFaceSet }).fonts.ready;

  // FORMA lockup top-left.
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

  // The cohort line.
  ctx.fillStyle = "rgba(255,255,255,0.85)";
  ctx.font = "500 30px 'IBM Plex Mono', monospace";
  ctx.fillText(`FOUNDING RIDER · ${opts.number} OF 100`, 72, 856);

  // The number — the trophy itself.
  ctx.fillStyle = "#FFFFFF";
  ctx.font = "800 300px Archivo, sans-serif";
  ctx.fillText(String(opts.number), 64, 900);
  const nw = ctx.measureText(String(opts.number)).width;

  // Flamme kite hanging off the number, same geometry as the wordmark dot.
  ctx.fillStyle = "#FF3D00";
  ctx.beginPath();
  ctx.moveTo(64 + nw + 28, 1090);
  ctx.lineTo(64 + nw + 92, 1090);
  ctx.lineTo(64 + nw + 60, 1150);
  ctx.closePath();
  ctx.fill();

  // Rider name at the foot.
  ctx.fillStyle = "rgba(255,255,255,0.7)";
  ctx.font = "500 26px 'IBM Plex Mono', monospace";
  ctx.fillText(
    `${opts.name.toUpperCase()} · RIDEWITHFORMA.COM`,
    72,
    H - 96
  );
}

export function FoundingBadge() {
  const { user } = useAuth();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [photo, setPhoto] = useState<HTMLImageElement | null>(null);
  const number = user?.founding_number ?? null;
  const name = user?.full_name || "Founding rider";

  const render = useCallback(async () => {
    if (!canvasRef.current || !number) return;
    await drawBadge(canvasRef.current, { number, name, photo });
  }, [number, name, photo]);

  useEffect(() => {
    void render();
  }, [render]);

  if (!number) return null;

  const onPickPhoto = async (file: File | undefined) => {
    if (!file) return;
    const url = URL.createObjectURL(file);
    try {
      setPhoto(await loadImage(url));
    } catch {
      URL.revokeObjectURL(url);
    }
  };

  const onDownload = () => {
    const a = document.createElement("a");
    a.download = `forma-founding-rider-${number}.png`;
    a.href = canvasRef.current!.toDataURL("image/png");
    a.click();
  };

  return (
    <section className="f-rise">
      <SectionHeader
        kicker={`Founding rider · ${number} of 100`}
        title="Your number."
      />
      <div className="grid gap-6 sm:grid-cols-[280px_1fr]">
        <canvas
          ref={canvasRef}
          className="w-full max-w-[280px] rounded-sm border border-vb-border-subtle"
          aria-label={`Founding rider badge, number ${number} of 100`}
        />
        <div className="max-w-md space-y-4 self-end">
          <p className="text-sm leading-relaxed text-vb-text-dim">
            One of the hundred, numbered for as long as Forma exists. Put
            your own photo behind it, or ride under the ink. Then wear it
            where the club can see it.
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <label className="cursor-pointer">
              <span className="inline-flex h-9 items-center rounded-sm border border-vb-border px-4 text-sm text-vb-text hover:border-vb-text">
                {photo ? "Change photo" : "Use your photo"}
              </span>
              <input
                type="file"
                accept="image/*"
                className="sr-only"
                onChange={(e) => void onPickPhoto(e.target.files?.[0])}
              />
            </label>
            {photo && (
              <button
                type="button"
                className="text-sm text-vb-text-muted underline-offset-4 hover:underline"
                onClick={() => setPhoto(null)}
              >
                Back to the ink
              </button>
            )}
            <Button variant="flamme" size="sm" onClick={onDownload}>
              Download your badge
            </Button>
          </div>
          <p className="f-data text-xs text-vb-text-muted">
            1080 × 1350, made for the feed. Your photo stays on your device.
          </p>
        </div>
      </div>
    </section>
  );
}
