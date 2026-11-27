import { RoomAction, RoomSelect } from "../../components/RoomControls";
import { useState, type FormEvent } from "react";
import { Field, FieldLabel } from "@ui-sdk/react/field";
import { Switch, SwitchThumb } from "@ui-sdk/react/switch";
import { BULK_DOWNLOAD_FORMATS, isBulkDownloadFormat, type Settings } from "../../api";
import type { FormState } from "./shared";

export function DownloadsPanel({
  settings,
  form,
  setForm,
  onSave,
  onOpenExtensions,
}: {
  settings: Settings;
  form: FormState;
  setForm: (updater: (previous: FormState) => FormState) => void;
  onSave: (message: string) => Promise<boolean>;
  onOpenExtensions: () => void;
}) {
  const [saving, setSaving] = useState(false);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    await onSave("Download settings saved");
    setSaving(false);
  };

  return (
    <>
      <div className="settings-section-title">
        <div>
          <h2>Downloads</h2>
          <p>Choose the default format and whether to search in the background.</p>
        </div>
        <span className="settings-health" data-ready={settings.download_ready}>{settings.download_ready ? "Ready to search" : "Needs setup"}</span>
      </div>
      <div className="settings-card">
        <form className="settings-form" onSubmit={submit}>
          <Field name="bulk_download_format">
            <RoomSelect label="Default format for bulk downloads" describedBy="bulk-download-format-help"
              value={form.bulkDownloadFormat} options={Object.entries(BULK_DOWNLOAD_FORMATS).map(([value, label]) => ({ value, label }))}
              onChange={value => { if (isBulkDownloadFormat(value)) setForm(previous => ({ ...previous, bulkDownloadFormat: value })); }} />
            <p id="bulk-download-format-help" className="settings-help">MP3 first also accepts FLAC or Apple Lossless. Choose a format for one run in Jobs.</p>
          </Field>
          <Field name="auto_download">
            <div className="check-setting">
              <FieldLabel>Keep searching in the background</FieldLabel>
              <Switch checked={form.autoDownload}
                onCheckedChange={autoDownload => setForm(previous => ({ ...previous, autoDownload }))}>
                <SwitchThumb />
              </Switch>
            </div>
          </Field>
          <div className="settings-actions">
            <RoomAction primary type="submit" disabled={saving}>Save download settings</RoomAction>
            <RoomAction onClick={onOpenExtensions}>Manage download sources</RoomAction>
          </div>
        </form>
      </div>
    </>
  );
}
