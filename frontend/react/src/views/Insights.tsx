import { useEffect, useState } from "react";
import { api, fileSize, readable, time } from "../api";
import { useStore } from "../store";

type TrafficSample = { download_bytes: number; upload_bytes: number };
type PlaybackTraffic = TrafficSample & { window_seconds: number; history?: TrafficSample[] };
type TrafficReading = { kind: "ready"; payload: PlaybackTraffic } | { kind: "error" };
const rateText = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

function usePlaybackTraffic(output: string, connected: boolean) {
  const [reading, setReading] = useState<TrafficReading | null>(null);
  useEffect(() => {
    setReading(null);
    if (!connected) return;
    let pending: AbortController | undefined;
    let timer: number | undefined;

    const cancel = () => {
      window.clearTimeout(timer);
      pending?.abort();
      pending = undefined;
    };
    const sample = async () => {
      if (document.hidden) return;
      const request = new AbortController();
      pending = request;
      const timeout = window.setTimeout(() => request.abort(), 4000);
      try {
        const payload = await api<PlaybackTraffic>("/api/insights", {
          signal: request.signal, headers: { "X-Listening-Output": output },
        });
        if (pending === request) setReading({ kind: "ready", payload });
      } catch {
        if (pending === request) setReading({ kind: "error" });
      } finally {
        window.clearTimeout(timeout);
        if (pending === request) timer = window.setTimeout(() => void sample(), 1000);
      }
    };
    const visibility = () => {
      cancel();
      void sample();
    };
    void sample();
    document.addEventListener("visibilitychange", visibility);
    return () => {
      cancel();
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [output, connected]);
  return connected ? reading : null;
}

export function Insights({ connected }: { connected: boolean }) {
  const store = useStore();
  const reading = usePlaybackTraffic(store.sessionSlice.outputId, connected);
  const latest = reading?.kind === "ready" ? reading.payload : null;
  const idle = latest && latest.download_bytes === 0 && latest.upload_bytes === 0;
  const session = store.session;
  const queueLength = session?.queue_length;
  const preview = (session?.upcoming ?? []).filter(item => item !== null).slice(0, 5);
  const buffered = store.sessionSlice.buffer.ready;
  const external = store.radioSounding && (store.radioSlice.kind === "live" || store.radioSlice.kind === "youtube");

  return <section className="view" aria-labelledby="insights-heading">
    <header className="listening-view-head">
      <div>
        <h1 id="insights-heading">Insights</h1>
        <p>Playback traffic for this device and what’s coming up in the room queue.</p>
      </div>
    </header>
    <section className="insights-section" aria-labelledby="insights-traffic">
      <div className="insights-section-header">
        <h2 id="insights-traffic">Playback traffic</h2>
        {latest && <span>Last {latest.window_seconds} seconds</span>}
      </div>
      <div className="insights-rates">
        {(["download_bytes", "upload_bytes"] as const).map(direction => {
          const history = latest?.history;
          const series = (history ?? []).map(sample => sample[direction]);
          const peak = Math.max(1, ...series);
          const rate = latest ? latest[direction] * 8 / (1000 * latest.window_seconds) : undefined;
          const bytes = series.reduce((total, sample) => total + sample, 0);
          const down = direction === "download_bytes";
          return <article className="insights-rate" key={direction}>
            <h3>{down ? "Down from room" : "Up to room"}</h3>
            <p className="insights-value">
              {rate === undefined ? "—" : rateText.format(rate >= 100 ? Math.round(rate) : rate)} <small>kb/s</small>
            </p>
            <p>{down ? "Audio streams and buffering" : "Playback commands"}</p>
            {history && <p>Last minute · {bytes < 1024 ? `${bytes} B` : fileSize(bytes)}</p>}
            <div className="insights-graph" aria-hidden="true">
              {series.map((value, index) => <i key={index} style={{ height: Math.max(4, value / peak * 100) + "%" }} />)}
            </div>
          </article>;
        })}
      </div>
      {reading?.kind === "error" && <p className="insights-note">Couldn’t read playback traffic from the room.</p>}
      {external && <p className="insights-note">This station streams directly from its provider. Its traffic bypasses the room.</p>}
      {idle && !external && <p className="insights-note">
        No playback transfers in the last {latest.window_seconds} seconds. Buffered audio can keep playing between transfers.
      </p>}
    </section>
    <section className="insights-section" aria-labelledby="insights-queue">
      <div className="insights-section-header">
        <h2 id="insights-queue">Up next</h2>
        {queueLength !== undefined && <strong>{readable(queueLength)} {queueLength === 1 ? "song" : "songs"} queued</strong>}
      </div>
      <p>{buffered} {buffered === 1 ? "song" : "songs"} buffered on this device.</p>
      {preview.length > 0 && <ol className="insights-tracks">
        {preview.map((track, index) => <li key={index}>
          <div><strong>{track.title}</strong><span>{track.artist}</span></div>
          {track.duration > 0 && <span>{time(track.duration)}</span>}
        </li>)}
      </ol>}
      {queueLength === 0 && <p>The queue is empty.</p>}
      {queueLength === undefined && <p>The room queue will appear when a playback session is available.</p>}
    </section>
  </section>;
}
