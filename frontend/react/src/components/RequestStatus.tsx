import { useState } from "react";
import { statusLabel, type RequestRow } from "../api";
import { useStore } from "../store";
import { RoomIconButton } from "./RoomIcon";

type RequestState = Pick<RequestRow, "status" | "phase" | "retry_at" | "copy_review">;
export const FAILED_REQUESTS = ["not_found", "error", "rejected"];

export function RequestStatus({ request }: { request: RequestState }) {
  const { status, phase, retry_at, copy_review } = request;
  const label = copy_review ? "Choose a copy"
    : status === "queued" && retry_at ? "Retry scheduled"
    : status === "searching" && phase === "downloading" ? "Downloading"
    : status === "searching" && phase === "verifying" ? "Verifying"
    : statusLabel(status);
  return <span className={`status-pill request-status ${status}`}>{label}</span>;
}

export function RetryRequestButton({ request, title, onRetry }: {
  request: RequestState & Pick<RequestRow, "id">;
  title: string;
  onRetry?: () => void;
}) {
  const store = useStore();
  const [pending, setPending] = useState(false);
  if (store.member || request.copy_review || !(request.status === "queued" || FAILED_REQUESTS.includes(request.status))) {
    return null;
  }
  async function retry() {
    setPending(true);
    try {
      if (await store.retryRequest(request.id)) onRetry?.();
    } finally {
      setPending(false);
    }
  }
  return <RoomIconButton icon="refresh" label={`Retry now: ${title}`} disabled={pending} onClick={() => void retry()} />;
}
