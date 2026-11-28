import { RoomIcon } from "./RoomIcon";
import { useRef, type ReactNode } from "react";
import { Menu, MenuContent, MenuItem, MenuTrigger } from "@ui-sdk/react/menu";
import { BUSY_STATUSES, isHost, statusLabel, time, type DownloadFavorite, type Track } from "../api";
import { isDestinationFilter, useStore } from "../store";
import {
  DESTINATION_DESCRIPTORS,
} from "../destinations";
import { CoverArt } from "./Artwork";
import { usePlaylistComposer } from "./PlaylistComposer";
import { FavoriteButton } from "./FavoriteButton";
import { StateCard, TrackLoading } from "./StateCard";
import { TryMp3Button } from "./TryMp3Button";
import { RetryRequestButton } from "./RequestStatus";

function TrackRow({ track }: { track: Track }) {
  const store = useStore();
  const composer = usePlaylistComposer();
  const menuTrigger = useRef<HTMLButtonElement>(null);
  const ready = track.status === "verified";
  const request = store.requestRows.find(row => row.track_index === track.id ||
    (row.status === "unavailable" && row.original_track_index === track.id));
  const retry = request ? request.mp3_retry : track.mp3_retry;
  const requestStatus = request?.status ?? "";
  const status = !ready && (BUSY_STATUSES.includes(requestStatus) || ["not_found", "waiting_format", "unavailable"].includes(requestStatus))
    ? requestStatus : track.status;
  const busy = BUSY_STATUSES.includes(status);
  const requesting = store.requestingIds.has(track.id);
  const isPlaying = store.current?.id === track.id;

  const onMenuAction = (value: string) => {
    if (value === "play_next")
      void store.sendSession("play_next", { track_id: track.id }, "Playing next");
    else if (value === "enqueue")
      void store.sendSession("enqueue", { track_id: track.id }, "Added to queue");
    else if (value === "open") void store.openRoomPlayer(track.id, menuTrigger.current, store.playlist);
    else if (value === "desktop") void store.openSource(track.id);
    else if (value === "add_playlist") composer.open({ song: { title: track.title, artist: track.artist, album: track.album, track_id: track.id, track: track.spotify_url } });
    else if (value === "replace_copy") void store.requestsSlice.replaceTrackCopy(track.id);
    else if (value === "info") store.openSongInfo(track.id);
    else if (value === "album_tracks") store.openSongInfo(track.id, "album-tracks", menuTrigger.current);
  };

  return (
    <div className={`track-row${isPlaying ? " is-playing" : ""}`}>
      <div className="song-cell">
        <button
          className="track-art track-art-button"
          type="button"
          aria-label={`Song details for ${track.title}`}
          onClick={() => store.openSongInfo(track.id)}
        >
          <CoverArt cover={track.cover} />
        </button>
        <span className="song-info">
          <button
            className="track-title"
            type="button"
            title={`Song details for ${track.title}`}
            onClick={() => store.openSongInfo(track.id)}
          >
            {track.title}
          </button>
          <span className="song-byline">
            <button type="button" className="music-profile-byline" title={track.artist}
              aria-label={`Open artist ${track.artist}`}
              onClick={event => store.openSongInfo(track.id, "artist-profile", event.currentTarget)}>{track.artist}</button>
            {track.album && <button type="button" className="music-profile-byline music-profile-mobile-album"
              aria-label={`Open album ${track.album}`}
              onClick={event => store.openSongInfo(track.id, "album-tracks", event.currentTarget)}>{track.album}</button>}
          </span>
        </span>
      </div>
      <span className="album-cell">
        {track.album ? <button type="button" className="music-profile-byline" title={track.album}
          aria-label={`Open album ${track.album}`}
          onClick={event => store.openSongInfo(track.id, "album-tracks", event.currentTarget)}>{track.album}</button> : "—"}
      </span>
      <span className="track-meta">
        <span
          className={`status-pill ${status}`}
          title={track.reason || statusLabel(status)}
        >
          {statusLabel(status)}
        </span>
        <span className="time-cell">{time(track.duration)}</span>
      </span>
      <span className={`row-action${ready ? " play" : ""}`}>
        <FavoriteButton track={track} />
        <Menu onAction={onMenuAction}>
          <MenuTrigger ref={menuTrigger}
            className="icon-action track-more"
            aria-label={`More actions for ${track.title}`}
            title="More actions"
          >
            <RoomIcon name="more" />
          </MenuTrigger>
          <MenuContent className="track-menu">
            {ready ? (
              <>
                <MenuItem value="play_next">Play next</MenuItem>
                <MenuItem value="enqueue">Add to queue</MenuItem>
                <MenuItem value="open">Open in room player</MenuItem>
                {!store.member && isHost(store.settings) && <MenuItem value="replace_copy">Replace copy</MenuItem>}
                {store.settings?.desktop_player_available && <MenuItem value="desktop">Open in server’s desktop music app</MenuItem>}
              </>
            ) : null}
            {composer.available && <MenuItem value="add_playlist">Add to playlist</MenuItem>}
            <MenuItem value="info">Song information</MenuItem>
            {track.album ? <MenuItem value="album_tracks">Album track list</MenuItem> : null}
          </MenuContent>
        </Menu>
        {ready ? (
          <button
            type="button"
            aria-label={`Play ${track.title}`}
            onClick={() =>
              void store.sendSession("play", {
                track_id: track.id,
                playlist: store.playlist,
              })
            }
          >
            Play
          </button>
        ) : status === "unavailable" ? (
          request && <RetryRequestButton request={request} title={track.title} />
        ) : !store.member && retry ? (
          <TryMp3Button retry={retry} title={track.title} />
        ) : !store.member ? (
          <button
            type="button"
            disabled={busy || requesting}
            aria-label={status === "waiting_format" ? `Choose format for ${track.title}` : `Find a copy of ${track.title}`}
            onClick={() => void store.requestTrack(track.id)}
          >
            {busy ? statusLabel(status) : status === "waiting_format" ? "Choose format" : "Find copy"}
          </button>
        ) : null}
      </span>
    </div>
  );
}

