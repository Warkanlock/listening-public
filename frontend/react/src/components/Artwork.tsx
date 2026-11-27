import { RoomIcon } from "./RoomIcon";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { coverUrl, type Track } from "../api";

// Artwork subject chain (plan 028 items 17-18, item 34): an imported
// HTTPS cover, then the generated portrait seeded from the subject,
// then the designed placeholder glyph. A failed cover image swaps to
// the portrait.

export interface ArtworkSubject {
  title?: string;
  artist?: string;
  album?: string;
  cover?: string;
  seed?: string | number;
  id?: string | number;
}

export function trackArtworkSubject(track: Track | null | undefined): ArtworkSubject {
  return {
    title: track?.title || "",
    artist: track?.artist || "",
    album: track?.album || "",
    cover: track?.cover || "",
    seed: track?.id ?? "",
  };
}

export function portraitSeed(subject: ArtworkSubject): string {
  const raw = String(subject.seed ?? subject.id ?? "").trim();
  if (/^[a-f0-9]{3,}$/i.test(raw)) return raw.slice(0, 12);
  const label = raw || `${subject.artist || ""} ${subject.title || ""}`.trim();
  let seed = "";
  for (const character of label) seed += (character.charCodeAt(0) % 16).toString(16);
  return seed.slice(0, 12);
}

let portraitPalettesCache: string[][] | null = null;

function portraitPalettes(): string[][] {
  if (portraitPalettesCache) return portraitPalettesCache;
  const style = getComputedStyle(document.body);
  portraitPalettesCache = [1, 2, 3, 4].map((index) =>
    ["a", "b", "c", "d"].map((tone) =>
      style.getPropertyValue(`--portrait-${index}${tone}`).trim(),
    ),
  );
  return portraitPalettesCache;
}

export function GradientPortrait({ seed: rawSeed }: { seed: string }) {
  const seed =
    String(rawSeed || "abc123")
      .replace(/[^a-f0-9]/g, "")
      .slice(0, 12) || "abc123";
  const n = parseInt(seed.slice(0, 6), 16);
  const palettes = portraitPalettes();
  const colors = palettes[n % palettes.length];
  const x = 84 + (n % 56);
  const y = 88 + (Math.floor(n / 7) % 32);
  return (
    <svg viewBox="0 0 300 300" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <defs>
        <linearGradient id={`g${seed}`} x2="1" y2="1">
          <stop stopColor={colors[0]} />
          <stop offset=".53" stopColor={colors[1]} />
          <stop offset="1" stopColor={colors[2]} />
        </linearGradient>
        <filter id={`n${seed}`}>
          <feTurbulence
            type="fractalNoise"
            baseFrequency=".65"
            numOctaves="3"
            seed={n % 50}
            stitchTiles="stitch"
          />
          <feColorMatrix type="saturate" values="0" />
        </filter>
      </defs>
      <rect width="300" height="300" fill={`url(#g${seed})`} />
      <circle cx={x} cy={y} r="87" fill={colors[2]} opacity=".28" />
      <path
        d="M35 300C51 208 91 192 148 194c70 2 111 47 117 106Z"
        fill={colors[3]}
        opacity=".62"
      />
      <ellipse cx="148" cy="126" rx="57" ry="70" fill={colors[3]} opacity=".72" />
      <path
        d="M94 116c4-50 35-76 72-73 42 4 62 38 61 84-22-21-36-40-49-48-25 34-55 48-84 37Z"
        fill={colors[3]}
        opacity=".84"
      />
      <rect
        width="300"
        height="300"
        filter={`url(#n${seed})`}
        opacity=".15"
        style={{ mixBlendMode: "multiply" }}
      />
    </svg>
  );
}

