import { FindCopySources } from "./Extensions";
import { RoomDialogContent } from "../components/WindowContent";
import { useEffect, useRef, useState } from "react";
import { Dialog, DialogTrigger, DialogBackdrop, DialogClose, DialogTitle, DialogDescription } from "@ui-sdk/react/dialog";
import { audioExportFormats, RoomAction, RoomSelect } from "../components/RoomControls";
import { RoomIcon, RoomIconButton } from "../components/RoomIcon";
import { RetryRequestButton } from "../components/RequestStatus";
import { AddToPlaylist, usePlaylistComposer } from "../components/PlaylistComposer";
import {
  createTabsController,
  Tabs,
  TabsList,
  TabsPanel,
  TabsTab,
} from "@ui-sdk/react/tabs";
import {
  canQueueTrackCopy,
  isHost,
  postJson,
  readable,
  scrollBehavior,
  type ExternalResult,
} from "../api";
import { isDestinationFilter, useStore, type TrackFilter } from "../store";
import {
  DESTINATION_DESCRIPTORS,
} from "../destinations";
import { notify } from "../toasts";
import { Artwork, CoverArt } from "../components/Artwork";
import { GenreTagFilter } from "../components/GenreTagFilter";
import { PlaylistShare } from "../components/PlaylistShare";
import { PlaylistCards } from "../components/PlaylistCards";
import { PlaylistMerge } from "../components/PlaylistMerge";
import { SourceBadge } from "../components/SourceBadge";
import { SkeletonLines, StateCard } from "../components/StateCard";
import { panelHasState, TrackListBody } from "../components/TrackTable";

const FILTERS: { value: TrackFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "ready", label: "Ready to play" },
  { value: "missing", label: "Need a copy" },
];

