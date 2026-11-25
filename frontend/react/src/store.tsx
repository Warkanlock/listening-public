import type { SongInfoMode } from "./artistConnections";
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  api,
  ApiError,
  currentAccount,
  type Activity,
  type ActivityExplore,
  type Announcement,
  type DevicesResult,
  type Discover,
  type DiscoverTrack,
  type ExternalResult,
  type DownloadJobScope,
  type JobKind,
  type JobsResult,
  type Overview,
  type ProcessSnapshot,
  type RadioStation,
  type RequestRow,
  type RequestSong,
  type Session,
  type Settings,
  type SettingsBody,
  type SpotifyStatus,
  type Track,
} from "./api";
import type { DestinationFilter } from "./destinations";
import type { ArtworkSubject } from "./components/Artwork";
import type { LyricsSubject } from "./components/LyricsStage";
import { notify } from "./toasts";
import { startPolls } from "./polls";
import { errorMessage, type SliceHost } from "./slices/host";
import { SessionSlice } from "./slices/session";
import { RequestsSlice } from "./slices/requests";
import { DiscoverSlice } from "./slices/discover";
import { ActivitySlice } from "./slices/activity";
import { AnnouncementsSlice } from "./slices/announcements";
import { JobsSlice } from "./slices/jobs";
import { SettingsSlice } from "./slices/settings";
import { BrowsingSlice } from "./slices/browsing";
import {
  RadioSlice,
  type RadioControls,
  type RadioStatus,
} from "./slices/radio";

type RoomConnection = "connecting" | "catalog_loading" | "ready" | "unreachable" | "pairing_required";

export type View =
  | "all"
  | "discover"
  | "browse"
  | "moods"
  | "activities"
  | "my-devices"
  | "requests"
  | "jobs"
  | "extensions"
  | "settings"
  | "private-playlists"
  | "radio"
  | "hiking"
  | "insights";
export type TrackFilter =
  | "all"
  | "ready"
  | "missing"
  | "favorites"
  | "recent"
  | "most";

// Sidebar destinations (plan 048, plan 050 step 5): filters that
// are places of their own, not availability tabs. Navigating
// anywhere else, or committing a new search, leaves them behind.
export const DESTINATION_FILTERS: TrackFilter[] = [
  "favorites",
  "recent",
  "most",
];
export const isDestinationFilter = (
  filter: TrackFilter,
): filter is DestinationFilter => DESTINATION_FILTERS.includes(filter);

export const deviceId =
  localStorage.getItem("listening-device-id") ||
  // Pairing works on a private HTTP origin, where randomUUID is unavailable.
  Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
localStorage.setItem("listening-device-id", deviceId);

// Item 8's capture half: the prompt event is held here from first
// paint; the Settings install card (Stage 4) consumes it.
let installPrompt: Event | null = null;
window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  installPrompt = event;
});
export const peekInstallPrompt = () => installPrompt;
export const takeInstallPrompt = () => {
  const prompt = installPrompt;
  installPrompt = null;
  return prompt;
};

const overviewSignature = (overview: Overview) =>
  JSON.stringify({
    playlists: overview.playlists,
    summary: overview.summary,
    total: overview.total,
    favorites: overview.favorites,
  });

export class Store implements SliceHost {
  onChange: (() => void) | null = null;

  constructor(readonly member = false) {}

  readonly sessionSlice: SessionSlice = new SessionSlice(this);
  readonly requestsSlice: RequestsSlice = new RequestsSlice(this);
  readonly discoverSlice: DiscoverSlice = new DiscoverSlice(this);
  readonly activitySlice: ActivitySlice = new ActivitySlice(this);
  readonly announcementsSlice: AnnouncementsSlice = new AnnouncementsSlice(this);
  readonly jobsSlice: JobsSlice = new JobsSlice(this);
  readonly settingsSlice: SettingsSlice = new SettingsSlice(this);
  readonly browsingSlice: BrowsingSlice = new BrowsingSlice(this);
  readonly radioSlice: RadioSlice = new RadioSlice(this);

