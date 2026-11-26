import { ApiError, api, type JamInvite, type Session, type SessionTrack } from "../api";
import { notify } from "../toasts";
import { deviceId } from "../store";
import { audioHref, TrackBuffer } from "../trackBuffer";
import { errorMessage, type SliceHost } from "./host";

type BufferedBoundary = { position: number; natural: boolean };
type BufferedSession = { base: Session; advances: BufferedBoundary[]; finished: BufferedBoundary | null };

function playbackOutputId() {
  const key = "listening-playback-output";
  const navigation = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined;
  const existing = sessionStorage.getItem(key);
  if (navigation?.type === "reload" && existing) return existing;
  const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte =>
    byte.toString(16).padStart(2, "0")).join("");
  sessionStorage.setItem(key, id);
  return id;
}

function nextPresenceEpoch() {
  const key = "listening-playback-epoch";
  const previous = Number(sessionStorage.getItem(key));
  const epoch = Math.max(Number.isSafeInteger(previous) ? previous + 1 : 1, Date.now());
  sessionStorage.setItem(key, String(epoch));
  return epoch;
}

type SessionEndpoint = "/api/session" | "/api/my/session";

// The playback session (plan 056): the shared session snapshot,
// this device's output preferences (crossfade, ReplayGain
// normalization), and the queue panel's open state.
export class SessionSlice {
  session: Session | null = null;
  sessionFailed = false;
  followingJam: JamInvite | null = null;
  hostingJam = false;
  setHostingJam(hosting: boolean) {
    if (this.hostingJam === hosting) return;
    this.hostingJam = hosting;
    this.host.bump();
  }
  private readingJam = false;
  audioJoiner: (() => boolean) | null = null;
  unlockAudio: (() => void) | null = null;
  readPosition: (() => number) | null = null;
  readMediaPosition: (() => number | undefined) | null = null;
  readAudioPlaying: (() => boolean) | null = null;
  detachMedia: (() => void) | null = null;
  private offline: BufferedSession | null = null;
  private bufferGraceTimer: number | undefined;
  private reconnecting: Promise<void> | null = null;
  readonly outputId = playbackOutputId();
  private presenceEpoch = nextPresenceEpoch();
  private activeEndpoints = new Set<SessionEndpoint>();
  private presence: "active" | "idle" | "closed" = "active";
  private pendingActions = 0;
  get presenceActive() { return this.presence === "active"; }
  get playbackInUse() { return this.pendingActions > 0 || this.readAudioPlaying?.() === true; }
  readonly buffer = new TrackBuffer(this.outputId, () => this.host.bump());
  bufferAhead = Math.max(1, Math.min(3, Math.trunc(Number(localStorage.getItem("listening-buffer-ahead")) || 1)));
  get bufferedPlayback() { return this.offline !== null; }
  personalQueue = false;
  queueEpoch = 0;
  // This device's crossfade between songs, in seconds (plan 039):
  // a player output preference like the volume, so it lives in
  // localStorage rather than the room's shared settings.
  crossfade = [0, 2, 5].includes(
    Number(localStorage.getItem("listening-crossfade")),
  )
    ? Number(localStorage.getItem("listening-crossfade"))
    : 0;
  // This device's ReplayGain normalization (plan 039): opt-in and
  // off by default; a player output preference like the crossfade,
  // so it lives in localStorage too.
  normalize = localStorage.getItem("listening-normalize") === "1";
  compactAudio = localStorage.getItem("listening-mobile-audio") === "compact";
  queueOpen = false;
  queuePreference: string | null = localStorage.getItem("listening-queue-open");

  constructor(private readonly host: SliceHost) {}

  get current(): Session["current"] {
    return this.session?.current ?? null;
  }

  // The favorite flip's session half (plan 048): browsing's
  // toggleFavorite flips the track in its own list and bumps,
  // and asks here for the current and upcoming copies.
  setFavorite(id: number | string, favorite: boolean) {
    if (this.session?.current?.id === id) {
      this.session.current.favorite = favorite;
    }
    for (const item of this.session?.upcoming ?? []) {
      if (item?.id === id) item.favorite = favorite;
    }
  }

