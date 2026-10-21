import type { NextConfig } from "next";

// The backend. src/middleware.ts forwards /api/* to the same place, adding
// the x-forma-edge header the rate limits trust; this rewrite stays as the
// fallback for anything the middleware doesn't match. Change both together.
const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${API_URL}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
