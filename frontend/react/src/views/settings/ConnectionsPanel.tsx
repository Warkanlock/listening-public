import { useEffect, useState } from "react";
import { Button } from "@ui-sdk/react/button";
import { Field, FieldLabel } from "@ui-sdk/react/field";
import { Input } from "@ui-sdk/react/input";
import {
  isHost,
  postJson,
  readable,
  type Settings as SettingsData,
  type SpotifyStatus,
} from "../../api";
import { useStore } from "../../store";
import { SpotifyPlaylists } from "./SpotifyPlaylists";
import { notify } from "../../toasts";
import { errorText, type FormState } from "./shared";

function spotifyStatusText(spotify: SpotifyStatus, host: boolean): string {
  if (!host) return "Connect on the server computer.";
  if (spotify.running)
    return `${spotify.stage || "Syncing Spotify…"} · ${readable(spotify.saved_total)} liked ${spotify.saved_total === 1 ? "song" : "songs"} last recorded`;
  if (spotify.connected) {
    const last = spotify.last_success
      ? `Last synced ${new Date(spotify.last_success * 1000).toLocaleString()}`
      : "Not synced yet";
    return `Connected as ${spotify.account || "Spotify account"} · ${last} · ${readable(spotify.saved_total)} liked ${spotify.saved_total === 1 ? "song" : "songs"} · ${readable(spotify.playlist_count)} ${spotify.playlist_count === 1 ? "playlist" : "playlists"}${spotify.last_error ? ` · ${spotify.last_error}` : ""}`;
  }
  return spotify.last_error || "Enter a Client ID, then connect your Spotify account.";
}

export function ConnectionsPanel({
  settings,
  form,
  setForm,
}: {
  settings: SettingsData | null;
  form: FormState;
  setForm: (updater: (previous: FormState) => FormState) => void;
}) {
  const store = useStore();
  const host = isHost(settings);
  const spotify = store.spotifyStatus ?? settings?.spotify ?? null;
  const [devOpen, setDevOpen] = useState(false);

  useEffect(() => {
    if (settings) setDevOpen(!settings.spotify.client_id);
  }, [settings]);

  const connect = async () => {
    try {
      const result = await postJson<{ url: string }>("/api/spotify/connect", {
        client_id: form.clientId.trim(),
      });
      location.assign(result.url);
    } catch (error) {
      notify(errorText(error, "Spotify connection failed"));
    }
  };

  const syncNow = async () => {
    try {
      const result = await postJson<{
        started: boolean;
        spotify: SpotifyStatus;
      }>("/api/spotify/sync", {});
      store.setSpotifyStatus(result.spotify);
      notify(
        result.started
          ? "Spotify sync started"
          : "Spotify sync is already running",
      );
    } catch (error) {
      notify(errorText(error, "Spotify sync failed"));
    }
  };

  const disconnect = async () => {
    try {
      const result = await postJson<{ spotify: SpotifyStatus }>(
        "/api/spotify/disconnect",
        {},
      );
      store.setSpotifyStatus(result.spotify);
      notify("Spotify disconnected. Local songs remain.");
    } catch (error) {
      notify(errorText(error, "Spotify disconnect failed"));
    }
  };

  return (
    <>
      <div className="settings-section-title">
        <div>
          <h2>Spotify</h2>
          <p>
            Sync liked songs and playlists you own or collaborate on.
          </p>
        </div>

      </div>
      <div className="settings-card spotify-card">
        <div className="spotify-card-heading">
          <div>
            <h3>Account connection</h3>
            <p>Listening Room checks Spotify every 15 minutes.</p>
          </div>
        </div>
        <div className="settings-note" role="status" aria-live="polite">
          {spotify ? spotifyStatusText(spotify, host) : "Checking connection…"}
        </div>
        <div className="settings-actions">
          <Button
            className="primary-action"
            type="button"
            disabled={!host}
            onClick={() => void connect()}
          >
            {spotify?.connected ? "Reconnect" : "Connect Spotify"}
          </Button>
          <button
            className="quiet-button"
            type="button"
            disabled={!host || !spotify?.connected || !!spotify?.running}
            onClick={() => void syncNow()}
          >
            Sync now
          </button>
          <button
            className="quiet-button"
            type="button"
            disabled={!host || !spotify?.connected}
            onClick={() => void disconnect()}
          >
            Disconnect
          </button>
        </div>
        <details
          className="settings-details"
          open={devOpen}
          onToggle={(event) => setDevOpen(event.currentTarget.open)}
        >
          <summary>Developer app setup</summary>
          <Field name="spotify_client_id" className="spotify-client-label">
            <FieldLabel>Spotify Client ID</FieldLabel>
            <Input
              autoComplete="off"
              spellCheck={false}
              maxLength={32}
              placeholder="32-character Client ID"
              value={form.clientId}
              onValueChange={(value) =>
                setForm((previous) => ({ ...previous, clientId: value }))
              }
            />
          </Field>
          <p>
            Create a Web API app in the{" "}
            <a
              href="https://developer.spotify.com/dashboard"
              target="_blank"
              rel="noopener noreferrer"
            >
              Spotify Developer Dashboard
            </a>{" "}
            and register this redirect URI.
          </p>
          <code className="spotify-redirect">
            {settings?.spotify_redirect_uri ?? ""}
          </code>
          <p>
            Your tokens stay on this computer. Development apps require an
            eligible account and allowlisted users.
          </p>
        </details>
      </div>
      {host && spotify?.connected && <SpotifyPlaylists syncTime={spotify.last_success} running={!!spotify.running} />}
    </>
  );
}
