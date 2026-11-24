import { useEffect, useState } from "react";
import { ApiError, api, BUSY_STATUSES, postJson, statusLabel } from "../api";
import { RoomAction } from "../components/RoomControls";
import { RoomIconButton } from "../components/RoomIcon";
import { useStore } from "../store";

type WeeklyTrack = {
  id: string;
  title: string;
  artist: string;
  depth: number;
  trail?: string[];
  in_library?: boolean;
  request?: { id: number; status: string; audio?: string } | null;
};

type WeeklyPlaylist = {
  week: string;
  state: "waiting" | "building" | "ready" | "unavailable";
  tracks: WeeklyTrack[];
  can_request: boolean;
};

const route = "/api/discover/weekly";
const ready = (request: WeeklyTrack["request"]) =>
  request?.status === "downloaded" && request.audio ? request.id : null;
const needsCopy = (track: WeeklyTrack) => !track.in_library &&
  (!track.request || ["not_found", "error", "rejected"].includes(track.request.status));

export function WeeklyDiscovery() {
  const store = useStore();
  const [playlist, setPlaylist] = useState<WeeklyPlaylist>();
  const [revision, setRevision] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const requestStatus = store.requestRows.map(row => `${row.id}/${row.status}`).join(",");

  useEffect(() => {
    const abort = new AbortController();
    let timer: number | undefined;
    async function refresh() {
      try {
        const next = await api<WeeklyPlaylist>(route, { signal: abort.signal, cache: "no-store" });
        if (abort.signal.aborted) return;
        setPlaylist(next);
        setError("");
        const busy = next.state === "building" || next.tracks.some(track =>
          track.request && BUSY_STATUSES.includes(track.request.status));
        timer = window.setTimeout(() => void refresh(), busy ? 5000 : 60000);
      } catch (failure) {
        if (abort.signal.aborted) return;
        setError(failure instanceof ApiError && failure.status === 404
          ? "Update the room server to use Weekly Discovery."
          : "Weekly Discovery couldn’t load.");
        timer = window.setTimeout(() => void refresh(), 60000);
      }
    }
    void refresh();
    return () => {
      abort.abort();
      window.clearTimeout(timer);
    };
  }, [revision, requestStatus]);

  const tracks = playlist?.tracks ?? [];
  const requestIds = tracks.flatMap(track => {
    const id = ready(track.request);
    return id === null ? [] : [id];
  });
  const downloads = tracks.filter(needsCopy);
  const edition = playlist?.week && new Date(playlist.week + "T00:00:00");

  async function download(ids: string[]) {
    if (sending || !ids.length) return;
    setSending(true);
    setNotice("");
    try {
      const result = await postJson<{ queued: number; existing: number }>(route, { ids });
      setNotice(result.queued
        ? `${result.queued} ${result.queued === 1 ? "song" : "songs"} queued for download.`
        : "These songs already have a room request or copy.");
      setRevision(value => value + 1);
      void store.loadRequests();
    } catch (failure) {
      setNotice(failure instanceof Error ? failure.message : "Downloads couldn’t be queued.");
    } finally {
      setSending(false);
    }
  }

  const state = playlist?.state;
  return (
    <section className="weekly-discovery" aria-label="Weekly Discovery playlist view">
      <header className="weekly-discovery-header">
        <div>
          {edition && state === "ready" && <p>Week of <time dateTime={playlist?.week}>
            {edition.toLocaleDateString(undefined, { month: "short", day: "numeric" })}
          </time></p>}
        </div>
        {(requestIds.length > 0 || (playlist?.can_request && downloads.length > 0)) && (
          <div className="weekly-discovery-actions">
            {requestIds.length > 0 && <RoomAction primary icon="play"
              onClick={() => void store.sendSession("play_requests", { request_ids: requestIds })}>Play downloaded songs</RoomAction>}
            {playlist?.can_request && downloads.length > 0 && <RoomAction icon="download" disabled={sending}
              onClick={() => void download(downloads.map(track => track.id))}>
              {sending ? "Queuing…" : "Download playlist"}
            </RoomAction>}
          </div>
        )}
      </header>
      {notice && <p role="status">{notice}</p>}
      {error ? <p role="status">{error} <RoomAction onClick={() => setRevision(value => value + 1)}>Try again</RoomAction></p>
        : !state || state === "building" ? <p role="status">Building this week’s playlist…</p>
        : state === "waiting" ? <p>Listen to a few songs to get your first playlist.</p>
        : state === "unavailable" ? <p role="status">Music discovery is temporarily unavailable.{" "}
          <RoomAction onClick={() => setRevision(value => value + 1)}>Try again</RoomAction></p>
        : !tracks.length ? <p>No new songs matched your listening this week.</p> : null}
      {playlist && !playlist.can_request && downloads.length > 0 && <p>Downloads are unavailable in this room.</p>}
      {tracks.length > 0 && <>
        <ol className="weekly-discovery-tracks" aria-label="Weekly Discovery playlist">
          {tracks.slice(0, expanded ? tracks.length : 6).map(track => {
            const id = ready(track.request);
            const relation = track.trail?.length ? track.trail.join(" → ") : track.depth === 0
              ? "Artist you’ve played" : `${track.depth} artist ${track.depth === 1 ? "connection" : "connections"} away`;
            return <li key={track.id}>
              <div><strong>{track.title}</strong><small>{track.artist} · {relation}</small>
                {track.request && needsCopy(track) && <small>{statusLabel(track.request.status)}</small>}
              </div>
              {id !== null ? <RoomIconButton icon="play" label={`Play ${track.title}`}
                onClick={() => void store.sendSession("play_request", { request_id: id })} />
                : track.request?.status === "missing" ? <small>Copy unavailable</small>
                : track.in_library ? <small>In library</small>
                : playlist?.can_request && needsCopy(track) ? <RoomIconButton icon={track.request ? "refresh" : "download"}
                  disabled={sending} label={track.request ? `Retry ${track.title}` : `Download ${track.title}`}
                  onClick={() => void download([track.id])} />
                : track.request && !needsCopy(track) ? <small>{statusLabel(track.request.status)}</small> : null}
            </li>;
          })}
        </ol>
        {tracks.length > 6 && <RoomAction icon={expanded ? "chevronUp" : "chevronDown"} aria-expanded={expanded}
          onClick={() => setExpanded(value => !value)}>{expanded ? "Show fewer songs" : `Show all ${tracks.length} songs`}</RoomAction>}
      </>}
    </section>
  );
}
