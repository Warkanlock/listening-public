import { RoomDialogContent } from "./WindowContent";
import { ExtensionsContent, FindCopySources } from "../views/Extensions";
import { useState, useEffect, useRef } from "react";
import { Dialog, DialogBackdrop, DialogDescription, DialogTitle } from "@ui-sdk/react/dialog";
import { postJson, REQUEST_FORMATS, type RequestRow } from "../api";
import { useStore } from "../store";
import { notify } from "../toasts";

export function RequestFormatPicker({ row }: { row: RequestRow }) {
  const store = useStore();
  type LosslessFormat = keyof typeof REQUEST_FORMATS;
  const [choice, setChoice] = useState<LosslessFormat>(
    row.requested_format === "mp3" ? "any_lossless" : row.requested_format ?? "any_lossless",
  );
  const [showExtensions, setShowExtensions] = useState(false);
  const sourceReturn = useRef<HTMLButtonElement>(null);
  const restoreSourceFocus = useRef(false);
  const back = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (showExtensions) back.current?.focus();
    else if (restoreSourceFocus.current) sourceReturn.current?.focus();
  }, [showExtensions]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const close = () => store.requestsSlice.chooseFormat(null);
  const confirm = async () => {
    if (saving) return;
    setSaving(true);
    setError("");
    try {
      await postJson("/api/requests/format", {
        request_id: row.id, requested_format: choice, format_revision: row.format_revision,
      });
      close();
      notify(`Format confirmed for ${row.title}`);
      void store.loadRequests();
      if (store.view === "all" || store.view === "moods") void store.loadTracks(true);
    } catch (error) {
      setError(error instanceof Error ? error.message : "Could not confirm the format.");
      setSaving(false);
      void store.loadRequests();
    }
  };
  const dismiss = () => {
    if (saving) return;
    if (showExtensions) setShowExtensions(false);
    else close();
  };
  const dialogClass = showExtensions ? "extensions-dialog" : "format-dialog";
  return (
    <Dialog open onOpenChange={open => { if (!open) dismiss(); }}>
      <DialogBackdrop />
      <RoomDialogContent className={dialogClass}>
        {showExtensions ? <>
          <DialogTitle>Extensions</DialogTitle>
          <button ref={back} className="quiet-button" type="button" onClick={() => setShowExtensions(false)}>Back to format</button>
          <ExtensionsContent />
        </> : <>
        <DialogTitle>Choose format</DialogTitle>
        <DialogDescription>{[row.title, row.artist].filter(Boolean).join(" · ")}</DialogDescription>
        <fieldset disabled={saving}>
          <legend>Audio format to search for</legend>
          {(Object.keys(REQUEST_FORMATS) as LosslessFormat[]).map(format => (
            <label key={format}>
              <input type="radio" name="requested-format" checked={choice === format}
                onChange={() => setChoice(format)} />
              {REQUEST_FORMATS[format]}
            </label>
          ))}
        </fieldset>
        <FindCopySources disabled={saving} buttonRef={sourceReturn} open={() => {
          restoreSourceFocus.current = true;
          setShowExtensions(true);
        }} />
        {error && <p role="alert">{error}</p>}
        <div className="settings-actions">
          <button className="quiet-button" type="button" disabled={saving} onClick={close}>Later</button>
          <button className="primary-action" type="button" disabled={saving} onClick={() => void confirm()}>
            {saving ? "Confirming…" : "Confirm and search"}
          </button>
        </div>
        </>}
      </RoomDialogContent>
    </Dialog>
  );
}
