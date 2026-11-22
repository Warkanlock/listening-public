import { accountHeaders, currentAccount } from "./api";

export type HikeTrack = {
  id: number;
  title: string;
  artist: string;
  duration: number;
  audio: string;
};
export type HikePlan = {
  id: string;
  requested: number;
  total: number;
  mixes: { name: string; tracks: HikeTrack[] }[];
  offline_origin: string;
};
type SavedHike = { plan: HikePlan; downloaded: number[]; bytes: number };
type Hikes = { active: SavedHike | null; draft: SavedHike | null };
type SavedAudio = { bytes: ArrayBuffer; type: string };
type HikeFiles = { trips: IDBObjectStore; audio: IDBObjectStore };

export const hikeAccountKey = "listening-hiking-account";
const maxTripBytes = 1024 ** 3;
const maxSongBytes = 64 * 1024 ** 2;

export const offlineHeadingHash = "#camping";
export const tripPlanningHash = "#camping-plan";
export const isOfflineTripHash = (hash: string) => hash === offlineHeadingHash || hash === "#hiking";
export const isTripPlanningHash = (hash: string) => hash === tripPlanningHash || hash === "#hiking-plan";
export const offlineAccount = () => localStorage.getItem(hikeAccountKey) || "";
export function setOfflineAccount(account: string) {
  if (account) localStorage.setItem(hikeAccountKey, account);
  else localStorage.removeItem(hikeAccountKey);
}

function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

let database: Promise<IDBDatabase> | undefined;
function openDatabase() {
  if (!database) {
    const request = indexedDB.open("listening-hiking", 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore("trips");
      request.result.createObjectStore("audio");
    };
    database = requestResult(request).catch(error => {
      database = undefined;
      throw error;
    });
  }
  return database;
}

async function useHikes<T>(
  account: string, mode: IDBTransactionMode, use: (state: Hikes, files: HikeFiles) => T,
): Promise<T> {
  const db = await openDatabase();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(["trips", "audio"], mode);
    const files = { trips: tx.objectStore("trips"), audio: tx.objectStore("audio") };
    const request = files.trips.get(account);
    let result: T;
    tx.oncomplete = () => resolve(result);
    tx.onabort = () => reject(tx.error || new Error("Saving on this device failed."));
    request.onsuccess = () => {
      try {
        // Queue writes before IDB dispatch returns. WebKit can close the
        // transaction before a continuation after await runs.
        result = use(request.result ?? { active: null, draft: null }, files);
      } catch (error) {
        reject(error);
        tx.abort();
      }
    };
  });
}

export function getHikes(account: string) {
  return useHikes(account, "readonly", state => state);
}

function removeAudio(audio: IDBObjectStore, trip: string) {
  audio.delete(IDBKeyRange.bound([trip, 0], [trip, Number.MAX_SAFE_INTEGER]));
}

export async function removeHike(account: string, kind: keyof Hikes) {
  await useHikes(account, "readwrite", (state, { trips, audio }) => {
    const hike = state[kind];
    if (!hike) return;
    removeAudio(audio, hike.plan.id);
    trips.put({ ...state, [kind]: null }, account);
  });
  if (kind === "active" && offlineAccount() === account) setOfflineAccount("");
}

export async function readHikeAudio(account: string, plan: string, track: number): Promise<Blob | undefined> {
  const request = await useHikes(account, "readonly", (state, { audio }) =>
    offlineAccount() === account && state.active?.plan.id === plan ? audio.get([plan, track]) : undefined);
  const clip: SavedAudio | undefined = request?.result;
  return clip ? new Blob([clip.bytes], { type: clip.type }) : undefined;
}

function requireDraft(state: Hikes, plan: HikePlan) {
  if (state.draft?.plan.id !== plan.id) throw new Error("This trip changed in another tab. Reopen Camping mode.");
  return state.draft;
}

async function saveSongs({
  account, plan, signal, progress,
}: {
  account: string;
  plan: HikePlan;
  signal: AbortSignal;
  progress: (done: number) => void;
}) {
  if (!plan.total) throw new Error("There are no ready songs to save.");
  let draft = await useHikes(account, "readwrite", (state, { trips }) => {
    const next = state.draft || { plan, downloaded: [], bytes: 0 };
    if (next.plan.id !== plan.id) throw new Error("Resume or discard the unfinished trip first.");
    trips.put({ ...state, draft: next }, account);
    return next;
  });
  progress(draft.downloaded.length);
  for (const track of plan.mixes.flatMap(mix => mix.tracks)) {
    if (signal.aborted) throw signal.reason;
    if (draft.downloaded.includes(track.id)) continue;
    if (!/^\/api\/audio\/[1-9][0-9]*\?recording=[a-f0-9]{64}$/.test(track.audio)) {
      throw new Error("The room returned an invalid song.");
    }
    const response = await fetch(track.audio + "&format=aac", {
      headers: accountHeaders(track.audio), signal, cache: "no-store", redirect: "error",
    });
    const size = Number(response.headers.get("Content-Length"));
    const type = response.headers.get("Content-Type");
    if (response.status !== 200 || !type?.startsWith("audio/")) {
      await response.body?.cancel();
      if (response.status === 409) throw new Error("The library changed. Discard this download and make new mixes.");
      throw new Error(`Couldn't save “${track.title}”. Reconnect to the room and resume.`);
    }
    if (!Number.isSafeInteger(size) || size <= 0 || size > Math.min(maxSongBytes, maxTripBytes - draft.bytes)) {
      await response.body?.cancel();
      throw new Error(`“${track.title}” exceeds the download limit. Try a smaller trip or reshuffle.`);
    }
    const bytes = await response.arrayBuffer();
    if (signal.aborted) throw signal.reason;
    if (bytes.byteLength !== size) throw new Error("A song download was incomplete. Resume to try again.");
    const clip = { bytes, type } satisfies SavedAudio;
    draft = await useHikes(account, "readwrite", (state, { trips, audio }) => {
      const current = requireDraft(state, plan);
      if (current.downloaded.includes(track.id)) return current;
      if (current.bytes + size > maxTripBytes) throw new Error("This trip exceeds the 1 GB download limit.");
      const next = { plan, downloaded: [...current.downloaded, track.id], bytes: current.bytes + size };
      audio.put(clip, [plan.id, track.id]);
      trips.put({ ...state, draft: next }, account);
      return next;
    });
    progress(draft.downloaded.length);
  }
  if (signal.aborted) throw signal.reason;
  await useHikes(account, "readwrite", (state, { trips, audio }) => {
    const complete = requireDraft(state, plan);
    if (complete.downloaded.length !== plan.total) throw new Error("Some songs still need to download.");
    if (state.active) removeAudio(audio, state.active.plan.id);
    trips.put({ active: complete, draft: null }, account);
  });
  if (!signal.aborted && currentAccount() === account) setOfflineAccount(account);
}

export async function saveHike(options: Parameters<typeof saveSongs>[0]) {
  await navigator.storage?.persist?.().catch(() => false);
  if (!navigator.locks) return saveSongs(options);
  return navigator.locks.request(`listening-hike-${options.account}`, { ifAvailable: true }, async lock => {
    if (!lock) throw new Error("This trip is downloading in another tab.");
    await saveSongs(options);
  });
}