  overview: Overview | null = null;
  get view(): View { return this.browsingSlice.view; }
  set view(value: View) { this.browsingSlice.view = value; }
  get playlist(): string { return this.browsingSlice.playlist; }
  set playlist(value: string) { this.browsingSlice.playlist = value; }
  get filter(): TrackFilter { return this.browsingSlice.filter; }
  set filter(value: TrackFilter) { this.browsingSlice.filter = value; }
  get searchText(): string { return this.browsingSlice.searchText; }
  set searchText(value: string) { this.browsingSlice.searchText = value; }
  get query(): string { return this.browsingSlice.query; }
  set query(value: string) { this.browsingSlice.query = value; }
  get genres(): string[] { return this.browsingSlice.genres; }
  set genres(value: string[]) { this.browsingSlice.genres = value; }
  get genreOptions(): string[] { return this.browsingSlice.genreOptions; }
  set genreOptions(value: string[]) { this.browsingSlice.genreOptions = value; }

  get tracks(): Track[] { return this.browsingSlice.tracks; }
  set tracks(value: Track[]) { this.browsingSlice.tracks = value; }
  get tracksTotal(): number { return this.browsingSlice.tracksTotal; }
  set tracksTotal(value: number) { this.browsingSlice.tracksTotal = value; }
  get tracksLoading(): boolean { return this.browsingSlice.tracksLoading; }
  set tracksLoading(value: boolean) { this.browsingSlice.tracksLoading = value; }
  get tracksResetting(): boolean { return this.browsingSlice.tracksResetting; }
  set tracksResetting(value: boolean) { this.browsingSlice.tracksResetting = value; }
  get tracksLoaded(): boolean { return this.browsingSlice.tracksLoaded; }
  set tracksLoaded(value: boolean) { this.browsingSlice.tracksLoaded = value; }
  get tracksError(): string | null { return this.browsingSlice.tracksError; }
  set tracksError(value: string | null) { this.browsingSlice.tracksError = value; }
  get tracksPageError(): string | null { return this.browsingSlice.tracksPageError; }
  set tracksPageError(value: string | null) { this.browsingSlice.tracksPageError = value; }

  get parkedSongAt() { return this.session?.parked_at ?? 0; }
  get session(): Session | null { return this.sessionSlice.session; }
  set session(value: Session | null) { this.sessionSlice.session = value; }
  get requestRows(): RequestRow[] { return this.requestsSlice.requestRows; }
  set requestRows(value: RequestRow[]) { this.requestsSlice.requestRows = value; }
  get requestsLoaded(): boolean { return this.requestsSlice.requestsLoaded; }
  set requestsLoaded(value: boolean) { this.requestsSlice.requestsLoaded = value; }
  get requestsError(): string | null { return this.requestsSlice.requestsError; }
  set requestsError(value: string | null) { this.requestsSlice.requestsError = value; }
  get discover(): Discover | null { return this.discoverSlice.discover; }
  set discover(value: Discover | null) { this.discoverSlice.discover = value; }
  get discoverState(): "idle" | "loading" | "ready" | "building" | "error" | "failed" { return this.discoverSlice.discoverState; }
  set discoverState(value: "idle" | "loading" | "ready" | "building" | "error" | "failed") { this.discoverSlice.discoverState = value; }
  get discoverError(): string | null { return this.discoverSlice.discoverError; }
  set discoverError(value: string | null) { this.discoverSlice.discoverError = value; }
  get activity(): Activity | null { return this.activitySlice.activity; }
  set activity(value: Activity | null) { this.activitySlice.activity = value; }
  get activityFailed(): boolean { return this.activitySlice.activityFailed; }
  set activityFailed(value: boolean) { this.activitySlice.activityFailed = value; }
  get downloadActivity(): ActivityExplore | null { return this.activitySlice.downloadActivity; }
  set downloadActivity(value: ActivityExplore | null) { this.activitySlice.downloadActivity = value; }
  get downloadActivityError(): string | null { return this.activitySlice.downloadActivityError; }
  set downloadActivityError(value: string | null) { this.activitySlice.downloadActivityError = value; }
  get sessionFailed(): boolean { return this.sessionSlice.sessionFailed; }
  set sessionFailed(value: boolean) { this.sessionSlice.sessionFailed = value; }
  get audioJoiner(): (() => boolean) | null { return this.sessionSlice.audioJoiner; }
  set audioJoiner(value: (() => boolean) | null) { this.sessionSlice.audioJoiner = value; }
  get crossfade(): number { return this.sessionSlice.crossfade; }
  set crossfade(value: number) { this.sessionSlice.crossfade = value; }
  get compactAudio(): boolean { return this.sessionSlice.compactAudio; }
  setCompactAudio(on: boolean) { this.sessionSlice.setCompactAudio(on); }
  get normalize(): boolean { return this.sessionSlice.normalize; }
  set normalize(value: boolean) { this.sessionSlice.normalize = value; }

