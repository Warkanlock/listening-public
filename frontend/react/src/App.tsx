import { useEffect, useRef, useState } from "react";
import { createInputController } from "@ui-sdk/react/input";
import { Toast, ToastAction, ToastRegion, ToastTitle } from "@ui-sdk/react/toast";
import { announceAccountChange, postJson, scrollBehavior } from "./api";
import { AppearanceProvider } from "./appearance";
import {
  isDestinationFilter,
  StoreProvider,
  useStore,
  type View,
} from "./store";
import { DESTINATION_DESCRIPTORS } from "./destinations";
import { notify, toaster } from "./toasts";
import { RequestFormatPicker } from "./components/RequestFormatPicker";
import type { RoomRole } from "./components/RoomLogin";
import { ActivityDialog } from "./components/ActivityDialog";
import { DownloadUpdatesTray } from "./components/DownloadUpdatesTray";
import { RoomIcon, type RoomIconName } from "./components/RoomIcon";
import { AnnouncementsDialog } from "./components/AnnouncementsDialog";
import { ArtworkDialog } from "./components/ArtworkDialog";
import { Jams } from "./components/Jams";
import { Player } from "./components/Player";
import { QueuePanel } from "./components/QueuePanel";
import { SearchField } from "./components/SearchField";
import { ShortcutsDialog } from "./components/ShortcutsDialog";
import { SongDialog } from "./components/SongDialog";
import { LyricsStage } from "./components/LyricsStage";
import { Collection } from "./views/Collection";
import { PlaylistComposer } from "./components/PlaylistComposer";
import { PrivatePlaylists } from "./views/PrivatePlaylists";
import { DeepDive } from "./views/DeepDive";
import { PublicBrowse } from "./views/PublicBrowse";
import { Jobs } from "./views/Jobs";
import { Radio } from "./views/Radio";
import { Requests } from "./views/Requests";
import { Extensions } from "./views/Extensions";
import { Settings } from "./views/Settings";
import { MoodMap } from "./views/MoodMap";
import { ActivityMixes } from "./views/ActivityMixes";
import { MyDevices } from "./views/MyDevices";
import { Insights } from "./views/Insights";
import { Hiking } from "./views/Hiking";
import { isTripPlanningHash, setOfflineAccount } from "./hikingStorage";

const NAV: { view: View; icon: RoomIconName; label: string }[] = [
  { view: "all", icon: "library", label: "Library" },
  { view: "discover", icon: "discover", label: "Deep Dive" },
  { view: "radio", icon: "radio", label: "Radio" },
  { view: "moods", icon: "moods", label: "Mood map" },
  { view: "activities", icon: "listen", label: "Activities" },
  { view: "insights", icon: "insights", label: "Insights" },
  { view: "hiking", icon: "camping", label: "Camping mode" },
  { view: "browse", icon: "catalogs", label: "Browse catalogs" },
  { view: "private-playlists", icon: "playlists", label: "My playlists" },
  { view: "my-devices", icon: "devices", label: "My devices" },
  { view: "requests", icon: "requests", label: "Requests" },
  { view: "jobs", icon: "jobs", label: "Jobs" },
  { view: "extensions", icon: "extensions", label: "Extensions" },
  { view: "settings", icon: "settings", label: "Settings" },
];

