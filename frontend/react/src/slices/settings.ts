import {
  api,
  postJson,
  type DevicesResult,
  type ExtensionSources,
  type Settings,
  type SettingsBody,
  type SpotifyStatus,
} from "../api";
import { macRoomClient } from "../mediaOutput";
import { notify } from "../toasts";
import { deviceId } from "../store";
import { errorMessage, type SliceHost } from "./host";

const defaultDeviceName = () =>
  localStorage.getItem("listening-device-name") ||
  (macRoomClient ? "Listening on Mac" : navigator.userAgent.includes("Mobile") ? "Mobile browser" : "Desktop browser");

// Settings (plan 056): the room's settings and their load/save,
// the Spotify connection status, the devices list and this
// device's presence heartbeat, the sources actions, and the
// Settings tab state.
export class SettingsSlice {
  spotifyStatus: SpotifyStatus | null = null;
  devicesResult: DevicesResult | null = null;
  settings: Settings | null = null;
  settingsError: string | null = null;
  settingsTab = "setup";
  extensionSources: ExtensionSources | null = null;
  sourceError = "";
  private sourceCheckedAt = 0;
  private sourceRequest?: Promise<void>;

  async loadExtensionSources(refresh = false): Promise<void> {
    if (this.sourceRequest) {
      await this.sourceRequest;
      if (!refresh) return;
    }
    if (!refresh && Date.now() - this.sourceCheckedAt < 30_000) return;
    this.sourceRequest = api<ExtensionSources>("/api/extensions/sources", { cache: "no-store" })
      .then(data => { this.extensionSources = data; this.sourceError = ""; })
      .catch(() => { this.extensionSources = null; this.sourceError = "Couldn’t check the room’s download sources."; })
      .finally(() => {
        this.sourceCheckedAt = Date.now();
        this.sourceRequest = undefined;
        this.host.bump();
      });
    return this.sourceRequest;
  }

  deviceName = defaultDeviceName();

  constructor(private readonly host: SliceHost) {}

  registerDevice(signal?: AbortSignal) {
    return api("/api/device", {
      method: "POST",
      headers: { ...this.host.playbackOutputHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({ id: deviceId, name: this.deviceName }),
      signal,
    });
  }

  leaveDevice() {
    return api("/api/device", {
      method: "POST",
      headers: { ...this.host.playbackOutputHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({ id: deviceId, op: "leave" }),
      keepalive: true,
    });
  }

  async heartbeat() {
    if (!this.host.playbackPresenceActive) return;
    try {
      await this.registerDevice();
    } catch {
      // Retry when the room reconnects.
    }
  }

  async refreshDevices() {
    try {
      this.devicesResult = await api<DevicesResult>("/api/devices");
      this.host.bump();
    } catch {
      // The Devices panel renders its own error state.
    }
  }

  async refreshSpotifyStatus() {
    if (this.host.view !== "settings") return;
    try {
      this.spotifyStatus = await api<SpotifyStatus>("/api/spotify/status");
      this.host.bump();
    } catch {
      // The Connections panel (Stage 4) renders its own error state.
    }
  }

  setSpotifyStatus(status: SpotifyStatus) {
    this.spotifyStatus = status;
    this.host.bump();
  }

  async loadSettings() {
    try {
      this.settings = await api<Settings>("/api/settings");
      this.settingsError = null;
    } catch (error) {
      this.settingsError = errorMessage(error, "Settings could not load");
      notify(this.settingsError);
    }
    this.host.bump();
  }

  async saveSettings(body: SettingsBody): Promise<boolean> {
    try {
      this.settings = await postJson<Settings>("/api/settings", body);
      this.settingsError = null;
      this.host.bump();
      void this.host.refreshOverview();
      void this.host.refreshActivity();
      void this.host.pollSession();
      void this.host.loadTracks(true);
      return true;
    } catch (error) {
      notify(errorMessage(error, "Settings could not be saved"));
      return false;
    }
  }

  async changeSource(
    op: "watch" | "unwatch" | "link" | "unlink",
    path: string,
  ): Promise<boolean> {
    try {
      const result = await postJson<{ imported: number; errors: unknown[] }>(
        "/api/sources",
        { op, path },
      );
      await Promise.all([this.loadSettings(), this.host.refreshOverview()]);
      void this.host.loadTracks(true);
      if (result.errors?.length) {
        notify(
          `${result.imported} lists added; ${result.errors.length} files could not be read`,
        );
      } else {
        notify(
          op === "watch"
            ? `${result.imported} lists added; folder is now watched`
            : op === "link"
              ? "Playlist imported and linked; changes will sync automatically"
              : "Source no longer linked; imported lists remain",
        );
      }
      return true;
    } catch (error) {
      notify(errorMessage(error, "Source update failed"));
      return false;
    }
  }

  async syncSources() {
    try {
      const result = await postJson<{ status: string }>("/api/sync", {});
      notify(result.status);
      void this.host.refreshOverview();
      void this.host.loadTracks(true);
    } catch (error) {
      notify(errorMessage(error, "Sync failed"));
    }
  }

  setSettingsTab(tab: string) {
    this.settingsTab = tab;
    this.host.bump();
  }

  saveDeviceName(name: string) {
    this.deviceName = name;
    localStorage.setItem("listening-device-name", name);
    void this.heartbeat();
    notify("Device name saved");
  }
}
