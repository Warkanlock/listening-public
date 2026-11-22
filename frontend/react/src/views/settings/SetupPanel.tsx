import { readable, type Settings as SettingsData } from "../../api";
import { useStore } from "../../store";
import { Updates } from "./Updates";

export function SetupPanel({ settings, onOpenTab }: {
  settings: SettingsData;
  onOpenTab: (tab: string) => void;
}) {
  const store = useStore();
  const files = settings.list_files.length;
  const missing = store.overview
    ? Math.max(0, store.overview.total - store.overview.summary.verified)
    : null;
  const tasks = [
    {
      title: "Add music",
      status: files ? `${readable(files)} playlist files` : "Import playlists or connect a music folder on the room server.",
      action: "Manage sources",
      open: () => onOpenTab("sources"),
    },
    {
      title: "Choose optional extensions",
      status: settings.download_ready ? "A download engine is ready." : "Add radio sources or a download engine when you need them.",
      action: "Manage extensions",
      open: () => onOpenTab("extensions"),
    },
    {
      title: "Check library progress",
      status: missing === null ? "Checking the library…" : `${readable(missing)} ${missing === 1 ? "song needs" : "songs need"} audio`,
      action: "View activity",
      open: () => store.setView("jobs"),
    },
    {
      title: "Connect a device",
      status: "Get the app for your device and find this room’s server address.",
      action: "Get client apps",
      open: () => onOpenTab("clients"),
    },
  ];
  return (
    <>
      <div className="settings-section-title"><div><h2>Room setup</h2><p>Set up your library, server downloads, and listening devices.</p></div></div>
      <div className="settings-card" data-layout="tasks">
        {tasks.map(task => (
          <div key={task.title}>
            <div><h3>{task.title}</h3><p>{task.status}</p></div>
            <button type="button" className="quiet-button" onClick={task.open}>{task.action}</button>
          </div>
        ))}
      </div>
      <Updates settings={settings} />
    </>
  );
}
