import { rememberMusicSearch } from "../recentMusicSearches";
import {
  api,
  canQueueTrackCopy,
  postJson,
  scrollBehavior,
  type DownloadFavorite,
  type ExternalResult,
  type FavoriteTrack,
  type Track,
  type Mood,
  type TracksPage,
} from "../api";
import { notify } from "../toasts";
import { isDestinationFilter, type TrackFilter, type View } from "../store";
import { errorMessage, type SliceHost } from "./host";

// Browsing (plan 056): the current view and playlist, the
// tracks table and its paging, the search text and genre tags,
// the Explore cluster, the shelf's expansion and merge mode,
// and the per-track request actions.
export class BrowsingSlice {
  view: View = "all";
  playlist = "all";
  filter: TrackFilter = "all";
  searchText = "";
  query = "";
  genres: string[] = [];
  genreOptions: string[] = [];
  moods: Mood[] = [];

  tracks: Track[] = [];
  downloadFavorites: DownloadFavorite[] = [];
  get loadedTrackCount() { return this.tracks.length + this.downloadFavorites.length; }
  tracksTotal = 0;
  tracksLoading = false;
  tracksResetting = false;
  tracksLoaded = false;
  tracksError: string | null = null;
  tracksPageError: string | null = null;

  externalResults: ExternalResult[] = [];
  externalQuery = "";
  resultsTab: "library" | "explore" = "library";
  externalStatus: "idle" | "loading" | "ready" | "error" = "idle";
  externalOpen = false;
  externalLoading = false;
  externalError: string | null = null;
  externalRevision = 0;

  playlistsExpanded = false;
  // Playlist merge (plan 046): client-only selection state for
  // the shelf's merge mode, never persisted. The server owns the
  // union; this only collects the choice.
  playlistFromMyPlaylists = false;
  requestingIds = new Set<number>();
  retryingVisible = false;
  searchingPlaylist: string | null = null;

  private loadSeq = 0;
  private loadedTrackScope: string | null = null;
  private loadingTrackScope: string | null = null;
  private searchTimer: ReturnType<typeof setTimeout> | undefined;

  constructor(private readonly host: SliceHost) {}

  async loadTracks(reset = false) {
    const scope = JSON.stringify([this.view, this.playlist, this.query, this.filter, this.genres]);
    if (this.tracksLoading && (!reset || (this.tracksResetting && this.loadingTrackScope === scope))) return;
    const keepVisible = reset && this.loadedTrackScope === scope;
    const visible = keepVisible ? this.loadedTrackCount : 0;
    this.tracksLoading = true;
    this.loadingTrackScope = scope;
    const sequence = ++this.loadSeq;
    if (reset && !keepVisible) {
      this.loadedTrackScope = null;
      this.tracks = [];
      this.downloadFavorites = [];
      this.tracksError = null;
      this.tracksPageError = null;
      this.tracksResetting = true;
    } else {
      this.tracksPageError = null;
    }
    this.host.bump();
    try {
      const params = new URLSearchParams({
        playlist: this.playlist,
        q: this.query,
        filter: this.filter,
        offset: String(reset ? 0 : this.loadedTrackCount),
        limit: this.view === "moods" ? "1" : String(Math.min(100, Math.max(60, visible))),
      });
      if (reset && this.view === "moods") {
        params.set("facets", "moods");
      }
      if (this.filter === "favorites") params.append("facets", "download-favorites");
      for (const genre of this.genres) params.append("genre", genre);
      const result = await api<TracksPage>(`/api/tracks?${params}`);
      const favorites = result.download_favorites ?? [];
      let loaded = result.tracks.length + favorites.length;
      while (sequence === this.loadSeq && loaded > 0 && loaded < Math.min(visible, result.total)) {
        params.set("offset", String(loaded));
        params.set("limit", String(Math.min(100, visible - loaded)));
        const page = await api<TracksPage>(`/api/tracks?${params}`);
        const count = page.tracks.length + (page.download_favorites?.length ?? 0);
        if (count === 0) break;
        result.tracks.push(...page.tracks);
        favorites.push(...(page.download_favorites ?? []));
        loaded += count;
      }
      if (sequence !== this.loadSeq) return;
      if (result.moods) this.moods = result.moods;
      if (result.genres) this.genreOptions = result.genres;
      this.tracks = reset ? result.tracks : [...this.tracks, ...result.tracks];
      this.downloadFavorites = reset ? favorites : [...this.downloadFavorites, ...favorites];
      this.tracksError = null;
      this.tracksTotal = result.total;
      this.tracksLoaded = true;
      this.loadedTrackScope = scope;
    } catch (error) {
      if (sequence !== this.loadSeq) return;
      const message = errorMessage(error, "Songs could not load");
      if (keepVisible) return;
      if (reset) this.tracksError = message;
      else this.tracksPageError = message;
      notify(message);
    } finally {
      if (sequence === this.loadSeq) {
        this.tracksLoading = false;
        this.loadingTrackScope = null;
        this.tracksResetting = false;
        this.host.bump();
      }
    }
  }

