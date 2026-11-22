import { RoomDialogContent } from "./WindowContent";
import { useRef, useState } from "react";
import {
  Slider,
  SliderControl,
  SliderIndicator,
  SliderThumb,
  SliderTrack,
} from "@ui-sdk/react/slider";
import { Dialog, DialogBackdrop, DialogClose, DialogTitle } from "@ui-sdk/react/dialog";
import { Toggle } from "@ui-sdk/react/toggle";
import { time, type SessionTrack } from "../api";
import { canOpenMusicProfile } from "../artistConnections";
import { lyricRef } from "../lyrics";
import { macRoomClient, usesSystemAudio } from "../mediaOutput";
import { useStore } from "../store";
import { useSessionEngine } from "../sessionEngine";
import { CoverArt } from "./Artwork";
import { FavoriteButton } from "./FavoriteButton";
import { NowPlaying } from "./NowPlaying";
import { PlaybackDevices } from "./PlaybackDevices";
import { RoomIcon } from "./RoomIcon";
import { RadioBar } from "./RadioBar";

// The quality readout (item 35): format, depth, and rate for
// lossless tracks only, from the current track's probe facts
// (e.g. "FLAC · 24-bit / 96 kHz"). Lossy tracks and tracks the
// probe has not described show nothing.
const LOSSLESS_CODECS = new Set(["flac", "alac", "ape", "wavpack", "tta"]);

function qualityLabel(track: SessionTrack): string | null {
  const codec = (track.codec || "").toLowerCase();
  if (!codec) return null;
  if (
    !LOSSLESS_CODECS.has(codec) &&
    !codec.startsWith("pcm_") &&
    !codec.startsWith("dsd")
  )
    return null;
  const name = codec.startsWith("pcm_")
    ? "PCM"
    : codec === "alac"
      ? "ALAC"
      : codec.toUpperCase();
  const parts = [name];
  if (track.bit_depth) parts.push(`${track.bit_depth}-bit`);
  if (track.sample_rate)
    parts.push(`${parseFloat((track.sample_rate / 1000).toFixed(2))} kHz`);
  return parts.join(" · ");
}

