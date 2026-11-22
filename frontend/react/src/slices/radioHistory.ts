import type { RadioMix } from "./radio";

type Source = Exclude<RadioMix, { kind: "library" }> | { kind: "library"; name: string };
export type RadioHistory = {
  source: Source;
  title: string;
  channel: string;
  current: string | number | null;
  position?: number;
  sample?: number;
  saved_at?: number;
  tuning?: string;
  retry?: { id: string; revision: number };
};

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function videoId(value: unknown): value is string {
  return typeof value === "string" && /^[\w-]{11}$/.test(value);
}

function readSource(source: unknown): Source | null {
  if (!record(source) || typeof source.name !== "string" || !source.name) return null;
  const { name, kind } = source;
  switch (kind) {
    case "library":
      return { kind, name };
    case "youtube":
      return Array.isArray(source.seeds) && source.seeds.length && source.seeds.every(videoId)
        ? { kind, name, seeds: source.seeds }
        : null;
    case "noise":
      return source.profile === "white" || source.profile === "brown"
        ? { kind, name, profile: source.profile }
        : null;
    case "live": {
      const stream = source.stream;
      if (!record(stream) || typeof stream.title !== "string" || typeof stream.url !== "string"
          || !/^https:\/\/ice2\.somafm\.com\/[\w-]+-128-mp3$/.test(stream.url)) return null;
      return { kind, name, stream: { title: stream.title, url: stream.url } };
    }
    default:
      return null;
  }
}

function tuningId(value: unknown): value is string {
  return typeof value === "string" && /^[a-f0-9]{32}$/.test(value);
}

export function parseRadioHistory(saved: unknown): RadioHistory | null {
  if (!record(saved) || typeof saved.title !== "string" || typeof saved.channel !== "string") return null;
  const source = readSource(saved.source);
  if (!source) return null;
  const { current, title, channel, tuning, retry, position, sample, saved_at } = saved;
  const seekable = source.kind === "library" || source.kind === "youtube";
  return {
    source, title, channel,
    current: source.kind === "library" && typeof current === "number" && Number.isSafeInteger(current)
      ? current
      : source.kind === "youtube" && videoId(current) ? current : null,
    position: seekable && typeof position === "number" && position >= 0 && position <= 86400
      ? position : undefined,
    sample: seekable && typeof sample === "number" && Number.isSafeInteger(sample) && sample >= 0
      ? sample : undefined,
    saved_at: typeof saved_at === "number" && Number.isFinite(saved_at) ? saved_at : undefined,
    tuning: tuningId(tuning) ? tuning : undefined,
    retry: record(retry) && tuningId(retry.id) && typeof retry.revision === "number" &&
      Number.isSafeInteger(retry.revision) && retry.revision >= 0
      ? { id: retry.id, revision: retry.revision } : undefined,
  };
}

export function readRadioHistory(key: string): RadioHistory | null {
  try {
    return parseRadioHistory(JSON.parse(localStorage.getItem(key) ?? "null"));
  } catch {
    return null;
  }
}

export function saveRadioHistory(key: string, history: RadioHistory | null) {
  try {
    if (history) localStorage.setItem(key, JSON.stringify(history));
    else localStorage.removeItem(key);
  } catch {
    // Playback still works when the device cannot persist browser state.
  }
}


export function readRadioClear(key: string): string | null {
  try {
    const id = localStorage.getItem(key);
    return tuningId(id) ? id : null;
  } catch {
    return null;
  }
}

export function saveRadioClear(key: string, id: string | null) {
  try {
    if (id) localStorage.setItem(key, id);
    else localStorage.removeItem(key);
  } catch {
    // A clear can still reach the room when device storage is unavailable.
  }
}
