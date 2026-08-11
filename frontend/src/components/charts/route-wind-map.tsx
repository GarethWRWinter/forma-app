"use client";

import { useEffect, useRef } from "react";
import "leaflet/dist/leaflet.css";

export interface RouteWind {
  track: [number, number][];
  km: number[];
  segments: { from_km: number; to_km: number; wind: "head" | "tail" | "cross" }[];
  wind_deg?: number | null;
  wind_kph?: number | null;
}

// Data colours (never brand): tailwind = free speed, headwind = the tax,
// crosswind = attention. From the zone ink ramp.
const WIND_COLOURS: Record<string, string> = {
  tail: "#439D7C",
  head: "#D92420",
  cross: "#D9AC34",
};

const WIND_LABELS: Record<string, string> = {
  tail: "Tailwind",
  head: "Headwind",
  cross: "Crosswind",
};

/** The goal route coloured by wind advantage, with the wind's arrow. */
export function RouteWindMap({ route }: { route: RouteWind }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<import("leaflet").Map | null>(null);

  useEffect(() => {
    if (!containerRef.current || route.track.length < 2 || mapRef.current) return;

    let cancelled = false;
    (async () => {
      const L = (await import("leaflet")).default;
      if (cancelled || !containerRef.current) return;

      const map = L.map(containerRef.current, {
        zoomControl: false,
        scrollWheelZoom: false,
      });
      mapRef.current = map;

      L.tileLayer(
        "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        {
          attribution:
            '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
          subdomains: "abcd",
          maxZoom: 19,
        }
      ).addTo(map);

      // Slice the track per wind segment via the km array.
      const bounds = L.latLngBounds(route.track);
      for (const seg of route.segments) {
        const idxFrom = route.km.findIndex((k) => k >= seg.from_km);
        let idxTo = route.km.findIndex((k) => k >= seg.to_km);
        if (idxTo === -1) idxTo = route.km.length - 1;
        const slice = route.track.slice(Math.max(0, idxFrom), idxTo + 1);
        if (slice.length >= 2) {
          L.polyline(slice, {
            color: WIND_COLOURS[seg.wind] ?? "#9A9A94",
            weight: 4,
            opacity: 0.95,
            lineJoin: "round",
          }).addTo(map);
        }
      }

      // Start dot + finish kite, same grammar as the ride map.
      L.circleMarker(route.track[0], {
        radius: 5, color: "#0B0B0C", fillColor: "#0B0B0C", fillOpacity: 1, weight: 0,
      }).addTo(map);
      L.marker(route.track[route.track.length - 1], {
        icon: L.divIcon({
          className: "",
          html: '<div style="width:0;height:0;border-left:7px solid transparent;border-right:7px solid transparent;border-top:13px solid #FF3D00;"></div>',
          iconSize: [14, 13],
          iconAnchor: [7, 13],
        }),
      }).addTo(map);

      // The wind itself: an ink arrow in the corner pointing where the wind
      // BLOWS TO (deg + 180), with speed.
      if (route.wind_deg != null) {
        const Arrow = L.Control.extend({
          onAdd() {
            const div = L.DomUtil.create("div");
            div.style.cssText =
              "background:#FFFFFF;border:1px solid #D8D8D2;padding:6px 10px;display:flex;align-items:center;gap:8px;font-family:'IBM Plex Mono',monospace;font-size:11px;color:#0B0B0C;";
            div.innerHTML = `<span style="display:inline-block;transform:rotate(${(route.wind_deg! + 180) % 360}deg);font-size:14px;line-height:1;">&uarr;</span> WIND${route.wind_kph != null ? ` ${Math.round(route.wind_kph)} KM/H` : ""}`;
            return div;
          },
        });
        new Arrow({ position: "topright" }).addTo(map);
      }

      map.fitBounds(bounds, { padding: [24, 24] });
      L.control.zoom({ position: "bottomright" }).addTo(map);
      requestAnimationFrame(() => {
        map.invalidateSize();
        map.fitBounds(bounds, { padding: [24, 24] });
      });
    })();

    return () => {
      cancelled = true;
      mapRef.current?.remove();
      mapRef.current = null;
    };
  }, [route]);

  if (route.track.length < 2) return null;

  return (
    <div>
      <div
        ref={containerRef}
        className="h-[300px] w-full border border-vb-border-subtle md:h-[380px]"
        aria-label="Goal route with wind advantage"
      />
      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1">
        {(["tail", "head", "cross"] as const).map((k) => (
          <span key={k} className="flex items-center gap-1.5">
            <span className="inline-block h-1 w-5" style={{ background: WIND_COLOURS[k] }} />
            <span className="f-kicker text-vb-text-dim">{WIND_LABELS[k]}</span>
          </span>
        ))}
      </div>
    </div>
  );
}
