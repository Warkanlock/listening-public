import { api, postJson, type DownloadOutcome, type RecentDownload, type RequestRow, type RequestSong } from "../api";
import { notify } from "../toasts";
import { canChooseFormat, requestStage, requestStageLabel } from "../requestState";
import { errorMessage, type SliceHost } from "./host";

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
  requestsLoaded = false;
  requestsError: string | null = null;
  private renderedRequestsSignature: string | null = null;
  private readRevision = 0;
  private seenOutcomes: string[] | null = null;

  constructor(private readonly host: SliceHost) {}

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
      const result = await api<{ requests: RequestRow[]; downloads?: RecentDownload[]; outcomes?: DownloadOutcome[] }>("/api/requests");
      if (revision !== this.readRevision) return true;
      this.requestRows = result.requests;
      this.recentDownloads = result.downloads ?? [];
      if (result.outcomes) this.announceOutcomes(result.outcomes);
      this.requestsLoaded = true;
      this.requestsError = null;
      const signature = requestsSignature(result.requests) + JSON.stringify(this.recentDownloads);
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
    const fresh = outcomes.filter(row => !previous.includes(row.key));
    const latest = fresh[0];
    if (fresh.length > 1) {
      const ready = fresh.filter(row => row.status === "downloaded").length;
      const missing = fresh.filter(row => requestStage(row) === "no_matches").length;
      const review = fresh.length - ready - missing;
      notify([ready && `${ready} ready to play`, missing && `${missing} no copy found`, review && `${review} need review`].filter(Boolean).join(" · "));
    } else if (latest) notify(`${latest.title} · ${latest.status === "downloaded" ? "Ready to play" : requestStageLabel(latest)}`);
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

  async chooseRecording(current: RequestRow, song: RequestSong) {
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
      notify(errorMessage(error, "Couldn’t change the recording"));
      void this.loadRequests();
      return null;
    }
  }

  async retryRequest(id: number, searchTitle?: string, revision?: number) {
    try {
      const result = await postJson<{ status: string; request?: RequestRow }>("/api/requests/retry", {
        request_id: id,
        search_title: searchTitle,
        format_revision: revision,
      });
      if (result.request) this.applyRequest(result.request);
      notify(result.status);
      await this.loadRequests();
      return true;
    } catch (error) {
      notify(errorMessage(error, "Retry failed"));
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
