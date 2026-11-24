import { RadioSource } from "../components/RadioSource";
import { RoomIcon } from "../components/RoomIcon";
import { useEffect, useRef, useState } from "react";
import { Button } from "@ui-sdk/react/button";
import {
  Slider,
  SliderControl,
  SliderIndicator,
  SliderThumb,
  SliderTrack,
} from "@ui-sdk/react/slider";
import { useStore } from "../store";
import type { NoiseProfile } from "../radioNoise";
import { useRadioEngine } from "../radioEngine";
import { isHost, type LiveRadio } from "../api";
import { StateCard } from "../components/StateCard";
import { RoomAction } from "../components/RoomControls";

type Choice =
  | { kind: "library"; name: string; count: number }
  | { kind: "live"; name: string; stream: LiveRadio }
  | { kind: "noise"; name: string; profile: NoiseProfile }
  | { kind: "youtube"; name: string; count: number };

const noise: Choice[] = [
  { kind: "noise", name: "White noise", profile: "white" },
  { kind: "noise", name: "Brown noise", profile: "brown" },
];
const resultLimit = 8;

function description(choice: Choice) {
  switch (choice.kind) {
    case "library": return `Your library · ${choice.count} ${choice.count === 1 ? "song" : "songs"}`;
    case "live": return `SomaFM · ${choice.stream.title}`;
    case "noise": return "Generated on this device";
    case "youtube": return `YouTube · ${choice.count} ${choice.count === 1 ? "video" : "videos"}`;
  }
}