function Sidebar({ named }: { named: boolean }) {
  const store = useStore();
  const [navOpen, setNavOpen] = useState(false);
  const sidebar = useRef<HTMLElement>(null);
  const navButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!navOpen) return;
    const closeOutside = (event: PointerEvent) => {
      if (event.target instanceof Node && !sidebar.current?.contains(event.target)) {
        setNavOpen(false);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setNavOpen(false);
      navButton.current?.focus({ preventScroll: true });
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [navOpen]);
  const destinations = {
    favorites: () => store.showFavorites(),
    recent: () => store.showRecentlyPlayed(),
    most: () => store.showMostPlayed(),
  };
  function navLink(item: typeof NAV[number]) {
    const active = item.view === store.view && (item.view !== "all" ||
      (store.playlist === "all" && !isDestinationFilter(store.filter)));
    return (
      <button key={item.view} aria-label={item.label}
        aria-description={item.view === "requests" && store.requestsBadge > 0
          ? `${store.requestsBadge} pending` : undefined}
        className={`nav-link${active ? " is-active" : ""}`} type="button"
        aria-current={active ? "page" : undefined}
        onClick={() => {
          if (item.view === "all") store.clearSearch();
          store.setView(item.view);
        }}>
        <span className="nav-icon"><RoomIcon name={item.icon} /></span>
        {item.label}
        {item.view === "requests" && store.requestsBadge > 0 &&
          <span className="nav-badge">{store.requestsBadge}</span>}
      </button>
    );
  }
  return (
    <aside ref={sidebar} className="sidebar" onBlur={event => {
      if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) setNavOpen(false);
    }}>
      <div className="brand" aria-label="Listening">
        <span className="brand-mark" aria-hidden="true">
          <i />
          <i />
          <i />
          <i />
        </span>
        <span>
          listening<span className="brand-dot">.</span>
        </span>
      </div>
      <button ref={navButton} className="mobile-menu" type="button" aria-expanded={navOpen}
        aria-controls="room-navigation" onClick={() => setNavOpen(!navOpen)}>
        Menu <span><RoomIcon name={navOpen ? "close" : "menu"} /></span>
      </button>
      <nav id="room-navigation" className="primary-nav" aria-label="Main" data-open={navOpen}
        onClick={() => {
          if (navOpen) {
            setNavOpen(false);
            navButton.current?.focus({ preventScroll: true });
          }
        }}>
        <div className="nav-core">{(store.member ? NAV.slice(0, 3).filter(item => item.view !== "discover") : NAV.slice(0, 3)).map(navLink)}</div>
        <div className="nav-overflow">
          {NAV.slice(3).filter((item) => {
            if (item.view === "private-playlists" || item.view === "my-devices") return named;
            return !store.member || item.view === "extensions" || item.view === "browse" || item.view === "moods" || item.view === "activities" || item.view === "hiking" || item.view === "insights" || item.view === "settings";
          }).map(navLink)}
          {(Object.keys(destinations) as (keyof typeof destinations)[]).map((key) => {
            const active = store.view === "all" && store.filter === key;
            const item = DESTINATION_DESCRIPTORS[key];
            return (
              <button key={key} type="button"
                className={`nav-link listening-link${active ? " is-active" : ""}`}
                aria-current={active ? "page" : undefined}
                onClick={destinations[key]}>
                <span className="nav-icon"><RoomIcon name={key} /></span>
                {item.navLabel}
              </button>
            );
          })}
        </div>
      </nav>
    </aside>
  );
}

function Topbar({
  controller,
  role,
}: {
  controller: ReturnType<typeof createInputController>;
  role: RoomRole;
}) {
  const store = useStore();
  const unread = store.unreadAnnouncements;
  return (
    <header className="topbar">
      <SearchField
        controller={controller}
        value={store.searchText}
        onValueChange={(value) => store.typeSearch(value)}
        onSearch={() => void store.searchExternal()}
        catalogSearch={!store.member}
      />
      <div className="topbar-actions">
        {(role === "owner" || role === "member") && <Jams />}
        {!store.member && <ActivityBar />}
        {!store.member && <button
          className="announcements-button"
          type="button"
          aria-label={
            unread ? `Announcements, ${unread} unread` : "Announcements"
          }
          title="Announcements"
          onClick={() => store.setAnnouncementsOpen(true)}
        >
          <RoomIcon name="announcements" />
          {unread ? (
            <span className="announcements-badge">
              {unread > 9 ? "9+" : unread}
            </span>
          ) : null}
        </button>}
        <button
          className="shortcuts-button"
          type="button"
          aria-label="Keyboard shortcuts"
          title="Keyboard shortcuts"
          onClick={() => store.setShortcutsOpen(true)}
        >
          <RoomIcon name="help" />
        </button>
        {role === "listener" && <a className="quiet-button" href="/?signin=1">Sign in</a>}
        {role === "owner" && store.sessionSlice.personalQueue && <button type="button" className="quiet-button" onClick={() => {
          store.sessionSlice.selectQueue(false);
          void store.pollSession();
        }}>Use shared queue</button>}
        {(role === "member" || role === "owner") && <button type="button" className="quiet-button" onClick={async () => {
          try {
            await postJson("/api/account/logout", {});
            setOfflineAccount("");
            announceAccountChange();
            location.reload();
          } catch {
            notify("Couldn't sign out. Try again.");
          }
        }}>Sign out</button>}
      </div>
    </header>
  );
}

