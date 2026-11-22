import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { LoadingStatus, SkeletonLines } from "./StateCard";
import { RoomIcon, RoomIconButton } from "./RoomIcon";
import { api } from "../api";
import { parseConnectionGraph, type ConnectionEdge, type ConnectionGraph, type ConnectionNode, type ConnectionTarget, type TrackCredit } from "../artistConnections";

type Frame = { target: ConnectionTarget; title: string; via?: ConnectionEdge };
type RequestState =
  | { path: string; status: "ready"; graph: ConnectionGraph }
  | { path: string; status: "error" };
type Position = { x: number; y: number };

function branchPosition(index: number, count: number): Position {
  const firstSide = Math.ceil(count / 2);
  const left = index < firstSide;
  const sideCount = left ? firstSide : count - firstSide;
  const slot = left ? index : index - firstSide;
  return { x: left ? 20 : 80, y: sideCount > 1 ? 13 + slot * 74 / (sideCount - 1) : 50 };
}

function sourceLink(entity: ConnectionNode) {
  return `https://musicbrainz.org/${entity.kind}/${entity.id}`;
}

function ConnectionsMap({ graph, selected, preview, explore, inspect }: {
  graph: Extract<ConnectionGraph, { state: "ready" }>;
  selected: string | null;
  preview: (id: string | null) => void;
  explore: (edge: ConnectionEdge, keyboard: boolean) => void;
  inspect: () => void;
}) {
  const { root, edges } = graph;
  const positioned = edges.map((edge, index) => ({ edge, point: branchPosition(index, edges.length) }));
  return (
    <div className="connections-map" data-dense={edges.length > 6} role="group" aria-label={`Connections around ${root.title}`}>
      <svg className="connections-wires" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
        {positioned.map(({ edge, point }) => (
          <path
            key={edge.node.id}
            data-highlighted={selected === edge.node.id}
            d={`M 50 50 C ${point.x} 50, 50 ${point.y}, ${point.x} ${point.y}`}
            vectorEffect="non-scaling-stroke"
          />
        ))}
      </svg>
      <button type="button" className="connections-node connections-hub" aria-label={`Inspect ${root.title}`}
        onClick={inspect} onFocus={inspect} onPointerEnter={inspect}>
        <span>{root.detail}</span>
        <strong>{root.title}</strong>
      </button>
      {positioned.map(({ edge, point }, index) => (
        <button
          key={edge.node.id}
          type="button"
          className="connections-node"
          data-highlighted={selected === edge.node.id}
          style={{ left: `${point.x}%`, top: `${point.y}%`, animationDelay: `${index * 14}ms` }}
          aria-label={`Explore ${edge.node.title} · ${edge.label}`}
          onClick={(event) => explore(edge, event.detail === 0)}
          onFocus={() => preview(edge.node.id)}
          onPointerEnter={() => preview(edge.node.id)}
        >
          <span>{edge.label}</span>
          <strong>{edge.node.title}</strong>
        </button>
      ))}
    </div>
  );
}

function LibraryMap({ track, showDetails }: { track: TrackCredit; showDetails: () => void }) {
  return <section className="library-credit-map" aria-label="Catalog connections">
    {track.artist && <div><small>Artist</small><strong>{track.artist}</strong></div>}
    {track.album && <div><small>Album</small><strong>{track.album}</strong></div>}
    {track.title && <button type="button" onClick={showDetails}>
      <small>Song information</small><strong>{track.title}</strong>
    </button>}
  </section>;
}

function ConnectionsLoading({ via }: { via?: ConnectionEdge }) {
  return <div className="connections-stage" data-loading>
    <div className="connections-map connections-map-loading" aria-hidden="true">
      <div className="connections-node connections-hub"><SkeletonLines count={2} /></div>
      {Array.from({ length: 4 }, (_, index) => {
        const point = branchPosition(index, 4);
        return <div key={index} className="connections-node"
          style={{ left: `${point.x}%`, top: `${point.y}%` }}>
          <SkeletonLines count={2} />
        </div>;
      })}
    </div>
    <aside className="connections-panel connections-inspector">
      {via && <span className="connections-type">{[via.label, via.note].filter(Boolean).join(" · ")}</span>}
      <LoadingStatus>Looking up credits…</LoadingStatus>
    </aside>
  </div>;
}

