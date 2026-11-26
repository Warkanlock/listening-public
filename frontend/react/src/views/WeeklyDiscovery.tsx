import { useEffect, useState } from "react";
import { ApiError, api, BUSY_STATUSES, postJson, type RequestRow } from "../api";
import { RoomAction } from "../components/RoomControls";
import { RoomIconButton } from "../components/RoomIcon";
import { RequestStatus, RetryRequestButton } from "../components/RequestStatus";
import { canChangeRecording, canChooseFormat, needsCopyChoice, requestStage } from "../requestState";
import { RecordingVersions } from "../components/RecordingVersions";
import { useStore } from "../store";

type WeeklyTrack = {
  id: string;
  title: string;
  artist: string;
  depth: number;
  trail?: string[];
  in_library?: boolean;
  request?: (Pick<RequestRow, "id" | "status" | "audio" | "copy_review" | "phase" | "retry_at" | "download_percent" | "request_state"> &
    Partial<Pick<RequestRow, "requested_format" | "format_revision" | "title" | "artist" | "version" | "musicbrainz_id">>) | null;
};

type WeeklyPlaylist = {
  week: string;
  state: "waiting" | "building" | "ready" | "unavailable";
  tracks: WeeklyTrack[];
  can_request: boolean;
  download_progress?: { total: number; downloaded: number; pending: number; needs_attention: number; percent: number };
};

const route = "/api/discover/weekly";
const ready = (request: WeeklyTrack["request"]) =>
  request?.status === "downloaded" && request.audio ? request.id : null;
const needsCopy = (track: WeeklyTrack) => !track.in_library &&
  (!track.request || !needsCopyChoice(requestStage(track.request)) && ["not_found", "error", "rejected"].includes(track.request.status));

function requestRow(track: WeeklyTrack): RequestRow | null {
  const request = track.request;
  if (!request || typeof request.format_revision !== "number") return null;
  return { ...request, track_index: null, title: request.title || track.title, artist: request.artist || track.artist,
    musicbrainz_id: request.musicbrainz_id || track.id, requested_format: request.requested_format ?? null,
    format_revision: request.format_revision, message: "" };
}

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
  const progress = playlist?.download_progress;
  const nextReview = tracks.find(track => !track.in_library && track.request && needsCopyChoice(requestStage(track.request)));

  function reviewCopies(track: WeeklyTrack) {
    const request = requestRow(track);
    if (request) store.requestsSlice.chooseFormat(request);
  }

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
        {(nextReview || requestIds.length > 0 || (playlist?.can_request && downloads.length > 0)) && (
          <div className="weekly-discovery-actions">
            {requestIds.length > 0 && <RoomAction primary icon="play"
              onClick={() => void store.sendSession("play_requests", { request_ids: requestIds })}>Play downloaded songs</RoomAction>}
            {playlist?.can_request && downloads.length > 0 && <RoomAction icon="download" disabled={sending}
              onClick={() => void download(downloads.map(track => track.id))}>
              {sending ? "Queuing…" : "Download playlist"}
            </RoomAction>}
            {nextReview && <RoomAction icon="catalogs" onClick={() => reviewCopies(nextReview)}>Review copies</RoomAction>}
          </div>
        )}
      </header>
      {notice && <p role="status">{notice}</p>}
      {progress && progress.total > 0 && <div className="weekly-discovery-progress">
        <progress aria-label="Playlist downloads" max={100} value={progress.percent} />
        <p>{progress.downloaded} of {progress.total} downloaded · {progress.percent}%
          {progress.needs_attention > 0 && ` · ${progress.needs_attention} ${progress.needs_attention === 1 ? "needs" : "need"} attention`}</p>
      </div>}
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
            const row = requestRow(track);
            const stage = track.request && requestStage(track.request);
            const chooseFormat = row && stage && canChooseFormat(stage);
            const review = stage && needsCopyChoice(stage);
            const relation = track.trail?.length ? track.trail.join(" → ") : track.depth === 0
              ? "Artist you’ve played" : `${track.depth} artist ${track.depth === 1 ? "connection" : "connections"} away`;
            return <li key={track.id}>
              <div><strong>{row?.title ?? track.title}</strong><small>{[row?.artist ?? track.artist, row?.version, relation].filter(Boolean).join(" · ")}</small></div>
              <div className="request-actions">
                {track.request && track.request.status !== "missing" && <RequestStatus request={track.request} />}
                {track.request && !track.in_library && <RetryRequestButton request={track.request} title={track.title}
                  onRetry={() => setRevision(value => value + 1)} />}
                {!store.member && !review && !track.in_library && (!track.request || id !== null || row && stage && canChangeRecording(stage)) &&
                  <RecordingVersions song={row ?? { ...track, musicbrainz_id: track.id }}
                    onChoose={song => {
                    if (!row || id !== null) void store.createSongRequest(song);
                    else void store.requestsSlice.chooseRecording(row, song).then(() => setRevision(value => value + 1));
                  }} />}
                {id !== null ? <RoomIconButton icon="play" label={`Play ${track.title}`}
                  onClick={() => void store.sendSession("play_request", { request_id: id })} />
                  : track.request?.status === "missing" ? <small>Copy unavailable</small>
                  : track.in_library ? <small>In library</small>
                  : chooseFormat ? <RoomIconButton icon={review ? "catalogs" : "settings"} label={review
                    ? `Review copies for ${track.title}` : `Choose format for ${track.title}`}
                    onClick={() => reviewCopies(track)} />
                  : playlist?.can_request && needsCopy(track) && !track.request ? <RoomIconButton icon="download"
                    disabled={sending} label={`Download ${track.title}`}
                    onClick={() => void download([track.id])} />
                  : null}
              </div>
            </li>;
          })}
        </ol>
        {tracks.length > 6 && <RoomAction icon={expanded ? "chevronUp" : "chevronDown"} aria-expanded={expanded}
          onClick={() => setExpanded(value => !value)}>{expanded ? "Show fewer songs" : `Show all ${tracks.length} songs`}</RoomAction>}
      </>}
    </section>
  );
}
