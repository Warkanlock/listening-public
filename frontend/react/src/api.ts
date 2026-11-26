import type { RequestState } from "./requestState";

export interface PlaylistSummary {
  id: string;
  name: string;
  entries: number;
  revision?: string;
  playable: number;
  cover: string;
  // Source classification (plan 046): "spotify" | "linked" |
  // "watched" | "local". Anything but "local" is kept in sync
  // from its source and wears the magnet badge.
  source?: string;
  can_add?: boolean;
  can_merge?: boolean;
  spotify_link?: {
    mode: "two_way" | "read_only" | "paused" | "disconnected" | "multiple";
    account_id?: string;
    playlist_id?: string;
    needs_write_permission?: boolean;
    targets?: { mode: string; playlist_id: string; account_id: string; name: string; needs_write_permission?: boolean }[];
  };
}

export interface MergeResult {
  file: string;
  name: string;
  tracks: number;
  added: number;
  skipped_rows: number;
  status: string;
}

export interface LibrarySummary {
  verified: number;
  attempted: number;
  unmatched: number;
  rejected: number;
  pending: number;
  playlists?: Record<string, { playable?: number }>;
  [key: string]: unknown;
}

export interface Overview {
  playlists: PlaylistSummary[];
  summary: LibrarySummary;
  total: number;
  favorites: number;
}

export interface Track {
  id: number;
  artist: string;
  title: string;
  album: string;
  genre: string;
  genre_canonical: string;
  genre_source: string;
  favorite: boolean;
  duration: number;
  cover: string;
  status: string;
  audio: string | null;
  spotify_url: string;
  reason: string;
  mp3_retry?: Mp3Retry;
}

export interface Mood {
  x: number;
  y: number;
  description: string;
  id: string;
  name: string;
  genres: string[];
}

export interface TracksPage {
  moods?: Mood[];
  ready_genres?: Record<string, number>;
  tracks: Track[];
  total: number;
  offset: number;
  limit: number;
  genres: string[];
}

export interface SessionTrack {
  id: number | string;
  title: string;
  artist: string;
  album?: string;
  cover: string;
  audio: string;
  duration: number;
  favorite: boolean;
  // Probe facts (plan 042 stage 6), present only when the probe
  // found them: the quality readout and ReplayGain normalization.
  codec?: string;
  sample_rate?: number;
  bit_depth?: number;
  track_gain?: number;
  album_gain?: number;
  track_peak?: number;
  album_peak?: number;
  r128_track_gain?: number;
}

export interface MetadataConnection {
  musicbrainz_id: string;
  relationship: string;
  title: string;
  artist: string;
}

export interface SongMetadata {
  title: string;
  artist: string;
  album?: string;
  first_release?: string;
  isrc?: string;
  genre?: string;
  genre_source_label?: string;
  genres?: string[];
  provenance?: string;
  lists?: string[];
  identifier?: string;
  source?: string;
  favorite?: boolean;
  connections?: MetadataConnection[];
  musicbrainz_url?: string;
  spotify_url?: string;
  play_count?: number;
  last_played?: number | null;
}

export interface LyricsResult {
  found: boolean;
  synced?: string;
  plain?: string;
}

export interface ActivityEvent {
  song: string;
  state: string;
  reason?: string;
  batch?: string;
}

export interface ActivityExplore {
  activity: Activity;
  summary: {
    verified: number;
    unmatched: number;
    rejected: number;
    pending: number;
    library_gib?: number;
    free_gib?: number;
    [key: string]: unknown;
  };
  events: ActivityEvent[];
  logs: string[];
  bulk_running: boolean;
  requests_running: boolean;
}

export interface Session {
  buffer_reconciliation?: boolean;
  version: number;
  occurrence?: number;
  current: SessionTrack | null;
  current_ref: unknown;
  upcoming: (SessionTrack | null)[];
  queue_length: number;
  shuffle: boolean;
  genre_flow: boolean;
  playing: boolean;
  session_playing?: boolean;
  output_selected?: boolean;
  outputs_explicit?: boolean;
  outputs?: { id: string; name: string; selected: boolean }[];
  position: number;
  parked_at?: number | null;
}

export type NearbySong = { title: string; artist: string };
export type JamInvite = { id: string; host: string; song: NearbySong };
export type NearbyListening = {
  available: boolean;
  sharing: boolean;
  hosting: boolean;
  people: { name: string; playing: boolean; song: NearbySong | null }[];
  invites: JamInvite[];
};

