import { useEffect, useState } from "react";
import { api, postJson, readable, type PlaylistSummary, type TracksPage } from "../../api";
import { useStore } from "../../store";
import { RoomAction, RoomConfirm, RoomSelect } from "../../components/RoomControls";
import { RoomIcon } from "../../components/RoomIcon";
import { SourceBadge } from "../../components/SourceBadge";
import { errorText } from "./shared";

interface Playlist {
  id: string;
  key: string;
  name: string;
  linked: boolean;
  owned: boolean;
  available: boolean;
  mode: NonNullable<PlaylistSummary["spotify_link"]>["mode"];
  needs_write_permission?: boolean;
}
interface Account {
  account_id: string;
  connected: boolean;
  running: boolean;
  can_create: boolean;
  playlists: Playlist[];
}
interface Member { uri: string; artist: string; title: string }

type PlaylistOperation = "create" | "link" | "unlink" | "rename" | "add" | "remove";

type Resource<T> = { loading: true; data?: T; error?: never }
  | { loading: false; data: T; error?: never }
  | { loading: false; data?: never; error: string };

function useResource<T>(path: string, revision: string): Resource<T> | undefined {
  const [result, setResult] = useState<{ path: string; revision: string; resource: Resource<T> }>();
  useEffect(() => {
    if (!path) return;
    const abort = new AbortController();
    void api<T>(path, { signal: abort.signal }).then(data => {
      if (!abort.signal.aborted) setResult({ path, revision, resource: { data, loading: false } });
    }).catch(error => {
      if (!abort.signal.aborted) setResult({
        path,
        revision,
        resource: { error: errorText(error, "Could not load Spotify data"), loading: false },
      });
    });
    return () => abort.abort();
  }, [path, revision]);

  if (!path) return;
  if (result?.path === path && result.revision === revision) return result.resource;
  return { loading: true, data: result?.path === path ? result.resource.data : undefined };
}

interface EditProps {
  list: Playlist;
  account: Account;
  busy: boolean;
  revision: string;
  act: (op: PlaylistOperation, fields?: Record<string, unknown>) => Promise<boolean>;
}

function PlaylistEditor({ list, account, busy, revision, act }: EditProps) {
  const [draftName, setDraftName] = useState<string>();
  const name = draftName ?? list.name;
  const canRename = !!draftName?.trim() && draftName.trim() !== list.name.trim();
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

  return (
    <div data-spotify-editor>
      <div data-settings-list-head>
        <div data-settings-list-content>
          <h4>{list.name}</h4>
          <SourceBadge playlist={{ spotify_link: { mode: list.mode } }} />
        </div>
        <RoomAction
          disabled={busy || !account.connected || (!list.available && !list.linked)}
          onClick={() => act(list.linked ? "unlink" : "link")}
        >
          {list.linked ? "Unlink playlist" : "Link playlist"}
        </RoomAction>
      </div>
      {list.mode === "read_only" && <p>{list.needs_write_permission
        ? "Reconnect your Spotify account to grant editing access for this playlist."
        : "You can follow this playlist in Listening. This account has read-only access."}</p>}
      {list.mode === "disconnected" && <p>Reconnect the Spotify account to resume syncing.</p>}
      {!list.available && <p>This playlist is no longer available on the connected account.</p>}

      {editable && (
        <details data-settings-list-disclosure>
          <summary><span>Edit on Spotify</span><RoomIcon name="chevronDown" /></summary>
          <div data-settings-list-content>
            {!liked && list.owned && (
              <form data-settings-list-form onSubmit={async event => {
                event.preventDefault();
                if (canRename && await act("rename", { name })) {
                  setDraftName(previous => previous === name ? undefined : previous);
                }
              }}>
                <label data-settings-list-field>
                  Playlist name
                  <input value={name} maxLength={100} onChange={event => setDraftName(event.target.value)} />
                </label>
                <RoomAction type="submit" disabled={busy || !canRename}>
                  Rename on Spotify
                </RoomAction>
              </form>
            )}
            <form data-settings-list-form onSubmit={event => {
              event.preventDefault();
              setSearch(draft.trim());
            }}>
              <label data-settings-list-field>
                Find a catalog song
                <input type="search" value={draft} onChange={event => { setDraft(event.target.value); setSearch(""); }} />
              </label>
              <RoomAction type="submit" disabled={!draft.trim() || busy}>Search catalog</RoomAction>
            </form>
            {matches?.loading && <p role="status">Searching catalog…</p>}
            {matches?.error && <p role="alert">{matches.error}</p>}
            {matches?.data && (
              matches.data.tracks.length ? (
                <ul data-settings-list-rows>
                  {matches.data.tracks.map(song => (
                    <li data-settings-list-row key={song.id}>
                      <div><strong>{song.title}</strong><span>{song.artist}</span></div>
                      <RoomAction disabled={busy} onClick={() => act("add", { track: song.spotify_url })}>Add song</RoomAction>
                    </li>
                  ))}
                </ul>
              ) : <p role="status">No matching catalog songs with Spotify IDs.</p>
            )}
            <form data-settings-list-form onSubmit={async event => {
              event.preventDefault();
              if (await act("add", { track })) setTrack("");
            }}>
              <label data-settings-list-field>
                Spotify track link
                <input value={track} onChange={event => setTrack(event.target.value)} />
              </label>
              <RoomAction type="submit" disabled={busy || !track.trim()}>Add from link</RoomAction>
            </form>
          </div>
        </details>
      )}

      <div data-settings-list-content>
        <label data-settings-list-field>
          Find in this playlist
          <input type="search" value={query} onChange={event => { setQuery(event.target.value); setOffset(0); }} />
        </label>
        {members?.loading && !members.data && <p role="status">Loading songs…</p>}
        {members?.error && <p role="alert">{members.error}</p>}
        {members?.data && (
          <div data-settings-list-content aria-busy={members.loading}>
            <p role="status">
              {members.data.total
                ? `${readable(members.data.total)} ${members.data.total === 1 ? "song" : "songs"}`
                : query.trim() ? "No songs match your search." : "This playlist is empty."}
            </p>
            {members.data.tracks.length > 0 && (
              <ul data-settings-list-rows>
                {members.data.tracks.map((song, index) => (
                  <li data-settings-list-row key={`${song.uri}-${index}`}>
                    <div><strong>{song.title}</strong><span>{song.artist}</span></div>
                    {editable && <RoomAction disabled={busy} onClick={() => setRemoving(song)}>Remove</RoomAction>}
                  </li>
                ))}
              </ul>
            )}
            {(offset > 0 || members.data.total > 40) && (
              <div data-settings-list-actions>
                <RoomAction disabled={busy || offset === 0} onClick={() => setOffset(Math.max(0, offset - 40))}>Previous</RoomAction>
                <RoomAction disabled={busy || offset + 40 >= members.data.total} onClick={() => setOffset(offset + 40)}>Next</RoomAction>
              </div>
            )}
          </div>
        )}
      </div>
      <RoomConfirm
        open={!!removing}
        title="Remove from Spotify?"
        description={`Remove every entry for “${removing?.title || ""}” from ${list.name} on Spotify.`}
        confirmLabel="Remove song"
        onClose={() => setRemoving(null)}
        onConfirm={() => { if (removing && editable) void act("remove", { track: removing.uri }); }}
      />
    </div>
  );
}

