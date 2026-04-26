import path from "path";

import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

// In dev, Vite on :5173 proxies `/api/*` to Uvicorn (default :8000). If
// Windows blocks :8000 (WinError 10013), run Uvicorn on e.g. :8001 and set
// in `frontend/.env` or `.env.local`:  VITE_DEV_API_TARGET=http://127.0.0.1:8001
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, __dirname, "VITE_");
  const apiTarget = env.VITE_DEV_API_TARGET || "http://127.0.0.1:8000";

  return {
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
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
  };
});
