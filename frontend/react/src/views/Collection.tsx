import { FindCopySources } from "./Extensions";
import { RoomDialogContent, RoomAlertContent } from "../components/WindowContent";
import { useEffect, useRef, useState } from "react";
import {
  AlertDialog,
  AlertDialogBackdrop,
  AlertDialogClose,
  AlertDialogDescription,
  AlertDialogTitle,
} from "@ui-sdk/react/alert-dialog";
import { Dialog, DialogTrigger, DialogBackdrop, DialogClose, DialogTitle, DialogDescription } from "@ui-sdk/react/dialog";
import { audioExportFormats, RoomAction, RoomSelect } from "../components/RoomControls";
import { RoomIcon, RoomIconButton } from "../components/RoomIcon";
import { AddToPlaylist, usePlaylistComposer } from "../components/PlaylistComposer";
import { Input } from "@ui-sdk/react/input";
import {
  createTabsController,
  Tabs,
  TabsList,
  TabsPanel,
  TabsTab,
} from "@ui-sdk/react/tabs";
import { Toggle } from "@ui-sdk/react/toggle";
import {
  ApiError,
  isHost,
  postJson,
  readable,
  scrollBehavior,
  type ExternalResult,
  type MergeResult,
} from "../api";
import { isDestinationFilter, useStore, type TrackFilter } from "../store";
import {
  DESTINATION_DESCRIPTORS,
} from "../destinations";
import { notify } from "../toasts";
import { CoverArt } from "../components/Artwork";
import { GenreTagFilter } from "../components/GenreTagFilter";
import { PlaylistShare } from "../components/PlaylistShare";
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
      {selected && <div className="playlist-banner-art" aria-hidden="true">
        <CoverArt cover={selected.cover} fallback={selected.name.trim().slice(0, 1)} />
      </div>}
      <div className="hero-copy">
        {selected ? <button className="library-back icon-control" title="Back to library" aria-label="Back to library" type="button"
          onClick={() => store.setView("all")}><RoomIcon name="back" /></button> : null}
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
              const link = selected.spotify_link;
              store.spotifyDestination = link?.playlist_id && link.account_id
                ? { playlist: link.playlist_id, account: link.account_id } : null;
              store.settingsTab = "spotify";
              store.setView("settings");
            }}>Manage Spotify link</RoomAction>}
        </>}
      </div>
    </section>
  );
}

const NEW_TARGET = "__new__";
const OWNED_TARGET_REFUSAL =
  "That playlist is kept in sync from its source; merging into it would be overwritten by the next sync. Choose one of your own lists, or merge into a new list.";

