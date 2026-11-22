import { RoomIcon } from "../../components/RoomIcon";
import { RoomAlertContent } from "../../components/WindowContent";
import {
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
  type FormEvent,
} from "react";
import {
  AlertDialog,
  AlertDialogBackdrop,
  AlertDialogClose,
  AlertDialogDescription,
  AlertDialogTitle,
} from "@ui-sdk/react/alert-dialog";
import { Button } from "@ui-sdk/react/button";
import {
  Progress,
  ProgressIndicator,
  ProgressTrack,
} from "@ui-sdk/react/progress";
import {
  api,
  ApiError,
  fileSize,
  isHost,
  readable,
  shortPath,
  type DiscoverLocal,
  type Settings as SettingsData,
} from "../../api";
import { useStore } from "../../store";
import { notify } from "../../toasts";
import { PathField } from "../../components/PathField";
import { StateCard } from "../../components/StateCard";
import { PlaylistLinks } from "./PlaylistLinks";
import { errorText } from "./shared";

const IMPORT_EXTENSIONS = new Set(["csv", "m3u", "m3u8", "xspf", "zip"]);

function importFileIssue(file: File): string {
  const extension = file.name.split(".").pop()?.toLowerCase() || "";
  if (!IMPORT_EXTENSIONS.has(extension))
    return "Choose a CSV, M3U/M3U8, XSPF, or Listening Room ZIP";
  if (!file.size) return "This file is empty";
  if (file.size > (extension === "zip" ? 30 * 1024 * 1024 : 20 * 1024 * 1024))
    return `File exceeds ${extension === "zip" ? "30" : "20"} MB`;
  return "";
}

const fileBase64 = async (file: File) => {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let index = 0; index < bytes.length; index += 32768) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 32768));
  }
  return btoa(binary);
};

interface ImportConflict {
  fileName: string;
  conflicts: string[];
}

