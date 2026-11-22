import { RoomSelect, audioExportFormats } from "../../components/RoomControls";
import { useEffect, useState, type ChangeEvent, type FormEvent } from "react";
import { Button } from "@ui-sdk/react/button";
import { Field, FieldLabel } from "@ui-sdk/react/field";
import { Input } from "@ui-sdk/react/input";
import {
  api,
  derivedStoragePaths,
  postJson,
  shortPath,
  type Settings as SettingsData,
} from "../../api";
import { notify } from "../../toasts";
import { PathField } from "../../components/PathField";
import { type FormState } from "./shared";

function storageSize(bytes: number) {
  if (bytes < 1024 ** 2) return `${Math.round(bytes / 1024)} KiB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
}

export function StoragePanel({
  settings,
  form,
  setForm,
  onSave,
}: {
  settings: SettingsData | null;
  form: FormState;
  setForm: (updater: (previous: FormState) => FormState) => void;
  onSave: (message: string) => Promise<boolean>;
}) {
  const [saving, setSaving] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportFormat, setExportFormat] = useState("original");
  const [exportNote, setExportNote] = useState("");
  const derived = derivedStoragePaths(settings, form.folder.trim());
  const collection = settings?.collection;

  type ExportProgress = SettingsData["export_status"] & { active?: boolean };
  const [progress, setProgress] = useState<ExportProgress | null>(null);
  useEffect(() => {
    let mounted = true;
    const refresh = async () => {
      try {
        const value = await api<ExportProgress>("/api/export/status");
        if (mounted) setProgress(value);
      } catch {
        // The next tick retries a transient connection failure.
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 2000);
    return () => { mounted = false; window.clearInterval(timer); };
  }, []);
  const exportStatus = progress ?? settings?.export_status;
  const exportLine = exportNote || (exportStatus?.state === "running"
    ? `Processed ${exportStatus.done} of ${exportStatus.total} songs.`
    : exportStatus?.state === "done"
      ? `Export complete. ${exportStatus.written ?? exportStatus.done - exportStatus.failed} written, ${exportStatus.skipped ?? 0} already present, ${exportStatus.failed} failed.${exportStatus.destination ? " " + exportStatus.destination : ""}`
      : "");

  const startExport = async () => {
    if (exporting) return;
    setExporting(true);
    try {
      await postJson("/api/export", { format: exportFormat });
      setExportNote("");
      notify("Export started");
    } catch (error) {
      setExportNote(
        error instanceof Error ? error.message : "The export could not start",
      );
    }
    setExporting(false);
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    await onSave("Storage paths saved");
    setSaving(false);
  };

  const loadSettingsJson = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    try {
      const imported = JSON.parse(await file.text()) as Record<string, unknown>;
      setForm((previous) => ({
        ...previous,
        folder:
          !collection && typeof imported.folder === "string" && imported.folder
            ? imported.folder
            : previous.folder,
        shareFolder:
          typeof imported.share_folder === "string" && imported.share_folder
            ? imported.share_folder
            : previous.shareFolder,
        autoDownload:
          imported.auto_download !== undefined
            ? !!imported.auto_download
            : previous.autoDownload,
      }));
      notify("Settings loaded. Review and save the values in their settings panels.");
    } catch {
      notify("Choose the settings.json inside a Listening Room export");
    }
  };

  return (
    <>
      <div className="settings-section-title">
        <div>
          <h2>Storage</h2>
          <p>
            {collection
              ? "The collection database holds your playlists, catalog, and original audio."
              : "Choose where this room stores music."}
          </p>
        </div>
      </div>
      {collection ? (
        <div className="settings-card">
          <Field name="collection_database">
            <FieldLabel>Server collection database</FieldLabel>
            <Input value={collection.path} readOnly />
          </Field>
          <p className="settings-note">{storageSize(collection.bytes)} database file</p>
          <details>
            <summary>Working folders on the server</summary>
            <p className="settings-note">
              The server uses these folders to read incoming files and prepare playlists for other players.
            </p>
            {[
              ["list_workspace", "Playlist working copies", collection.list_workspace],
              ["music_folder", "Audio inbox", settings.music_folder],
              ["playlist_output", "Player playlists", settings.playlist_output],
            ].map(([name, label, path]) => (
              <Field key={name} name={name}>
                <FieldLabel>{label}</FieldLabel>
                <Input value={path} readOnly />
              </Field>
            ))}
          </details>
        </div>
      ) : (
        <>
          <div className="settings-card">
            <form className="settings-form" onSubmit={submit}>
              <PathField
                name="folder"
                label="Library folder"
                mode="folder"
                required
                value={form.folder}
                onValueChange={(value) =>
                  setForm((previous) => ({ ...previous, folder: value }))
                }
              />
              <Field name="lists_folder">
                <FieldLabel>Portable lists</FieldLabel>
                <Input value={derived.lists_folder} readOnly />
              </Field>
              <Field name="music_folder">
                <FieldLabel>Audio files</FieldLabel>
                <Input value={derived.music_folder} readOnly />
              </Field>
              <Field name="playlist_output">
                <FieldLabel>Player playlists</FieldLabel>
                <Input value={derived.playlist_output} readOnly />
              </Field>
              <div className="settings-actions">
                <Button className="primary-action" type="submit" disabled={saving}>
                  Save storage paths
                </Button>
              </div>
            </form>
            <div className="settings-note">
              {settings
                ? `${settings.list_files.length} lists stored in ${shortPath(settings.lists_folder)}`
                : ""}
            </div>
          </div>
          <div className="settings-card">
            <h3>Where downloads land</h3>
            <form className="settings-form" onSubmit={submit}>
              <RoomSelect label="New downloads land in" value={form.audioStore} describedBy="download-storage-help"
                options={[{ value: "files", label: "Files" }, { value: "database", label: "The media database" }]}
                onChange={value => setForm(previous => ({ ...previous, audioStore: value === "database" ? "database" : "files" }))} />
              <p className="settings-note" id="download-storage-help">
                {form.audioStore === "database"
                  ? "New downloads go into the database. Export them in their original format at any time."
                  : "New downloads go into the audio folder. Existing database recordings remain available."}
              </p>
              <div className="settings-actions">
                <Button className="primary-action" type="submit" disabled={saving}>
                  Save download storage
                </Button>
              </div>
            </form>
          </div>
        </>
      )}
      <div className="settings-card">
        <h3>Export library audio</h3>
        {settings?.media && (
          <div className="settings-note">
            {`${settings.media.tracks} audio files · ${storageSize(settings.media.stored_bytes)} stored · ${storageSize(settings.media.audio_bytes)} original`}
            {settings.media.dead_bytes > 0 && ` · ${storageSize(settings.media.dead_bytes)} reclaimable`}
          </div>
        )}
        {collection && (
          <Field name="export_folder">
            <FieldLabel>Export folder</FieldLabel>
            <Input value={collection.export_folder} readOnly />
          </Field>
        )}
        <div className="settings-actions">
          <RoomSelect label="Library export format" hideLabel value={exportFormat}
            options={audioExportFormats} onChange={setExportFormat} />
          <Button
            className="quiet-button"
            disabled={exporting || progress?.active}
            onClick={() => void startExport()}
          >
            Export library audio
          </Button>
        </div>
        {exportLine && <div className="settings-note">{exportLine}</div>}
      </div>
      <div className="settings-card">
        <h3>Playlists and host settings</h3>
        <p>Export the playlists as a portable ZIP, or save this host’s settings as JSON.</p>
        <div className="settings-actions">
          <a className="quiet-button" href="/api/export" download>
            Export playlists
          </a>
          <a className="quiet-button" href="/api/export/settings" download>
            Export host settings
          </a>
          <label className="quiet-button import-label">
            Load settings JSON
            <input
              type="file"
              accept="application/json,.json"
              hidden
              onChange={(event) => void loadSettingsJson(event)}
            />
          </label>
        </div>
      </div>
    </>
  );
}
