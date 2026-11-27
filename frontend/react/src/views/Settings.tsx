import { ExtensionsContent } from "./Extensions";
import { useAppearance } from "../appearance";
import { useCallback, useEffect, useState } from "react";
import "./settings/room-settings.css";
import { Tabs, TabsList, TabsPanel, TabsTab } from "@ui-sdk/react/tabs";
import {
  api,
  derivedStoragePaths,
  isHost,
  scrollBehavior,
  type DiscoverLocal,
  type SettingsBody,
} from "../api";
import { useStore } from "../store";
import { notify } from "../toasts";
import { EMPTY_FORM, errorText, type FormState } from "./settings/shared";
import { ClientInstallers } from "./settings/ClientInstallers";
import { AppearancePanel } from "./settings/AppearancePanel";
import { ConnectionsPanel } from "./settings/ConnectionsPanel";
import { DevicesPanel } from "./settings/DevicesPanel";
import { DownloadsPanel } from "./settings/DownloadsPanel";
import { PlaybackPanel } from "./settings/PlaybackPanel";
import { RadioPanel } from "./settings/RadioPanel";
import { SetupPanel } from "./settings/SetupPanel";
import { SourcesPanel } from "./settings/SourcesPanel";
import { StoragePanel } from "./settings/StoragePanel";

export function Settings() {
  const store = useStore();
  if (store.member) {
    return <div className="view" data-view="settings">
      <header className="listening-view-head"><h1>Settings</h1></header>
      <AppearancePanel />
    </div>;
  }
  return <RoomSettings />;
}

function RoomSettings() {
  const store = useStore();
  const appearance = useAppearance();
  const settings = store.settings;
  const host = isHost(settings);
  const [form, setFormState] = useState<FormState>(EMPTY_FORM);
  const setForm = useCallback(
    (updater: (previous: FormState) => FormState) =>
      setFormState((previous) => updater(previous)),
    [],
  );
  const [discovered, setDiscovered] = useState<DiscoverLocal["folders"]>([]);
  const [discoveryDone, setDiscoveryDone] = useState(false);
  const [scanError, setScanError] = useState("");
  const [scanStatus, setScanStatus] = useState("Searching nearby folders…");

  useEffect(() => {
    if (!settings) return;
    setFormState((previous) => ({
      ...previous,
      folder: settings.folder,
      autoDownload: settings.auto_download,
      bulkDownloadFormat: settings.bulk_download_format ?? "mp3_preferred",
      audioStore: settings.audio_store || "database",
      clientId: settings.spotify.client_id || "",
    }));
  }, [settings]);

  const discoverLocal = useCallback(async () => {
    if (!host) return;
    setDiscoveryDone(false);
    setScanError("");
    setScanStatus("Searching common music folders…");
    try {
      const result = await api<DiscoverLocal>("/api/discover-local");
      setDiscovered(result.folders);
      setDiscoveryDone(true);
      setScanStatus(
        `${result.folders.length} nearby folder${result.folders.length === 1 ? "" : "s"} found`,
      );
    } catch (error) {
      setScanError(errorText(error, "Scan unavailable"));
      setDiscoveryDone(true);
      setScanStatus("Scan unavailable");
    }
  }, [host]);

  useEffect(() => {
    if (host && store.settingsTab === "sources" && !discoveryDone) {
      void discoverLocal();
    }
  }, [host, store.settingsTab, discoveryDone, discoverLocal]);

  useEffect(() => {
    if (matchMedia("(max-width: 650px)").matches) {
      window.scrollTo({ top: 0, behavior: scrollBehavior() });
    } else {
      document
        .querySelector(".main-area")
        ?.scrollTo({ top: 0, behavior: scrollBehavior() });
    }
  }, [store.settingsTab]);

  const buildBody = useCallback((): SettingsBody => {
    const folder = form.folder.trim();
    const derived = derivedStoragePaths(settings, folder);
    return {
      folder,
      lists_folder: derived.lists_folder,
      music_folder: derived.music_folder,
      playlist_output: derived.playlist_output,
      auto_download: form.autoDownload,
      bulk_download_format: form.bulkDownloadFormat,
      audio_store: form.audioStore,
    };
  }, [form, settings]);

  const onSave = useCallback(
    async (message: string): Promise<boolean> => {
      const ok = await store.saveSettings(buildBody());
      if (ok) notify(message);
      return ok;
    },
    [store, buildBody],
  );

  const openTab = (tab: string) => {
    store.setSettingsTab(tab);
    if (tab === "devices") void store.heartbeat();
  };

  if (!settings) {
    return (
      <div className="view">
        <header className="listening-view-head"><h1>Settings</h1></header>
        <p role="status">{store.settingsError ?? "Loading room settings…"}</p>
        {store.settingsError && (
          <button className="quiet-button" type="button" onClick={() => void store.loadSettings()}>Try again</button>
        )}
      </div>
    );
  }
  const sections = [
    ...(host ? [
      { key: "setup", name: "Room setup", panel: <SetupPanel settings={settings} onOpenTab={openTab} /> },
      { key: "sources", name: "Sources", panel: <SourcesPanel settings={settings}
        discovered={discovered} discoveryDone={discoveryDone} scanError={scanError}
        scanStatus={scanStatus} discoverLocal={discoverLocal} /> },
      { key: "downloads", name: "Downloads", panel: <DownloadsPanel settings={settings} form={form}
        setForm={setForm} onSave={onSave} onOpenExtensions={() => openTab("extensions")} /> },
      { key: "spotify", name: "Spotify", panel: <ConnectionsPanel settings={settings} form={form} setForm={setForm} /> },
      { key: "storage", name: "Storage", panel: <StoragePanel settings={settings} form={form} setForm={setForm} onSave={onSave} /> },
    ] : []),
    ...(appearance ? [{ key: "appearance", name: "Appearance", panel: <AppearancePanel /> }] : []),
    { key: "extensions", name: "Extensions", panel: <ExtensionsContent /> },
    { key: "playback", name: "Playback", panel: <PlaybackPanel /> },
    { key: "radio", name: "Radio", panel: <RadioPanel /> },
    { key: "devices", name: "Devices", panel: <DevicesPanel settings={settings} /> },
    { key: "clients", name: "Connect a device", panel: <ClientInstallers onPair={() => openTab("devices")} /> },
  ];
  const active = sections.some(section => section.key === store.settingsTab)
    ? store.settingsTab
    : "playback";

  return (
    <div className="view" data-view="settings">
      <header className="listening-view-head"><h1>Settings</h1></header>
      <Tabs value={active} onValueChange={value => { if (value) openTab(value); }}>
        <TabsList className="settings-tabs" aria-label="Settings sections">
          {sections.map(({ key, name }) => <TabsTab key={key} value={key}>{name}</TabsTab>)}
        </TabsList>
        {sections.map(({ key, panel }) => (
          <TabsPanel key={key} value={key} className="settings-panel" keepMounted={key !== "storage" && key !== "clients" && key !== "extensions"}>
            {panel}
          </TabsPanel>
        ))}
      </Tabs>
    </div>
  );
}