export function SourcesPanel({
  settings,
  discovered,
  discoveryDone,
  scanError,
  scanStatus,
  discoverLocal,
}: {
  settings: SettingsData | null;
  discovered: DiscoverLocal["folders"];
  discoveryDone: boolean;
  scanError: string;
  scanStatus: string;
  discoverLocal: () => Promise<void>;
}) {
  const store = useStore();
  const host = isHost(settings);
  const [folderPath, setFolderPath] = useState("");
  const [filePath, setFilePath] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [importing, setImporting] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(
    null,
  );
  const [importStatus, setImportStatus] = useState("");
  const [failures, setFailures] = useState<{ file: File; message: string }[]>([]);
  const [conflict, setConflict] = useState<ImportConflict | null>(null);
  const conflictResolve = useRef<((choice: "replace" | "rename") => void) | null>(
    null,
  );
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  const watched = settings?.source_folders ?? [];
  const linked = settings?.source_files ?? [];
  const watchedPaths = new Set(watched.map((item) => item.path));
  const candidates = discovered.filter(
    (item) => item.path !== settings?.lists_folder && !watchedPaths.has(item.path),
  );
  const issues = files.map(importFileIssue);
  const importDisabled =
    !host || importing || !files.length || issues.some(Boolean);

  const askConflict = (fileName: string, conflicts: string[]) =>
    new Promise<"replace" | "rename">((resolve) => {
      conflictResolve.current = resolve;
      setConflict({ fileName, conflicts });
    });

  const addFiles = (incoming: File[]) => {
    if (importing) return;
    setFiles((previous) => {
      const known = new Set(
        previous.map((file) => `${file.name}\0${file.size}\0${file.lastModified}`),
      );
      const next = [...previous];
      for (const file of incoming) {
        const key = `${file.name}\0${file.size}\0${file.lastModified}`;
        if (!known.has(key)) {
          next.push(file);
          known.add(key);
        }
      }
      return next;
    });
    setFailures([]);
    setImportStatus("");
    setProgress(null);
  };

  const importSelected = async () => {
    const selected = [...files];
    if (!selected.length || selected.some(importFileIssue) || importing) return;
    setImporting(true);
    setProgress({ done: 0, total: selected.length });
    const failed: { file: File; message: string }[] = [];
    let imported = 0;
    for (const [index, file] of selected.entries()) {
      setImportStatus(`Importing ${index + 1} of ${selected.length}: ${file.name}`);
      try {
        const zipped = file.name.toLowerCase().endsWith(".zip");
        const path = zipped ? "/api/import-bundle" : "/api/import-list";
        const payload = zipped
          ? { content: await fileBase64(file) }
          : { name: file.name, content: new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(await file.arrayBuffer()) };
        const limit = zipped ? 50 * 1024 * 1024 : 25 * 1024 * 1024;
        const send = async (extra: Record<string, unknown> = {}) => {
          const body = JSON.stringify({ ...payload, ...extra });
          if (new TextEncoder().encode(body).length > limit)
            throw new Error("Encoded file exceeds the import limit");
          return api(path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body,
          });
        };
        try {
          await send();
        } catch (error) {
          const conflicts =
            error instanceof ApiError
              ? (error.data as { conflicts?: unknown })?.conflicts
              : undefined;
          if (!Array.isArray(conflicts) || !conflicts.length) throw error;
          setImportStatus(`Choose how to import ${file.name}`);
          const choice = await askConflict(file.name, conflicts as string[]);
          await send({ on_conflict: choice });
        }
        imported++;
      } catch (error) {
        failed.push({ file, message: errorText(error, "Import failed") });
      }
      setProgress({ done: index + 1, total: selected.length });
    }
    setFiles(failed.map((item) => item.file));
    setImporting(false);
    setImportStatus(
      `${imported} imported${failed.length ? ` · ${failed.length} failed` : ""}`,
    );
    setFailures(failed);
    if (imported) {
      await Promise.allSettled([store.loadSettings(), store.refreshOverview()]);
      void store.loadTracks(true);
    }
  };

  const watchSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const path = folderPath.trim();
    if (!path) return notify("Enter a folder path to watch");
    if (await store.changeSource("watch", path)) setFolderPath("");
  };

  const linkSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const path = filePath.trim();
    if (!path) return notify("Enter a playlist file path to link");
    if (await store.changeSource("link", path)) setFilePath("");
  };

  const onDrop = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    setDragging(false);
    if (!host || importing) return;
    addFiles([...event.dataTransfer.files]);
  };

  return (
    <>
      <div className="settings-section-title">
        <div>
          <h2>Music sources</h2>
          <p>
            Imported playlists are saved in your library. Watch a folder to keep its lists in sync.
          </p>
        </div>
        <Button
          className="quiet-button"
          type="button"
          disabled={!host}
          onClick={() => void discoverLocal()}
        >
          Search this computer
        </Button>
      </div>
      <div className="settings-stack">
        <div className="settings-card import-card">
          <h3>Import music lists</h3>
          <label
            className={`import-drop-zone${!host || importing ? " is-disabled" : ""}${dragging ? " is-dragging" : ""}`}
            htmlFor="listImportFiles"
            onDragOver={(event: DragEvent<HTMLLabelElement>) => {
              if (!event.dataTransfer?.types.includes("Files")) return;
              event.preventDefault();
              if (!host || importing) {
                event.dataTransfer.dropEffect = "none";
                return;
              }
              event.dataTransfer.dropEffect = "copy";
              setDragging(true);
            }}
            onDragLeave={(event: DragEvent<HTMLLabelElement>) => {
              if (!event.currentTarget.contains(event.relatedTarget as Node))
                setDragging(false);
            }}
            onDrop={onDrop}
          >
            <input
              id="listImportFiles"
              ref={fileInputRef}
              type="file"
              accept=".csv,.m3u,.m3u8,.xspf,.zip,text/csv,application/xspf+xml,application/zip"
              aria-describedby="importHint importStatus"
              multiple
              disabled={!host || importing}
              onChange={(event: ChangeEvent<HTMLInputElement>) => {
                addFiles([...(event.target.files ?? [])]);
                event.target.value = "";
              }}
            />
            <span className="import-drop-icon" aria-hidden="true">
              <RoomIcon name="up" />
            </span>
            <span className="import-drop-copy">
              <strong>Drop playlist files here</strong>
            </span>
            <span className="import-browse" aria-hidden="true">
              Choose files
            </span>
          </label>
          <p id="importHint" className="import-hint">
            CSV, M3U/M3U8, XSPF · up to 20 MB each. Listening Room ZIP · up to
            30 MB.
          </p>
          <div className="import-selection" aria-live="polite">
            {files.length ? (
              files.map((file, index) => (
                <div
                  className={`import-file${issues[index] ? " is-invalid" : ""}`}
                  key={`${file.name}\0${file.size}\0${file.lastModified}`}
                >
                  <span className="import-file-type" aria-hidden="true">
                    {file.name.split(".").pop()?.toUpperCase()}
                  </span>
                  <span className="import-file-name" title={file.name}>
                    <strong>{file.name}</strong>
                    <small>
                      {fileSize(file.size)}
                      {issues[index] ? ` · ${issues[index]}` : ""}
                    </small>
                  </span>
                  <button
                    type="button"
                    aria-label={`Remove ${file.name}`}
                    disabled={importing}
                    onClick={() => {
                      setFiles((previous) =>
                        previous.filter((_, itemIndex) => itemIndex !== index),
                      );
                    }}
                  >
                    <RoomIcon name="close" />
                  </button>
                </div>
              ))
            ) : (
              <span className="import-selection-empty">No files selected yet</span>
            )}
          </div>
          <div className="settings-actions import-actions">
            <Button
              className="primary-action"
              type="button"
              disabled={importDisabled}
              onClick={() => void importSelected()}
            >
              {importing
                ? "Importing…"
                : files.length
                  ? `Import ${files.length} file${files.length === 1 ? "" : "s"}`
                  : "Import files"}
            </Button>
            <span className="import-summary">
              {!host
                ? "Import on the server computer"
                : issues.some(Boolean)
                  ? "Remove files marked in red to continue"
                  : files.length
                    ? `${files.length} file${files.length === 1 ? "" : "s"} ready to import`
                    : "Choose one or more files to continue"}
            </span>
          </div>
          {progress ? (
            <Progress
              className="import-progress"
              value={progress.done}
              max={progress.total}
              aria-label="Import progress"
            >
              <ProgressTrack>
                <ProgressIndicator />
              </ProgressTrack>
            </Progress>
          ) : null}
          <div
            id="importStatus"
            className="settings-note import-status"
            role="status"
            aria-live="polite"
          >
            {importStatus}
          </div>
          {failures.length ? (
            <div className="import-errors">
              {failures.map(({ file, message }) => (
                <p key={`${file.name}\0${file.size}\0${file.lastModified}`}>
                  <strong>{file.name}</strong>
                  <span>{message}</span>
                </p>
              ))}
            </div>
          ) : null}
        </div>

        <div className="settings-card">
          <div className="settings-card-heading">
            <h3>Found on this computer</h3>
            <span className="settings-note">{scanStatus}</span>
          </div>
          <div className="source-list">
            {candidates.length ? (
              candidates.map((item) => (
                <div className="source-item" key={item.path}>
                  <div>
                    <strong>{item.name}</strong>
                    <span title={item.path}>
                      {shortPath(item.path)} · {readable(item.count)} list{" "}
                      {item.count === 1 ? "file" : "files"}
                    </span>
                  </div>
                  <button
                    className="quiet-button"
                    type="button"
                    onClick={() => void store.changeSource("watch", item.path)}
                  >
                    Watch folder
                  </button>
                </div>
              ))
            ) : !settings && store.settingsError ? (
              <StateCard
                kind="error"
                icon="alert"
                label="Sources"
                title="Settings could not load"
                body={store.settingsError}
                compact
                action={
                  <button
                    className="quiet-button"
                    type="button"
                    onClick={() => void store.loadSettings()}
                  >
                    Try again
                  </button>
                }
              />
            ) : scanError ? (
              <StateCard
                kind="error"
                icon="alert"
                label="Local scan"
                title="Folder search did not finish"
                body={scanError}
                compact
                action={
                  <button
                    className="quiet-button"
                    type="button"
                    onClick={() => void discoverLocal()}
                  >
                    Try again
                  </button>
                }
              />
            ) : discoveryDone ? (
              <StateCard
                icon="search"
                label="Local scan"
                title={host ? "No other folders found" : "Folder search runs on the host"}
                body={
                  host
                    ? "You can import playlist files below or enter a folder path."
                    : "Open Settings on the server computer to scan its folders."
                }
                compact
              />
            ) : (
              <StateCard
                kind="loading"
                icon="search"
                label="Local scan"
                title="Looking for music lists"
                body="Checking common folders on this computer."
                compact
              />
            )}
          </div>
        </div>
        <div className="settings-card">
          <div className="settings-card-heading">
            <h3>Watched folders</h3>
            <button
              className="quiet-button"
              type="button"
              disabled={!host}
              onClick={() => void store.syncSources()}
            >
              Check for changes
            </button>
          </div>
          <p>
            New and edited playlists in these folders sync automatically.
          </p>
          <div className="source-list">
            {watched.length ? (
              watched.map((item) => (
                <div className="source-item" key={item.path}>
                  <div>
                    <strong>{shortPath(item.path)}</strong>
                    <span title={item.path}>
                      {readable(item.count)} list{" "}
                      {item.count === 1 ? "file" : "files"} · watching for
                      changes
                    </span>
                  </div>
                  <button
                    className="quiet-button"
                    type="button"
                    onClick={() => void store.changeSource("unwatch", item.path)}
                  >
                    Stop watching
                  </button>
                </div>
              ))
            ) : (
              <StateCard
                icon="library"
                label="Watched folders"
                title="No folders watched"
                body="Imported files and connected playlists still appear in your library."
                compact
              />
            )}
          </div>
          {host ? (
            <form className="source-add-form" onSubmit={watchSubmit}>
              <PathField
                name="source_folder"
                label="Add a folder by path"
                mode="folder"
                placeholder="Folder on this computer"
                value={folderPath}
                onValueChange={setFolderPath}
              />
              <div className="settings-actions">
                <Button className="quiet-button" type="submit">
                  Watch folder
                </Button>
              </div>
            </form>
          ) : null}
        </div>
        <div className="settings-card">
          <h3>Linked playlists</h3>
          <p>
            Keep one CSV, M3U, or XSPF file in sync as you edit it. The file
            stays in its original folder.
          </p>
          <div className="source-list">
            {linked.length ? (
              linked.map((item) => (
                <div className="source-item" key={item.path}>
                  <div>
                    <strong>{item.path.split(/[\\/]/).pop()}</strong>
                    <span title={item.path}>
                      {shortPath(item.path)} ·{" "}
                      {item.available
                        ? "linked and checking for changes"
                        : "file missing; last copy kept"}
                    </span>
                  </div>
                  <button
                    className="quiet-button"
                    type="button"
                    onClick={() => void store.changeSource("unlink", item.path)}
                  >
                    Stop linking
                  </button>
                </div>
              ))
            ) : (
              <StateCard
                icon="northeast"
                label="Linked playlists"
                title="No files linked"
                body="Link a playlist file above to keep its changes in sync."
                compact
              />
            )}
          </div>
          {host ? (
            <form className="source-add-form" onSubmit={linkSubmit}>
              <PathField
                name="source_file"
                label="Playlist file on this computer"
                mode="file"
                placeholder="Path to a playlist file"
                value={filePath}
                onValueChange={setFilePath}
              />
              <div className="settings-actions">
                <Button className="quiet-button" type="submit">
                  Import &amp; link
                </Button>
              </div>
            </form>
          ) : null}
        </div>
        <PlaylistLinks revision={importStatus + scanStatus} />
      </div>
      <AlertDialog
        open={!!conflict}
        onOpenChange={(
          open: boolean,
          details: { returnValue?: string },
        ) => {
          if (open) return;
          const choice = details.returnValue === "replace" ? "replace" : "rename";
          conflictResolve.current?.(choice);
          conflictResolve.current = null;
          setConflict(null);
        }}
      >
        <AlertDialogBackdrop />
        <RoomAlertContent className="import-conflict-dialog">
          <AlertDialogTitle>{conflict?.fileName}</AlertDialogTitle>
          <AlertDialogDescription>
            {conflict
              ? `Nothing was imported. ${conflict.conflicts.length === 1 ? "This list already exists" : "These lists already exist"} with different content: ${conflict.conflicts.join(", ")}. Replace the existing version, or keep both under a new name?`
              : ""}
          </AlertDialogDescription>
          <div className="settings-actions import-conflict-actions">
            <AlertDialogClose className="quiet-button" value="rename">
              Keep both
            </AlertDialogClose>
            <AlertDialogClose className="primary-action" value="replace">
              Replace
            </AlertDialogClose>
          </div>
        </RoomAlertContent>
      </AlertDialog>
    </>
  );
}
