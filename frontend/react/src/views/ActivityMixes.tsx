import { useEffect, useState } from "react";
import { api } from "../api";
import { RoomIconButton } from "../components/RoomIcon";
import { RoomAction } from "../components/RoomControls";
import { useStore } from "../store";

type Activity = { id: string; name: string; min_bpm: number; max_bpm: number; count: number };
type TempoIndex = { activities: Activity[]; indexed: number; total: number; preparing: boolean; analysis_available: boolean };
type LoadState = { kind: "loading" } | { kind: "ready"; index: TempoIndex } | { kind: "error" };

function isActivity(value: unknown): value is Activity {
  return typeof value === "object" && value !== null &&
    "id" in value && typeof value.id === "string" &&
    "name" in value && typeof value.name === "string" &&
    "min_bpm" in value && typeof value.min_bpm === "number" &&
    "max_bpm" in value && typeof value.max_bpm === "number" &&
    "count" in value && typeof value.count === "number";
}

function isTempoIndex(value: unknown): value is TempoIndex {
  return typeof value === "object" && value !== null &&
    "activities" in value && Array.isArray(value.activities) && value.activities.every(isActivity) &&
    "indexed" in value && typeof value.indexed === "number" &&
    "total" in value && typeof value.total === "number" &&
    "preparing" in value && typeof value.preparing === "boolean" &&
    "analysis_available" in value && typeof value.analysis_available === "boolean";
}

export function ActivityMixes() {
  const store = useStore();
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [starting, setStarting] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function update() {
      try {
        const index = await api<unknown>("/api/activities", { signal });
        if (signal.aborted) return;
        if (!isTempoIndex(index)) throw new Error("Invalid activity index");
        setState({ kind: "ready", index });
        if (index.preparing || index.analysis_available && index.indexed < index.total) timer = setTimeout(update, 7000);
      } catch {
        if (!signal.aborted) setState({ kind: "error" });
      }
    }
    void update();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [attempt]);

  async function start(activity: Activity) {
    if (starting) return;
    setStarting(activity.id);
    try {
      await store.sessionSlice.sendSession("play_activity", { activity: activity.id });
    } finally {
      setStarting(null);
    }
  }

  const index = state.kind === "ready" ? state.index : null;
  const status = index && (index.preparing ? "Loading saved tempos…"
    : !index.total ? "Add music to your library to start a mix."
    : !index.analysis_available ? "Tempo analysis is unavailable on this server."
    : index.indexed < index.total ? `Checking tempos · ${index.indexed} of ${index.total} songs` : "");
  return <section className="view" aria-label="Activity mixes">
    <header className="listening-view-head">
      <div><h1>Activities</h1><p>Play a mix from your library by tempo.</p></div>
      <RoomAction icon="moods" onClick={() => store.setView("moods")}>Mood map</RoomAction>
    </header>
    {state.kind === "loading" && <p role="status">Loading activities…</p>}
    {state.kind === "error" && <div className="settings-actions">
      <p role="status">Couldn’t load activity mixes.</p>
      <RoomAction icon="refresh" onClick={() => { setState({ kind: "loading" }); setAttempt(value => value + 1); }}>Retry</RoomAction>
    </div>}
    {index && <div className="activity-mixes">
      {index.activities.map(activity => <article key={activity.id} className="activity-mix">
        <div>
          <h2>{activity.name}</h2>
          <p>{activity.min_bpm}–{activity.max_bpm} BPM · {activity.count} {activity.count === 1 ? "song" : "songs"}</p>
        </div>
        <RoomIconButton icon="play" label={`Play ${activity.name} mix`}
          disabled={activity.count === 0 || starting !== null}
          aria-busy={starting === activity.id} onClick={() => void start(activity)} />
      </article>)}
    </div>}
    {status && <p className="settings-note" role="status">{status}</p>}
  </section>;
}
