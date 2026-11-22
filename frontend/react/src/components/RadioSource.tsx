import { time } from "../api";
import { useStore } from "../store";
import { RoomIcon } from "./RoomIcon";

export function RadioSource({ showStation = false }: { showStation?: boolean }) {
  const store = useStore();
  const radio = store.radioSlice;
  const source = radio.source;
  if (!source) return null;

  let provider;
  switch (source.kind) {
    case "youtube": {
      const id = radio.videoId;
      const href = () => {
        if (!id) return `https://www.youtube.com/results?search_query=${encodeURIComponent(radio.videoTitle || source.name)}`;
        const seconds = Math.floor(radio.savedPosition);
        return `https://www.youtube.com/watch?v=${encodeURIComponent(id)}${seconds > 0 ? `&t=${seconds}s` : ""}`;
      };
      provider = <a href={href()} target="_blank" rel="noopener noreferrer"
        onClick={event => {
          if (!id) return;
          if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) radio.controls?.checkpoint();
          else radio.parkYouTube();
          event.currentTarget.href = href();
        }}>
        {id ? "Open in YouTube" : "Find on YouTube"} <RoomIcon name="northeast" />
      </a>;
      break;
    }
    case "live":
      provider = <a href={source.stream.url} target="_blank" rel="noopener noreferrer" aria-label="Open SomaFM stream">
        SomaFM <RoomIcon name="northeast" />
      </a>;
      break;
    case "library": {
      const track = radio.lyricTrack;
      provider = track ? <button type="button" aria-label="Open song in your library"
        onClick={event => store.openSongInfo(track.id, "details", event.currentTarget)}>Your library</button> : "Your library";
      break;
    }
    case "noise":
      provider = showStation ? "This device" : "Generated on this device";
      break;
  }
  const artist = source.kind === "library" || source.kind === "youtube" ? radio.videoChannel : "";
  const resume = radio.status === "paused" && radio.savedPosition >= 1 ? `Resume at ${time(radio.savedPosition)}` : "";
  const detail = [resume, showStation && source.name, artist].filter(Boolean).join(" · ");
  return <div className="radio-source">
    {provider}
    {detail && <small>· {detail}</small>}
  </div>;
}
