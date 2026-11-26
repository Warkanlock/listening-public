import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Dialog, DialogBackdrop, DialogClose, DialogTitle } from "@ui-sdk/react/dialog";
import { api, ApiError } from "../api";
import { CoverArt } from "../components/Artwork";
import { MusicProfile, type Profile } from "../components/MusicProfile";
import { RoomAction } from "../components/RoomControls";
import { RoomIcon } from "../components/RoomIcon";
import { LoadingStatus } from "../components/StateCard";
import { RoomDialogContent } from "../components/WindowContent";

type Release = {
  id: string;
  title: string;
  artist: string;
  seed_artist: string;
  kind: "Album" | "EP" | "Single";
  date: string;
  cover: string;
};

type RadarFeed = {
  state: "building" | "waiting" | "ready" | "empty" | "unavailable" | "stale";
  refreshing: boolean;
  updated_at: string | null;
  releases: Release[];
};

const releaseDate = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", timeZone: "UTC" });

export function ReleaseRadar({ active }: { active: boolean }) {
  const [feed, setFeed] = useState<RadarFeed>();
  const [failure, setFailure] = useState<"unsupported" | "unavailable">();
  const [retry, setRetry] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const [selected, setSelected] = useState<Release>();
  const cache = useRef(new Map<string, Profile>());
  const dialog = useRef<HTMLDivElement>(null);
  const launcher = useRef<HTMLButtonElement>(null);

  useLayoutEffect(() => {
    if (!selected && launcher.current) {
      if (launcher.current.isConnected) launcher.current.focus();
      launcher.current = null;
    }
  }, [selected]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    let timer: number | undefined;
    async function load() {
      try {
        const next = await api<RadarFeed>("/api/discover/releases", { signal: controller.signal, cache: "no-store" });
        if (controller.signal.aborted) return;
        setFeed(next);
        setFailure(undefined);
        if (next.refreshing) timer = window.setTimeout(() => void load(), 3000);
      } catch (error) {
        if (controller.signal.aborted) return;
        setFailure(error instanceof ApiError && (error.status === 404 || error.status === 403)
          ? "unsupported" : "unavailable");
      }
    }
    void load();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [active, retry]);

  if (failure === "unsupported") return null;
  const releases = feed?.releases ?? [];
  const stale = feed?.state === "stale";
  const available = !failure && !stale && feed?.state !== "unavailable";
  return <section className="release-radar" aria-label="Release Radar">
    <header className="release-radar-head"><h2>Release Radar</h2></header>
    {available && (!feed || feed.state === "building") && <LoadingStatus>Checking recent releases…</LoadingStatus>}
    {feed?.state === "waiting" && <p>Play a few songs to see recent releases from their artists.</p>}
    {feed?.state === "empty" && <p>No recent releases from your listening artists.</p>}
    {(failure || feed?.state === "unavailable" || stale) && <p role="status">
      {feed?.refreshing ? "Refreshing recent releases…" : releases.length ? "Showing the last successful update."
        : "Recent releases are temporarily unavailable."}
      {stale && feed.updated_at && <>{" "}Updated <time dateTime={feed.updated_at}>
        {releaseDate.format(new Date(feed.updated_at))}</time>.</>}
      {failure && <>{" "}<RoomAction icon="refresh" onClick={() => setRetry(value => value + 1)}>Try again</RoomAction></>}
    </p>}
    {releases.length > 0 && <ul className="release-radar-grid">
      {releases.slice(0, expanded ? releases.length : 6).map(release => <li key={release.id}>
        <button className="discovery-recording release-radar-card" type="button" onClick={event => {
          launcher.current = event.currentTarget;
          setSelected(release);
        }}>
          <span className="release-radar-art" aria-hidden="true"><CoverArt cover={release.cover} /></span>
          <span className="discovery-recording-info">
            <strong>{release.title}</strong>
            <small>{release.artist} · {release.kind} · <time dateTime={release.date}>
              {releaseDate.format(new Date(release.date))}</time></small>
            <small>Because you played {release.seed_artist}</small>
          </span>
          <RoomIcon name="chevronRight" />
        </button>
      </li>)}
    </ul>}
    {releases.length > 6 && <RoomAction icon={expanded ? "chevronUp" : "chevronDown"} aria-expanded={expanded}
      onClick={() => setExpanded(value => !value)}>
      {expanded ? "Show fewer releases" : `Show all ${releases.length} releases`}
    </RoomAction>}
    {selected && <Dialog open onOpenChange={open => { if (!open) setSelected(undefined); }}>
      <DialogBackdrop />
      <RoomDialogContent className="connections-dialog" contentRef={dialog}>
        <DialogTitle className="visually-hidden">{selected.title}</DialogTitle>
        <DialogClose className="dialog-close" aria-label="Close album">
          <RoomIcon name="close" />
        </DialogClose>
        <MusicProfile key={selected.id} root={{ kind: "release-group", id: selected.id, title: selected.title }}
          cache={cache.current} resetScroll={() => dialog.current?.scrollTo(0, 0)} />
      </RoomDialogContent>
    </Dialog>}
  </section>;
}
