import { useEffect, useId, useState, type FormEvent } from "react";
import { Switch, SwitchThumb } from "@ui-sdk/react/switch";
import { RoomAction, RoomConfirm, RoomSelect } from "../../components/RoomControls";
import { RoomIcon, type RoomIconName } from "../../components/RoomIcon";
import { api, postJson } from "../../api";
import { notify } from "../../toasts";
import { useStore } from "../../store";
import { ExtensionIconAction, ExtensionInventory, ExtensionInventoryRow, retryTime } from "./ExtensionInventory";
import { ExtensionUpdateReview, type ExtensionPackage, type ExtensionRole } from "./ExtensionUpdateReview";

type Role = ExtensionRole;
type Engine = ExtensionPackage & {
  enabled?: boolean; ready?: boolean; retry_at?: number;
  previous?: unknown; configured_settings?: string[];
};
export type Extensions = {
  radio: string[]; installed: Engine[]; available: Engine[];
  configured: boolean; error: string; selection: Partial<Record<Role, string>> | null; reviewed_updates?: boolean;
};

function packageMetadata({ version, image, server_versions, capabilities, sources, settings }: Engine) {
  return JSON.stringify([version, image, server_versions, capabilities, sources, settings]);
}

export function PluginResources() {
  return <div className="extension-resources">
    <a href="https://github.com/Warkanlock/listening-public/blob/main/docs/download-engines.md" target="_blank" rel="noopener noreferrer">
      Plugin setup guide <RoomIcon name="northeast" />
    </a>
    <a href="https://github.com/Warkanlock/listening-plugins-public" target="_blank" rel="noopener noreferrer">
      Listening Plugins on GitHub <RoomIcon name="northeast" />
    </a>
  </div>;
}

const radioProviders = [
  { id: "youtube", name: "YouTube mixes", description: "Build mixes from video links you choose." },
  { id: "somafm", name: "SomaFM", description: "Tune into SomaFM’s live stations." },
];

function EngineSettings({ engine, busy, save }: {
  engine: Engine; busy: boolean; save: (settings: Record<string, string>) => Promise<boolean>;
}) {
  if (!engine.settings?.length) return null;
  const fields = engine.settings;
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    const updates = Object.fromEntries(fields.flatMap(({ key }) => {
      const value = values.get(key);
      return typeof value === "string" && value.trim() ? [[key, value]] : [];
    }));
    if (Object.keys(updates).length && await save(updates)) form.reset();
  }

  return <form className="settings-form extension-config" aria-label={`Settings for ${engine.id}`} onSubmit={event => void submit(event)}>
    <p className="settings-note">Leave a field blank to keep its existing value. Required settings are checked when the engine starts.</p>
    {fields.map(field => {
      const saved = engine.configured_settings?.includes(field.key) ?? false;
      return <label key={field.key}>
        {field.label}{field.required ? " (required to start)" : ""}
        <input name={field.key} type={field.type === "secret" ? "password" : "text"}
          autoComplete={field.type === "secret" ? "new-password" : "off"}
          placeholder={saved ? "Saved · leave blank to keep" : undefined}
          maxLength={512} disabled={busy} />
      </label>;
    })}
    <div className="settings-actions">
      <RoomAction primary type="submit" disabled={busy}>Save engine settings</RoomAction>
    </div>
  </form>;
}

