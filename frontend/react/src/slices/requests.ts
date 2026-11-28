import { api, ApiError, postJson, type DownloadOutcome, type DownloadOutcomePayload, type RecentDownload, type RequestRow, type RequestSong } from "../api";
import { notify, notifyDownloadUpdates } from "../toasts";
import { canChooseFormat, requestNeedsAttention, requestStage, requestStageLabel } from "../requestState";
import { errorMessage, type SliceHost } from "./host";

export type DownloadTray = { kind: "room" } | { kind: "weekly"; rows: readonly RequestRow[] };

type TrayRequest = RequestRow | DownloadOutcome;
const dismissedUpdatesKey = "listening-dismissed-download-updates";

function updateKey(row: TrayRequest) {
  const revision = "format_revision" in row ? row.format_revision : row.key.split("/")[2];
  return `${row.id}/${revision}/${requestStage(row)}`;
}

function readDismissedUpdates() {
  try { return localStorage.getItem(dismissedUpdatesKey)?.split("\n") ?? []; }
  catch { return []; }
}

const requestsSignature = (rows: RequestRow[]) =>
  JSON.stringify(
    rows.map((row) => [
      row.id,
      row.status,
      row.request_state,
      row.phase,
      row.download_percent,
      row.retry_at,
      row.copy_review,
      row.replace_copy,
      row.requested_format,
      row.format_revision,
      row.mp3_retry,
      row.message,
      row.audio,
      row.track_index,
      row.musicbrainz_id,
      row.original_recording_id,
      row.search_title,
      row.title,
      row.artist,
      row.version,
    ]),
  );

// Song requests (plan 056): the request rows, their loading
// state, and the rendered-rows signature that keeps an unchanged
// poll from re-rendering the list.
export class RequestsSlice {
  requestRows: RequestRow[] = [];
  recentDownloads: RecentDownload[] = [];
  formatPicker: RequestRow | null = null;
  downloadTray: DownloadTray | null = null;
  requestsLoaded = false;
  requestsError: string | null = null;
  private renderedRequestsSignature: string | null = null;
  private readRevision = 0;
  private seenOutcomes: string[] | null = null;
  private outcomes: DownloadOutcome[] = [];
  private dismissedUpdates = new Set(readDismissedUpdates());

  constructor(private readonly host: SliceHost) {}

  get attentionRows() {
    const seen = new Set<number>();
    return this.requestRows.filter(row => {
      const stage = requestStage(row);
      if (row.track_index !== null && !(row.replace_copy && stage === "unavailable")) {
        if (seen.has(row.track_index)) return false;
        seen.add(row.track_index);
      }
      return requestNeedsAttention(stage);
    });
  }

  updateRows(tray: DownloadTray) {
    const live = new Map(this.requestRows.map(row => [row.id, row]));
    const rows = new Map<number, TrayRequest>();
    const attention = tray.kind === "weekly" ? tray.rows : this.attentionRows;
    for (const seed of attention) {
      const row = live.get(seed.id) ?? seed;
      if (requestNeedsAttention(requestStage(row))) rows.set(row.id, row);
    }
    if (tray.kind === "room") {
      for (const outcome of this.outcomes.filter(row => requestStage(row) === "ready")) {
        const row = live.get(outcome.id) ?? outcome;
        if (requestStage(row) === "ready") rows.set(row.id, row);
      }
    }
    return [...rows.values()].filter(row => !this.dismissedUpdates.has(updateKey(row)));
  }

  dismissUpdate(row: TrayRequest) {
    const keys = new Set([updateKey(row), ...readDismissedUpdates(), ...this.dismissedUpdates]);
    this.dismissedUpdates = new Set([...keys].slice(0, 256));
    try { localStorage.setItem(dismissedUpdatesKey, [...this.dismissedUpdates].join("\n")); }
    catch {}
    this.host.bump();
  }

  get requestsBadge(): number {
    return this.requestRows.filter((row) =>
      ["waiting_format", "queued", "searching"].includes(row.status),
    ).length;
  }

  // Browsing's setView forces the next loadRequests to bump even
  // when the rows are unchanged (plan 056): the signature is
  // this slice's state, so the reset lives here.
  resetRequestsSignature() {
    this.renderedRequestsSignature = null;
  }

  async loadRequests() {
    const revision = ++this.readRevision;
    try {
      const result = await api<{ requests: RequestRow[]; downloads?: RecentDownload[]; outcomes?: DownloadOutcomePayload[] }>("/api/requests");
      if (revision !== this.readRevision) return true;
      this.requestRows = result.requests;
      this.recentDownloads = result.downloads ?? [];
      this.outcomes = (result.outcomes ?? []).flatMap(row => {
        const id = row.id ?? Number(row.key.split("/", 1)[0]);
        return Number.isSafeInteger(id) && id > 0 ? [{ ...row, id, artist: row.artist ?? "" }] : [];
      });
      if (result.outcomes) this.announceOutcomes(this.outcomes);
      this.requestsLoaded = true;
      this.requestsError = null;
      const signature = requestsSignature(result.requests) + JSON.stringify([this.recentDownloads, this.outcomes]);
      if (signature !== this.renderedRequestsSignature) {
        this.renderedRequestsSignature = signature;
        this.host.bump();
      }
      return true;
    } catch (error) {
      if (revision !== this.readRevision) return false;
      this.renderedRequestsSignature = null;
      this.requestsError = errorMessage(error, "Requests could not load");
      this.host.bump();
      return false;
    }
  }

