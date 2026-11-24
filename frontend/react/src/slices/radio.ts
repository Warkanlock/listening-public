import {
  ApiError,
  api,
  currentAccount,
  postJson,
  type LiveRadio,
  type RadioLibraryResult,
  type RadioLibraryTrack,
  type RadioStation,
  type StationsResult,
} from "../api";
import type { NoiseProfile } from "../radioNoise";
import { notify } from "../toasts";
import { errorMessage, type SliceHost } from "./host";
import {
  parseRadioHistory, readRadioClear, readRadioHistory, saveRadioClear, saveRadioHistory, type RadioHistory,
} from "./radioHistory";

// Radio (plan 059): the stations list, this device's radio state,
// and the bridge to the view's YouTube engine. Radio is per-device
// (ADR 0003): the stations are the room's (server-side, paired
// tier), but what sounds, and at what volume, is this device's
// alone. The engine registers its controls the way the session
// engine registers the audio joiner; the slice owns every state
// the surfaces render.
export type RadioStatus = "idle" | "starting" | "playing" | "paused" | "failed";

export type RadioMix =
  | { kind: "youtube"; name: string; seeds: string[] }
  | { kind: "library"; name: string; tracks: RadioLibraryTrack[] }
  | { kind: "live"; name: string; stream: LiveRadio }
  | { kind: "noise"; name: string; profile: NoiseProfile };

type RadioTuning = {
  id: string;
  claim: "new" | "unconfirmed" | "confirmed" | "superseded";
  baseRevision: number;
};

export interface RadioControls {
  start(mix: RadioMix, first?: string | number, position?: number): void;
  checkpoint(): void;
  libraryTime(id: number): number | null;
  seekLibrary(id: number, seconds: number): void;
  prepareLibrary(): void;
  toggle(): void;
  next(): void;
  previous(): void;
  retune(): void;
  stop(): void;
  setVolume(value: number): void;
}

const storedVolume = Number(localStorage.getItem("listening-radio-volume") ?? 80);

export class RadioSlice {
  stations: RadioStation[] = [];
  libraryGenres: Record<string, number> = {};
  live: Record<string, LiveRadio> = {};
  kind: RadioMix["kind"] | null = null;

  get source() { return this.restored?.source ?? this.activeSource; }

  get lyricTrack() {
    const source = this.source;
    const id = this.restored ? this.restored.current : this.current;
    if (source?.kind !== "library" || typeof id !== "number") return null;
    return { id, title: this.videoTitle, artist: this.videoChannel, cover: "" };
  }

  get restoredPosition() { return this.restored?.position; }
  get savedPosition() { return this.restored?.position ?? (this.activeSource ? this.position : 0); }
  get hasNext() {
    return !this.restored && (this.kind === "youtube" || this.kind === "library");
  }
  stationsLoaded = false;
  youtubeEnabled = false;
  somafmEnabled = false;
  stationsError: string | null = null;
  tuningGenre = "";
  tuneError = "";
  private libraryRequest: AbortController | null = null;
  status: RadioStatus = "idle";
  stationName: string | null = null;
  videoTitle = "";
  videoChannel = "";
  videoId = "";
  failedStation = "";
  // Once a station has started, the view (and its player) stays
  // mounted so the sound survives navigation; the App shell reads
  // this to keep it in the tree.
  started = false;
  volume =
    Number.isFinite(storedVolume) && storedVolume >= 0 && storedVolume <= 100
      ? Math.round(storedVolume)
      : 80;
  controls: RadioControls | null = null;
  // A start asked for before the engine registered its controls
  // (a fast first click on a chip): the engine flushes this when
  // it registers, so the click is never silently dropped.
  pendingStart: { mix: RadioMix; first?: string | number; position: number } | null = null;
  private readonly account = currentAccount();
  private readonly historyKey = `listening-radio-history:${this.account}`;
  private readonly clearKey = `${this.historyKey}:clear`;
  private pendingClear = readRadioClear(this.clearKey);
  private retrying = false;
  private restored: RadioHistory | null = null;
  private activeSource: RadioHistory["source"] | null = null;
  private current: RadioHistory["current"] = null;
  private position = 0;
  private sample = 0;
  private sharedTuning: RadioTuning | null = null;
  private serverRevision = 0;
  private writes = Promise.resolve();
  private reading: Promise<void> | null = null;
  private localChange = 0;
  private playIntent = 0;
  private readonly output = Array.from(crypto.getRandomValues(new Uint8Array(16)),
    byte => byte.toString(16).padStart(2, "0")).join("");
  private presence: Promise<boolean> = Promise.resolve(true);