function FavoriteDownloadRow({ track }: { track: DownloadFavorite }) {
  const store = useStore();
  const playing = store.current?.id === track.id;
  return <div className={`track-row${playing ? " is-playing" : ""}`}>
    <div className="song-cell">
      <span className="track-art"><CoverArt cover={track.cover} /></span>
      <span className="song-info">
        <strong className="track-title">{track.title}</strong>
        <span className="song-byline">{track.artist}</span>
      </span>
    </div>
    <span className="album-cell">{track.album || "—"}</span>
    <span className="track-meta">
      <span className="status-pill verified">{statusLabel("verified")}</span>
      <span className="time-cell">{time(track.duration)}</span>
    </span>
    <span className="row-action play">
      <FavoriteButton track={track} />
      <button type="button" aria-label={`Play ${track.title}`}
        onClick={() => void store.sendSession("play_request", { request_id: track.request_id })}>Play</button>
    </span>
  </div>;
}

function emptyCard(
  store: ReturnType<typeof useStore>,
): ReactNode {
  if (store.query) {
    return (
      <StateCard
        icon="search"
        label="Library search"
        title="No matches in your library"
        body={store.member ? "Try another spelling or clear the search." : "Try another spelling, or search open catalogs for a copy to request."}
        action={
          <button
            className="quiet-button"
            type="button"
            onClick={() => void store.searchExternal()}
          >
            Search open catalogs <RoomIcon name="northeast" />
          </button>
        }
      />
    );
  }
  if (isDestinationFilter(store.filter)) {
    const destination = DESTINATION_DESCRIPTORS[store.filter];
    return (
      <StateCard
        icon={destination.emptyIcon}
        title={destination.emptyTitle}
        body={destination.emptyBody}
      />
    );
  }
  if (store.filter === "ready") {
    return (
      <StateCard
        icon="music"
        label="Ready to play"
        title="No songs ready here yet"
        body="Copies you find and verify will appear here."
        action={
          <button
            className="quiet-button"
            type="button"
            onClick={() => store.setFilter("missing")}
          >
            See songs needing a copy
          </button>
        }
      />
    );
  }
  if (store.filter === "missing") {
    return (
      <StateCard
        icon="check"
        label="Need a copy"
        title="Nothing missing here"
        body="Every song in this view is ready, or this playlist has no songs yet."
        action={
          <button
            className="quiet-button"
            type="button"
            onClick={() => store.setFilter("ready")}
          >
            Browse ready songs
          </button>
        }
      />
    );
  }
  return (
    <StateCard
      icon="music"
      title="No songs here yet"
      body={isHost(store.settings) ? "Import a playlist or link a music folder to start your library." : "Songs will appear here when the room owner adds music."}
      action={isHost(store.settings) ?
        <button
          className="quiet-button"
          type="button"
          onClick={() => {
            store.settingsTab = "sources";
            store.setView("settings");
          }}
        >
          Add a music source
        </button> : null
      }
    />
  );
}

// The body of one availability panel: head, list (rows, skeleton,
// state cards), and the load-more control. Only the active filter's
// panel renders it; inactive panels stay empty, as in the vanilla UI.
export function TrackListBody() {
  const store = useStore();
  const { downloadFavorites, loadedTrackCount } = store.browsingSlice;
  return (
    <>
      <div className="table-head">
        <span>Song</span>
        <span>Album</span>
        <span>Status</span>
        <span>Time</span>
        <span />
      </div>
      <div aria-busy={store.tracksLoading}>
        {store.tracksResetting ? (
          <TrackLoading />
        ) : store.tracksError ? (
          <StateCard
            kind="error"
            icon="alert"
            label="Library"
            title="Songs could not load"
            body={store.tracksError}
            action={
              <button
                className="quiet-button"
                type="button"
                onClick={() => void store.loadTracks(true)}
              >
                Try again
              </button>
            }
          />
        ) : loadedTrackCount ? (
          <>
            {store.tracks.map(track => <TrackRow key={track.id} track={track} />)}
            {downloadFavorites.map(track => <FavoriteDownloadRow key={track.id} track={track} />)}
          </>
        ) : (
          emptyCard(store)
        )}
        {!store.tracksResetting && store.tracksPageError ? (
          <StateCard
            kind="error"
            compact
            icon="alert"
            label="More songs"
            title="The next songs could not load"
            body={store.tracksPageError}
            action={
              <button
                className="quiet-button"
                type="button"
                onClick={() => void store.loadTracks(false)}
              >
                Retry this page
              </button>
            }
          />
        ) : null}
      </div>
      {!store.tracksResetting && loadedTrackCount < store.tracksTotal ? (
        <button
          className="load-more"
          type="button"
          disabled={store.tracksLoading}
          onClick={() => void store.loadTracks(false)}
        >
          {store.tracksLoading ? "Loading more songs…" : "Show more tracks"}
        </button>
      ) : null}
    </>
  );
}

// Whether the panel currently shows a state card instead of the
// table; the stylesheet hides the table head in that case.
export function panelHasState(store: ReturnType<typeof useStore>): boolean {
  return (
    store.tracksResetting || !!store.tracksError || store.browsingSlice.loadedTrackCount === 0
  );
}
