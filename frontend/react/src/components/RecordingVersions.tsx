import { useEffect, useState } from "react";
import { Dialog, DialogBackdrop, DialogDescription, DialogTitle } from "@ui-sdk/react/dialog";
import { api, time, type DiscoverTrack, type RequestSong } from "../api";
import { RoomIcon, RoomIconButton } from "./RoomIcon";
import { RoomDialogContent } from "./WindowContent";

type Song = Pick<DiscoverTrack, "title" | "artist"> & Partial<Pick<DiscoverTrack, "musicbrainz_id">>;
type Results = { kind: "loading" | "error" } | { kind: "loaded"; versions: DiscoverTrack[] };

export function RecordingVersions({ song, onChoose }: {
  song: Song; onChoose: (song: RequestSong) => void;
}) {
  const [picker, setPicker] = useState<{ song: Song; onChoose: (song: RequestSong) => void } | null>(null);
  const close = () => setPicker(null);
  return <>
    <RoomIconButton icon="album" label={`Other recordings: ${song.title}`} onClick={() => setPicker({ song, onChoose })} />
    {picker && <Dialog open onOpenChange={value => { if (!value) close(); }}>
      <DialogBackdrop />
      <RoomDialogContent className="format-dialog">
        <header className="format-dialog-header">
          <DialogTitle>Other recordings</DialogTitle>
          <RoomIconButton icon="close" label="Close recording choices" onClick={close} />
        </header>
        <DialogDescription>{picker.song.title} · {picker.song.artist}</DialogDescription>
        <RecordingChoices song={picker.song} onChoose={version => {
          close();
          picker.onChoose(version);
        }} />
      </RoomDialogContent>
    </Dialog>}
  </>;
}

export function RecordingChoices({ song, disabled, onChoose }: {
  song: Song; disabled?: boolean; onChoose: (song: RequestSong) => void;
}) {
  const [result, setResult] = useState<Results>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const { artist, title, musicbrainz_id: current } = song;
  useEffect(() => {
    const controller = new AbortController();
    const query = new URLSearchParams({ q: `${artist.slice(0, 70)} - ${title}`.slice(0, 160) });
    const path = current ? "/api/discover/recording/" + encodeURIComponent(current) : "/api/discover/matches?" + query;
    void api<{ leads: { kind: string; track?: DiscoverTrack }[] }>(path, { signal: controller.signal })
      .then(({ leads }) => {
        const versions = new Map<string, DiscoverTrack>();
        for (const lead of leads) {
          if (lead.kind === "recording" && lead.track?.duration && lead.track.musicbrainz_id !== current) {
            versions.set(lead.track.musicbrainz_id, lead.track);
          }
        }
        if (!controller.signal.aborted) setResult({ kind: "loaded", versions: [...versions.values()] });
      }).catch(() => {
        if (!controller.signal.aborted) setResult({ kind: "error" });
      });
    return () => controller.abort();
  }, [artist, title, current, attempt]);

  return <>
    {result.kind === "loading" && <p role="status">Looking for versions…</p>}
    {result.kind === "error" && <p role="alert">Couldn’t load versions.{" "}
      <button className="quiet-button" type="button" onClick={() => {
        setResult({ kind: "loading" });
        setAttempt(value => value + 1);
      }}>Try again</button>
    </p>}
    {result.kind === "loaded" && (result.versions.length ? <div aria-label="Recording versions">
      {result.versions.map(track => <button type="button" className="radio-result" key={track.musicbrainz_id}
        disabled={disabled} onClick={() => {
          const { artist, title, album, musicbrainz_id, duration, performance, version } = track;
          onChoose({ artist, title, album, musicbrainz_id, duration, performance, version });
        }}>
        <span><strong>{track.title}</strong><small>{[
          track.artist, track.version, track.performance, track.album, time(track.duration ?? 0),
        ].filter(Boolean).join(" · ")}</small></span>
        <RoomIcon name="chevronRight" />
      </button>)}
    </div> : <p role="status">No other recordings found.</p>)}
  </>;
}
