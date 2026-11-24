import { useEffect, useState, type FormEvent } from "react";
import { Switch, SwitchThumb } from "@ui-sdk/react/switch";
import { api, postJson, type SomaFmSettings } from "../../api";
import { RoomAction, RoomSelect } from "../../components/RoomControls";
import { RoomIconButton } from "../../components/RoomIcon";
import { useStore } from "../../store";
import { notify } from "../../toasts";

type Selection = Pick<SomaFmSettings, "enabled" | "stations">;

export function SomaFmPanel() {
  const store = useStore();
  const [saved, setSaved] = useState<SomaFmSettings | null>(null);
  const [draft, setDraft] = useState<Selection | null>(null);
  const [addition, setAddition] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    api<SomaFmSettings>("/api/radio/somafm", { signal: controller.signal })
      .then(result => {
        setSaved(result);
        setDraft({ enabled: result.enabled, stations: result.stations });
      })
      .catch(failure => {
        if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "SomaFM settings could not load.");
      });
    return () => controller.abort();
  }, [attempt]);

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!saved || !draft || saving) return;
    setSaving(true);
    setError("");
    try {
      const result = await postJson<SomaFmSettings>("/api/radio/somafm", {
        ...(draft.enabled !== saved.enabled ? { enabled: draft.enabled } : {}),
        ...(JSON.stringify(draft.stations) !== JSON.stringify(saved.stations) ? { stations: draft.stations } : {}),
      });
      setSaved(result);
      setDraft({ enabled: result.enabled, stations: result.stations });
      await store.loadStations();
      notify("SomaFM settings saved.");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "SomaFM settings could not be saved.");
    } finally {
      setSaving(false);
    }
  };

  const heading = <h3 id="somafm-settings-heading">SomaFM</h3>;
  if (!saved || !draft) return <section className="settings-card" aria-labelledby="somafm-settings-heading">
      {heading}
      <p role="status">{error || "Loading SomaFM settings…"}</p>
      {error && <RoomAction onClick={() => { setError(""); setAttempt(previous => previous + 1); }}>Try again</RoomAction>}
  </section>;
  const channels = Object.entries(saved.channels).map(([value, label]) => ({ value, label }))
    .sort((a, b) => a.label.localeCompare(b.label));
  const additions = channels.filter(channel => !draft.stations.some(station =>
    station.channel === channel.value || station.name.toLowerCase() === channel.label.toLowerCase()));
  const channelToAdd = additions.find(option => option.value === addition);
  const dirty = draft.enabled !== saved.enabled || JSON.stringify(draft.stations) !== JSON.stringify(saved.stations);
  return <section className="settings-card" aria-labelledby="somafm-settings-heading">
    {heading}
    <form className="settings-form" onSubmit={event => void save(event)}>
        <div className="check-setting">
          <span>Enable SomaFM</span>
          <Switch aria-label="Enable SomaFM" checked={draft.enabled} disabled={saving}
            onCheckedChange={enabled => setDraft({ ...draft, enabled })}><SwitchThumb /></Switch>
        </div>
        <p className="settings-note">Choose which channels appear in this room’s Radio.</p>
        <div className="somafm-channel-row">
          <RoomSelect label="Channel to add" hideLabel value={channelToAdd?.value ?? ""} disabled={saving || !additions.length}
            options={[{ value: "", label: "Choose a channel to add" }, ...additions]} onChange={setAddition} />
          <RoomAction icon="add" disabled={!channelToAdd || saving} onClick={() => {
            if (!channelToAdd) return;
            setDraft({ ...draft, stations: [...draft.stations, { name: channelToAdd.label, channel: channelToAdd.value }] });
            setAddition("");
          }}>Add station</RoomAction>
        </div>
        {draft.stations.length ? <ul className="somafm-stations" aria-label="Configured SomaFM stations">
          {draft.stations.map((station, index) => <li key={station.name} className="somafm-channel-row">
            <strong>{station.name}</strong>
            <RoomSelect label={`Channel for ${station.name}`} value={station.channel} hideLabel
              options={channels} disabled={saving} onChange={channel => setDraft({ ...draft,
                stations: draft.stations.map((entry, i) => i === index ? { ...entry, channel } : entry),
              })} />
            <RoomIconButton icon="trash" label={`Remove ${station.name}`} disabled={saving}
              onClick={() => setDraft({ ...draft, stations: draft.stations.filter((_, i) => i !== index) })} />
          </li>)}
        </ul> : <p role="status">No SomaFM stations selected.</p>}
        {error && <p role="alert">{error}</p>}
        <div className="settings-actions">
          <button className="primary-action" type="submit" disabled={!dirty || saving}>
            {saving ? "Saving…" : "Save SomaFM settings"}
          </button>
        </div>
    </form>
  </section>;
}
