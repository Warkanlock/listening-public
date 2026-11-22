import { useEffect, type RefObject } from "react";
import type { RadioLibraryTrack } from "./api";
import { selectPlaybackAudioSession, silence } from "./mediaOutput";
import { audioHref } from "./trackBuffer";
import { noiseAudio } from "./radioNoise";
import type { RadioControls, RadioMix } from "./slices/radio";
import type { Store } from "./store";

interface YtPlayer {
  loadPlaylist(videos: string[], index: number, startSeconds: number): void;
  cuePlaylist(videos: string[], index: number): void;
  playVideo(): void;
  pauseVideo(): void;
  stopVideo(): void;
  nextVideo(): void;
  previousVideo(): void;
  setVolume(value: number): void;
  unMute(): void;
  getCurrentTime(): number;
  getPlayerState(): number;
  getPlaylistIndex(): number;
  getVideoData(): { title?: string; author?: string; video_id?: string };
  destroy(): void;
}

interface YtNamespace {
  Player: new (element: HTMLElement, options: Record<string, unknown>) => YtPlayer;
  PlayerState: { ENDED: number; PLAYING: number; PAUSED: number; CUED: number };
}

declare global {
  interface Window {
    YT?: YtNamespace;
    onYouTubeIframeAPIReady?: () => void;
  }
}

let apiPromise: Promise<YtNamespace> | null = null;
const START_TIMEOUT_MS = 9000;
const CHECKPOINT_MS = 10_000;
const MAX_LIVE_ERRORS = 8;

function loadApi(): Promise<YtNamespace> {
  if (window.YT?.Player) return Promise.resolve(window.YT);
  if (!apiPromise) {
    apiPromise = new Promise<YtNamespace>((resolve, reject) => {
      const previous = window.onYouTubeIframeAPIReady;
      const script = document.createElement("script");
      const cleanup = () => {
        clearTimeout(timer);
        window.onYouTubeIframeAPIReady = previous;
      };
      const fail = () => {
        cleanup();
        script.remove();
        reject(new Error("YouTube player unavailable"));
      };
      const timer = window.setTimeout(fail, START_TIMEOUT_MS);
      window.onYouTubeIframeAPIReady = () => {
        cleanup();
        previous?.();
        if (window.YT?.Player) resolve(window.YT);
        else fail();
      };
      script.src = "https://www.youtube.com/iframe_api";
      script.onerror = fail;
      document.head.appendChild(script);
    }).catch((error: unknown) => {
      apiPromise = null;
      throw error;
    });
  }
  return apiPromise;
}

function shuffled<T>(items: T[]) {
  const order = [...items];
  for (let index = order.length - 1; index > 0; index--) {
    const other = Math.floor(Math.random() * (index + 1));
    [order[index], order[other]] = [order[other], order[index]];
  }
  return order;
}

