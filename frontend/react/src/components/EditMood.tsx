import { useState } from "react";
import { postJson, type Mood } from "../api";
import { useStore } from "../store";

export function EditMood({ mood, close }: { mood: Mood; close: () => void }) {
  const store = useStore();
  const [tags, setTags] = useState(mood.genres);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const options = [...new Set([...mood.genres, ...store.genreOptions])].sort();
  const save = async () => {
    setSaving(true);
    try {
      await postJson("/api/moods", { mood: mood.id, genres: tags });
      await store.loadTracks(true);
      close();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save this mood.");
      setSaving(false);
    }
  };
  return <fieldset className="mood-editor" disabled={saving}>
    <legend>Genres for {mood.name}</legend>
    {options.map(tag => <label key={tag}>
      <input type="checkbox" checked={tags.includes(tag)} onChange={e =>
        setTags(e.target.checked ? [...tags, tag] : tags.filter(value => value !== tag))} /> {tag}
    </label>)}
    {error && <p role="alert">{error}</p>}
    <div className="settings-actions">
      <button className="quiet-button" type="button" onClick={close}>Cancel</button>
      <button className="primary-action" type="button" onClick={() => void save()}>Save for room</button>
    </div>
  </fieldset>;
}