  constructor(private readonly host: SliceHost) {
    this.showHistory(readRadioHistory(this.historyKey));
  }

  private get signedIn() {
    return this.account !== "" && this.account !== "paired";
  }

  private showHistory(history: RadioHistory | null) {
    this.restored = history;
    this.sharedTuning = history?.tuning ? {
      id: history.tuning, claim: "confirmed", baseRevision: this.serverRevision,
    } : history?.retry ? {
      id: history.retry.id, claim: "unconfirmed", baseRevision: history.retry.revision,
    } : null;
    this.started = history !== null;
    this.kind = history?.source.kind ?? null;
    this.status = history ? "paused" : "idle";
    this.stationName = history?.source.name ?? null;
    this.videoTitle = history?.title ?? "";
    this.videoChannel = history?.channel ?? "";
    this.videoId = typeof history?.current === "string" ? history.current : "";
  }

  refreshHistory() {
    if (!this.reading) this.reading = this.readSharedHistory().finally(() => { this.reading = null; });
    return this.reading;
  }

  private async readSharedHistory() {
    if (!this.signedIn || this.libraryRequest) return;
    if (this.activeSource && (this.status !== "paused" || this.sharedTuning?.claim === "unconfirmed")) return;
    const sessionAudioActive = () => [...document.querySelectorAll<HTMLAudioElement>(".player audio")]
      .some(audio => !audio.paused && !audio.ended);
    if (sessionAudioActive()) return;
    const version = this.localChange;
    const deadline = AbortSignal.timeout(2500);
    try {
      await Promise.race([this.writes, new Promise<void>(resolve =>
        deadline.addEventListener("abort", () => resolve(), { once: true }))]);
      if (deadline.aborted || this.account !== currentAccount() || this.localChange !== version) return;
      const result = await api<{ revision: number; history: unknown }>("/api/my/radio", { cache: "no-store", signal: deadline });
      if (this.localChange !== version || this.libraryRequest || this.account !== currentAccount() || sessionAudioActive()) return;
      this.serverRevision = Math.max(this.serverRevision, result.revision);
      if (this.sharedTuning?.claim === "unconfirmed") return;
      const history = parseRadioHistory(result.history);
      if (history?.tuning && history.tuning === this.pendingClear) return;
      if (!this.activeSource && this.host.view !== "radio" && history &&
          this.host.parkedSongAt > (history.saved_at ?? 0)) {
        this.showHistory(null);
        this.host.bump();
        return;
      }
      const local = this.restored;
      if (history?.tuning && local?.tuning === history.tuning && (local.sample ?? 0) > (history.sample ?? 0)) {
        this.writeHistory(local);
        return;
      }
      if (this.activeSource) {
        if (this.status !== "paused" || history?.tuning === this.sharedTuning?.id) return;
        this.controls?.stop();
        this.activeSource = null;
      } else if (!history?.tuning && !this.restored?.tuning) {
        return;
      }
      if (local?.saved_at && local.tuning === history?.tuning && local.saved_at === history?.saved_at) return;
      saveRadioHistory(this.historyKey, history);
      this.showHistory(history);
      this.host.bump();
    } catch {
      // The locally saved station remains available when the room cannot be reached.
    }
  }

