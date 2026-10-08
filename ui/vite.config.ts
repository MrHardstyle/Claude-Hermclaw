import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// API target for the dev server / preview proxy (the built UI talks to the same origin '/api' by default).
const apiTarget = process.env.HERMCLAW_UI_API_TARGET ?? "http://127.0.0.1:8000";
const proxy = {
  "/api": {
    target: apiTarget,
    changeOrigin: false,
    // SSE must not be buffered or compressed by the proxy
    configure: (p: { on: (ev: "proxyRes", cb: (res: { headers: Record<string, unknown> }) => void) => void }) => {
      p.on("proxyRes", (res) => {
        const ctype = String(res.headers["content-type"] ?? "");
        if (ctype.startsWith("text/event-stream")) {
          res.headers["cache-control"] = "no-cache";
          res.headers["x-accel-buffering"] = "no";
        }
      });
    },
  },
};

export default defineConfig({
  plugins: [react()],
  base: process.env.HERMCLAW_UI_BASE ?? "/",
  build: {
    outDir: "dist",
    sourcemap: true,
    target: "es2022",
    assetsInlineLimit: 0,
  },
  server: { port: 5173, strictPort: true, proxy },
  preview: { port: Number(process.env.HERMCLAW_UI_PORT ?? 4173), strictPort: true, proxy },
});
