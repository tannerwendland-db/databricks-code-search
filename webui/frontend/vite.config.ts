import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Backend (webui/main.py, FastAPI) mounts SPAStaticFiles(directory=dist, html=True) at "/"
// AFTER its API routers, so in production the same origin serves both the built SPA and
// /api/*. In dev the backend runs on a separate port, so /api is proxied to it here.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
