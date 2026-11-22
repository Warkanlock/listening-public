import { useEffect, useId, useState } from "react";
import { api, BUSY_STATUSES, statusLabel, time, type RequestRow } from "../api";
import { useStore } from "../store";
import { CoverArt } from "./Artwork";
import { RoomIcon } from "./RoomIcon";
import { RoomAction } from "./RoomControls";
import { LoadingStatus, SkeletonLines, Spinner } from "./StateCard";

type Target = { kind: "artist" | "album" | "release-group"; id: string | number; title?: string };
type Related = { id: string; title: string; detail?: string; year?: string };
type Release = Related & { cover?: string; listens?: number };
type LibraryAlbum = { title: string; track_id: number; cover?: string; plays?: number };
type Highlights = { tracks: Track[]; albums: Release[]; releases: Release[] };
type Track = {
  track_id?: number; ready?: boolean; title: string; artist: string; album: string;
  duration: number; musicbrainz_id: string; version?: string; disc?: number; number?: string; request?: RequestRow; library_only?: boolean;
  copy_status?: "not_found";
  listens?: number; plays?: number; cover?: string;
};
export type Profile = {
  kind: "artist" | "album"; identity?: string; title: string; artist?: string;
  artist_id?: string; detail?: string; description?: string; area?: string; year?: string; edition?: string;
  unavailable?: boolean; truncated?: boolean;
  releases?: Release[]; connections?: Related[]; library_albums?: LibraryAlbum[];
  library?: { tracks: number; ready: number; albums: number };
  tracks: Track[];
};

type PlayRef = number | string;

function requestFor(track: Track, requests: RequestRow[]) {
  return requests.find(item => track.track_id
    ? item.track_index === track.track_id
    : !!track.musicbrainz_id && item.musicbrainz_id === track.musicbrainz_id) ?? track.request;
}

function playableRef(track: Track, row?: RequestRow | null): PlayRef | undefined {
  if (track.ready && track.track_id) return track.track_id;
  if (row?.status === "downloaded" && row.audio) return row.track_index || `custom-${row.id}`;
  return undefined;
}

const numberFormat = new Intl.NumberFormat();

function ProfileTrack({ track, albumQueue, rank }: { track: Track; albumQueue: PlayRef[]; rank?: number }) {
  const store = useStore();
  const [submitted, setSubmitted] = useState<RequestRow | null>(null);
  const [pending, setPending] = useState(false);
  const row = requestFor(track, store.requestRows) ?? submitted;
  const reference = playableRef(track, row);
  const ready = reference !== undefined;
  const busy = !!row && BUSY_STATUSES.includes(row.status);
  const format = !!row && ["waiting_format", "not_found", "error", "rejected"].includes(row.status);
  const canRequest = !!track.track_id || !!track.musicbrainz_id && track.duration > 0;
  const label = ready ? "Play" : busy && row ? statusLabel(row.status) : format ? "Choose format" : "Find a copy";

  async function act() {
    if (pending) return;
    if (format && row && !ready) return store.requestsSlice.chooseFormat(row);
    setPending(true);
    try {
      if (ready) {
        if (albumQueue.includes(reference)) await store.sendSession("play_album", {
          track_ids: albumQueue, track_id: reference,
        });
        else if (typeof reference === "number") await store.sendSession("play", { track_id: reference, playlist: "all" });
        else if (row) await store.sendSession("play_request", { request_id: row.id });
        return;
      }
      setSubmitted(await store.createSongRequest(track.track_id ? { track_id: track.track_id } : {
        title: track.title, artist: track.artist, album: track.album,
        duration: track.duration, musicbrainz_id: track.musicbrainz_id, version: track.version,
      }));
    } finally {
      setPending(false);
    }
  }

  return <li className="music-profile-track" data-ranked={rank !== undefined || undefined}>
    <span className="music-profile-track-number">{rank ?? track.number ?? (track.library_only ? "Library" : "")}</span>
    {rank !== undefined && <span className="artist-cover artist-song-cover" aria-hidden="true"><CoverArt cover={track.cover} /></span>}
    <span><strong>{track.title}</strong><small>{[rank !== undefined ? track.album : track.artist,
      track.duration > 0 && time(track.duration),
      track.listens && `${numberFormat.format(track.listens)} listens`,
      track.plays && `${numberFormat.format(track.plays)} ${track.plays === 1 ? "play" : "plays"}`,
      !ready && (row?.status ?? track.copy_status) === "not_found" && statusLabel("not_found")].filter(Boolean).join(" · ")}</small></span>
    {(ready || !store.member && (canRequest || row)) && <RoomAction primary={ready} disabled={busy} aria-disabled={pending || undefined}
      aria-busy={pending} aria-label={`${label} ${track.title}`} onClick={() => void act()}>
      <span className="music-profile-action-label" data-pending={pending}>
        <span>{label}</span>{pending && <Spinner />}
      </span>
    </RoomAction>}
    {!ready && !store.member && !canRequest && !row && <small>Duration unavailable</small>}
  </li>;
}

