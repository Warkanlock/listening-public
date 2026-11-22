import { RoomSelect } from "../../components/RoomControls";
import { useEffect, useState } from "react";
import { Button } from "@ui-sdk/react/button";
import { api, postJson } from "../../api";
import { useStore } from "../../store";
import { errorText } from "./shared";

interface Candidate {
  base: string;
  source: string;
  fixed_base: boolean;
  matched: number;
  matching: string[];
  base_only: string[];
  source_only: string[];
  ambiguous: string[];
}
interface Review {
  sources: Record<string, string>;
  links: { id: string; name: string; sources: string[] }[];
  candidates: Candidate[];
  dismissed: [string, string][];
}
type Action = (body: Record<string, string>) => Promise<void>;

function TrackDifferences({ label, tracks }: { label: string; tracks: string[] }) {
  const [open, setOpen] = useState(false);
  const [limit, setLimit] = useState(30);
  return <details onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{label} ({tracks.length})</summary>
    {open && <>
      <ul>{tracks.slice(0, limit).map((track, i) => <li key={i}>{track}</li>)}</ul>
      {tracks.length > limit && <Button className="quiet-button" onClick={() => setLimit(limit + 30)}>Show more songs</Button>}
    </>}
  </details>;
}

function Match({ candidate, sources, act, busy }: {
  candidate: Candidate; sources: Review["sources"]; act: Action; busy: boolean;
}) {
  const [base, setBase] = useState(candidate.base);
  const [name, setName] = useState(sources[candidate.base]);
  const extra = base === candidate.base ? candidate.source : candidate.base;
  const differences: [string, string[]][] = [
    ["In both lists", candidate.matching],
    [`Only in ${sources[candidate.base]}`, candidate.base_only],
    [`Only in ${sources[candidate.source]}`, candidate.source_only],
    ["Recordings to check", candidate.ambiguous],
  ];
  return (
    <details>
      <summary>{sources[candidate.base]} and {sources[candidate.source]} ({candidate.matched} recordings in common)</summary>
      {differences.filter(([, tracks]) => tracks.length > 0).map(([label, tracks]) => (
        <TrackDifferences key={label} label={label} tracks={tracks} />
      ))}
      <RoomSelect label="Keep the order of" value={base} disabled={busy || candidate.fixed_base} onChange={value => {
        setBase(value); setName(sources[value]);
      }} options={[candidate.base, candidate.source].map(value => ({ value, label: sources[value] }))} />
      {!candidate.fixed_base && <label>Playlist name{" "}
        <input value={name} maxLength={120} disabled={busy} onChange={(event) => setName(event.target.value)} />
      </label>}
      <div className="settings-actions">
        <Button className="quiet-button" disabled={busy || !name.trim()} onClick={() => void act({ op: "confirm", base, source: extra, name })}>Link playlists</Button>
        <Button className="quiet-button" disabled={busy} onClick={() => void act({ op: "dismiss", base, source: extra })}>Keep separate</Button>
      </div>
    </details>
  );
}

export function PlaylistLinks({ revision }: { revision: string }) {
  const store = useStore();
  const [review, setReview] = useState<Review | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [base, setBase] = useState("");
  const [extra, setExtra] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void api<Review>("/api/playlist-links", { signal: controller.signal }).then((data) => {
      if (!controller.signal.aborted) { setReview(data); setMessage(""); }
    }).catch((error) => {
      if (!controller.signal.aborted) setMessage(errorText(error, "Could not load playlist matches"));
    });
    return () => controller.abort();
  }, [revision, refresh]);
  const act: Action = async (body) => {
    setBusy(true);
    try {
      setReview(await postJson<Review>("/api/playlist-links", body));
      await store.refreshOverview();
      await store.loadTracks(true);
      setMessage("");
    } catch (error) {
      setMessage(errorText(error, "Could not update playlist links"));
    } finally {
      setBusy(false);
    }
  };
  const grouped = new Set(review?.links.flatMap(link => link.sources));
  const choices = Object.entries(review?.sources ?? {}).filter(([key]) => !grouped.has(key));
  return (
    <div className="settings-card">
      <div className="settings-card-heading">
        <h3>Playlist matches</h3>
        <Button className="quiet-button" disabled={busy} onClick={() => setRefresh((n) => n + 1)}>Refresh matches</Button>
      </div>
      {message && <p role="alert">{message}</p>}
      {!review && !message && <p>Looking for matching playlists…</p>}
      {review && <>
        {!review.links.length && !review.candidates.length && <p>No matching playlists found.</p>}
        {choices.length > 1 && <details><summary>Group two playlists manually</summary>
          <p>Show both sources as one playlist in Listening. Each source keeps its own songs and order.</p>
          <div className="settings-actions">
            <RoomSelect label="Keep the name and order of" value={base} onChange={setBase}
              options={[{ value: "", label: "Choose a playlist" }, ...choices.map(([id, name]) => ({
                value: id, label: `${name} (${id})`, disabled: id === extra,
              }))]} />
            <RoomSelect label="Include songs from" value={extra} onChange={setExtra}
              options={[{ value: "", label: "Choose a playlist" }, ...choices.map(([id, name]) => ({
                value: id, label: `${name} (${id})`, disabled: id === base,
              }))]} />
            <Button className="quiet-button" disabled={busy || !choices.some(([id]) => id === base) || !choices.some(([id]) => id === extra)}
              onClick={() => void act({ op: "group", base, source: extra })}>Group in Listening</Button>
          </div>
        </details>}
        {review.links.map((link) => <div className="source-item" key={link.id}>
          <div><strong>{link.name}</strong><span>{link.sources.map((key) => review.sources[key] ?? key).join(", ")}</span></div>
          <Button className="quiet-button" disabled={busy} onClick={() => void act({ op: "unlink", id: link.id })}>Unlink</Button>
        </div>)}
        {review.candidates.map((candidate) => <Match key={JSON.stringify([candidate.base, candidate.source])}
          candidate={candidate} sources={review.sources} act={act} busy={busy} />)}
        {review.dismissed.length > 0 && <details><summary>Kept separate</summary>
          {review.dismissed.map(([base, source]) => <div className="source-item" key={JSON.stringify([base, source])}>
            <span>{review.sources[base] ?? base} and {review.sources[source] ?? source}</span>
            <Button className="quiet-button" disabled={busy} onClick={() => void act({ op: "reconsider", base, source })}>Review again</Button>
          </div>)}
        </details>}
      </>}
    </div>
  );
}
