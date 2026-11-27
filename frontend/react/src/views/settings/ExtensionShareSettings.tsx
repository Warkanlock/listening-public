import { useEffect, useId, useState, type FormEvent } from "react";
import { postJson } from "../../api";
import { RoomAction } from "../../components/RoomControls";
import { errorText } from "./shared";

type ShareCommand = { operation: "status" | "stop" } | { operation: "start"; folder: string };
type ShareStatus = { running: boolean; folder: string; available: boolean };

async function requestShare(engine: string, command: ShareCommand, signal?: AbortSignal): Promise<ShareStatus> {
  const reply = await postJson<unknown>("/api/extensions", { action: "share", engine, ...command }, signal);
  if (typeof reply !== "object" || reply === null ||
      !("running" in reply) || typeof reply.running !== "boolean" ||
      !("folder" in reply) || typeof reply.folder !== "string" ||
      !("available" in reply) || typeof reply.available !== "boolean") {
    throw new Error("The extension returned an invalid sharing status.");
  }
  return { running: reply.running, folder: reply.folder, available: reply.available };
}

export function ExtensionShareSettings({ engine, busy, setSaving }: {
  engine: string; busy: boolean; setSaving: (saving: boolean) => void;
}) {
  const [status, setStatus] = useState<ShareStatus>();
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [folder, setFolder] = useState<string>();
  const helpId = useId();

  useEffect(() => {
    const controller = new AbortController();
    void requestShare(engine, { operation: "status" }, controller.signal).then(reply => {
      if (!controller.signal.aborted) setStatus(reply);
    }).catch(failure => {
      if (!controller.signal.aborted) setError(errorText(failure, "Could not check sharing."));
    });
    return () => controller.abort();
  }, [engine, attempt]);

  const change = async (command: ShareCommand) => {
    if (busy) return;
    setSaving(true);
    setError("");
    try {
      setStatus(await requestShare(engine, command));
      setFolder(undefined);
    } catch (failure) {
      setError(errorText(failure, "Could not change sharing."));
    } finally {
      setSaving(false);
    }
  };

  const start = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!status?.available || status.running) return;
    void change({ operation: "start", folder: folder ?? status.folder });
  };

  return <form className="settings-form" data-settings-share aria-label={`Library sharing with ${engine}`} onSubmit={start}>
    <header className="settings-card-heading">
      <h3>Library sharing</h3>
      {status && <span className="settings-health" data-ready={status.running} role="status">
        {status.running ? "Sharing is running." :
          status.available ? "Sharing is stopped." : "The server’s sharing folder is unavailable."}
      </span>}
    </header>
    {error && <p role="alert" className="settings-note">{error}</p>}
    {!status ? (
      error ? <div className="settings-actions">
        <RoomAction disabled={busy} onClick={() => { setError(""); setAttempt(value => value + 1); }}>
          Retry checking sharing
        </RoomAction>
      </div> : <p role="status" className="settings-note">Checking sharing…</p>
    ) : <>
      {status.available && <>
        <label>
          Folder to share
          <input name="folder" autoComplete="off" spellCheck={false} placeholder="Entire sharing folder"
            value={folder ?? status.folder} onChange={event => setFolder(event.target.value)}
            aria-describedby={helpId} readOnly={status.running} disabled={busy} />
        </label>
        <p id={helpId} className="settings-note">
          Files are shared with other users on this extension’s network. Enter a subfolder path or leave it blank to share the entire folder configured on the server.
        </p>
      </>}
      {(status.running || status.available) && <div className="settings-actions">
        <RoomAction primary={!status.running} type={status.running ? "button" : "submit"}
          onClick={status.running ? () => void change({ operation: "stop" }) : undefined}
          disabled={busy}>
          {status.running ? "Stop sharing" : "Start sharing"}
        </RoomAction>
      </div>}
    </>}
  </form>;
}
