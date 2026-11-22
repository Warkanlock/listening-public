import { useEffect, useRef, useState } from "react";
import { api, type LyricsResult } from "./api";

export function lyricRef(id: unknown): id is number | `custom-${number}` {
  return typeof id === "number" || (typeof id === "string" && /^custom-[1-9]\d{0,17}$/.test(id));
}

export interface LyricLine {
  time: number;
  text: string;
}

// The one reading of the /api/lyrics/<id> payload (plan 061 step 3):
// every consumer shares this classification instead of projecting
// the payload its own way. A payload that is found but carries
// neither text form is missing, not plain: the unification Plan
// 049's review recorded. A failed load is its own outcome, not
// missing, because the stage and the dialog answer it with
// different copy ("Lyrics are unavailable right now") than they
// answer a song with no lyrics.
export type LyricsOutcome =
  | { kind: "synced"; lines: LyricLine[] }
  | { kind: "plain"; text: string }
  | { kind: "missing" }
  | { kind: "unavailable" };

export function parseSyncedLyrics(synced: string): LyricLine[] {
  const lines: LyricLine[] = [];
  for (const raw of synced.split("\n")) {
    const stamps = [...raw.matchAll(/\[(\d+):(\d+(?:\.\d+)?)\]/g)];
    if (!stamps.length) continue;
    const text = raw.replace(/\[(\d+):(\d+(?:\.\d+)?)\]/g, "").trim();
    for (const stamp of stamps) {
      lines.push({
        time: Number(stamp[1]) * 60 + Number(stamp[2]),
        text,
      });
    }
  }
  return lines.sort((a, b) => a.time - b.time);
}

export function classifyLyrics(result: LyricsResult): LyricsOutcome {
  if (result.found && result.synced) {
    const lines = parseSyncedLyrics(result.synced);
    if (lines.length) return { kind: "synced", lines };
  }
  if (result.found && result.plain) {
    return { kind: "plain", text: result.plain };
  }
  return { kind: "missing" };
}

export async function loadLyrics(id: number | string): Promise<LyricsOutcome> {
  try {
    return classifyLyrics(await api<LyricsResult>(`/api/lyrics/${id}`));
  } catch {
    return { kind: "unavailable" };
  }
}

// The note an outcome reads as, where a surface shows one: the
// two failure kinds carry their own copy, and a found outcome
// needs none. Both the stage and the dialog project through
// this, so the strings live once.
export function lyricsNote(outcome: LyricsOutcome | null): string | null {
  if (outcome?.kind === "missing") return "No lyrics found for this song";
  if (outcome?.kind === "unavailable") {
    return "Lyrics are unavailable right now";
  }
  return null;
}

// The sequence guard the stage and the dialog each ran by hand:
// a response for an older id never overwrites a newer song's
// state. The outcome is null until the first load for the
// current id settles, which is the consumers' loading state.
export function useLyrics(
  id: number | string | null,
): { outcome: LyricsOutcome | null } {
  const [loaded, setLoaded] = useState<{ id: number | string; outcome: LyricsOutcome } | null>(null);
  const sequence = useRef(0);

  useEffect(() => {
    const current = ++sequence.current;
    setLoaded(null);
    if (id === null) return;
    void loadLyrics(id).then((result) => {
      if (sequence.current !== current) return;
      setLoaded({ id, outcome: result });
    });
    return () => { sequence.current++; };
  }, [id]);

  return { outcome: loaded?.id === id ? loaded.outcome : null };
}
