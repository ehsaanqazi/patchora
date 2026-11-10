import type { NextConfig } from "next";

// The browser talks to the Next.js origin only; API calls are proxied to FastAPI so the
// session cookie stays same-origin (HttpOnly, SameSite=Strict) and no backend URL or
// credential is embedded in client code.
const backend = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

// Demo build (NEXT_PUBLIC_DEMO_MODE=true): a fully static export to `out/` that answers every API
// call in the browser from dummy data (lib/demo). No server and no backend, so it can be hosted on any
// static host (Netlify). Rewrites and headers are unsupported in static exports; for the demo the
// security headers are set by the host instead (see netlify.toml).
const demo = process.env.NEXT_PUBLIC_DEMO_MODE === "true";

const securityHeaders = [
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

const nextConfig: NextConfig = demo
  ? {
      output: "export",
      reactStrictMode: true,
      poweredByHeader: false,
    }
  : {
      reactStrictMode: true,
      poweredByHeader: false,
      compress: false, // keep Server-Sent Events unbuffered through the proxy
      async rewrites() {
        return [{ source: "/api/v1/:path*", destination: `${backend}/api/v1/:path*` }];
      },
      async headers() {
        return [{ source: "/:path*", headers: securityHeaders }];
      },
    };

export default nextConfig;
