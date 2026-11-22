import { RoomDialogContent } from "./WindowContent";
import { createContext, useContext, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Dialog, DialogBackdrop, DialogClose, DialogDescription, DialogTitle } from "@ui-sdk/react/dialog";
import { api, postJson, type PlaylistSummary } from "../api";
import { useStore } from "../store";
import { notify } from "../toasts";
import { RoomAction, RoomSelect } from "./RoomControls";
import { RoomIcon, RoomIconButton } from "./RoomIcon";
import { playlistSourceLabel, SourceBadge } from "./SourceBadge";

type Song = { title: string; artist: string; album?: string; track_id?: number; track?: string };
type PersonalList = { id: string; name: string; revision: number };
type Compose = { song?: Song; list?: PlaylistSummary; personal?: PersonalList; onSaved?: () => void };
const context = createContext<{ available: boolean; open: (request: Compose) => void }>({ available: false, open: () => {} });
export const usePlaylistComposer = () => useContext(context);

function destinationIdentity(list: PlaylistSummary) {
  const link = list.spotify_link;
  if (!link) return JSON.stringify([list.source]);
  const targets = link.mode === "multiple" ? link.targets ?? [] : [link];
  return JSON.stringify([link.mode, targets.map(item => JSON.stringify([item.mode, item.account_id, item.playlist_id])).sort()]);
}

export function AddToPlaylist({ song, iconOnly = false }: { song: Song; iconOnly?: boolean }) {
  const composer = usePlaylistComposer();
  if (!composer.available) return null;
  const open = () => composer.open({ song });
  return iconOnly ? <RoomIconButton icon="add" label="Add to playlist" onClick={open} /> :
    <RoomAction icon="add" onClick={open}>Add to playlist</RoomAction>;
}