export const BULK_DOWNLOAD_FORMATS = {
  mp3_preferred: "MP3 first",
  mp3_only: "MP3 only",
  lossless_only: "Lossless only",
} as const;

export type BulkDownloadFormat = keyof typeof BULK_DOWNLOAD_FORMATS;

export function isBulkDownloadFormat(value: string | null): value is BulkDownloadFormat {
  return value !== null && Object.hasOwn(BULK_DOWNLOAD_FORMATS, value);
}

export const REQUEST_FORMATS = [
  { key: "any_format", label: "Any supported format (FLAC, ALAC, MP3 or AAC)" },
  { key: "any_lossless", label: "Any lossless (prefer FLAC)" },
  { key: "flac", label: "FLAC only" },
  { key: "alac", label: "Apple Lossless (ALAC) only" },
  { key: "any_lossy", label: "MP3 or AAC (prefer MP3)" },
  { key: "mp3", label: "MP3 only" },
] as const;

export type RequestFormat = (typeof REQUEST_FORMATS)[number]["key"];
export type CopyMatch = {
  key: string;
  label: string;
  format?: string;
  duration?: number;
  bitrate?: number;
  recommended?: boolean;
  failure?: string;
};
export type Mp3Retry = { id: number; format_revision: number };
export type RequestSong = { track_id: number } | { artist: string; title: string; musicbrainz_id?: string; version?: string; performance?: string; album?: string; duration?: number };

export interface RequestRow {
  request_state?: RequestState;
  musicbrainz_id?: string;
  original_recording_id?: string;
  version?: string;
  phase?: string;
  retry_at?: number;
  download_percent?: number;
  requested_format: RequestFormat | null;
  format_revision: number;
  mp3_retry?: Mp3Retry;
  copy_review?: boolean;
  id: number;
  track_index: number | null;
  artist: string;
  title: string;
  status: string;
  message: string;
  audio?: string;
  search_title?: string;
}

export type RecentDownload = Pick<RequestRow, "id" | "track_index" | "artist" | "title"> & { audio: string };
export type DownloadOutcome = Pick<RequestRow, "title" | "request_state"> & {
  key: string;
  status: "downloaded" | "not_found" | "rejected" | "error";
};

export interface WatchdogHealth {
  last_error: string;
  last_error_at: number;
  failures: number;
}

export interface Activity {
  status: string;
  batch: string;
  running: boolean;
  attempted: number;
  total: number;
  verified: number;
  queued_requests: number;
  recent?: { song: string }[];
  genres?: { running: boolean; categorized: number; total: number };
  watchdog?: WatchdogHealth;
}

export interface Announcement {
  id: number;
  sender: string;
  message: string;
  sent_at: number;
}

export interface SpotifyStatus {
  configured: boolean;
  connected: boolean;
  client_id: string;
  account: string;
  last_success: number | null;
  last_error: string;
  last_attempt: number | null;
  next_sync_at: number | null;
  saved_total: number;
  playlist_count: number;
  running: boolean;
  stage: string;
  interval_seconds: number;
}

// Where new downloads land (plan 060): the landing mode the
// audio_store setting, the snapshot, and the settings form share.
export type AudioStoreMode = "files" | "database";

export interface ExtensionSources {
  inventory_available: boolean;
  selection: { search?: string; fetch?: string } | null;
  installed: { id: string; version: string; enabled: boolean; ready: boolean; retry_at?: number;
    capabilities: ("search" | "fetch")[]; sources: Partial<Record<"search" | "fetch", string[]>> }[];
  retry_at?: number;
  state: "ready" | "legacy" | "unconfigured" | "unavailable" | "cooldown";
}

export type ExtensionHealth = {
  checks: { label: string; state: "ready" | "failed" | "unverified"; message: string }[];
};

