import { useEffect, useState } from "react";
import { Field, FieldLabel } from "@ui-sdk/react/field";
import { RoomSelect } from "../../components/RoomControls";
import { Switch, SwitchThumb } from "@ui-sdk/react/switch";
import { macRoomClient, usesSystemAudio } from "../../mediaOutput";
import { peekInstallPrompt, takeInstallPrompt, useStore } from "../../store";
import { notify } from "../../toasts";

export function PlaybackPanel() {
  const store = useStore();
  const [canInstall, setCanInstall] = useState(() => !!peekInstallPrompt());
  useEffect(() => {
    const update = () => setCanInstall(!!peekInstallPrompt());
    window.addEventListener("beforeinstallprompt", update);
    window.addEventListener("appinstalled", update);
    return () => {
      window.removeEventListener("beforeinstallprompt", update);
      window.removeEventListener("appinstalled", update);
    };
  }, []);
  const install = async () => {
    const prompt = takeInstallPrompt() as (Event & { prompt(): Promise<void> }) | null;
    setCanInstall(false);
    try {
      await prompt?.prompt();
    } catch {
      notify("To install this player, open your browser’s app menu.");
    }
  };
  return (
    <>
      <div className="settings-section-title"><div>
        <h2>Playback</h2>
        <p>These settings apply to this device.</p>
      </div></div>
      <section className="settings-card playback-card" aria-labelledby="audio-quality-heading">
        <h3 id="audio-quality-heading">Audio quality</h3>
        <Field name="mobile-audio" className="check-setting">
          <div>
            <FieldLabel>Compact mobile audio</FieldLabel>
            <p id="compact-audio-help" className="settings-note">Use smaller AAC streams on this device. Turn off to play the original files.</p>
          </div>
          <Switch aria-describedby="compact-audio-help" checked={store.compactAudio} onCheckedChange={checked => store.setCompactAudio(checked)}>
            <SwitchThumb />
          </Switch>
        </Field>
        {usesSystemAudio ? (
          <p className="settings-note">{macRoomClient ? "The Mac app uses the system player for playback." : "Background playback uses your device’s media player. Adjust volume with your device buttons."}</p>
        ) : (
          <Field name="normalize" className="check-setting">
            <div>
              <FieldLabel>Normalize volume</FieldLabel>
              <p id="normalize-help" className="settings-note">Use ReplayGain tags to even out differences in loudness.</p>
            </div>
            <Switch aria-describedby="normalize-help" checked={store.normalize} onCheckedChange={checked => store.setNormalize(checked)}>
              <SwitchThumb />
            </Switch>
          </Field>
        )}
      </section>
      <section className="settings-card playback-card" aria-labelledby="queue-heading">
        <h3 id="queue-heading">Playback queue</h3>
        <RoomSelect label="Buffer ahead" description="Keeps the current song and upcoming songs in memory, up to 128 MB. Switching outputs holds the buffer for five minutes."
          value={String(store.sessionSlice.bufferAhead)} onChange={value => store.sessionSlice.setBufferAhead(Number(value))}
          options={[{ value: "1", label: "1 upcoming song" }, { value: "2", label: "2 upcoming songs" }, { value: "3", label: "3 upcoming songs" }]} />
        {!usesSystemAudio && <RoomSelect label="Crossfade between songs" value={String(store.crossfade)}
          options={[{ value: "0", label: "Off" }, { value: "2", label: "2 seconds" }, { value: "5", label: "5 seconds" }]}
          onChange={value => store.setCrossfade(Number(value))} />}
      </section>
      {canInstall && (
        <div className="settings-actions">
          <button className="quiet-button" type="button" onClick={() => void install()}>Install web player</button>
        </div>
      )}
    </>
  );
}
