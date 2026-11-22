import { PlaybackDevices } from "../components/PlaybackDevices";
import { useStore } from "../store";

export function MyDevices({ owner }: { owner: boolean }) {
  const store = useStore();
  return <section className="view">
    <header className="listening-view-head">
      <h1>My devices</h1>
      {owner && <button className="quiet-button" type="button" onClick={() => {
        store.setSettingsTab("devices");
        store.setView("settings");
      }}>Manage room devices</button>}
    </header>
    <div className="settings-card"><h2>Playback devices</h2><PlaybackDevices /></div>
    <p>Offline-capable players can save music on your device.</p>
  </section>;
}
