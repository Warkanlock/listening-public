import { type FormEvent } from "react";
import { Button } from "@ui-sdk/react/button";
import { Field, FieldLabel } from "@ui-sdk/react/field";
import { Input } from "@ui-sdk/react/input";
import { isHost } from "../../api";
import { useStore } from "../../store";
import { PathField } from "../../components/PathField";
import { type FormState } from "./shared";

export function SharingPanel({
  form,
  setForm,
  onSave,
}: {
  form: FormState;
  setForm: (updater: (previous: FormState) => FormState) => void;
  onSave: (message: string) => Promise<boolean>;
}) {
  const store = useStore();
  const process = store.processSnapshot;
  if (!isHost(store.settings)) {
    return (
      <div className="settings-section-title">
        <div>
          <h2>Sharing</h2>
          <p>Manage Soulseek sharing on the server computer.</p>
          {process && <p>Sharing is {process.share_running ? "running" : "stopped"}.</p>}
        </div>
      </div>
    );
  }
  return (
    <>
      <div className="settings-section-title">
        <div>
          <h2>Sharing</h2>
          <p>Offer a music folder to Soulseek through Nicotine+.</p>
        </div>
      </div>
      <div className="settings-card">
        <form
          className="settings-form"
          onSubmit={async (event: FormEvent<HTMLFormElement>) => {
            event.preventDefault();
            await onSave("Sharing setup saved");
          }}
        >
          <PathField
            name="share_folder"
            label="Folder to share"
            mode="folder"
            placeholder="An existing music folder"
            value={form.shareFolder}
            onValueChange={(value) =>
              setForm((previous) => ({ ...previous, shareFolder: value }))
            }
          />
          <PathField
            name="nicotine_binary"
            label="Nicotine+ app"
            mode="file"
            placeholder="Path to Nicotine+"
            value={form.nicotine}
            onValueChange={(value) =>
              setForm((previous) => ({ ...previous, nicotine: value }))
            }
          />
          <Field name="share_username">
            <FieldLabel>Sharing account username</FieldLabel>
            <Input
              autoComplete="username"
              value={form.shareUsername}
              onValueChange={(value) =>
                setForm((previous) => ({ ...previous, shareUsername: value }))
              }
            />
          </Field>
          <Field name="share_password">
            <FieldLabel>Sharing account password</FieldLabel>
            <Input
              type="password"
              autoComplete="new-password"
              placeholder="Leave blank to keep saved password"
              value={form.sharePassword}
              onValueChange={(value) =>
                setForm((previous) => ({ ...previous, sharePassword: value }))
              }
            />
          </Field>
          <div className="settings-actions">
            <Button className="primary-action" type="submit">
              Save sharing setup
            </Button>
          </div>
        </form>
        <div className="settings-actions">
          <button
            className="quiet-button"
            type="button"
            onClick={() => void store.runWorkerAction("share_start")}
          >
            Start sharing
          </button>
          <button
            className="quiet-button"
            type="button"
            onClick={() => void store.runWorkerAction("share_stop")}
          >
            Stop sharing
          </button>
        </div>
        <div className="settings-note">
          {process
            ? process.share_running
              ? "Nicotine+ sharing is running"
              : "Nicotine+ sharing is stopped"
            : ""}
        </div>
        <p className="settings-help">
          Use a separate account from downloads so the two clients do not
          disconnect each other.
        </p>
      </div>
    </>
  );
}
