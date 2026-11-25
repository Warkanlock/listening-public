import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, BUSY_STATUSES, statusLabel, time, type DiscoverTrack, type RequestRow } from "../api";
import { useStore } from "../store";
import { RoomAction } from "../components/RoomControls";
import { RoomIcon, RoomIconButton } from "../components/RoomIcon";
import { CoverArt } from "../components/Artwork";
import { parseConnectionGraph } from "../artistConnections";
import { DiscoveryBoard } from "./DiscoveryBoard";
import { AddToPlaylist } from "../components/PlaylistComposer";
import { TryMp3Button } from "../components/TryMp3Button";
import { RequestStatus, RetryRequestButton } from "../components/RequestStatus";
import { canChangeRecording, canChooseFormat, needsCopyChoice, requestStage, requestStageLabel } from "../requestState";
import { RecordingVersions } from "../components/RecordingVersions";
import { WeeklyDiscovery } from "./WeeklyDiscovery";

export type TrailStop = { kind: "artist" | "recording" | "search" | "song"; id: string; name: string; label: string; note?: string; track?: DiscoverTrack };
type Branch = { leads: TrailStop[]; partial?: boolean; track?: DiscoverTrack; source?: { name: string; url: string } | null; request?: RequestRow | null };
type Frame = { stop: TrailStop; visible: number };
const identity = (stop: TrailStop) => stop.kind + "/" + stop.id;
const frame = (stop: TrailStop): Frame => ({ stop, visible: 3 });

function weeklyLeads(stop: TrailStop, tracks: DiscoverTrack[]): TrailStop[] {
  if (stop.kind !== "artist") return [];
  const leads: TrailStop[] = [];
  for (const track of tracks) {
    if ((track.artist_id || track.artist) === stop.id) {
      leads.push({ kind: "recording", id: track.musicbrainz_id, name: track.title, label: "Recording", track });
    } else if ((track.seed_id || track.seed_artist) === stop.id) {
      leads.push({ kind: "artist", id: track.artist_id || track.artist, name: track.artist, label: "Suggested artist",
        note: track.title });
    }
  }
  return leads;
}

function RecordingActions({ track, branch }: { track: DiscoverTrack; branch?: Branch }) {
  const store = useStore();
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState<RequestRow | null>(null);
  const row = store.requestRows.find(r => r.musicbrainz_id === track.musicbrainz_id ||
    r.original_recording_id === track.musicbrainz_id || r.id === submitted?.id) ?? submitted ?? branch?.request ?? track.request;
  const ready = row?.status === "downloaded" && !!row.audio;
  const stage = row && requestStage(row);
  const choosing = stage && canChooseFormat(stage);
  const review = stage && needsCopyChoice(stage);
  const busy = !!row && BUSY_STATUSES.includes(row.status);
  const search = new URL("https://www.youtube.com/results");
  search.searchParams.set("search_query", [track.artist, track.title, track.version, track.performance].filter(Boolean).join(" "));
  const requestLabel = ready ? "Play in room" : review ? "Review copies"
    : choosing ? "Choose format" : busy && row ? requestStageLabel(row) : "Find a copy";
  const listenLabel = branch?.source ? "Listen on " + branch.source.name : "Search on YouTube";
  async function request() {
    if (ready && row) return void store.sendSession("play_request", { request_id: row.id });
    if (choosing && row) return store.requestsSlice.chooseFormat(row);
    setSubmitting(true);
    try {
      setSubmitted(await store.createSongRequest({ artist: track.artist, title: track.title, album: track.album,
        duration: track.duration, version: track.version, performance: track.performance, musicbrainz_id: track.musicbrainz_id }));
    } finally { setSubmitting(false); }
  }
  return <>
    <div className="trail-actions">
      <a className="quiet-button icon-control" href={branch?.source?.url ?? search.href} target="_blank"
        rel="noopener noreferrer" aria-label={listenLabel} title={listenLabel}>
        <RoomIcon name={branch?.source ? "listen" : "search"} />
      </a>
      {stage !== "copy_missing" && <RoomIconButton primary label={requestLabel}
        icon={ready ? "play" : review ? "catalogs" : choosing ? "settings" : busy ? "recent" : "download"}
        onClick={() => void request()}
        disabled={submitting || (!ready && !choosing && (busy || !track.duration))} />}
      {!store.member && row?.mp3_retry && <TryMp3Button retry={row.mp3_retry} title={track.title} />}
      {row && <RetryRequestButton request={row} title={track.title} />}
      {!store.member && (!row || ready || stage && canChangeRecording(stage)) && <RecordingVersions
        song={row?.musicbrainz_id ? row : track}
        onChoose={song => void (row && !ready ? store.requestsSlice.chooseRecording(row, song) : store.createSongRequest(song))} />}
      <RoomIconButton icon="info" label="Song details" onClick={() => store.openSongInfo(track)} />
      <AddToPlaylist song={track} iconOnly />
    </div>
    {!track.duration && !row && <p>A duration is needed to verify a room copy.</p>}
    {row && <div role="status"><RequestStatus request={row} />
      {row.musicbrainz_id && row.musicbrainz_id !== track.musicbrainz_id && <p>{[row.version || row.title, row.artist].join(" · ")}</p>}
      {review ? <p>{stage === "copy_unavailable" ? "Choose another copy to continue." : "No copy was saved."}</p>
        : row.message && row.message !== statusLabel(row.status) && <p>{row.message}</p>}
    </div>}
  </>;
}