  setCrossfade(seconds: number) { this.sessionSlice.setCrossfade(seconds); }
  setNormalize(on: boolean) { this.sessionSlice.setNormalize(on); }

  get stations(): RadioStation[] { return this.radioSlice.stations; }
  get stationsLoaded(): boolean { return this.radioSlice.stationsLoaded; }
  get stationsError(): string | null { return this.radioSlice.stationsError; }
  get radioStatus(): RadioStatus { return this.radioSlice.status; }
  get radioStationName(): string | null { return this.radioSlice.stationName; }
  get radioVideoTitle(): string { return this.radioSlice.videoTitle; }
  get radioVideoChannel(): string { return this.radioSlice.videoChannel; }
  get radioVideoId(): string { return this.radioSlice.videoId; }
  get radioFailedStation(): string { return this.radioSlice.failedStation; }
  get radioStarted(): boolean { return this.radioSlice.started; }
  get radioVolume(): number { return this.radioSlice.volume; }
  get radioSounding(): boolean {
    const session = this.session;
    const roomPlaying = session?.current && (session.session_playing ?? session.playing);
    return this.radioSlice.sounding && (this.radioSlice.status !== "paused" || !roomPlaying);
  }
  get radioControls(): RadioControls | null { return this.radioSlice.controls; }
  set radioControls(value: RadioControls | null) { this.radioSlice.controls = value; }

  async loadStations() { await this.radioSlice.loadStations(); }
  saveStations(stations: RadioStation[]): Promise<boolean> {
    return this.radioSlice.saveStations(stations);
  }
  startStation(name: string, videos?: string[]) { this.radioSlice.startStation(name, videos); }
  toggleRadio() { this.radioSlice.toggleRadio(); }
  nextRadio() { this.radioSlice.nextRadio(); }
  retuneRadio() { this.radioSlice.retuneRadio(); }
  stopRadio(clearShared = true) { this.radioSlice.stopRadio(clearShared); }
  setRadioVolume(value: number) { this.radioSlice.setVolume(value); }
  artworkSubject: ArtworkSubject | null = null;
  artworkTrigger: HTMLElement | null = null;
  get processSnapshot(): ProcessSnapshot | null { return this.jobsSlice.processSnapshot; }
  set processSnapshot(value: ProcessSnapshot | null) { this.jobsSlice.processSnapshot = value; }
  get jobs(): JobsResult | null { return this.jobsSlice.jobs; }
  set jobs(value: JobsResult | null) { this.jobsSlice.jobs = value; }
  get jobsError(): string | null { return this.jobsSlice.jobsError; }
  set jobsError(value: string | null) { this.jobsSlice.jobsError = value; }
  get jobActionError(): { kind: JobKind; message: string } | null { return this.jobsSlice.jobActionError; }
  set jobActionError(value: { kind: JobKind; message: string } | null) { this.jobsSlice.jobActionError = value; }
  get spotifyStatus(): SpotifyStatus | null { return this.settingsSlice.spotifyStatus; }
  set spotifyStatus(value: SpotifyStatus | null) { this.settingsSlice.spotifyStatus = value; }
  get devicesResult(): DevicesResult | null { return this.settingsSlice.devicesResult; }
  set devicesResult(value: DevicesResult | null) { this.settingsSlice.devicesResult = value; }
  get settings(): Settings | null { return this.settingsSlice.settings; }
  set settings(value: Settings | null) { this.settingsSlice.settings = value; }
  get settingsError(): string | null { return this.settingsSlice.settingsError; }
  set settingsError(value: string | null) { this.settingsSlice.settingsError = value; }

  get announcements(): Announcement[] { return this.announcementsSlice.announcements; }
  set announcements(value: Announcement[]) { this.announcementsSlice.announcements = value; }
  get announcementsReady(): boolean { return this.announcementsSlice.announcementsReady; }
  set announcementsReady(value: boolean) { this.announcementsSlice.announcementsReady = value; }
  get lastSeenAnnouncement(): number { return this.announcementsSlice.lastSeenAnnouncement; }
  set lastSeenAnnouncement(value: number) { this.announcementsSlice.lastSeenAnnouncement = value; }
  get announcementsOpen(): boolean { return this.announcementsSlice.announcementsOpen; }
  set announcementsOpen(value: boolean) { this.announcementsSlice.announcementsOpen = value; }

