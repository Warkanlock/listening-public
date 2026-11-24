import { RoomIcon } from "../../components/RoomIcon";
import { useEffect, useState, type FormEvent } from "react";
import { Button } from "@ui-sdk/react/button";
import { Field, FieldLabel } from "@ui-sdk/react/field";
import { Input } from "@ui-sdk/react/input";
import { RoomAction } from "../../components/RoomControls";
import { isHost, type RadioStation } from "../../api";
import { useStore } from "../../store";
import { notify } from "../../toasts";
import { SomaFmPanel } from "./SomaFmPanel";

// YouTube mixes are edited in a local draft. The stations endpoint validates
// pasted video links and normalizes them to video IDs when saved.
export function RadioPanel() {
  const store = useStore();
  const [draft, setDraft] = useState<RadioStation[]>([]);
  const [seedInputs, setSeedInputs] = useState<Record<number, string>>({});
  const [newName, setNewName] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (store.stationsLoaded) {
      setDraft(
        store.stations.map((station) => ({
          name: station.name,
          seeds: [...station.seeds],
        })),
      );
    }
  }, [store.stations, store.stationsLoaded]);

  const dirty =
    store.stationsLoaded &&
    JSON.stringify(draft) !== JSON.stringify(store.stations);

  const patchStation = (
    index: number,
    patch: Partial<RadioStation>,
  ) =>
    setDraft((previous) =>
      previous.map((station, i) =>
        i === index ? { ...station, ...patch } : station,
      ),
    );

  const moveStation = (index: number, delta: number) =>
    setDraft((previous) => {
      const target = index + delta;
      if (target < 0 || target >= previous.length) return previous;
      const next = [...previous];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });

  const moveSeed = (index: number, seedIndex: number, delta: number) =>
    setDraft((previous) =>
      previous.map((station, i) => {
        if (i !== index) return station;
        const target = seedIndex + delta;
        if (target < 0 || target >= station.seeds.length) return station;
        const seeds = [...station.seeds];
        [seeds[seedIndex], seeds[target]] = [seeds[target], seeds[seedIndex]];
        return { ...station, seeds };
      }),
    );

  const onSave = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSaving(true);
    const ok = await store.saveStations(draft);
    setSaving(false);
    if (ok) notify("Stations saved");
  };

  const addStation = () => {
    const name = newName.trim();
    if (!name) return;
    setDraft((previous) => [...previous, { name, seeds: [] }]);
    setNewName("");
  };

  return (
    <>
      <div className="settings-section-title">
        <div>
          <h2>Radio</h2>
          <p>
            Choose live stations and manage saved YouTube mixes.
          </p>
        </div>
      </div>
      {isHost(store.settings) && store.settingsTab === "radio" && <SomaFmPanel />}
      <section className="settings-card" aria-labelledby="youtube-mixes-settings-heading">
        <h3 id="youtube-mixes-settings-heading">YouTube mixes</h3>
        {!store.stationsLoaded ? (
        <div className="settings-card">
          <p className="settings-note">
            {store.stationsError ?? "Loading stations…"}
          </p>
          {store.stationsError ? (
            <div className="settings-actions">
              <Button className="quiet-button" type="button" onClick={() => void store.loadStations()}>
                Try again
              </Button>
            </div>
          ) : null}
        </div>
      ) : !store.radioSlice.youtubeEnabled ? (
        <p className="settings-note">Enable YouTube in Extensions to create video mixes.</p>
      ) : (
        <form className="settings-form" onSubmit={onSave}>
          {draft.map((station, index) => (
            <div className="settings-card radio-station-card" key={index}>
              <Field name={`station_name_${index}`}>
                <FieldLabel>Station name</FieldLabel>
                <Input
                  value={station.name}
                  onValueChange={(value) =>
                    patchStation(index, { name: value })
                  }
                />
              </Field>
              <details className="radio-saved-videos">
                <summary>Saved videos ({station.seeds.length})</summary>
                <div className="radio-seed-list">
                  {station.seeds.length === 0 ? (
                    <p className="settings-help">
                      Add a video to make this saved mix available in Radio.
                    </p>
                  ) : (
                    station.seeds.map((seed, seedIndex) => (
                      <div className="radio-seed-row" key={seedIndex}>
                        <a
                          href={`https://www.youtube.com/watch?v=${seed}`}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {seed}
                        </a>
                        <span>
                          <button
                            className="quiet-button"
                            type="button"
                            aria-label={`Move seed ${seed} up in ${station.name}`}
                            disabled={seedIndex === 0}
                            onClick={() => moveSeed(index, seedIndex, -1)}
                          >
                            <RoomIcon name="up" />
                          </button>
                          <button
                            className="quiet-button"
                            type="button"
                            aria-label={`Move seed ${seed} down in ${station.name}`}
                            disabled={seedIndex === station.seeds.length - 1}
                            onClick={() => moveSeed(index, seedIndex, 1)}
                          >
                            <RoomIcon name="down" />
                          </button>
                          <button
                            className="quiet-button"
                            type="button"
                            aria-label={`Remove seed ${seed} from ${station.name}`}
                            onClick={() =>
                              patchStation(index, {
                                seeds: station.seeds.filter(
                                  (_, i) => i !== seedIndex,
                                ),
                              })
                            }
                          >
                            Remove
                          </button>
                        </span>
                      </div>
                    ))
                  )}
                </div>
                <div className="radio-seed-form">
                  <Field name={`station_seed_${index}`}>
                    <FieldLabel>YouTube video link</FieldLabel>
                    <Input
                      placeholder="Paste a YouTube link"
                      value={seedInputs[index] ?? ""}
                      onValueChange={(value) =>
                        setSeedInputs((previous) => ({
                          ...previous,
                          [index]: value,
                        }))
                      }
                    />
                  </Field>
                  <RoomAction
                    icon="add"
                    disabled={!(seedInputs[index] ?? "").trim()}
                    onClick={() => {
                      const value = (seedInputs[index] ?? "").trim();
                      if (!value) return;
                      patchStation(index, {
                        seeds: [...station.seeds, value],
                      });
                      setSeedInputs((previous) => ({
                        ...previous,
                        [index]: "",
                      }));
                    }}
                  >
                    Add video
                  </RoomAction>
                </div>
              </details>
              <div className="settings-actions">
                <button
                  className="quiet-button"
                  type="button"
                  disabled={index === 0}
                  onClick={() => moveStation(index, -1)}
                >
                  Move up
                </button>
                <button
                  className="quiet-button"
                  type="button"
                  disabled={index === draft.length - 1}
                  onClick={() => moveStation(index, 1)}
                >
                  Move down
                </button>
                <button
                  className="quiet-button"
                  type="button"
                  onClick={() =>
                    setDraft((previous) =>
                      previous.filter((_, i) => i !== index),
                    )
                  }
                >
                  Delete station
                </button>
              </div>
            </div>
          ))}
          <div className="radio-seed-form radio-new-mix">
            <Field name="new_station_name">
              <FieldLabel>New mix</FieldLabel>
              <Input
                placeholder="Mix name"
                value={newName}
                onValueChange={setNewName}
              />
            </Field>
            <div className="radio-mix-actions">
              <RoomAction
                icon="add"
                aria-label="Add mix"
                title="Add mix"
                disabled={!newName.trim() || saving}
                onClick={addStation}
              >
                <span>Add mix</span>
              </RoomAction>
              <RoomAction
                primary
                icon="check"
                type="submit"
                aria-label="Save mixes"
                title="Save mixes"
                disabled={!dirty || saving}
              >
                <span>Save mixes</span>
              </RoomAction>
            </div>
          </div>
        </form>
        )}
      </section>
    </>
  );
}
