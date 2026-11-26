import { canOpenMusicProfile } from "../artistConnections";
import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import { Dialog, DialogBackdrop, DialogClose, DialogContent, DialogTitle } from "@ui-sdk/react/dialog";
import { Toggle } from "@ui-sdk/react/toggle";
import type { SessionTrack } from "../api";
import { useStore } from "../store";
import { CoverArt, GradientPortrait, portraitSeed } from "./Artwork";
import { FavoriteButton } from "./FavoriteButton";
import { PlaybackDevices } from "./PlaybackDevices";
import { RoomIcon } from "./RoomIcon";

type Point = { x: number; y: number };
type Edge = "left" | "right" | "top" | "bottom";
type Placement = Point | Edge | null;
const positionKey = "listening-player-window";
const arrows = {
  ArrowLeft: "left", ArrowRight: "right", ArrowUp: "top", ArrowDown: "bottom",
} as const;
const gutter = 12;

function readPlacement(): Placement {
  const stored = localStorage.getItem(positionKey);
  if (stored === "left" || stored === "right" || stored === "top" || stored === "bottom") return stored;
  if (!stored) return null;
  const [x, y] = stored.split(",").map(Number);
  return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null;
}

function clamp(point: Point, width: number, height: number): Point {
  return {
    x: Math.max(gutter, Math.min(point.x, innerWidth - width - gutter)),
    y: Math.max(gutter, Math.min(point.y, innerHeight - height - gutter)),
  };
}

function nearbyEdge(x: number, y: number): Edge | null {
  const distances: Record<Edge, number> = { left: x, right: innerWidth - x, top: y, bottom: innerHeight - y };
  const nearest = (Object.keys(distances) as Edge[]).reduce((closest, edge) =>
    distances[edge] < distances[closest] ? edge : closest);
  return distances[nearest] <= 28 ? nearest : null;
}

