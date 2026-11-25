import { useEffect, useState, type FormEvent } from "react";
import { api, postJson, type TracksPage } from "../api";
import { RoomIcon } from "../components/RoomIcon";
import { usePlaylistComposer } from "../components/PlaylistComposer";
import { PlaylistCards } from "../components/PlaylistCards";
import { RoomAction, RoomConfirm } from "../components/RoomControls";
import { StateCard } from "../components/StateCard";
import { useStore } from "../store";

type Playlist = { id: string; name: string; revision: number };
type Entry = { id: number | null; title: string; artist: string; ready: boolean };
type PlaylistDetail = Playlist & { tracks: (Entry | null)[] };
type Edit = { op: "rename"; name: string } | { op: "add"; track_id: number } |
  { op: "move"; position: number; target: number } | { op: "remove"; position: number };
const endpoint = "/api/my/playlists";

function message(error: unknown) {
  return error instanceof Error ? error.message : "The playlist could not be loaded.";
}

function RecordSleeve({ slot = 0 }: { slot?: number }) {
  return <span className="record-sleeve" data-color={slot % 4} aria-hidden="true"><i /></span>;
}

function Editor({ id, owner, close }: { id: string; owner: boolean; close: () => void }) {
  const store = useStore();
  const composer = usePlaylistComposer();
  const [playlist, setPlaylist] = useState<PlaylistDetail | null>(null);
  const [error, setError] = useState("");
  const [working, setWorking] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const [matches, setMatches] = useState<TracksPage["tracks"] | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    api<PlaylistDetail>(`${endpoint}?id=${encodeURIComponent(id)}`, { signal: controller.signal })
      .then(setPlaylist).catch((error: unknown) => {
        if (!controller.signal.aborted) setError(message(error));
      }).finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [id, attempt]);

  function reload() {
    setError("");
    setLoading(true);
    setAttempt((previous) => previous + 1);
  }

  async function edit(action: Edit | { op: "delete" }) {
    if (!playlist || working) return;
    setError("");
    setWorking(true);
    try {
      if (action.op === "delete") {
        await postJson(endpoint, { ...action, id, revision: playlist.revision });
        close();
      } else {
        const updated = await postJson<PlaylistDetail>(endpoint, { ...action, id, revision: playlist.revision });
        setPlaylist(updated);
      }
    } catch (failure) {
      setError(message(failure));
    } finally {
      setWorking(false);
    }
  }

  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = String(new FormData(event.currentTarget).get("query") ?? "").trim();
    if (!query || working) return;
    setWorking(true);
    setError("");
    try {
      const page = await api<TracksPage>(`/api/tracks?${new URLSearchParams({ q: query, limit: "12" })}`);
      setMatches(page.tracks);
    } catch (failure) {
      setError(message(failure));
    } finally {
      setWorking(false);
    }
  }

  if (loading) return <StateCard kind="loading" title="Opening playlist" body="Loading your saved tracks." />;
  return <>
    <button type="button" className="library-back icon-control" title="Back to my playlists" aria-label="Back to my playlists" onClick={close}><RoomIcon name="back" /></button>
    {error && <StateCard kind="error" title="Playlist update unavailable" body={error}
      action={<RoomAction type="button" onClick={reload}>Reload playlist</RoomAction>} />}
    {playlist && <>
      <header className="private-playlist-detail">
        <RecordSleeve />
        <div>
          <h1>{playlist.name}</h1>
          <p>{playlist.tracks.length} {playlist.tracks.length === 1 ? "track" : "tracks"} in this private playlist</p>
          <RoomAction type="button" primary disabled={!playlist.tracks.some(entry => entry?.ready)}
            onClick={() => {
              if (owner) store.sessionSlice.selectQueue(true);
              void store.sendSession("play_available", { playlist: `private:${id}` });
            }}>Play privately</RoomAction>
        </div>
      </header>
      <div className="private-playlist-workspace">
        <section className="private-playlist-tracks">
          <h2>Tracks</h2>
          {playlist.tracks.length ? <ol className="private-track-list">
            {playlist.tracks.map((entry, position) => <li key={position}>
              <div className="private-track-title">
                <strong>{entry?.title || "Unavailable song"}</strong>
                {entry && <small>{entry.artist}{entry.ready ? "" : " (awaiting audio)"}</small>}
              </div>
              <div className="settings-actions">
                <RoomAction type="button" aria-label={`Move track ${position + 1} up`}
                  disabled={working || position === 0}
                  onClick={() => void edit({ op: "move", position, target: position - 1 })}><RoomIcon name="up" /></RoomAction>
                <RoomAction type="button" aria-label={`Move track ${position + 1} down`}
                  disabled={working || position === playlist.tracks.length - 1}
                  onClick={() => void edit({ op: "move", position, target: position + 1 })}><RoomIcon name="down" /></RoomAction>
                <RoomAction type="button" aria-label={`Remove track ${position + 1}`}
                  disabled={working} onClick={() => void edit({ op: "remove", position })}>Remove</RoomAction>
              </div>
            </li>)}
          </ol> : <p>Add a song by title and artist or choose one from the library.</p>}
        </section>
        <section className="private-playlist-add">
          <h2>Add songs</h2>
          <RoomAction icon="add" onClick={() => composer.open({ personal: playlist, onSaved: reload })}>Add by title and artist</RoomAction>
          <form className="private-playlist-form" onSubmit={(event) => void search(event)}>
            <label>Search songs to add <input name="query" type="search" maxLength={120} required /></label>
            <RoomAction type="submit" disabled={working}>Search</RoomAction>
          </form>
          {matches && (matches.length ? <ul className="private-track-list">
            {matches.map(track => <li key={track.id}>
              <div className="private-track-title"><strong>{track.title}</strong><small>{track.artist}</small></div>
              <RoomAction type="button" disabled={working} aria-label={`Add ${track.title}`}
                onClick={() => void edit({ op: "add", track_id: track.id })}>Add</RoomAction>
            </li>)}
          </ul> : <p>No songs match that search.</p>)}
        </section>
      </div>
      <details className="private-playlist-settings">
        <summary>Playlist settings</summary>
        <form key={playlist.name} className="private-playlist-form" onSubmit={(event) => {
          event.preventDefault();
          void edit({ op: "rename", name: String(new FormData(event.currentTarget).get("name") ?? "") });
        }}>
          <label>Playlist name <input name="name" maxLength={100} required defaultValue={playlist.name} /></label>
          <RoomAction type="submit" disabled={working}>Rename</RoomAction>
        </form>
        <RoomAction type="button" disabled={working}
          onClick={() => setConfirmDelete(true)}>Delete playlist</RoomAction>
        <RoomConfirm open={confirmDelete} title="Delete playlist?" description={`Remove “${playlist.name}” from your playlists.`}
          confirmLabel="Delete playlist" onClose={() => setConfirmDelete(false)} onConfirm={() => void edit({ op: "delete" })} />
      </details>
    </>}
  </>;
}