function Explorer({ tracks, start, onBack }: { tracks: DiscoverTrack[]; start: TrailStop; onBack: () => void }) {
  const [path, setPath] = useState<Frame[]>(() => [frame(start)]);
  const [attempt, setAttempt] = useState(0);
  const [response, setResponse] = useState<{ key: string; data?: Branch; failed?: boolean }>();
  const focus = useRef("heading");
  const heading = useRef<HTMLHeadingElement>(null);
  const fork = useRef<HTMLUListElement>(null);
  const current = path[path.length - 1];
  const stop = current.stop;
  const key = identity(stop);
  const remote = stop.kind === "search" || stop.kind === "song" || /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(stop.id);
  const route = stop.kind === "song" ? "/api/connections/song/" + stop.id :
    stop.kind === "search" ? "/api/discover/matches?" + new URLSearchParams({ q: stop.id }) : "/api/discover/" + key;
  const result = response?.key === key ? response : undefined;
  useEffect(() => {
    if (!remote) return;
    const controller = new AbortController();
    setResponse(undefined);
    const options = { signal: controller.signal };
    const request = stop.kind === "song"
      ? api<unknown>(route, options).then(value => {
          const graph = parseConnectionGraph(value);
          if (graph.state === "ready" && graph.root.kind === "artist") {
            return { leads: [{ kind: "artist" as const, id: graph.root.id, name: graph.root.title, label: "Library artist" }] };
          }
          const edges = graph.state === "ready" ? graph.edges : graph.credit ? [{ node: graph.credit, label: "Artist", note: "" }] : [];
          return { leads: edges.flatMap(({ node, label, note }) => node.kind === "artist"
            ? [{ kind: "artist" as const, id: node.id, name: node.title, label, note }] : []) };
        })
      : api<Branch>(route, options);
    request
      .then(data => { if (!controller.signal.aborted) setResponse({ key, data }); })
      .catch(() => { if (!controller.signal.aborted) setResponse({ key, failed: true }); });
    return () => controller.abort();
  }, [key, attempt, remote, route, stop.kind]);

  useLayoutEffect(() => {
    if (!focus.current) return;
    const target = [...(fork.current?.querySelectorAll("button") ?? [])].find(btn => btn.dataset.stop === focus.current);
    if (target || focus.current === "heading" || result) {
      (target ?? heading.current)?.focus();
      focus.current = "";
    } else {
      heading.current?.focus();
    }
  }, [key, result]);

  // The weekly leads appear immediately, including on rooms with an older shelf.
  const leads = new Map<string, TrailStop>();
  for (const lead of [...weeklyLeads(stop, tracks), ...(result?.data?.leads ?? [])]) {
    const id = identity(lead);
    if (!path.some(f => identity(f.stop) === id) && !leads.has(id)) leads.set(id, lead);
  }
  if (stop.kind === "song" && stop.note) {
    const search: TrailStop = { kind: "search", id: stop.note.slice(0, 160), name: stop.note, label: "Search artist’s recordings" };
    leads.set(identity(search), search);
  }
  const choices = [...leads.values()];
  const detail = result?.data?.track;
  const track = detail && stop.track ? { ...stop.track, ...detail, version: detail.version || stop.track.version,
    performance: detail.performance || stop.track.performance, album: detail.album || stop.track.album, duration: detail.duration || stop.track.duration } : stop.track;
  function follow(lead: TrailStop) {
    focus.current = "heading";
    setPath([...path, frame(lead)]);
  }
  function back(index: number) {
    focus.current = identity(path[index + 1].stop);
    setPath(path.slice(0, index + 1));
  }
  return <div className="discovery-trail">
    <div className="trail-toolbar">
      <nav aria-label="Discovery trail"><ol className="trail-path">
        {path.map(({ stop: entry }, i) => <li key={identity(entry)}>
          {i > 0 && <small>{entry.label}</small>}
          {i === path.length - 1 ? <span aria-current="step">{entry.name}</span> :
            <button type="button" onClick={() => back(i)}>{entry.name}</button>}
        </li>)}
      </ol></nav>
      <RoomIconButton icon="back" label="Back to discovery board" onClick={onBack} />
    </div>
    <div className="trail-stage">
      <article className="trail-stop" key={key} aria-label={stop.kind === "artist" ? "Selected artist" : stop.kind === "search" ? "Selected search" : "Selected recording"}>
        <p className="trail-relation">{stop.label}</p>
        <h2 ref={heading} tabIndex={-1}>{stop.name}</h2>
        {track && <>
          <div className="trail-release">{track.cover && <CoverArt cover={track.cover} fallback={null} />}
            <p><strong>{track.artist}</strong><br />
              {[track.version, track.performance, track.album, track.duration ? time(track.duration) : ""].filter(Boolean).join(" · ")}</p>
          </div>
          <RecordingActions track={track} branch={result?.data} />
        </>}
      </article>
      <section className="trail-fork" aria-label="Paths to follow">
        <ul ref={fork} className="trail-leads">
          {choices.slice(0, current.visible).map(lead => <li key={identity(lead)}>
            <button type="button" data-kind={lead.kind} data-stop={identity(lead)} onClick={() => follow(lead)}>
              <span>{lead.label}</span><strong>{lead.name}</strong>
              {(lead.note || lead.track) && <small>{lead.note || [lead.track?.artist, lead.track?.version || lead.track?.album].filter(Boolean).join(" · ")}</small>}
            </button></li>)}
        </ul>
        {choices.length > current.visible && <RoomIconButton icon="chevronDown" label="Show more" onClick={() =>
          setPath([...path.slice(0, -1), { ...current, visible: current.visible + 3 }])} />}
        {remote && !result && <p role="status">Looking for connections…</p>}
        {(result?.failed || result?.data?.partial) && <p role="status">More connections couldn’t load. <RoomIconButton icon="refresh" label="Retry branch"
          onClick={() => setAttempt(n => n + 1)} /></p>}
        {(!remote || (result?.data && !result.data.partial)) && !choices.length && <p>{stop.kind === "search" ? "No new recordings matched this search." : "No further connections here."}</p>}
      </section>
    </div>
  </div>;
}

