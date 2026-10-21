import { NextResponse, type NextRequest } from "next/server";

/**
 * Every /api/* call from the browser goes to the FastAPI backend on Railway,
 * stamped with a secret only this server side holds, so the backend can tell
 * a request that came through Forma's own frontend from one sent straight to
 * Railway (review round 3, new problem 2).
 *
 * Why it matters: the rate limits key on the rider's address. Through Vercel,
 * the first X-Forwarded-For hop is the rider, because Vercel overwrites the
 * header with the address that really connected. Straight to Railway, the
 * first hop is whatever the caller typed. Without a way to tell the two
 * apart, a direct caller could pick a new address on every request and walk
 * past every limit (app/core/ratelimit.py, via_edge).
 *
 * FORMA_EDGE_SECRET is read here on the server only. It has no NEXT_PUBLIC_
 * prefix, so it never reaches a browser bundle. Set the same value on
 * Railway (FORMA_EDGE_SECRET). Order matters: set it on Vercel and redeploy
 * the frontend first, then set it on Railway. Until Railway has it, the
 * backend ignores the header and keeps its older rule.
 *
 * Kept as middleware.ts, not Next 16's proxy.ts: middleware runs on the edge,
 * where Vercel performs the rewrite itself and streams the body through, as
 * the next.config.ts rewrite always has. proxy.ts would run on Node.
 */

// The same destination as the rewrite in next.config.ts, which stays as the
// fallback. Change both together.
const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// app/core/ratelimit.py EDGE_HEADER.
const EDGE_HEADER = "x-forma-edge";

export function middleware(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  // Built as next.config.ts builds it: `${API_URL}/api/:path*`.
  const destination = new URL(`${API_URL}${pathname}${search}`);

  const headers = new Headers(request.headers);
  // Never pass on one the caller sent: only this server sets it.
  headers.delete(EDGE_HEADER);
  const secret = (process.env.FORMA_EDGE_SECRET || "").trim();
  if (secret) headers.set(EDGE_HEADER, secret);

  return NextResponse.rewrite(destination, { request: { headers } });
}

export const config = {
  matcher: "/api/:path*",
};
