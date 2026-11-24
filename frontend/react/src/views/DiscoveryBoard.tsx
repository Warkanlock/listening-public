import { useEffect, useState, type ReactNode } from "react";
import { api, type DiscoverTrack } from "../api";
import { CoverArt } from "../components/Artwork";
import { RoomIcon } from "../components/RoomIcon";
import { recentMusicSearches } from "../recentMusicSearches";
import { useStore } from "../store";
import type { TrailStop } from "./DeepDive";

type Song = { id: number; title: string; artist: string };
type Activity = { liked: Song[]; played: Song[] };
type Story = { title: string; published: string; url: string };
type OpenTrail = (stop: TrailStop, origin: HTMLButtonElement) => void;

function useBoardData<T>(route: string, revision = "") {
  const [data, setData] = useState<T>();
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const request = new AbortController();
    setFailed(false);
    api<T>(route, { signal: request.signal }).then(value => {
      if (!request.signal.aborted) setData(value);
    }).catch(() => { if (!request.signal.aborted) setFailed(true); });
    return () => request.abort();
  }, [route, attempt, revision]);
  return { data, failed, retry: () => setAttempt(value => value + 1) };
}

function BoardStatus({ loading, failed, retry, empty }: { loading: boolean; failed: boolean; retry: () => void; empty: string }) {
  return <p role="status">{failed ? <>Couldn’t load. <button className="quiet-button" onClick={retry}>Try again</button></>
    : loading ? "Loading…" : empty}</p>;
}

function BoardLink({ title, note, cover, onClick }: {
  title: string; note?: string; cover?: string; onClick: (button: HTMLButtonElement) => void;
}) {
  return <button type="button" className="board-link" onClick={event => onClick(event.currentTarget)}>
    {cover && <span className="board-art"><CoverArt cover={cover} fallback={null} /></span>}
    <span><strong>{title}</strong>{note && <small>{note}</small>}</span><RoomIcon name="chevronRight" />
  </button>;
}

function Tile({ title, tone, children }: { title: string; tone?: string; children: ReactNode }) {
  return <section className="discovery-tile" aria-label={title} data-tone={tone}>
    <h2>{title}</h2>{children}
  </section>;
}

function RecordingPicks({ tracks, onOpen }: { tracks: DiscoverTrack[]; onOpen: OpenTrail }) {
  const picks = [...tracks].sort((a, b) => Number(!!b.version) - Number(!!a.version)).slice(0, 12);
  return <section className="discovery-recordings" aria-label="Recordings to hear">
    <h2>Recordings to hear</h2>
    <ul>{picks.map(track => <li key={track.musicbrainz_id}>
      <button className="discovery-recording" type="button" onClick={event => onOpen({
        kind: "recording", id: track.musicbrainz_id, name: track.title, label: "Recording", track,
      }, event.currentTarget)}>
        {track.cover && <span className="discovery-recording-art"><CoverArt cover={track.cover} fallback={null} /></span>}
        <span className="discovery-recording-info"><strong>{track.title}</strong>
          <small>{[track.artist, track.version || track.album].filter(Boolean).join(" · ")}</small></span>
        <RoomIcon name="chevronRight" />
      </button>
    </li>)}</ul>
  </section>;
}

