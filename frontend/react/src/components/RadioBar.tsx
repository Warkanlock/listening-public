import {
  Slider,
  SliderControl,
  SliderIndicator,
  SliderThumb,
  SliderTrack,
} from "@ui-sdk/react/slider";
import { RadioSource } from "./RadioSource";
import { RoomIcon } from "./RoomIcon";
import { useStore } from "../store";

// The player bar while Radio sounds on this device (plan 059,
// grill round 3): the bar follows the sound, so it shows the
// video's title and station with radio's own transport and volume.
// Stopping hands the bar back to the session exactly as it was;
// the session was never touched.
export function RadioBar() {
  const store = useStore();
  const playing = store.radioStatus === "playing";
  return (
    <>
      <div className="player-track">
        <span className="radio-bar-badge" aria-hidden="true">
          <RoomIcon name="radio" />
        </span>
        <div className="player-text">
          <strong>
            {store.radioVideoTitle ||
              (store.radioStatus === "starting" ? "Tuning in…" : "Radio")}
          </strong>
          <RadioSource showStation />
        </div>
      </div>
      <div className="player-center">
        <div className="player-buttons">
          <button
            type="button"
            aria-label="Retune station"
            title="Retune: start a fresh mix"
            onClick={() => store.retuneRadio()}
          >
            <RoomIcon name="refresh" />
          </button>
          <button
            className="player-toggle"
            type="button"
            aria-label={playing ? "Pause radio" : "Play radio"}
            onClick={() => store.toggleRadio()}
          >
            <RoomIcon name={playing ? "pause" : "play"} />
          </button>
          {store.radioSlice.lyricTrack && <button type="button" className="radio-lyrics" aria-label="Show lyrics"
            title="Lyrics" onClick={() => store.openPlaybackLyrics("radio")}><RoomIcon name="lyrics" /></button>}
          {store.radioSlice.hasNext && <button
            type="button"
            aria-label="Next track"
            title="Next track"
            onClick={() => store.nextRadio()}
          >
            <RoomIcon name="next" />
          </button>}
          <button
            className="radio-stop"
            type="button"
            aria-label="Stop radio"
            title="Stop radio"
            onClick={() => store.stopRadio()}
          >
            <RoomIcon name="stop" />
          </button>
        </div>
      </div>
      <div className="player-extra">
        <span>Radio · {store.radioStationName ?? ""}</span>
        <span aria-hidden="true"><RoomIcon name="volume" /></span>
        <Slider
          value={store.radioVolume}
          min={0}
          max={100}
          step={1}
          onValueChange={(value: number) => store.setRadioVolume(value)}
        >
          <SliderControl>
            <SliderTrack>
              <SliderIndicator />
              <SliderThumb aria-label="Radio volume" />
            </SliderTrack>
          </SliderControl>
        </Slider>
      </div>
    </>
  );
}
