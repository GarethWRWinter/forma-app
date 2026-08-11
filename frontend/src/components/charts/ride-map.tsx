"use client";

import { useEffect, useMemo, useRef } from "react";
import type { RideDataPoint } from "@/lib/api";
import "leaflet/dist/leaflet.css";

/** The route, printed: monochrome tiles (chalk/ink register) with the ride
    drawn as a flamme line. Start = ink dot, finish = flamme kite. */
export function RideMap({ data }: { data: RideDataPoint[] }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<import("leaflet").Map | null>(null);

  const track = useMemo(
    () =>
      data
        .filter((p) => p.latitude != null && p.longitude != null)
        .map((p) => [p.latitude as number, p.longitude as number] as [number, number]),
    [data]
  );

  useEffect(() => {
    if (!containerRef.current || track.length < 2 || mapRef.current) return;

    let cancelled = false;
    (async () => {
      const L = (await import("leaflet")).default;
      if (cancelled || !containerRef.current) return;

      const map = L.map(containerRef.current, {
        zoomControl: false,
        attributionControl: true,
        scrollWheelZoom: false,
      });
      mapRef.current = map;

      // Positron: the print-not-pixels basemap. Light grey, no visual noise.
      L.tileLayer(
        "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        {
          attribution:
            '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
          subdomains: "abcd",
          maxZoom: 19,
        }
      ).addTo(map);

      const line = L.polyline(track, {
        color: "#FF3D00",
        weight: 3,
        opacity: 0.9,
        lineJoin: "round",
      }).addTo(map);

      // Start: quiet ink dot.
      L.circleMarker(track[0], {
        radius: 5,
        color: "#0B0B0C",
        fillColor: "#0B0B0C",
        fillOpacity: 1,
        weight: 0,
      }).addTo(map);

      // Finish: the kite, tip on the spot.
      const finish = track[track.length - 1];
      L.marker(finish, {
        icon: L.divIcon({
          className: "",
          html: '<div style="width:0;height:0;border-left:7px solid transparent;border-right:7px solid transparent;border-top:13px solid #FF3D00;"></div>',
          iconSize: [14, 13],
          iconAnchor: [7, 13],
        }),
      }).addTo(map);

      map.fitBounds(line.getBounds(), { padding: [24, 24] });
      L.control.zoom({ position: "bottomright" }).addTo(map);

      // Leaflet measures the container at init, which can be mid-layout and
      // wrongly sized (the classic zoomed-to-a-point bug). Re-measure and
      // re-fit once the frame has settled.
      requestAnimationFrame(() => {
        map.invalidateSize();
        map.fitBounds(line.getBounds(), { padding: [24, 24] });
      });
    })();

    return () => {
      cancelled = true;
      mapRef.current?.remove();
      mapRef.current = null;
    };
  }, [track]);

  if (track.length < 2) return null;

  return (
    <div
      ref={containerRef}
      className="h-[320px] w-full border border-vb-border-subtle md:h-[400px]"
      aria-label="Ride route map"
    />
  );
}
