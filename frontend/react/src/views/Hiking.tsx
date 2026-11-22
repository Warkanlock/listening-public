import { useEffect, useRef, useState } from "react";
import { accountChangedKey, api, currentAccount } from "../api";
import { RoomIcon } from "../components/RoomIcon";
import { RoomConfirm } from "../components/RoomControls";
import { verifiedRoomAccount } from "../components/RoomLogin";
import {
  getHikes, hikeAccountKey, offlineAccount, offlineHeadingHash, readHikeAudio, removeHike, saveHike, setOfflineAccount, tripPlanningHash,
  type HikePlan, type HikeTrack,
} from "../hikingStorage";

const sizes = [30, 60, 90];

function summary(plan: HikePlan) {
  return `${plan.total} ${plan.total === 1 ? "song" : "songs"} across ${plan.mixes.length} ${plan.mixes.length === 1 ? "mix" : "mixes"}`;
}

function mixLength(tracks: HikeTrack[]) {
  const minutes = Math.round(tracks.reduce((total, track) => total + track.duration, 0) / 60);
  const hours = Math.floor(minutes / 60);
  const length = hours ? `${hours} hr ${minutes % 60} min` : `${minutes} min`;
  return tracks.length + (tracks.length === 1 ? " song" : " songs") + (minutes ? " · " + length : "");
}

function MixPreview({ plan }: { plan: HikePlan }) {
  return <div className="hike-mixes">
    {plan.mixes.map((mix, index) => <details key={index}>
      <summary><span>{mix.name}</span><small>{mixLength(mix.tracks)}</small></summary>
      <ol>{mix.tracks.map(track => <li key={track.id}>{track.title} <span>{track.artist}</span></li>)}</ol>
    </details>)}
  </div>;
}

