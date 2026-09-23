import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built assets land inside the Python package so the wheel ships a working
// dashboard and `swarmdeck serve` needs no Node at runtime.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/swarmdeck/ui",
    emptyOutDir: true,
    chunkSizeWarningLimit: 700,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8787",
      "/ws": { target: "ws://127.0.0.1:8787", ws: true },
    },
  },
});
