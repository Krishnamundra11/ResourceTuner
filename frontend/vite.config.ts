import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

// The app is served by FastAPI under /ui/ (see resourcetuner/dashboard/main.py), so assets must
// be requested relative to that prefix. Built output lands in the dashboard's static directory;
// `app/` keeps it separate from the original dashboard.html, which stays reachable at /legacy.
export default defineConfig({
  base: "/ui/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  build: {
    outDir: "../resourcetuner/dashboard/static/app",
    emptyOutDir: true,
    sourcemap: false,
    // The charts chunk is ECharts itself and is loaded lazily; 500 kB is its tree-shaken floor.
    chunkSizeWarningLimit: 600,
  },
  server: {
    port: 5173,
    // `npm run dev` talks to a locally running dashboard; production serves both from one origin.
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true, ws: false } },
  },
});
