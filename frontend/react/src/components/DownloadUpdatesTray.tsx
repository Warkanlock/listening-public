import { useEffect, useRef, useState, type ReactNode } from "react";
import { Dialog, DialogBackdrop, DialogClose, DialogContent, DialogTitle } from "@ui-sdk/react/dialog";
import { api, type DownloadOutcome, type RequestRow } from "../api";
import { canChooseFormat, needsCopyChoice, requestStage } from "../requestState";
import { useStore } from "../store";
import { RoomAction } from "./RoomControls";
import { RoomIcon } from "./RoomIcon";
import { RequestStatus } from "./RequestStatus";

const groups = ["Needs review", "Ready to play", "No copy found", "In progress"] as const;

function outcomeGroup(row: DownloadOutcome | RequestRow): typeof groups[number] {
  const state = requestStage(row);
  if (state === "ready") return "Ready to play";
  if (state === "no_matches") return "No copy found";
  return state === "copy_missing" || canChooseFormat(state) ? "Needs review" : "In progress";
}

function outcomeAction(row: DownloadOutcome | RequestRow) {
  const stage = requestStage(row);
  if (stage === "ready") return "Play";
  if (needsCopyChoice(stage)) return "Review copies";
  if (canChooseFormat(stage)) return "Change format";
  return null;
}

export function DownloadUpdatesTray({ outcomes, children }: { outcomes: DownloadOutcome[]; children: ReactNode }) {
  const store = useStore();
  const operation = useRef<AbortController | null>(null);
  const [pending, setPending] = useState<number | null>(null);
  const [message, setMessage] = useState("");
  useEffect(() => () => operation.current?.abort(), []);
  const rows = outcomes.map(outcome => ({ outcome,
    row: store.requestRows.find(request => request.id === outcome.id) ?? outcome }));

  async function open(outcome: DownloadOutcome, action: NonNullable<ReturnType<typeof outcomeAction>>) {
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
      if (!row || outcomeAction(row) !== action || (action === "Play" && !row.audio)) {
        setMessage(`${outcome.title} has changed. Check its current status below.`);
      } else if (action === "Play") store.playRequest(row);
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
        <DialogTitle>Download updates</DialogTitle>
        <DialogClose className="quiet-button icon-control" aria-label="Close download updates"><RoomIcon name="close" /></DialogClose>
      </header>
      {message && <p role="status">{message}</p>}
      <div className="download-updates-groups">
        {groups.map(group => {
          const items = rows.filter(({ row }) => outcomeGroup(row) === group);
          return items.length > 0 && <section key={group} aria-label={group}>
            <h3>{group}</h3>
            <ul>
              {items.map(({ outcome, row }) => {
                const action = outcomeAction(row);
                return <li key={outcome.key}>
                  <div className="download-updates-song"><strong>{row.title}</strong>
                    <small>{row.artist}</small><RequestStatus request={row} /></div>
                  {action && <RoomAction icon={action === "Play" ? "play" : action === "Review copies" ? "catalogs" : "settings"}
                    aria-label={`${action} ${row.title}`} aria-disabled={pending !== null}
                    onClick={() => void open(outcome, action)}>
                    {pending === outcome.id ? "Loading…" : action}
                  </RoomAction>}
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
