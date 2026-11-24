import { useEffect, useState } from "react";
import { api, fileSize } from "../../api";
import { RoomAction } from "../../components/RoomControls";
import { RoomIcon } from "../../components/RoomIcon";

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
        <section className="settings-card client-connect-row" aria-labelledby="client-address-heading">
          <div>
            <h3 id="client-address-heading">Server address</h3>
            <p className="lan-link">{address}</p>
          </div>
          <RoomAction onClick={onPair}>Open pairing settings</RoomAction>
        </section>
        <section className="settings-card" aria-labelledby="client-installers-heading">
          <h3 id="client-installers-heading">App installers</h3>
          {release ? (
            <ul className="client-installer-list" aria-label="App installers">
              {release.installers.map(installer => {
                const android = installer.platform === "android";
                const platform = android ? "Android" : "Mac";
                const format = android ? "APK" : "DMG";
                const available = "url" in installer;
                return (
                  <li className="client-installer-row" key={installer.platform}>
                    <span className="client-installer-icon"><RoomIcon name={android ? "devices" : "outputs"} /></span>
                    <div className="client-installer-info">
                      <strong>{platform}</strong>
                      <div className="client-installer-meta">
                        {available ? <>
                          <span>{!android && `${installer.architecture} · `}{installer.signing} · {release.version} · {fileSize(installer.size)}</span>
                          <details>
                            <summary aria-label={`SHA-256 checksum for ${platform}`}>SHA-256 checksum</summary>
                            <code>{installer.sha256}</code>
                          </details>
                        </> : <span>{installer.repair}</span>}
                      </div>
                    </div>
                    {available && (
                      <a className="room-action quiet-button client-installer-download" href={installer.url}
                        download aria-label={`Download ${format}`}>
                        <RoomIcon name="download" /><span><span>Download </span>{format}</span>
                      </a>
                    )}
                  </li>
                );
              })}
            </ul>
          ) : (
            <>
              <p role="status">{failed ? "Couldn’t load this room’s installers." : "Checking the room’s release files…"}</p>
              {failed && <RoomAction onClick={() => {
                setFailed(false);
                setAttempt(value => value + 1);
              }}>Try again</RoomAction>}
            </>
          )}
        </section>
        <div className="client-browser-options">
          <section className="settings-card" aria-labelledby="client-ios-heading">
            <h3 id="client-ios-heading">iPhone or iPad</h3>
            <p>Open the server address in Safari, then choose Share → Add to Home Screen. Use the room’s trusted HTTPS address for offline Camping music.</p>
          </section>
          <section className="settings-card" aria-labelledby="client-browser-heading">
            <h3 id="client-browser-heading">Browser</h3>
            <p>Open the server address from a browser that can reach the room.</p>
          </section>
        </div>
      </div>
    </>
  );
}