  private announceOutcomes(outcomes: DownloadOutcome[]) {
    const storageKey = "listening-download-outcomes-v1";
    try { this.seenOutcomes = localStorage.getItem(storageKey)?.split("\n") ?? this.seenOutcomes; } catch { /* Storage is optional. */ }
    const previous = this.seenOutcomes;
    this.seenOutcomes = [...new Set([...outcomes.map(row => row.key), ...(previous ?? [])])].slice(0, 96);
    try { localStorage.setItem(storageKey, this.seenOutcomes.join("\n")); } catch { /* Storage is optional. */ }
    if (previous === null) return;
    const visible = new Set(this.updateRows({ kind: "room" }).map(updateKey));
    const fresh = outcomes.filter(row => !previous.includes(row.key) && visible.has(updateKey(row)));
    if (!fresh.length) return;
    const [latest] = fresh;
    const ready = fresh.filter(row => row.status === "downloaded").length;
    const missing = fresh.filter(row => requestStage(row) === "no_matches").length;
    const review = fresh.length - ready - missing;
    const summary = fresh.length === 1
      ? `${latest.title} · ${latest.status === "downloaded" ? "Ready to play" : requestStageLabel(latest)}`
      : [ready && `${ready} ready to play`, missing && `${missing} no copy found`,
        review && `${review} ${review === 1 ? "needs" : "need"} review`].filter(Boolean).join(" · ");
    notifyDownloadUpdates(summary, () => this.openDownloadTray());
  }

  openDownloadTray(tray: DownloadTray = { kind: "room" }) {
    this.downloadTray = tray;
    this.host.bump();
  }

  closeDownloadTray() {
    this.downloadTray = null;
    this.host.bump();
  }

  chooseFormat(row: RequestRow | null) {
    this.formatPicker = row;
    this.host.bump();
  }

  applyRequest(row: RequestRow) {
    const existing = this.requestRows.find(request => request.id === row.id);
    if (existing && existing.format_revision > row.format_revision) return;
    this.readRevision++;
    this.requestRows = existing ? this.requestRows.map(request => request.id === row.id ? row : request)
      : [row, ...this.requestRows];
    this.resetRequestsSignature();
    this.host.bump();
  }

  async replaceTrackCopy(id: number) {
    try {
      const row = await postJson<RequestRow>("/api/requests/replace-copy", { track_id: id });
      this.applyRequest(row);
      if (canChooseFormat(requestStage(row))) this.chooseFormat(row);
      else notify(`${row.title} · ${requestStageLabel(row)}`);
      void this.loadRequests();
    } catch (error) {
      notify(errorMessage(error, "Couldn’t open replacement copies"));
    }
  }

  async createSongRequest(song: RequestSong): Promise<RequestRow | null> {
    try {
      const row = await postJson<RequestRow>("/api/requests", song);
      this.applyRequest(row);
      if (canChooseFormat(requestStage(row))) this.chooseFormat(row);
      else notify(`${row.title} · ${requestStageLabel(row)}`);
      void this.loadRequests();
      return row;
    } catch (error) {
      notify(errorMessage(error, "Request failed"));
      return null;
    }
  }

  async chooseRecording(current: RequestRow, song: RequestSong, onError: (message: string) => void = notify) {
    try {
      const row = await postJson<RequestRow>("/api/requests", {
        ...song, request_id: current.id, format_revision: current.format_revision,
      });
      this.applyRequest(row);
      if (requestStage(row) === "choose_format") this.chooseFormat(row);
      else notify(`${row.title} · ${requestStageLabel(row)}`);
      void this.loadRequests();
      return row;
    } catch (error) {
      onError(errorMessage(error, "Couldn’t change the recording"));
      void this.loadRequests();
      return null;
    }
  }

  async retryRequest(id: number, searchTitle?: string, revision?: number,
    options?: { restoreMissing: boolean; onError?: (message: string) => void }) {
    try {
      const result = await postJson<{ status: string; request?: RequestRow }>("/api/requests/retry", {
        request_id: id,
        search_title: searchTitle,
        format_revision: revision,
        restore_missing: options?.restoreMissing || undefined,
      });
      if (result.request) this.applyRequest(result.request);
      notify(result.status);
      await this.loadRequests();
      return true;
    } catch (error) {
      if (options?.onError) {
        options.onError(error instanceof ApiError ? error.message : "Couldn’t retry the request. Try again.");
      } else {
        notify(errorMessage(error, "Retry failed"));
      }
      return false;
    }
  }

  playRequest(row: Pick<RequestRow, "id" | "track_index" | "audio">) {
    if (!row.audio) return;
    if (row.track_index) {
      void this.host.sendSession("play", {
        track_id: row.track_index,
        playlist: "all",
      });
    } else {
      void this.host.sendSession("play_request", { request_id: row.id });
    }
  }
}