// Playlist merge (plan 046): pick playlists on the shelf, union
// them into one of your own lists or a new one. The server owns
// the union; this only collects the choice. Playlists with a
// magnet are kept in sync from their source, so they can be
// merged from but never into: the target select shows them
// disabled, with the reason on the item and in the note.
function MergeBar() {
  const store = useStore();
  const playlists = store.overview?.playlists ?? [];
  const selected = playlists.filter((playlist) =>
    store.mergeSelection.has(playlist.id),
  );
  const [target, setTarget] = useState(NEW_TARGET);
  const [typedName, setTypedName] = useState<string>();
  const [merging, setMerging] = useState(false);
  const [conflict, setConflict] = useState<string[] | null>(null);
  const nameFieldRef = useRef<HTMLLabelElement>(null);

  const prefill = selected[0] ? `${selected[0].name} (merged)` : "";
  const shownName = typedName ?? prefill;

  useEffect(() => {
    nameFieldRef.current?.querySelector("input")?.focus();
  }, []);

  const focusNameField = () =>
    requestAnimationFrame(() =>
      nameFieldRef.current?.querySelector("input")?.focus(),
    );

  const submitMerge = async (extra: Record<string, unknown> = {}) => {
    const intoExisting = target !== NEW_TARGET;
    const body = {
      sources: [...store.mergeSelection],
      target: intoExisting
        ? { playlist: target }
        : { name: shownName.trim() },
      ...extra,
    };
    setMerging(true);
    try {
      const result = await postJson<MergeResult>("/api/playlists/merge", body);
      store.exitMergeMode();
      await store.refreshOverview();
      notify(
        `Merged into ${result.name}: ${readable(result.tracks)} ${result.tracks === 1 ? "song" : "songs"}${
          intoExisting ? `, ${readable(result.added)} added` : ""
        }`,
      );
    } catch (error) {
      const conflicts =
        error instanceof ApiError
          ? (error.data as { conflicts?: unknown })?.conflicts
          : undefined;
      if (Array.isArray(conflicts) && conflicts.length) {
        setConflict(conflicts as string[]);
      } else {
        notify(error instanceof Error ? error.message : "Merge failed");
      }
    } finally {
      setMerging(false);
    }
  };

  return (
    <div className="merge-bar">
      <p className="merge-count" role="status">
        {selected.length} selected
      </p>
      <RoomSelect label="Merge into" value={target} onChange={setTarget}
        options={[{ value: NEW_TARGET, label: "New list…" }, ...playlists.map(playlist => {
          const owned = playlist.source !== "local";
          return { value: playlist.id, label: playlist.name, icon: owned ? "magnet" as const : undefined,
            disabled: owned, title: owned ? OWNED_TARGET_REFUSAL : undefined };
        })]} />
      {target === NEW_TARGET ? (
        <label className="merge-name-label" ref={nameFieldRef}>
          Name{" "}
          <Input
            value={shownName}
            maxLength={120}
            onValueChange={(value) => setTypedName(value)}
          />
        </label>
      ) : null}
      <p className="merge-note">
        Linked playlists can be merge sources. Choose a local playlist for the result.
      </p>
      <div className="merge-actions">
        <button
          className="primary-action"
          type="button"
          disabled={merging || !selected.length}
          onClick={() => void submitMerge()}
        >
          Merge
        </button>
        <button
          className="quiet-button"
          type="button"
          onClick={() => store.exitMergeMode()}
        >
          Cancel
        </button>
      </div>
      <AlertDialog
        open={!!conflict}
        onOpenChange={(
          open: boolean,
          details: { returnValue?: string },
        ) => {
          if (open) return;
          const choice = details.returnValue;
          setConflict(null);
          if (choice === "rename") void submitMerge({ on_conflict: "rename" });
          else focusNameField();
        }}
      >
        <AlertDialogBackdrop />
        <RoomAlertContent className="import-conflict-dialog">
          <AlertDialogTitle>{conflict?.join(", ")}</AlertDialogTitle>
          <AlertDialogDescription>
            {conflict
              ? `Nothing was merged. This list already exists with different content: ${conflict.join(", ")}. Keep both under a new name, or go back and use a different name?`
              : ""}
          </AlertDialogDescription>
          <div className="settings-actions import-conflict-actions">
            <AlertDialogClose className="quiet-button" value="different">
              Use a different name
            </AlertDialogClose>
            <AlertDialogClose className="primary-action" value="rename">
              Keep both
            </AlertDialogClose>
          </div>
        </RoomAlertContent>
      </AlertDialog>
    </div>
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
          {isHost(store.settings) ? <Toggle
            className="quiet-button merge-toggle"
            pressed={store.mergeMode}
            onPressedChange={(pressed) => {
              if (pressed) store.enterMergeMode();
              else store.exitMergeMode();
            }}
          >
            Merge
          </Toggle> : null}
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
      {store.mergeMode ? <MergeBar /> : null}
      <div className="playlist-cards">
        {visible.map((playlist) => {
          const isSelected = store.mergeSelection.has(playlist.id);
          return (
            <button
              className={`playlist-card${store.mergeMode ? " is-selecting" : ""}${
                store.mergeMode && isSelected ? " is-selected" : ""
              }`}
              type="button"
              key={playlist.id}
              aria-pressed={store.mergeMode ? isSelected : undefined}
              onClick={() => {
                if (store.mergeMode) {
                  store.toggleMergeSelection(playlist.id);
                } else {
                  store.clearSearch();
                  store.setView("all", playlist.id);
                }
              }}
            >
              <span className="playlist-card-art" aria-hidden="true">
                <CoverArt cover={playlist.cover} fallback={playlist.name.trim().slice(0, 1)} />
              </span>
              {store.mergeMode ? (
                <span className="merge-check" aria-hidden="true">
                  {isSelected ? "✓" : ""}
                </span>
              ) : null}
              <strong title={playlist.name}>
                {playlist.name}
              </strong>
              <span>
                {readable(playlist.playable)} ready ·{" "}
                {readable(playlist.entries)}{" "}
                {playlist.entries === 1 ? "song" : "songs"}
                <SourceBadge playlist={playlist} />
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
}

function ExternalRow({ item }: { item: ExternalResult }) {
  const store = useStore();
  const ready = item.status === "verified";
  const byline = [
    item.artist,
    item.album,
    ready ? "Ready in library" : "",
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
      ) : (
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
  const missingShown = store.tracks.filter(
    (track) =>
      !["verified", "waiting_format", "queued", "searching", "waiting_space"].includes(
        track.status,
      ),
  );
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
