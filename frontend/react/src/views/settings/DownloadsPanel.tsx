import { RoomSelect } from "../../components/RoomControls";
import { useState, type FormEvent } from "react";
import { Button } from "@ui-sdk/react/button";
import { Field, FieldLabel } from "@ui-sdk/react/field";
import { Input } from "@ui-sdk/react/input";
import { Switch, SwitchThumb } from "@ui-sdk/react/switch";
import { BULK_DOWNLOAD_FORMATS, isBulkDownloadFormat, type Settings as SettingsData } from "../../api";
import { PathField } from "../../components/PathField";
import type { FormState } from "./shared";

export function DownloadsPanel({
  settings,
  form,
  setForm,
  detectedSockseek,
  onSave,
}: {
  settings: SettingsData | null;
  form: FormState;
  setForm: (updater: (previous: FormState) => FormState) => void;
  detectedSockseek: string;
  onSave: (message: string) => Promise<boolean>;
}) {
  const [saving, setSaving] = useState(false);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    await onSave("Download setup saved");
    setSaving(false);
  };

  return (
    <>
      <div className="settings-section-title">
        <div>
          <h2>Downloads</h2>
          <p>Search for missing recordings. Downloads run on the room server.</p>
        </div>
        <span className="settings-health">
          {settings
            ? settings.download_ready
              ? "Ready to search"
              : "Needs setup"
            : "Checking tools…"}
        </span>
      </div>
      <div className="settings-stack">
        <div className="settings-card">
          <h3>Soulseek account</h3>
          <p className="settings-note">
            Use your Soulseek credentials, or choose an unused username and a password to create an account.
          </p>
          <form className="settings-form" onSubmit={submit}>
            <PathField
              name="sockseek_binary"
              label="Sockseek app"
              mode="file"
              required
              value={form.sockseek}
              onValueChange={(value) =>
                setForm((previous) => ({ ...previous, sockseek: value }))
              }
            />
            <Field name="soulseek_username">
              <FieldLabel>Soulseek username</FieldLabel>
              <Input
                autoComplete="username"
                placeholder="Username"
                value={form.soulseekUsername}
                onValueChange={(value) =>
                  setForm((previous) => ({
                    ...previous,
                    soulseekUsername: value,
                  }))
                }
              />
            </Field>
            <Field name="soulseek_password">
              <FieldLabel>Soulseek password</FieldLabel>
              <Input
                type="password"
                autoComplete="new-password"
                placeholder="Leave blank to keep saved password"
                value={form.soulseekPassword}
                onValueChange={(value) =>
                  setForm((previous) => ({
                    ...previous,
                    soulseekPassword: value,
                  }))
                }
              />
            </Field>
            <Field name="bulk_download_format">
              <RoomSelect label="Default format for bulk downloads" describedBy="bulk-download-format-help"
                value={form.bulkDownloadFormat} options={Object.entries(BULK_DOWNLOAD_FORMATS).map(([value, label]) => ({ value, label }))}
                onChange={value => { if (isBulkDownloadFormat(value)) setForm(previous => ({ ...previous, bulkDownloadFormat: value })); }} />
              <p id="bulk-download-format-help" className="settings-help">MP3 first also accepts FLAC or Apple Lossless. Choose a format for one run in Jobs.</p>
            </Field>
            <Field name="auto_download">
              <div className="check-setting">
                <FieldLabel>Keep searching in the background</FieldLabel>
                <Switch
                  checked={form.autoDownload}
                  onCheckedChange={(checked: boolean) =>
                    setForm((previous) => ({
                      ...previous,
                      autoDownload: checked,
                    }))
                  }
                >
                  <SwitchThumb />
                </Switch>
              </div>
            </Field>
            <div className="settings-actions">
              <Button className="primary-action" type="submit" disabled={saving}>
                Save download setup
              </Button>
              {detectedSockseek && settings && !settings.dependencies.sockseek ? (
                <Button
                  className="quiet-button"
                  type="button"
                  onClick={() =>
                    setForm((previous) => ({
                      ...previous,
                      sockseek: detectedSockseek,
                    }))
                  }
                >
                  Use detected Sockseek
                </Button>
              ) : null}
            </div>
          </form>
          <div className="settings-note">
            {settings
              ? Object.entries(settings.dependencies)
                  .map(([name, ready]) => `${ready ? "✓" : "○"} ${name}`)
                  .join(" · ")
              : ""}
          </div>
          <p className="settings-help">To install Sockseek on the server, run <code>listening setup --downloads</code>.</p>
        </div>
      </div>
    </>
  );
}
