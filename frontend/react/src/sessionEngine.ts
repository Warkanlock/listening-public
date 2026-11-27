import { useEffect, useRef, useState, type RefObject } from "react";
import type { SessionTrack } from "./api";
import { selectPlaybackAudioSession, silence, usesSystemAudio } from "./mediaOutput";
import { audioHref } from "./trackBuffer";
import type { Store } from "./store";
import { notify } from "./toasts";

type Slot = 0 | 1;
const mediaSlots: readonly Slot[] = usesSystemAudio ? [0] : [0, 1];
interface LoadedTrack {
  href: string;
  id: number | string;
  track: SessionTrack;
}

interface AudioGraph {
  context: AudioContext;
  gains: [GainNode, GainNode];
}

interface AppliedPlayback {
  version: number;
  occurrence?: number;
  ref: unknown;
}

// ReplayGain normalization (item 35): the linear gain a track
// plays at on this device. Off, or no gain data, is exactly 1.0,
// the pass-through the gain chain promises with features off.
// Track gain wins and album gain is the fallback; an R128-only
// tag converts with the +5 dB between the R128 reference
// (-23 LUFS) and ReplayGain's 89 dB reference (about -18 LUFS).
// A known peak clamps the gain so the result cannot clip.
const trackGain = (
  store: Store,
  track: SessionTrack | null | undefined,
): number => {
  if (!track || !store.normalize) return 1;
  let db = track.track_gain ?? track.album_gain ?? null;
  if (db == null && track.r128_track_gain != null) {
    db = track.r128_track_gain + 5;
  }
  if (db == null) return 1;
  let gain = Math.pow(10, db / 20);
  const peak = track.track_peak ?? track.album_peak ?? null;
  if (peak != null && peak > 0 && gain * peak > 1) gain = 1 / peak;
  return gain;
};