type ReleaseTile = {
  target: Target;
  title: string;
  detail?: string;
  year?: string;
  cover?: string;
  listens?: number;
  plays?: number;
};

function catalogTile(release: Release): ReleaseTile {
  return { ...release, target: { kind: "release-group", id: release.id, title: release.title } };
}

function libraryTile(release: LibraryAlbum): ReleaseTile {
  return { ...release, target: { kind: "album", id: release.track_id, title: release.title } };
}

function ReleaseShelf({ releases, open }: { releases: ReleaseTile[]; open: (target: Target) => void }) {
  return <ul className="artist-release-shelf">{releases.map(release => (
    <li key={`${release.target.kind}:${release.target.id}`}>
      <button type="button" className="artist-release-card" onClick={() => open(release.target)}>
        <span className="artist-cover artist-release-cover" aria-hidden="true">
          <CoverArt cover={release.cover} fallback={<RoomIcon name="album" />} />
        </span>
        <strong>{release.title}</strong>
        <small>{[release.detail, release.year, release.listens && `${numberFormat.format(release.listens)} listens`,
          release.plays && `${numberFormat.format(release.plays)} ${release.plays === 1 ? "play" : "plays"}`]
          .filter(Boolean).join(" · ")}</small>
      </button>
    </li>
  ))}</ul>;
}

const releaseFilters = [
  { label: "All releases", types: [] },
  { label: "Albums", types: ["Album"] },
  { label: "Singles & EPs", types: ["Single", "EP"] },
  { label: "Compilations", types: ["Compilation"] },
];