  setView(view: View, playlist = "all") {
    this.playlistFromMyPlaylists = view === "all" && playlist !== "all" && this.view === "private-playlists";
    // The sidebar destinations (Favorites, Your listening) are
    // places, not availability tabs: navigating anywhere else
    // leaves them behind (plan 048, plan 050 step 5).
    if (isDestinationFilter(this.filter)) this.filter = "all";
    this.view = view;
    if (view === "moods") {
      clearTimeout(this.searchTimer);
      this.playlist = "all";
      this.filter = "all";
      this.query = "";
      this.searchText = "";
      this.genres = [];
    }
    if (view === "all") this.playlist = playlist;
    if (view === "all" || view === "moods") void this.loadTracks(true);
    else if (view === "radio") void this.host.loadStations();
    else if (view === "requests") {
      this.host.resetRequestsSignature();
      void this.host.loadRequests();
    } else if (view === "jobs") {
      void this.host.refreshJobs();
    } else if (view === "discover") {
      void this.host.loadDiscover();
    } else if (view === "settings") {
      void this.host.loadSettings();
    }
    this.host.bump();
  }

  setFilter(filter: TrackFilter) {
    this.filter = filter;
    void this.loadTracks(true);
    this.host.bump();
  }

  setGenres(genres: string[]) {
    this.genres = genres;
    void this.loadTracks(true);
    this.host.bump();
  }

  setResultsTab(tab: "library" | "explore") {
    this.resultsTab = tab;
    this.host.bump();
  }

  clearSearch() {
    clearTimeout(this.searchTimer);
    this.searchText = "";
    this.query = "";
    this.externalQuery = "";
    this.externalOpen = false;
    this.externalResults = [];
    this.externalError = null;
    this.externalLoading = false;
    this.externalStatus = "idle";
    this.resultsTab = "library";
    this.host.bump();
  }

  typeSearch(text: string) {
    this.searchText = text;
    this.host.bump();
    clearTimeout(this.searchTimer);
    this.searchTimer = setTimeout(() => {
      const committed = text.trim();
      if (committed !== this.query && this.genres.length) {
        // A new search searches the whole library: a genre left
        // over from browsing must not silently narrow it (it read
        // as broken search, 2026-11-16).
        this.genres = [];
      }
      if (committed !== this.query && isDestinationFilter(this.filter)) {
        // The same rule for the sidebar destinations: a new
        // search searches the whole library, not only the
        // favorites or the play history.
        this.filter = "all";
      }
      this.query = committed;
      this.resultsTab = "library";
      this.externalStatus = "idle";
      this.externalOpen = false;
      this.externalQuery = "";
      this.externalResults = [];
      this.externalError = null;
      this.externalLoading = false;
      this.playlist = "all";
      if (this.view !== "all") this.setView("all");
      else {
        void this.loadTracks(true);
        this.host.bump();
      }
    }, 180);
  }

  async searchExternal() {
    clearTimeout(this.searchTimer);
    const query = this.searchText.trim();
    if (!query) {
      notify("Type a song, artist, or album to search.");
      return;
    }
    rememberMusicSearch(query);
    if (query !== this.query) {
      this.genres = [];
      if (isDestinationFilter(this.filter)) this.filter = "all";
    }
    this.query = query;
    this.playlist = "all";
    if (this.view !== "all") this.setView("all");
    else void this.loadTracks(true);
    this.externalQuery = query;
    this.externalResults = [];
    this.externalOpen = true;
    this.externalLoading = true;
    this.externalError = null;
    this.externalStatus = "loading";
    this.resultsTab = "explore";
    this.externalRevision += 1;
    this.host.bump();
    try {
      const result = await api<{ results: ExternalResult[] }>(
        `/api/search?${new URLSearchParams({ q: query })}`,
      );
      if (this.externalQuery !== query) return;
      this.externalResults = result.results;
      this.externalStatus = "ready";
    } catch (error) {
      if (this.externalQuery !== query) return;
      this.externalResults = [];
      this.externalError = errorMessage(error, "Catalog search is unavailable");
      this.externalStatus = "error";
    } finally {
      if (this.externalQuery === query) {
        this.externalLoading = false;
        this.host.bump();
      }
    }
  }

  clearExternal() {
    this.externalOpen = false;
    this.externalQuery = "";
    this.externalResults = [];
    this.externalError = null;
    this.externalLoading = false;
    this.externalStatus = "idle";
    this.resultsTab = "library";
    this.host.bump();
  }