  get externalResults(): ExternalResult[] { return this.browsingSlice.externalResults; }
  set externalResults(value: ExternalResult[]) { this.browsingSlice.externalResults = value; }
  get externalQuery(): string { return this.browsingSlice.externalQuery; }
  set externalQuery(value: string) { this.browsingSlice.externalQuery = value; }
  get resultsTab(): "library" | "explore" { return this.browsingSlice.resultsTab; }
  set resultsTab(value: "library" | "explore") { this.browsingSlice.resultsTab = value; }
  get externalStatus(): "idle" | "loading" | "ready" | "error" { return this.browsingSlice.externalStatus; }
  set externalStatus(value: "idle" | "loading" | "ready" | "error") { this.browsingSlice.externalStatus = value; }
  get externalOpen(): boolean { return this.browsingSlice.externalOpen; }
  set externalOpen(value: boolean) { this.browsingSlice.externalOpen = value; }
  get externalLoading(): boolean { return this.browsingSlice.externalLoading; }
  set externalLoading(value: boolean) { this.browsingSlice.externalLoading = value; }
  get externalError(): string | null { return this.browsingSlice.externalError; }
  set externalError(value: string | null) { this.browsingSlice.externalError = value; }
  get externalRevision(): number { return this.browsingSlice.externalRevision; }
  set externalRevision(value: number) { this.browsingSlice.externalRevision = value; }

  get playlistsExpanded(): boolean { return this.browsingSlice.playlistsExpanded; }
  set playlistsExpanded(value: boolean) { this.browsingSlice.playlistsExpanded = value; }
  get playlistFromMyPlaylists(): boolean { return this.browsingSlice.playlistFromMyPlaylists; }
  get queueOpen(): boolean {
    return (this.view !== "moods" || this.queuePreference !== null) && this.sessionSlice.queueOpen;
  }
  set queueOpen(value: boolean) { this.sessionSlice.queueOpen = value; }
  get queuePreference(): string | null { return this.sessionSlice.queuePreference; }
  set queuePreference(value: string | null) { this.sessionSlice.queuePreference = value; }
  get activityDialogOpen(): boolean { return this.activitySlice.activityDialogOpen; }
  set activityDialogOpen(value: boolean) { this.activitySlice.activityDialogOpen = value; }
  playerLayer: "now-playing" | "devices" | null = null;
  playerTrigger: HTMLElement | null = null;
  setPlayerLayer(layer: "now-playing" | "devices" | null, trigger?: HTMLElement | null) {
    if (layer) this.playerTrigger = trigger ?? null;
    this.playerLayer = layer;
    this.bump();
  }

  async openRoomPlayer(ref: number | string, trigger: HTMLElement | null, playlist = "all"): Promise<void> {
    if (this.session?.current_ref !== ref) {
      const started = typeof ref === "number"
        ? await this.sendSession("play", { track_id: ref, playlist })
        : await this.sendSession("play_request", { request_id: Number(ref.slice("custom-".length)) });
      if (!started) return;
    }
    this.setPlayerLayer("now-playing", trigger);
  }

  shortcutsOpen = false;
  songInfoId: number | string | null = null;
  songInfoTrack: DiscoverTrack | null = null;
  songInfoMode: SongInfoMode = "details";
  private songInfoReturnFocus: HTMLElement | null = null;
  // Lyrics stage subject (plan 049): the song whose lyrics fill the stage.
  lyricsSubject: LyricsSubject | "room" | "radio" | null = null;
  openPlaybackLyrics(source: "room" | "radio") {
    this.lyricsSubject = source;
    this.bump();
  }
  spotifyDestination: { playlist: string; account: string } | null = null;
  get settingsTab(): string { return this.settingsSlice.settingsTab; }
  set settingsTab(value: string) { this.settingsSlice.settingsTab = value; }
  get deviceName(): string { return this.settingsSlice.deviceName; }
  set deviceName(value: string) { this.settingsSlice.deviceName = value; }
  topbarFailed = false;
  get requestingIds(): Set<number> { return this.browsingSlice.requestingIds; }
  set requestingIds(value: Set<number>) { this.browsingSlice.requestingIds = value; }
  get retryingVisible(): boolean { return this.browsingSlice.retryingVisible; }
  set retryingVisible(value: boolean) { this.browsingSlice.retryingVisible = value; }

