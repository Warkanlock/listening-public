import { useEffect, useState, type Ref } from "react";
import { Dialog, DialogBackdrop, DialogClose, DialogTitle, DialogTrigger } from "@ui-sdk/react/dialog";
import { ApiError, isHost, postJson, type ExtensionHealth, type ExtensionSources } from "../api";
import { useStore } from "../store";
import { RoomDialogContent } from "../components/WindowContent";
import { RoomIcon, RoomIconButton } from "../components/RoomIcon";
import { ExtensionsPanel, PluginResources } from "./settings/ExtensionsPanel";
import { ExtensionIconAction, ExtensionInventory, ExtensionInventoryRow, retryTime } from "./settings/ExtensionInventory";

function sourceDescription(data: ExtensionSources, showProviders: boolean) {
  const search = data.selection?.search;
  const fetch = data.selection?.fetch;
  if (data.state === "cooldown") {
    const time = retryTime(data.retry_at);
    return time ? `A source couldn’t sign in. Your requests stay queued. Find copy can retry after ${time}.` :
      "The sign-in pause has ended. Queued requests can retry.";
  }
  if (data.state === "ready" || data.state === "legacy") {
    if (showProviders && search && fetch)
      return `Search: ${search} · Download: ${fetch}`;
    return "Find copy uses the room’s configured download sources.";
  }
  if (data.state === "unavailable") {
    const disabled = data.installed.find(engine => !engine.enabled && (engine.id === search || engine.id === fetch));
    if (disabled) {
      const source = showProviders ? disabled.id : "A download source";
      return `${source} is disabled. Find copy requests stay queued until the room owner enables a source.`;
    }
    return "Download sources are unavailable. Check the enabled engines and their settings.";
  }
  return "The room owner can choose how to find and download missing songs.";
}

function useSources() {
  const { settingsSlice } = useStore();
  useEffect(() => {
    void settingsSlice.loadExtensionSources();
    const timer = window.setInterval(() => void settingsSlice.loadExtensionSources(), 35_000);
    return () => window.clearInterval(timer);
  }, [settingsSlice]);
  const deadline = settingsSlice.extensionSources?.retry_at;
  useEffect(() => {
    if (!deadline) return;
    const timer = window.setTimeout(() => void settingsSlice.loadExtensionSources(true), Math.max(1000, deadline * 1000 - Date.now() + 1000));
    return () => window.clearTimeout(timer);
  }, [deadline, settingsSlice]);
  return settingsSlice;
}

function SourceStatus({ showProviders = false }: { showProviders?: boolean }) {
  const sources = useSources();
  if (sources.sourceError) return <span role="status">{sources.sourceError}{" "}
    <RoomIconButton icon="refresh" label="Retry checking download sources" onClick={() => void sources.loadExtensionSources(true)} />
  </span>;
  return <span role="status">{sources.extensionSources ? sourceDescription(sources.extensionSources, showProviders) : "Checking download sources…"}</span>;
}

function ExtensionHealthCheck() {
  const { settingsSlice } = useStore();
  const [result, setResult] = useState<
    { kind: "idle" | "running" } | { kind: "checked"; health: ExtensionHealth } | { kind: "error"; message: string }
  >({ kind: "idle" });
  const check = async () => {
    setResult({ kind: "running" });
    try {
      const health = await postJson<ExtensionHealth>("/api/extensions/health", {});
      setResult({ kind: "checked", health });
      void settingsSlice.loadExtensionSources(true);
    } catch (error) {
      setResult({ kind: "error", message: error instanceof ApiError && error.status === 404
        ? "Update the room server to run a health check." : "Couldn’t run the health check. Try again shortly." });
    }
  };

  return <section className="settings-card">
    <header className="settings-card-heading">
      <h2>Extensions health check</h2>
      <ExtensionIconAction icon="check" label="Run extensions health check" disabled={result.kind === "running"}
        onClick={() => void check()} />
    </header>
    <div role="status" aria-busy={result.kind === "running"}>
      {result.kind === "idle" && <p>Check the sources Find copy uses.</p>}
      {result.kind === "running" && <p>Checking download sources…</p>}
      {result.kind === "error" && <p>{result.message}</p>}
      {result.kind === "checked" && <ul className="extension-health-results">
        {result.health.checks.map(({ label, state, message }) => <li key={label}>
          <strong>{label}</strong>
          <span>{state === "ready" ? "Passed" : state === "failed" ? "Needs attention" : "Unverified"} · {message}</span>
        </li>)}
      </ul>}
    </div>
  </section>;
}

function ExtensionsContent() {
  const store = useStore();
  const { extensionSources } = useSources();
  if (!store.settings) return store.settingsError ? <div role="status">
    Couldn’t check your access to room settings.{" "}
    <RoomIconButton icon="refresh" label="Retry checking room access" onClick={() => void store.loadSettings()} />
  </div> : <p role="status">Loading room access…</p>;
  return <div className="extensions-stack">
    <ExtensionHealthCheck />
    {isHost(store.settings) ? <ExtensionsPanel /> : <section className="settings-card">
    <header className="settings-card-heading">
      <h2>Song downloads</h2>
      <ExtensionIconAction icon="refresh" label="Refresh extensions" onClick={() => void store.settingsSlice.loadExtensionSources(true)} />
    </header>
    <p><SourceStatus showProviders /></p>
    <p className="settings-note">The room owner installs extensions and chooses the sources used by all devices.</p>
    <PluginResources />
    {extensionSources && <>
      <h2>Installed extensions</h2>
      {extensionSources.installed.length ? <ExtensionInventory label="Installed extensions">
        {extensionSources.installed.map(engine => <ExtensionInventoryRow key={engine.id} engine={engine} />)}
      </ExtensionInventory> :
        <p>{!extensionSources.inventory_available ? "The installed list is unavailable." : "No download extensions installed."}</p>}
    </>}
    </section>}
  </div>;
}

export function Extensions() {
  return <section className="view" data-view="extensions">
    <header className="listening-view-head"><div><h1>Extensions</h1><p>Manage song downloads and Radio providers.</p></div></header>
    <ExtensionsContent />
  </section>;
}

// The request form can reuse its dialog while keeping its chosen format in memory.
export function FindCopySources({ open, buttonRef, disabled }: {
  open?: () => void; buttonRef?: Ref<HTMLButtonElement>; disabled?: boolean;
}) {
  const [shown, setShown] = useState(false);
  return <div className="find-copy-sources">
    <SourceStatus />
    {open ? <RoomIconButton icon="extensions" label="View extensions" ref={buttonRef} disabled={disabled} onClick={open} /> :
      <Dialog open={shown} onOpenChange={setShown}>
        <DialogTrigger className="quiet-button icon-control" aria-label="View extensions" title="View extensions">
          <RoomIcon name="extensions" />
        </DialogTrigger>
        {shown && <><DialogBackdrop /><RoomDialogContent className="extensions-dialog">
          <DialogTitle>Extensions</DialogTitle>
          <DialogClose className="dialog-close icon-control" aria-label="Close extensions" title="Close extensions"><RoomIcon name="close" /></DialogClose>
          <ExtensionsContent />
        </RoomDialogContent></>}
      </Dialog>}
  </div>;
}

export { ExtensionsContent };
