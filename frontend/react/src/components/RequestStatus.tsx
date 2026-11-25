import { useState } from "react";
import type { RequestRow } from "../api";
import { canRetryRequest, requestStage, requestStageLabel } from "../requestState";
import { useStore } from "../store";
import { RoomIconButton } from "./RoomIcon";

type RequestState = Pick<RequestRow, "status" | "phase" | "retry_at" | "copy_review" | "download_percent" | "request_state">
  & Partial<Pick<RequestRow, "message" | "format_revision">>;
export function RequestStatus({ request }: { request: RequestState }) {
  return <span className={`status-pill request-status ${request.status}`} data-request-state={requestStage(request)}>
    {requestStageLabel(request)}
  </span>;
}

export function RetryRequestButton({ request, title, onRetry }: {
  request: RequestState & Pick<RequestRow, "id">;
  title: string;
  onRetry?: () => void;
}) {
  const store = useStore();
  const [pending, setPending] = useState(false);
  if (store.member || !canRetryRequest(requestStage(request))) {
    return null;
  }
  async function retry() {
    setPending(true);
    try {
      if (await store.retryRequest(request.id, undefined, request.format_revision)) onRetry?.();
    } finally {
      setPending(false);
    }
  }
  return <RoomIconButton icon="refresh" label={`Retry now: ${title}`} disabled={pending} onClick={() => void retry()} />;
}
