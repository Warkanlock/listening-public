import { useEffect, useRef, useState, type ReactNode } from "react";
import { Dialog, DialogBackdrop, DialogClose, DialogContent, DialogTitle } from "@ui-sdk/react/dialog";
import { api, type RequestRow } from "../api";
import { canChooseFormat, needsCopyChoice, requestNeedsAttention, requestStage } from "../requestState";
import type { DownloadTray } from "../slices/requests";
import { useStore } from "../store";
import { RoomAction } from "./RoomControls";
import { RoomIcon } from "./RoomIcon";
import { RequestStatus } from "./RequestStatus";

const groups = ["Needs review", "Ready to play", "No copy found", "Unavailable", "In progress"] as const;

type TrayRequest = DownloadTray["rows"][number];
type TrayAction = "Play" | "Review copies" | "Choose format" | "Change format" | "Retry" | "Download again";

function outcomeGroup(row: TrayRequest): typeof groups[number] {
  const state = requestStage(row);
  if (state === "ready") return "Ready to play";
  if (state === "no_matches") return "No copy found";
  if (state === "unavailable") return "Unavailable";
  return state === "copy_missing" || canChooseFormat(state) ? "Needs review" : "In progress";
}

function outcomeActions(row: TrayRequest, source: DownloadTray["source"]): TrayAction[] {
  const stage = requestStage(row);
  if (stage === "ready") return ["Play"];
  if (stage === "copy_missing") return ["Download again"];
  if (needsCopyChoice(stage)) return ["Review copies"];
  if (stage === "choose_format") return ["Choose format"];
  if (source !== "updates" && (stage === "search_failed" || stage === "verification_failed")) return ["Retry", "Change format"];
  return canChooseFormat(stage) ? ["Change format"] : [];
}

export function DownloadUpdatesTray({ tray, children }: { tray: DownloadTray; children: ReactNode }) {
  const store = useStore();
  const operation = useRef<AbortController | null>(null);
  const [pending, setPending] = useState<number | null>(null);
  const [message, setMessage] = useState("");
  useEffect(() => () => operation.current?.abort(), []);
  const title = tray.source === "weekly" ? "Weekly Discovery needs attention"
    : tray.source === "requests" ? "Requests needing attention" : "Download updates";
  const current = tray.source === "requests" ? store.requestsSlice.attentionRows
    : tray.source === "updates"
    ? tray.rows.map(seed => store.requestRows.find(request => request.id === seed.id) ?? seed) : tray.rows;
  const rows = current
    .filter(row => tray.source === "updates" || requestNeedsAttention(requestStage(row)));

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
      if (!row || !outcomeActions(row, tray.source).includes(action) || (action === "Play" && !row.audio) ||
          (retry && row.format_revision !== outcome.format_revision)) {
        setMessage(`${outcome.title} has changed. Check its current status below.`);
      } else if (action === "Play") store.playRequest(row);
      else if (retry) {
        const queued = await store.requestsSlice.retryRequest(row.id, undefined, row.format_revision,
          { restoreMissing: action === "Download again", onError: error => { if (!abort.signal.aborted) setMessage(error); } });
        if (queued && !abort.signal.aborted) setMessage(`${row.title} queued for retry.`);
      }
      else store.requestsSlice.chooseFormat(row);
    } catch (error) {
      if (!abort.signal.aborted) setMessage(error instanceof Error ? error.message : "Couldn’t load the request. Try again.");
    } finally {
      operation.current = null;
      if (!abort.signal.aborted) setPending(null);
    }
  }

  return <Dialog open onOpenChange={open => { if (!open) store.requestsSlice.closeDownloadTray(); }}>
    <DialogBackdrop />
    <DialogContent className="download-updates-tray">
      <header className="download-updates-head">
        <DialogTitle>{title}</DialogTitle>
        <DialogClose className="quiet-button icon-control" aria-label={tray.source === "updates" ? "Close download updates" : "Close attention list"}><RoomIcon name="close" /></DialogClose>
      </header>
      {message && <p role="status">{message}</p>}
      <div className="download-updates-groups">
        {rows.length === 0 && <p>No requests need attention.</p>}
        {groups.map(group => {
          const items = rows.filter(row => outcomeGroup(row) === group);
          return items.length > 0 && <section key={group} aria-label={group}>
            <h3>{group}</h3>
            <ul>
              {items.map(row => {
                const actions = outcomeActions(row, tray.source);
                return <li key={row.id}>
                  <div className="download-updates-song"><strong>{row.title}</strong>
                    {row.artist && <small>{row.artist}</small>}<RequestStatus request={row} />
                    {tray.source !== "updates" && row.message && row.message !== "No playable copy in the library" && <small>{row.message}</small>}</div>
                  {actions.length > 0 && <div className="download-updates-actions">
                    {actions.map(action => <RoomAction key={action}
                      icon={action === "Play" ? "play" : action === "Review copies" ? "catalogs"
                        : action === "Retry" ? "refresh" : action === "Download again" ? "download" : "settings"}
                      aria-label={`${action} ${row.title}`} aria-disabled={pending !== null}
                      onClick={() => void open(row, action)}>{action}</RoomAction>)}
                  </div>}
                  {pending === row.id && <small role="status">Loading {row.title}…</small>}
                </li>;
              })}
            </ul>
          </section>;
        })}
      </div>
      {children}
    </DialogContent>
  </Dialog>;
}
