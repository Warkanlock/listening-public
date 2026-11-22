import { RoomDialogContent } from "./WindowContent";
import { useEffect, useState } from "react";
import {
  Dialog,
  DialogBackdrop,
  DialogClose,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "@ui-sdk/react/dialog";
import { api } from "../api";
import { RoomIcon } from "./RoomIcon";
import { StateCard } from "./StateCard";

type SharedTrack = { title: string; artist: string; album: string; musicbrainz_url: string | null };
type Preview = { name: string; tracks: SharedTrack[] };
type PreviewState = { kind: "loading" } | { kind: "error" } | { kind: "ready"; preview: Preview };

function hasTrack(value: unknown): value is SharedTrack {
  return typeof value === "object" && value !== null &&
    "title" in value && typeof value.title === "string" &&
    "artist" in value && typeof value.artist === "string" &&
    "album" in value && typeof value.album === "string" &&
    "musicbrainz_url" in value && (value.musicbrainz_url === null ||
      (typeof value.musicbrainz_url === "string" &&
        /^https:\/\/musicbrainz\.org\/recording\/[a-f0-9-]{36}$/.test(value.musicbrainz_url)));
}

function parsePreview(value: unknown): Preview {
  if (typeof value === "object" && value !== null && "name" in value && typeof value.name === "string" &&
      "tracks" in value && Array.isArray(value.tracks) && value.tracks.every(hasTrack)) {
    return { name: value.name, tracks: value.tracks };
  }
  throw new Error("The playlist preview is unavailable.");
}

function PreviewContent({ endpoint }: { endpoint: string }) {
  const [state, setState] = useState<PreviewState>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [showText, setShowText] = useState(false);
  useEffect(() => {
    const request = new AbortController();
    api<unknown>(endpoint, { signal: request.signal }).then(parsePreview).then((preview) => {
      if (!request.signal.aborted) setState({ kind: "ready", preview });
    }).catch(() => {
      if (!request.signal.aborted) setState({ kind: "error" });
    });
    return () => request.abort();
  }, [endpoint, attempt]);

  if (state.kind === "loading") {
    return <StateCard compact kind="loading" title="Preparing the playlist" body="Reading tracks in playlist order." />;
  }
  if (state.kind === "error") {
    return <StateCard compact kind="error" title="Playlist preview unavailable" body="Check your room connection and try again."
      action={<button type="button" className="quiet-button" onClick={() => {
        setState({ kind: "loading" });
        setAttempt(attempt + 1);
      }}>Retry</button>} />;
  }
  const post = [state.preview.name, ...state.preview.tracks.map(
    (track) => `${track.artist} — ${track.title}`,
  )].join("\n");
  return <>
    <h3>{state.preview.name}</h3>
    {state.preview.tracks.length ? (
      <ol className="playlist-share-tracks">
        {state.preview.tracks.map((track, index) => (
          <li key={index}>
            {track.musicbrainz_url ? <a href={track.musicbrainz_url} target="_blank" rel="noreferrer">{track.title}</a> : <strong>{track.title}</strong>}
            {" · "}{track.artist}{track.album ? ` · ${track.album}` : ""}
          </li>
        ))}
      </ol>
    ) : <p>This playlist is empty.</p>}
    <a className="primary-action" href={`${endpoint}&download=1`} download>Download XSPF</a>
    <button type="button" className="quiet-button" aria-expanded={showText} onClick={() => setShowText(!showText)}>Text for a post</button>
    {showText && <textarea aria-label="Playlist post text" value={post} readOnly onFocus={(event) => event.currentTarget.select()} />}
  </>;
}

export function PlaylistShare({ playlist }: { playlist: string }) {
  const [open, setOpen] = useState(false);
  const endpoint = `/api/playlists/share?playlist=${encodeURIComponent(playlist)}`;
  return <Dialog open={open} onOpenChange={setOpen}>
    <DialogTrigger className="quiet-button room-action"><RoomIcon name="share" />Share playlist</DialogTrigger>
    <DialogBackdrop />
    <RoomDialogContent className="playlist-share-dialog">
      <DialogClose className="dialog-close" aria-label="Close playlist preview"><RoomIcon name="close" /></DialogClose>
      <DialogTitle>Share playlist</DialogTitle>
      <DialogDescription>Preview the song list before sharing the file. Anyone with the file can import it into their room.</DialogDescription>
      {open && <PreviewContent key={endpoint} endpoint={endpoint} />}
    </RoomDialogContent>
  </Dialog>;
}
