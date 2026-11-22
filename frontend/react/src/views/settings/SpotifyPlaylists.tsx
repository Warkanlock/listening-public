import { useEffect, useState } from "react";
import { api, postJson, type TracksPage } from "../../api";
import { useStore } from "../../store";
import { RoomAction, RoomConfirm, RoomSelect } from "../../components/RoomControls";
import { SourceBadge } from "../../components/SourceBadge";
import { errorText } from "./shared";

interface Playlist {
  id: string; key: string; name: string; linked: boolean; owned: boolean; available: boolean;
  mode: NonNullable<import("../../api").PlaylistSummary["spotify_link"]>["mode"];
}
interface Account {
  account_id: string; connected: boolean; running: boolean; can_create: boolean;
  playlists: Playlist[];
}
interface Member { uri: string; artist: string; title: string }

function useResource<T>(path: string, revision: string) {
  const [result, setResult] = useState<{ key: string; data?: T; error?: string }>();
  const key = JSON.stringify([path, revision]);
  useEffect(() => {
    if (!path) return;
    const abort = new AbortController();
    void api<T>(path, { signal: abort.signal }).then(data => {
      if (!abort.signal.aborted) setResult({ key, data });
    }).catch(e => {
      if (!abort.signal.aborted) setResult({ key, error: errorText(e, "Could not load playlists") });
    });
    return () => abort.abort();
  }, [path, key]);
  return result?.key === key ? result : undefined;
}

interface EditProps {
  list: Playlist; account: Account; busy: boolean; revision: string;
  act: (op: string, fields?: Record<string, unknown>) => Promise<boolean>;
}
function PlaylistEditor({ list, account, busy, revision, act }: EditProps) {
  const [name, setName] = useState(list.name);
  const [track, setTrack] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [draft, setDraft] = useState("");
  const [search, setSearch] = useState("");
  const [removing, setRemoving] = useState<Member | null>(null);
  const liked = list.id === "liked";
  const params = new URLSearchParams({ account_id: account.account_id, playlist: list.id, q: query, offset: String(offset) });
  const members = useResource<{ tracks: Member[]; total: number }>(`/api/spotify/playlists?${params}`, revision);
  const matches = useResource<TracksPage>(search ? `/api/tracks?${new URLSearchParams({ q: search, limit: "40", spotify: "1" })}` : "", "");
  const editable = list.mode === "two_way";
  return <>
    <div className="settings-actions">
      <RoomAction type="button" disabled={busy || !account.connected || (!list.available && !list.linked)}
        onClick={() => void act(list.linked ? "unlink" : "link")}>{list.linked ? "Unlink playlist" : "Link playlist"}</RoomAction>
      <SourceBadge playlist={{ spotify_link: { mode: list.mode } }} />
    </div>
    {list.mode === "read_only" && <p>You can follow this playlist in Listening. This account has read-only access.</p>}
    {list.mode === "disconnected" && <p>Reconnect the Spotify account to resume syncing.</p>}
    {!list.available && <p>This playlist is no longer available on the connected account.</p>}
    {editable && <>
      {!liked && list.owned && <form className="settings-actions" onSubmit={e => { e.preventDefault(); void act("rename", { name }); }}>
        <label>Playlist name <input value={name} maxLength={100} onChange={e => setName(e.target.value)} /></label>
        <RoomAction type="submit" disabled={busy || !name.trim() || name.trim() === list.name}>Rename on Spotify</RoomAction>
      </form>}
      <form className="settings-actions" onSubmit={e => { e.preventDefault(); setSearch(draft.trim()); }}>
        <label>Find a catalog song <input value={draft} onChange={e => { setDraft(e.target.value); setSearch(""); }} /></label>
        <RoomAction type="submit" disabled={!draft.trim() || busy}>Search catalog</RoomAction>
      </form>
      {matches?.error && <p role="alert">{matches.error}</p>}
      {matches?.data && <div>
        {!matches.data.tracks.length && <p>No matching catalog songs with Spotify IDs.</p>}
        {matches.data.tracks.map(song => <div className="source-item" key={song.id}>
          <span>{song.title} · {song.artist}</span><RoomAction type="button" disabled={busy}
            onClick={() => void act("add", { track: song.spotify_url })}>Add song</RoomAction>
        </div>)}
      </div>}
      <form className="settings-actions" onSubmit={async e => { e.preventDefault(); if (await act("add", { track })) setTrack(""); }}>
        <label>Spotify track link <input value={track} onChange={e => setTrack(e.target.value)} /></label>
        <RoomAction type="submit" disabled={busy || !track.trim()}>Add from link</RoomAction>
      </form>
    </>}
    <label>Find in this playlist <input value={query} onChange={e => { setQuery(e.target.value); setOffset(0); }} /></label>
    {members?.error && <p role="alert">{members.error}</p>}
    {members?.data && <div>
      <p>{members.data.total} {members.data.total === 1 ? "song" : "songs"}</p>
      {members.data.tracks.map((song, index) => <div className="source-item" key={`${song.uri}-${index}`}>
        <span>{song.title} · {song.artist}</span>
        {editable && <RoomAction type="button" disabled={busy}
          onClick={() => setRemoving(song)}>Remove</RoomAction>}
      </div>)}
      {(offset > 0 || members.data.total > 40) && <div className="settings-actions">
        <RoomAction disabled={busy || offset === 0} onClick={() => setOffset(Math.max(0, offset - 40))}>Previous</RoomAction>
        <RoomAction disabled={busy || offset + 40 >= members.data.total} onClick={() => setOffset(offset + 40)}>Next</RoomAction>
      </div>}
    </div>}
    <RoomConfirm open={!!removing} title="Remove from Spotify?"
      description={`Remove every entry for “${removing?.title || ""}” from ${list.name} on Spotify.`}
      confirmLabel="Remove song" onClose={() => setRemoving(null)}
      onConfirm={() => { if (removing && editable) void act("remove", { track: removing.uri }); }} />
  </>;
}

