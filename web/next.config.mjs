/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The dashboard is fully static: it reads a JSON bundle produced by the
  // Python engine at build time. Nothing here needs a server at request time,
  // which is exactly what makes it safe to host on Vercel while every Choice
  // API call stays on the static-IP machine.
  output: "export",
  images: { unoptimized: true },
  trailingSlash: true,
};

export default nextConfig;