export function ExtensionsPanel() {
  const store = useStore();
  const settingsId = useId();
  const [configuring, setConfiguring] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const [reviewing, setReviewing] = useState<string | null>(null);
  const [extensions, setExtensions] = useState<Extensions>();
  const [selection, setSelection] = useState<Partial<Record<Role, string>>>({});
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const busy = saving || loading;
  const [attempt, setAttempt] = useState(0);
  const [error, setError] = useState("");
  const [clock, setClock] = useState(Date.now);
  const nextRetry = Math.min(...(extensions?.installed ?? [])
    .map(engine => (engine.retry_at ?? 0) * 1000)
    .filter(deadline => deadline > clock));
  useEffect(() => {
    if (!Number.isFinite(nextRetry)) return;
    const timer = window.setTimeout(() => setClock(Date.now()), Math.max(0, nextRetry - Date.now()) + 100);
    return () => window.clearTimeout(timer);
  }, [nextRetry]);
  const accept = (result: Extensions) => {
    setExtensions(result);
    setSelection(result.selection ?? {});
  };
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    void api<Extensions>("/api/extensions", { signal: controller.signal, cache: "no-store" }).then(result => {
      if (!controller.signal.aborted) accept(result);
    }).catch((failure: Error) => {
      if (!controller.signal.aborted) setError(failure.message);
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [attempt]);
  const refresh = () => {
    setLoading(true);
    setAttempt(value => value + 1);
    void store.settingsSlice.loadExtensionSources(true);
  };
  const change = async (action: string, fields: Record<string, unknown>) => {
    setSaving(true);
    setError("");
    try {
      accept(await postJson<Extensions>("/api/extensions", { action, ...fields }));
      void store.settingsSlice.loadExtensionSources(true);
      if (action === "radio") await store.loadStations();
      notify(action === "test" ? "Engine setup verified." : "Extension settings saved.");
      return true;
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The extension action failed.");
      // A sign-in failure can change the source's retry time even when the action fails.
      await Promise.allSettled([
        api<Extensions>("/api/extensions", { cache: "no-store" }).then(setExtensions),
        store.settingsSlice.loadExtensionSources(true),
      ]);
      return false;
    } finally {
      setSaving(false);
    }
  };
  if (!extensions) return <section className="settings-card">
    <p role="status">{error || "Loading extensions…"}</p>
    {error && <RoomAction disabled={busy} onClick={refresh}>Retry</RoomAction>}
    <PluginResources />
  </section>;

  const action = (name: string, icon: RoomIconName, label: string, engine: string, blocked = false) =>
    <ExtensionIconAction icon={icon} label={`${label} ${engine}`} disabled={busy || blocked}
      onClick={() => void change(name, { engine })} />;
  const active = extensions.selection;
  const catalog = extensions.available.filter(engine => !extensions.installed.some(entry => entry.id === engine.id));
  const selectedEngines = extensions.installed.filter(engine => engine.id === active?.search || engine.id === active?.fetch);
  const ready = extensions.installed.filter(engine => engine.ready);
  const search = ready.find(engine => engine.id === selection.search);
  const fetch = ready.find(engine => engine.id === selection.fetch);
  const compatible = !!search?.sources?.search?.length && !!fetch &&
    search.sources.search.every(source => fetch.sources?.fetch?.includes(source));
  const hasSelection = !!(active?.search && active.fetch);
  const unavailable = hasSelection && (selectedEngines.some(engine => !engine.ready) ||
    !extensions.installed.some(engine => engine.id === active.search) ||
    !extensions.installed.some(engine => engine.id === active.fetch));
  const waiting = selectedEngines.some(engine => !!retryTime(engine.retry_at));

  return <div className="extensions-stack" aria-busy={busy}>
    <section className="settings-card">
      <h2>Song downloads</h2>
      {error && <p className="extension-notice" role="alert">{error}</p>}
      {extensions.error && <p className="extension-notice" role="alert">{extensions.error}</p>}
      <p>Choose how Listening finds and downloads missing songs.</p>
      {active === null ? <p>The room uses its existing downloader.</p> : hasSelection ?
        <div className="extension-source-summary">
          <div><span>Search with</span><strong>{active.search}</strong></div>
          <div><span>Download with</span><strong>{active.fetch}</strong></div>
        </div> : null}
      {hasSelection && (waiting || unavailable) && <p className="extension-notice" role="status">
        {unavailable ? "Your requests will wait until the selected extensions are available." :
          "Your requests will wait while a download extension tries to sign in."}
      </p>}
      {(ready.length > 0 || hasSelection) && <details className="extension-settings" open={!hasSelection}>
        <summary><RoomIcon name="chevronDown" /> Change download sources</summary>
        {ready.length > 0 && <>
          <div className="extension-source-picker">
            {(["search", "fetch"] as const).map(role => {
              const options = ready.filter(engine => engine.capabilities.includes(role))
                .map(engine => ({ value: engine.id, label: engine.id }));
              const selected = selection[role];
              if (selected && !options.some(option => option.value === selected)) {
                options.unshift({ value: selected, label: `${selected} (unavailable)` });
              }
              return <RoomSelect key={role} label={role === "search" ? "Search with" : "Download with"}
                value={selected || ""} disabled={busy}
                options={[{ value: "", label: "Choose an extension" }, ...options]}
                onChange={value => setSelection(current => ({ ...current, [role]: value }))} />;
            })}
          </div>
          {selection.search && selection.fetch && search && fetch && !compatible &&
            <p className="settings-note" role="status">Choose a download extension that accepts results from your search extension.</p>}
        </>}
        <div className="settings-actions">
          {ready.length > 0 && <RoomAction primary disabled={busy || !compatible ||
            (selection.search === active?.search && selection.fetch === active?.fetch)}
            onClick={async () => { await change("select", { selection }); }}>Save sources</RoomAction>}
          {hasSelection && <RoomAction disabled={busy} onClick={async () => { await change("select", { selection: {} }); }}>
            Clear sources
          </RoomAction>}
        </div>
      </details>}
    </section>
    <section className="settings-card">
      <header className="settings-card-heading">
        <h2>Download extensions</h2>
        <ExtensionIconAction icon="refresh" label="Refresh extensions" disabled={busy} onClick={refresh} />
      </header>
      {!extensions.configured && <p>Set up the host engine manager to install download extensions.</p>}
      {extensions.configured && !extensions.error && !extensions.installed.length && !catalog.length &&
        <p>No compatible download extensions are listed in this room’s catalog.</p>}
      {(extensions.installed.length > 0 || catalog.length > 0) && <ExtensionInventory label="Download extensions" manage>
        {extensions.installed.map(engine => {
          const retry = retryTime(engine.retry_at);
          const update = extensions.available.find(entry => entry.id === engine.id && packageMetadata(entry) !== packageMetadata(engine));
          const formId = `${settingsId}-${engine.id}`;
          return <ExtensionInventoryRow key={engine.id} engine={engine} settingsId={formId}
            settingsOpen={configuring === engine.id} actions={<>
              {action(engine.enabled ? "disable" : "enable", engine.enabled ? "pause" : "power",
                engine.enabled ? "Disable" : "Enable", engine.id, !engine.enabled && !!retry)}
              {action("test", "check", "Check setup for", engine.id, !!retry)}
              {!!engine.settings?.length && <ExtensionIconAction icon="settings" label={`Configure ${engine.id}`}
                disabled={busy} aria-expanded={configuring === engine.id} aria-controls={formId}
                onClick={() => setConfiguring(value => value === engine.id ? null : engine.id)} />}
              {update && <ExtensionIconAction icon="up" label={`Update ${engine.id}`}
                disabled={busy || (engine.enabled && !!retry && update.image !== engine.image)}
                onClick={() => setReviewing(engine.id)} />}
              {engine.previous ? action("rollback", "undo", "Roll back", engine.id, engine.enabled && !!retry) : null}
              <ExtensionIconAction icon="trash" label={`Remove ${engine.id}`} disabled={busy}
                onClick={() => setRemoving(engine.id)} />
            </>}>
            {!!engine.settings?.length && <EngineSettings engine={engine} busy={busy}
              save={settings => change("configure", { engine: engine.id, settings })} />}
          </ExtensionInventoryRow>;
        })}
        {catalog.map(engine => <ExtensionInventoryRow key={engine.id} engine={engine} available
          actions={action("install", "download", "Install", engine.id)} />)}
      </ExtensionInventory>}
      <PluginResources />
    </section>
    <section className="settings-card">
      <h2>Radio providers</h2>
      {radioProviders.map(provider => <RadioProviderRow key={provider.id} provider={provider} busy={busy}
        enabled={extensions.radio.includes(provider.id)}
        onChange={enabled => void change("radio", { provider: provider.id, enabled })} />)}
      {(store.view === "extensions" || store.view === "settings") &&
        <div className="settings-actions"><RoomAction icon="settings" onClick={() => store.openRadioSettings()}>
          Manage SomaFM stations
        </RoomAction></div>}
    </section>
    {removing && <RoomConfirm open title={`Remove ${removing}?`}
      description="This stops the extension and removes it from this room’s installed list."
      confirmLabel="Remove extension" onClose={() => setRemoving(null)}
      onConfirm={() => void change("remove", { engine: removing })} />}
    {reviewing && <ExtensionUpdateReview key={reviewing} engine={reviewing} supported={extensions.reviewed_updates === true}
      close={() => setReviewing(null)} onUpdated={result => {
        accept(result);
        setReviewing(null);
        void store.settingsSlice.loadExtensionSources(true);
        notify("Extension updated.");
      }} />}
  </div>;
}

function RadioProviderRow({ provider, enabled, busy, onChange }: {
  provider: typeof radioProviders[number]; enabled: boolean; busy: boolean; onChange: (enabled: boolean) => void;
}) {
  const caption = useId();
  return <label className="extension-row extension-radio">
    <span><strong>{provider.name}</strong><span id={caption}>{provider.description}</span></span>
    <Switch aria-label={provider.name} aria-describedby={caption} checked={enabled} disabled={busy} onCheckedChange={onChange}>
      <SwitchThumb />
    </Switch>
  </label>;
}