function ArtistOverview({ profile, open }: { profile: Profile; open: (target: Target) => void }) {
  const store = useStore();
  const id = useId();
  const [highlights, setHighlights] = useState<Highlights | null>(null);
  const [showSongs, setShowSongs] = useState(false);
  const [showLibrary, setShowLibrary] = useState(false);
  const [showReleases, setShowReleases] = useState(false);
  const [filter, setFilter] = useState(releaseFilters[0]);
  const [playing, setPlaying] = useState(false);
  const localAlbums = profile.library_albums ?? [];
  const catalogReleases = profile.releases ?? [];
  const popular = highlights?.tracks ?? [];
  const popularAlbums = highlights?.albums ?? [];
  const popularReleases = highlights?.releases ?? [];
  const topPublicRelease = popularAlbums[0] ?? popularReleases[0];
  const matchedTracks = profile.tracks;
  const showingPopular = popular.length > 0 && !showLibrary;
  const visibleTracks = showingPopular ? popular : matchedTracks;
  const ready = [...new Set(matchedTracks.flatMap(track => {
    const ref = playableRef(track, requestFor(track, store.requestRows));
    return ref === undefined ? [] : [ref];
  }))];
  const playedRelease = localAlbums.find(release => !!release.plays);
  const latestRelease = catalogReleases[0];
  const featured = topPublicRelease ? catalogTile(topPublicRelease)
    : playedRelease ? libraryTile(playedRelease)
    : latestRelease ? catalogTile(latestRelease)
    : localAlbums[0] ? libraryTile(localAlbums[0]) : null;
  const featuredHeading = topPublicRelease ? (popularAlbums.length ? "Top album" : "Top release")
    : playedRelease ? "Most played release" : latestRelease ? "Latest listed release" : "From your library";
  const hasLocalRanking = matchedTracks.some(track => !!track.plays);
  const songHeading = showingPopular ? "Popular songs" : hasLocalRanking ? "Most played here" : "Songs in your library";
  const filters = releaseFilters.filter(option => !option.types.length
    || catalogReleases.some(release => option.types.includes(release.detail ?? "")));
  const filtered = filter.types.length ? catalogReleases.filter(release => filter.types.includes(release.detail ?? "")) : catalogReleases;
  const library = profile.library;
  const description = [profile.detail, profile.area, profile.description].filter(Boolean).join(" · ");

  useEffect(() => {
    if (!profile.identity) return;
    const controller = new AbortController();
    void api<Highlights>(`/api/music-profile/artist-highlights/${profile.identity}`, { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setHighlights(result); })
      .catch(() => { if (!controller.signal.aborted) setHighlights({ tracks: [], albums: [], releases: [] }); });
    return () => controller.abort();
  }, [profile.identity]);

  async function playArtist() {
    if (playing) return;
    setPlaying(true);
    try {
      await store.sendSession("play_album", { track_ids: ready });
    } finally {
      setPlaying(false);
    }
  }

  return <div className="artist-overview">
    <header className="artist-overview-hero">
      <div className="artist-identity">
        <h2>{profile.title || "Artist"}</h2>
        {description && <p>{description}</p>}
        <div className="artist-hero-actions">
          {ready.length > 0 && <RoomAction primary icon={playing ? undefined : "play"}
            aria-busy={playing} aria-disabled={playing || undefined} onClick={() => void playArtist()}>
            {playing && <Spinner />}Play artist
          </RoomAction>}
          {profile.identity && <a className="room-action quiet-button" target="_blank" rel="noreferrer"
            href={`https://musicbrainz.org/artist/${profile.identity}`}>MusicBrainz <RoomIcon name="northeast" /></a>}
        </div>
      </div>
      {(library || matchedTracks.length > 0) && <dl className="artist-library-stats">
        <div><dt>Songs in your library</dt><dd>{numberFormat.format(library?.tracks ?? matchedTracks.length)}</dd></div>
        <div><dt>Ready to play</dt><dd>{numberFormat.format(library?.ready ?? ready.length)}</dd></div>
        <div><dt>Library releases</dt><dd>{numberFormat.format(library?.albums ?? localAlbums.length)}</dd></div>
      </dl>}
    </header>
    {profile.unavailable && <p className="artist-profile-note" role="status">MusicBrainz is temporarily unavailable. Your library matches are still here.</p>}
    <div className="artist-overview-columns" data-featured={!!featured}>
      <section className="artist-overview-section" aria-labelledby={`${id}-songs`}>
        <header className="artist-section-head">
          <div>
            <h3 id={`${id}-songs`}>{songHeading}</h3>
            {showingPopular ? <p>Ranked by public ListenBrainz listens</p>
              : hasLocalRanking && <p>Ranked by completed plays</p>}
          </div>
          {popular.length > 0 && matchedTracks.length > 0 && <RoomAction onClick={() => {
            setShowLibrary(!showLibrary);
            setShowSongs(false);
          }}>{showLibrary ? "Show popular songs" : "Show library songs"}</RoomAction>}
        </header>
        {visibleTracks.length > 0 ? <ol className="music-profile-tracks" id={`${id}-song-list`}>
          {(showSongs ? visibleTracks : visibleTracks.slice(0, 5)).map((track, index) => <ProfileTrack
            key={`${track.musicbrainz_id || track.track_id}:${index}`} track={track} albumQueue={[]} rank={index + 1} />)}
        </ol> : profile.identity && highlights === null
          ? <LoadingStatus>Loading popular songs…</LoadingStatus>
          : <p className="artist-profile-note">No songs are listed for this artist yet.</p>}
        {visibleTracks.length > 5 && <div>
          <RoomAction aria-expanded={showSongs} aria-controls={`${id}-song-list`} onClick={() => setShowSongs(!showSongs)}>
            {showSongs ? "Show fewer songs" : "Show more songs"}
          </RoomAction>
        </div>}
      </section>
      {featured && <section className="artist-overview-section artist-featured" aria-labelledby={`${id}-featured`}>
        <header className="artist-section-head">
          <h3 id={`${id}-featured`}>{featuredHeading}</h3>
        </header>
        <button type="button" className="artist-featured-release" aria-label={`Open release ${featured.title}`}
          onClick={() => open(featured.target)}>
          <span className="artist-cover" aria-hidden="true"><CoverArt cover={featured.cover} fallback={<RoomIcon name="album" />} /></span>
          <span>
            <strong>{featured.title}</strong>
            <small>{[featured.detail, featured.year, featured.plays && `${numberFormat.format(featured.plays)} ${featured.plays === 1 ? "play" : "plays"}`]
              .filter(Boolean).join(" · ")}</small>
          </span>
          <RoomIcon name="forward" />
        </button>
      </section>}
    </div>
    {popularAlbums.length > 0 && <section className="artist-overview-section" aria-labelledby={`${id}-popular-albums`}>
      <header className="artist-section-head"><div>
        <h3 id={`${id}-popular-albums`}>Popular albums</h3>
        <p>Ranked by public ListenBrainz listens</p>
      </div></header>
      <ReleaseShelf releases={popularAlbums.map(catalogTile)} open={open} />
    </section>}
    {popularReleases.length > 0 && <section className="artist-overview-section" aria-labelledby={`${id}-popular-releases`}>
      <header className="artist-section-head"><h3 id={`${id}-popular-releases`}>Other popular releases</h3></header>
      <ReleaseShelf releases={popularReleases.map(catalogTile)} open={open} />
    </section>}
    {catalogReleases.length > 0 && <section className="artist-overview-section" aria-labelledby={`${id}-discography`}>
      <header className="artist-section-head">
        <h3 id={`${id}-discography`}>Discography</h3>
        {filtered.length > 6 && <RoomAction aria-expanded={showReleases} onClick={() => setShowReleases(!showReleases)}>
          {showReleases ? "Show fewer releases" : "Show all releases"}
        </RoomAction>}
      </header>
      {filters.length > 1 && <div className="filter-tabs artist-release-filters" role="group" aria-label="Release type">
        {filters.map(option => <RoomAction key={option.label} primary={filter === option} aria-pressed={filter === option}
          onClick={() => { setFilter(option); setShowReleases(false); }}>{option.label}</RoomAction>)}
      </div>}
      <ReleaseShelf releases={(showReleases ? filtered : filtered.slice(0, 6)).map(catalogTile)} open={open} />
    </section>}
    {localAlbums.length > 0 && <section className="artist-overview-section" aria-labelledby={`${id}-library`}>
      <header className="artist-section-head"><h3 id={`${id}-library`}>Albums in your library</h3></header>
      <ReleaseShelf releases={localAlbums.map(libraryTile)} open={open} />
    </section>}
    {!!profile.connections?.length && <section className="artist-overview-section" aria-labelledby={`${id}-related`}>
      <header className="artist-section-head"><h3 id={`${id}-related`}>Bands and related artists</h3></header>
      <ul className="music-profile-releases">{profile.connections.map(artist => <li key={artist.id}>
        <button type="button" className="music-profile-link" onClick={() => open({ kind: "artist", id: artist.id, title: artist.title })}>
          {artist.title}<small>{artist.detail}</small>
        </button>
      </li>)}</ul>
    </section>}
  </div>;
}