export function Artwork({ subject, loading }: { subject: ArtworkSubject; loading?: "lazy" | "eager" }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [subject.cover]);
  const url = coverUrl(subject.cover);
  if (url && !failed) {
    return <img src={url} alt="" loading={loading} onError={() => setFailed(true)} />;
  }
  const seed = portraitSeed(subject);
  return seed ? (
    <GradientPortrait seed={seed} />
  ) : (
    <span className="fallback"><RoomIcon name="music" /></span>
  );
}

// Simple cover-or-glyph rendering, the artMarkup shape used by rows,
// playlist thumbs and cards, and the hero.
export function CoverArt({
  cover,
  fallback = <RoomIcon name="music" />,
}: {
  cover?: string;
  fallback?: ReactNode;
}) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [cover]);
  const url = coverUrl(cover);
  if (url && !failed) {
    return <img src={url} alt="" loading="lazy" onError={() => setFailed(true)} />;
  }
  return <span className="fallback">{fallback}</span>;
}

// The CSS-only 3D CD case (item 18): fine-pointer tilt on the scene,
// reset on leave, never for touch or under reduced motion.
export function CdCase({ subject }: { subject: ArtworkSubject }) {
  const caseRef = useRef<HTMLDivElement>(null);
  const setTilt = (rx: number, ry: number) => {
    caseRef.current?.style.setProperty("--case-rx", `${rx}deg`);
    caseRef.current?.style.setProperty("--case-ry", `${ry}deg`);
  };
  return (
    <div
      className="cd-scene"
      onPointerMove={(event) => {
        if (event.pointerType === "touch") return;
        if (!matchMedia("(pointer: fine)").matches) return;
        if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
        const rect = event.currentTarget.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        const x = (event.clientX - rect.left) / rect.width - 0.5;
        const y = (event.clientY - rect.top) / rect.height - 0.5;
        setTilt(Number((4 - y * 8).toFixed(2)), Number((-18 + x * 10).toFixed(2)));
      }}
      onPointerLeave={() => setTilt(4, -18)}
    >
      <div className="cd-case" ref={caseRef}>
        <div className="cd-face cd-back" aria-hidden="true" />
        <div className="cd-face cd-spine" aria-hidden="true">
          <span>{subject.album || subject.title || ""}</span>
        </div>
        <div className="cd-face cd-front">
          <Artwork subject={subject} />
        </div>
        <div className="cd-gloss" aria-hidden="true" />
      </div>
      <div className="cd-shadow" aria-hidden="true" />
    </div>
  );
}

// The four artwork states (item 34): small and medium are circular
// and, in the artwork stage, use the showcase sizes from the plan 016
// tokens; large is the square state; case is the CD case.
export const ARTWORK_VIEWS = ["small", "medium", "large", "case"] as const;
export type ArtworkViewName = (typeof ARTWORK_VIEWS)[number];

export function ArtworkStage({
  subject,
  view,
}: {
  subject: ArtworkSubject;
  view: ArtworkViewName;
}) {
  if (view === "case") return <CdCase subject={subject} />;
  const className =
    view === "small"
      ? "artwork-large is-small"
      : view === "medium"
        ? "artwork-large is-medium"
        : "artwork-large";
  return (
    <div className={className}>
      <Artwork subject={subject} />
    </div>
  );
}

// The per-device remembered artwork view, shared by the song dialog
// and the artwork stage (item 34): starts from the stored value, and
// cycling walks small, medium, large, case, then back to small.
export function useArtworkView() {
  const [view, setViewState] = useState<ArtworkViewName>(() => {
    const stored = localStorage.getItem("listening-artwork-view");
    return (ARTWORK_VIEWS as readonly string[]).includes(stored || "")
      ? (stored as ArtworkViewName)
      : "large";
  });
  const setView = (next: ArtworkViewName, remember = true) => {
    setViewState(next);
    if (remember) localStorage.setItem("listening-artwork-view", next);
  };
  const cycle = () =>
    setView(
      ARTWORK_VIEWS[
        (ARTWORK_VIEWS.indexOf(view) + 1) % ARTWORK_VIEWS.length
      ],
    );
  return { view, setView, cycle };
}