export interface Settings {
  collection: { path: string; bytes: number; list_workspace: string; export_folder: string } | null;
  folder: string;
  lists_folder: string;
  csv_folder: string;
  source_folders: { path: string; count: number }[];
  source_files: { path: string; available: boolean }[];
  version: string;
  install_kind?: "checkout" | "app" | "brew" | "docker";
  managed_updates?: boolean;
  desktop_player_available?: boolean;
  frontend_rebuild_needed: boolean;
  music_folder: string;
  playlist_output: string;
  share_folder: string;
  share_username: string;
  sockseek_binary: string;
  nicotine_binary: string;
  has_credentials: boolean;
  has_share_credentials: boolean;
  download_ready: boolean;
  auto_download: boolean;
  bulk_download_format: BulkDownloadFormat;
  // Portable rooms always store downloads in their collection database.
  audio_store: AudioStoreMode;
  media: { tracks: number; audio_bytes: number; stored_bytes: number; dead_bytes: number } | null;
  export_status: { state: string; done: number; total: number; failed: number; written?: number; skipped?: number; destination?: string; stamp: number };
  list_files: string[];
  csv_files: string[];
  lan_url: string;
  can_invite?: boolean;
  pairing_network?: "home" | "tailscale" | "wireguard" | "lan" | "";
  local_url: string;
  onboarding: boolean;
  spotify: SpotifyStatus;
  spotify_redirect_uri: string;
  dependencies: Record<string, boolean>;
}

// The one body every settings form posts (vanilla settingsBody()):
// the three forms share it, so a save from any panel carries the
// values the panels currently show.
export interface SettingsBody {
  folder: string;
  lists_folder: string;
  music_folder: string;
  playlist_output: string;
  sockseek_binary: string;
  nicotine_binary: string;
  soulseek_username: string;
  soulseek_password: string;
  share_folder: string;
  share_username: string;
  share_password: string;
  auto_download: boolean;
  bulk_download_format: BulkDownloadFormat;
  audio_store: AudioStoreMode;
}

// Radio stations (plan 059): a genre name plus its seed videos,
// stored server-side and edited through the paired-tier stations
// endpoint. Seeds are always bare YouTube video IDs.
export interface RadioStation {
  name: string;
  seeds: string[];
}

export type LiveRadio = { title: string; url: string };
export type SomaFmSettings = {
  enabled: boolean;
  channels: Record<string, string>;
  stations: { name: string; channel: string }[];
};

export interface RadioLibraryTrack {
  id: number;
  title: string;
  artist: string;
  audio: string;
}

export interface RadioLibraryResult {
  tracks: RadioLibraryTrack[];
}

export interface StationsResult {
  youtube_enabled?: boolean;
  somafm_enabled?: boolean;
  live?: Record<string, LiveRadio>;
  ready_genres?: Record<string, number>;
  stations: RadioStation[];
}

export interface ProcessSnapshot {
  activity: Activity;
  bulk_running: boolean;
  requests_running: boolean;
  genres_running: boolean;
  share_running: boolean;
  auto_download: boolean;
  logs: string[];
}

export type JobKind =
  | "downloads"
  | "genres"
  | "requests"
  | "reclaim"
  | "migrate";

export interface JobScope {
  type: "all" | "list" | "library" | "queue";
  id?: string;
  name?: string;
  format?: BulkDownloadFormat;
}

export type DownloadJobScope = (
  | { type: "all" }
  | { type: "list"; id: string }
) & { format?: BulkDownloadFormat };

export interface JobCard {
  engines?: { search?: string; fetch?: string };
  kind: JobKind;
  running: boolean;
  state: "idle" | "queued" | "running" | "failed" | "done" | "cancelled";
  queue_position: number | null;
  error: string;
  started_at: number | null;
  scope: JobScope;
  progress: Record<string, number>;
  blocker: string;
  auto_download?: boolean;
  default_format?: BulkDownloadFormat;
  destination?: string;
}

export interface JobsResult {
  jobs: JobCard[];
  playlists: { id: string; name: string }[];
}

export interface DevicePresence {
  id: string;
  name: string;
  address: string;
  host: boolean;
}

export interface PairedDevice {
  offline_sync?: boolean;
  playlist_access?: boolean;
  id: string;
  name: string;
  paired_at: number;
  last_seen_at: number;
  present: boolean;
}

export interface DevicesResult {
  devices: DevicePresence[];
  paired_devices: PairedDevice[];
  server: string;
  manage?: boolean;
}

export interface DiscoverLocal {
  folders: { path: string; name: string; count: number }[];
  sockseek: string;
}

export interface BrowseEntry {
  name: string;
  path: string;
  kind: "folder" | "file";
}

export interface BrowseResult {
  path: string;
  parent: string;
  home: string;
  entries: BrowseEntry[];
}

export interface UpdateCheck {
  current: string;
  remote: string;
  behind: number;
  checked_at: number;
  applying?: string;
  update_error?: string;
}

