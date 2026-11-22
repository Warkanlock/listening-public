import { useEffect, useState } from "react";
import { api, readable } from "../../api";

type Installer = {
  platform: "android" | "macos";
  name: string;
  architecture: string;
  signing: string;
} & ({ url: string; size: number; sha256: string } | { repair: string });

interface ClientRelease {
  version: string;
  origin: string;
  installers: Installer[];
}

export function ClientInstallers({ onPair }: { onPair: () => void }) {
  const [attempt, setAttempt] = useState(0);
  const [release, setRelease] = useState<ClientRelease>();
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const request = new AbortController();
    api<ClientRelease>("/api/setup/clients", { signal: request.signal })
      .then(setRelease)
      .catch(() => { if (!request.signal.aborted) setFailed(true); });
    return () => request.abort();
  }, [attempt]);

  const address = release?.origin || location.origin;
  return (
    <>
      <div className="settings-section-title"><div><h2>Connect a device</h2><p>Install a client, then enter your server address or open a pairing invitation.</p></div></div>
      <div className="settings-stack">
        <div className="settings-card">
          <h3>Server address</h3>
          <p className="lan-link">{address}</p>
          <button type="button" className="quiet-button" onClick={onPair}>Open pairing settings</button>
        </div>
        <div className="settings-card">
          <h3>iPhone or iPad</h3>
          <p>Open this address in Safari. Choose Share, then Add to Home Screen. Sign in or use a pairing invitation.</p>
          <p>Use the room’s trusted HTTPS address for saving and reopening Camping music offline.</p>
        </div>
        <div className="settings-card">
          <h3>Browser</h3>
          <p>Open the room’s address from a browser that can reach this server.</p>
        </div>
        {!release && <div className="settings-card">
          <h3>Android and Mac installers</h3>
          <p role="status">{failed ? "Couldn’t load this room’s installers." : "Checking the room’s release files…"}</p>
          {failed && <button type="button" className="quiet-button" onClick={() => {
            setFailed(false);
            setAttempt(value => value + 1);
          }}>Try again</button>}
        </div>}
        {release?.installers.map(installer => (
          <div className="settings-card" key={installer.platform}>
            <h3>{installer.platform === "android" ? "Android" : "Mac"}</h3>
            <p>{installer.architecture} · {installer.signing} · Version {release.version}</p>
            {"url" in installer ? <>
              <p>{readable(Math.round(installer.size / 1024))} KiB</p>
              <a className="quiet-button" href={installer.url} download>{installer.platform === "android" ? "Download APK" : "Download DMG"}</a>
              <details>
                <summary>SHA-256 checksum</summary>
                <p className="lan-link">{installer.sha256}</p>
              </details>
            </> : <p>{installer.repair}</p>}
          </div>
        ))}
      </div>
    </>
  );
}
