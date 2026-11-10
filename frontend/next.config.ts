import type { NextConfig } from "next";

// The browser talks to the Next.js origin only; API calls are proxied to FastAPI so the
// session cookie stays same-origin (HttpOnly, SameSite=Strict) and no backend URL or
// credential is embedded in client code.
const backend = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const securityHeaders = [
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  compress: false, // keep Server-Sent Events unbuffered through the proxy
  async rewrites() {
    // Demo build (NEXT_PUBLIC_DEMO_MODE=true) serves dummy data in the browser; there is no backend.
    if (process.env.NEXT_PUBLIC_DEMO_MODE === "true") return [];
    return [{ source: "/api/v1/:path*", destination: `${backend}/api/v1/:path*` }];
  },
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