function AddSongForm({ initial, named, close }: { initial: Compose; named: boolean; close: () => void }) {
  const store = useStore();
  const roomLists = (store.overview?.playlists ?? []).filter(list => list.can_add);
  const [personalLists, setPersonalLists] = useState<PersonalList[]>([]);
  const [listError, setListError] = useState("");
  const [listAttempt, retryLists] = useState(0);
  const [loading, setLoading] = useState(named && !initial.list && !initial.personal);
  const [choice, setChoice] = useState(initial.personal ? "mine:" + initial.personal.id : initial.list ? "room:" + initial.list.id : "");
  const [selectedRoom, setSelectedRoom] = useState(initial.list);
  const [artist, setArtist] = useState(initial.song?.artist || "");
  const [title, setTitle] = useState(initial.song?.title || "");
  const [album, setAlbum] = useState(initial.song?.album || "");
  const [track, setTrack] = useState(initial.song?.track || "");
  const [target, setTarget] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const submitting = useRef(false);
  const options = [
    ...roomLists.map(item => ({ value: "room:" + item.id, label: [item.name, playlistSourceLabel(item)].filter(Boolean).join(" · ") })),
    ...personalLists.map(item => ({ value: "mine:" + item.id, label: item.name + " · Private" })),
  ];
  const destination = choice;
  const room = selectedRoom && destination === "room:" + selectedRoom.id ? selectedRoom : undefined;
  const liveRoom = room && roomLists.find(item => item.id === room.id);
  const changed = !!room && (!liveRoom || destinationIdentity(liveRoom) !== destinationIdentity(room));
  const mine = initial.personal || personalLists.find(item => destination === "mine:" + item.id);
  const spotify = room?.spotify_link;
  const multiple = spotify?.mode === "multiple";
  const targets = multiple ? (spotify.targets || []).filter(item => item.mode === "two_way") : [];
  const chosen = multiple ? targets.find(item => JSON.stringify([item.account_id, item.playlist_id]) === target) : spotify;
  const linked = multiple || spotify?.mode === "two_way";
  useEffect(() => {
    if (!named || initial.list || initial.personal) return;
    const abort = new AbortController();
    setLoading(true);
    setListError("");
    api<{ playlists: PersonalList[] }>("/api/my/playlists", { signal: abort.signal })
      .then(data => { if (!abort.signal.aborted) setPersonalLists(data.playlists); })
      .catch(failure => { if (!abort.signal.aborted) setListError(failure instanceof Error ? failure.message : "Could not load personal playlists."); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [named, initial, listAttempt]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current || changed || !artist.trim() || !title.trim() || (!room && !mine) || (multiple && !chosen)) return;
    submitting.current = true;
    setSaving(true);
    setError("");
    const sameSong = initial.song && artist.trim() === initial.song.artist && title.trim() === initial.song.title && album.trim() === (initial.song.album || "");
    const metadata = { artist: artist.trim(), title: title.trim(), album: album.trim() };
    try {
      let confirmation = "";
      if (mine) {
        const data = sameSong && initial.song?.track_id != null ? { op: "add", track_id: initial.song.track_id } : { op: "add_metadata", ...metadata };
        const latest = await api<PersonalList>(`/api/my/playlists?id=${encodeURIComponent(mine.id)}`);
        await postJson("/api/my/playlists", { ...data, id: mine.id, revision: latest.revision });
        confirmation = `Added to ${mine.name}.`;
      } else if (room) {
        const result = await postJson<{ message: string }>("/api/playlists/add-song", {
          playlist: room.id, ...metadata,
          ...(sameSong && initial.song?.track_id != null ? { track_id: initial.song.track_id } : {}),
          ...(linked ? { account_id: chosen?.account_id, playlist_id: chosen?.playlist_id, track: track.trim() || undefined } : {}),
        });
        confirmation = result.message;
        try { await store.refreshOverview(); }
        catch { confirmation += " Refresh the library to see your changes."; }
      }
      notify(confirmation);
      initial.onSaved?.();
      close();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not add the song. Try again.");
      if (mine && !initial.personal) retryLists(n => n + 1);
    } finally {
      submitting.current = false;
      setSaving(false);
    }
  }

  return <Dialog open onOpenChange={open => { if (!open && !submitting.current) close(); }}>
    <DialogBackdrop />
    <RoomDialogContent className="playlist-composer">
      <DialogClose className="dialog-close" disabled={saving} aria-label="Close add song"><RoomIcon name="close" /></DialogClose>
      <DialogTitle>{initial.song ? "Add to playlist" : "Add a song"}</DialogTitle>
      <DialogDescription>Save the song details now. You can find an audio copy later.</DialogDescription>
      <form onSubmit={event => void submit(event)} className="composer-form">
        {initial.list || initial.personal ? <div className="composer-destination">
          <RoomIcon name="playlists" /><strong>{initial.list?.name || initial.personal?.name}</strong>
          {room && <SourceBadge playlist={room} />}
        </div> : options.length > 0 ? <RoomSelect label="Playlist" value={destination} options={[{ value: "", label: "Choose a playlist" }, ...options]} onChange={value => {
          setChoice(value); setSelectedRoom(roomLists.find(item => value === "room:" + item.id)); setTarget(""); setError("");
        }} disabled={saving} /> : null}
        {loading && <p role="status">Loading your playlists…</p>}
        {!loading && !options.length && !initial.personal && !listError && <p role="status">{named ? "Create a playlist in My playlists to save this song." : "There are no editable playlists for this device."}</p>}
        {listError && <p role="alert">{listError} <RoomAction disabled={saving} onClick={() => retryLists(n => n + 1)}>Retry</RoomAction></p>}
        <label>Song title<input name="title" value={title} disabled={saving} onChange={e => { setTitle(e.target.value); setTrack(""); }} maxLength={linked ? 250 : 512} required /></label>
        <label>Artist<input name="artist" value={artist} disabled={saving} onChange={e => { setArtist(e.target.value); setTrack(""); }} maxLength={linked ? 250 : 512} required /></label>
        <label>Album (optional)<input name="album" value={album} disabled={saving} onChange={e => { setAlbum(e.target.value); setTrack(""); }} maxLength={linked ? 250 : 512} /></label>
        {multiple && <RoomSelect label="Spotify destination" value={target} onChange={setTarget}
          options={[{ value: "", label: "Choose a linked playlist" }, ...targets.map(item => ({
            value: JSON.stringify([item.account_id, item.playlist_id]), label: `${item.name} · ${item.account_id} · …${item.playlist_id.slice(-6)}`,
          }))]} disabled={saving} />}
        {linked && <>
          <label>Spotify track link (optional)<input name="track" value={track} disabled={saving} onChange={e => setTrack(e.target.value)} placeholder="https://open.spotify.com/track/…" /></label>
          <p>This addition also updates the linked playlist on Spotify.</p>
        </>}
        {changed && <p role="alert">This playlist’s link or access changed. Close this form and choose the playlist again.</p>}
        {error && <p role="alert">{error}</p>}
        <div className="composer-actions"><RoomAction primary type="submit" icon="add" disabled={saving || changed || (!room && !mine) || (multiple && !chosen) || !title.trim() || !artist.trim()}>
          {saving ? "Adding song…" : "Add song"}
        </RoomAction></div>
      </form>
    </RoomDialogContent>
  </Dialog>;
}

export function PlaylistComposer({ named, children }: { named: boolean; children: ReactNode }) {
  const store = useStore();
  const [request, setRequest] = useState<Compose | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  function close() {
    setRequest(null);
    requestAnimationFrame(() => opener.current?.focus());
  }
  return <context.Provider value={{ available: named || !!store.overview?.playlists.some(item => item.can_add),
    open: next => { opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; setRequest(next); } }}>
    {children}
    {request && <AddSongForm initial={request} named={named} close={close} />}
  </context.Provider>;
}