  async toggleFavorite(track: FavoriteTrack): Promise<boolean> {
    // Optimistic flip on every surface holding the track (plan
    // 048): the tracks list, the session's current and upcoming,
    // and, through those same objects, the open song dialog. The
    // POST reconciles; a failure reverts and toasts.
    const target = typeof track.id === "number" ? { index: track.id }
      : track.request_id !== undefined ? { request_id: track.request_id } : null;
    if (!target) return false;
    const original = !!track.favorite;
    const next = !original;
    const flip = (favorite: boolean) => {
      for (const item of this.tracks) {
        if (item.id === track.id) item.favorite = favorite;
      }
      for (const item of this.downloadFavorites) {
        if (item.id === track.id) item.favorite = favorite;
      }
      this.host.setSessionFavorite(track.id, favorite);
      this.host.bump();
    };
    flip(next);
    try {
      const result = await postJson<{ favorite: boolean }>(
        "/api/favorites",
        { ...target, favorite: next },
      );
      if (result.favorite !== next) flip(result.favorite);
      if (this.filter === "favorites" && (!result.favorite || "request_id" in target)) {
        // An un-favorite leaves the favorites view: reload so the
        // row and the total stay truthful.
        void this.loadTracks(true);
      }
      void this.host.refreshOverview();
      return true;
    } catch (error) {
      flip(original);
      notify(errorMessage(error, "Favorite could not be saved"));
      return false;
    }
  }

  showFavorites() {
    this.showDestination("favorites");
  }

  showRecentlyPlayed() {
    this.showDestination("recent");
  }

  showMostPlayed() {
    this.showDestination("most");
  }

  private showDestination(filter: TrackFilter) {
    this.clearSearch();
    this.genres = [];
    this.view = "all";
    this.playlist = "all";
    this.filter = filter;
    void this.loadTracks(true);
    this.host.bump();
  }

  async requestTrack(id: number) {
    this.requestingIds.add(id);
    this.host.bump();
    try {
      const result = await this.host.createSongRequest({ track_id: id });
      const track = this.tracks.find((item) => item.id === id);
      if (track && result) track.status = result.status;
    } finally {
      this.requestingIds.delete(id);
      this.host.bump();
    }
  }

  async requestExternal(item: { title?: string; artist?: string; catalog_id?: number }) {
    if (!item?.title) return;
    const song = item.catalog_id
      ? { track_id: item.catalog_id }
      : { artist: item.artist || "", title: item.title };
    await this.host.createSongRequest(song);
  }

  async retryVisibleCopies() {
    const ids = this.tracks
      .filter(canQueueTrackCopy)
      .slice(0, 50)
      .map((track) => track.id);
    if (!ids.length || this.retryingVisible) return;
    this.retryingVisible = true;
    this.host.bump();
    try {
      const result = await postJson<{ added: number }>("/api/requests/batch", {
        track_ids: ids,
      });
      notify(`${result.added} broader searches queued`);
      void this.loadTracks(true);
      void this.host.loadRequests();
    } catch (error) {
      notify(errorMessage(error, "Request failed"));
    } finally {
      this.retryingVisible = false;
      this.host.bump();
    }
  }

  async findPlaylistCopies(playlist: string) {
    if (this.searchingPlaylist) return;
    this.searchingPlaylist = playlist;
    this.host.bump();
    try {
      const { added } = await postJson<{ added: number }>("/api/requests/batch", { playlist });
      notify(added ? `Queued ${added} ${added === 1 ? "copy search" : "copy searches"}` : "No new copy searches to queue");
      if (this.playlist === playlist) void this.loadTracks(true);
      void this.host.loadRequests();
    } catch (error) {
      notify(errorMessage(error, "Couldn’t search the playlist"));
    } finally {
      this.searchingPlaylist = null;
      this.host.bump();
    }
  }

  async openSource(ref: number | string) {
    try {
      const result = await postJson<{ status: string }>("/api/open-source", { ref });
      notify(result.status);
    } catch (error) {
      notify(errorMessage(error, "Could not open the file"));
    }
  }

  exploreLibrary(filter: TrackFilter, query = "") {
    this.host.setActivityDialogOpen(false);
    this.genres = [];
    clearTimeout(this.searchTimer);
    this.searchText = query;
    this.query = query;
    this.externalOpen = false;
    this.externalQuery = "";
    this.externalResults = [];
    this.externalError = null;
    this.view = "all";
    this.playlist = "all";
    this.filter = filter;
    void this.loadTracks(true);
    this.host.bump();
    requestAnimationFrame(() => {
      document
        .querySelector(".tracks-section")
        ?.scrollIntoView({ behavior: scrollBehavior(), block: "start" });
    });
  }

  togglePlaylistsExpanded() {
    this.playlistsExpanded = !this.playlistsExpanded;
    this.host.bump();
  }

}
