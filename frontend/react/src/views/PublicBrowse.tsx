import { FindCopySources } from "./Extensions";
import { rememberMusicSearch } from "../recentMusicSearches";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { RoomAction, RoomSelect } from "../components/RoomControls";
import { RoomIcon, type RoomIconName } from "../components/RoomIcon";
import { AddToPlaylist } from "../components/PlaylistComposer";
import { useStore } from "../store";
import { api, postJson } from "../api";

type Kind = "archive" | "peertube" | "funkwhale";
type Scope = "public" | "room";
type Source = { id: string; name: string; kind: Kind | "musicbrainz" | "apple" | "jellyfin";
  media: string[]; address: string; custom: boolean; scope?: Scope };
const sourceInfo: Record<Source["kind"], { icon: RoomIconName; description: string }> = {
  musicbrainz: { icon: "library", description: "Recordings, releases and artist credits." },
  apple: { icon: "playlists", description: "Songs and artists from Apple Music." },
  archive: { icon: "library", description: "Recordings from an Internet Archive collection." },
  peertube: { icon: "play", description: "Music videos from independent channels." },
  funkwhale: { icon: "radio", description: "Tracks shared by a Funkwhale community." },
  jellyfin: { icon: "devices", description: "Music on your connected Jellyfin server." },
};
type Configuration = { sources: Source[]; can_manage: boolean; can_import?: boolean };
type Query = { media: "music"; scope: Scope; source: string; query: string; page: number };
type Item = { title: string; creator: string; url: string; detail: string; license: string; song?: boolean; album?: string; musicbrainz_id?: string; apple_id?: string; duration_ms?: number; track_id?: number; ready?: boolean };
type Group = { source: string; name: string; items: Item[]; more: boolean; error: string };
type Search =
  | { kind: "empty" }
  | { kind: "loading" }
  | { kind: "ready"; query: Query; groups: Group[] }
  | { kind: "error"; message: string };

const catalogScopes: { id: Scope; label: string }[] = [
  { id: "public", label: "Public catalogs" },
  { id: "room", label: "Private catalog" },
];
const formats: Record<Kind, { label: string; example: string; address: string }> = {
  archive: { label: "Internet Archive collection",
    example: "https://archive.org/details/etree", address: "Collection ID or link" },
  peertube: { label: "PeerTube server",
    example: "https://video.example.org", address: "Server address" },
  funkwhale: { label: "Funkwhale server",
    example: "https://music.example.org", address: "Server address" },
};
const kinds = Object.keys(formats).filter(isKind);

function isKind(value: string): value is Kind {
  return Object.hasOwn(formats, value);
}