  private renderedOverviewSignature: string | null = null;
  private overviewSequence = 0;

  bump() {
    this.onChange?.();
  }

  get current(): Session["current"] {
    return this.sessionSlice.current;
  }

  setSessionFavorite(id: number, favorite: boolean) {
    this.sessionSlice.setFavorite(id, favorite);
  }

  resetRequestsSignature() {
    this.requestsSlice.resetRequestsSignature();
  }

  get requestsBadge(): number {
    return this.requestsSlice.requestsBadge;
  }

  get unreadAnnouncements(): number {
    return this.announcementsSlice.unreadAnnouncements;
  }

  async refreshOverview() {
    const sequence = ++this.overviewSequence;
    const next = await api<Overview>("/api/overview");
    if (sequence !== this.overviewSequence) return;
    const previous = this.overview;
    const signature = overviewSignature(next);
    if (signature !== this.renderedOverviewSignature) {
      this.overview = next;
      this.renderedOverviewSignature = signature;
      this.bump();
    }
    if (!previous) return;
    const coverageChanged = previous.summary.attempted !== next.summary.attempted || previous.summary.verified !== next.summary.verified;
    const before = previous.playlists.find(list => list.id === this.playlist);
    const after = next.playlists.find(list => list.id === this.playlist);
    const membershipChanged = before?.revision !== after?.revision || before?.entries !== after?.entries;
    if ((this.view === "all" || this.view === "moods") && (coverageChanged || membershipChanged || previous.total !== next.total))
      await this.loadTracks(true);
    if (coverageChanged && this.view === "requests") void this.loadRequests();
  }

  loadRequests() { return this.requestsSlice.loadRequests(); }
  createSongRequest(song: RequestSong): Promise<RequestRow | null> {
    return this.requestsSlice.createSongRequest(song);
  }
  retryRequest(id: number, searchTitle?: string) {
    return this.requestsSlice.retryRequest(id, searchTitle);
  }
  playRequest(row: RequestRow) { this.requestsSlice.playRequest(row); }

  loadDiscover() { return this.discoverSlice.loadDiscover(); }

  refreshActivity() { return this.activitySlice.refreshActivity(); }
  refreshDownloadExplorer() { return this.activitySlice.refreshDownloadExplorer(); }

  pollSession() { return this.sessionSlice.pollSession(); }
  applySession(session: Session | null) { this.sessionSlice.applySession(session); }
  sendSession(
    op: string,
    fields: Record<string, unknown> = {},
    successMessage = "",
  ): Promise<boolean> {
    return this.sessionSlice.sendSession(op, fields, successMessage);
  }
  togglePlayback() { this.sessionSlice.togglePlayback(); }

  exploreLibrary(filter: TrackFilter, query = "") {
    this.browsingSlice.exploreLibrary(filter, query);
  }

  openArtwork(subject: ArtworkSubject, trigger?: HTMLElement | null) {
    this.artworkSubject = subject;
    this.artworkTrigger = trigger ?? null;
    this.bump();
  }

  closeArtwork() {
    this.artworkSubject = null;
    this.bump();
    const trigger = this.artworkTrigger;
    this.artworkTrigger = null;
    trigger?.focus?.();
  }

  closeSongInfo() {
    const trigger = this.songInfoReturnFocus;
    this.songInfoId = null;
    this.songInfoTrack = null;
    this.songInfoReturnFocus = null;
    this.bump();
    if (trigger) requestAnimationFrame(() => trigger.focus({ preventScroll: true }));
  }

  openLyrics(subject: LyricsSubject) {
    this.lyricsSubject = subject;
    this.bump();
  }

  closeLyrics() {
    this.lyricsSubject = null;
    this.bump();
  }

  get playbackPresenceActive(): boolean { return this.sessionSlice.presenceActive; }
  get playbackOutputHeaders(): Record<string, string> { return this.sessionSlice.outputHeaders; }
  wakePlayback(): boolean {
    const wasActive = this.playbackPresenceActive;
    if (!this.sessionSlice.resumePresence()) return false;
    if (!wasActive && !this.member) void this.settingsSlice.heartbeat();
    return true;
  }
  async heartbeat() {
    await this.settingsSlice.heartbeat();
    if (!document.hidden && this.view === "settings" && this.settingsTab === "devices") {
      await this.settingsSlice.refreshDevices();
    }
  }