function ActivityBar() {
  const store = useStore();
  const activity = store.activity;
  const text = store.topbarFailed ? "Library status unavailable"
    : store.activityFailed ? "Downloader status unavailable"
    : activity?.running ? activity.status : "Download activity";
  return (
    <button
      className={`activity-bar${activity && !activity.running ? " is-idle" : ""}`}
      type="button"
      title="Explore download activity"
      aria-haspopup="dialog"
      aria-expanded={store.activityDialogOpen}
      onClick={() => store.setActivityDialogOpen(true)}
    >
      <RoomIcon name="download" />
      {activity?.running && <span className="activity-pulse" aria-hidden="true" />}
      <span className="activity-text">{text}</span>
    </button>
  );
}

function RoomRecovery({ role }: { role: RoomRole }) {
  const store = useStore();
  const state = store.roomConnection;
  if (state === "ready") return null;
  const pairing = state === "pairing_required";
  const signIn = pairing && (role === "owner" || role === "member");
  const loading = state === "catalog_loading";
  const connecting = state === "connecting";
  return (
    <section className="room-recovery" aria-labelledby="room-connection-heading">
      <h1 id="room-connection-heading">
        {signIn ? "Sign-in expired" : pairing ? "Pairing expired" : loading ? "Catalog is still loading" :
          connecting ? "Connecting to the room…" : "Room unreachable"}
      </h1>
      <p role="status">
        {signIn ? "Sign in again to continue listening." :
          pairing ? "Open a fresh invitation from Settings → Devices on a paired admin device." :
          loading ? "The server is building its catalog. This page will update when it’s ready." :
          connecting ? "Checking the connection to your server." :
          "Check your private network connection, then try again."}
      </p>
      {!connecting && <button type="button" className="quiet-button"
        onClick={() => signIn ? location.reload() : pairing ? location.assign("/?pair=1") : void store.checkRoom()}>
        {signIn ? "Sign in again" : pairing ? "Pair again" : "Retry"}
      </button>}
    </section>
  );
}