// The shared session engine (item 30), extended with plan 039's
// player quality (item 35): two audio elements share one Web Audio
// gain chain. The standby element reads the buffered next track
// so a track boundary can swap elements without another network
// request, and a crossfade blends the swap when one is set. An
// upcoming song outside the buffer budget can preload from HTTP. The
// session poll still owns position truth: a local start the
// session does not confirm is abandoned on the next apply, and a
// confirmed start keeps its head start as that track's position
// offset. Outside a crossfade each slot's gain rests at its
// track's normalization gain, which is exactly 1.0 with
// normalization off. iOS uses one system media element so the
// audio continues when Web Audio is suspended in the background.
export function useSessionEngine(
  store: Store,
  audioRefA: RefObject<HTMLAudioElement | null>,
  audioRefB: RefObject<HTMLAudioElement | null>,
) {
  const hrefOf = (track: SessionTrack) => audioHref(track, store.compactAudio, store.sessionSlice.outputId);
  const buffer = store.sessionSlice.buffer;
  const sourceOf = (href: string) => buffer.url(href) || href;
  const nextTrack = store.session?.upcoming[0];
  const nextHref = nextTrack ? hrefOf(nextTrack) : null;
  const standbySource = (href: string) => buffer.url(href) || (buffer.canStreamInstead(href) ? href : null);
  const renderedStandbySource = nextHref ? standbySource(nextHref) : null;
  const [status, setStatus] = useState("Shared session");
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [audioPlaying, setAudioPlaying] = useState(false);
  const joinedRef = useRef(false);
  const appliedRef = useRef<AppliedPlayback | null>(null);
  const boundaryInFlight = useRef<AppliedPlayback | null>(null);
  const queueEpoch = store.sessionSlice.queueEpoch;
  const appliedQueue = useRef(queueEpoch);
  const musicActive = () => store.playbackPresenceActive &&
    appliedQueue.current === store.sessionSlice.queueEpoch && !!store.session?.playing;
  const primedRef = useRef<[boolean, boolean]>([false, false]);
  const primingRef = useRef<[boolean, boolean]>([false, false]);
  const activeSlotRef = useRef<Slot>(0);
  const loadedRef = useRef<[LoadedTrack | null, LoadedTrack | null]>([
    null,
    null,
  ]);
  const readyRef = useRef<[boolean, boolean]>([false, false]);
  const pendingRef = useRef<{ slot: Slot; track: LoadedTrack } | null>(null);
  const fadingRef = useRef(false);
  const offsetRef = useRef<{ href: string; value: number } | null>(null);
  const graphRef = useRef<AudioGraph | null>(null);
  const volumeRef = useRef(0.8);
  const positionStateRef = useRef<{ position: number; duration: number } | null>(
    null,
  );
  // The track href the current duration value belongs to. A
  // length-less record states no duration, so the value comes
  // from the loaded element instead; the ref keeps a session
  // poll for that same track from stomping the adopted value
  // back to zero (the seek slider disables itself at zero).
  const durationHrefRef = useRef<string | null>(null);
  const controlsRef = useRef<{
    seekTo: (seconds: number) => void;
    commitSeek: (seconds: number) => void;
  }>({ seekTo: () => {}, commitSeek: () => {} });

  const slotElement = (slot: Slot) =>
    (slot === 0 ? audioRefA : audioRefB).current;

  const unload = (slot: Slot) => {
    const element = slotElement(slot);
    if (element?.hasAttribute("src")) {
      element.pause();
      element.removeAttribute("src");
      element.load();
    }
    loadedRef.current[slot] = null;
    readyRef.current[slot] = false;
  };
  // The element the listener hears: the session's element, except
  // that a locally started next track takes over at the boundary
  // while a crossfade still reports the outgoing track.
  const audibleSlot = (): Slot =>
    fadingRef.current
      ? activeSlotRef.current
      : (pendingRef.current?.slot ?? activeSlotRef.current);

  // The gain chain is built once, lazily, at the first play
  // attempt: a media element source reroutes its element for
  // good, and a context created before any gesture starts
  // suspended anyway. No nodes beyond the per-element gains.
  const ensureGraph = (): AudioGraph | null => {
    if (graphRef.current) return graphRef.current;
    if (usesSystemAudio || typeof AudioContext === "undefined") return null;
    const elements = [audioRefA.current, audioRefB.current];
    if (!elements[0] || !elements[1]) return null;
    const context = new AudioContext();
    const gains = elements.map((element) => {
      const source = context.createMediaElementSource(element!);
      const gain = context.createGain();
      gain.gain.value = 1;
      source.connect(gain);
      gain.connect(context.destination);
      return gain;
    }) as [GainNode, GainNode];
    graphRef.current = { context, gains };
    window.addEventListener(
      "pointerdown",
      () => void graphRef.current?.context.resume(),
      { once: true },
    );
    return graphRef.current;
  };

  const setGain = (slot: Slot, value: number) => {
    const graph = graphRef.current;
    if (!graph) return;
    const gain = graph.gains[slot].gain;
    gain.cancelScheduledValues(graph.context.currentTime);
    gain.setValueAtTime(value, graph.context.currentTime);
  };

  const rampGain = (slot: Slot, to: number, seconds: number) => {
    const graph = graphRef.current;
    if (!graph) return;
    const gain = graph.gains[slot].gain;
    const now = graph.context.currentTime;
    gain.cancelScheduledValues(now);
    gain.setValueAtTime(gain.value, now);
    gain.linearRampToValueAtTime(to, now + seconds);
  };

  // The gain a slot rests at outside a crossfade: its loaded
  // track's normalization gain (exactly 1.0 with the feature off
  // or the track untagged).
  const baseGain = (slot: Slot): number =>
    trackGain(store, loadedRef.current[slot]?.track);

  const resetGains = () => {
    setGain(0, baseGain(0));
    setGain(1, baseGain(1));
  };

  const compactFailed = (element: HTMLAudioElement) => element.error && store.compactAudio;
  const compactError = "Compact audio unavailable. Tap Play to retry or choose Original in Playback settings.";

  const attemptPlay = (slot: Slot, retry = false): Promise<boolean> => {
    const element = slotElement(slot);
    if (!element) return Promise.resolve(false);
    selectPlaybackAudioSession();
    primingRef.current[slot] = false;
    element.muted = false;
    if (compactFailed(element)) {
      if (!retry) return Promise.resolve(false);
      const position = store.session?.position ?? 0;
      element.addEventListener("loadedmetadata", () => {
        element.currentTime = Math.min(position, element.duration || position);
      }, { once: true });
      element.load();
    }
    const graph = ensureGraph();
    if (graph) {
      void graph.context.resume().catch(() => {});
      // The graph may have been built by this very attempt, after
      // the apply pass already set the slot's gain against no
      // graph; restate it. A fade in flight owns its slots' ramps.
      if (!fadingRef.current) setGain(slot, baseGain(slot));
    }
    return element
      .play()
      .then(() => {
        // A suspended context leaves the element playing into
        // silence; that is the autoplay block the status line
        // exists for, so it reports like a rejected play().
        if (graph && graph.context.state !== "running") {
          element.pause();
          return false;
        }
        primedRef.current[slot] = true;
        joinedRef.current = true;
        return true;
      })
      .catch(() => false);
  };

  const restoreBufferedAudio = (slot: Slot) => {
    const element = slotElement(slot);
    const loaded = loadedRef.current[slot];
    const cached = loaded && buffer.url(loaded.href);
    if (!store.playbackPresenceActive || !element || !cached || element.src === cached || element.ended) return false;
    const position = element.currentTime || store.session?.position || 0;
    element.addEventListener("loadedmetadata", () => {
      if (element.src === cached) element.currentTime = Math.min(position, element.duration || position);
    }, { once: true });
    element.src = cached;
    if (musicActive() && !store.radioSounding) void attemptPlay(slot);
    return true;
  };

  const updatePositionState = (element: HTMLAudioElement) => {
    if (
      !("mediaSession" in navigator) ||
      typeof navigator.mediaSession.setPositionState !== "function"
    )
      return;
    const length = element.duration;
    if (!Number.isFinite(length) || length <= 0) return;
    const position = Math.min(element.currentTime, length);
    const last = positionStateRef.current;
    if (
      last &&
      last.duration === length &&
      Math.abs(last.position - position) < 1
    )
      return;
    positionStateRef.current = { position, duration: length };
    try {
      navigator.mediaSession.setPositionState({
        duration: length,
        playbackRate: element.playbackRate,
        position,
      });
    } catch {
      // A browser that disagrees about the range; the next
      // timeupdate retries with fresher numbers.
    }
  };

  const maybeStartFade = () => {
    if (usesSystemAudio || !musicActive()) return;
    if (store.sessionSlice.hostingJam) return;
    const seconds = store.crossfade;
    if (!seconds || pendingRef.current || fadingRef.current) return;
    const session = store.session;
    if (!session?.playing || !session.current) return;
    const next = session.upcoming?.[0];
    if (!next) return;
    const active = activeSlotRef.current;
    const standby = (1 - active) as Slot;
    const loaded = loadedRef.current[standby];
    if (!loaded || !readyRef.current[standby] || loaded.href !== hrefOf(next))
      return;
    const element = slotElement(active);
    const standbyElement = slotElement(standby);
    if (!element || !standbyElement) return;
    if (!ensureGraph()) return;
    const remaining = element.duration - element.currentTime;
    if (
      !Number.isFinite(remaining) ||
      element.duration <= seconds ||
      remaining > seconds
    )
      return;
    fadingRef.current = true;
    pendingRef.current = { slot: standby, track: loaded };
    standbyElement.currentTime = 0;
    setGain(standby, 0);
    rampGain(standby, baseGain(standby), seconds);
    rampGain(active, 0, seconds);
    void attemptPlay(standby);
  };

  const startStandby = (next: SessionTrack | null | undefined) => {
    if (usesSystemAudio) return;
    const active = activeSlotRef.current;
    const standby = (1 - active) as Slot;
    if (fadingRef.current && pendingRef.current?.slot === standby) {
      // The blend already started the next track; it stays
      // pending until the session confirms it.
      fadingRef.current = false;
      setGain(standby, baseGain(standby));
      return;
    }
    const loaded = loadedRef.current[standby];
    if (
      !next ||
      !loaded ||
      !readyRef.current[standby] ||
      loaded.href !== hrefOf(next)
    )
      return;
    const element = slotElement(standby);
    if (!element) return;
    pendingRef.current = { slot: standby, track: loaded };
    element.currentTime = 0;
    void attemptPlay(standby);
  };

  const onBoundary = () => {
    const ended = appliedRef.current;
    if (!musicActive() || !ended || boundaryInFlight.current || store.sessionSlice.followingJam) return;
    const session = store.session;
    const next = session?.upcoming[0];
    if (store.sessionSlice.advanceBufferedBoundary(ended.ref, ended.occurrence)) {
      if (store.session?.occurrence !== session?.occurrence) startStandby(next);
      return;
    }
    // A natural boundary is guarded against stale clients. A
    // deliberate Next button press does not supply that guard.
    boundaryInFlight.current = ended;
    void store.sendSession("next", {
      expected_ref: ended.ref,
      expected_occurrence: ended.occurrence,
    }).finally(() => {
      if (boundaryInFlight.current === ended) boundaryInFlight.current = null;
    });
    if (!store.sessionFailed && navigator.onLine) startStandby(next);
  };

  useEffect(() => {
    store.sessionSlice.readPosition = () => {
      const session = store.session;
      if (appliedRef.current?.version === session?.version) {
        const element = slotElement(audibleSlot());
        if (element && element.readyState >= 1) return element.currentTime;
      }
      return session?.position ?? 0;
    };
    const recover = () => restoreBufferedAudio(activeSlotRef.current);
    const readMediaPosition = () => {
      const session = store.session;
      const element = slotElement(activeSlotRef.current);
      if (!session || !element || element.paused || element.ended || element.readyState < 1 ||
          store.radioSounding || appliedQueue.current !== store.sessionSlice.queueEpoch ||
          appliedRef.current?.version !== session.version) return;
      return element.currentTime;
    };
    store.sessionSlice.readMediaPosition = readMediaPosition;
    store.sessionSlice.detachMedia = () => mediaSlots.forEach(unload);
    store.sessionSlice.readAudioPlaying = () => mediaSlots.some(slot => {
      const audio = slotElement(slot);
      return audio !== null && !audio.paused && !audio.ended && !audio.muted;
    });
    window.addEventListener("offline", recover);
    store.sessionSlice.unlockAudio = () => {
      if (navigator.userActivation?.isActive === false) return;
      selectPlaybackAudioSession();
      void ensureGraph()?.context.resume().catch(() => {});
      for (const slot of mediaSlots) {
        const element = slotElement(slot);
        if (!element || !element.paused || primedRef.current[slot] || primingRef.current[slot]) continue;
        if (!element.getAttribute("src")) element.src = silence;
        primingRef.current[slot] = true;
        element.muted = true;
        const finish = () => {
          if (!primingRef.current[slot]) return;
          primingRef.current[slot] = false;
          element.pause();
          element.muted = false;
        };
        void element.play().then(() => {
          primedRef.current[slot] = true;
          finish();
        }, finish);
      }
    };
    store.audioJoiner = () => {
      if (!store.session?.playing) return false;
      const slot = audibleSlot();
      const element = slotElement(slot);
      if (!element || !element.paused) return false;
      void attemptPlay(slot, true).then((ok) => {
        if (ok) setStatus("");
        else if (compactFailed(element)) setStatus(compactError);
        else notify("Browser audio is blocked. Tap Play again.");
      });
      return true;
    };
    return () => {
      window.removeEventListener("offline", recover);
      store.audioJoiner = null;
      store.sessionSlice.unlockAudio = null;
      store.sessionSlice.readPosition = null;
      store.sessionSlice.readMediaPosition = null;
      store.sessionSlice.detachMedia = null;
      store.sessionSlice.readAudioPlaying = null;
      store.sessionSlice.clearBuffer();
    };
  }, [store]);

  useEffect(() => {
    const cleanups: (() => void)[] = [];
    mediaSlots.forEach((slot) => {
      const element = slotElement(slot);
      if (!element) return;
      element.volume = volumeRef.current;
      const onTime = () => {
        if (primingRef.current[slot] || !loadedRef.current[slot]) return;
        if (slot === activeSlotRef.current && !pendingRef.current)
          maybeStartFade();
        if (slot !== audibleSlot()) return;
        setCurrentTime(element.currentTime);
        const hero = document.querySelector(".collection-hero");
        if (hero instanceof HTMLElement) {
          hero.style.setProperty(
            "--play-progress",
            `${element.duration ? (element.currentTime / element.duration) * 100 : 0}%`,
          );
        }
        updatePositionState(element);
      };
      const onMeta = () => {
        if (primingRef.current[slot] || !loadedRef.current[slot]) return;
        if (slot === audibleSlot()) {
          setDuration(
            element.duration || store.session?.current?.duration || 0,
          );
          durationHrefRef.current =
            loadedRef.current[slot]?.href ?? durationHrefRef.current;
        }
        updatePositionState(element);
      };
      const onPlay = () => {
        if (!primingRef.current[slot] && slot === audibleSlot()) setAudioPlaying(true);
      };
      const onPause = () => {
        if (slot === audibleSlot()) setAudioPlaying(false);
      };
      const onEnded = () => {
        if (!primingRef.current[slot] && loadedRef.current[slot] && slot === activeSlotRef.current) onBoundary();
      };
      const onCanPlay = () => {
        readyRef.current[slot] = true;
      };
      const onError = () => {
        // A failed preload falls back to the load-on-apply path;
        // nothing was ever promised from this element.
        if (slot !== activeSlotRef.current) {
          loadedRef.current[slot] = null;
          readyRef.current[slot] = false;
        } else if (!restoreBufferedAudio(slot) && store.compactAudio) {
          setStatus(compactError);
        }
      };
      const onRate = () => updatePositionState(element);
      const attach = (type: string, handler: () => void) => {
        element.addEventListener(type, handler);
        cleanups.push(() => element.removeEventListener(type, handler));
      };
      attach("timeupdate", onTime);
      attach("loadedmetadata", onMeta);
      attach("play", onPlay);
      attach("pause", onPause);
      attach("ended", onEnded);
      attach("canplay", onCanPlay);
      attach("error", onError);
      attach("ratechange", onRate);
    });
    return () => cleanups.forEach((cleanup) => cleanup());
  }, [store]);

  useEffect(() => {
    store.sessionSlice.prepareBuffer();
  }, [store, store.session, store.compactAudio, store.sessionSlice.bufferAhead]);

  useEffect(() => {
    const session = store.session;
    if (appliedQueue.current !== queueEpoch) {
      appliedQueue.current = queueEpoch;
      appliedRef.current = null;
      boundaryInFlight.current = null;
      pendingRef.current = null;
      fadingRef.current = false;
      offsetRef.current = null;
      durationHrefRef.current = null;
      positionStateRef.current = null;
      joinedRef.current = false;
      resetGains();
      mediaSlots.forEach(unload);
      setCurrentTime(0);
      setDuration(0);
      setAudioPlaying(false);
    }
    if (!session) return;
    const active = activeSlotRef.current;
    const element = slotElement(active);
    if (!element) return;
    const previous = appliedRef.current;
    const versionChanged = !previous || previous.version !== session.version;
    const previousRef = previous?.ref;
    const newPlay = previousRef !== session.current_ref || previous?.occurrence !== session.occurrence;
    appliedRef.current = { version: session.version, occurrence: session.occurrence, ref: session.current_ref };
    const track = session.current;

    const abandonPending = () => {
      const pending = pendingRef.current;
      if (pending) {
        slotElement(pending.slot)?.pause();
        pendingRef.current = null;
      }
      if (fadingRef.current) {
        fadingRef.current = false;
        resetGains();
      }
    };
    const mirror = (el: HTMLAudioElement, slot: Slot) => {
      // Radio hold (plan 059, ADR 0003): while radio sounds on
      // this device, its library audio stays paused whatever the
      // shared session does; the session itself is untouched.
      if (session.playing && !store.radioSounding) {
        if (usesSystemAudio && el.ended && !newPlay) {
          // A later session poll can finish the guarded advance after a lost connection.
          onBoundary();
          return;
        }
        if (!versionChanged && !(joinedRef.current && el.paused)) return;
        void attemptPlay(slot).then(ok => {
          if (!musicActive() || store.radioSounding) {
            el.pause();
            return;
          }
          setStatus(ok ? "" : compactFailed(el) ? compactError : "Tap Play to join audio");
        });
      } else {
        el.pause();
        setStatus(store.radioSounding ? "Radio is playing" : session.session_playing ? "Playing on another device" : "Session paused");
      }
    };

    // All Jam outputs follow the server clock, including a host coming out of a crossfade.
    if (store.sessionSlice.hostingJam) {
      if (fadingRef.current) abandonPending();
      offsetRef.current = null;
    }
    if (!track || session.output_selected === false || !store.playbackPresenceActive) {
      abandonPending();
      mediaSlots.forEach(unload);
      offsetRef.current = null;
      setAudioPlaying(false);
      if (track) {
        setCurrentTime(session.position);
        setDuration(track.duration || 0);
        durationHrefRef.current = hrefOf(track);
        if (store.playbackPresenceActive) mirror(element, active);
        else setStatus("Session paused");
      } else {
        setStatus("Shared session");
        durationHrefRef.current = null;
        setDuration(0);
      }
      return;
    }
    const wanted = hrefOf(track);
    if (track.duration) {
      durationHrefRef.current = wanted;
      setDuration(track.duration);
    } else if (durationHrefRef.current !== wanted) {
      // A length-less record (a bare list import, a bring-your-
      // own match) states no duration: a loaded element is the
      // only source. Adopt what the element holding this track
      // already knows (either slot: after a gapless swap the
      // holder is the standby the pending branch below is about
      // to make active, and its metadata fired while it was not
      // yet the audible slot), or wait for the metadata handler
      // to fill the value in when the load lands.
      durationHrefRef.current = wanted;
      const holder = ([0, 1] as Slot[]).find(
        (slot) => loadedRef.current[slot]?.href === wanted,
      );
      const held = holder !== undefined ? slotElement(holder) : null;
      setDuration(
        held && Number.isFinite(held.duration)
          ? held.duration || 0
          : 0,
      );
    }
    // Same track, still length-less: keep the value the
    // element's metadata adopted; a poll must not reset it.
    const pending = pendingRef.current;
    if (pending && newPlay && loadedRef.current[pending.slot]?.href === wanted) {
      // The session confirmed the locally started track: the
      // standby becomes the player, and its head start is kept
      // as this track's position offset for the drift check.
      // The loaded entry takes the session's own track object,
      // which carries the probe facts the preload's copy lacked.
      loadedRef.current[pending.slot] = { href: wanted, id: track.id, track };
      activeSlotRef.current = pending.slot;
      pendingRef.current = null;
      fadingRef.current = false;
      resetGains();
      slotElement((1 - pending.slot) as Slot)?.pause();
      const el = slotElement(pending.slot);
      if (!el) return;
      if (store.sessionSlice.bufferedPlayback) restoreBufferedAudio(pending.slot);
      offsetRef.current = {
        href: wanted,
        value: el.currentTime - session.position,
      };
      mirror(el, pending.slot);
      return;
    }
    if (pending && loadedRef.current[active]?.href === wanted) {
      // A poll from before the boundary landed: the local start
      // stays pending and the session's element is left alone,
      // beyond the shared play/pause mirror. An ended element is
      // never restarted by that mirror; it ended on purpose.
      if (!session.playing) slotElement(pending.slot)?.pause();
      if (!element.ended) mirror(element, active);
      return;
    }
    abandonPending();
    const loaded = loadedRef.current[active];
    const cached = buffer.url(wanted);
    const resumedHere = session.playing && element.paused && versionChanged;
    const changed = !loaded || loaded.href !== wanted || newPlay ||
      (resumedHere && (cached ? element.src !== cached : element.src.startsWith("blob:")));
    if (!changed && loaded) loaded.track = track;
    if (changed) {
      element.src = sourceOf(wanted);
      loadedRef.current[active] = { href: wanted, id: track.id, track };
      readyRef.current[active] = false;
      offsetRef.current = null;
      element.addEventListener(
        "loadedmetadata",
        () => {
          element.currentTime = Math.min(
            session.position,
            element.duration || session.position,
          );
        },
        { once: true },
      );
    } else if (element.readyState >= 1) {
      const offset =
        offsetRef.current?.href === wanted ? offsetRef.current.value : 0;
      const drift = Math.abs(element.currentTime - (session.position + offset));
      const pausedClock = resumedHere || !(session.session_playing ?? session.playing);
      const tolerance = store.sessionSlice.hostingJam || store.sessionSlice.followingJam ? 0.75 : 3;
      if (pausedClock ? Math.abs(element.currentTime - session.position) > 0.01 : drift > tolerance) {
        element.currentTime = Math.min(
          session.position,
          element.duration || session.position,
        );
        offsetRef.current = null;
      }
    }
    mirror(element, active);
    // The resting gain follows the track and the Normalize
    // setting; a fade in flight owns its slots' ramps instead.
    if (!fadingRef.current) setGain(active, baseGain(active));
  }, [store, store.session, store.compactAudio, store.radioSounding, queueEpoch, store.playbackPresenceActive, store.sessionSlice.hostingJam]);

  useEffect(() => {
    if (usesSystemAudio) return;
    const session = store.session;
    if (pendingRef.current || fadingRef.current) return;
    const standby = (1 - activeSlotRef.current) as Slot;
    const element = slotElement(standby);
    if (!element) return;
    const next = session?.upcoming?.[0];
    if (!session?.current || session.output_selected === false || !store.playbackPresenceActive || !next) {
      unload(standby);
      return;
    }
    const href = hrefOf(next);
    const source = standbySource(href);
    if (!source || href === hrefOf(session.current)) {
      unload(standby);
      return;
    }
    if (loadedRef.current[standby]?.href === href && element.src === source) return;
    element.preload = "auto";
    element.src = source;
    loadedRef.current[standby] = { href, id: next.id, track: next };
    readyRef.current[standby] = false;
  }, [store, store.session, store.compactAudio, renderedStandbySource, store.playbackPresenceActive]);

  // Toggling Normalize re-bases both slots on the tracks they
  // hold. A fade in flight keeps the targets it started with.
  useEffect(() => {
    if (fadingRef.current) return;
    ([0, 1] as Slot[]).forEach((slot) => {
      if (loadedRef.current[slot]) setGain(slot, baseGain(slot));
    });
  }, [store.normalize]);

  useEffect(() => {
    if (store.sessionFailed || store.roomConnection === "unreachable") {
      restoreBufferedAudio(activeSlotRef.current);
    }
  }, [store.sessionFailed, store.roomConnection, buffer.ready]);

  useEffect(() => {
    const track = store.session?.current;
    document.title = track ? `${track.title} — Listening` : "Listening";
  }, [store.session?.current]);

  useEffect(() => {
    if (!("mediaSession" in navigator)) return;
    const mediaSession = navigator.mediaSession;
    mediaSession.setActionHandler("play", () => {
      if (store.radioSounding) {
        if (store.radioStatus === "paused" && store.wakePlayback()) store.toggleRadio();
        return;
      }
      const session = store.session;
      const wasActive = store.playbackPresenceActive;
      if (!session?.current || !store.wakePlayback()) return;
      if (wasActive && session.playing) store.audioJoiner?.();
      else void store.sendSession("resume");
    });
    mediaSession.setActionHandler("pause", () => {
      if (store.radioSounding) {
        if (store.radioStatus === "playing") store.toggleRadio();
        return;
      }
      if (store.session?.playing) void store.sendSession("pause");
    });
    mediaSession.setActionHandler("previoustrack", () => {
      if (!store.wakePlayback()) return;
      if (store.radioSounding) store.radioControls?.previous();
      else void store.sendSession("previous");
    });
    mediaSession.setActionHandler("nexttrack", () => {
      if (!store.wakePlayback()) return;
      if (store.radioSounding) store.nextRadio();
      else void store.sendSession("next");
    });
    mediaSession.setActionHandler("seekto", (details) => {
      if (store.radioSounding || details.seekTime == null || !store.wakePlayback()) return;
      const seconds = Math.max(0, details.seekTime);
      controlsRef.current.seekTo(seconds);
      controlsRef.current.commitSeek(seconds);
    });
    return () => {
      mediaSession.setActionHandler("play", null);
      mediaSession.setActionHandler("pause", null);
      mediaSession.setActionHandler("previoustrack", null);
      mediaSession.setActionHandler("nexttrack", null);
      mediaSession.setActionHandler("seekto", null);
    };
  }, [store]);

  useEffect(() => {
    if (!("mediaSession" in navigator)) return;
    if (store.radioSounding) {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: store.radioVideoTitle || store.radioStationName || "Radio",
        artist: store.radioVideoChannel,
        album: store.radioStationName || "Radio",
      });
      return;
    }
    const track = store.session?.current;
    if (!track) {
      navigator.mediaSession.metadata = null;
      return;
    }
    let artwork: MediaImage[] = [];
    if (track.cover) {
      try {
        artwork = [{ src: new URL(track.cover, location.origin).href }];
      } catch {
        artwork = [];
      }
    }
    navigator.mediaSession.metadata = new MediaMetadata({
      title: track.title,
      artist: track.artist,
      album: track.album || "",
      artwork,
    });
  }, [store.session?.current, store.radioSounding, store.radioVideoTitle, store.radioVideoChannel, store.radioStationName]);

  useEffect(() => {
    if (!("mediaSession" in navigator)) return;
    if (store.radioSounding) {
      navigator.mediaSession.playbackState = store.radioStatus === "playing" ? "playing" : "paused";
      return;
    }
    navigator.mediaSession.playbackState = !store.session?.current
      ? "none"
      : audioPlaying
        ? "playing"
        : "paused";
  }, [store.session?.current, store.radioStatus, audioPlaying]);

  const seekTo = (seconds: number) => {
    const element = slotElement(audibleSlot());
    if (element) element.currentTime = seconds;
    setCurrentTime(seconds);
  };
  const commitSeek = (seconds: number) => {
    offsetRef.current = null;
    void store.sendSession("seek", { position: seconds });
  };
  const setVolume = (value: number) => {
    volumeRef.current = value;
    ([0, 1] as Slot[]).forEach((slot) => {
      const element = slotElement(slot);
      if (element) element.volume = value;
    });
  };
  controlsRef.current = { seekTo, commitSeek };

  return {
    status: store.sessionSlice.bufferedPlayback ? "Playing from the buffer" : store.sessionFailed ? "Reconnecting…" : status,
    currentTime,
    duration,
    audioPlaying,
    seekTo,
    commitSeek,
    setVolume,
  };
}
