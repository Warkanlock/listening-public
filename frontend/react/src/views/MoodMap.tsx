import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { ApiError, api, isHost, postJson, type Mood } from "../api";
import { RoomAction, RoomSelect } from "../components/RoomControls";
import { EditMood } from "../components/EditMood";
import { useStore } from "../store";

type Point = { x: number; y: number };
type Mix = { first: Mood; second?: Mood; percent: number };
type Source = "auto" | "youtube" | "catalogs";

function sample(moods: Mood[], point: Point): Mix {
  const distances = moods.map(mood => ({ mood, distance: (mood.x - point.x) ** 2 + (mood.y - point.y) ** 2 }))
    .sort((a, b) => a.distance - b.distance);
  const [first, second] = distances;
  const percent = second ? Math.round(100 * first.distance / (first.distance + second.distance || 1)) : 0;
  return { first: first.mood, second: percent >= 5 ? second.mood : undefined, percent: percent >= 5 ? percent : 0 };
}

function mixName({ first, second }: Mix) {
  return second ? first.name + " + " + second.name : first.name;
}

function genresFor({ first, second }: Mix) {
  return [...new Set([...first.genres, ...second?.genres ?? []])].sort();
}

function pointAt(event: PointerEvent<HTMLElement>, moods: Mood[]) {
  // Capture keeps a drag alive outside its first target. Hit-test the release
  // position so a finger can also travel from one landmark to another.
  const node = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>("[data-landmark]");
  const mood = node && event.currentTarget.contains(node) && moods.find(m => m.id === node.dataset.landmark);
  if (mood) return mood;
  const box = event.currentTarget.getBoundingClientRect();
  return { x: (event.clientX - box.left) * 100 / box.width, y: (event.clientY - box.top) * 100 / box.height };
}