export function PrivatePlaylists({ owner }: { owner: boolean }) {
  const store = useStore();
  const roomPlaylists = store.overview?.playlists ?? [];
  const [items, setItems] = useState<(Playlist & { count: number })[] | null>(null);
  const [selected, setSelected] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (selected) return;
    const controller = new AbortController();
    api<{ playlists: (Playlist & { count: number })[] }>(endpoint, { signal: controller.signal })
      .then((data) => setItems(data.playlists)).catch((error: unknown) => {
        if (!controller.signal.aborted) setError(message(error));
      });
    return () => controller.abort();
  }, [selected, attempt]);

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (working) return;
    const name = String(new FormData(event.currentTarget).get("name") ?? "");
    setWorking(true);
    setError("");
    try {
      const playlist = await postJson<PlaylistDetail>(endpoint, { op: "create", name });
      setSelected(playlist.id);
    } catch (failure) {
      setError(message(failure));
    } finally {
      setWorking(false);
    }
  }

  return <section className="view private-playlist-view">
    {selected ? <Editor key={selected} id={selected} owner={owner} close={() => {
      setItems(null);
      setSelected("");
    }} /> : <>
      <header className="listening-view-head">
        <div><h1>My playlists</h1></div>
      </header>
      <div className="private-playlist-workspace">
        <div>
          <h2>Private playlists</h2>
          {error ? <StateCard kind="error" title="Couldn’t open playlists" body={error}
            action={<RoomAction type="button" onClick={() => {
              setError("");
              setAttempt(previous => previous + 1);
            }}>Retry</RoomAction>} /> : items === null ? <p role="status">Loading playlists…</p> :
            items.length ? <ul className="private-playlist-shelf">
              {items.map((playlist, slot) => <li key={playlist.id}>
                <button type="button" className="private-playlist-tile" onClick={() => setSelected(playlist.id)}>
                  <RecordSleeve slot={slot} />
                  <strong>{playlist.name}</strong>
                  <small>{playlist.count} {playlist.count === 1 ? "track" : "tracks"}</small>
                </button>
              </li>)}
            </ul> : <div className="playlist-empty">
              <RecordSleeve />
              <div><h2>Make your first playlist</h2><p>Give it a name, then add songs from the room library.</p></div>
            </div>}
        </div>
        <form className="private-playlist-form private-playlist-create" onSubmit={(event) => void create(event)}>
          <h2><RoomIcon name="playlists" />New playlist</h2>
          <label>New playlist name <input name="name" maxLength={100} required /></label>
          <RoomAction type="submit" primary disabled={working}>Create playlist</RoomAction>
        </form>
      </div>
      {roomPlaylists.length > 0 && <section className="playlist-shelf" aria-labelledby="room-playlists-heading">
        <div className="section-header"><h2 id="room-playlists-heading">Room playlists</h2></div>
        <PlaylistCards playlists={roomPlaylists} />
      </section>}
    </>}
  </section>;
}