export function SpotifyPlaylists({ syncTime, running }: { syncTime: number | null; running: boolean }) {
  const store = useStore();
  const [revision, setRevision] = useState(0);
  const [choice, setChoice] = useState(store.spotifyDestination);
  const [createName, setCreateName] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const version = `${revision}-${syncTime}-${running}`;
  const response = useResource<Account>("/api/spotify/playlists", version);
  const account = response?.data;
  const list = account && choice?.account === account.account_id
    ? account.playlists.find(playlist => playlist.id === choice.playlist) : undefined;
  const busy = saving || running || !!account?.running;
  const act = async (op: string, fields: Record<string, unknown> = {}) => {
    if (busy || !account) return false;
    setSaving(true);
    try {
      const result = await postJson<{ playlist?: string; message: string }>("/api/spotify/playlists", {
        op, playlist: op === "create" ? "" : list?.id, account_id: account.account_id, ...fields,
      });
      if (result.playlist) setChoice({ playlist: result.playlist, account: account.account_id });
      setMessage(result.message);
      setRevision(n => n + 1);
      void store.refreshOverview();
      return true;
    } catch (e) {
      setMessage(errorText(e, "Could not update Spotify"));
      return false;
    } finally { setSaving(false); }
  };
  return <section className="settings-card spotify-playlists">
    <h3>Manage playlists</h3>
    {response?.error && <p role="alert">{response.error}</p>}
    {message && <p role="status">{message}</p>}
    <RoomAction type="button" disabled={saving} onClick={() => setRevision(n => n + 1)}>Refresh list</RoomAction>
    {account?.connected && <form className="settings-actions" onSubmit={async e => {
      e.preventDefault(); if (await act("create", { name: createName })) setCreateName("");
    }}>
      <label>New playlist name <input value={createName} maxLength={100} onChange={e => setCreateName(e.target.value)} /></label>
      <RoomAction type="submit" primary disabled={busy || !account.can_create || !createName.trim()}>Create private playlist</RoomAction>
      {!account.can_create && <small>Reconnect Spotify with permission to create private playlists.</small>}
    </form>}
    {account && account.playlists.length > 0 && <RoomSelect label="Spotify playlist" value={list?.id || ""} disabled={saving}
      onChange={id => setChoice({ playlist: id, account: account.account_id })}
      options={[{ value: "", label: "Choose a playlist" }, ...account.playlists.map(p => ({ value: p.id, label: p.name + (p.linked ? " · Linked" : " · Unlinked") }))]} />}
    {list && account && <>
      <PlaylistEditor key={account.account_id + list.id} account={account} list={list} busy={busy} revision={version} act={act} />
    </>}
  </section>;
}
