import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the API runs on `pgt serve` (port 8000); Vite proxies /api to it.
// `vite build --mode replay` makes the static site GitHub Pages serves from a subpath,
// so its asset links are relative.
export default defineConfig(({ mode }) => ({
  plugins: [react()],
  base: mode === "replay" ? "./" : "/",
  build: { outDir: mode === "replay" ? "dist-replay" : "dist" },
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8000" } },
}));
