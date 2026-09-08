/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // NOTE: this is deliberately NOT a static export any more. Multi-user login
  // needs server-side routes so the session token can live in an httpOnly
  // cookie the browser cannot read.
  //
  // Vercel still cannot talk to Choice — Choice binds each API key to a
  // declared static IP and rejects everything else. So these server routes
  // call the ENGINE, which runs on that static-IP machine and holds one
  // authenticated Choice session per logged-in user. No credential and no
  // Choice call ever originates from Vercel.
  poweredByHeader: false,
  images: { unoptimized: true },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
};

export default nextConfig;