export interface DiscoverTrack {
  request?: RequestRow | null;
  performance?: string;
  seed_rank?: number;
  seed_id?: string;
  artist_id?: string;
  version?: string;
  musicbrainz_id: string;
  title: string;
  artist: string;
  album?: string;
  duration?: number;
  reason?: string;
  genres?: string[];
  art_seed?: string;
  cover?: string;
  seed_artist?: string;
}

export interface Discover {
  week: string;
  weekly_edition?: string;
  source: string;
  status?: string;
  tracks: DiscoverTrack[];
}

export interface ExternalResult {
  title: string;
  artist: string;
  album?: string;
  catalog_id?: number;
  musicbrainz_id?: string;
  status?: string;
}

export class ApiError extends Error {
  data: unknown;
  constructor(message: string, data: unknown, readonly status: number) {
    super(message);
    this.data = data;
  }
}

let expectedAccount = "";

export function setExpectedAccount(account: string) {
  expectedAccount = account;
}

export const currentAccount = () => expectedAccount;

export const accountChangedKey = "listening-account-change";

export function announceAccountChange() {
  try {
    localStorage.setItem(accountChangedKey, String(Date.now()));
  } catch {
    // Request assertions still protect tabs where browser storage is unavailable.
  }
}

export function accountHeaders(path: string, init?: HeadersInit) {
  const headers = new Headers(init);
  if (expectedAccount && path.startsWith("/api/") && path !== "/api/account" && path !== "/api/account/login") {
    headers.set("X-Listening-Account", expectedAccount);
  }
  return headers;
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = accountHeaders(path, options.headers);
  const response = await fetch(path, { ...options, headers });
  const data = (await response.json()) as { error?: string; code?: string };
  if (!response.ok) {
    if (response.status === 409 && data.code === "account_changed") location.reload();
    throw new ApiError(data.error || `Request failed (${response.status})`, data, response.status);
  }
  return data as T;
}

export const readable = (number: number) => Number(number || 0).toLocaleString();

export const time = (seconds: number) => {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
};

export const coverUrl = (url: unknown) =>
  typeof url === "string" && url.startsWith("https://") ? url : "";

// The server sends its local URL only to owner requests. The browser address
// can be loopback even when a forwarded connection is a paired device.
export const isHost = (settings: Settings | null) => !!settings?.local_url;

export const scrollBehavior = (): ScrollBehavior =>
  matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";

const STATUS_LABELS: Record<string, string> = {
  verified: "Ready",
  downloaded: "Ready",
  unmatched: "Search missed",
  not_found: "No copy found",
  pending: "Not downloaded",
  rejected: "Needs review",
  queued: "Queued",
  searching: "Searching",
  waiting_space: "Waiting for space",
  waiting_format: "Waiting for format",
  error: "Search failed",
};

export const statusLabel = (status: string) => STATUS_LABELS[status] || status;

export const BUSY_STATUSES = ["queued", "searching", "waiting_space"];

export const shortPath = (path: string) => {
  const parts = String(path || "").split(/[\\/]/).filter(Boolean);
  return `${parts.length > 3 ? "…/" : ""}${parts.slice(-3).join("/")}`;
};

export const fileSize = (bytes: number) => {
  if (!bytes) return "0 KB";
  return bytes < 1024 * 1024
    ? `${Math.max(1, Math.round(bytes / 1024))} KB`
    : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

// One folder is configured (plan 043 addendum): the lists, audio,
// and playlist paths are derived from the Library folder. A subpath
// that lives inside the current Library folder keeps its relative
// name under the new root, so an existing room's legacy in-root
// names (library/, offline_playlists/) stay that room's derived
// names; anything else falls back to the standard names.
export const derivedStoragePaths = (settings: Settings | null, folder: string) => {
  const current = settings;
  const oldRoot = (current?.folder || "").replace(/\/+$/, "");
  const root = (folder || "").replace(/\/+$/, "");
  const derive = (value: string | undefined, fallback: string) => {
    if (value && oldRoot && value.startsWith(`${oldRoot}/`))
      return root + value.slice(oldRoot.length);
    if (value && root === oldRoot) return value;
    return `${root}/${fallback}`;
  };
  return {
    lists_folder: derive(current?.lists_folder, "lists"),
    music_folder: derive(current?.music_folder, "audio"),
    playlist_output: derive(current?.playlist_output, "playlists"),
  };
};

export const postJson = <T,>(path: string, body: unknown, signal?: AbortSignal): Promise<T> =>
  api<T>(path, {
    method: "POST",
    signal,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