export function DeepDive() {
  const store = useStore();
  const [start, setStart] = useState<TrailStop | null>(null);
  const [weekly, setWeekly] = useState(false);
  const origin = useRef<HTMLElement | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  useLayoutEffect(() => {
    if (weekly) heading.current?.focus();
    if (!start && !weekly && origin.current) {
      (origin.current.isConnected ? origin.current : heading.current)?.focus();
      origin.current = null;
    }
  }, [start, weekly]);
  if (weekly) return <section className="view">
    <header className="listening-view-head">
      <div className="weekly-page-title">
        <RoomIconButton icon="back" label="Back to Deep Dive" onClick={() => setWeekly(false)} />
        <h1 ref={heading} tabIndex={-1}>Weekly Discovery</h1>
      </div>
    </header>
    <WeeklyDiscovery />
  </section>;
  const weeklyEdition = store.discover?.weekly_edition;
  const monday = weeklyEdition && new Date(weeklyEdition + "T00:00:00Z");
  const edition = monday ? Math.floor(monday.valueOf() / (7 * 86400000)) % 3 : 0;
  return <section className="view">
    <header className="listening-view-head" data-align="start"><h1 ref={heading} tabIndex={-1}>Deep Dive</h1>
      <RoomAction icon="listen" onClick={() => store.setView("activities")}>Activities</RoomAction>
    </header>
    <div hidden={!!start}>
      <aside className="weekly-entry" data-edition={edition} aria-label="Weekly Discovery">
        <div><h2>Weekly Discovery</h2>{monday && <p>Week of <time dateTime={weeklyEdition}>
          {monday.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" })}</time></p>}</div>
        <RoomIconButton icon="chevronRight" label="Open Weekly Discovery" onClick={() => {
          origin.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
          setWeekly(true);
        }} />
      </aside>
      <DiscoveryBoard onOpen={(stop, button) => { origin.current = button; setStart(stop); }} />
    </div>
    {start && <Explorer start={start} tracks={store.discoverState === "ready" ? store.discover?.tracks ?? [] : []} onBack={() => setStart(null)} />}
  </section>;
}
