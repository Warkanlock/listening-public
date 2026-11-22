import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const base = process.env.BASE ?? "/";

export default defineConfig({
  plugins: [react()],
  base,
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    rollupOptions: {
      input: [
        fileURLToPath(new URL("./index.html", import.meta.url)),
        fileURLToPath(new URL("./about/index.html", import.meta.url)),
      ],
    },
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8765",
    },
  },
});
