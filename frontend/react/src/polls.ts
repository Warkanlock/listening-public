import type { Store } from "./store";

// Item 1: the POLLS registry. Ten polls at the vanilla cadences;
// hidden tabs skip non-playback ticks, and every poll ticks once when the tab
// becomes visible again. Gates reproduce plan 015's: the explorer
// only runs while its dialog is open, process and spotify only
// while the Settings view is showing, and jobs (item 28) only
// while the Jobs view is open.
export interface Poll {
  name: string;
  ms: number;
  gate?: (store: Store) => boolean;
  foregroundMs?: (store: Store) => number;
  backgroundMs?: (store: Store) => number;
  run: (store: Store) => Promise<unknown> | unknown;
}

export const POLLS: Poll[] = [
  { name: "overview", ms: 15000, run: (store) => store.refreshOverview() },
  { name: "requests", ms: 3000, gate: (store) => !store.member,
    foregroundMs: (store) => store.view === "requests" && store.requestRows.some(
      row => row.status === "searching" || row.status === "queued" && !row.retry_at) ? 3000 : 15000,
    run: (store) => store.loadRequests() },
  { name: "radio-history", ms: 10000, gate: store => store.view === "radio" || store.radioStatus === "paused",
    run: store => store.radioSlice.refreshHistory() },
  { name: "activity", ms: 5000, gate: (store) => !store.member, run: (store) => store.refreshActivity() },
  {
    name: "activity-explorer",
    ms: 5000,
    gate: (store) => store.activityDialogOpen,
    run: (store) => store.refreshDownloadExplorer(),
  },
  {
    name: "session",
    ms: 1800,
    backgroundMs: (store) => store.session?.playing ? 0 : 8000,
    run: (store) => store.pollSession(),
  },
  {
    name: "heartbeat",
    ms: 12000,
    backgroundMs: () => 12000,
    gate: (store) => !store.member,
    run: (store) => store.settingsSlice.heartbeat(),
  },
  {
    name: "devices",
    ms: 5000,
    gate: (store) => !store.member && store.view === "settings" && store.settingsTab === "devices",
    run: (store) => store.settingsSlice.refreshDevices(),
  },
  { name: "announcements", ms: 3000, gate: (store) => !store.member, run: (store) => store.pollAnnouncements() },
  {
    name: "process",
    ms: 5000,
    gate: (store) => store.view === "settings",
    run: (store) => store.refreshProcess(),
  },
  {
    name: "spotify",
    ms: 5000,
    gate: (store) => store.view === "settings",
    run: (store) => store.refreshSpotifyStatus(),
  },
  {
    name: "jobs",
    ms: 5000,
    gate: (store) => store.view === "jobs",
    run: (store) => store.refreshJobs(),
  },
];

export function startPolls(store: Store) {
  const lastRun = new Map<Poll, number>();
  let pageAway = false;
  let hiddenIdleSince: number | undefined;
  const playingHere = () => store.sessionSlice.playbackInUse || Boolean(store.radioSlice.tuningGenre) ||
    store.radioStatus === "playing" || store.radioStatus === "starting";
  const leave = (closing: boolean) => {
    hiddenIdleSince = undefined;
    if (store.playbackPresenceActive && !store.member) void store.settingsSlice.leaveDevice().catch(() => {});
    store.sessionSlice.leavePresence(closing);
  };
  const checkRoom = () => { if (!pageAway && !document.hidden) void store.checkRoom(); };
  const tick = (poll: Poll) => {
    if (pageAway || !store.playbackPresenceActive) return;
    const interval = document.hidden ? poll.backgroundMs?.(store) : poll.foregroundMs?.(store) ?? 0;
    const now = performance.now();
    if (interval === undefined || now - (lastRun.get(poll) ?? -Infinity) < interval ||
      store.roomConnection !== "ready") return;
    if (poll.gate && !poll.gate(store)) return;
    lastRun.set(poll, now);
    const result = poll.run(store);
    if (result && typeof (result as Promise<unknown>).catch === "function") {
      (result as Promise<unknown>).catch(() => {});
    }
  };
  const timers = POLLS.map((poll) => setInterval(() => tick(poll), poll.ms));
  timers.push(setInterval(() => {
    if (pageAway) return;
    if (!document.hidden) {
      checkRoom();
    } else if (store.playbackPresenceActive) {
      if (playingHere()) {
        hiddenIdleSince = undefined;
      } else {
        // Keep a recently playing tab reachable briefly for a remote pause or output switch.
        hiddenIdleSince ??= performance.now();
        if (performance.now() - hiddenIdleSince >= 30_000) leave(false);
      }
    }
  }, 4000));
  const refreshRadio = () => {
    if (pageAway || document.hidden) return;
    void store.radioSlice.refreshHistory();
  };
  const resume = () => {
    if (pageAway || document.hidden) return;
    hiddenIdleSince = undefined;
    store.sessionSlice.resumePresence();
    checkRoom();
    refreshRadio();
    POLLS.forEach((poll) => tick(poll));
  };
  const visibility = () => {
    if (pageAway) return;
    if (!document.hidden) resume();
    else if (store.playbackPresenceActive && !playingHere() && !store.session?.playing) leave(false);
  };
  const hidePage = () => {
    pageAway = true;
    store.radioSlice.cancelLibraryTune();
    leave(true);
  };
  const showPage = () => {
    pageAway = false;
    store.sessionSlice.reopenPresence();
    resume();
  };
  document.addEventListener("visibilitychange", visibility);
  window.addEventListener("pagehide", hidePage);
  window.addEventListener("pageshow", showPage);
  window.addEventListener("focus", refreshRadio);
  const reconnectRadio = () => {
    if (store.playbackPresenceActive && !pageAway) store.radioSlice.retryHistory();
  };
  window.addEventListener("online", checkRoom);
  window.addEventListener("online", reconnectRadio);
  window.addEventListener("offline", checkRoom);
  return () => {
    timers.forEach(clearInterval);
    document.removeEventListener("visibilitychange", visibility);
    window.removeEventListener("pagehide", hidePage);
    window.removeEventListener("pageshow", showPage);
    window.removeEventListener("focus", refreshRadio);
    window.removeEventListener("online", checkRoom);
    window.removeEventListener("online", reconnectRadio);
    window.removeEventListener("offline", checkRoom);
  };
}
