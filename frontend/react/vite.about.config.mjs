import { cp, rename } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const root = new URL("./", import.meta.url);
const output = new URL("landing-dist/", root);
const tourUrl = "https://listening-room-tour.vercel.app/";
const socialImage = "listening-room-social.jpg";
const socialPreview = {
  title: "Listening Room",
  description: "Stream the records you own. Host your music library and listen across your devices.",
  image: new URL(socialImage, tourUrl).href,
  "image:alt": "Listening Room logo beside a screenshot of the music library and playback queue.",
};
const socialTags = [
  { tag: "link", injectTo: "head", attrs: { rel: "canonical", href: tourUrl } },
  ...Object.entries({
    type: "website",
    url: tourUrl,
    ...socialPreview,
    "image:type": "image/jpeg",
    "image:width": "1200",
    "image:height": "630",
  }).map(([key, content]) => ({ tag: "meta", injectTo: "head", attrs: { property: `og:${key}`, content } })),
  ...Object.entries({ card: "summary_large_image", ...socialPreview })
    .map(([key, content]) => ({ tag: "meta", injectTo: "head", attrs: { name: `twitter:${key}`, content } })),
];

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
          const standalone = html
            .replace(/<section data-room-only[\s\S]*?<\/section>/, "")
            .replaceAll(/<a [^>]*data-room-only[^>]*>[\s\S]*?<\/a>/g, "")
            .replace('class="about-room-link" href="%BASE_URL%">Open the room',
              'class="about-room-link" href="https://github.com/Warkanlock/listening-public">GitHub')
            .replaceAll('href="%BASE_URL%">Open the room', 'href="#connect">Connect a device');
          return { html: standalone, tags: socialTags };
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
        await cp(new URL(`about/${socialImage}`, root), new URL(socialImage, output));
      },
    },
  ],
  build: {
    outDir: fileURLToPath(output),
    emptyOutDir: true,
    rollupOptions: { input: fileURLToPath(new URL("about/index.html", root)) },
  },
});