export function Hiking({ ready }: { ready: boolean }) {
  const account = currentAccount();
  const [count, setCount] = useState(30);
  const [preview, setPreview] = useState<HikePlan>();
  const [stored, setStored] = useState<Awaited<ReturnType<typeof getHikes>>>();
  const [removing, setRemoving] = useState<"active" | "draft" | null>(null);
  const [planning, setPlanning] = useState(false);
  const [saving, setSaving] = useState(false);
  const [done, setDone] = useState(0);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const transfer = useRef<AbortController | null>(null);
  const planRequest = useRef<AbortController | null>(null);
  const plan = stored?.draft?.plan || preview;
  const worker = navigator.serviceWorker;
  const secure = isSecureContext && !!worker;
  const [canReopen, setCanReopen] = useState(Boolean(worker?.controller));

  useEffect(() => {
    const update = () => setCanReopen(Boolean(worker?.controller));
    worker?.addEventListener("controllerchange", update);
    return () => worker?.removeEventListener("controllerchange", update);
  }, [worker]);

  useEffect(() => {
    let mounted = true;
    void getHikes(account).then(data => {
      if (!mounted) return;
      setStored(data);
      if (data.active) setOfflineAccount(account);
      if (data.draft) setDone(data.draft.downloaded.length);
    }).catch(() => {
      if (mounted) setError("This device did not allow offline storage.");
    });
    return () => {
      mounted = false;
      transfer.current?.abort();
      planRequest.current?.abort();
    };
  }, [account]);

  async function shuffle() {
    if (planning || saving || !ready || stored?.draft) return;
    planRequest.current?.abort();
    const controller = new AbortController();
    planRequest.current = controller;
    setPlanning(true);
    setError("");
    setMessage("");
    try {
      setPreview(await api<HikePlan>(`/api/hiking?songs=${count}`, { signal: controller.signal }));
    } catch {
      if (!controller.signal.aborted) setError("Couldn't find ready songs. Reconnect to the room and try again.");
    } finally {
      setPlanning(false);
    }
  }

  async function download() {
    if (!plan || !ready || transfer.current) return;
    const controller = new AbortController();
    transfer.current = controller;
    setError("");
    setMessage("");
    setSaving(true);
    try {
      await saveHike({ account, plan, signal: controller.signal, progress: setDone });
      setPreview(undefined);
      setMessage("Your trip is ready offline.");
    } catch (failure) {
      if (controller.signal.aborted) setMessage("Download paused. You can resume where you left off.");
      else setError(failure instanceof DOMException && failure.name === "QuotaExceededError"
        ? "This device ran out of storage. Free some space and resume."
        : failure instanceof Error ? failure.message : "Couldn't save the trip. Reconnect and resume.");
    } finally {
      transfer.current = null;
      setSaving(false);
      await getHikes(account).then(setStored).catch(() => {});
    }
  }

  async function remove(kind: "active" | "draft") {
    if (saving) return;
    try {
      await removeHike(account, kind);
      setStored(await getHikes(account));
      if (kind === "draft") {
        setPreview(undefined);
        setDone(0);
      }
      setError("");
      setMessage(kind === "active" ? "Saved trip removed from this device." : "Unfinished download discarded.");
    } catch {
      setError("Couldn't remove this trip. Try again.");
    }
  }

  return <section className="view hike-page" aria-labelledby="hike-heading">
    {removing && <RoomConfirm open title={removing === "active" ? "Remove saved trip?" : "Discard unfinished download?"}
      description="This removes the trip files saved on this device."
      confirmLabel={removing === "active" ? "Remove trip" : "Discard download"}
      onClose={() => setRemoving(null)} onConfirm={() => void remove(removing)} />}
    <header className="listening-view-head">
      <div>
        <h1 id="hike-heading">Camping mode</h1>
        <p>Pack a few shuffled mixes from your library for when the room is out of reach.</p>
      </div>
      <svg className="hike-landscape" viewBox="0 0 520 174" preserveAspectRatio="xMidYMid slice"
        aria-hidden="true" focusable="false">
        <circle cx="370" cy="49" r="26" fill="var(--map-peach)" />
        <g fill="none" stroke="var(--map-ink)" strokeWidth="0.7" opacity="0.16">
          <path d="M276 68c24-38 81-58 137-53 47 5 84 27 103 52" />
          <path d="M246 67C282 16 346-5 417 2c53 6 90 27 116 53" />
          <path d="M302 67c24-30 65-44 109-39 33 4 62 18 81 37" />
        </g>
        <path d="M0 123 59 81l18 10 39-38 89 67 54-25 73 79H0Z" fill="var(--map-lavender)" />
        <path d="m0 137 60-36 34 15 58-23 36 23 95-93 58 59 17-9 47 43 57-39 58 35v62H0Z"
          fill="var(--map-fern)" />
        <path d="m283 23 58 59 17-9 47 43 57-39 58 35v62H213l38-61 17-53Z"
          fill="var(--map-mint)" />
        <path d="m257 49 26-26 29 30-21-9-10 6-10-5Z" fill="var(--map-paper)" />
        <path d="M0 153c53-12 101-24 157-13 48 10 91 8 150-1 108-16 164 3 213 10v25H0Z"
          fill="var(--map-mint)" />
        <g fill="none" stroke="var(--map-ink)" strokeWidth="0.85" opacity="0.21">
          <path d="M-10 162c62-12 122-23 172-13 59 12 102 8 156-1 84-14 146-6 209 10" />
          <path d="M-10 170c62-12 122-22 169-12 58 13 110 9 165 0 78-12 140-7 203 8" />
          <path d="M94 116c16 7 34 11 57 11 37 0 69-17 104-45 31-25 72-3 95 19 18 17 34 25 60 27" />
          <path d="M182 125c27-7 52-24 77-42 25-18 62 3 78 20 14 15 29 24 53 29" />
        </g>
        <path d="M189 174c-6-12 19-15 42-19 27-4 42-11 38-20-3-8-22-9-19-18 3-7 24-7 38-18"
          fill="none" stroke="var(--map-active)" strokeWidth="1.5" strokeDasharray="2 4" strokeLinecap="round" />
        <circle cx="288" cy="99" r="4" fill="var(--map-paper)" stroke="var(--map-active)" strokeWidth="1.5" />
      </svg>
    </header>

    {stored?.active && <section className="hike-saved" aria-label="Saved trip">
      <div className="hike-actions">
        <div><h2>Saved on this device</h2><p>{summary(stored.active.plan)} · {Math.round(stored.active.bytes / 1024 ** 2)} MB</p></div>
        <a className="primary-action" href={offlineHeadingHash}>Open saved trip</a>
        <button className="quiet-button" type="button" onClick={() => setRemoving("active")} disabled={saving}>Remove saved trip</button>
      </div>
      {!canReopen && <p className="hike-note">Keep this tab open to play the saved trip offline.</p>}
      <MixPreview plan={stored.active.plan} />
    </section>}

    <div className="hike-planner">
      {!stored?.draft && <div className="hike-actions">
        <div className="hike-length" role="group" aria-label="Songs for the trip">
          {sizes.map(size => <button className="quiet-button" key={size} type="button" aria-pressed={count === size}
            disabled={planning || saving} onClick={() => { setCount(size); setPreview(undefined); }}>{size} songs</button>)}
        </div>
        <button className="primary-action" type="button" onClick={() => void shuffle()}
          disabled={!ready || !stored || planning || saving}>{planning ? "Finding songs…" : preview ? "Reshuffle" : "Make mixes"}</button>
      </div>}
      {!ready && <p role="status">Connect to the room to make or resume a trip.</p>}
      {plan && !plan.total && <p>No ready songs yet. Add playable music to the library and try again.</p>}
      {!!plan?.total && <>
        <div>
          <h2>{stored?.draft || saving ? "Download trip" : "Your mixes"}</h2>
          <p>{summary(plan)}{plan.total < plan.requested ? " · All available songs" : ""}</p>
        </div>
        <MixPreview plan={plan} />
        {!secure && <p className="hike-note">{plan.offline_origin
          ? <>To reopen a saved trip without a connection, <a href={plan.offline_origin + "/" + tripPlanningHash}
            target="_blank" rel="noreferrer">open Camping mode over private HTTPS</a> before saving.</>
          : "Keep this tab open to listen offline. Reopening it offline requires a trusted HTTPS address."}</p>}
        {secure && <p className="hike-note">Download over Wi-Fi. On iPhone or iPad, add the HTTPS page to your Home Screen before saving.</p>}
        {(saving || stored?.draft) && <div className="hike-progress">
          <progress max={plan.total} value={done} aria-label="Trip download progress" />
          <p role="status">Saved {done} of {plan.total} songs</p>
        </div>}
        <div className="hike-actions">
          {saving
            ? <button className="quiet-button" type="button" onClick={() => transfer.current?.abort()}>Pause download</button>
            : <>
              <button className="primary-action" type="button" disabled={!ready} onClick={() => void download()}>
                {stored?.draft ? "Resume download" : "Save on this device"}
              </button>
              {stored?.draft && <button className="quiet-button" type="button" onClick={() => setRemoving("draft")}>Discard download</button>}
            </>}
        </div>
      </>}
      {message && <p role="status">{message}</p>}
      {error && <p role="alert">{error}</p>}
    </div>
  </section>;
}