export function DiscoveryBoard({ onOpen }: { onOpen: OpenTrail }) {
  const store = useStore();
  const revision = [store.overview?.favorites, store.session?.occurrence, store.session?.current?.id].join("/");
  const activity = useBoardData<Activity>("/api/discover/activity", revision);
  const press = useBoardData<{ stories: Story[] }>("/api/discover/press");
  const searches = recentMusicSearches();
  const shelf = store.discoverState === "ready" ? store.discover : null;
  const tracks = shelf?.tracks ?? [];
  const roots = new Map<string, { name: string; leads: Set<string> }>();
  for (const track of [...tracks].sort((a, b) => (a.seed_rank ?? 0) - (b.seed_rank ?? 0))) {
    const name = track.seed_artist || track.artist;
    const id = track.seed_id || name;
    const root = roots.get(id) ?? { name, leads: new Set<string>() };
    root.leads.add(track.artist === name ? track.title : track.artist);
    roots.set(id, root);
  }
  const words = (value: string) => ' ' + value.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim() + ' ';
  const artists = [...(activity.data?.liked ?? []), ...(activity.data?.played ?? [])].map(song => words(song.artist)).filter(artist => artist.trim());
  const related = (story: Story) => artists.some(artist => words(story.title).includes(artist));
  const stories = [...(press.data?.stories ?? [])].sort((a, b) => Number(related(b)) - Number(related(a))).slice(0, 3);
  const weeklyFailed = store.discoverState === "error" || store.discoverState === "failed";
  return <div role="region" aria-label="Discovery board">
    {tracks.length > 0 && <RecordingPicks tracks={tracks} onOpen={onOpen} />}
    <div className="discovery-board">
    <Tile title="Artist trails" tone="trails">
      {shelf?.week && <p className="board-edition">{shelf.week.replace(/^(\d{4})-W(\d{2})$/, "Week $2 · $1")}</p>}
      {[...roots].map(([id, root]) => <BoardLink key={id} title={root.name} note={`Explore ${[...root.leads].slice(0, 2).join(" · ")}`}
        onClick={button => onOpen({ kind: "artist", id, name: root.name, label: "From your library" }, button)} />)}
      {!roots.size && <BoardStatus failed={weeklyFailed} loading={store.discoverState !== "ready"} retry={() => void store.loadDiscover()} empty="No new artist trails this week." />}
    </Tile>
    <Tile title="Recently liked" tone="likes">
      {activity.data?.liked.length ? <><p>Explore the credits behind these favorites.</p>
        {activity.data.liked.map(song => <BoardLink key={song.id} title={song.title} note={song.artist}
          onClick={button => onOpen({ kind: "song", id: String(song.id), name: song.title, note: song.artist, label: `Recently liked · ${song.artist}` }, button)} />)}</> :
        <BoardStatus loading={!activity.data} failed={activity.failed} retry={activity.retry} empty="Like a song to explore its credits here." />}
    </Tile>
    <Tile title="Recently played">
      {activity.data?.played.length ? <><p>Explore recordings beyond the room’s library.</p>
        {activity.data.played.map(song => <BoardLink key={song.id} title={song.artist} note={song.title}
          onClick={button => onOpen({ kind: "search", id: song.artist.slice(0, 160), name: song.artist, label: `Because the room played “${song.title}”` }, button)} />)}</> :
        <BoardStatus loading={!activity.data} failed={activity.failed} retry={activity.retry} empty="Played library songs will appear here." />}
    </Tile>
    <Tile title="The music press" tone="press">
      <p>The Guardian · Past seven days</p>
      {stories.map(story => <a className="board-story" key={story.url} href={story.url} target="_blank" rel="noopener noreferrer">
        <time dateTime={story.published}>{new Date(story.published).toLocaleDateString(undefined, { month: "short", day: "numeric" })}</time>
        <strong>{story.title}</strong><RoomIcon name="northeast" />
      </a>)}
      {!stories.length && <BoardStatus loading={!press.data} failed={press.failed} retry={press.retry} empty="No stories from the past seven days." />}
    </Tile>
    <Tile title="Recent searches" tone="search">
      {searches.length ? <><p>Find recordings outside your library.</p>
        {searches.map(query => <BoardLink key={query} title={query} onClick={button => onOpen({ kind: "search", id: query, name: query, label: "From a recent search" }, button)} />)}</> :
        <><p>Public music searches from this device will appear here.</p>
          <button className="board-browse" type="button" onClick={() => store.setView("browse")}><RoomIcon name="search" /> Browse catalogs</button></>}
    </Tile>
    </div>
  </div>;
}