export function SpotifyPlaylists({ syncTime, running }: { syncTime: number | null; running: boolean }) {
  const store = useStore();
  const [revision, setRevision] = useState(0);
  const [choice, setChoice] = useState(store.spotifyDestination);
  const [createName, setCreateName] = useState("");
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<{ message: string; error: boolean }>();
  const version = `${revision}-${syncTime}-${running}`;
  const response = useResource<Account>("/api/spotify/playlists", version);
  const account = response?.data;
  const list = account && choice?.account === account.account_id
    ? account.playlists.find(playlist => playlist.id === choice.playlist) : undefined;
  const busy = saving || running || !!account?.running || !!response?.loading;

  const act: EditProps["act"] = async (op, fields = {}) => {
    if (busy || !account) return false;
    setSaving(true);
    try {
      const result = await postJson<{ playlist?: string; message: string }>("/api/spotify/playlists", {
        op, playlist: op === "create" ? "" : list?.id, account_id: account.account_id, ...fields,
      });
      if (result.playlist) setChoice({ playlist: result.playlist, account: account.account_id });
      setFeedback({ message: result.message, error: false });
      setRevision(n => n + 1);
      void store.refreshOverview();
      return true;
    } catch (error) {
      setFeedback({ message: errorText(error, "Could not update Spotify"), error: true });
      return false;
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="settings-card" data-settings-lists="spotify" aria-labelledby="spotify-playlists-heading">
      <div data-settings-list-head>
        <div>
          <h3 id="spotify-playlists-heading">Manage playlists</h3>
          <p>Choose a Spotify playlist to manage its link and songs.</p>
        </div>
        <RoomAction icon="refresh" disabled={saving || !!response?.loading} onClick={() => setRevision(n => n + 1)}>
          Refresh list
        </RoomAction>
      </div>
      {response?.loading && !account && <p data-settings-list-message role="status">Loading Spotify playlists…</p>}
      {response?.error && <p data-settings-list-message role="alert">{response.error}</p>}
      {feedback && (
        <p data-settings-list-message role={feedback.error ? "alert" : "status"}>{feedback.message}</p>
      )}
      {account && (
        <>
          {account.playlists.length > 0 ? (
            <RoomSelect label="Spotify playlist" value={list?.id || ""} disabled={saving}
              onChange={id => setChoice({ playlist: id, account: account.account_id })}
              options={[
                { value: "", label: "Choose a playlist" },
                ...account.playlists.map(playlist => ({
                  value: playlist.id,
                  label: playlist.name + (playlist.linked ? " · Linked" : " · Unlinked"),
                })),
              ]}
            />
          ) : <p data-settings-list-message>No Spotify playlists available.</p>}
          {account.connected && (
            <div data-settings-list-content>
              <form data-settings-list-form onSubmit={async event => {
                event.preventDefault();
                if (await act("create", { name: createName })) setCreateName("");
              }}>
                <label data-settings-list-field>
                  New playlist name
                  <input value={createName} maxLength={100} onChange={event => setCreateName(event.target.value)} />
                </label>
                <RoomAction type="submit" disabled={busy || !account.can_create || !createName.trim()}>
                  Create private playlist
                </RoomAction>
              </form>
              {!account.can_create && <p>Reconnect Spotify with permission to create private playlists.</p>}
            </div>
          )}
          {list && (
            <PlaylistEditor
              key={JSON.stringify([account.account_id, list.id])}
              account={account} list={list} busy={busy} revision={version} act={act}
            />
          )}
        </>
      )}
    </section>
  );
}
