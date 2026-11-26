import type { RequestRow } from "./api";

const labels = {
  choose_format: "Waiting for format",
  queued_search: "Queued",
  queued_copy: "Copy queued",
  retry_search: "Retry scheduled",
  retry_copy: "Copy retry scheduled",
  searching: "Searching",
  starting_copy: "Starting download",
  trying_copy: "Trying a copy",
  downloading: "Downloading",
  verifying: "Verifying",
  choose_copy: "Choose a copy",
  copy_unavailable: "Copy unavailable",
  no_matches: "No copy found",
  search_failed: "Search failed",
  verification_failed: "Needs review",
  waiting_space: "Waiting for space",
  preparing: "Preparing playback",
  copy_missing: "Copy unavailable",
  ready: "Ready",
};

export type RequestState = keyof typeof labels;
type RequestProgress = Pick<RequestRow, "request_state" | "status" | "phase" | "retry_at" | "download_percent" | "copy_review" | "message">;

export function requestStage(request: Partial<RequestProgress>): RequestState {
  if (request.request_state && Object.hasOwn(labels, request.request_state)) return request.request_state;
  if (request.copy_review) return "choose_copy";
  if (request.message?.startsWith("The selected copy could not be downloaded or verified")) return "copy_unavailable";
  switch (request.status) {
    case "waiting_format": return "choose_format";
    case "queued": return request.retry_at ? "retry_search" : "queued_search";
    case "searching":
      if (request.phase === "testing") return "trying_copy";
      if (request.phase === "downloading" || request.phase === "verifying") return request.phase;
      return "searching";
    case "not_found": return "no_matches";
    case "error": return "search_failed";
    case "rejected": return "verification_failed";
    case "waiting_space": return "waiting_space";
    case "downloaded": return "ready";
    default: return "copy_missing";
  }
}

export function requestStageLabel(request: Partial<RequestProgress>) {
  const state = requestStage(request);
  const progress = request.download_percent;
  return (state === "trying_copy" || state === "downloading") && typeof progress === "number"
    ? `${labels[state]} · ${progress}%`
    : labels[state];
}

export const needsCopyChoice = (state: RequestState) => state === "choose_copy" || state === "copy_unavailable";
export const canRetryRequest = (state: RequestState) =>
  ["queued_search", "queued_copy", "retry_search", "retry_copy", "no_matches", "search_failed", "verification_failed"].includes(state);
export const canChooseFormat = (state: RequestState) =>
  ["choose_format", "choose_copy", "copy_unavailable", "no_matches", "search_failed", "verification_failed"].includes(state);
export const canChangeRecording = (state: RequestState) => canChooseFormat(state) ||
  ["queued_search", "queued_copy", "retry_search", "retry_copy"].includes(state);
export const requestNeedsAttention = (state: RequestState) => canChooseFormat(state) || state === "copy_missing";
export const requestAttentionLabel = (count: number) => `${count} ${count === 1 ? "needs" : "need"} attention`;
