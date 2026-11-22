import { RoomAction, RoomConfirm, RoomSelect } from "../../components/RoomControls";
import { PlaybackDevices } from "../../components/PlaybackDevices";
import { useRef, useState, type FormEvent } from "react";
import { Button } from "@ui-sdk/react/button";
import { Field, FieldLabel } from "@ui-sdk/react/field";
import { Input } from "@ui-sdk/react/input";
import { Textarea } from "@ui-sdk/react/textarea";
import { isHost, postJson, type Settings as SettingsData } from "../../api";
import { deviceId, useStore } from "../../store";
import { notify } from "../../toasts";
import { StateCard } from "../../components/StateCard";
import { errorText } from "./shared";

const EVERYONE = "__everyone__";

export function DevicesPanel({ settings }: { settings: SettingsData | null }) {
  const store = useStore();
  const host = isHost(settings);
  const [name, setName] = useState(store.deviceName);
  const [recipient, setRecipient] = useState("");
  const [message, setMessage] = useState("");
  const [announceStatus, setAnnounceStatus] = useState("");
  const [confirmation, setConfirmation] = useState<"rotate" | { id: string; name: string } | null>(null);
  const [sending, setSending] = useState(false);
  const result = store.devicesResult;
  const devices = result?.devices ?? [];
  const paired = result?.paired_devices ?? [];
  const effectiveRecipient =
    recipient && devices.some((item) => item.id === recipient) ? recipient : "";

  const linkText = useRef<HTMLDivElement>(null);

  const copyLink = async () => {
    try {
      if (navigator.clipboard) {
        await navigator.clipboard.writeText(settings?.lan_url ?? "");
      } else {
        const selection = window.getSelection();
        if (!selection || !linkText.current) return;
        const range = document.createRange();
        range.selectNodeContents(linkText.current);
        selection.removeAllRanges();
        selection.addRange(range);
        if (!document.execCommand("copy")) {
          notify("Use Copy on the selected pairing link");
          return;
        }
      }
      notify("Paired link copied");
    } catch {
      notify("Select and copy the link above");
    }
  };

  const rotate = async () => {
    try {
      await postJson("/api/pairing/rotate", {});
      await store.loadSettings();
      notify("Pairing link rotated");
    } catch (error) {
      notify(errorText(error, "Rotation failed"));
    }
  };

  const allowOffline = async (id: string, allowed: boolean) => {
    try {
      await postJson("/api/devices/offline", { id, allowed });
      await store.heartbeat();
      notify(allowed ? "Offline sync allowed" : "Offline sync paused");
    } catch (error) {
      notify(errorText(error, "Could not change offline sync"));
    }
  };

  const [changingAccess, setChangingAccess] = useState("");
  const allowAdditions = async (id: string, enabled: boolean) => {
    if (changingAccess) return;
    setChangingAccess(id);
    try {
      await postJson("/api/devices/playlist-access", { id, enabled });
      await store.heartbeat();
      notify(enabled ? "Playlist additions allowed" : "Playlist additions turned off");
    } catch (error) {
      notify(errorText(error, "Could not change playlist access"));
    } finally { setChangingAccess(""); }
  };

  const revoke = async (id: string, deviceName: string) => {
    try {
      await postJson("/api/devices/revoke", { id });
      notify(`${deviceName} revoked`);
      await store.heartbeat();
    } catch (error) {
      notify(errorText(error, "Revoke failed"));
    }
  };

  const announce = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (sending) return;
    setSending(true);
    try {
      await postJson("/api/announcements", {
        recipient: effectiveRecipient,
        message: message.trim(),
      });
      setMessage("");
      setAnnounceStatus(
        effectiveRecipient ? "Sent to device" : "Sent to everyone",
      );
      void store.pollAnnouncements();
    } catch (error) {
      setAnnounceStatus(errorText(error, "Announcement failed"));
    } finally {
      setSending(false);
    }
  };

  return (
    <>
      {confirmation && <RoomConfirm open
        title={confirmation === "rotate" ? "Rotate the pairing link?" : `Revoke ${confirmation.name}?`}
        description={confirmation === "rotate" ? "The old link stops working. Devices already paired stay paired." :
          "This browser will need the current pairing link to join again."}
        confirmLabel={confirmation === "rotate" ? "Rotate link" : "Revoke access"}
        onClose={() => setConfirmation(null)}
        onConfirm={() => void (confirmation === "rotate" ? rotate() : revoke(confirmation.id, confirmation.name))} />}
      <div className="settings-section-title">
        <div>
          <h2>Devices</h2>
        </div>
      </div>
      <div className="settings-stack">
        <div className="settings-card"><h3>Playback devices</h3><PlaybackDevices /></div>
        <div className="settings-card"><h3>Offline listening</h3>
          <p>Use an offline-capable player to save music on a paired device. Playback buffering is set separately on each device.</p>
        </div>
        {(host || settings?.can_invite) && (
          <div className="settings-card">
            <h3>Pair a device</h3>
            <div className="lan-link" ref={linkText}>
              {settings
                ? settings.lan_url || "No local network address found"
                : "Finding local address…"}
            </div>
            <div className="settings-actions">
              <button
                className="quiet-button"
                type="button"
                disabled={!settings?.lan_url}
                onClick={() => void copyLink()}
              >
                Copy pairing link
              </button>
              {host && (
                <button
                  className="quiet-button"
                  type="button"
                  onClick={() => setConfirmation("rotate")}
                >
                  Rotate pairing link
                </button>
              )}
            </div>
            <p className="settings-help">
              {settings?.pairing_network === "home"
                ? "Open this link on home Wi-Fi or through Tailscale while away."
                : settings?.pairing_network === "tailscale"
                ? "Private network · Tailscale. Connect the other device to the same tailnet, then open the link."
                : settings?.pairing_network === "wireguard"
                ? "Private network · WireGuard. Connect the other device to your WireGuard tunnel, then open the link."
                : settings?.lan_url
                  ? "Local network · Connect the other device to the same LAN, then open the link."
                  : "Start the server on a network address to pair another device."}
            </p>
          </div>
        )}
        <div className="settings-card">
          <h3>Connected devices</h3>
          <div className="settings-actions">
            <Field name="device_name" className="device-name-field">
              <Input
                aria-label="Name this device"
                placeholder="Name this device"
                maxLength={60}
                value={name}
                onValueChange={setName}
              />
            </Field>
            <button
              className="quiet-button"
              type="button"
              onClick={() =>
                store.saveDeviceName(name.trim().slice(0, 60) || store.deviceName)
              }
            >
              Save name
            </button>
          </div>
          <div className="device-list">
            {!result ? (
              <p role="status">Checking connected devices…</p>
            ) : devices.length ? (
              devices.map((item) => (
                <div className="device-item" key={item.id}>
                  <span className="live-dot" aria-hidden="true" />
                  <strong>
                    {item.name}
                    {item.id === deviceId ? " · this device" : ""}
                  </strong>
                  <small>
                    {item.address}
                    {item.host ? " · server" : ""}
                  </small>
                </div>
              ))
            ) : (
              <StateCard
                icon="devices"
                label="Devices"
                title="No devices connected"
                body="Devices appear here while Listening is in use."
                compact
              />
            )}
          </div>
        </div>
        {paired.length ? (
          <div className="settings-card">
            <h3>Paired devices</h3>
            <p className="settings-help">
              Choose which devices can sync an offline library or add songs to editable room playlists, including linked Spotify playlists.
            </p>
            <div className="device-list">
              {paired.map((item) => (
                <div className="device-item" key={item.id}>
                  <span
                    className={`live-dot${item.present ? "" : " off"}`}
                    aria-hidden="true"
                  />
                  <div>
                    <strong>
                      {item.name}
                      {item.id === deviceId ? " · this device" : ""}
                    </strong>
                    <small>
                      {item.present ? "Online" : "Offline"} · Paired{" "}
                      {new Date(item.paired_at * 1000).toLocaleDateString()}
                    </small>
                  </div>
                  {result?.manage && <div className="device-permissions">
                    <button className="quiet-button" type="button"
                      aria-pressed={Boolean(item.offline_sync)}
                      onClick={() => void allowOffline(item.id, !item.offline_sync)}>
                      {item.offline_sync ? "Pause offline sync" : "Allow offline sync"}
                    </button>
                    <RoomAction icon={item.playlist_access ? "check" : "playlists"} role="checkbox"
                    aria-checked={!!item.playlist_access} disabled={!!changingAccess}
                    onClick={() => void allowAdditions(item.id, !item.playlist_access)}>
                    Allow playlist additions
                    </RoomAction>
                    <button
                      className="quiet-button"
                      type="button"
                      onClick={() => setConfirmation({ id: item.id, name: item.name })}
                    >
                      Revoke
                    </button>
                  </div>}
                </div>
              ))}
            </div>
          </div>
        ) : null}
        {host ? (
          <div className="settings-card">
            <h3>Send an announcement</h3>
            <p>Send a message to one device or everyone in the room.</p>
            <form className="announcement-form" onSubmit={announce}>
              <RoomSelect label="Send to" value={effectiveRecipient || EVERYONE}
                options={[{ value: EVERYONE, label: "Everyone" }, ...devices.filter(item => item.id !== deviceId)
                  .map(item => ({ value: item.id, label: item.name }))]}
                onChange={value => setRecipient(value === EVERYONE ? "" : value)} />
              <Field name="announcement_message">
                <FieldLabel>Message</FieldLabel>
                <Textarea
                  maxLength={280}
                  rows={3}
                  required
                  placeholder="What should they know?"
                  value={message}
                  onValueChange={setMessage}
                />
              </Field>
              <div className="settings-actions">
                <Button
                  className="primary-action"
                  type="submit"
                  disabled={sending}
                >
                  Send announcement
                </Button>
                <span className="settings-note" role="status">
                  {announceStatus}
                </span>
              </div>
            </form>
          </div>
        ) : null}
      </div>
    </>
  );
}
