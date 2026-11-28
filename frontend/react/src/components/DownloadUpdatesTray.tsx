import { useEffect, useRef, useState, type ReactNode } from "react";
import { Dialog, DialogBackdrop, DialogClose, DialogContent, DialogTitle } from "@ui-sdk/react/dialog";
import { api, postJson, type RequestRow } from "../api";
import { canChooseFormat, canMarkUnavailable, needsCopyChoice, requestStage, requestStageLabel } from "../requestState";
import type { DownloadTray, RequestsSlice } from "../slices/requests";
import { useStore } from "../store";
import { RoomAction } from "./RoomControls";
import { RoomIcon, type RoomIconName } from "./RoomIcon";
import { RequestStatus } from "./RequestStatus";

type TrayRequest = ReturnType<RequestsSlice["updateRows"]>[number];
const actionIcons = {
  Play: "play", "Review copies": "catalogs", "Choose format": "settings",
  "Change format": "settings", Retry: "refresh", "Download again": "download",
} satisfies Record<string, RoomIconName>;
type TrayAction = keyof typeof actionIcons | "Mark as unavailable";

function outcomeActions(row: TrayRequest): (keyof typeof actionIcons)[] {
  const stage = requestStage(row);
  if (stage === "ready") return ["Play"];
  if (stage === "copy_missing") return ["Download again"];
  if (needsCopyChoice(stage)) return ["Review copies"];
  if (stage === "choose_format") return ["Choose format"];
  if (stage === "search_failed" || stage === "verification_failed") return ["Retry", "Change format"];
  return canChooseFormat(stage) ? ["Change format"] : [];
}

export function DownloadUpdatesTray({ tray, children }: { tray: DownloadTray; children: ReactNode }) {
  const store = useStore();
  const operation = useRef<AbortController | null>(null);
  const content = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const [pending, setPending] = useState<number | null>(null);
  const [message, setMessage] = useState("");
  useEffect(() => () => operation.current?.abort(), []);
  const rows = store.requestsSlice.updateRows(tray);
  const groups = [
    { title: "Needs attention", items: rows.filter(row => requestStage(row) !== "ready") },
    { title: "Ready to play", items: rows.filter(row => requestStage(row) === "ready") },
  ];

  function restoreRowFocus(row: TrayRequest) {
    const buttons = Array.from(content.current?.querySelectorAll<HTMLButtonElement>("[data-dismiss-update]") ?? []);
    const index = rows.indexOf(row);
    const next = buttons[index + 1] ?? buttons[index - 1] ?? close.current;
    next?.focus({ preventScroll: true });
  }

  function dismiss(row: TrayRequest) {
    restoreRowFocus(row);
    store.requestsSlice.dismissUpdate(row);
    setMessage(`${row.title} dismissed.`);
  }

  async function open(outcome: TrayRequest, action: TrayAction) {
    if (operation.current) return;
    if (action === "Play") store.sessionSlice.unlockAudio?.();
    const abort = new AbortController();
    operation.current = abort;
    setPending(outcome.id);
    setMessage("");
    try {
      const { requests } = await api<{ requests: RequestRow[] }>(`/api/requests?id=${outcome.id}`, {
        cache: "no-store", signal: abort.signal,
      });
      if (abort.signal.aborted) return;
      const row = requests.find(request => request.id === outcome.id);
      if (row) store.requestsSlice.applyRequest(row);
      const retry = action === "Retry" || action === "Download again";
      const marking = action === "Mark as unavailable";
      const allowed = row && (marking ? canMarkUnavailable(row) : outcomeActions(row).includes(action));
      const changed = (retry || marking) && (!("format_revision" in outcome) || row?.format_revision !== outcome.format_revision);
      if (!row || !allowed || changed || (action === "Play" && !row.audio)) {
        setMessage(`${outcome.title} has changed. Check its current status in Requests.`);
      } else if (action === "Play") store.playRequest(row);
      else if (retry) {
        const queued = await store.requestsSlice.retryRequest(row.id, undefined, row.format_revision,
          { restoreMissing: action === "Download again", onError: error => { if (!abort.signal.aborted) setMessage(error); } });
        if (queued && !abort.signal.aborted) {
          close.current?.focus({ preventScroll: true });
          setMessage(`${row.title} queued for retry.`);
        }
      } else if (marking) {
        const updated = await postJson<RequestRow>("/api/requests/stop-search", {
          request_id: row.id, format_revision: row.format_revision,
        });
        if (!abort.signal.aborted) restoreRowFocus(outcome);
        store.requestsSlice.applyRequest(updated);
        if (!abort.signal.aborted) setMessage(`${row.title} marked as unavailable.`);
        void store.loadRequests();
      } else store.requestsSlice.chooseFormat(row);
    } catch (error) {
      if (!abort.signal.aborted) setMessage(error instanceof Error ? error.message : "Couldn’t load the request. Try again.");
    } finally {
      operation.current = null;
      if (!abort.signal.aborted) setPending(null);
    }
  }

  return <Dialog open onOpenChange={open => { if (!open) store.requestsSlice.closeDownloadTray(); }}>
    <DialogBackdrop />
    <DialogContent ref={content} className="download-updates-tray">
      <header className="download-updates-head">
        <DialogTitle>Download updates</DialogTitle>
        <DialogClose ref={close} className="quiet-button icon-control" aria-label="Close download updates"><RoomIcon name="close" /></DialogClose>
      </header>
      {tray.kind === "weekly" && <p className="download-updates-scope">Weekly Discovery</p>}
      <p className="download-updates-feedback" role="status">{message}</p>
      <div className="download-updates-groups">
        {rows.length === 0 && <p>No download updates.</p>}
        {groups.map(({ title, items }) => items.length > 0 && <section key={title} aria-label={title}>
          <h3>{title}</h3>
          <ul>
            {items.map(row => <li key={row.id} aria-busy={pending === row.id}>
              <div className="download-updates-song">
                <strong>{row.title}</strong>
                {row.artist && <small>{row.artist}</small>}
                <RequestStatus request={row} />
                {"message" in row && row.message && row.message !== requestStageLabel(row) && requestStage(row) !== "ready" &&
                  <small>{row.message}</small>}
              </div>
              <button type="button" className="download-update-dismiss" data-dismiss-update
                aria-label={`Dismiss ${row.title}`} title="Dismiss update"
                aria-disabled={pending !== null} onClick={() => { if (!operation.current) dismiss(row); }}>
                <RoomIcon name="close" />
              </button>
              <div className="download-updates-actions">
                {outcomeActions(row).map(action => <RoomAction
                  key={action} icon={actionIcons[action]} aria-label={`${action} ${row.title}`}
                  aria-disabled={pending !== null} onClick={() => void open(row, action)}>{action}</RoomAction>)}
                {"format_revision" in row && canMarkUnavailable(row) && <button
                  type="button" className="download-update-mark" aria-label={`Mark ${row.title} as unavailable`}
                  aria-disabled={pending !== null} onClick={() => void open(row, "Mark as unavailable")}>Mark as unavailable</button>}
              </div>
            </li>)}
          </ul>
        </section>)}
      </div>
      {children}
    </DialogContent>
  </Dialog>;
}
