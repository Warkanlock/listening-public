import { useEffect, useState } from "react";
import { useStore } from "../store";

export function PlaybackDevices({ close }: { close?: () => void }) {
  const store = useStore();
  const here = store.sessionSlice.outputId;
  const outputs = store.session?.outputs ?? [];
  const [draft, setDraft] = useState<string[] | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => { void store.pollSession(); }, [store]);
  const current = outputs.filter(device => device.selected).map(device => device.id);
  const selected = (draft ?? current).filter(id => outputs.some(device => device.id === id));

  async function save(devices: string[]) {
    if (!devices.length || saving) return;
    setSaving(true);
    try {
      if (await store.sendSession("outputs", { devices })) {
        setDraft(null);
        close?.();
      }
    } finally {
      setSaving(false);
    }
  }

  const active = outputs.filter(device => device.selected);
  return <div className="playback-devices">
    <p>Choose where this queue should play. Open Listening on any device you want to include.</p>
    {store.session?.session_playing && active.length > 0 &&
      <p>Playing on {active.map(device => device.id === here ? "this device" : device.name).join(", ")}</p>}
    {outputs.length ? <>
      <fieldset disabled={saving}>
        <legend>Connected players</legend>
        {outputs.map(device => <label key={device.id}>
          <input type="checkbox" checked={selected.includes(device.id)} onChange={event => {
            setDraft(event.currentTarget.checked ? [...selected, device.id] : selected.filter(id => id !== device.id));
          }} />
          <span>{device.name}{device.id === here && <small>This device</small>}</span>
        </label>)}
      </fieldset>
      <div className="playback-device-actions">
        <button type="button" className="primary-action" disabled={selected.length === 0 || saving}
          onClick={() => void save(selected)}>Use selected devices</button>
        {outputs.some(device => device.id === here) &&
          <button type="button" className="quiet-button" disabled={saving}
            onClick={() => void save([here])}>Listen here only</button>}
      </div>
    </> : <p role="status">{store.sessionFailed ? "The room is unreachable."
      : store.session ? "This server does not report playback devices." : "Connecting this player…"}</p>}
  </div>;
}
