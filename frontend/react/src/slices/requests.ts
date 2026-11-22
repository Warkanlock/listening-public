import { api, postJson, statusLabel, type RequestRow, type RequestSong } from "../api";
import { notify } from "../toasts";
import { errorMessage, type SliceHost } from "./host";

const requestsSignature = (rows: RequestRow[]) =>
  JSON.stringify(
    rows.map((row) => [
      row.id,
      row.status,
      row.requested_format,
      row.format_revision,
      row.message,
      row.audio,
      row.track_index,
      row.search_title,
      row.title,
      row.artist,
    ]),
  );

// Song requests (plan 056): the request rows, their loading
// state, and the rendered-rows signature that keeps an unchanged
// poll from re-rendering the list.
export class RequestsSlice {
  requestRows: RequestRow[] = [];
  formatPicker: RequestRow | null = null;
  requestsLoaded = false;
  requestsError: string | null = null;
  private renderedRequestsSignature: string | null = null;

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
    try {
      const result = await api<{ requests: RequestRow[] }>("/api/requests");
      this.requestRows = result.requests;
      this.requestsLoaded = true;
      this.requestsError = null;
      const signature = requestsSignature(result.requests);
      if (signature !== this.renderedRequestsSignature) {
        this.renderedRequestsSignature = signature;
        this.host.bump();
      }
    } catch (error) {
      this.renderedRequestsSignature = null;
      this.requestsError = errorMessage(error, "Requests could not load");
      this.host.bump();
    }
  }

  chooseFormat(row: RequestRow | null) {
    this.formatPicker = row;
    this.host.bump();
  }

  async createSongRequest(song: RequestSong): Promise<RequestRow | null> {
    try {
      const row = await postJson<RequestRow>("/api/requests", song);
      if (row.status === "waiting_format") this.chooseFormat(row);
      else notify(`${row.title}: ${statusLabel(row.status)}`);
      void this.loadRequests();
      return row;
    } catch (error) {
      notify(errorMessage(error, "Request failed"));
      return null;
    }
  }

  async retryRequest(id: number, searchTitle: string) {
    try {
      const result = await postJson<{ status: string }>("/api/requests/retry", {
        request_id: id,
        search_title: searchTitle,
      });
      notify(result.status);
      void this.loadRequests();
    } catch (error) {
      notify(errorMessage(error, "Retry failed"));
    }
  }

  playRequest(row: RequestRow) {
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
