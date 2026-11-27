import { useState } from "react";
import { Button } from "@ui-sdk/react/button";
import { api, postJson, type Settings as SettingsData, type UpdateCheck } from "../../api";
import { errorText } from "./shared";

export function Updates({ settings }: { settings: SettingsData }) {
  const [updateStatus, setUpdateStatus] = useState("");
  const [applyRemote, setApplyRemote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const installKind = settings.install_kind ?? "checkout";
  const managed = installKind === "docker" && settings.managed_updates;
  const short = (revision: string) => revision.slice(0, 7);
  const status = updateStatus || (settings.version ? `Installed revision ${short(settings.version)}` : "");

  const waitForUpdate = async (target: string) => {
    for (let attempt = 0; attempt < (managed ? 300 : 90); attempt++) {
      await new Promise((resolve) => setTimeout(resolve, managed ? 4000 : 2000));
      try {
        if (managed) {
          const next = await api<UpdateCheck>("/api/update?status=1");
          if (next.current === target && !next.applying) {
            location.reload();
            return;
          }
          if (next.update_error) {
            setUpdateStatus(next.update_error);
            return;
          }
          if (!next.applying) {
            setUpdateStatus("The update stopped before it finished. Check for updates again.");
            return;
          }
        } else if ((await api<SettingsData>("/api/settings")).version === target) {
          location.reload();
          return;
        }
      } catch {
        // Requests can fail while Compose replaces the running container.
      }
    }
    setUpdateStatus("The update is taking longer than expected. Check for updates again to see its status.");
  };

  const checkUpdates = async () => {
    setApplyRemote(null);
    setBusy(true);
    setUpdateStatus("Checking for updates…");
    try {
      const result = await api<UpdateCheck>("/api/update");
      if (result.applying) {
        setUpdateStatus(`Updating to ${short(result.applying)}. The room will restart after the build.`);
        await waitForUpdate(result.applying);
      } else if (result.behind > 0) {
        const available = `Update available to ${short(result.remote)}.`;
        setUpdateStatus(result.update_error ? `${result.update_error} ${available}` : available);
        setApplyRemote(result.remote);
      } else {
        setUpdateStatus(result.update_error || `You are on the latest revision (${short(result.current)}).`);
      }
    } catch (error) {
      setUpdateStatus(errorText(error, "Update check failed"));
    } finally {
      setBusy(false);
    }
  };

  const applyUpdate = async (revision: string) => {
    setApplyRemote(null);
    setBusy(true);
    setUpdateStatus(`Starting the update to ${short(revision)}…`);
    try {
      const result = await postJson<{ status: string; updating_to?: string }>(
        "/api/process",
        { action: "update", revision },
      );
      setUpdateStatus(result.status);
      if (result.updating_to) await waitForUpdate(result.updating_to);
    } catch (error) {
      setUpdateStatus(errorText(error, "Update failed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="settings-card">
      <h3>App updates</h3>
      {status && <div className="settings-note" role="status">{status}</div>}
      {settings?.frontend_rebuild_needed ? (
        <p className="settings-note">
          Rebuild the web player with <code>listening setup --client web --non-interactive</code> on the server, then reload this page.
        </p>
      ) : null}
      {installKind === "app" && <p>Quit Listening, then install the app from the new DMG.</p>}
      {installKind === "brew" && <p>On the server, run <code>brew upgrade listening-room</code>.</p>}
      {installKind === "docker" && !managed && (
        <p>For a source-built room, enable the Compose host updater to install updates here. For a registry image, run <code>docker compose pull</code> followed by <code>docker compose up -d</code>.</p>
      )}
      {(installKind === "checkout" || managed) && <div className="settings-actions">
        <button
          className="quiet-button"
          type="button"
          disabled={busy}
          onClick={() => void checkUpdates()}
        >
          Check for updates
        </button>
        {applyRemote ? (
          <Button
            className="primary-action"
            type="button"
            onClick={() => void applyUpdate(applyRemote)}
          >
            {`Update to ${short(applyRemote)}`}
          </Button>
        ) : null}
      </div>}
    </div>
  );
}