  pollAnnouncements() { return this.announcementsSlice.pollAnnouncements(); }

  refreshProcess() { return this.jobsSlice.refreshProcess(); }

  refreshSpotifyStatus() { return this.settingsSlice.refreshSpotifyStatus(); }
  setSpotifyStatus(status: SpotifyStatus) { this.settingsSlice.setSpotifyStatus(status); }
  loadSettings() { return this.settingsSlice.loadSettings(); }
  saveSettings(body: SettingsBody): Promise<boolean> {
    return this.settingsSlice.saveSettings(body);
  }
  changeSource(
    op: "watch" | "unwatch" | "link" | "unlink",
    path: string,
  ): Promise<boolean> {
    return this.settingsSlice.changeSource(op, path);
  }
  syncSources() { return this.settingsSlice.syncSources(); }

  refreshJobs() { return this.jobsSlice.refreshJobs(); }
  runJob(
    kind: JobKind,
    action: "start" | "stop",
    scope?: DownloadJobScope,
  ): Promise<boolean> {
    return this.jobsSlice.runJob(kind, action, scope);
  }
  runWorkerAction(action: string): Promise<boolean> {
    return this.jobsSlice.runWorkerAction(action);
  }

  setSettingsTab(tab: string) { this.settingsSlice.setSettingsTab(tab); }
  openRadioSettings() {
    this.setSettingsTab("radio");
    this.setView("settings");
  }
  saveDeviceName(name: string) { this.settingsSlice.saveDeviceName(name); }

  loadTracks(reset = false) { return this.browsingSlice.loadTracks(reset); }
  setView(view: View, playlist = "all") { this.browsingSlice.setView(view, playlist); }
  setFilter(filter: TrackFilter) { this.browsingSlice.setFilter(filter); }
  setGenres(genres: string[]) { this.browsingSlice.setGenres(genres); }
  setResultsTab(tab: "library" | "explore") { this.browsingSlice.setResultsTab(tab); }
  clearSearch() { this.browsingSlice.clearSearch(); }
  typeSearch(text: string) { this.browsingSlice.typeSearch(text); }
  searchExternal() { return this.browsingSlice.searchExternal(); }
  clearExternal() { this.browsingSlice.clearExternal(); }
  toggleFavorite(track: {
    id: number;
    title: string;
    favorite: boolean;
  }): Promise<boolean> {
    return this.browsingSlice.toggleFavorite(track);
  }
  showFavorites() { this.browsingSlice.showFavorites(); }
  showRecentlyPlayed() { this.browsingSlice.showRecentlyPlayed(); }
  showMostPlayed() { this.browsingSlice.showMostPlayed(); }
  requestTrack(id: number) { return this.browsingSlice.requestTrack(id); }
  requestExternal(item: { title?: string; artist?: string; catalog_id?: number }) {
    return this.browsingSlice.requestExternal(item);
  }
  retryVisibleCopies() { return this.browsingSlice.retryVisibleCopies(); }
  openSource(ref: number | string) { return this.browsingSlice.openSource(ref); }
  togglePlaylistsExpanded() { this.browsingSlice.togglePlaylistsExpanded(); }

  setQueueOpen(open: boolean, remember = true) {
    this.sessionSlice.setQueueOpen(open, remember);
  }

  setActivityDialogOpen(open: boolean) {
    this.activitySlice.setActivityDialogOpen(open);
  }

  setShortcutsOpen(open: boolean) {
    this.shortcutsOpen = open;
    this.bump();
  }

  setAnnouncementsOpen(open: boolean) {
    this.announcementsSlice.setAnnouncementsOpen(open);
  }

  openSongInfo(song: number | string | DiscoverTrack, mode: SongInfoMode = "details", returnFocus?: HTMLElement | null) {
    const id = typeof song === "object" ? song.musicbrainz_id : song;
    if (typeof song === "object") this.songInfoTrack = song;
    else if (id !== this.songInfoId) this.songInfoTrack = null;
    if (this.songInfoId === null || returnFocus) this.songInfoReturnFocus = returnFocus ?? null;
    this.songInfoId = id;
    this.songInfoMode = mode;
    this.bump();
  }

