import { RoomIcon, RoomIconButton, type RoomIconName } from "./RoomIcon";
import { RoomAction } from "./RoomControls";
import { Tabs, TabsList, TabsPanel, TabsTab } from "@ui-sdk/react/tabs";
import { RoomDialogContent } from "./WindowContent";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import {
  Dialog,
  DialogBackdrop,
  DialogClose,
  DialogTitle,
} from "@ui-sdk/react/dialog";
import {
  Combobox,
  ComboboxContent,
  ComboboxEmpty,
  ComboboxInput,
  ComboboxItem,
  ComboboxLabel,
  ComboboxList,
} from "@ui-sdk/react/combobox";
import { api, isHost, postJson, type SongMetadata } from "../api";
import { canOpenMusicProfile, type ConnectionGraph, type SongInfoMode } from "../artistConnections";
import { lyricsNote, useLyrics } from "../lyrics";
import { notify } from "../toasts";
import { useStore } from "../store";
import { Artwork, type ArtworkSubject } from "./Artwork";
import { MusicProfile, type Profile } from "./MusicProfile";
import { ArtistConnections } from "./ArtistConnections";
import { FavoriteButton } from "./FavoriteButton";
import { LoadingStatus, SkeletonLines, Spinner } from "./StateCard";
import { YouTubeLink } from "./YouTubeLink";

