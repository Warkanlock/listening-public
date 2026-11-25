import { RoomDialogContent } from "./WindowContent";
import { ExtensionsContent, FindCopySources } from "../views/Extensions";
import { useState, useEffect, useRef } from "react";
import { Dialog, DialogBackdrop, DialogDescription, DialogTitle } from "@ui-sdk/react/dialog";
import { postJson, REQUEST_FORMATS, time, type CopyMatch, type RequestFormat, type RequestRow } from "../api";
import { useStore } from "../store";
import { notify } from "../toasts";
import { RoomIcon, RoomIconButton } from "./RoomIcon";
import { RoomAction } from "./RoomControls";

type BrowseState = { kind: "idle" | "searching" } |
  { kind: "results"; matches: CopyMatch[] } | { kind: "error"; message: string };

export function RequestFormatPicker({ row }: { row: RequestRow }) {
  const store = useStore();
  const [choice, setChoice] = useState<RequestFormat>(row.requested_format ?? "any_lossless");
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
  const [browse, setBrowse] = useState<BrowseState>({ kind: "idle" });
  const browsing = browse.kind !== "idle";
  const browseBack = useRef<HTMLButtonElement>(null);
  const browseButton = useRef<HTMLButtonElement>(null);
  const restoreBrowseFocus = useRef(false);
  const listing = useRef<AbortController>(null);
  useEffect(() => () => listing.current?.abort(), []);
  useEffect(() => {
    if (browsing) browseBack.current?.focus();
    else if (restoreBrowseFocus.current) {
      browseButton.current?.focus();
      restoreBrowseFocus.current = false;
    }
  }, [browsing]);
  const close = () => store.requestsSlice.chooseFormat(null);
  const fields = { request_id: row.id, requested_format: choice, format_revision: row.format_revision };
  const leaveMatches = () => {
    listing.current?.abort();
    restoreBrowseFocus.current = true;
    setBrowse({ kind: "idle" });
    setError("");
  };
  const showMatches = async () => {
    listing.current?.abort();
    const controller = new AbortController();
    listing.current = controller;
    setError("");
    setBrowse({ kind: "searching" });
    try {
      const { matches } = await postJson<{ matches: CopyMatch[] }>("/api/requests/copies", fields, controller.signal);
      if (!controller.signal.aborted) setBrowse({ kind: "results", matches });
    } catch (error) {
      if (!controller.signal.aborted) setBrowse({ kind: "error",
        message: error instanceof Error ? error.message : "Could not search for copies." });
    }
  };
  const confirm = async (copyKey?: string) => {
    if (saving) return;
    setSaving(true);
    setError("");
    try {
      await postJson("/api/requests/format", {
        ...fields, ...(copyKey ? { copy_key: copyKey } : {}),
      });
      close();
      notify(copyKey ? `Copy queued for ${row.title}` : `Format confirmed for ${row.title}`);
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
    else if (browse.kind !== "idle") leaveMatches();
    else close();
  };
  const dialogClass = showExtensions ? "extensions-dialog" : "format-dialog";
  return (
    <Dialog open onOpenChange={open => { if (!open) dismiss(); }}>
      <DialogBackdrop />
      <RoomDialogContent className={dialogClass}>
        {showExtensions ? <>
          <header className="format-dialog-header">
            <RoomIconButton ref={back} icon="back" label="Back to format" onClick={() => setShowExtensions(false)} />
            <DialogTitle>Extensions</DialogTitle>
          </header>
          <ExtensionsContent />
        </> : browsing ? <>
          <header className="format-dialog-header">
            <RoomIconButton ref={browseBack} icon="back" label="Back to format" disabled={saving} onClick={leaveMatches} />
            <DialogTitle>Choose a copy</DialogTitle>
          </header>
          <DialogDescription>{[row.title, row.artist].filter(Boolean).join(" · ")}</DialogDescription>
          {browse.kind === "searching" && <p role="status">Searching for copies…</p>}
          {browse.kind === "error" && <p role="alert">{browse.message}</p>}
          {browse.kind === "results" && (browse.matches.length ? <div aria-label="Available copies">
            {browse.matches.map(match => {
              const facts = [match.format?.toUpperCase(), match.duration ? time(match.duration) : "",
                match.bitrate ? `${match.bitrate} kbps` : "",
                match.recommended === true ? "Recommended" : match.recommended === false ? "Check match" : ""].filter(Boolean);
              return <button type="button" className="radio-result" key={match.key} disabled={saving}
                onClick={() => void confirm(match.key)}>
                <span><strong>{match.label}</strong>{facts.length > 0 && <small>{facts.join(" · ")}</small>}</span>
                <RoomIcon name="download" />
              </button>;
            })}
          </div> : <p role="status">No copies found in this format.</p>)}
          {error && <p role="alert">{error}</p>}
          {(browse.kind === "error" || browse.kind === "results" && !browse.matches.length) &&
            <RoomAction icon="refresh" onClick={() => void showMatches()}>Search again</RoomAction>}
        </> : <>
        <header className="format-dialog-header">
          <DialogTitle>Choose format</DialogTitle>
          <RoomIconButton icon="close" label="Later" disabled={saving} onClick={close} />
        </header>
        <DialogDescription>{[row.title, row.artist].filter(Boolean).join(" · ")}</DialogDescription>
        <fieldset disabled={saving}>
          <legend>Audio format to search for</legend>
          {REQUEST_FORMATS.map(({ key, label }) => (
            <label key={key}>
              <input type="radio" name="requested-format" checked={choice === key}
                onChange={() => setChoice(key)} />
              {label}
            </label>
          ))}
        </fieldset>
        <FindCopySources disabled={saving} buttonRef={sourceReturn} open={() => {
          restoreSourceFocus.current = true;
          setShowExtensions(true);
        }} />
        {error && <p role="alert">{error}</p>}
        <div className="format-dialog-footer">
          <RoomAction ref={browseButton} icon="catalogs" disabled={saving} onClick={() => void showMatches()}>Browse copies</RoomAction>
          <button className="primary-action" type="button" disabled={saving} onClick={() => void confirm()}>
            <RoomIcon name="search" />
            {saving ? "Confirming…" : "Confirm and search"}
          </button>
        </div>
        </>}
      </RoomDialogContent>
    </Dialog>
  );
}