export function ArtistConnections({ target, title, track, artistId, cache, showDetails }: {
  target: ConnectionTarget; title: string; track: TrackCredit; artistId?: string;
  cache: Map<string, ConnectionGraph>; showDetails: () => void;
}) {
  const [history, setHistory] = useState<Frame[]>([]);
  const [request, setRequest] = useState<RequestState | null>(null);
  const [retry, setRetry] = useState(0);
  const [highlight, setHighlight] = useState<string | null>(null);
  const originTitle = useRef(title);
  const followFocus = useRef(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const frame = history.at(-1) ?? { target, title };
  const path = `/api/connections/${frame.target.kind}/${frame.target.id}`;
  const fallbackArtist = !history.length && target.kind === "recording" && artistId &&
    /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(artistId) ? artistId : null;
  const response = request?.path === path ? request : null;
  const graph = cache.get(path) ?? (response?.status === "ready" ? response.graph : null);
  const loading = !graph && !response;
  const edge = graph?.state === "ready" ? graph.edges.find(item => item.node.id === highlight) : undefined;
  const subject = edge?.node ?? (graph?.state === "ready" ? graph.root : null);

  useEffect(() => {
    if (cache.has(path)) return;
    const controller = new AbortController();
    const read = (url: string) => api<unknown>(url, { signal: controller.signal }).then(parseConnectionGraph);
    async function load() {
      let graph: ConnectionGraph;
      try {
        graph = await read(path);
      } catch (error) {
        if (!fallbackArtist || controller.signal.aborted) throw error;
        return read("/api/connections/artist/" + fallbackArtist);
      }
      if (fallbackArtist && (graph.state !== "ready" || !graph.edges.length)) {
        return read("/api/connections/artist/" + fallbackArtist);
      }
      return graph;
    }
    load()
      .then(result => {
        if (controller.signal.aborted) return;
        if (result.state === "ready" || !result.retry) cache.set(path, result);
        setRequest({ path, status: "ready", graph: result });
      })
      .catch(() => {
        if (!controller.signal.aborted) setRequest({ path, status: "error" });
      });
    return () => controller.abort();
  }, [path, retry, fallbackArtist, cache]);

  useLayoutEffect(() => {
    if (followFocus.current) {
      followFocus.current = false;
      heading.current?.focus();
    }
  }, [path]);

  const navigate = (next: Frame[], keyboard: boolean) => {
    followFocus.current = keyboard;
    setHighlight(null);
    setRequest(null);
    setHistory(next);
  };
  const explore = (item: ConnectionEdge, keyboard: boolean) => {
    if (!history.length && graph?.state === "ready") originTitle.current = graph.root.title;
    if (history.length >= 24) {
      navigate([{ target: item.node, title: item.node.title, via: item }], keyboard);
      return;
    }
    navigate([...history, { target: item.node, title: item.node.title, via: item }], keyboard);
  };

  const origin = !history.length;
  const library = graph?.library ?? (graph?.state === "unmatched"
    ? { ...track, artist: graph.artist || track.artist, title: target.kind === "album" ? track.title : graph.title || track.title }
    : track);
  const root = graph?.state === "ready" ? graph.root : frame.via?.node;
  const showLibrary = origin && Boolean(library.title || library.artist || library.album) &&
    (graph?.state !== "ready" || !graph.edges.length);
  const retryable = response?.status === "error" || (graph?.state === "unmatched" && graph.retry);
  const showInspector = !showLibrary || !!root || retryable;
  const retryLookup = () => { setRequest(null); setRetry(value => value + 1); };

  return (
    <>
      <div className="connections-path">
        {!origin && (
          <RoomIconButton icon="back" label={`Back to ${history.at(-2)?.title ?? originTitle.current}`}
            onClick={(event) => navigate(history.slice(0, -1), event.detail === 0)} />
        )}
        <h3 ref={heading} tabIndex={-1}>{root?.title ?? frame.title}</h3>
      </div>

      {loading ? <ConnectionsLoading via={frame.via} /> : graph?.state === "ready" && !showLibrary ? (
        <div className="connections-stage" key={path}>
          <ConnectionsMap graph={graph} selected={highlight} preview={setHighlight} explore={explore}
            inspect={() => setHighlight(null)} />
          <aside className="connections-panel connections-inspector" aria-label="Credit details">
            <span className="connections-type">{edge?.label ?? frame.via?.label ?? (origin && root?.kind === "artist" && graph.library ? "Library artist" : "Exploring")}</span>
            <h4>{subject?.title}</h4>
            {subject ? <p>{subject.detail}</p> : null}
            {(edge?.note ?? frame.via?.note) ? <p>{edge?.note ?? frame.via?.note}</p> : null}
            {!graph.edges.length && history.length > 0 && <p>From {history.at(-2)?.title ?? originTitle.current}</p>}
            {subject ? <a className="room-action quiet-button connections-source" href={sourceLink(subject)} target="_blank" rel="noreferrer">MusicBrainz <RoomIcon name="northeast" /></a> : null}
          </aside>
        </div>
      ) : (
        <>
          {showLibrary && <LibraryMap track={library} showDetails={showDetails} />}
          {showInspector && <div className="connections-panel connections-state" aria-live="polite">
            {frame.via && <p>{[frame.via.label, frame.via.note].filter(Boolean).join(" · ")}</p>}
            {root && <a className="room-action quiet-button connections-source" href={sourceLink(root)} target="_blank" rel="noreferrer">MusicBrainz <RoomIcon name="northeast" /></a>}
            {response?.status === "error" && <p role="alert">These credits couldn’t load.</p>}
            {retryable && <button type="button" className="quiet-button" onClick={retryLookup}>Retry artist lookup</button>}
          </div>}
        </>
      )}
    </>
  );
}
