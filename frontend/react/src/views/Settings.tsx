import { ExtensionsContent } from "./Extensions";
import { useAppearance } from "../appearance";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import "./settings/room-settings.css";
import "./settings/settings-lists.css";
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
import { RoomIcon, type RoomIconName } from "../components/RoomIcon";
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

const wideSettings = "(min-width: 900px)";

function subscribeToSettingsWidth(notify: () => void) {
  const query = matchMedia(wideSettings);
  query.addEventListener("change", notify);
  return () => query.removeEventListener("change", notify);
}

type SettingsSection = { key: string; name: string; icon: RoomIconName; panel: ReactNode };

function SettingsSections({ sections, active, onOpenTab }: {
  sections: SettingsSection[];
  active: string;
  onOpenTab: (tab: string) => void;
}) {
  const wide = useSyncExternalStore(subscribeToSettingsWidth, () => matchMedia(wideSettings).matches);
  const index = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const list = index.current;
    const selected = list?.querySelector<HTMLElement>('[aria-selected="true"]');
    if (!list || !selected) return;
    const frame = list.getBoundingClientRect();
    const item = selected.getBoundingClientRect();
    if (wide) {
      if (item.top < frame.top) list.scrollTop -= frame.top - item.top;
      if (item.bottom > frame.bottom) list.scrollTop += item.bottom - frame.bottom;
    } else {
      if (item.left < frame.left) list.scrollLeft -= frame.left - item.left;
      if (item.right > frame.right) list.scrollLeft += item.right - frame.right;
    }
  }, [active, wide]);

  return (
    <Tabs data-settings-layout value={active} orientation={wide ? "vertical" : "horizontal"}
      onValueChange={value => { if (value) onOpenTab(value); }}>
      <TabsList ref={index} className="settings-tabs" aria-label="Settings sections">
        {sections.map(({ key, name, icon }) => <TabsTab key={key} value={key}>
          <RoomIcon name={icon} /><span>{name}</span>
        </TabsTab>)}
      </TabsList>
      {sections.map(({ key, panel }) => (
        <TabsPanel key={key} value={key} className="settings-panel"
          keepMounted={key !== "storage" && key !== "clients" && key !== "extensions"}>
          {panel}
        </TabsPanel>
      ))}
    </Tabs>
  );
}

export function Settings() {
  const store = useStore();
  if (store.member) {
    return <div className="view" data-view="settings">
      <header className="listening-view-head"><h1>Settings</h1></header>
      <div className="settings-panel"><AppearancePanel /></div>
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
  const sections: SettingsSection[] = [
    ...(host ? [
      { key: "setup", icon: "settings", name: "Room setup", panel: <SetupPanel settings={settings} onOpenTab={openTab} /> },
      { key: "sources", icon: "library", name: "Sources", panel: <SourcesPanel settings={settings}
        discovered={discovered} discoveryDone={discoveryDone} scanError={scanError}
        scanStatus={scanStatus} discoverLocal={discoverLocal} /> },
      { key: "downloads", icon: "download", name: "Downloads", panel: <DownloadsPanel settings={settings} form={form}
        setForm={setForm} onSave={onSave} onOpenExtensions={() => openTab("extensions")} /> },
      { key: "spotify", icon: "music", name: "Spotify", panel: <ConnectionsPanel settings={settings} form={form} setForm={setForm} /> },
      { key: "storage", icon: "jobs", name: "Storage", panel: <StoragePanel settings={settings} form={form} setForm={setForm} onSave={onSave} /> },
    ] satisfies SettingsSection[] : []),
    ...(appearance ? [{ key: "appearance", icon: "moods", name: "Appearance", panel: <AppearancePanel /> }] satisfies SettingsSection[] : []),
    { key: "extensions", icon: "extensions", name: "Extensions", panel: <>
      <header className="settings-section-title"><div><h2>Extensions</h2><p>Manage download sources and radio providers.</p></div></header>
      <ExtensionsContent />
    </> },
    { key: "playback", icon: "listen", name: "Playback", panel: <PlaybackPanel /> },
    { key: "radio", icon: "radio", name: "Radio", panel: <RadioPanel /> },
    { key: "devices", icon: "outputs", name: "Devices", panel: <DevicesPanel settings={settings} /> },
    { key: "clients", icon: "devices", name: "Connect a device", panel: <ClientInstallers onPair={() => openTab("devices")} /> },
  ];
  const active = sections.some(section => section.key === store.settingsTab)
    ? store.settingsTab
    : "playback";

  return (
    <div className="view" data-view="settings">
      <header className="listening-view-head"><h1>Settings</h1></header>
      <SettingsSections sections={sections} active={active} onOpenTab={openTab} />
    </div>
  );
}