export function MusicProfile({ root, cache, resetScroll }: {
  root: Target;
  cache: Map<string, Profile>;
  resetScroll: () => void;
}) {
  const store = useStore();
  const [trail, setTrail] = useState<Target[]>([root]);
  const [result, setResult] = useState<{ path: string; profile: Profile } | null>(null);
  const [failure, setFailure] = useState<{ path: string; message: string } | null>(null);
  const [playingAlbum, setPlayingAlbum] = useState(false);
  const [retry, setRetry] = useState(0);
  const current = trail.at(-1)!;
  const path = `/api/music-profile/${current.kind}/${current.id}`;
  const profile = cache.get(path) ?? (result?.path === path ? result.profile : null);
  const message = failure?.path === path ? failure.message : "";
  const artistPage = current.kind === "artist";
  const placeholderLayout = artistPage ? "music-profile-releases" : "music-profile-tracks";
  const albumQueue = profile?.kind === "album"
    ? profile.tracks.flatMap(track => {
        const reference = playableRef(track, requestFor(track, store.requestRows));
        return reference === undefined ? [] : [reference];
      })
    : [];

  useEffect(() => {
    if (cache.has(path)) return;
    const controller = new AbortController();
    void api<Profile>(path, { signal: controller.signal })
      .then(profile => {
        if (controller.signal.aborted) return;
        if (!profile.unavailable) cache.set(path, profile);
        setResult({ path, profile });
      })
      .catch(error => {
        if (!controller.signal.aborted) setFailure({ path, message: error instanceof Error
          ? error.message : "This music profile could not load." });
      });
    return () => controller.abort();
  }, [path, retry, cache]);

  const open = (next: Target) => {
    setFailure(null);
    resetScroll();
    setTrail(previous => [...previous, next]);
  };

  return <section className="music-profile" aria-label="Music profile">
    {trail.length > 1 && <RoomAction icon="back" onClick={() => {
      setFailure(null);
      resetScroll();
      setTrail(previous => previous.slice(0, -1));
    }}>Back</RoomAction>}
    {message ? <div className="connections-panel"><p role="alert">{message}</p>
      <RoomAction onClick={() => {
        setFailure(null);
        setRetry(value => value + 1);
      }}>Try again</RoomAction></div>
      : !profile ? <>
        <header className="music-profile-head">
          <div>
            {current.title && <h2>{current.title}</h2>}
            <LoadingStatus>{artistPage ? "Loading artist…" : "Loading album…"}</LoadingStatus>
          </div>
        </header>
        <div className={placeholderLayout} aria-hidden="true">
          {Array.from({ length: 4 }, (_, index) => <div className="song-detail-placeholder" key={index}>
            <SkeletonLines count={2} />
          </div>)}
        </div>
      </>
      : profile.kind === "artist" ? <ArtistOverview key={path} profile={profile} open={open} />
      : <>
        <header className="music-profile-head">
          <div>
            <h2>{profile.title || "Album information"}</h2>
            {profile.artist_id && <button type="button" className="music-profile-byline"
              onClick={() => open({ kind: "artist", id: profile.artist_id!, title: profile.artist })}>{profile.artist}</button>}
            <p>{[profile.artist_id ? "" : profile.artist, profile.detail, profile.year, profile.area, profile.description].filter(Boolean).join(" · ")}</p>
          </div>
          {profile.identity && <a className="room-action quiet-button" target="_blank" rel="noreferrer"
            href={`https://musicbrainz.org/${profile.edition ? "release" : "release-group"}/${profile.edition || profile.identity}`}>MusicBrainz</a>}
        </header>
        {profile.unavailable && <p role="status">MusicBrainz is temporarily unavailable. Your library matches are still here.</p>}
        {!!profile.tracks.length && <section className="music-profile-section">
          <div className="music-profile-head">
            <h3>Track list</h3>
            {albumQueue.length > 1 && <RoomAction primary icon={playingAlbum ? undefined : "play"}
              aria-busy={playingAlbum} aria-disabled={playingAlbum || undefined} onClick={async () => {
                if (playingAlbum) return;
                setPlayingAlbum(true);
                try {
                  await store.sendSession("play_album", { track_ids: albumQueue });
                } finally {
                  setPlayingAlbum(false);
                }
              }}>{playingAlbum && <Spinner />}Play available tracks</RoomAction>}
          </div>
          <ol className="music-profile-tracks">{profile.tracks.map((track, index) => <ProfileTrack
            key={`${track.track_id ?? track.musicbrainz_id}:${index}`} track={track} albumQueue={albumQueue} />)}</ol>
        </section>}
        {!profile.tracks.length && <p>No tracks are listed for this album yet.</p>}
        {profile.truncated && <p>MusicBrainz lists more tracks on this edition.</p>}
      </>}
  </section>;
}
