import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In dev, the Vite dev server runs on :5173 and proxies `/api/*` to the
// FastAPI process on :8000. In production, the built bundle is served by
// FastAPI itself (see app/main.py StaticFiles mount), so no proxy is
// needed.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
