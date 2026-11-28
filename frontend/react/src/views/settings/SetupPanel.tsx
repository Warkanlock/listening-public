import { useState } from "react";
import { RoomIcon, type RoomIconName } from "../../components/RoomIcon";
import { readable, type Settings as SettingsData } from "../../api";
import { useStore } from "../../store";
import { ExtensionsPanel } from "./ExtensionsPanel";
import { Updates } from "./Updates";

type SetupDetail = "folder" | "extensions";

export function SetupPanel({ settings, onOpenTab }: {
  settings: SettingsData;
  onOpenTab: (tab: string) => void;
}) {
  const store = useStore();
  const [detail, setDetail] = useState<SetupDetail | null>(null);
  const files = settings.list_files.length;
  const missing = store.overview
    ? Math.max(0, store.overview.total - store.overview.summary.verified)
    : null;
  const tasks: { icon: RoomIconName; title: string; status: string; action: string; open: () => void }[] = [
    {
      icon: "devices",
      title: "Connect a device",
      status: "Find this room’s address, pairing instructions, and app installers.",
      action: "Get client apps",
      open: () => onOpenTab("clients"),
    },
    {
      icon: "playlists",
      title: "Import playlists",
      status: files ? `${readable(files)} playlist ${files === 1 ? "file" : "files"}` : "Import a playlist file or watch a folder for playlists.",
      action: "Manage sources",
      open: () => onOpenTab("sources"),
    },
    {
      icon: "insights",
      title: "Check library progress",
      status: missing === null ? "Checking the library…" : `${readable(missing)} ${missing === 1 ? "song needs" : "songs need"} audio`,
      action: "View activity",
      open: () => store.setView("jobs"),
    },
  ];

  function toggle(name: SetupDetail) {
    setDetail(current => current === name ? null : name);
  }

  return (
    <>
      <div className="settings-section-title" data-setup-title>
        <div>
          <h2>Room setup</h2>
          <p>Add music, choose extensions, or connect another device. You can also go straight to your library.</p>
        </div>
        <button type="button" className="quiet-button" onClick={() => store.continueToLibrary()}>Continue to library</button>
      </div>
      <div className="settings-card" data-layout="tasks">
        <div>
          <RoomIcon name="library" />
          <div><h3>Use music you own</h3><p>Import tagged audio from a folder on the room server.</p></div>
          <button type="button" className="quiet-button" aria-expanded={detail === "folder"}
            aria-controls="setup-music-folder" onClick={() => toggle("folder")}>Add a music folder</button>
        </div>
        <section id="setup-music-folder" data-setup-detail hidden={detail !== "folder"} aria-label="Music folder setup">
          <p>On the host, stop the room before changing its music folder. For a local install, run <code>listening setup --music PATH</code> with the folder path, then start the room again to import the audio.</p>
          <p>For Docker, use the guide to stop the service, mount the folder read-only at <code>/imports</code>, and run setup. Listening leaves your source files in place. Allow space for a verified copy of the imported audio.</p>
          <div className="extension-resources">
            <a href="https://github.com/Warkanlock/listening-public/blob/main/docs/private-install.md#add-your-music-and-claim-the-room"
              target="_blank" rel="noopener noreferrer">Music folder setup guide <RoomIcon name="northeast" /></a>
          </div>
        </section>
        <div>
          <RoomIcon name="extensions" />
          <div><h3>Optional extensions</h3><p>{settings.download_ready
            ? "Your download sources are ready. Browse plugins and radio providers."
            : "Choose compatible plugins for finding copies, or try a built-in radio provider."}</p></div>
          <button type="button" className="quiet-button" aria-expanded={detail === "extensions"}
            aria-controls="setup-extensions" onClick={() => toggle("extensions")}>Browse extensions</button>
        </div>
        <section id="setup-extensions" data-setup-detail hidden={detail !== "extensions"} aria-label="Extensions for this room">
          {detail === "extensions" && store.settingsTab === "setup" && <ExtensionsPanel />}
        </section>
        {tasks.map(task => (
          <div key={task.title}>
            <RoomIcon name={task.icon} />
            <div><h3>{task.title}</h3><p>{task.status}</p></div>
            <button type="button" className="quiet-button" onClick={task.open}>{task.action}</button>
          </div>
        ))}
      </div>
      <Updates settings={settings} />
    </>
  );
}
