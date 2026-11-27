import { RoomIcon } from "../components/RoomIcon";
import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/figtree";
import recording from "./first-light.mp3";
import sleeve from "./first-light.webp";
import { enableFeatureSearch } from "./feature-search";
import { enableDockerCopy, enableInstallTabs } from "./install-tabs";

function Audition() {
  const player = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState(false);
  async function toggle() {
    if (!player.current) return;
    setError(false);
    if (player.current.paused) {
      try { await player.current.play(); } catch { setError(true); }
    } else player.current.pause();
  }
  return <>
    <audio ref={player} src={recording} preload="none" onPlay={() => setPlaying(true)}
      onPause={() => setPlaying(false)} onEnded={() => setPlaying(false)} />
    <button className="about-play" type="button" onClick={() => void toggle()}>
      <span aria-hidden="true"><RoomIcon name={playing ? "pause" : "play"} /></span> {playing ? "Pause demo" : "Play demo"}
    </button>
    {error && <small role="status">Audio unavailable. Try again.</small>}
  </>;
}

const scenes = ["Device handoff", "Artist credits", "Camping mode"] as const;
type Scene = typeof scenes[number];
const credits = [
  ["Paul Desmond", "Composer · alto saxophone"],
  ["Dave Brubeck", "Piano"],
  ["Joe Morello", "Drums"],
  ["Eugene Wright", "Bass"],
] as const;

function Preview() {
  const [scene, select] = useState<Scene>("Device handoff");
  const [phone, setPhone] = useState(false);
  const [credit, setCredit] = useState(0);
  const [album, showAlbum] = useState(false);
  const [packed, pack] = useState(false);
  const [size, setSize] = useState(30);
  return <div className="about-preview">
    <div className="about-switches" role="group" aria-label="Player previews">
      {scenes.map((name) => <button key={name} aria-pressed={scene === name} onClick={() => select(name)}>{name}</button>)}
    </div>
    <div className="about-scene" key={scene}>
      {scene === "Device handoff" && <>
        <div className="about-scene-heading"><small>Playback preview</small><h3>From laptop to phone.</h3>
          <p>The room remembers where a library track stopped, including a song on Library Radio. Live stations return to their current broadcast.</p></div>
        <div className="about-mini" data-device={phone ? "phone" : "laptop"}><img src={sleeve} alt="" width="76" height="76" /><div><strong>First light</strong><span>Listening Room</span></div>
          <small aria-live="polite">{phone ? "Phone" : "Laptop"} · 0:04 / 0:08</small><meter min={0} max={8} value={4} aria-label="Saved playback position, four seconds" /></div>
        <button className="about-demo-action" onClick={() => setPhone(!phone)}>Resume on {phone ? "laptop" : "phone"} <RoomIcon name="forward" /></button>
      </>}
      {scene === "Artist credits" && <>
        <div className="about-scene-heading"><small>You know what?</small><h3>{album ? "Time Out" : "Who played on Take Five?"}</h3>
          <p>{album ? "The Dave Brubeck Quartet · 1959. Take Five appears on this album." : <>Explore a credit from the Dave Brubeck Quartet’s <i>Time Out</i>.</>}</p></div>
        {!album && <div className="about-options" role="group" aria-label="Recording credits">{credits.map(([name], index) =>
          <button key={name} aria-pressed={index === credit} onClick={() => setCredit(index)}>{name}</button>)}</div>}
        <p className="about-result" aria-live="polite"><b>{credits[credit][0]}</b> · {credits[credit][1]}</p>
        <button className="about-demo-action" onClick={() => showAlbum(!album)}>{album ? <><RoomIcon name="back" /> Back to Take Five</> : <>Follow credit to Time Out <RoomIcon name="forward" /></>}</button>
        <a className="about-source" href="https://musicbrainz.org/search?query=Time+Out+Dave+Brubeck+Quartet&type=release_group&method=indexed">Recording credits <RoomIcon name="northeast" /></a>
      </>}
      {scene === "Camping mode" && <>
        <div className="about-scene-heading"><small>Offline mix preview</small><h3>Pack a few records.</h3>
          <p>Choose how many songs to bring. In your room, Camping mode builds shuffled mixes from available tracks and saves them on this device.</p></div>
        <div className="about-options" role="group" aria-label="Trip size">{[30, 60, 90].map((count) =>
          <button key={count} aria-pressed={size === count} onClick={() => { setSize(count); pack(false); }}>{count} songs</button>)}</div>
        <p className="about-result" aria-live="polite">{packed ? "Saved trip preview" : "Trip preview"} · up to <b>{size} songs</b></p>
        <button className="about-demo-action" onClick={() => pack(!packed)}>{packed ? "Remove preview mix" : <>Pack preview mix <RoomIcon name="forward" /></>}</button>
      </>}
    </div>
  </div>;
}

function Tour() {
  useEffect(() => {
    const stage = document.querySelector<HTMLElement>(".about-platter");
    if (!stage) return;
    const allowed = matchMedia("(hover: hover) and (prefers-reduced-motion: no-preference)");
    function tilt(event: PointerEvent) {
      if (!allowed.matches || !stage) return;
      const rect = stage.getBoundingClientRect();
      stage.style.setProperty("--turn", ((event.clientX - rect.left) / rect.width * 5 - 2.5) + "deg");
    }
    function reset() { stage?.style.removeProperty("--turn"); }
    stage.addEventListener("pointermove", tilt);
    stage.addEventListener("pointerleave", reset);
    allowed.addEventListener("change", reset);
    return () => {
      stage.removeEventListener("pointermove", tilt);
      stage.removeEventListener("pointerleave", reset);
      allowed.removeEventListener("change", reset);
      reset();
    };
  }, []);
  return <Preview />;
}

const audition = document.getElementById("audition");
const preview = document.getElementById("tour-preview");
if (audition) createRoot(audition).render(<Audition />);
if (preview) createRoot(preview).render(<Tour />);

enableFeatureSearch();
enableInstallTabs();
enableDockerCopy();