export function MoodMap() {
  const store = useStore();
  const browse = store.browsingSlice;
  const moods = browse.moods;
  const [chosen, setChosen] = useState<Point | null>(null);
  const [preview, setPreview] = useState<Point | null>(null);
  const [genre, setGenre] = useState("");
  const [keyFeedback, setKeyFeedback] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [editing, setEditing] = useState<Mood | null>(null);
  const [source, setSource] = useState<Source>("auto");
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [links, setLinks] = useState<{ title: string; url: string }[]>([]);
  const request = useRef<AbortController | null>(null);
  const sessionRequest = useRef<Promise<boolean> | null>(null);
  useEffect(() => () => request.current?.abort(), []);

  const origin = chosen ?? moods.find(m => m.id === "focused") ?? moods[0];
  const point = preview ?? origin;
  const selection = origin && moods.length ? sample(moods, origin) : null;
  const mix = preview && selection ? sample(moods, preview) : selection;
  const genres = selection ? genresFor(selection) : [];
  const currentGenre = genres.includes(genre) ? genre : "";

  function cancelLookup() {
    request.current?.abort();
    setPending(false);
    setFeedback("");
    setLinks([]);
  }

  async function tune(position: Point, preferredGenre = genre) {
    if (!moods.length) return;
    const target = sample(moods, position);
    const tag = genresFor(target).includes(preferredGenre) ? preferredGenre : "";
    const term = tag || mixName(target);
    setChosen(position);
    setPreview(null);
    setGenre(tag);
    setEditing(null);
    cancelLookup();
    const controller = new AbortController();
    request.current = controller;
    const { signal } = controller;
    setPending(true);
    const fields = { mood: target.first.id, genre: tag,
      ...(target.second ? { blend: target.second.id, blend_percent: target.percent } : {}) };
    try {
      // Serialize session mutations so a slow earlier tap cannot replace the latest mix.
      if (sessionRequest.current) await sessionRequest.current;
      if (signal.aborted) return;
      if (source === "auto") {
        let empty = false;
        const playback = store.sessionSlice.sendSession("play_mood", fields, "", error => {
          empty = error instanceof ApiError && (error.data as { code?: string } | null)?.code === "empty_mood";
        });
        sessionRequest.current = playback;
        let started;
        try { started = await playback; }
        finally { if (sessionRequest.current === playback) sessionRequest.current = null; }
        if (signal.aborted) return;
        if (started) return;
        if (!empty) {
          setFeedback("Couldn’t start this mix. Check the room connection and try again.");
          return;
        }
      }
      if (source === "catalogs") {
        const result = await postJson<{ groups: { items: { title: string; url: string }[] }[] }>(
          "/api/discovery/search", { media: "music", scope: "public", source: "live-music", query: term.replace(" + ", " "), page: 0 }, signal);
        if (signal.aborted) return;
        const found = result.groups.flatMap(group => group.items).slice(0, 4);
        setLinks(found);
        setFeedback(found.length ? "Open a recording at its source." : "No recordings found in this catalog.");
        return;
      }
      if (!store.radioSlice.youtubeEnabled) {
        setFeedback("No playable tracks in this mood. Try another mood or choose Open catalogs.");
        return;
      }
      const params = new URLSearchParams({ mood: target.first.id, genre: tag });
      if (target.second) params.set("blend", target.second.id);
      const { videos } = await api<{ videos: string[] }>("/api/radio/search?" + params, { signal });
      if (signal.aborted) return;
      if (videos.length) {
        store.radioSlice.startMix(term, videos);
      } else {
        setFeedback("No playable mix found. Try a nearby mood or another genre.");
      }
    } catch {
      if (!signal.aborted) setFeedback("Couldn’t reach this source. Try again or change the playback source.");
    } finally {
      if (request.current === controller) {
        request.current = null;
        if (!signal.aborted) setPending(false);
      }
    }
  }

  function steer(event: KeyboardEvent<HTMLButtonElement>) {
    const steps: Record<string, Point> = { ArrowUp: { x: 0, y: -5 }, ArrowDown: { x: 0, y: 5 },
      ArrowLeft: { x: -5, y: 0 }, ArrowRight: { x: 5, y: 0 } };
    const step = steps[event.key];
    if (!step || !origin) return;
    event.preventDefault();
    adjust({ x: Math.max(0, Math.min(100, origin.x + step.x)), y: Math.max(0, Math.min(100, origin.y + step.y)) });
  }

  function adjust(next: Point) {
    setPreview(null);
    setChosen(next);
    const mix = sample(moods, next);
    setKeyFeedback(mix.second ? `${100 - mix.percent}% ${mix.first.name}, ${mix.percent}% ${mix.second.name}` : mix.first.name);
    if (!genresFor(mix).includes(genre)) setGenre("");
  }

  return <section className="view" aria-label="Browse by mood">
    <header className="listening-view-head"><div>
      <h1>How do you feel?</h1>
      <p>Tap to play. Drag between moods to blend them.</p>
    </div>
      {selection && <RoomAction icon="settings" aria-expanded={settingsOpen} aria-controls="mood-settings"
        onClick={() => setSettingsOpen(open => !open)}>Map settings</RoomAction>}
    </header>
    {mix && point ? <div className="mood-layout">
      <section className="mood-settings" id="mood-settings" aria-label="Map settings" hidden={!settingsOpen}>
        <RoomSelect label="Genre" value={currentGenre} options={[{ value: "", label: "Whole mix" },
          ...genres.map(value => ({ value, label: value }))]} onChange={value => void tune(origin, value)} />
        <RoomSelect label="Tune from" value={source} options={[{ value: "auto", label: store.radioSlice.youtubeEnabled ? "Library, then YouTube" : "Library" },
          ...(store.radioSlice.youtubeEnabled ? [{ value: "youtube", label: "YouTube" }] : []),
          { value: "catalogs", label: "Open catalogs" }]}
          onChange={value => { if (value === "auto" || value === "youtube" || value === "catalogs") {
            cancelLookup(); setSource(value);
          } }} />
        <div className="mood-adjust">
          <label>Brightness<input type="range" min="0" max="100" step="1" value={Math.round(origin.x)}
            onChange={event => adjust({ x: Number(event.target.value), y: origin.y })} /></label>
          <label>Energy<input type="range" min="0" max="100" step="1" value={100 - Math.round(origin.y)}
            onChange={event => adjust({ x: origin.x, y: 100 - Number(event.target.value) })} /></label>
        </div>
        <div className="settings-actions">
          <RoomAction primary icon={source === "catalogs" ? "search" : "play"} onClick={() => void tune(origin, currentGenre)}>
            {source === "catalogs" ? "Explore recordings" : "Play selection"}
          </RoomAction>
          {selection && !selection.second && isHost(store.settings) && <RoomAction
            aria-expanded={!!editing} onClick={() => setEditing(editing ? null : selection.first)}>Edit genres</RoomAction>}
        </div>
        {editing && <EditMood key={editing.id} mood={editing} close={() => setEditing(null)} />}
      </section>
      <div className="mood-map" role="group" aria-label="Mood map" data-mood={mix.first.id}
        onPointerDown={event => {
          if (!event.isPrimary || event.button !== 0) return;
          event.currentTarget.setPointerCapture(event.pointerId);
          setPreview(pointAt(event, moods));
        }}
        onPointerMove={event => {
          if (event.isPrimary) setPreview(pointAt(event, moods));
        }}
        onPointerUp={event => {
          if (!event.isPrimary || !event.currentTarget.hasPointerCapture(event.pointerId)) return;
          const p = pointAt(event, moods);
          setPreview(null);
          if (p.x >= 0 && p.y >= 0 && p.x <= 100 && p.y <= 100) void tune(p);
        }}
        onPointerCancel={() => setPreview(null)}
        onPointerLeave={() => setPreview(null)} onBlur={event => {
          if (!event.currentTarget.contains(event.relatedTarget)) setPreview(null);
        }}>
        <svg className="mood-lines" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
          {moods.map(m => <g key={m.id} transform={`translate(${m.x} ${m.y}) rotate(-26)`}
            data-emphasis={m.id === mix.first.id || m.id === mix.second?.id}>
            {[8, 14, 21, 29].map(r => <ellipse className="mood-contour" key={r}
              rx={r} ry={r * .69} vectorEffect="non-scaling-stroke" />)}
          </g>)}
          {[mix.first, ...(mix.second ? [mix.second] : [])].map(m => <line className="mood-connection" key={m.id}
            x1={point.x} y1={point.y} x2={m.x} y2={m.y} vectorEffect="non-scaling-stroke" />)}
        </svg>
        <button className="mood-field" type="button" aria-label="Blend moods"
          aria-description="Use the arrow keys to move, then press Enter or Space to play."
          onKeyDown={steer} onClick={event => { if (event.detail === 0) void tune(origin); }} />
        <span className="mood-axis" data-edge="top" aria-hidden="true">High energy</span>
        <span className="mood-axis" data-edge="bottom" aria-hidden="true">Low energy</span>
        <span className="mood-axis" data-edge="left" aria-hidden="true">Introspective</span>
        <span className="mood-axis" data-edge="right" aria-hidden="true">Bright</span>
        <span className="mood-pin" aria-hidden="true" style={{ left: point.x + "%", top: point.y + "%" }} />
        {moods.map(m => <button key={m.id} type="button" className="mood-node"
          data-landmark={m.id} aria-pressed={chosen?.x === m.x && chosen?.y === m.y} data-preview={m.id === mix.first.id}
          style={{ left: m.x + "%", top: m.y + "%" }} aria-description={m.description}
          onFocus={() => setPreview(m)} onPointerEnter={() => setPreview(m)} onClick={event => { if (event.detail === 0) void tune(m); }}>
          <span aria-hidden="true" />{m.name}
        </button>)}
      </div>
      <span className="visually-hidden" role="status">{keyFeedback}</span>
      <div className="mood-status" role="status">{pending ? "Tuning in…" : feedback}</div>
      {links.length > 0 && <ul className="mood-alternatives">{links.map(link =>
        <li key={link.url}><a href={link.url} target="_blank" rel="noreferrer">{link.title}</a></li>)}</ul>}
    </div> : <div role="status">{browse.tracksError || (browse.tracksLoading ? "Loading the mood map…" : "No moods available.")}
      {browse.tracksError && <RoomAction onClick={() => void browse.loadTracks(true)}>Try again</RoomAction>}</div>}
  </section>;
}
