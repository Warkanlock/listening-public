import { useState } from "react";
import { postJson, type Mp3Retry, type RequestRow } from "../api";
import { useStore } from "../store";
import { notify } from "../toasts";

export function TryMp3Button({ retry, title }: { retry: Mp3Retry; title: string }) {
  return <Mp3Action key={`${retry.id}:${retry.format_revision}`} retry={retry} title={title} />;
}

function Mp3Action({ retry, title }: { retry: Mp3Retry; title: string }) {
  const store = useStore();
  const [state, setState] = useState<"idle" | "sending" | "sent">("idle");
  const label = state === "sending" ? "Requesting MP3…" : state === "sent" ? "MP3 requested" : "Try MP3";

  async function submit() {
    setState("sending");
    try {
      await postJson<RequestRow>("/api/requests/format", {
        request_id: retry.id, format_revision: retry.format_revision, requested_format: "mp3",
      });
      setState("sent");
      notify(`MP3 search requested for ${title}`);
      if (store.view === "all" || store.view === "moods") void store.loadTracks(true);
    } catch (error) {
      setState("idle");
      notify(error instanceof Error ? error.message : "Could not request an MP3.");
    } finally {
      void store.loadRequests();
    }
  }

  return <button type="button" className="quiet-button" disabled={state !== "idle"}
    aria-label={`${label} for ${title}`} aria-busy={state === "sending"} onClick={() => void submit()}>{label}</button>;
}
