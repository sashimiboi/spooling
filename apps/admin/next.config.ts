import type { NextConfig } from "next";

const SYNC_SERVER = process.env.SYNC_SERVER_URL || "http://localhost:8001";

const config: NextConfig = {
  output: "standalone",
  env: {
    NEXT_PUBLIC_SYNC_API_KEY: process.env.NEXT_PUBLIC_SYNC_API_KEY,
  },
  turbopack: {
    root: __dirname,
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          {
            key: "Content-Security-Policy",
            value: [
              "default-src 'self'",
              "script-src 'self' 'unsafe-inline' 'unsafe-eval'", // unsafe-eval needed by Next.js dev; tighten in prod
              "style-src 'self' 'unsafe-inline'",
              "img-src 'self' data: blob: https:",
              "font-src 'self' data:",
              "connect-src 'self' https://api.anthropic.com https://api.openai.com https://api.groq.com https://generativelanguage.googleapis.com https://*.amazonaws.com",
              "frame-ancestors 'none'",
            ].join("; "),
          },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
  async rewrites() {
    return [
      {
        source: "/v1/:path*",
        destination: `${SYNC_SERVER}/v1/:path*`,
      },
    ];
  },
};

export default config;
