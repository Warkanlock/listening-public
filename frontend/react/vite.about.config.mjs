import { cp, rename } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const root = new URL("./", import.meta.url);
const output = new URL("landing-dist/", root);

export default defineConfig({
  root: fileURLToPath(root),
  publicDir: false,
  plugins: [
    react(),
    {
      name: "standalone-about",
      transformIndexHtml: {
        order: "pre",
        handler(html) {
          return html
            .replace(/<section data-room-only[\s\S]*?<\/section>/, "")
            .replaceAll(/<a [^>]*data-room-only[^>]*>[\s\S]*?<\/a>/g, "")
            .replace('class="about-room-link" href="%BASE_URL%">Open the room',
              'class="about-room-link" href="https://github.com/Warkanlock/listening-public">GitHub')
            .replaceAll('href="%BASE_URL%">Open the room', 'href="#connect">Connect a device')
            .replace('<meta charset="utf-8" />', '<meta charset="utf-8" /><meta name="robots" content="noindex" />');
        },
      },
      async closeBundle() {
        await rename(new URL("about/index.html", output), new URL("index.html", output));
        await cp(new URL("public/assets/about-showcase/room/", root), new URL("assets/about-showcase/room/", output), { recursive: true });
        for (const screenshot of ["owned-music.webp", "connect-a-device.webp"]) {
          await cp(new URL(`public/assets/about-showcase/onboarding/${screenshot}`, root),
            new URL(`assets/about-showcase/onboarding/${screenshot}`, output));
        }
        await cp(new URL("public/assets/icons/favicon.svg", root), new URL("assets/icons/favicon.svg", output));
      },
    },
  ],
  build: {
    outDir: fileURLToPath(output),
    emptyOutDir: true,
    rollupOptions: { input: fileURLToPath(new URL("about/index.html", root)) },
  },
});
