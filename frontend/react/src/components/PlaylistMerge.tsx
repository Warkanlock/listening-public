import { useRef, useState, type FormEvent } from "react";
import { Dialog, DialogBackdrop, DialogClose, DialogDescription, DialogTitle, DialogTrigger } from "@ui-sdk/react/dialog";
import { ApiError, postJson, readable, type MergeResult, type PlaylistSummary } from "../api";
import { useStore } from "../store";
import { notify } from "../toasts";
import { RoomAction, RoomSelect } from "./RoomControls";
import { RoomIcon } from "./RoomIcon";
import { RoomDialogContent } from "./WindowContent";

type Feedback = { kind: "conflict" | "failure"; message: string };

export function PlaylistMerge({ playlist, playlists }: { playlist: PlaylistSummary; playlists: PlaylistSummary[] }) {
  const store = useStore();
  const nameInput = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [additional, setAdditional] = useState<string[]>([]);
  const [target, setTarget] = useState("");
  const [name, setName] = useState(playlist.name + " (merged)");
  const [working, setWorking] = useState(false);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const selected = [playlist.id, ...additional];
  const eligible = target ? selected.some(id => id !== target) : additional.length > 0 && !!name.trim();

  async function merge(rename = false) {
    if (!eligible || working) return;
    setWorking(true);
    setFeedback(null);
    try {
      const result = await postJson<MergeResult>("/api/playlists/merge", {
        sources: selected,
        target: target ? { playlist: target } : { name: name.trim() },
        ...(rename ? { on_conflict: "rename" } : {}),
      });
      setOpen(false);
      const count = readable(target ? result.added : result.tracks);
      notify(target ? `Added ${count} to ${result.name}.` : `Created ${result.name} with ${count} songs.`);
      void store.refreshOverview().catch(() => {});
    } catch (failure) {
      setFeedback({
        kind: failure instanceof ApiError && failure.status === 409 ? "conflict" : "failure",
        message: failure instanceof Error ? failure.message : "Couldn’t merge the playlists.",
      });
    } finally {
      setWorking(false);
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void merge();
  }

  return <Dialog open={open} onOpenChange={next => {
    if (working) return;
    if (next) {
      setAdditional([]);
      setTarget("");
      setName(playlist.name + " (merged)");
      setFeedback(null);
    }
    setOpen(next);
  }}>
    <DialogTrigger className="quiet-button room-action"><RoomIcon name="playlists" />Merge playlist</DialogTrigger>
    <DialogBackdrop />
    <RoomDialogContent className="playlist-composer">
      <DialogClose className="dialog-close" aria-label="Close merge playlist" disabled={working}><RoomIcon name="close" /></DialogClose>
      <DialogTitle>Merge playlist</DialogTitle>
      <DialogDescription>Combine songs from {playlist.name} with other playlists.</DialogDescription>
      <form className="composer-form" onSubmit={submit}>
        <RoomSelect label="Merge into" value={target} onChange={key => { setTarget(key); setFeedback(null); }}
          options={[{ value: "", label: "New playlist" }, ...playlists.filter(item => item.source === "local")
            .map(item => ({ value: item.id, label: item.name }))]} disabled={working} />
        {!target && <label>New playlist name <input ref={nameInput} value={name} maxLength={120}
          onChange={event => { setName(event.target.value); setFeedback(null); }} required disabled={working} /></label>}
        <fieldset className="playlist-merge-sources" disabled={working}>
          <legend>Include songs from</legend>
          {playlists.filter(item => item.id !== target).map(item =>
            <label key={item.id}>
              <input type="checkbox" checked={item.id === playlist.id || additional.includes(item.id)} disabled={item.id === playlist.id}
                onChange={event => {
                  const checked = event.target.checked;
                  setAdditional(previous => checked ? [...previous, item.id] : previous.filter(id => id !== item.id));
                }} />
              {item.name}
            </label>)}
        </fieldset>
        {feedback && <p role="alert">{feedback.message}</p>}
        <div className="composer-actions">
          {feedback?.kind === "conflict" ? <>
            <RoomAction onClick={() => { setFeedback(null); nameInput.current?.focus(); }}>Use a different name</RoomAction>
            <RoomAction primary onClick={() => void merge(true)}>Keep both</RoomAction>
          </> : <RoomAction primary type="submit" disabled={!eligible || working}>{working ? "Merging…" : "Merge"}</RoomAction>}
        </div>
      </form>
    </RoomDialogContent>
  </Dialog>;
}