function message(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function ManageSources({ sources, onChange }: {
  sources: Source[];
  onChange: (value: Configuration) => void;
}) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<Kind>("archive");
  const [address, setAddress] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState("");
  const form = formats[kind];
  const local = sources.filter(source => source.custom && source.media.includes("music"));

  async function change(body: unknown, success: string) {
    setBusy(true);
    setFeedback("");
    try {
      onChange(await postJson<Configuration>("/api/discovery/sources", body));
      setFeedback(success);
      return true;
    } catch (error) {
      setFeedback(message(error, "The source could not be saved."));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (await change({ op: "add", kind, media: "music", address, name }, "Source saved.")) {
      setName("");
      setAddress("");
    }
  }

  return <section className="discovery-manage">
    <RoomAction icon="settings" aria-expanded={open} onClick={() => setOpen(!open)}>Manage discovery sources</RoomAction>
    {open && <div className="discovery-manager-body">
      <h2>Add a catalog</h2>
      <form className="discovery-source-form" onSubmit={event => void add(event)}>
        <RoomSelect label="Source type" value={kind} options={kinds.map(value => ({ value, label: formats[value].label }))}
          onChange={next => { if (isKind(next)) { setKind(next); setAddress(""); } }} />
        <label>Source name
          <input required maxLength={80} value={name} onChange={event => setName(event.target.value)} />
        </label>
        <label>{form.address}
          <input required maxLength={4096} value={address} placeholder={form.example}
            onChange={event => setAddress(event.target.value)} />
        </label>
        <RoomAction icon="add" type="submit" disabled={busy}>Add source</RoomAction>
      </form>
      {feedback && <p role="status">{feedback}</p>}
      {local.length > 0 && <ul className="discovery-source-list">
        {local.map(source => <li key={source.id}>
          <div><strong>{source.name}</strong><small>{source.address}</small></div>
          <RoomAction type="button" aria-label={`Remove ${source.name}`} disabled={busy}
            onClick={() => void change({ op: "remove", id: source.id }, "Source removed.")}>Remove</RoomAction>
        </li>)}
      </ul>}
    </div>}
  </section>;
}

function ImportSong({ item }: { item: Item }) {
  const store = useStore();
  const [saved, setSaved] = useState<{ track_id: number | null; ready: boolean } | null>(
    item.track_id ? { track_id: item.track_id, ready: !!item.ready } : null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save() {
    setBusy(true);
    setError("");
    try {
      const result = await postJson<{ track_id: number | null; ready: boolean }>("/api/discovery/import", {
        artist: item.creator, title: item.title, album: item.album || "", duration_ms: item.duration_ms,
        musicbrainz_id: item.musicbrainz_id, apple_id: item.apple_id,
      });
      setSaved(result);
      void store.refreshOverview().catch(() => {});
    } catch (failure) {
      setError(message(failure, "Could not import this song."));
    } finally {
      setBusy(false);
    }
  }
  const trackId = saved?.track_id;
  return <>
    {saved ? <>
      <small role="status">{saved.track_id ? "In library" : "Imported · waiting for library refresh"}</small>
      {trackId && (saved.ready
        ? <RoomAction icon="play" onClick={event => void store.openRoomPlayer(trackId, event.currentTarget)}>Play</RoomAction>
        : <RoomAction disabled={store.requestingIds.has(trackId)} onClick={() => void store.requestTrack(trackId)}>Find a copy</RoomAction>)}
    </> : <RoomAction icon="add" disabled={busy} onClick={() => void save()}>{busy ? "Importing…" : "Import song"}</RoomAction>}
    {error && <small role="alert">{error}</small>}
  </>;
}

function Results({ state, run, canImport }: { state: Search; canImport: boolean; run: (query: Query) => void }) {
  if (state.kind === "empty") return null;
  if (state.kind === "loading") return <p role="status">Searching catalogs…</p>;
  if (state.kind === "error") return <p role="alert">{state.message}</p>;
  const { query, groups } = state;
  const visible = groups.reduce((size, group) => size + group.items.length, 0);
  return <div className="discovery-results">
    <p className="discovery-status" role="status">
      {visible} {visible === 1 ? "result" : "results"}{query.query ? ` for “${query.query}”` : ""}
    </p>
    {groups.map(group => <section key={group.source} className="discovery-group">
      <h2>{group.name}</h2>
      {group.error ? <p>{group.error}</p> : group.items.length === 0 ?
        <p>{!query.query && group.source.startsWith("apple-") ? "Enter a title or creator to search." : "No results on this page."}</p> :
        <ul className="discovery-items">
          {group.items.map(item => <li key={item.url}>
            <div>
              <a href={item.url} target="_blank" rel="noopener noreferrer">{item.title}</a>
              {item.creator && <small>{item.creator}</small>}
            </div>
            <div className="discovery-result-actions">
              {(item.detail || item.license) && <small>{[item.detail, item.license].filter(Boolean).join(" · ")}</small>}
              {canImport && (item.musicbrainz_id || item.apple_id) && <ImportSong item={item} />}
              {item.song && <AddToPlaylist song={{ title: item.title, artist: item.creator, album: item.album }} />}
            </div>
          </li>)}
        </ul>}
    </section>)}
    {(query.page > 0 || groups.some(group => group.more)) &&
      <nav className="discovery-pages" aria-label="Discovery pages">
        <RoomAction type="button" disabled={query.page === 0}
          onClick={() => run({ ...query, page: query.page - 1 })}>Previous page</RoomAction>
        <span>Page {query.page + 1}</span>
        <RoomAction type="button" disabled={!groups.some(group => group.more)}
          onClick={() => run({ ...query, page: query.page + 1 })}>Next page</RoomAction>
      </nav>}
  </div>;
}

export function PublicBrowse() {
  const [config, setConfig] = useState<Configuration | null>(null);
  const [configError, setConfigError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [scope, setScope] = useState<Scope>("public");
  const [source, setSource] = useState("");
  const [query, setQuery] = useState("");
  const [state, setState] = useState<Search>({ kind: "empty" });
  const request = useRef<AbortController | null>(null);
  const searchInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const load = new AbortController();
    api<Configuration>("/api/discovery/sources", { signal: load.signal }).then(value => {
      if (!load.signal.aborted) setConfig(value);
    }).catch(error => {
      if (!load.signal.aborted) setConfigError(message(error, "Discovery sources are unavailable."));
    });
    return () => load.abort();
  }, [attempt]);
  useEffect(() => () => request.current?.abort(), []);

  function reset() {
    request.current?.abort();
    setState({ kind: "empty" });
  }

  function applyConfig(value: Configuration) {
    setConfig(value);
    if (source && !value.sources.some(item => item.id === source)) {
      setSource("");
      reset();
    }
  }

  async function run(value: Query) {
    request.current?.abort();
    const current = new AbortController();
    request.current = current;
    setState({ kind: "loading" });
    try {
      const result = await postJson<{ groups: Group[] }>("/api/discovery/search", value, current.signal);
      if (!current.signal.aborted) setState({ kind: "ready", query: value, groups: result.groups });
    } catch (error) {
      if (!current.signal.aborted)
        setState({ kind: "error", message: message(error, "Discovery is unavailable. Try again.") });
    }
  }

  const available = config?.sources.filter(item => item.media.includes("music") && (item.scope ?? "public") === scope) ?? [];
  const hasRecordings = available.some(item => item.id === "musicbrainz");
  const selected = source || (hasRecordings ? "recordings" : "all");
  return <section className="view discovery-view">
    <header className="listening-view-head">
      <div>
        <h1>Browse catalogs</h1>
        <p>Search by artist or song. Import recordings into your library, then choose which ones need an audio copy.</p>
      </div>
    </header>
    <FindCopySources />
    {configError ? <>
      <p role="alert">{configError}</p>
      <RoomAction onClick={() => { setConfigError(""); setAttempt(attempt + 1); }}>Retry</RoomAction>
    </> : !config ? <p role="status">Loading sources…</p> : <>
      <div className="catalog-search-panel">
        <div className="discovery-controls">
          <div className="discovery-scope" role="group" aria-label="Catalog location">
            {catalogScopes.map(option => <RoomAction key={option.id} icon={option.id === "public" ? "catalogs" : "library"} aria-pressed={option.id === scope}
              onClick={() => {
                if (option.id === scope) return;
                setScope(option.id);
                setSource("");
                reset();
              }}>{option.label}</RoomAction>)}
          </div>
        </div>
        {available.length ? <form className="discovery-search" onSubmit={event => {
          event.preventDefault();
          if (scope === "public") rememberMusicSearch(query);
          void run({ media: "music", scope, source: selected === "all" ? "" : selected, query: query.trim(), page: 0 });
        }}>
          <label>{scope === "room" ? "Search private catalog" : "Search public catalogs"}
            <input ref={searchInput} type="search" value={query} maxLength={160} onChange={event => setQuery(event.target.value)}
              placeholder="Artist, song title, or artist – title" />
          </label>
          <RoomSelect label="Search source" value={selected} options={[
            ...(hasRecordings ? [{ value: "recordings", label: "Song catalogs · MusicBrainz + Apple Music" }] : []),
            { value: hasRecordings ? "all-public" : "all", label: available.length === 1 ? "All music sources" : `All ${available.length} music sources` }, ...available.map(item => ({ value: item.id, label: item.name })),
          ]} onChange={next => { setSource(next === "all" ? "" : next); reset(); }} />
          <RoomAction primary icon="search" type="submit" disabled={state.kind === "loading" || (["recordings", "musicbrainz", "apple-music"].includes(selected) && !query.trim())}>
            {query.trim() || ["recordings", "musicbrainz", "apple-music"].includes(selected) ? "Search" : "Browse"}
          </RoomAction>
        </form> : <div className="discovery-no-source">
          <h2>No private catalog connected</h2>
          <p>{config.can_manage ? "Connect a catalog on the room server." : "Ask the room owner to connect a catalog."}
             Use Jellyfin for music.</p>
        </div>}
        {!!available.length && <p className="discovery-note">
          {scope === "public" ? "Search terms go to the selected catalogs." : "Search music on the connected server."}
          {scope === "public" && config.can_import && " Imported songs appear in Library → Catalog imports."}
        </p>}
      </div>
      {state.kind === "empty" && available.length > 0 && <section className="catalog-directory">
        <h2>{scope === "room" ? "Connected libraries" : "Available catalogs"}</h2>
        <ul>{available.map(item => <li key={item.id}>
          <button type="button" aria-label={`Search ${item.name}`} onClick={() => {
            setSource(item.id); searchInput.current?.focus();
          }}>
            <span className="catalog-source-icon"><RoomIcon name={sourceInfo[item.kind].icon} /></span>
            <span><strong>{item.name}</strong><small>{sourceInfo[item.kind].description}</small></span>
          </button>
        </li>)}</ul>
      </section>}
      <Results state={state} canImport={!!config.can_import} run={value => void run(value)} />
      {config.can_manage && scope === "public" && <ManageSources sources={config.sources} onChange={applyConfig} />}
    </>}
  </section>;
}