export function NowPlaying({ track, timeline, transport, close, explore }: {
  track: SessionTrack;
  timeline: ReactNode;
  transport: "Play" | "Pause" | "Join audio" | "Listen here";
  close: () => void;
  explore: (id: number | string, mode: "song" | "album" | "artist-profile" | "album-tracks") => void;
}) {
  const store = useStore();
  const session = store.session;
  const dialog = useRef<HTMLDivElement>(null);
  const [placement, setPlacement] = useState(readPlacement);
  const [preview, setPreview] = useState<Edge | null>(null);
  const drag = useRef<{ id: number; start: Point; offset: Point; point: Point | null; edge: Edge | null } | null>(null);
  const dock = typeof placement === "string" ? placement : null;
  const floating = typeof placement === "object" ? placement : null;

  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    const keepVisible = () => setPlacement(current => {
      if (!current || typeof current === "string") return current;
      const next = clamp(current, element.offsetWidth, element.offsetHeight);
      return current.x === next.x && current.y === next.y ? current : next;
    });
    const observer = new ResizeObserver(keepVisible);
    observer.observe(element);
    window.addEventListener("resize", keepVisible);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", keepVisible);
    };
  }, []);

  function place(next: Placement) {
    setPlacement(next);
    if (next) localStorage.setItem(positionKey, typeof next === "string" ? next : [next.x, next.y].join(","));
    else localStorage.removeItem(positionKey);
  }

  function startDrag(event: PointerEvent<HTMLDivElement>) {
    if (drag.current || !event.isPrimary || event.button !== 0 || !dialog.current) return;
    const rect = dialog.current.getBoundingClientRect();
    const width = dock ? Math.min(468, innerWidth - gutter * 2) : rect.width;
    drag.current = {
      id: event.pointerId,
      start: { x: event.clientX, y: event.clientY },
      offset: { x: (event.clientX - rect.left) / rect.width * width, y: event.clientY - rect.top },
      point: null,
      edge: null,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function dragWindow(event: PointerEvent<HTMLDivElement>) {
    const active = drag.current;
    const element = dialog.current;
    if (!active || !element || active.id !== event.pointerId) return;
    const { clientX: x, clientY: y } = event;
    if (!active.point && Math.hypot(x - active.start.x, y - active.start.y) < 5) return;
    active.point = clamp({ x: x - active.offset.x, y: y - active.offset.y },
      dock ? Math.min(468, innerWidth - gutter * 2) : element.offsetWidth,
      dock ? 0 : element.offsetHeight);
    active.edge = nearbyEdge(x, y);
    setPreview(active.edge);
    setPlacement(active.point);
  }

  function finishDrag(event: PointerEvent<HTMLDivElement>, snap: boolean) {
    const active = drag.current;
    if (!active || active.id !== event.pointerId) return;
    drag.current = null;
    setPreview(null);
    if (active.point) place((snap && active.edge) || active.point);
  }

  function moveWithKeys(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Home") {
      event.preventDefault();
      place(null);
      return;
    }
    const edge = arrows[event.key as keyof typeof arrows];
    const element = dialog.current;
    if (!edge || !element) return;
    event.preventDefault();
    if (event.shiftKey) {
      place(edge);
      return;
    }
    const rect = element.getBoundingClientRect();
    place(clamp({
      x: rect.left + (edge === "left" ? -16 : edge === "right" ? 16 : 0),
      y: rect.top + (edge === "top" ? -16 : edge === "bottom" ? 16 : 0),
    }, rect.width, rect.height));
  }

  const openConnections = (mode: "song" | "album" | "artist-profile" | "album-tracks") => {
    if (!canOpenMusicProfile(track.id)) return;
    explore(track.id, mode);
  };
  const nextTrack = session?.upcoming.find(Boolean);

  const panels = {
    track: <section className="now-playing-track" aria-label="Track and controls">
      <div className="now-playing-cover" aria-hidden="true">
        <div className="now-playing-artwork" key={track.cover || portraitSeed(track)}
          onLoadCapture={event => { event.currentTarget.dataset.ready = ""; }}>
          <CoverArt cover={track.cover} fallback={<GradientPortrait seed={portraitSeed(track)} />} />
        </div>
      </div>
      <div className="now-playing-song">
        <div className="now-playing-title">
          <h2>{track.title}</h2>
          <p>{canOpenMusicProfile(track.id) ? <button type="button" className="music-profile-byline"
            aria-label={`Open artist ${track.artist}`} onClick={() => openConnections("artist-profile")}>{track.artist}</button> : track.artist}</p>
          {track.album && <small>{canOpenMusicProfile(track.id) ? <button type="button" className="music-profile-byline"
            aria-label={`Open album ${track.album}`} onClick={() => openConnections("album-tracks")}>{track.album}</button> : track.album}</small>}
        </div>
        <FavoriteButton className="now-playing-icon" track={track} />
      </div>
      {timeline}
      <div className="now-playing-controls" role="group" aria-label="Playback controls">
        <Toggle className="now-playing-icon" pressed={!!session?.shuffle}
          onPressedChange={() => void store.sendSession("shuffle")} aria-label="Shuffle play" title="Shuffle play">
          <RoomIcon name="shuffle" />
        </Toggle>
        <button className="now-playing-icon" type="button" aria-label="Previous track"
          onClick={() => void store.sendSession("previous")}><RoomIcon name="previous" /></button>
        <button className="now-playing-icon now-playing-toggle" type="button" aria-label={transport}
          onClick={() => store.togglePlayback()}><RoomIcon name={transport === "Pause" ? "pause" : "play"} /></button>
        <button className="now-playing-icon" type="button" aria-label="Next track"
          onClick={() => void store.sendSession("next")}><RoomIcon name="next" /></button>
      </div>
      {typeof track.id === "number" ? (
        <div className="now-playing-connections">
          <button type="button" onClick={() => openConnections("song")}><RoomIcon name="discover" />Artist credits</button>
          {track.album ? <button type="button" onClick={() => openConnections("album")}><RoomIcon name="album" />Explore album</button> : null}
        </div>
      ) : null}
    </section>,
    devices: <details className="now-playing-details">
      <summary>Playback devices</summary>
      <PlaybackDevices />
    </details>,
    queue: <details className="now-playing-details">
      <summary><span>Queue</span>{nextTrack?.title && <small>Next · {nextTrack.title}</small>}</summary>
      {session?.upcoming.some(Boolean) ? <ol className="now-playing-queue">
        {session.upcoming.map((item, index) => item && <li key={index}>
          <button type="button" onClick={() => void store.sendSession("jump", { index, expected_version: session.version })}>
            <strong>{item.title}</strong><span>{item.artist}</span>
          </button>
        </li>)}
      </ol> : <p>Nothing queued.</p>}
    </details>,
  };

  return <Dialog open onOpenChange={open => { if (!open) close(); }}>
    <DialogBackdrop />
    {preview && <div className="now-playing-snap-preview" data-dock={preview} aria-hidden="true" />}
    <DialogContent ref={dialog} className="now-playing" data-dock={dock ?? undefined}
      style={floating ? { left: floating.x, top: floating.y, right: "auto", bottom: "auto", margin: 0 } : undefined}>
      <div className="now-playing-background" aria-hidden="true">
        <CoverArt cover={track.cover} fallback={<GradientPortrait seed={portraitSeed(track)} />} />
      </div>
      <header className="now-playing-header">
        <div className="now-playing-titlebar" role="group" aria-label="Player window position"
          title="Drag to move. Drop at an edge to snap. Double-click to center."
          tabIndex={0} aria-keyshortcuts="Shift+ArrowLeft Shift+ArrowRight Shift+ArrowUp Shift+ArrowDown Home"
          onPointerDown={startDrag} onPointerMove={dragWindow}
          onPointerUp={event => finishDrag(event, true)}
          onLostPointerCapture={event => finishDrag(event, false)}
          onDoubleClick={() => place(null)} onKeyDown={moveWithKeys}>
          <DialogTitle>Now playing</DialogTitle>
        </div>
        <DialogClose className="now-playing-icon" aria-label="Close now playing"><RoomIcon name="close" /></DialogClose>
      </header>
      <div className="now-playing-body">
        <div className="now-playing-panels">
          <div className="now-playing-panel" data-section="track">{panels.track}</div>
          <div className="now-playing-panel">{panels.queue}</div>
          <div className="now-playing-panel">{panels.devices}</div>
        </div>
      </div>
    </DialogContent>
  </Dialog>;
}
