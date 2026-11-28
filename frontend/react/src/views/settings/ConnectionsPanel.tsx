import { useEffect, useState } from "react";
import { Field, FieldLabel } from "@ui-sdk/react/field";
import { Input } from "@ui-sdk/react/input";
import { RoomAction } from "../../components/RoomControls";
import { RoomIcon } from "../../components/RoomIcon";
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

  const connected = !!spotify?.connected;
  const running = !!spotify?.running;
  const lastSync = spotify?.last_success ? new Date(spotify.last_success * 1000) : null;
  const syncMinutes = Math.round((spotify?.interval_seconds || 900) / 60);

  return (
    <>
      <div className="settings-section-title">
        <div>
          <h2>Spotify</h2>
          <p>Sync liked songs and playlists you own or collaborate on.</p>
        </div>
      </div>
      <section className="settings-card" data-settings-lists="account" aria-labelledby="spotify-account-heading">
        <div data-settings-list-head>
          <div>
            <h3 id="spotify-account-heading">Account connection</h3>
            <p>Listening checks Spotify every {syncMinutes} minutes.</p>
          </div>
          <span
            className="status-pill"
            data-spotify-status={running ? "syncing" : connected ? "connected" : undefined}
            role="status"
          >
            {running ? "Syncing" : connected ? "Connected" : spotify ? "Disconnected" : "Checking connection…"}
          </span>
        </div>

        {connected && spotify ? (
          <dl data-spotify-stats>
            <div>
              <dt>Spotify account</dt>
              <dd>{spotify.account || "Connected account"}</dd>
            </div>
            <div>
              <dt>Liked songs</dt>
              <dd>{readable(spotify.saved_total)}</dd>
            </div>
            <div>
              <dt>Playlists</dt>
              <dd>{readable(spotify.playlist_count)}</dd>
            </div>
            <div>
              <dt>Last synced</dt>
              <dd>{lastSync ? <time dateTime={lastSync.toISOString()}>{lastSync.toLocaleString(undefined, {
                dateStyle: "medium", timeStyle: "short",
              })}</time> : "Waiting for first sync"}</dd>
            </div>
          </dl>
        ) : spotify && (
          <p data-settings-list-message>
            {!host ? "Connect on the server computer." : "Enter your Spotify Client ID below to connect."}
          </p>
        )}
        {running && spotify?.stage && <p data-settings-list-message role="status">{spotify.stage}</p>}
        {spotify?.last_error && <p data-settings-list-message role="alert">{spotify.last_error}</p>}

        <div data-settings-list-actions data-spotify-account-actions>
          {connected ? (
            <>
              <RoomAction primary icon="refresh" disabled={!host || running} onClick={syncNow}>
                {running ? "Syncing…" : "Sync now"}
              </RoomAction>
              <RoomAction disabled={!host} onClick={connect}>Reconnect</RoomAction>
              <RoomAction data-spotify-disconnect disabled={!host} onClick={disconnect}>Disconnect</RoomAction>
            </>
          ) : (
            <RoomAction primary disabled={!host || !spotify} onClick={connect}>Connect Spotify</RoomAction>
          )}
        </div>

        <details
          data-settings-list-disclosure="developer"
          open={devOpen}
          onToggle={(event) => setDevOpen(event.currentTarget.open)}
        >
          <summary><span>Developer app setup</span><RoomIcon name="chevronDown" /></summary>
          <div data-settings-list-content>
            <p>
              Create a Web API app in the{" "}
              <a
                href="https://developer.spotify.com/dashboard"
                target="_blank"
                rel="noopener noreferrer"
              >
                Spotify Developer Dashboard
              </a>{" "}
              and register the redirect URI.
            </p>
            <dl data-spotify-redirect>
              <dt>Redirect URI</dt>
              <dd><code className="spotify-redirect">{settings?.spotify_redirect_uri ?? ""}</code></dd>
            </dl>
            <Field name="spotify_client_id" data-settings-list-field>
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
            <p>Your tokens stay on this computer. Development apps require an eligible account and allowlisted users.</p>
          </div>
        </details>
      </section>
      {host && spotify?.connected && <SpotifyPlaylists syncTime={spotify.last_success} running={running} />}
    </>
  );
}