export function useRadioEngine(store: Store, hostRef: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const slice = store.radioSlice;
    const host = hostRef.current;
    const mount = document.createElement("div");
    mount.className = "radio-player-mount";
    host?.appendChild(mount);
    const audio = new Audio();
    audio.preload = "none";
    let player: YtPlayer | null = null;
    let ready = false;
    let booting = false;
    let destroyed = false;
    let mix: RadioMix | null = null;
    let order: string[] = [];
    let libraryOrder: RadioLibraryTrack[] = [];
    let libraryIndex = 0;
    let attempt = 0;
    let startSeconds = 0;
    let started = false;
    let paused = false;
    let errors = 0;
    let timer = 0;
    let noiseUrl: string | undefined;

    const clearTimer = () => window.clearTimeout(timer);

    function stop() {
      clearTimer();
      mix = null;
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
      if (noiseUrl) {
        URL.revokeObjectURL(noiseUrl);
        noiseUrl = undefined;
      }
      if (ready) player?.stopVideo();
    }

    function fail() {
      const name = mix?.name ?? "";
      stop();
      slice.applyEngineFailed(name);
    }

    function load(index: number) {
      if (!player || !order[index]) {
        fail();
        return;
      }
      clearTimer();
      slice.selectVideo(order[index]);
      const position = startSeconds;
      startSeconds = 0;
      player.loadPlaylist(order, index, position);
      timer = window.setTimeout(() => {
        if (!started && mix?.kind === "youtube") load(++attempt);
      }, START_TIMEOUT_MS);
    }

    function cue(index: number) {
      slice.selectVideo(order[index]);
      player?.cuePlaylist(order, index);
    }

    function advanceVideo(natural: boolean) {
      if (!player || mix?.kind !== "youtube") return;
      const index = player.getPlaylistIndex();
      if (index >= 0 && index < order.length - 1) {
        // YouTube already advances within an active playlist.
        if (!natural) {
          if (paused) cue(index + 1);
          else player.nextVideo();
        }
        return;
      }
      order = shuffled(order);
      if (paused) cue(0);
      else load(0);
    }

    function currentVideo(active: YtPlayer) {
      const data = active.getVideoData();
      return {
        title: data?.title || "", channel: data?.author || "",
        videoId: data?.video_id || order[active.getPlaylistIndex()] || slice.videoId,
      };
    }

    function onVideoError() {
      if (mix?.kind !== "youtube") return;
      if (!started) {
        load(++attempt);
      } else if (++errors <= MAX_LIVE_ERRORS) {
        advanceVideo(false);
      } else {
        fail();
      }
    }

    function boot() {
      if (ready || booting || destroyed) return;
      booting = true;
      void loadApi().then(YT => {
        booting = false;
        if (destroyed || !host) return;
        const target = document.createElement("iframe");
        const params = new URLSearchParams({ enablejsapi: "1", origin: location.origin, playsinline: "1", rel: "0" });
        target.title = "YouTube radio player";
        target.referrerPolicy = "strict-origin-when-cross-origin";
        target.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
        target.allowFullscreen = true;
        target.src = `https://www.youtube.com/embed/?${params}`;
        mount.replaceChildren(target);
        player = new YT.Player(target, {
          events: {
            onReady: () => {
              ready = true;
              player?.setVolume(slice.volume);
              if (slice.volume > 0) player?.unMute();
              if (mix?.kind === "youtube") load(0);
            },
            onStateChange: (event: { data: number }) => {
              if (destroyed || !player || mix?.kind !== "youtube") return;
              if (event.data === YT.PlayerState.PLAYING) {
                paused = false;
                started = true;
                clearTimer();
                errors = 0;
                const video = currentVideo(player);
                const seconds = player.getCurrentTime();
                slice.applyEnginePlaying({ ...video, position: Number.isFinite(seconds) && seconds > 0 && seconds <= 86400 ? seconds : undefined });
              } else if (event.data === YT.PlayerState.PAUSED) {
                paused = true;
                savePosition();
                slice.applyEnginePaused();
              } else if (event.data === YT.PlayerState.CUED) {
                const current = currentVideo(player);
                slice.selectVideo(current.videoId, current.title, current.channel);
              } else if (event.data === YT.PlayerState.ENDED) {
                advanceVideo(true);
              }
            },
            onAutoplayBlocked: () => {
              if (mix?.kind !== "youtube") return;
              paused = true;
              clearTimer();
              slice.applyEnginePaused();
            },
            onError: onVideoError,
          },
        });
      }).catch(() => {
        booting = false;
        if (mix?.kind === "youtube") fail();
      });
    }

    function playAudio() {
      const active = mix;
      const source = audio.src;
      clearTimer();
      timer = window.setTimeout(audioFailed, START_TIMEOUT_MS);
      selectPlaybackAudioSession();
      void audio.play().catch((error: unknown) => {
        if (mix !== active || audio.src !== source) return;
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (error instanceof DOMException && error.name === "NotAllowedError") {
          clearTimer();
          slice.applyEnginePaused(mix?.kind === "library" ? libraryOrder[libraryIndex] : undefined, audio.currentTime);
        } else {
          audioFailed();
        }
      });
    }

    function loadLibraryTrack(autoplay = true, position = 0) {
      const track = libraryOrder[libraryIndex];
      if (!track) return fail();
      clearTimer();
      audio.src = audioHref(track, store.compactAudio, store.sessionSlice.outputId);
      if (position > 0) audio.currentTime = position;
      if (autoplay) playAudio();
      else slice.applyEnginePaused(track, position);
    }

    function advanceLibrary(autoplay = true) {
      const previous = libraryOrder[libraryIndex];
      if (++libraryIndex === libraryOrder.length) {
        libraryOrder = shuffled(libraryOrder);
        if (libraryOrder.length > 1 && libraryOrder[0] === previous) {
          [libraryOrder[0], libraryOrder[1]] = [libraryOrder[1], libraryOrder[0]];
        }
        libraryIndex = 0;
      }
      loadLibraryTrack(autoplay);
    }

    function audioFailed() {
      if (mix?.kind === "library") {
        const track = libraryOrder[libraryIndex];
        if (store.compactAudio && track && audio.src === audioHref(track, true, store.sessionSlice.outputId)) {
          const position = audio.currentTime;
          audio.src = audioHref(track, false, store.sessionSlice.outputId);
          if (position > 0) audio.currentTime = position;
          playAudio();
        } else {
          libraryOrder.splice(libraryIndex, 1);
          if (!libraryOrder.length) return fail();
          if (libraryIndex === libraryOrder.length) {
            libraryOrder = shuffled(libraryOrder);
            libraryIndex = 0;
          }
          audio.loop = libraryOrder.length === 1;
          loadLibraryTrack();
        }
      } else if (mix && mix.kind !== "youtube") {
        fail();
      }
    }

    audio.addEventListener("playing", () => {
      if (!mix || mix.kind === "youtube") return;
      clearTimer();
      errors = 0;
      const track = mix.kind === "library" ? libraryOrder[libraryIndex] : undefined;
      slice.applyEnginePlaying({
        title: track?.title ?? (mix.kind === "live" ? mix.stream.title : mix.name),
        channel: track?.artist ?? (mix.kind === "live" ? "SomaFM" : "Generated on this device"),
        videoId: "",
        trackId: track?.id,
        position: track ? audio.currentTime : undefined,
      });
    });
    audio.addEventListener("pause", () => {
      if (mix && mix.kind !== "youtube" && audio.paused) {
        clearTimer();
        slice.applyEnginePaused(mix.kind === "library" && audio.readyState >= 1 ? libraryOrder[libraryIndex] : undefined,
          audio.currentTime);
      }
    });
    function savePosition(leaving = false) {
      if (mix?.kind === "youtube" && started && player) {
        const id = currentVideo(player).videoId;
        if (id) slice.checkpointPosition(id, player.getCurrentTime(), leaving);
      } else if (mix?.kind === "library" && audio.readyState >= 1) {
        const track = libraryOrder[libraryIndex];
        if (track) slice.checkpointPosition(track.id, audio.currentTime, leaving);
      }
    }

    // Audio's media events can continue while a minimized browser throttles timers.
    let lastAudioCheckpoint = 0;
    audio.addEventListener("timeupdate", () => {
      if (mix?.kind !== "library" || audio.paused) return;
      const now = performance.now();
      if (now - lastAudioCheckpoint < CHECKPOINT_MS) return;
      lastAudioCheckpoint = now;
      savePosition();
    });
    audio.addEventListener("seeked", () => {
      if (mix?.kind === "library" && audio.paused) savePosition();
    });
    const checkpointTimer = window.setInterval(() => {
      if (mix?.kind === "youtube" && (slice.status === "playing" || slice.status === "paused")) savePosition();
    }, CHECKPOINT_MS);
    const departing = () => savePosition(true);
    const visibilityChanged = () => { if (document.hidden) departing(); };
    document.addEventListener("visibilitychange", visibilityChanged);
    window.addEventListener("pagehide", departing);
    audio.addEventListener("ended", () => { if (mix?.kind === "library") advanceLibrary(); });
    audio.addEventListener("error", () => { if (audio.error) audioFailed(); });

    const controls: RadioControls = {
      checkpoint: () => savePosition(),
      libraryTime: id => mix?.kind === "library" && libraryOrder[libraryIndex]?.id === id ? audio.currentTime : null,
      seekLibrary: (id, seconds) => {
        if (mix?.kind === "library" && libraryOrder[libraryIndex]?.id === id && audio.readyState >= 1)
          audio.currentTime = seconds;
      },
      prepareLibrary: () => {
        if (audio.getAttribute("src")) return;
        selectPlaybackAudioSession();
        audio.loop = false;
        audio.src = silence;
        void audio.play().catch(() => {});
      },
      start: (next, first, position = 0) => {
        stop();
        mix = next;
        started = false;
        paused = false;
        errors = 0;
        attempt = 0;
        startSeconds = position;
        if (next.kind !== "youtube") {
          audio.loop = next.kind === "noise" || (next.kind === "library" && next.tracks.length === 1);
          audio.volume = slice.volume / 100;
          if (next.kind === "library") {
            libraryOrder = shuffled(next.tracks);
            const savedIndex = libraryOrder.findIndex(track => track.id === first);
            if (savedIndex > 0) [libraryOrder[0], libraryOrder[savedIndex]] = [libraryOrder[savedIndex], libraryOrder[0]];
            libraryIndex = 0;
            loadLibraryTrack(true, position);
            return;
          }
          if (next.kind === "noise") {
            noiseUrl = URL.createObjectURL(noiseAudio(next.profile));
            audio.src = noiseUrl;
          } else {
            audio.src = next.stream.url;
          }
          playAudio();
          return;
        }
        order = shuffled([...new Set(next.seeds)]);
        if (typeof first === "string") order = [first, ...order.filter(video => video !== first)];
        if (ready) load(0);
        else {
          if (order.length) slice.selectVideo(order[0]);
          timer = window.setTimeout(fail, START_TIMEOUT_MS + 3000);
          boot();
        }
      },
      toggle: () => {
        if (mix && mix.kind !== "youtube") {
          if (audio.paused) playAudio();
          else audio.pause();
        } else if (player && mix?.kind === "youtube") {
          paused = !paused && player.getPlayerState() === window.YT?.PlayerState.PLAYING;
          if (paused) {
            player.pauseVideo();
            slice.applyEnginePaused();
          } else {
            player.playVideo();
          }
        }
      },
      next: () => {
        if (mix?.kind === "library") advanceLibrary(!audio.paused);
        else advanceVideo(false);
      },
      previous: () => {
        if (mix?.kind === "youtube" && player) {
          if (paused) cue(Math.max(0, player.getPlaylistIndex() - 1));
          else player.previousVideo();
        }
        else if (mix?.kind === "library") {
          if (libraryIndex === 0) audio.currentTime = 0;
          else {
            libraryIndex--;
            loadLibraryTrack(!audio.paused);
          }
        }
      },
      retune: () => { if (mix) slice.startMixFromRadio(mix); },
      stop,
      setVolume: (value) => {
        audio.volume = value / 100;
        player?.setVolume(value);
        if (value > 0) player?.unMute();
      },
    };
    store.radioControls = controls;
    if (slice.pendingStart) {
      const pending = slice.pendingStart;
      slice.pendingStart = null;
      controls.start(pending.mix, pending.first, pending.position);
    }
    return () => {
      destroyed = true;
      window.clearInterval(checkpointTimer);
      document.removeEventListener("visibilitychange", visibilityChanged);
      window.removeEventListener("pagehide", departing);
      stop();
      player?.destroy();
      host?.replaceChildren();
      if (store.radioControls === controls) store.radioControls = null;
    };
  }, [store, hostRef]);
}