export function Radio() {
  const store = useStore();
  const hostRef = useRef<HTMLDivElement>(null);
  useRadioEngine(store, hostRef);
  const [filter, setFilter] = useState("");
  const [showLive, setShowLive] = useState(false);
  const radio = store.radioSlice;
  const background = store.view !== "radio";
  const sounding = store.radioSounding;
  const status = store.radioStatus;
  const currentName = store.radioStationName;
  useEffect(() => {
    void radio.refreshHistory();
    if (!background) return () => radio.cancelLibraryTune();
  }, [background, radio]);

  const audible = sounding && status !== "paused";
  useEffect(() => {
    if (!audible) return;
    const timer = window.setInterval(() => radio.refreshPresence(), 20000);
    return () => {
      window.clearInterval(timer);
      void radio.signalPresence(null);
    };
  }, [audible, radio]);

  function play(choice: Choice) {
    if (choice.kind === "library" || choice.kind === "youtube") return void radio.playStation(choice.kind, choice.name);
    if (choice.kind === "live") return void radio.startLive(choice.name);
    if (choice.kind === "noise") return radio.startNoise(choice.name, choice.profile);
  }

  const query = filter.trim().toLowerCase();
  const live = Object.entries(radio.live).map(([name, stream]): Choice => ({ kind: "live", name, stream }));
  const library = Object.entries(radio.libraryGenres).map(([name, count]): Choice => ({ kind: "library", name, count }));
  const saved = radio.stations.filter(station => station.seeds.length > 0)
    .map(({ name, seeds }): Extract<Choice, { kind: "youtube" }> => ({ kind: "youtube", name, count: seeds.length }));
  const videoCount = saved.reduce((total, topic) => total + topic.count, 0);
  const matches = (choice: Choice) => choice.name.toLowerCase().includes(query) ||
    ((choice.kind === "live" || choice.kind === "youtube") && description(choice).toLowerCase().includes(query));
  const rank = (choice: Choice) => {
    const name = choice.name.toLowerCase();
    return name === query ? 0 : name.startsWith(query) ? 1 : 2;
  };
  const byRelevance = (a: Choice, b: Choice) => rank(a) - rank(b) || a.name.localeCompare(b.name);
  const otherMatches = query ? [...library, ...live, ...noise].filter(matches).sort(byRelevance) : [];
  const mixResults = query ? saved.filter(matches).sort(byRelevance) : saved;
  const otherResults = otherMatches.slice(0, resultLimit);
  const canManage = isHost(store.settings);

  function stationList(choices: Choice[], label: string, mixes = false) {
    return (
      <ul className={`radio-results${mixes ? " radio-mix-results" : ""}`} aria-label={label}>
        {choices.map(choice => (
          <li key={`${choice.kind}:${choice.name}`}>
            <button type="button" className="radio-result"
              onClick={() => void play(choice)}
              aria-label={`Play ${choice.name} · ${description(choice)}`}>
              <span><strong>{choice.name}</strong><small>{description(choice)}</small></span>
              <span aria-hidden="true"><RoomIcon name="play" /></span>
            </button>
          </li>
        ))}
      </ul>
    );
  }

  return (
    <div className={`view radio-view${background ? " is-background" : ""}`}>
      {!background && (
        <header className="listening-view-head">
          <div>
            <h1>Radio</h1>
            <p>Shuffle a genre from your library or tune into an enabled station.</p>
          </div>
          {canManage && <RoomAction icon="settings" onClick={() => store.openRadioSettings()}>Manage SomaFM</RoomAction>}
        </header>
      )}
      <div
        className="radio-stage"
        hidden={!sounding || (background && radio.kind !== "youtube")}
        data-video={radio.kind === "youtube" && radio.hasNext}
      >
        <div className="radio-backdrop" ref={hostRef} />
        {background && sounding && <button type="button" className="radio-return"
          onClick={() => store.setView("radio")}>Open Radio</button>}
        {!background && sounding && (
          <div className="radio-stage-copy">
            <span className="radio-stage-station">{currentName}</span>
            <strong>{store.radioVideoTitle || (status === "starting" ? "Tuning in…" : "Playing station")}</strong>
            <RadioSource />
          </div>
        )}
      </div>
      {background ? null : (
        <div>
          {!store.stationsLoaded && !store.stationsError ? (
            <StateCard
              kind="loading"
              icon="radio"
              title="Loading stations"
              body="Reading the station list from your room."
            />
          ) : store.stationsError ? (
            <StateCard
              kind="error"
              icon="radio"
              title="Stations unavailable"
              body={store.stationsError}
              action={
                <Button type="button" onClick={() => void store.loadStations()}>
                  Try again
                </Button>
              }
            />
          ) : (
            <>
              {status === "failed" ? (
                <p className="radio-failed" role="alert">
                  {store.radioFailedStation} could not play on this device. Try another station.
                </p>
              ) : null}

              {sounding ? (
                <div className="radio-controls">
                  <div className="player-buttons">
                    <button
                      type="button"
                      aria-label="Retune station"
                      title="Start a fresh station mix"
                      onClick={() => store.retuneRadio()}
                    >
                      <RoomIcon name="refresh" />
                    </button>
                    <button
                      className="player-toggle"
                      type="button"
                      aria-label={status === "playing" ? "Pause radio" : "Play radio"}
                      onClick={() => store.toggleRadio()}
                    >
                      <RoomIcon name={status === "playing" ? "pause" : "play"} />
                    </button>
                    {radio.hasNext && <button
                      type="button"
                      aria-label="Next track"
                      title="Next track"
                      onClick={() => store.nextRadio()}
                    >
                      <RoomIcon name="next" />
                    </button>}
                    <button
                      type="button"
                      aria-label="Stop radio"
                      title="Stop radio"
                      onClick={() => store.stopRadio()}
                    >
                      <RoomIcon name="stop" />
                    </button>
                  </div>
                  <div className="radio-volume">
                    <span aria-hidden="true"><RoomIcon name="volume" /></span>
                    <Slider
                      value={store.radioVolume}
                      min={0}
                      max={100}
                      step={1}
                      onValueChange={(value: number) =>
                        store.setRadioVolume(value)
                      }
                    >
                      <SliderControl>
                        <SliderTrack>
                          <SliderIndicator />
                          <SliderThumb aria-label="Radio volume" />
                        </SliderTrack>
                      </SliderControl>
                    </Slider>
                  </div>
                </div>
              ) : null}
              {sounding && radio.kind === "youtube" && (
                <details className="radio-background-help">
                  <summary>Playing while minimized</summary>
                  <p>For a floating video, use picture-in-picture if your browser offers it. Open in YouTube
                    continues from this timestamp. YouTube manages playback and listening history there.</p>
                </details>
              )}
              <form className="radio-filters" onSubmit={event => {
                event.preventDefault();
                if (!query) return;
                const first = mixResults[0] ?? otherResults[0];
                if (first) void play(first);
              }}>
                <label>Find a station
                  <input type="search" value={filter} onChange={event => {
                    radio.cancelLibraryTune();
                    setFilter(event.currentTarget.value);
                  }} placeholder="Search genres, mixes, or live stations" />
                </label>
              </form>
              {radio.tuningGenre && <p role="status">Loading songs for {radio.tuningGenre}…</p>}
              {radio.tuneError && <p className="radio-failed" role="alert">{radio.tuneError}</p>}
              {!query && (canManage || radio.somafmEnabled || live.length > 0) && (
                <section aria-labelledby="radio-live-heading">
                  <div className="radio-source-heading">
                    <h2 id="radio-live-heading">SomaFM live</h2>
                  </div>
                  {live.length > 0 ? stationList(showLive ? live : live.slice(0, 4), "Live stations") :
                    <p className="settings-note">{radio.somafmEnabled ? "No live stations selected." : "SomaFM is turned off."}</p>}
                  {live.length > 4 && <RoomAction icon={showLive ? "chevronUp" : "chevronDown"} aria-expanded={showLive}
                    onClick={() => setShowLive(previous => !previous)}>
                    {showLive ? "Show fewer stations" : `Show all ${live.length} live stations`}
                  </RoomAction>}
                </section>
              )}
              {mixResults.length > 0 && (
                <section aria-labelledby="radio-mix-heading">
                  <div className="radio-source-heading">
                    <h2 id="radio-mix-heading">YouTube mixes</h2>
                    <span>{saved.length} {saved.length === 1 ? "topic" : "topics"} · {videoCount} {videoCount === 1 ? "video" : "videos"}</span>
                  </div>
                  {stationList(mixResults, "YouTube mixes", true)}
                </section>
              )}
              {!query && library.length > 0 && (
                <section aria-labelledby="radio-library-heading">
                  <div className="radio-source-heading"><h2 id="radio-library-heading">Library Radio</h2></div>
                  {stationList(library.slice(0, 2), "Library Radio")}
                </section>
              )}
              {!query && <section aria-labelledby="radio-noise-heading">
                <div className="radio-source-heading"><h2 id="radio-noise-heading">Background noise</h2></div>
                {stationList(noise, "Background noise")}
              </section>}
              {otherResults.length > 0 && (
                <section aria-labelledby="radio-stations-heading">
                  <div className="radio-source-heading">
                    <h2 id="radio-stations-heading">Matching stations and genres</h2>
                  </div>
                  {stationList(otherResults, "Matching stations")}
                </section>
              )}
              {query && !mixResults.length && !otherResults.length &&
                <p role="status">No matching stations or playable genres.</p>}
              {query && otherMatches.length > resultLimit &&
                <p role="status">Showing the first {resultLimit} other stations. Keep typing to narrow the results.</p>}
            </>
          )}
        </div>
      )}
    </div>
  );
}