// The song dialog (item 32): release details, the host's genre
// correction (a combobox over the canonical facets since plan 049,
// free entry still allowed), a compact lyrics row that opens the
// lyrics stage, Song DNA connections that reopen the dialog on
// the connected recording, and the external links and share action.
export function SongDialog() {
  const store = useStore();
  const id = store.songInfoId;
  const mode = store.songInfoMode;
  const [metadata, setMetadata] = useState<{ id: NonNullable<typeof id>; song: SongMetadata } | null>(null);
  const data = metadata && metadata.id === id ? metadata.song : null;
  const [failure, setFailure] = useState<{ id: typeof id; message: string } | null>(null);
  const [attempt, retryDetails] = useState(0);
  const [savingGenre, setSavingGenre] = useState<typeof id>(null);
  const error = failure?.id === id ? failure?.message : null;
  const creditsCache = useMemo(() => new Map<string, ConnectionGraph>(), [id]);
  const profileCache = useMemo(() => new Map<string, Profile>(), [id]);
  const [genreInput, setGenreInput] = useState("");
  const [genreField, setGenreField] = useState<HTMLDivElement | null>(null);
  const [shareUrl, setShareUrl] = useState<string | null>(null);
  const sequence = useRef(0);
  const detailsTab = useRef<HTMLButtonElement>(null);
  const artistTab = useRef<HTMLButtonElement>(null);
  const albumTracksTab = useRef<HTMLButtonElement>(null);
  const dialogContent = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    dialogContent.current?.scrollTo(0, 0);
  }, [id, mode]);
  const { outcome: lyricsOutcome } = useLyrics(
    typeof id === "number" && mode === "details" ? id : null,
  );

  const currentTrack = store.session?.current;
  const known = (currentTrack?.id === id ? currentTrack : undefined) ??
    (typeof id === "number" ? store.tracks.find(track => track.id === id)
      : store.songInfoTrack ?? store.discover?.tracks.find(track => track.musicbrainz_id === id));
  const profiling = mode === "artist-profile" || mode === "album-tracks";
  const needMetadata = !(typeof id === "string" && canOpenMusicProfile(id)) && !profiling && (mode === "details" || !known || (typeof id === "number" && !known.album));

  useEffect(() => {
    const current = ++sequence.current;
    setFailure(null);
    setShareUrl(null);
    if (id === null) setMetadata(null);
    if (id === null || !needMetadata || data) return;
    const numeric = typeof id === "number";
    const path = numeric
      ? `/api/metadata/${id}`
      : `/api/metadata/recording/${encodeURIComponent(id)}`;
    const controller = new AbortController();
    api<SongMetadata>(path, { signal: controller.signal })
      .then((result) => {
        if (sequence.current !== current) return;
        setMetadata({ id, song: result });
        setGenreInput(result.genre || "");
      })
      .catch((fetchError) => {
        if (sequence.current !== current) return;
        setFailure({ id, message: fetchError instanceof Error
          ? fetchError.message : "Song information could not load" });
      });
    return () => {
      sequence.current++;
      controller.abort();
    };
  }, [id, needMetadata, attempt]);

  if (id === null) return null;
  const numeric = typeof id === "number";
  // The dialog reports only what was found (plan 049): the lines
  // themselves render in the lyrics stage, never in the dialog.
  const lyrics =
    lyricsOutcome === null
      ? null
      : {
          synced: lyricsOutcome.kind === "synced",
          note: lyricsNote(lyricsOutcome),
        };
  const showYouTube = numeric && known && "status" in known && known.status !== "verified";
  const title = data?.title || known?.title || (profiling ? "Music profile" : "Loading song");
  const album = data?.album || known?.album;
  const artistName = data?.artist || known?.artist;
  const artist = artistName || (profiling ? "" : "Please wait");
  const subject: ArtworkSubject = {
    title,
    artist,
    album: album || "",
    cover: (known && "cover" in known ? known.cover : "") || "",
    seed: (known && "art_seed" in known ? known.art_seed : undefined) ?? id,
  };

  const details: [string, string][] = [];
  if (data) {
    if (data.first_release) details.push(["First release", data.first_release]);
    if (data.isrc) details.push(["ISRC", data.isrc]);
    if (data.genre) {
      details.push(["Genre", `${data.genre} (${data.genre_source_label || "stored"})`]);
    } else if (data.genres?.length) {
      details.push(["Genres", data.genres.join(", ")]);
    }
    if (data.provenance) details.push(["Source", data.provenance]);
    if (data.lists?.length) details.push(["In lists", data.lists.join(", ")]);
    if (data.identifier) details.push(["Identifier", data.identifier]);
    if (data.source) details.push(["Catalog", data.source]);
  }

  const submitGenre = async (event: React.FormEvent) => {
    event.preventDefault();
    if (savingGenre === id) return;
    setSavingGenre(id);
    try {
      const updated = await postJson<Pick<SongMetadata, "genre" | "genre_source_label">>(
        "/api/genre", { track_id: id, genre: genreInput.trim() },
      );
      setMetadata((current) => current?.id === id
        ? { id, song: { ...current.song, ...updated } }
        : current);
      if (store.songInfoId === id) setGenreInput(updated.genre || "");
      notify("Genre saved");
      void store.loadTracks(false);
    } catch (saveError) {
      notify(saveError instanceof Error ? saveError.message : "Genre could not be saved");
    } finally {
      setSavingGenre(current => current === id ? null : current);
    }
  };

  const share = async () => {
    if (!data?.spotify_url) return;
    const payload = { title: data.title, text: `${data.title} by ${data.artist}`, url: data.spotify_url };
    try {
      if (navigator.share) {
        await navigator.share(payload);
        return;
      }
      throw new Error("share unavailable");
    } catch {
      try {
        await navigator.clipboard.writeText(data.spotify_url);
        notify("Link copied");
      } catch {
        setShareUrl(data.spotify_url);
      }
    }
  };

  const exploring = mode !== "details";
  const hasMusicProfile = canOpenMusicProfile(id);
  const canExplore = hasMusicProfile || /^[0-9a-f-]{36}$/.test(String(id));
  const tabRefs: Partial<Record<SongInfoMode, RefObject<HTMLButtonElement | null>>> = {
    details: detailsTab, "artist-profile": artistTab, "album-tracks": albumTracksTab,
  };
  const views: { value: SongInfoMode; label: string; icon: RoomIconName }[] = [
    ...(!numeric && hasMusicProfile ? [] : [
      { value: "details" as const, label: "Details", icon: "info" as const },
      { value: "song" as const, label: "Credits", icon: "discover" as const },
    ]),
  ];
  if (hasMusicProfile) views.push({ value: "artist-profile", label: "Artist", icon: "music" });
  if (hasMusicProfile && (album || mode === "album-tracks" || !numeric)) {
    views.push({ value: "album-tracks", label: "Track list", icon: "album" });
  }
  if (numeric && (album || mode === "album")) views.push({ value: "album", label: "Album", icon: "album" });

  const selectMode = (next: string | null, focusTab = false) => {
    const selected = views.find(({ value }) => value === next);
    if (!selected || selected.value === mode) return;
    if (focusTab) tabRefs[selected.value]?.current?.focus();
    store.openSongInfo(id, selected.value);
  };
  const content = profiling ? <MusicProfile key={`${mode}:${id}`}
    root={{ kind: mode === "artist-profile" ? "artist" : "album", id, title: mode === "artist-profile" ? known?.artist : album }}
    cache={profileCache} resetScroll={() => dialogContent.current?.scrollTo(0, 0)} />
    : exploring ? (
    <ArtistConnections
      key={`${mode}:${id}`}
      target={typeof id === "number" ? { kind: mode === "album" ? "album" : "song", id } : { kind: "recording", id }}
      track={{ title: data?.title || known?.title || "", artist: data?.artist || known?.artist || "", album: album || "" }}
      artistId={known && "artist_id" in known ? known.artist_id : undefined}
      cache={creditsCache}
      showDetails={() => selectMode("details", true)}
      title={mode === "album" ? album || "Album connections" : title === "Loading song" ? "Recording connections" : title}
    />
  ) : (
    <>
      {!data || error ? (
        <section className="song-info-loading">
          {error ? (
            <div className="connections-panel">
              <p role="alert">{error}</p>
              <RoomAction onClick={() => {
                setFailure(null);
                retryDetails(value => value + 1);
              }}>Try again</RoomAction>
            </div>
          ) : (
            <>
              <LoadingStatus>Loading recording details…</LoadingStatus>
              <div className="song-details-grid" aria-hidden="true">
                <div className="song-detail-sections">
                  {Array.from({ length: 5 }, (_, index) => <div className="song-detail-placeholder" key={index}>
                    <SkeletonLines count={2} />
                  </div>)}
                </div>
                <div className="song-detail-sections song-detail-placeholder"><SkeletonLines /></div>
              </div>
            </>
          )}
        </section>
      ) : (
        <div className="song-details-grid">
          <div className="song-detail-sections">
            <section className="song-detail-section" aria-labelledby="recording-details-heading">
              <h3 id="recording-details-heading">Recording details</h3>
              {data.album || details.length > 0 ? (
                <dl className="song-facts">
                  {data.album && <div>
                    <dt className="connections-type">Album</dt>
                    <dd>{hasMusicProfile ? <button type="button" className="music-profile-byline"
                      aria-label={`Open album ${data.album}`} onClick={() => selectMode("album-tracks", true)}>{data.album}</button> : data.album}</dd>
                  </div>}
                  {details.map(([label, value]) => (
                    <div key={label}>
                      <dt className="connections-type">{label}</dt>
                      <dd>{value}</dd>
                    </div>
                  ))}
                </dl>
              ) : <p>No release details are available for this recording.</p>}
            </section>
            {data.connections?.length ? (
              <section className="song-detail-section" aria-labelledby="song-dna-heading">
                <h3 id="song-dna-heading">Song DNA</h3>
                <div className="dna-list">
                  {data.connections.map((connection) => (
                    <button
                      key={connection.musicbrainz_id}
                      className="dna-item"
                      type="button"
                      onClick={() => store.openSongInfo(connection)}
                    >
                      <span className="connections-type">{connection.relationship}</span>
                      <strong>{connection.title}</strong>
                      <small>{connection.artist}</small>
                    </button>
                  ))}
                </div>
              </section>
            ) : null}
          </div>
          <div className="song-detail-sections">
            {numeric && isHost(store.settings) ? (
              <form className="song-detail-section genre-form" onSubmit={submitGenre}>
                <Combobox
                  allowCustomValue
                  autoHighlight
                  items={store.genreOptions}
                  itemToLabel={(genre) => genre}
                  onValueChange={(next) => setGenreInput(next ?? "")}
                  query={genreInput}
                  onQueryChange={setGenreInput}
                >
                  <ComboboxLabel className="connections-type">Genre</ComboboxLabel>
                  <div className="genre-combo-row">
                    <div className="genre-combo-field" ref={setGenreField}>
                      <ComboboxInput
                        placeholder="Electronic, jazz, ambient…"
                        autoComplete="off"
                        spellCheck={false}
                      />
                    </div>
                    <button className="primary-action icon-control" type="submit" aria-label="Save genre"
                      title="Save genre" aria-busy={savingGenre === id} aria-disabled={savingGenre === id || undefined}>
                      {savingGenre === id ? <Spinner /> : <RoomIcon name="check" />}
                    </button>
                  </div>
                  <ComboboxContent className="song-genre-menu" anchor={genreField}>
                    <ComboboxEmpty>
                      {(state) =>
                        state.empty && !state.loading
                          ? store.genreOptions.length
                            ? "No genres match"
                            : "No genres available yet"
                          : null
                      }
                    </ComboboxEmpty>
                    <ComboboxList>
                      {(state) =>
                        (state.items as string[]).map((genre) => (
                          <ComboboxItem key={genre} value={genre} label={genre}>
                            {genre}
                          </ComboboxItem>
                        ))
                      }
                    </ComboboxList>
                  </ComboboxContent>
                </Combobox>
                <p className="form-help">Automatic categorization keeps your correction.</p>
              </form>
            ) : null}
            {numeric ? (
              <section className="song-detail-section" aria-labelledby="song-lyrics-heading">
                <h3 id="song-lyrics-heading">Lyrics</h3>
                {lyrics === null ? (
                  <LoadingStatus>Looking for lyrics…</LoadingStatus>
                ) : lyrics.note ? (
                  <p>{lyrics.note}</p>
                ) : (
                  <div className="lyrics-row">
                    <span>
                      {lyrics.synced ? "Synced lyrics" : "Plain lyrics"}
                    </span>
                    <RoomAction icon="lyrics"
                      onClick={() =>
                        store.openLyrics({
                          id,
                          title,
                          artist,
                          cover: subject.cover || "",
                        })
                      }
                    >
                      Show lyrics
                    </RoomAction>
                  </div>
                )}
              </section>
            ) : null}
            <section className="song-detail-section" aria-labelledby="song-links-heading">
              <h3 id="song-links-heading">Links</h3>
              <div className="external-links">
                {data.musicbrainz_url ? (
                  <a className="room-action quiet-button" href={data.musicbrainz_url} target="_blank" rel="noreferrer">
                    MusicBrainz <RoomIcon name="northeast" />
                  </a>
                ) : null}
                <a
                  className="room-action quiet-button"
                  href={`https://www.whosampled.com/search/?q=${encodeURIComponent(`${data.artist} ${data.title}`)}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  WhoSampled <RoomIcon name="northeast" />
                </a>
                {data.spotify_url ? (
                  <RoomIconButton icon="share" label="Share" onClick={share} />
                ) : null}
                {shareUrl && (
                  <a className="room-action quiet-button" href={shareUrl} target="_blank" rel="noreferrer">
                    Open the share link <RoomIcon name="northeast" />
                  </a>
                )}
                {showYouTube && <YouTubeLink artist={known.artist} title={known.title} />}
              </div>
            </section>
          </div>
        </div>
      )}
    </>
  );

  return (
    <Dialog open onOpenChange={(open) => {
      if (!open) {
        sequence.current += 1;
        store.closeSongInfo();
      }
    }}>
      <DialogBackdrop />
      <RoomDialogContent className="connections-dialog" contentRef={dialogContent}>
        <DialogClose className="dialog-close" aria-label="Close song information" title="Close song information">
          <RoomIcon name="close" />
        </DialogClose>
        <header className="song-dialog-head" data-compact={mode === "artist-profile" || undefined}>
          {(data || known) && (
            <button className="art-medium-button" type="button" aria-label="Expand artwork" title="Expand artwork"
              onClick={(event) => store.openArtwork(subject, event.currentTarget)}>
              <Artwork subject={subject} />
            </button>
          )}
          <div className="song-dialog-identity">
            <DialogTitle className="song-dialog-title">{title}</DialogTitle>
            <p className="song-dialog-artist">{artistName && hasMusicProfile && mode !== "artist-profile"
              ? <button type="button" className="music-profile-byline" aria-label={`Open artist ${artistName}`}
                  onClick={() => selectMode("artist-profile", true)}>{artistName}</button>
              : artist}</p>
          </div>
          {numeric && (data || known) && (
            <FavoriteButton track={{
              id,
              title,
              favorite: (known && "favorite" in known ? known.favorite : undefined) ?? data?.favorite ?? false,
            }} />
          )}
        </header>
        {canExplore ? (
          <Tabs value={mode} onValueChange={(next) => selectMode(next)}>
            <TabsList className="results-tabs song-connection-actions" aria-label="Song views">
              {views.map(({ value, icon, label }) => (
                <TabsTab key={value} value={value} ref={tabRefs[value]}>
                  <RoomIcon name={icon} />{label}
                </TabsTab>
              ))}
            </TabsList>
            {views.map(({ value }) => (
              <TabsPanel key={value} value={value}>{value === mode ? content : null}</TabsPanel>
            ))}
          </Tabs>
        ) : content}
        {!data && showYouTube && (
          <div className="song-fallback-links">
            <YouTubeLink artist={known.artist} title={known.title} />
          </div>
        )}
      </RoomDialogContent>
    </Dialog>
  );
}