function Shell({ role }: { role: RoomRole }) {
  const store = useStore();
  const named = role === "owner" || role === "member";
  const [controller] = useState(createInputController);
  const mainRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const openLinkedView = () => {
      if (isTripPlanningHash(location.hash)) store.setView("hiking");
      if (location.hash === "#extensions") store.setView("extensions");
      if (location.hash === "#insights") store.setView("insights");
    };
    openLinkedView();
    window.addEventListener("hashchange", openLinkedView);
    return () => window.removeEventListener("hashchange", openLinkedView);
  }, [store]);

  useEffect(() => {
    if (matchMedia("(max-width: 650px)").matches) {
      window.scrollTo({ top: 0, behavior: scrollBehavior() });
    } else {
      mainRef.current?.scrollTo({ top: 0, behavior: scrollBehavior() });
    }
  }, [store.view, store.playlist]);

  // Shortcut handling (parity item 9): one keydown handler with the
  // vanilla guards. Cmd/Ctrl-K always focuses search; the rest skip
  // when focus is in a control, a popup or dialog is open, or the
  // shortcuts dialog itself is showing.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (
        (event.metaKey || event.ctrlKey) &&
        event.key.toLowerCase() === "k"
      ) {
        event.preventDefault();
        controller.focus();
        return;
      }
      const target = event.target as HTMLElement | null;
      const inPopup = !!target?.closest?.(
        '[data-slot="menu-content"], [data-slot="select-content"], [data-slot="dialog-content"]',
      );
      const tag = document.activeElement?.tagName || "";
      const inControl =
        inPopup ||
        store.shortcutsOpen ||
        ["INPUT", "TEXTAREA", "BUTTON", "SELECT"].includes(tag);
      if (
        event.code === "Space" &&
        !inControl &&
        !event.metaKey &&
        !event.ctrlKey
      ) {
        event.preventDefault();
        // Space toggles whatever is sounding on this device
        // (plan 059): radio when radio is up, the session otherwise.
        if (store.radioSounding) store.toggleRadio();
        else store.togglePlayback();
      }
      if (!inControl &&
          !event.metaKey && !event.ctrlKey && !event.altKey) {
        if (event.key.toLowerCase() === "q") {
          event.preventDefault();
          store.setQueueOpen(!store.queueOpen);
        }
        if (event.key === "?") {
          event.preventDefault();
          store.setShortcutsOpen(true);
        }
        if (event.key === "ArrowRight") {
          event.preventDefault();
          if (store.radioSounding) store.nextRadio();
          else void store.sendSession("next");
        }
        if (event.key === "ArrowLeft") {
          event.preventDefault();
          // Radio's mix has no previous in its control set; the
          // session's previous stays the session's while it sounds.
          if (!store.radioSounding) void store.sendSession("previous");
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [store, controller]);

  const { downloadTray, formatPicker } = store.requestsSlice;
  const picker = formatPicker && <RequestFormatPicker key={`${formatPicker.id}/${formatPicker.format_revision}`} row={formatPicker} />;
  return (
    <PlaylistComposer named={named}><div className={`app-shell${store.queueOpen ? " queue-visible" : ""}`}>
      <Sidebar named={named} />
      <main className="main-area" ref={mainRef}>
        {store.view !== "hiking" && <RoomRecovery role={role} />}
        <div className="room-content" hidden={store.roomConnection !== "ready"}>
          <Topbar controller={controller} role={role} />
          {store.view === "all" ? <Collection /> : null}
          {store.view === "requests" ? <Requests /> : null}
          {store.view === "discover" ? <DeepDive /> : null}
          {store.view === "browse" ? <PublicBrowse /> : null}
          {store.view === "moods" ? <MoodMap /> : null}
          {store.view === "activities" ? <ActivityMixes /> : null}
          {store.view === "my-devices" && named ? <MyDevices owner={role === "owner"} /> : null}
          {store.view === "private-playlists" && named ? <PrivatePlaylists owner={role === "owner"} /> : null}
          {/* Radio stays mounted once a station has started, so its
              player (and sound) survives navigation (plan 059). */}
          {store.view === "radio" || store.radioStarted ? <Radio /> : null}
          {store.view === "jobs" ? <Jobs /> : null}
          {store.view === "extensions" && <Extensions />}
          {store.view === "settings" ? <Settings /> : null}
        </div>
        {store.view === "hiking" && <Hiking ready={store.roomConnection === "ready"} />}
        {store.view === "insights" && (store.roomConnection === "ready" || store.session) &&
          <Insights connected={store.roomConnection === "ready"} />}
      </main>
      <Player />
      <QueuePanel />
      <ToastRegion toaster={toaster}>
        {toast => toast.action ? <Toast className="download-updates-toast">
          <ToastTitle className="visually-hidden" />
          <ToastAction aria-haspopup="dialog">{toast.action.label}<RoomIcon name="chevronRight" /></ToastAction>
        </Toast> : <Toast><ToastTitle /></Toast>}
      </ToastRegion>
      {downloadTray ? <DownloadUpdatesTray tray={downloadTray}>{picker}</DownloadUpdatesTray> : picker}
      <ShortcutsDialog />
      <AnnouncementsDialog />
      <SongDialog />
      <LyricsStage />
      <ActivityDialog />
      <ArtworkDialog />
    </div></PlaylistComposer>
  );
}

export function App({ role = "paired", appearanceAccount = null }: { role?: RoomRole; appearanceAccount?: string | null }) {
  return (
    <AppearanceProvider key={appearanceAccount ?? "paired"} account={appearanceAccount}>
      <StoreProvider member={role === "member"}>
        <Shell role={role} />
      </StoreProvider>
    </AppearanceProvider>
  );
}
