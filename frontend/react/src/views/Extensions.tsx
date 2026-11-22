import { useEffect, useState, type Ref } from "react";
import { Dialog, DialogBackdrop, DialogClose, DialogTitle, DialogTrigger } from "@ui-sdk/react/dialog";
import { isHost, type ExtensionSources } from "../api";
import { useStore } from "../store";
import { RoomDialogContent } from "../components/WindowContent";
import { ExtensionsPanel, PluginResources } from "./settings/ExtensionsPanel";
import { ExtensionIconAction, ExtensionInventory, ExtensionInventoryRow, retryTime } from "./settings/ExtensionInventory";

function sourceDescription(data: ExtensionSources) {
  const search = data.selection?.search;
  const fetch = data.selection?.fetch;
  if (data.state === "cooldown") {
    const time = retryTime(data.retry_at);
    return time ? `A source couldn’t sign in. Your requests stay queued. Find copy can retry after ${time}.` :
      "The sign-in pause has ended. Queued requests can retry.";
  }
  if (data.state === "ready" && search && fetch)
    return search === fetch ? `Find copy uses ${search} for search and download.` : `Find copy searches with ${search} and downloads with ${fetch}.`;
  if (data.state === "legacy") return "Find copy uses the room’s existing downloader.";
  if (data.state === "unavailable") {
    const disabled = data.installed.find(engine => !engine.enabled && (engine.id === search || engine.id === fetch));
    if (disabled) return `${disabled.id} is disabled. Find copy requests stay queued until the room owner enables a source.`;
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

function SourceStatus() {
  const sources = useSources();
  if (sources.sourceError) return <span role="status">{sources.sourceError}{" "}
    <button className="quiet-button" type="button" onClick={() => void sources.loadExtensionSources(true)}>Retry</button>
  </span>;
  return <span role="status">{sources.extensionSources ? sourceDescription(sources.extensionSources) : "Checking download sources…"}</span>;
}

function ExtensionsContent() {
  const store = useStore();
  const { extensionSources } = useSources();
  if (!store.settings) return store.settingsError ? <div role="status">
    Couldn’t check your access to room settings.{" "}
    <button className="quiet-button" type="button" onClick={() => void store.loadSettings()}>Retry</button>
  </div> : <p role="status">Loading room access…</p>;
  if (isHost(store.settings)) return <ExtensionsPanel />;
  return <section className="settings-card">
    <header className="settings-card-heading">
      <h2>Song downloads</h2>
      <ExtensionIconAction icon="refresh" label="Refresh extensions" onClick={() => void store.settingsSlice.loadExtensionSources(true)} />
    </header>
    <p><SourceStatus /></p>
    <p className="settings-note">The room owner installs extensions and chooses the sources used by all devices.</p>
    <PluginResources />
    {extensionSources && <>
      <h2>Installed extensions</h2>
      {extensionSources.installed.length ? <ExtensionInventory label="Installed extensions">
        {extensionSources.installed.map(engine => <ExtensionInventoryRow key={engine.id} engine={engine} />)}
      </ExtensionInventory> :
        <p>{!extensionSources.inventory_available ? "The installed list is unavailable." : "No download extensions installed."}</p>}
    </>}
  </section>;
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
    {open ? <button className="quiet-button" ref={buttonRef} type="button" disabled={disabled} onClick={open}>View extensions</button> :
      <Dialog open={shown} onOpenChange={setShown}>
        <DialogTrigger className="quiet-button">View extensions</DialogTrigger>
        {shown && <><DialogBackdrop /><RoomDialogContent className="extensions-dialog">
          <DialogTitle>Extensions</DialogTitle>
          <DialogClose className="dialog-close" aria-label="Close extensions">×</DialogClose>
          <ExtensionsContent />
        </RoomDialogContent></>}
      </Dialog>}
  </div>;
}

export { ExtensionsContent };