// The player bar (item 29) and the room's two audio elements:
// the session's element and the standby that preloads the next
// track for plan 039's gapless swap (item 35). Seek and volume
// are ui-sdk Sliders: a seek scrubs this device's audio while it
// moves and commits to the shared session when the interaction
// ends, exactly as the vanilla range input did.
export function Player() {
  const store = useStore();
  const audioRefA = useRef<HTMLAudioElement>(null);
  const audioRefB = useRef<HTMLAudioElement>(null);
  const engine = useSessionEngine(store, audioRefA, audioRefB);
  const [expanded, setExpanded] = useState(false);
  const [scrub, setScrub] = useState<number | null>(null);
  const layer = store.playerLayer;
  const session = store.session;
  const track = session?.current ?? null;
  const jam = store.sessionSlice.followingJam;
  const playbackStatus = jam ? `Jam with ${jam.host}` : engine.status;
  const hasQueue = !!session?.queue_length;
  const duration = engine.duration || track?.duration || 0;
  const shownTime = scrub ?? engine.currentTime;
  const quality = track ? qualityLabel(track) : null;

  const transport = session?.playing
    ? engine.audioPlaying ? "Pause" : "Join audio"
    : session?.session_playing && session.outputs_explicit && !store.sessionSlice.bufferedPlayback ? "Pause"
    : session?.session_playing && session.output_selected === false ? "Listen here" : "Play";

  function openLayer(next: "now-playing" | "devices", trigger: HTMLButtonElement) {
    store.setPlayerLayer(next, trigger);
  }

  function closeLayer(restoreFocus = true) {
    const trigger = store.playerTrigger;
    store.setPlayerLayer(null);
    if (restoreFocus) requestAnimationFrame(() => { if (trigger?.isConnected) trigger.focus({ preventScroll: true }); });
  }

  const timeline = (
    <div className="timeline">
      <span>{time(shownTime)}</span>
      <Slider
        value={Math.min(shownTime, duration || shownTime)}
        min={0}
        max={duration}
        step={1}
        disabled={!track || duration <= 0 || !!jam}
        onValueChange={(value: number) => {
          setScrub(value);
          engine.seekTo(value);
        }}
        onValueCommit={(value: number) => {
          engine.commitSeek(value);
          setScrub(null);
        }}
      >
        <SliderControl>
          <SliderTrack>
            <SliderIndicator />
            <SliderThumb aria-label="Seek" />
          </SliderTrack>
        </SliderControl>
      </Slider>
      <span>{time(duration)}</span>
      {track && lyricRef(track.id) && (
        <button type="button" className="player-lyrics" aria-label="Show lyrics" onClick={() => store.openPlaybackLyrics("room")}>
          <RoomIcon name="lyrics" /> Lyrics
        </button>
      )}
      {quality ? <span className="player-quality">{quality}</span> : null}
    </div>
  );

  return (<>
    <footer className="player" aria-label="Audio player" data-expanded={expanded}
      data-mode={store.radioSounding ? "radio" : track ? "track" : hasQueue ? "queued" : "idle"}>
      <button type="button" className="tablet-player-toggle" aria-expanded={expanded}
        aria-label={expanded ? "Collapse player controls" : "Expand player controls"}
        onClick={() => setExpanded(!expanded)}>
        <span aria-hidden="true"><RoomIcon name={expanded ? "chevronDown" : "chevronUp"} /></span>
      </button>
      {store.radioSounding ? (
        <RadioBar />
      ) : (
        <>
      <div className="player-track">
        <button
          className="player-cover"
          type="button"
          aria-label="Expand artwork"
          title="Expand artwork"
          disabled={!track}
          onClick={(event) => {
            if (!track) return;
            store.openArtwork(
              {
                title: track.title,
                artist: track.artist,
                album: track.album || "",
                cover: track.cover,
                seed: track.id,
              },
              event.currentTarget,
            );
          }}
        >
          {track ? <CoverArt cover={track.cover} /> : <RoomIcon name="music" />}
        </button>
        {track ? (
          <div className="player-text">
            <button className="player-track-trigger" type="button" aria-label="Open now playing"
              onClick={event => openLayer("now-playing", event.currentTarget)}><strong>{track.title}</strong></button>
            {canOpenMusicProfile(track.id) ? <div className="music-profile-player-links">
              <button type="button" className="music-profile-byline" aria-label={`Open artist ${track.artist}`}
                onClick={event => store.openSongInfo(track.id, "artist-profile", event.currentTarget)}>{track.artist}</button>
              {track.album && <button type="button" className="music-profile-byline" aria-label={`Open album ${track.album}`}
                onClick={event => store.openSongInfo(track.id, "album-tracks", event.currentTarget)}>{track.album}</button>}
            </div> : <span>{track.artist}</span>}
          </div>
        ) : (
          <div className="player-text"><strong>Nothing playing</strong><span>{hasQueue ? "Queue ready to play" : "Choose a ready track"}</span></div>
        )}
        {track && typeof track.id === "number" ? (
          <button
            className="player-info"
            type="button"
            aria-label="Song information"
            title="Song information"
            onClick={() => store.openSongInfo(track.id as number)}
          >
            <RoomIcon name="info" />
          </button>
        ) : null}
        {track && typeof track.id === "number" ? (
          <FavoriteButton
            className="player-info"
            track={{
              id: track.id,
              title: track.title,
              favorite: !!track.favorite,
            }}
          />
        ) : null}
        {track && store.settings?.desktop_player_available ? (
          <button
            className="player-info"
            type="button"
            aria-label="Open in server’s desktop music app"
            title="Open in server’s desktop music app"
            onClick={() =>
              void store.openSource(
                store.session?.current_ref as number | string,
              )
            }
          >
            <RoomIcon name="northeast" />
          </button>
        ) : null}
      </div>
      <div className="player-center">
        <div className="player-buttons">
          <Toggle
            className="player-shuffle"
            pressed={!!session?.shuffle}
            onPressedChange={() => void store.sendSession("shuffle")}
            aria-label="Shuffle play"
            title="Shuffle play"
          >
            <RoomIcon name="shuffle" />
          </Toggle>
          <Toggle
            className="genre-flow-button"
            pressed={!!session?.genre_flow}
            onPressedChange={() => void store.sendSession("genre_flow")}
            aria-label="Genre flow"
            title="Genre flow: keep the queue in the current song's genre"
          >
            Genre
          </Toggle>
          <button
            type="button"
            aria-label="Previous track"
            onClick={() => void store.sendSession("previous")}
          >
            <RoomIcon name="previous" />
          </button>
          <button
            className="player-toggle"
            type="button"
            aria-label={transport}
            onClick={() => store.togglePlayback()}
          >
            <RoomIcon name={transport === "Pause" ? "pause" : "play"} />
          </button>
          <button
            type="button"
            aria-label="Next track"
            onClick={() => void store.sendSession("next")}
          >
            <RoomIcon name="next" />
          </button>
          <button className="player-output" type="button" aria-label="Playback devices" title="Playback devices"
            onClick={event => openLayer("devices", event.currentTarget)}>
            <RoomIcon name="outputs" />
          </button>
          <button
            className="player-queue"
            type="button"
            aria-label="Open queue"
            title="Open queue"
            aria-expanded={store.queueOpen}
            onClick={() => store.setQueueOpen(!store.queueOpen)}
          >
            <RoomIcon name="menu" />
          </button>
        </div>
        {(track || !hasQueue) && layer !== "now-playing" && timeline}
      </div>
      <div className="player-extra">
        {playbackStatus && <span>{playbackStatus}</span>}
        {(!usesSystemAudio || macRoomClient) && <>
        <span aria-hidden="true"><RoomIcon name="volume" /></span>
        <Slider
          defaultValue={80}
          min={0}
          max={100}
          step={1}
          onValueChange={(value: number) => engine.setVolume(value / 100)}
        >
          <SliderControl>
            <SliderTrack>
              <SliderIndicator />
              <SliderThumb aria-label="Volume" />
            </SliderTrack>
          </SliderControl>
        </Slider>
        </>}
      </div>
        </>
      )}
      {!store.radioSounding && track && duration > 0 &&
        <progress className="tablet-progress" aria-hidden="true" max={duration} value={shownTime} />}
      <audio ref={audioRefA} preload="metadata" />
      {!usesSystemAudio && <audio ref={audioRefB} preload="auto" />}
    </footer>
    {layer === "now-playing" && track && (
      <NowPlaying track={track} timeline={timeline} transport={transport} close={closeLayer}
        explore={(id, mode) => {
          closeLayer(false);
          store.openSongInfo(id, mode, store.playerTrigger);
        }} />
    )}
    {layer === "devices" && <Dialog open onOpenChange={open => { if (!open) closeLayer(); }}>
      <DialogBackdrop />
      <RoomDialogContent className="devices-dialog">
        <header className="devices-dialog-header">
          <DialogTitle>Playback devices</DialogTitle>
          <DialogClose className="dialog-close" aria-label="Close playback devices"><RoomIcon name="close" /></DialogClose>
        </header>
        <PlaybackDevices close={closeLayer} />
      </RoomDialogContent>
    </Dialog>}
  </>);
}
