import { RoomDialogContent } from "./WindowContent";
import { ExtensionsContent, FindCopySources } from "../views/Extensions";
import { useState, useEffect, useRef } from "react";
import { Dialog, DialogBackdrop, DialogDescription, DialogTitle } from "@ui-sdk/react/dialog";
import { postJson, REQUEST_FORMATS, time, type CopyMatch, type RequestFormat, type RequestRow, type RequestSong } from "../api";
import { useStore } from "../store";
import { notify } from "../toasts";
import { RoomIcon, RoomIconButton } from "./RoomIcon";
import { RoomAction } from "./RoomControls";
import { needsCopyChoice, requestStage } from "../requestState";
import { RecordingChoices } from "./RecordingVersions";

type BrowseState = { kind: "format" } |
  { kind: "searching"; format: RequestFormat; fresh: boolean } |
  { kind: "results"; format: RequestFormat; matches: CopyMatch[] } |
  { kind: "error"; format: RequestFormat; message: string };

export function RequestFormatPicker({ row }: { row: RequestRow }) {
  const store = useStore();
  const [choice, setChoice] = useState<RequestFormat>(row.requested_format ?? "any_lossless");
  const [panel, setPanel] = useState<"picker" | "extensions" | "recordings">("picker");
  const sourceReturn = useRef<HTMLButtonElement>(null);
  const restoreSourceFocus = useRef(false);
  const recordingsReturn = useRef<HTMLButtonElement>(null);
  const restoreRecordingsFocus = useRef(false);
  const back = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (panel !== "picker") back.current?.focus();
    else if (restoreRecordingsFocus.current) {
      recordingsReturn.current?.focus();
      restoreRecordingsFocus.current = false;
    } else if (restoreSourceFocus.current) {
      sourceReturn.current?.focus();
      restoreSourceFocus.current = false;
    }
  }, [panel]);
  const [saving, setSaving] = useState(false);
  const confirming = useRef(false);
  const [error, setError] = useState("");
  const [recordingError, setRecordingError] = useState("");
  const review = needsCopyChoice(requestStage(row));
  const [browse, setBrowse] = useState<BrowseState>(() => review
    ? { kind: "searching", format: choice, fresh: false } : { kind: "format" });
  const browsing = browse.kind !== "format";
  const search = browse.kind === "searching" ? browse : null;
  const browseBack = useRef<HTMLButtonElement>(null);
  const browseButton = useRef<HTMLButtonElement>(null);
  const restoreBrowseFocus = useRef(false);
  useEffect(() => {
    if (!search) return;
    const controller = new AbortController();
    const { format, fresh } = search;
    void postJson<{ matches: CopyMatch[] }>("/api/requests/copies", {
      request_id: row.id, requested_format: format, format_revision: row.format_revision, fresh,
    }, controller.signal).then(({ matches }) => {
      if (!controller.signal.aborted) setBrowse({ kind: "results", format, matches });
    }, error => {
      if (!controller.signal.aborted) setBrowse({ kind: "error", format,
        message: error instanceof Error ? error.message : "Could not search for copies." });
    });
    return () => controller.abort();
  }, [search, row.id, row.format_revision]);
  useEffect(() => {
    if (browsing) browseBack.current?.focus();
    else if (restoreBrowseFocus.current) {
      browseButton.current?.focus();
      restoreBrowseFocus.current = false;
    }
  }, [browsing]);
  const close = () => store.requestsSlice.chooseFormat(null);
  const leaveMatches = () => {
    restoreBrowseFocus.current = true;
    setBrowse({ kind: "format" });
    setError("");
  };
  const showMatches = (format: RequestFormat, fresh = false) => {
    setError("");
    setBrowse({ kind: "searching", format, fresh });
  };
  const submit = async (action: () => Promise<RequestRow>, success: (updated: RequestRow) => void, errorMessage: string) => {
    if (confirming.current) return;
    confirming.current = true;
    setSaving(true);
    setError("");
    try {
      const updated = await action();
      store.requestsSlice.applyRequest(updated);
      close();
      success(updated);
      void store.loadRequests();
      if (store.view === "all" || store.view === "moods") void store.loadTracks(true);
    } catch (error) {
      setError(error instanceof Error ? error.message : errorMessage);
      confirming.current = false;
      setSaving(false);
      void store.loadRequests();
    }
  };
  const confirm = (format: RequestFormat, copyKey?: string) => submit(
    () => postJson<RequestRow>("/api/requests/format", {
      request_id: row.id, requested_format: format, format_revision: row.format_revision,
      ...(copyKey ? { copy_key: copyKey } : {}),
    }),
    updated => {
      if (updated.status === "queued") notify(copyKey ? `Copy queued for ${row.title}` : `Search queued for ${row.title}`);
    },
    "Could not confirm the format.",
  );
  const stopSearch = () => submit(
    () => postJson<RequestRow>("/api/requests/stop-search", {
      request_id: row.id, format_revision: row.format_revision,
    }),
    () => notify(`Marked ${row.title} unavailable`),
    "Could not stop the search.",
  );
  const dismiss = () => {
    if (saving) return;
    if (panel !== "picker") setPanel("picker");
    else close();
  };
  const changeRecording = async (song: RequestSong) => {
    if (confirming.current) return;
    confirming.current = true;
    setSaving(true);
    setRecordingError("");
    const updated = await store.requestsSlice.chooseRecording(row, song, setRecordingError);
    if (updated && requestStage(updated) !== "choose_format") close();
    confirming.current = false;
    setSaving(false);
  };
  const dialogClass = panel === "extensions" ? "extensions-dialog" : "format-dialog";
  return (
    <Dialog open onOpenChange={open => { if (!open) dismiss(); }}>
      <DialogBackdrop />
      <RoomDialogContent className={dialogClass}>
        {panel === "extensions" ? <>
          <header className="format-dialog-header">
            <RoomIconButton ref={back} icon="back" label="Back to format" onClick={() => setPanel("picker")} />
            <DialogTitle>Extensions</DialogTitle>
          </header>
          <ExtensionsContent />
        </> : panel === "recordings" ? <>
          <header className="format-dialog-header">
            <RoomIconButton ref={back} icon="back" label="Back to copies" disabled={saving} onClick={dismiss} />
            <DialogTitle>Other recordings</DialogTitle>
            <RoomIconButton icon="close" label="Close recording choices" disabled={saving} onClick={close} />
          </header>
          <DialogDescription>{[row.title, row.artist].filter(Boolean).join(" · ")}</DialogDescription>
          {saving && <p role="status">Changing recording…</p>}
          {recordingError && <p role="alert">{recordingError}</p>}
          <RecordingChoices song={row} disabled={saving} onChoose={song => void changeRecording(song)} />
        </> : browsing ? <>
          <header className="format-dialog-header">
            <DialogTitle>Choose a copy</DialogTitle>
            <RoomIconButton ref={browseBack} icon="close" label="Close copy choices" disabled={saving} onClick={close} />
          </header>
          <DialogDescription>{[row.title, row.artist].filter(Boolean).join(" · ")}</DialogDescription>
          {browse.kind === "searching" && <p role="status">Searching for copies…</p>}
          {browse.kind === "error" && <p role="alert">{browse.message}</p>}
          {browse.kind === "results" && (browse.matches.length ? <div className="copy-picker-list" aria-label="Available copies">
            {browse.matches.map(match => {
              const facts = [match.format?.toUpperCase(), match.duration ? time(match.duration) : "",
                match.bitrate ? `${match.bitrate} kbps` : "",
                match.recommended === true ? "Recommended" : match.recommended === false ? "Check match" : ""].filter(Boolean);
              return <button type="button" className="radio-result" key={match.key} disabled={saving}
                onClick={() => void confirm(browse.format, match.key)}>
                <span><strong>{match.label}</strong>{facts.length > 0 && <small>{facts.join(" · ")}</small>}
                  {match.failure && <small className="copy-picker-failure">{match.failure}</small>}
                </span>
                <span className="copy-picker-action"><RoomIcon name={match.failure ? "refresh" : "download"} />
                  {match.failure ? "Retry" : "Download"}</span>
              </button>;
            })}
          </div> : <p role="status">No copies found in this format.</p>)}
          {error && <p role="alert">{error}</p>}
          <div className="format-dialog-footer">
            {review && !store.member && <RoomAction ref={recordingsReturn} icon="album" disabled={saving} onClick={() => {
              restoreRecordingsFocus.current = true;
              setRecordingError("");
              setPanel("recordings");
            }}>Other recordings</RoomAction>}
            <RoomAction icon="settings" disabled={saving} onClick={leaveMatches}>Change format</RoomAction>
            <RoomAction icon="refresh" disabled={saving || browse.kind === "searching"}
              onClick={() => showMatches(browse.format, true)}>Search again</RoomAction>
            {review && !store.member && <RoomAction disabled={saving} onClick={() => void stopSearch()}>
              Stop searching
            </RoomAction>}
          </div>
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
        <FindCopySources button={
          <RoomIconButton icon="extensions" label="View extensions" ref={sourceReturn} disabled={saving} onClick={() => {
            restoreSourceFocus.current = true;
            setPanel("extensions");
          }} />
        } />
        {error && <p role="alert">{error}</p>}
        <div className="format-dialog-footer">
          <RoomAction ref={browseButton} icon="catalogs" disabled={saving} onClick={() => showMatches(choice)}>Browse copies</RoomAction>
          <button className="primary-action" type="button" disabled={saving} onClick={() => void confirm(choice)}>
            <RoomIcon name="search" />
            {saving ? "Confirming…" : "Confirm and search"}
          </button>
        </div>
        </>}
      </RoomDialogContent>
    </Dialog>
  );
}