  async startup() {
    // The Spotify OAuth return (item 25): the callback redirects to
    // /?spotify=connected|error; vanilla start() announces it, opens
    // Settings on the Connections tab, and strips the query.
    const params = new URLSearchParams(location.search);
    const spotifyReturn = params.get("spotify");
    if (spotifyReturn) {
      history.replaceState(null, "", location.pathname);
      notify(
        spotifyReturn === "connected"
          ? "Spotify connected. Your library is syncing."
          : "Spotify connection needs attention in Settings.",
      );
      this.settingsTab = "spotify";
      this.view = "settings";
    } else if (["clients", "room", "radio"].includes(params.get("setup") ?? "")) {
      const setup = params.get("setup");
      this.settingsTab = setup === "radio" ? "radio" : setup === "clients" ? "clients" : "setup";
      this.view = "settings";
    } else {
      const search = (params.get("q") ?? "").trim().slice(0, 200);
      this.browsingSlice.searchText = search;
      this.browsingSlice.query = search;
    }
    await this.checkRoom();
  }

  roomConnection: RoomConnection = "connecting";
  private checkingRoom = false;
  private registeringRoom = true;

  async checkRoom() {
    if (this.checkingRoom || this.roomConnection === "pairing_required") return;
    this.checkingRoom = true;
    const previous = this.roomConnection;
    let state: RoomConnection = "unreachable";
    const controller = new AbortController();
    const { signal } = controller;
    const deadline = window.setTimeout(() => controller.abort(), 6500);
    try {
      // A bootstrap cookie can register the device but cannot read its catalog.
      if (this.registeringRoom && !this.member) {
        try {
          if (this.playbackPresenceActive) await this.settingsSlice.registerDevice(signal);
        } catch (error) {
          // A cookie can still read the room if browser storage lost its device ID.
          if (!(error instanceof ApiError && error.status === 403)) throw error;
        }
      }
      const info = await api<{ state: string }>("/api/room", { cache: "no-store", signal });
      state = info.state === "ready" || info.state === "catalog_loading" ? info.state : "unreachable";
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) state = "pairing_required";
    } finally {
      window.clearTimeout(deadline);
      this.checkingRoom = false;
    }
    this.registeringRoom = state === "unreachable";
    if (state === "pairing_required") {
      this.sessionSlice.clearBuffer();
      this.sessionSlice.session = null;
      this.sessionSlice.queueEpoch++;
    }
    this.roomConnection = state;
    if (state !== previous) this.bump();
    if (state === "ready") this.radioSlice.retryHistory();
    if (state === "ready" && previous !== "ready") void this.loadRoom();
  }

  private async loadRoom() {
    const results = await Promise.allSettled([
      this.refreshOverview(),
      this.pollSession().then(async () => {
        if (currentAccount() === "owner") await this.sessionSlice.restoreParkedOwnerQueue();
        await this.radioSlice.refreshHistory();
      }),
      this.loadSettings(),
      ...this.member ? [] : [this.loadRequests(), this.refreshActivity(), this.pollAnnouncements(), this.loadStations()],
    ]);
    const failed = results.find((result) => result.status === "rejected");
    if (failed) {
      this.topbarFailed = true;
      notify(
        errorMessage(
          (failed as PromiseRejectedResult).reason,
          "Could not load part of your room",
        ),
      );
    }
    await this.loadTracks(true);
    if (!this.member && this.settings?.onboarding && this.view !== "extensions") this.view = "settings";
    this.queueOpen =
      this.queuePreference === null
        ? innerWidth >= 1250
        : this.queuePreference === "true";
    this.bump();
  }
}

interface StoreContextValue {
  store: Store;
  tick: number;
}

const StoreContext = createContext<StoreContextValue | null>(null);

export function StoreProvider({ children, member = false }: { children: ReactNode; member?: boolean }) {
  const [store] = useState(() => new Store(member));
  const [tick, setTick] = useState(0);

  useEffect(() => {
    store.onChange = () => setTick((value) => value + 1);
    void store.startup();
    const stopPolling = startPolls(store);
    const media = matchMedia("(min-width: 1250px)");
    const onMedia = (event: MediaQueryListEvent) => {
      if (store.queuePreference === null) store.setQueueOpen(event.matches, false);
    };
    media.addEventListener("change", onMedia);
    return () => {
      media.removeEventListener("change", onMedia);
      stopPolling();
    };
  }, [store]);

  const value = useMemo(() => ({ store, tick }), [store, tick]);
  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>;
}

export function useStore(): Store {
  const context = useContext(StoreContext);
  if (!context) throw new Error("useStore must be used inside StoreProvider");
  return context.store;
}