function Hero() {
  const store = useStore();
  const overview = store.overview;
  const playlists = overview?.playlists ?? [];
  const selected = playlists.find((item) => item.id === store.playlist);
  const composer = usePlaylistComposer();
  const [exportOpen, setExportOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const exportStatus = store.settings?.export_status;
  const canExport = !!selected && isHost(store.settings);
  const spotifyTarget = selected?.spotify_link?.targets?.find(target => target.needs_write_permission)
    ?? selected?.spotify_link;
  const backLabel = store.playlistFromMyPlaylists ? "Back to my playlists" : "Back to library";
  const [exportFormat, setExportFormat] = useState("original");
  const startPlaylistExport = async () => {
    if (!selected || exporting) return;
    setExporting(true);
    try {
      await postJson("/api/export", { playlist: selected.id, format: exportFormat });
      setExportOpen(false);
      notify(
        `Export started. ${selected.name} is being written out as files.`,
      );
    } catch (error) {
      notify(
        error instanceof Error ? error.message : "The export could not start",
      );
    }
    setExporting(false);
  };
  if (store.query || isDestinationFilter(store.filter)) return null;
  const ready = selected?.playable ?? overview?.summary.verified;
  const total = selected?.entries ?? overview?.total;
  return (
    <section className="collection-hero" data-playlist={selected ? true : undefined}>
      {selected && <>
        <div className="playlist-banner-backdrop" aria-hidden="true">
          <Artwork subject={{ cover: selected.cover, seed: selected.id }} loading="lazy" />
        </div>
        <div className="playlist-banner-art" aria-hidden="true">
          <CoverArt cover={selected.cover} fallback={selected.name.trim().slice(0, 1)} />
        </div>
      </>}
      <div className="hero-copy">
        {selected ? <button className="library-back icon-control" title={backLabel} aria-label={backLabel} type="button"
          onClick={() => store.setView(store.playlistFromMyPlaylists ? "private-playlists" : "all")}><RoomIcon name="back" /></button> : null}
        {selected && <SourceBadge playlist={selected} />}
        <h1>{selected?.name ?? "Library"}</h1>
        {total != null && ready != null ? (
          <div className="hero-meta">
            {readable(total)} {total === 1 ? "song" : "songs"} · {readable(ready)} ready to play
          </div>
        ) : null}
      </div>
      <div className="hero-actions">
        <RoomAction primary icon="play" disabled={!ready}
          onClick={() => void store.sendSession("play_available", { playlist: store.playlist })}>
          {selected ? "Play playlist" : "Play library"}
        </RoomAction>
        {selected?.can_add && <RoomAction icon="add" onClick={() => composer.open({ list: selected })}>Add song</RoomAction>}
        {selected && selected.can_merge && playlists.length > 1 && <PlaylistMerge key={selected.id}
          playlist={selected} playlists={playlists} />}
        {canExport && selected && <>
          <PlaylistShare playlist={selected.id} />
          <Dialog open={exportOpen} onOpenChange={setExportOpen}>
            <DialogTrigger className="quiet-button room-action"><RoomIcon name="download" />Export playlist</DialogTrigger>
            <DialogBackdrop />
            <RoomDialogContent className="playlist-composer">
              <DialogClose className="dialog-close" aria-label="Close export"><RoomIcon name="close" /></DialogClose>
              <DialogTitle>Export playlist</DialogTitle>
              <DialogDescription>Write playable songs from {selected.name} to the server’s export folder.</DialogDescription>
              <div className="composer-form">
                <RoomSelect label="Export audio format" value={exportFormat} onChange={setExportFormat}
                  options={audioExportFormats} />
                <div className="composer-actions"><RoomAction primary icon="download" disabled={exporting || exportStatus?.state === "running" || !ready}
                  onClick={() => void startPlaylistExport()}>Start export</RoomAction></div>
              </div>
            </RoomDialogContent>
          </Dialog>
          {(selected.spotify_link || selected.source?.startsWith("spotify")) &&
            <RoomAction icon="magnet" onClick={() => {
              store.spotifyDestination = spotifyTarget?.playlist_id && spotifyTarget.account_id
                ? { playlist: spotifyTarget.playlist_id, account: spotifyTarget.account_id } : null;
              store.settingsTab = "spotify";
              store.setView("settings");
            }}>{spotifyTarget?.needs_write_permission ? "Enable Spotify edits" : "Manage Spotify link"}</RoomAction>}
        </>}
      </div>
    </section>
  );
}

function Shelf() {
  const store = useStore();
  const playlists = store.overview?.playlists ?? [];
  if (
    !playlists.length || store.playlist !== "all" ||
    store.query ||
    isDestinationFilter(store.filter)
  )
    return null;
  const visible = store.playlistsExpanded ? playlists : playlists.slice(0, 6);
  return (
    <section className="playlist-shelf">
      <div className="section-header">
        <h2>Playlists</h2>
        <div className="shelf-actions">
          {playlists.length > 6 ? (
            <button
              className="quiet-button"
              type="button"
              aria-expanded={store.playlistsExpanded}
              onClick={() => store.togglePlaylistsExpanded()}
            >
              {store.playlistsExpanded
                ? "Show fewer"
                : `Show all ${readable(playlists.length)}`}
            </button>
          ) : null}
        </div>
      </div>
      <PlaylistCards playlists={visible} />
    </section>
  );
}

function ExternalRow({ item }: { item: ExternalResult }) {
  const store = useStore();
  const ready = item.status === "verified";
  const stopped = item.catalog_id != null && store.requestRows.find(row => row.status === "unavailable" &&
    (row.track_index === item.catalog_id || row.original_track_index === item.catalog_id));
  const byline = [
    item.artist,
    item.album,
    ready ? "Ready in library" : stopped || item.status === "unavailable" ? "Unavailable" : "",
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="external-item">
      <span className="external-symbol" aria-hidden="true">
        <RoomIcon name="music" />
      </span>
      <span className="external-copy">
        <strong>{item.title}</strong>
        <span>{byline}</span>
      </span>
      <AddToPlaylist song={{ artist: item.artist, title: item.title, album: item.album, track_id: item.catalog_id ?? undefined }} />
      {ready && item.catalog_id != null ? (
        <button
          className="quiet-button"
          type="button"
          onClick={() =>
            void store.sendSession("play", {
              track_id: item.catalog_id,
              playlist: "all",
            })
          }
        >
          Play
        </button>
      ) : stopped ? <RetryRequestButton request={stopped} title={item.title} /> : item.status !== "unavailable" && (
        <button
          className="quiet-button"
          type="button"
          onClick={() => void store.requestExternal(item)}
        >
          Find copy
        </button>
      )}
    </div>
  );
}

function ExplorePanel({ onClear }: { onClear: () => void }) {
  const store = useStore();
  if (store.externalStatus === "idle") {
    return (
      <StateCard
        icon="search"
        title={`Look for “${store.query}” outside your library`}
        body="Search MusicBrainz recordings by artist, title, or release."
        action={
          <button
            className="quiet-button"
            type="button"
            onClick={() => void store.searchExternal()}
          >
            Search MusicBrainz
          </button>
        }
      />
    );
  }
  const fallback = (label: string, buttonLabel: string) => (
    <div className="external-fallback">
      <span>{label}</span>
      <button
        className="quiet-button"
        type="button"
        onClick={() =>
          void store.requestExternal({ artist: "", title: store.externalQuery })
        }
      >
        {buttonLabel}
      </button>
    </div>
  );
  return (
    <>
      <div className="section-header">
        <div>
          <h2>Explore results</h2>
          <p>MusicBrainz recordings · save a song to a playlist or find an audio copy</p>
        </div>
        <button className="quiet-button" type="button" onClick={onClear}>
          Clear
        </button>
      </div>
      <div className="external-list">
        {store.externalStatus === "loading" ? (
          <>
            <StateCard
              kind="loading"
              icon="search"
              title="Searching for this music"
              body="Checking recording titles, artists, and releases."
            />
            <div className="search-skeleton" aria-hidden="true">
              {[0, 1, 2].map((index) => (
                <div key={index}>
                  <SkeletonLines count={2} />
                </div>
              ))}
            </div>
          </>
        ) : store.externalStatus === "error" ? (
          <>
            <StateCard
              kind="error"
              icon="alert"
              title="Catalog search is unavailable"
              body="You can still ask your room to look for a copy."
            />
            {fallback(store.externalError ?? "", "Find copy")}
          </>
        ) : (
          <>
            {store.externalResults.length ? (
              store.externalResults.map((item, index) => (
                <ExternalRow
                  key={`${item.catalog_id ?? item.musicbrainz_id ?? index}`}
                  item={item}
                />
              ))
            ) : (
              <StateCard
                icon="search"
                title="No matching recordings"
                body="Try another spelling or ask your room to look for a copy."
              />
            )}
            {fallback(
              `Ask your download engine to search for “${store.externalQuery}”.`,
              "Find copy",
            )}
          </>
        )}
      </div>
    </>
  );
}

function LibraryTracks({ libraryCount }: { libraryCount: number | null }) {
  const store = useStore();
  const missingShown = store.tracks.filter(canQueueTrackCopy);
  const destination = isDestinationFilter(store.filter)
    ? DESTINATION_DESCRIPTORS[store.filter]
    : undefined;
  return (
    <>
      <div className="section-header">
        {store.query && store.member ? <h1>Search results</h1>
          : destination && !store.query ? <h1>{destination.headerTitle}</h1> : <h2>Songs</h2>}
        <div className="tracks-header-actions">
          {!store.member && store.query && missingShown.length ? (
            <button
              className="quiet-button"
              type="button"
              disabled={store.retryingVisible}
              onClick={() => void store.retryVisibleCopies()}
            >
              {`Find copies shown (${Math.min(missingShown.length, 50)})`}
            </button>
          ) : null}
          <span className="result-count">
            {libraryCount != null && !store.query && (destination || store.filter !== "all" || store.genres.length > 0)
              ? `${readable(store.tracksTotal)} track${store.tracksTotal === 1 ? "" : "s"}`
              : ""}
          </span>
          <GenreTagFilter />
        </div>
      </div>
      {isDestinationFilter(store.filter) ? (
        // The sidebar destinations (Favorites, Your listening)
        // are their own places, not availability tabs: one
        // table, no All/Ready/Missing tabs.
        <div
          className={`table-wrap${panelHasState(store) ? " has-state" : ""}`}
        >
          <TrackListBody />
        </div>
      ) : (
        <Tabs
          value={store.filter}
          onValueChange={(value) => {
            if (value) store.setFilter(value);
          }}
        >
          <TabsList className="filter-tabs" aria-label="Track availability">
            {FILTERS.map((filter) => (
              <TabsTab key={filter.value} value={filter.value}>
                {filter.label}
              </TabsTab>
            ))}
          </TabsList>
          {FILTERS.map((filter) => (
            <TabsPanel
              key={filter.value}
              value={filter.value}
              className={`table-wrap${
                store.filter === filter.value && panelHasState(store)
                  ? " has-state"
                  : ""
              }`}
            >
              {store.filter === filter.value ? <TrackListBody /> : null}
            </TabsPanel>
          ))}
        </Tabs>
      )}
    </>
  );
}

function TracksSection() {
  const store = useStore();
  const [resultsTabs] = useState(() =>
    createTabsController<"library" | "explore">(),
  );
  const sectionRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (store.externalRevision > 0) {
      sectionRef.current?.scrollIntoView({
        behavior: scrollBehavior(),
        block: "start",
      });
    }
  }, [store.externalRevision]);
  // The Library count is the panel's current total, written by the
  // same response as the result count; a reset load hides it until
  // the response lands. The Explore count exists only once a search
  // has succeeded, where a successful zero still shows 0.
  const libraryCount =
    store.tracksLoaded && !store.tracksResetting ? store.tracksTotal : null;
  const exploreCount =
    store.externalStatus === "ready" ? store.externalResults.length : null;
  const clearExplore = () => {
    store.clearExternal();
    requestAnimationFrame(() => resultsTabs.focus());
  };
  if (!store.query || store.member) {
    return (
      <section className="tracks-section" ref={sectionRef}>
        <LibraryTracks libraryCount={libraryCount} />
      </section>
    );
  }
  return (
    <section className="tracks-section" ref={sectionRef}>
      <div className="section-header"><h1>Search results</h1></div>
      <Tabs
        value={store.resultsTab}
        onValueChange={(value) => {
          if (value) store.setResultsTab(value);
        }}
        controller={resultsTabs}
      >
        <TabsList className="results-tabs" aria-label="Search results">
          <TabsTab
            value="library"
            aria-label={
              libraryCount == null
                ? "In library"
                : `In library, ${libraryCount} track${libraryCount === 1 ? "" : "s"}`
            }
          >
            In library
            {libraryCount == null ? null : (
              <span className="tab-count">{libraryCount}</span>
            )}
          </TabsTab>
          <TabsTab
            value="explore"
            aria-label={
              exploreCount == null
                ? "Explore"
                : `Explore, ${exploreCount} result${exploreCount === 1 ? "" : "s"}`
            }
          >
            Explore
            {exploreCount == null ? null : (
              <span className="tab-count">{exploreCount}</span>
            )}
          </TabsTab>
        </TabsList>
        <TabsPanel value="library" keepMounted>
          <LibraryTracks libraryCount={libraryCount} />
        </TabsPanel>
        <TabsPanel value="explore" keepMounted>
          {store.resultsTab === "explore" && <FindCopySources />}
          <ExplorePanel onClear={clearExplore} />
        </TabsPanel>
      </Tabs>
    </section>
  );
}

function RecentDownloads() {
  const store = useStore();
  const downloads = store.requestsSlice.recentDownloads;
  if (store.member || store.view !== "all" || store.query || store.playlist !== "all" || !downloads.length) return null;
  return <section className="recent-downloads" aria-labelledby="recent-downloads-heading">
    <h2 id="recent-downloads-heading">Recent downloads</h2>
    <ul>{downloads.map(track => <li key={track.id}>
      <div><strong>{track.title}</strong><small>{track.artist}</small></div>
      <RoomIconButton icon="play" label={`Play ${track.title}`} onClick={() => store.requestsSlice.playRequest(track)} />
    </li>)}</ul>
  </section>;
}

export function Collection() {
  const store = useStore();
  return (
    <div className={`view collection-view${store.query ? " is-searching" : ""}`}>
      <Hero />
      <RecentDownloads />
      <Shelf />
      <TracksSection />
    </div>
  );
}