export function OfflineHiking() {
  const [account] = useState(offlineAccount);
  const [plan, setPlan] = useState<HikePlan | null>();
  const [selection, setSelection] = useState<{ mix: number; index: number; play: boolean } | null>(null);
  const [error, setError] = useState("");
  const [audioReady, setAudioReady] = useState(false);
  const element = useRef<HTMLAudioElement>(null);
  const mix = selection && plan ? plan.mixes[selection.mix] : undefined;
  const track = mix && selection ? mix.tracks[selection.index] : undefined;

  useEffect(() => {
    let valid = true;
    let request: AbortController | undefined;

    function invalidate() {
      valid = false;
      request?.abort();
      setSelection(null);
      setPlan(null);
    }

    const changed = (event: StorageEvent) => {
      if (event.key === accountChangedKey || event.key === hikeAccountKey || event.key === null) invalidate();
    };

    async function open() {
      if (!account) { invalidate(); return; }
      if (!valid) return;
      request?.abort();
      const controller = new AbortController();
      request = controller;
      const timeout = window.setTimeout(() => controller.abort(), 6500);
      const response = await fetch("/api/account", { cache: "no-store", signal: controller.signal })
        .catch(() => undefined).finally(() => window.clearTimeout(timeout));
      if (!valid || request !== controller) return;
      if (response && response.status < 500) {
        const remote = response.ok ? verifiedRoomAccount(await response.json().catch(() => null)) : "";
        if (!valid || request !== controller) return;
        if (remote !== account) {
          if (offlineAccount() === account) setOfflineAccount("");
          invalidate();
          return;
        }
      }
      if (offlineAccount() !== account) return;
      const value = await getHikes(account);
      if (valid && request === controller) setPlan(value.active?.plan ?? null);
    }

    const load = () => void open().catch(() => {
      if (valid) {
        setPlan(null);
        setError("Couldn't open the saved trip on this device.");
      }
    });
    load();
    window.addEventListener("storage", changed);
    window.addEventListener("online", load);
    return () => {
      valid = false;
      request?.abort();
      window.removeEventListener("storage", changed);
      window.removeEventListener("online", load);
    };
  }, [account]);

  useEffect(() => {
    if (!plan || !track) return;
    const audio = element.current;
    if (!audio) return;
    let url = "";
    let current = true;
    setAudioReady(false);
    setError("");
    void readHikeAudio(account, plan.id, track.id).then(blob => {
      if (!current) return;
      if (!blob) throw new Error("The device removed this song from storage. Save the trip again when connected.");
      url = URL.createObjectURL(blob);
      audio.src = url;
      setAudioReady(true);
      if ("mediaSession" in navigator && "MediaMetadata" in window) {
        navigator.mediaSession.metadata = new MediaMetadata({ title: track.title, artist: track.artist, album: "Camping mode" });
      }
      if (selection?.play) void audio.play().catch(() => {});
    }).catch(failure => {
      if (current) setError(failure instanceof Error ? failure.message : "Couldn't load this song.");
    });
    return () => {
      current = false;
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
      if (url) URL.revokeObjectURL(url);
      if ("mediaSession" in navigator) navigator.mediaSession.metadata = null;
    };
  }, [account, plan, track, selection?.play]);

  function step(delta: number) {
    if (!selection || !mix) return;
    const index = selection.index + delta;
    if (index >= 0 && index < mix.tracks.length) setSelection({ ...selection, index, play: true });
  }

  return <main className="view hike-page hike-offline">
    <header className="listening-view-head">
      <div>
        <h1>Camping mode</h1>
        <p>{plan ? summary(plan) + " saved on this device" : plan === null ? "No trip saved on this device." : "Opening saved trip…"}</p>
      </div>
      <a className="quiet-button icon-control" href={tripPlanningHash} title="Back to the room" aria-label="Back to the room">
        <RoomIcon name="back" />
      </a>
    </header>
    {plan && <div className="hike-player">
      <div className="hike-length" role="group" aria-label="Choose a mix">
        {plan.mixes.map((item, index) => <button className="quiet-button" type="button" key={index}
          aria-pressed={selection?.mix === index} onClick={() => setSelection({ mix: index, index: 0, play: true })}>
          {item.name}
        </button>)}
      </div>
      {track && <div className="hike-now">
        <h2>{track.title}</h2><p>{track.artist}</p>
        <audio ref={element} controls preload="metadata" aria-label="Offline audio player" onEnded={() => step(1)} />
        {!audioReady && !error && <p role="status">Opening song…</p>}
        <div className="hike-actions">
          <button className="quiet-button" type="button" onClick={() => step(-1)} disabled={!selection?.index}>Previous</button>
          <button className="quiet-button" type="button" onClick={() => step(1)}
            disabled={!selection || !mix || selection.index + 1 >= mix.tracks.length}>Next</button>
        </div>
      </div>}
      {mix && selection && <ol className="hike-track-list">
        {mix.tracks.map((item, index) => <li key={item.id}><button type="button" aria-current={selection.index === index ? "true" : undefined}
          onClick={() => setSelection({ ...selection, index, play: true })}>{item.title}<span>{item.artist}</span></button></li>)}
      </ol>}
    </div>}
    {error && <p role="alert">{error}</p>}
  </main>;
}
