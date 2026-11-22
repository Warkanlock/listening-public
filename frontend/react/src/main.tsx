import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { RoomLogin } from "./components/RoomLogin";
import { isOfflineTripHash, offlineAccount, offlineHeadingHash } from "./hikingStorage";
import { OfflineHiking } from "./views/Hiking";
import "@fontsource-variable/figtree";
import "./styles.css";

if (import.meta.env.BASE_URL === "/") {
  navigator.serviceWorker?.register("/service-worker.js").catch(() => {});
}

function openSavedTrip() {
  if (!offlineAccount()) return false;
  location.replace(offlineHeadingHash);
  return true;
}

function RoomRoot() {
  const [offline, setOffline] = useState(isOfflineTripHash(location.hash));
  useEffect(() => {
    const sync = () => setOffline(isOfflineTripHash(location.hash));
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);
  return offline ? <OfflineHiking /> : <RoomLogin onUnavailable={openSavedTrip}>
    {role => <App role={role} />}
  </RoomLogin>;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RoomRoot />
  </StrictMode>,
);
