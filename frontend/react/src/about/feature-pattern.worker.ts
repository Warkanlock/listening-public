/// <reference lib="webworker" />

import type { PatternResult } from "./feature-search";

declare const self: DedicatedWorkerGlobalScope;

function reply(result: PatternResult) {
  self.postMessage(result);
}

self.addEventListener("message", ({ data }: MessageEvent<unknown>) => {
  if (!data || typeof data !== "object" || !("query" in data) || !("texts" in data) ||
      typeof data.query !== "string" || !Array.isArray(data.texts) ||
      !data.texts.every(text => typeof text === "string")) {
    reply({ kind: "invalid" });
    return;
  }
  let pattern: RegExp;
  try {
    pattern = new RegExp(data.query, "iu");
  } catch {
    reply({ kind: "invalid" });
    return;
  }
  reply({ kind: "matches", indexes: data.texts.flatMap((text, index) => pattern.test(text) ? [index] : []) });
});
