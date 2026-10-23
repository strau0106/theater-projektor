import type { NextConfig } from "next";

const basePath = (process.env.BASE_PATH ?? "").replace(/\/+$/, "");

const config: NextConfig = {
  output: "standalone",
  basePath,
  env: { NEXT_PUBLIC_BASE_PATH: basePath },
  serverExternalPackages: ["better-sqlite3"],
  poweredByHeader: false,
};

export default config;
