// Build-time service worker generator (plan 042, replacing plan 017's
// hand-maintained shell list and cache hash). Runs after every Vite
// build: enumerates the dist output, computes the cache name from the
// file contents, and writes dist/service-worker.js with the precache
// list under the build base. The fetch handler is plan 017's:
// network-first with cache fallback, same-origin GET only, and it
// never intercepts /api/ (audio lives under /api/audio/).

import { createHash } from "node:crypto";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const dist = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "dist");
const rawBase = process.env.BASE ?? "/";
const base = rawBase.endsWith("/") ? rawBase : `${rawBase}/`;
// These entry points must be fetched from the server. The feature tour
// can be restricted to the tailnet even when the player is open on the LAN.
const networkOnly = new Set(["service-worker.js", "pairing.html", "about/index.html"]);

function listFiles(dir, prefix = "") {
  const files = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      files.push(...listFiles(join(dir, entry.name), relative));
    } else if (entry.isFile() && !networkOnly.has(relative) &&
               !relative.startsWith("assets/first-light-") && !relative.startsWith("assets/feature-pattern.worker-") &&
               !relative.startsWith("assets/about-showcase/")) {
      files.push(relative);
    }
  }
  return files;
}

const files = listFiles(dist).sort();
const hash = createHash("sha256");
for (const file of files) {
  hash.update(readFileSync(join(dist, file)));
}
const cache = `listening-room-shell-${hash.digest("hex").slice(0, 8)}`;
const shell = [...new Set([base, ...files.map((file) => base + file)])];

const source = `const CACHE = ${JSON.stringify(cache)};
const SHELL = ${JSON.stringify(shell)};
self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});
self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((names) => Promise.all(names.filter((name) => name !== CACHE).map((name) => caches.delete(name)))));
  self.clients.claim();
});
self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/") || !SHELL.includes(url.pathname)) return;
  event.respondWith(fetch(event.request).then((response) => {
    if (response.ok) {
      const copy = response.clone();
      caches.open(CACHE).then((cache) => cache.put(event.request, copy));
    }
    if (response.status >= 500) {
      return caches.match(event.request).then((cached) => cached || response);
    }
    return response;
  }).catch(() => caches.match(event.request)));
});
`;

writeFileSync(join(dist, "service-worker.js"), source);
console.log(`gen-sw: ${cache}, ${shell.length} precached paths under ${base}`);
