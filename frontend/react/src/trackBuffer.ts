import { accountHeaders, type SessionTrack } from "./api";

const byteLimit = 128 * 1024 * 1024;

type RejectedAudio = { kind: "unavailable" } | { kind: "too-large"; minimumBytes: number };
type AudioRead = RejectedAudio | { kind: "ready"; file: Blob };

export function audioHref(track: Pick<SessionTrack, "audio">, compact: boolean, output: string) {
  const url = new URL(track.audio, location.origin);
  if (compact) url.searchParams.set("format", "aac");
  if (url.origin === location.origin) url.searchParams.set("output", output);
  return url.href;
}

async function readAudio(response: Response, limit: number): Promise<AudioRead> {
  if (!response.body) return { kind: "unavailable" };
  if (response.status !== 200 || !response.headers.get("Content-Type")?.startsWith("audio/")) {
    await response.body.cancel();
    return { kind: "unavailable" };
  }
  const length = Number(response.headers.get("Content-Length"));
  if (length > limit) {
    await response.body.cancel();
    return { kind: "too-large", minimumBytes: length };
  }
  const reader = response.body.getReader();
  const chunks: BlobPart[] = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) {
        await reader.cancel();
        return { kind: "too-large", minimumBytes: size };
      }
      chunks.push(new Uint8Array(value));
    }
    if (!size) return { kind: "unavailable" };
    return { kind: "ready", file: new Blob(chunks, { type: response.headers.get("Content-Type") || "" }) };
  } finally {
    reader.releaseLock();
  }
}

export class TrackBuffer {
  private wanted: string[] = [];
  private files = new Map<string, { url: string; size: number }>();
  private rejected = new Map<string, RejectedAudio>();
  private transfer: { href: string; controller: AbortController } | null = null;
  private retryAt = 0;
  private suspended = false;

  constructor(private readonly output: string, private readonly changed: () => void) {}

  url(href: string) { return this.files.get(href)?.url; }

  get ready() { return this.wanted.filter(href => this.files.has(href)).length; }

  private get usedBytes() {
    let size = 0;
    for (const file of this.files.values()) size += file.size;
    return size;
  }

  canStreamInstead(href: string) {
    const failure = this.rejected.get(href);
    return failure?.kind === "too-large" && failure.minimumBytes > byteLimit;
  }

  prepare(tracks: SessionTrack[], compact: boolean) {
    this.suspended = false;
    const wanted = [...new Set(tracks.map(track => audioHref(track, compact, this.output)))];
    if (wanted.join() !== this.wanted.join()) {
      this.retryAt = 0;
      this.wanted = wanted;
      for (const [href, file] of this.files) {
        if (!wanted.includes(href)) {
          URL.revokeObjectURL(file.url);
          this.files.delete(href);
        }
      }
      for (const href of this.rejected.keys()) {
        if (!wanted.includes(href)) this.rejected.delete(href);
      }
      if (this.transfer && !wanted.includes(this.transfer.href)) this.transfer.controller.abort();
    }
    if (!this.transfer && navigator.onLine && Date.now() >= this.retryAt) void this.fill();
  }

  suspend() {
    this.suspended = true;
    this.transfer?.controller.abort();
  }

  clear() { this.prepare([], false); }

  private async fill() {
    const remaining = byteLimit - this.usedBytes;
    if (remaining <= 0) return;
    const href = this.wanted.find(url => {
      const failure = this.rejected.get(url);
      return !this.files.has(url) && (!failure || (failure.kind === "too-large" && failure.minimumBytes <= remaining));
    });
    if (!href) return;
    const url = new URL(href);
    if (url.origin !== location.origin || !/^\/api\/audio\/(custom\/)?[0-9]+$/.test(url.pathname)) return;
    const controller = new AbortController();
    this.transfer = { href, controller };
    try {
      const response = await fetch(href, {
        signal: controller.signal, headers: accountHeaders(url.pathname), redirect: "error",
      });
      if (!response.ok) {
        await response.body?.cancel();
        throw new Error("Audio could not be buffered");
      }
      const audio = await readAudio(response, remaining);
      if (controller.signal.aborted || !this.wanted.includes(href)) return;
      if (audio.kind === "ready") {
        this.rejected.delete(href);
        this.files.set(href, { url: URL.createObjectURL(audio.file), size: audio.file.size });
      } else {
        this.rejected.set(href, audio);
      }
      this.changed();
    } catch {
      if (!controller.signal.aborted) this.retryAt = Date.now() + 5000;
    } finally {
      this.transfer = null;
      if (!this.suspended && navigator.onLine && Date.now() >= this.retryAt) void this.fill();
    }
  }
}