  private sendHistory(body: {
    op: "tune" | "update" | "clear"; tuning: string;
    history?: RadioHistory; expected_revision?: number | null;
  }) {
    return api<{ accepted: boolean; revision: number }>("/api/my/radio", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      keepalive: true,
      signal: AbortSignal.timeout(5000),
    });
  }

  private writeHistory(history: RadioHistory) {
    const tuning = this.sharedTuning;
    if (!tuning) return;
    this.writes = this.writes.then(async () => {
      if (this.account !== currentAccount() || tuning.claim === "superseded") return;
      const op = tuning.claim === "confirmed" ? "update" : "tune";
      // On retry, the original observation prevents overwriting a newer selection.
      const expected_revision = op === "tune" && tuning.claim === "unconfirmed" ? tuning.baseRevision : null;
      if (tuning.claim === "new") {
        tuning.baseRevision = this.serverRevision;
        tuning.claim = "unconfirmed";
        if (this.sharedTuning === tuning) this.saveCurrentHistory();
      }
      const response = await this.sendHistory({ op, tuning: tuning.id, history, expected_revision });
      this.serverRevision = Math.max(this.serverRevision, response.revision);
      tuning.claim = response.accepted ? "confirmed" : "superseded";
      if (this.sharedTuning !== tuning) return;
      if (this.restored) {
        this.restored = { ...this.restored, tuning: response.accepted ? tuning.id : undefined, retry: undefined };
        saveRadioHistory(this.historyKey, this.restored);
      } else {
        this.saveCurrentHistory();
      }
    }).catch(() => {});
  }

  private flushClear() {
    const id = this.pendingClear;
    if (!id) return;
    this.writes = this.writes.then(async () => {
      if (this.account !== currentAccount() || this.pendingClear !== id) return;
      const response = await this.sendHistory({ op: "clear", tuning: id });
      this.serverRevision = Math.max(this.serverRevision, response.revision);
      if (this.pendingClear === id) {
        this.pendingClear = null;
        saveRadioClear(this.clearKey, null);
      }
    }).catch(() => {});
  }

  private saveCurrentHistory() {
    if (!this.activeSource) return null;
    const tuning = this.sharedTuning;
    const history = {
      source: this.activeSource, title: this.videoTitle, channel: this.videoChannel,
      current: this.current,
      ...(this.activeSource.kind === "library" || this.activeSource.kind === "youtube" ? { position: this.position, sample: this.sample } : {}),
      tuning: tuning?.claim === "confirmed" ? tuning.id : undefined,
      retry: tuning?.claim === "new" || tuning?.claim === "unconfirmed"
        ? { id: tuning.id, revision: tuning.baseRevision } : undefined,
    };
    saveRadioHistory(this.historyKey, history);
    return history;
  }

  private recordStation() {
    const history = this.saveCurrentHistory();
    if (history) this.writeHistory(history);
  }

  retryHistory() {
    if (!this.signedIn || this.retrying) return;
    if (!this.pendingClear && this.sharedTuning?.claim !== "unconfirmed") return;
    this.retrying = true;
    this.flushClear();
    if (this.sharedTuning?.claim === "unconfirmed") {
      const history = this.restored ?? this.saveCurrentHistory();
      if (history) this.writeHistory(history);
    }
    void this.writes.then(() => { this.retrying = false; });
  }

  private restartStation(retune = false) {
    if (!this.restored) return;
    const { source, current, position } = this.restored;
    const first = retune ? undefined : current ?? undefined;
    if (source.kind === "library") {
      if (!this.libraryRequest) void this.startLibrary(source.name, typeof first === "number" ? first : undefined, true, retune ? 0 : position);
    } else if (source.kind === "live") {
      void this.startLive(source.name);
    } else {
      this.startMixFromRadio(source, first, retune ? 0 : position);
    }
  }

  // Radio is this device's sound source while a station is
  // starting, playing, or paused: the player bar shows it and the
  // session engine holds this device's library audio.
  get sounding(): boolean {
    return (
      this.status === "starting" ||
      this.status === "playing" ||
      this.status === "paused"
    );
  }

  async loadStations({ deferPlaybackCheck = false } = {}): Promise<boolean> {
    try {
      const data = await api<StationsResult>("/api/stations");
      if (JSON.stringify(this.stations) !== JSON.stringify(data.stations)) {
        this.stations = data.stations;
      }
      this.libraryGenres = data.ready_genres ?? {};
      this.live = data.live ?? {};
      this.somafmEnabled = data.somafm_enabled ?? Object.keys(this.live).length > 0;
      this.youtubeEnabled = data.youtube_enabled ?? data.stations.length > 0;
      if (!deferPlaybackCheck) this.stopUnavailableStation();
      this.stationsLoaded = true;
      this.stationsError = null;
    } catch (error) {
      this.stationsError = errorMessage(error, "Stations could not load");
    }
    this.host.bump();
    return this.stationsError === null;
  }

  async saveStations(stations: RadioStation[]): Promise<boolean> {
    try {
      const data = await postJson<StationsResult>("/api/stations", {
        stations,
      });
      this.stations = data.stations;
      this.stationsLoaded = true;
      this.stationsError = null;
      this.host.bump();
      return true;
    } catch (error) {
      notify(errorMessage(error, "Stations could not be saved"));
      return false;
    }
  }

  startStation(name: string, videos?: string[]) {
    const station = this.stations.find((item) => item.name === name);
    const seeds = videos ?? station?.seeds;
    if (!station || !seeds?.length) return;
    this.startMix(name, seeds);
  }

  startMix(name: string, seeds: string[]) {
    this.startMixFromRadio({ kind: "youtube", name, seeds });
  }

  cancelLibraryTune() {
    this.libraryRequest?.abort();
    this.libraryRequest = null;
    if (this.tuningGenre || this.tuneError) {
      this.tuningGenre = "";
      this.tuneError = "";
      this.host.bump();
    }
  }

  async startLibrary(name: string, first?: number, resumed = false, position = 0) {
    this.cancelLibraryTune();
    this.controls?.prepareLibrary();
    const controller = new AbortController();
    this.libraryRequest = controller;
    this.tuningGenre = name;
    this.host.bump();
    try {
      const query = new URLSearchParams({ genre: name });
      const { tracks } = await api<RadioLibraryResult>(`/api/radio/library?${query}`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      if (tracks.length) this.startMixFromRadio({ kind: "library", name, tracks }, first, position);
      else this.tuneError = "This genre has no playable songs right now.";
    } catch {
      if (!controller.signal.aborted) this.tuneError = "Could not load songs for this genre. Try again.";
    } finally {
      if (this.libraryRequest === controller) {
        this.libraryRequest = null;
        this.tuningGenre = "";
        if (resumed && this.tuneError) notify(this.tuneError);
        this.host.bump();
      }
    }
  }

  async startLive(name: string, { resume = false } = {}) {
    const intent = ++this.playIntent;
    const loaded = await this.loadStations({ deferPlaybackCheck: true });
    if (intent !== this.playIntent) return;
    if (!loaded) {
      notify(this.stationsError ?? "Stations could not load");
      return;
    }
    const stream = this.live[name];
    if (!stream) {
      this.stopUnavailableStation();
      notify("This live station is unavailable. Check the room’s Radio settings.");
      return;
    }
    if (resume && this.activeSource?.kind === "live" && this.activeSource.stream.url === stream.url) {
      const allowed = await this.signalPresence("live");
      if (allowed && intent === this.playIntent) this.controls?.toggle();
    } else {
      this.startMixFromRadio({ kind: "live", name, stream });
    }
  }

  startNoise(name: string, profile: NoiseProfile) {
    this.startMixFromRadio({ kind: "noise", name, profile });
  }

  private sourceAvailable(source: RadioHistory["source"]) {
    if (source.kind === "youtube") return this.youtubeEnabled;
    if (source.kind === "live") return Object.hasOwn(this.live, source.name);
    return true;
  }

  private stopUnavailableStation() {
    const source = this.activeSource ?? this.restored?.source;
    if (source && !this.sourceAvailable(source)) this.stopRadio(false);
  }

  async signalPresence(source: RadioMix["kind"] | null): Promise<boolean> {
    // Serialize release and resume for the same browser output.
    this.presence = this.presence.then(async () => {
      try {
        await postJson("/api/radio/presence", { output: this.output, source }, AbortSignal.timeout(4000));
        return true;
      } catch (error) {
        if (error instanceof ApiError && (error.status === 403 || error.status === 503)) {
          return false;
        }
        // Old servers do not provide a presence endpoint.
        if (error instanceof ApiError && error.status === 404) return true;
        return source !== "youtube" && source !== "live";
      }
    });
    return this.presence;
  }

  refreshPresence() {
    const source = this.activeSource;
    if (!source) return;
    void this.signalPresence(source.kind).then(allowed => {
      if (this.activeSource !== source || allowed) return;
      this.stopRadio(false);
      notify("This Radio source is unavailable.");
    });
  }

  startMixFromRadio(mix: RadioMix, first?: string | number, position = 0) {
    const intent = ++this.playIntent;
    if (!this.sourceAvailable(mix)) return;
    void this.signalPresence(mix.kind).then(allowed => {
      if (intent !== this.playIntent) return;
      if (allowed) this.playMix(mix, first, position);
      else notify("This Radio source is unavailable. Check Extensions or try again.");
    });
  }

  private playMix(mix: RadioMix, first?: string | number, position = 0) {
    this.cancelLibraryTune();
    this.restored = null;
    this.localChange++;
    this.sharedTuning = this.signedIn ? {
      id: Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join(""),
      claim: "new", baseRevision: this.serverRevision,
    } : null;
    this.activeSource = mix.kind === "library" ? { kind: mix.kind, name: mix.name } : mix;
    const track = mix.kind === "library" ? mix.tracks.find(item => item.id === first) : undefined;
    this.current = mix.kind === "library" ? track?.id ?? null : first ?? null;
    this.position = this.current !== null ? position : 0;
    this.sample = 0;
    this.started = true;
    this.stationName = mix.name;
    this.kind = mix.kind;
    this.status = "starting";
    this.failedStation = "";
    this.videoTitle = track?.title ?? (mix.kind === "live" ? mix.stream.title : mix.name);
    this.videoChannel = track?.artist ?? (mix.kind === "live" ? "SomaFM" :
      mix.kind === "noise" ? "Generated on this device" : "");
    this.videoId = typeof first === "string" ? first : "";
    this.recordStation();
    // One audible thing at a time (ADR 0003): this device's library
    // audio pauses now, and the session engine's radio hold keeps
    // it paused while radio sounds. The shared session itself is
    // never touched; other devices hear no change.
    document.querySelectorAll("audio").forEach((element) => element.pause());
    if (this.controls) this.controls.start(mix, first, this.position);
    else this.pendingStart = { mix, first, position: this.position };
    this.host.bump();
  }

  checkpointPosition(id: number | string, seconds: number, leaving = false) {
    if ((this.activeSource?.kind !== "library" && this.activeSource?.kind !== "youtube") ||
        this.current !== id || !Number.isFinite(seconds) || seconds < 0 || seconds > 86400) return;
    if (!leaving && this.position === seconds) return;
    this.position = seconds;
    this.sample++;
    if (this.status === "paused") this.host.bump();
    const history = this.saveCurrentHistory();
    if (!history) return;
    const tuning = this.sharedTuning;
    if (leaving && tuning && tuning.claim !== "superseded" && this.account === currentAccount()) {
      const confirmed = tuning.claim === "confirmed";
      void this.sendHistory({
        op: confirmed ? "update" : "tune", tuning: tuning.id, history,
        expected_revision: confirmed ? undefined : tuning.baseRevision,
      }).catch(() => {});
    } else {
      this.writeHistory(history);
    }
  }

  private resumeHere() {
    this.localChange++;
    if (this.restored) {
      this.restartStation();
    } else if (this.status === "paused" && this.activeSource) {
      if (this.activeSource.kind === "live") {
        void this.startLive(this.activeSource.name, { resume: true });
        return;
      }
      const intent = this.playIntent;
      void this.signalPresence(this.activeSource.kind).then(allowed => {
        if (allowed && intent === this.playIntent) this.controls?.toggle();
      });
    } else {
      this.controls?.toggle();
    }
  }

  async toggleRadio() {
    const intent = ++this.playIntent;
    const version = this.localChange;
    if (this.status === "paused" && this.signedIn && (this.kind === "youtube" || this.kind === "library")) {
      if (this.kind === "library") this.controls?.prepareLibrary();
      await this.refreshHistory();
      if (this.playIntent !== intent || this.localChange !== version || this.status !== "paused") return;
    }
    this.resumeHere();
  }

  async playStation(kind: "library" | "youtube", name: string) {
    const intent = ++this.playIntent;
    if (this.signedIn && (this.status === "idle" || this.status === "paused")) {
      const version = this.localChange;
      if (kind === "library") this.controls?.prepareLibrary();
      await this.refreshHistory();
      if (intent !== this.playIntent || version !== this.localChange || this.host.view !== "radio") return;
    }
    if (this.status === "paused" && this.kind === kind && this.stationName === name) this.resumeHere();
    else if (kind === "library") await this.startLibrary(name);
    else this.startStation(name);
  }

  parkYouTube() {
    this.cancelLibraryTune();
    this.playIntent++;
    this.localChange++;
    if (this.activeSource?.kind !== "youtube") return;
    this.controls?.checkpoint();
    this.restored = this.saveCurrentHistory();
    this.activeSource = null;
    this.pendingStart = null;
    this.controls?.stop();
    this.status = "paused";
    this.host.bump();
  }

  nextRadio() {
    this.localChange++;
    this.controls?.next();
  }

  retuneRadio() {
    const intent = ++this.playIntent;
    if (this.restored) this.restartStation(true);
    else if (this.activeSource?.kind === "live") void this.startLive(this.activeSource.name);
    else if (this.activeSource) void this.signalPresence(this.activeSource.kind).then(allowed => {
      if (allowed && intent === this.playIntent) this.controls?.retune();
    });
  }

  stopRadio(clearShared = true) {
    this.playIntent++;
    void this.signalPresence(null);
    this.cancelLibraryTune();
    this.pendingStart = null;
    this.localChange++;
    if (clearShared && this.sharedTuning) {
      this.pendingClear = this.sharedTuning.id;
      saveRadioClear(this.clearKey, this.pendingClear);
      this.flushClear();
    }
    this.sharedTuning = null;
    this.restored = null;
    this.activeSource = null;
    this.current = null;
    saveRadioHistory(this.historyKey, null);
    this.controls?.stop();
    this.status = "idle";
    this.kind = null;
    this.stationName = null;
    this.videoTitle = "";
    this.videoChannel = "";
    this.videoId = "";
    this.host.bump();
  }

  setVolume(value: number) {
    this.volume = Math.max(0, Math.min(100, Math.round(value)));
    localStorage.setItem("listening-radio-volume", String(this.volume));
    this.controls?.setVolume(this.volume);
    this.host.bump();
  }

  // The engine's reports (radioEngine.ts): a video is sounding,
  // the listener paused it, the station could not start, or a
  // sounding station gave out and ended quietly.
  selectVideo(id: string, title = "", channel = "") {
    if (!id) return;
    const same = id === this.videoId;
    const nextTitle = title || (same && this.videoTitle) || this.stationName || "";
    const nextChannel = channel || (same && this.videoChannel) || "";
    if (same && this.videoTitle === nextTitle && this.videoChannel === nextChannel) return;
    if (!same) this.position = 0;
    this.sample++;
    this.videoId = id;
    this.current = id;
    this.videoTitle = nextTitle;
    this.videoChannel = nextChannel;
    this.recordStation();
    this.host.bump();
  }

  applyEnginePlaying(video: { title: string; channel: string; videoId: string; trackId?: number; position?: number }) {
    this.status = "playing";
    this.videoTitle = video.title;
    this.videoChannel = video.channel;
    this.videoId = video.videoId;
    const current = video.trackId ?? (video.videoId || null);
    this.position = video.position ?? (current === this.current ? this.position : 0);
    this.current = current;
    this.sample++;
    this.recordStation();
    this.host.bump();
  }

  applyEnginePaused(track?: RadioLibraryTrack, position?: number) {
    if (track) {
      this.videoTitle = track.title;
      this.videoChannel = track.artist;
      this.current = track.id;
      this.position = position ?? 0;
      this.sample++;
    }
    if (track || this.status === "playing" || this.status === "starting") {
      this.status = "paused";
      this.recordStation();
      this.host.bump();
    }
  }

  applyEngineFailed(stationName: string) {
    this.status = "failed";
    this.failedStation = stationName;
    this.videoTitle = "";
    this.videoChannel = "";
    this.videoId = "";
    this.host.bump();
  }

  // A station that sounded and then gave out ends here: back to
  // idle with no message (the could-not-start report is only for
  // stations whose seeds all fail at start).
  applyEngineStopped() {
    this.status = "idle";
    this.kind = null;
    this.stationName = null;
    this.videoTitle = "";
    this.videoChannel = "";
    this.videoId = "";
    this.host.bump();
  }
}