  async pollSession() {
    if (this.reconnecting || !this.presenceActive) return;
    if (this.followingJam) return this.pollJam(this.followingJam);
    const endpoint = this.endpoint;
    const epoch = this.queueEpoch;
    const presence = this.presenceEpoch;
    try {
      const session = await api<Session>(endpoint, { headers: this.headersFor(endpoint) });
      if (epoch !== this.queueEpoch || !this.isPresent(presence) || this.reconnecting) return;
      if (this.offline) {
        const pending = this.rejoin(session, epoch, presence);
        this.reconnecting = pending;
        try {
          await pending;
        } finally {
          this.reconnecting = null;
        }
        return;
      }
      this.sessionFailed = false;
      this.applySession(session);
    } catch (error) {
      if (epoch !== this.queueEpoch || !this.isPresent(presence)) return;
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) this.clearBuffer();
      if (!this.sessionFailed) {
        this.sessionFailed = true;
        this.host.bump();
      }
    }
  }

  private async pollJam(jam: JamInvite) {
    if (this.readingJam) return;
    this.readingJam = true;
    try {
      const session = await api<Session>(`/api/jams/session?id=${encodeURIComponent(jam.id)}`, {
        headers: this.outputHeaders, signal: AbortSignal.timeout(3500),
      });
      if (this.followingJam === jam && this.presenceActive) this.applySession(session);
    } catch (error) {
      if (this.followingJam !== jam) return;
      if (error instanceof ApiError && [403, 410].includes(error.status)) {
        this.leaveJam();
        notify("The Jam has ended or is no longer nearby.");
      } else if (this.session?.playing) {
        this.applySession({ ...this.session, playing: false, session_playing: false });
      }
    } finally {
      this.readingJam = false;
    }
  }

  jamAction<T>(op: string, fields: Record<string, unknown> = {}, keepalive = false) {
    return api<T>("/api/jams", {
      method: "POST", keepalive,
      headers: { ...this.outputHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({ op, ...fields }),
      signal: AbortSignal.timeout(4000),
    });
  }

  async joinJam(invite: JamInvite) {
    if (!this.resumePresence()) return;
    this.unlockAudio?.();
    const presence = this.presenceEpoch;
    const session = await this.jamAction<Session>("join", { id: invite.id });
    if (!this.isPresent(presence)) return;
    this.leavePresence(false);
    this.resumePresence();
    this.followingJam = invite;
    this.queueEpoch++;
    this.clearBuffer();
    this.session = null;
    this.sessionFailed = false;
    this.host.stopRadio(false);
    this.applySession(session);
  }

  leaveJam() {
    const jam = this.followingJam;
    if (!jam) return;
    this.followingJam = null;
    void this.jamAction("leave", { id: jam.id }, true).catch(() => {});
    this.queueEpoch++;
    this.clearBuffer();
    this.detachMedia?.();
    this.session = null;
    this.host.bump();
  }

  async restoreParkedOwnerQueue() {
    const shared = this.session;
    if (!this.presenceActive || this.followingJam || this.personalQueue || shared?.playing) return;
    const epoch = this.queueEpoch;
    const presence = this.presenceEpoch;
    try {
      const personal = await api<Session>("/api/my/session", { headers: this.headersFor("/api/my/session") });
      if (epoch !== this.queueEpoch || !this.isPresent(presence) || this.session?.version !== shared?.version) return;
      if ((personal.parked_at ?? 0) > (shared?.parked_at ?? 0)) {
        this.selectQueue(true);
        this.applySession(personal);
      }
    } catch {
      // The shared queue stays available if the personal queue cannot be loaded.
    }
  }

  private get endpoint(): SessionEndpoint {
    return this.personalQueue ? "/api/my/session" : "/api/session";
  }

  get outputHeaders() {
    return {
      "X-Listening-Output": this.outputId,
      "X-Listening-Output-Name": encodeURIComponent(this.host.deviceName),
      "X-Listening-Output-Epoch": String(this.presenceEpoch),
    };
  }

  private headersFor(endpoint: SessionEndpoint) {
    this.activeEndpoints.add(endpoint);
    return this.outputHeaders;
  }

  private isPresent(epoch: number) {
    return this.presenceActive && this.presenceEpoch === epoch;
  }

  resumePresence() {
    if (this.presence === "closed") return false;
    if (this.presence === "idle") {
      this.presenceEpoch = nextPresenceEpoch();
      this.presence = "active";
      this.prepareBuffer();
      this.host.bump();
    }
    return true;
  }

  reopenPresence() {
    if (this.presence === "closed") this.presence = "idle";
  }

  selectQueue(personal: boolean) {
    if (this.personalQueue !== personal) {
      this.leaveJam();
      this.personalQueue = personal;
      this.queueEpoch++;
      this.clearBuffer();
      this.session = null;
      this.host.bump();
    }
  }

  clearBuffer() {
    this.offline = null;
    this.cancelBufferGrace();
    this.buffer.clear();
  }

  private cancelBufferGrace() {
    window.clearTimeout(this.bufferGraceTimer);
    this.bufferGraceTimer = undefined;
  }

  prepareBuffer() {
    const session = this.session;
    if (this.presenceActive && (this.offline || this.sessionFailed)) return;
    if (!session?.current) {
      this.cancelBufferGrace();
      this.buffer.clear();
      return;
    }
    if (!this.presenceActive || session.output_selected === false) {
      this.buffer.suspend();
      if (!this.buffer.ready) return;
      if (this.bufferGraceTimer === undefined) {
        this.bufferGraceTimer = window.setTimeout(() => {
          this.bufferGraceTimer = undefined;
          this.buffer.clear();
          this.host.bump();
        }, 5 * 60_000);
      }
      return;
    }
    this.cancelBufferGrace();
    this.buffer.prepare([session.current, ...session.upcoming.slice(0, this.bufferAhead)]
      .filter((track): track is SessionTrack => track !== null), this.compactAudio);
  }

  applySession(session: Session | null) {
    if (!session) return;
    if (this.session && session.version < this.session.version) return;
    const changed = JSON.stringify(session) !== JSON.stringify(this.session);
    this.session = session;
    if (changed) this.host.bump();
  }

  private async requestSession(endpoint: SessionEndpoint, op: string, fields: Record<string, unknown>) {
    const controller = new AbortController();
    const timer = ["next", "pause", "resume", "seek", "resume_buffer"].includes(op)
      ? window.setTimeout(() => controller.abort(), 3000) : undefined;
    try {
      return await api<Session>(endpoint, {
        method: "POST",
        headers: { ...this.headersFor(endpoint), "Content-Type": "application/json" },
        body: JSON.stringify({ op, device_id: deviceId, ...fields }),
        signal: controller.signal,
      });
    } finally {
      window.clearTimeout(timer);
    }
  }

  private async rejoin(remote: Session, epoch: number, presence: number) {
    if (!this.offline || !this.session) return;
    const offline = this.offline;
    const local = this.session;
    if (remote.buffer_reconciliation && remote.version === offline.base.version && remote.current_ref === offline.base.current_ref &&
        remote.occurrence === offline.base.occurrence && remote.output_selected !== false) {
      remote = await this.requestSession(this.endpoint, "resume_buffer", {
        expected_version: offline.base.version,
        expected_ref: offline.base.current_ref,
        expected_occurrence: offline.base.occurrence,
        advances: offline.advances,
        finished: offline.finished,
        playing: local.playing,
        position: this.readPosition?.() ?? local.position,
      });
    }
    if (epoch !== this.queueEpoch || !this.isPresent(presence) || this.offline !== offline) return;
    this.offline = null;
    this.sessionFailed = false;
    this.session = null;
    this.applySession(remote);
  }

  advanceBufferedBoundary(ref: Session["current_ref"], occurrence: Session["occurrence"]) {
    const next = this.session?.upcoming[0];
    if (!this.session?.buffer_reconciliation || this.reconnecting || (this.offline?.advances.length ?? 0) >= this.bufferAhead ||
        !next || !this.buffer.url(audioHref(next, this.compactAudio, this.outputId))) return false;
    const advanced = this.localAction("next", { expected_ref: ref, expected_occurrence: occurrence });
    if (advanced && navigator.onLine) void this.pollSession();
    return advanced;
  }

  private localAction(op: string, fields: Record<string, unknown>) {
    const session = this.session;
    if (!session?.current || session.output_selected === false) return false;
    if ((fields.expected_ref != null && fields.expected_ref !== session.current_ref) ||
        (fields.expected_occurrence != null && fields.expected_occurrence !== session.occurrence)) return true;
    const buffered = (track: SessionTrack) => !!this.buffer.url(audioHref(track, this.compactAudio, this.outputId));
    let change: Partial<Session>;
    const offline = this.offline ?? { base: session, advances: [], finished: null };
    const position = this.readPosition?.() ?? session.position;
    let { advances, finished } = offline;
    if (op === "next") {
      const next = session.upcoming[0];
      const boundary = { position, natural: fields.expected_ref != null };
      if (!session.queue_length) {
        if (!session.playing) return false;
        change = { playing: false, position: 0 };
        finished = boundary;
        notify("End of the queue.");
      } else if (!next || !buffered(next) || advances.length >= this.bufferAhead) {
        if (!session.playing) return false;
        change = { playing: false, position };
        if (boundary.natural) finished = boundary;
        notify("Reconnect to play the next song.");
      } else {
        change = { current: next, current_ref: next.id, position: 0, playing: true,
          occurrence: (session.occurrence ?? 0) + 1, upcoming: session.upcoming.slice(1),
          queue_length: Math.max(0, session.queue_length - 1) };
        advances = [...advances, boundary];
      }
    } else if (op === "pause" || op === "resume") {
      if (op === "resume" && !buffered(session.current)) return false;
      if (op === "resume" && finished && session.queue_length) return false;
      if (op === "pause" && finished && session.queue_length) {
        finished = null;
        change = { playing: false, position: 0 };
      } else {
        change = { playing: op === "resume", position: finished && !session.playing ? session.position : position };
      }
    } else if (op === "seek" && typeof fields.position === "number" && buffered(session.current)) {
      change = { position: fields.position };
      if (session.queue_length) finished = null;
    } else {
      return false;
    }
    this.offline = { base: offline.base, advances, finished };
    this.sessionFailed = true;
    this.applySession({ ...session, ...change, version: session.version + 1 });
    return true;
  }

  leavePresence(closing: boolean) {
    if (this.presenceActive) {
      const session = this.session;
      const position = session?.playing && !this.offline ? this.readMediaPosition?.() : undefined;
      this.presence = closing ? "closed" : "idle";
      const headers = { ...this.outputHeaders, "Content-Type": "application/json" };
      for (const endpoint of this.activeEndpoints) {
        const checkpoint = endpoint === this.endpoint && session && position !== undefined
          ? { position, expected_version: session.version, expected_occurrence: session.occurrence,
              expected_ref: session.current_ref }
          : {};
        void api(endpoint, {
          method: "POST",
          headers,
          body: JSON.stringify({ op: "leave", ...checkpoint }),
          keepalive: true,
        }).catch(() => {});
      }
      this.activeEndpoints.clear();
      this.detachMedia?.();
    }
    if (closing) {
      this.leaveJam();
      this.presence = "closed";
      this.cancelBufferGrace();
      this.buffer.clear();
    } else {
      this.prepareBuffer();
    }
    this.host.bump();
  }

  async sendSession(
    op: string,
    fields: Record<string, unknown> = {},
    successMessage = "",
    onError?: (error: unknown) => void,
  ): Promise<boolean> {
    if (!this.presenceActive) return false;
    if (this.followingJam) {
      if (op === "pause" || ["play", "play_available", "play_request", "play_requests", "play_mood", "play_album", "play_activity"].includes(op)) {
        this.leaveJam();
        if (op === "pause") return true;
      } else {
        if (op === "resume") this.audioJoiner?.();
        else notify("The Jam host controls playback. Leave the Jam to use your own queue.");
        return false;
      }
    }
    this.pendingActions++;
    try {
      return await this.sendPresentAction(op, fields, successMessage, onError);
    } finally {
      this.pendingActions--;
    }
  }

  private async sendPresentAction(
    op: string,
    fields: Record<string, unknown>,
    successMessage: string,
    onError?: (error: unknown) => void,
  ): Promise<boolean> {
    const presence = this.presenceEpoch;
    // One audible thing at a time (plan 059, ADR 0003): a session
    // action that starts library sound on this device stops its
    // radio first. Queue and flag actions that make no sound by
    // themselves leave radio alone.
    const playing = this.session;
    const sampled = op === "pause" && playing?.playing ? this.readMediaPosition?.() : undefined;
    if (playing && sampled !== undefined) {
      fields = { ...fields, position: sampled, expected_version: playing.version,
        expected_occurrence: playing.occurrence, expected_ref: playing.current_ref };
    }
    const startsMusic = ["play", "play_available", "play_request", "play_requests", "play_mood", "play_album", "play_activity", "jump", "next", "previous", "resume"].includes(op)
      && fields.expected_ref == null;
    const selectsHere = op === "outputs" && Array.isArray(fields.devices) && fields.devices.includes(this.outputId);
    const applyLocal = () => {
      if (!this.localAction(op, fields)) return false;
      if (startsMusic && this.session?.playing) {
        this.host.stopRadio(false);
      }
      return true;
    };
    if (startsMusic || selectsHere) this.unlockAudio?.();
    if (this.reconnecting) await this.reconnecting.catch(() => {});
    if (!this.isPresent(presence)) return false;
    if (this.offline || !navigator.onLine) {
      if (applyLocal()) return true;
      if (op !== "play_mood") notify("Reconnect to change the room queue.");
      return false;
    }
    const endpoint = this.endpoint;
    const epoch = this.queueEpoch;
    try {
      const session = await this.requestSession(endpoint, op, fields);
      if (epoch !== this.queueEpoch || !this.isPresent(presence)) return false;
      if (session.playing && (startsMusic || selectsHere)) {
        this.host.stopRadio(false);
      }
      this.applySession(session);
      if (successMessage) notify(successMessage);
      return true;
    } catch (error) {
      if (this.reconnecting) await this.reconnecting.catch(() => {});
      if (!this.isPresent(presence)) return false;
      if (epoch === this.queueEpoch && (error instanceof TypeError || error instanceof DOMException && error.name === "AbortError")
          && applyLocal()) return true;
      onError?.(error);
      if (op !== "play_mood") notify(errorMessage(error, "Session update failed"));
      return false;
    }
  }

  togglePlayback() {
    const session = this.session;
    if (!session?.current) {
      if (session?.queue_length) void this.sendSession("next");
      return;
    }
    if (session.playing) {
      if (this.audioJoiner?.()) return;
    }
    const playing = this.offline ? session.playing : session.session_playing ?? session.playing;
    const remote = session.output_selected === false && !session.outputs_explicit;
    void this.sendSession(playing && !remote ? "pause" : "resume");
  }

  setCrossfade(seconds: number) {
    this.crossfade = [0, 2, 5].includes(seconds) ? seconds : 0;
    localStorage.setItem("listening-crossfade", String(this.crossfade));
    this.host.bump();
  }

  setCompactAudio(on: boolean) {
    this.compactAudio = on;
    localStorage.setItem("listening-mobile-audio", on ? "compact" : "original");
    this.host.bump();
  }

  setBufferAhead(count: number) {
    if (![1, 2, 3].includes(count)) return;
    this.bufferAhead = count;
    localStorage.setItem("listening-buffer-ahead", String(count));
    this.prepareBuffer();
    this.host.bump();
  }

  setNormalize(on: boolean) {
    this.normalize = on;
    localStorage.setItem("listening-normalize", on ? "1" : "0");
    this.host.bump();
  }

  setQueueOpen(open: boolean, remember = true) {
    this.queueOpen = open;
    if (remember) {
      this.queuePreference = String(open);
      localStorage.setItem("listening-queue-open", this.queuePreference);
    }
    this.host.bump();
  }
}
