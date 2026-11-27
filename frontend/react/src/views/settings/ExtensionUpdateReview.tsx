import { useEffect, useState } from "react";
import { Dialog, DialogBackdrop, DialogDescription, DialogTitle } from "@ui-sdk/react/dialog";
import { ApiError, postJson, type ExtensionCapability, type ExtensionRole } from "../../api";
import { RoomAction } from "../../components/RoomControls";
import { RoomIconButton } from "../../components/RoomIcon";
import { RoomDialogContent } from "../../components/WindowContent";
import type { Extensions } from "./ExtensionsPanel";

type Setting = { key: string; label: string; type: "text" | "secret"; required: boolean };
export type ExtensionPackage = {
  id: string; version: string; image: string; capabilities: ExtensionCapability[]; server_versions: string[];
  sources: Partial<Record<ExtensionRole, string[]>>; settings?: Setting[];
};

type Preview = {
  approval: string;
  installed: ExtensionPackage;
  proposed: ExtensionPackage;
  missing_settings: string[];
  requires_disable: boolean;
  incompatible_sources: boolean;
};

type Review =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; preview: Preview };

function changes({ installed, proposed }: Preview) {
  const details: { label: string; description: string }[] = [];
  if (installed.image !== proposed.image) {
    details.push({ label: "Worker image", description: "A new worker image will be installed." });
  }
  const sharedBefore = installed.capabilities.includes("share");
  if (sharedBefore !== proposed.capabilities.includes("share")) {
    details.push({ label: "Library sharing", description: sharedBefore ? "Removes sharing support." : "Adds sharing support." });
  }
  for (const role of ["search", "fetch"] as const) {
    const before = installed.sources?.[role] ?? [];
    const after = proposed.sources?.[role] ?? [];
    const added = after.filter(source => !before.includes(source));
    const removed = before.filter(source => !after.includes(source));
    if (added.length || removed.length) {
      const summary = [added.length ? `Adds ${added.join(", ")}.` : "",
        removed.length ? `Removes ${removed.join(", ")}.` : ""].filter(Boolean).join(" ");
      details.push({ label: role === "search" ? "Search sources" : "Download sources", description: summary });
    }
  }
  const fields = new Map((installed.settings ?? []).map(field => [field.key, field]));
  for (const field of proposed.settings ?? []) {
    const before = fields.get(field.key);
    fields.delete(field.key);
    if (!before || before.label !== field.label || before.type !== field.type || before.required !== field.required) {
      const state = field.required ? "Required" : "Optional";
      const kind = field.type === "secret" ? "secret" : "text";
      const previous = before && before.label !== field.label ? ` Previously ${before.label}.` : "";
      details.push({ label: field.label, description: `${state} ${kind} setting.${previous}` });
    }
  }
  for (const field of fields.values()) {
    details.push({ label: field.label, description: "Removed setting." });
  }
  if ((installed.server_versions ?? []).join() !== proposed.server_versions.join()) {
    details.push({ label: "Compatible server versions", description: proposed.server_versions.join(", ") });
  }
  return details;
}

export function ExtensionUpdateReview({ engine, supported, close, onUpdated }: {
  engine: string;
  supported: boolean;
  close: () => void;
  onUpdated: (extensions: Extensions) => void;
}) {
  const [review, setReview] = useState<Review>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const reload = () => {
    setReview({ kind: "loading" });
    setAttempt(value => value + 1);
  };
  useEffect(() => {
    if (!supported) return;
    const controller = new AbortController();
    void postJson<Preview>("/api/extensions", { action: "update_preview", engine }, controller.signal)
      .then(preview => {
        if (!controller.signal.aborted) setReview({ kind: "ready", preview });
      }, (failure: unknown) => {
        if (!controller.signal.aborted) setReview({ kind: "error",
          message: failure instanceof Error ? failure.message : "Could not review this update." });
      });
    return () => controller.abort();
  }, [attempt, engine, supported]);

  async function confirm(preview: Preview) {
    setSaving(true);
    setNotice("");
    try {
      const result = await postJson<Extensions>("/api/extensions", {
        action: "update_reviewed", engine, approval: preview.approval,
      });
      onUpdated(result);
    } catch (failure) {
      setNotice(failure instanceof Error ? failure.message : "Could not update this extension.");
      if (failure instanceof ApiError && failure.status === 409) reload();
    } finally {
      setSaving(false);
    }
  }

  const preview = supported && review.kind === "ready" ? review.preview : null;
  return <Dialog open onOpenChange={open => { if (!open && !saving) close(); }}>
    <DialogBackdrop />
    <RoomDialogContent className="format-dialog extension-update-dialog">
      <header className="format-dialog-header">
        <DialogTitle>Update {engine}</DialogTitle>
        <RoomIconButton icon="close" label="Close update review" disabled={saving} onClick={close} />
      </header>
      <DialogDescription>Review the changes in the signed catalog before updating.</DialogDescription>
      {!supported && <p role="status">Update the host engine manager to review extension updates.</p>}
      {supported && review.kind === "loading" && <p role="status">Checking the signed catalog…</p>}
      {notice && <p role="alert">{notice}</p>}
      {supported && review.kind === "error" && <p role="alert">{review.message}</p>}
      {preview && <>
        <dl className="extension-update-changes">
          <div><dt>Version</dt><dd>{preview.installed.version} → {preview.proposed.version}</dd></div>
          {changes(preview).map(({ label, description }, index) =>
            <div key={index}><dt>{label}</dt><dd>{description}</dd></div>)}
        </dl>
        {preview.incompatible_sources &&
          <p role="status">Choose compatible download sources in Extensions before updating.</p>}
        {preview.missing_settings.length > 0 && <p role="status">
          {preview.requires_disable ? "Disable this extension before updating. " : ""}
          After updating, enter {preview.missing_settings.join(", ")}, check setup, and enable the extension.
        </p>}
      </>}
      <footer className="dialog-actions">
        <RoomAction onClick={close} disabled={saving}>Close</RoomAction>
        {supported && review.kind === "error" && <RoomAction onClick={reload}>Check again</RoomAction>}
        {preview && <RoomAction primary disabled={saving || preview.requires_disable || preview.incompatible_sources}
          onClick={() => void confirm(preview)}>{saving ? "Updating…" : "Update extension"}</RoomAction>}
      </footer>
    </RoomDialogContent>
  </Dialog>;
}
