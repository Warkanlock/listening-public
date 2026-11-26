import { FindCopyExtensionsButton, FindCopySources } from "./Extensions";
import { RoomIcon, RoomIconButton } from "../components/RoomIcon";
import { useRef, useState, type FormEvent } from "react";
import { Button } from "@ui-sdk/react/button";
import { Field, FieldLabel } from "@ui-sdk/react/field";
import { Input } from "@ui-sdk/react/input";
import { REQUEST_FORMATS, statusLabel, type RequestRow } from "../api";
import { useStore } from "../store";
import { StateCard } from "../components/StateCard";
import { YouTubeLink } from "../components/YouTubeLink";
import { TryMp3Button } from "../components/TryMp3Button";
import { RequestStatus, RetryRequestButton } from "../components/RequestStatus";
import { canChangeRecording, canChooseFormat, needsCopyChoice, requestStage } from "../requestState";
import { RecordingVersions } from "../components/RecordingVersions";
import { RefreshButton } from "../components/RefreshButton";

function RetryForm({ row, onRetry }: { row: RequestRow; onRetry: () => void }) {
  const store = useStore();
  const [value, setValue] = useState(row.search_title || row.title);
  const [sending, setSending] = useState(false);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSending(true);
    try {
      if (await store.retryRequest(row.id, value, row.format_revision)) onRetry();
    } finally {
      setSending(false);
    }
  };
  return (
    <form className="retry-form" onSubmit={submit}>
      <Input
        aria-label={`Alternative search for ${row.title}`}
        placeholder="Try another title or spelling"
        value={value}
        onValueChange={setValue}
        required
      />
      <Button className="quiet-button" type="submit" disabled={sending}>
        Retry search
      </Button>
    </form>
  );
}

function RequestItem({ row }: { row: RequestRow }) {
  const store = useStore();
  const [editingSearch, setEditingSearch] = useState(false);
  const format = REQUEST_FORMATS.find(format => format.key === row.requested_format)?.label;
  const stage = requestStage(row);
  const review = needsCopyChoice(stage);
  const editSearch = ["no_matches", "search_failed", "verification_failed"].includes(stage);
  return (
    <div className="request-item">
      <span className="request-symbol">
        <RoomIcon name={row.status === "downloaded" ? "music" : "search"} />
      </span>
      <span className="request-details">
        <strong>{row.title}</strong>
        <span>
          {[row.artist, row.version, format].filter(Boolean).join(" · ")}
        </span>
        {review ? <span>{stage === "copy_unavailable" ? "Choose another copy to continue." : "No copy was saved."}</span>
          : row.message && row.message !== statusLabel(row.status) && <span>{row.message}</span>}
      </span>
      <div className="request-actions">
        <RequestStatus request={row} />
        {row.audio ? (
          <>
            <RoomIconButton icon="play" label={`Play ${row.title}`} onClick={() => store.playRequest(row)} />
            <RoomIconButton icon="outputs" label="Open in room player"
              onClick={event => void store.openRoomPlayer(row.track_index ?? `custom-${row.id}`, event.currentTarget)} />
            {store.settings?.desktop_player_available && (
              <RoomIconButton icon="northeast" label="Open in server’s desktop music app"
                onClick={() => void store.openSource(row.track_index ?? `custom-${row.id}`)} />
            )}
          </>
        ) : null}
        {!review && row.mp3_retry && <TryMp3Button retry={row.mp3_retry} title={row.title} />}
        {review ? (
          <button type="button" className="quiet-button" onClick={() => store.requestsSlice.chooseFormat(row)}>
            {stage === "copy_unavailable" ? "Browse copies" : "Review copies"}
          </button>
        ) : canChooseFormat(stage) && (
          <RoomIconButton icon="settings" label={stage === "choose_format" ? "Choose format" : "Change format"}
            onClick={() => store.requestsSlice.chooseFormat(row)} />
        )}
        <RetryRequestButton request={row} title={row.title} onRetry={() => setEditingSearch(false)} />
        {!store.member && (row.audio || canChangeRecording(stage)) && <RecordingVersions song={row}
          onBrowseCopies={review ? () => store.requestsSlice.chooseFormat(row) : undefined}
          onChoose={song => void (row.audio ? store.createSongRequest(song) : store.requestsSlice.chooseRecording(row, song))} />}
        {editSearch && <RoomIconButton icon="search" label={`Edit search: ${row.title}`}
          aria-expanded={editingSearch} onClick={() => setEditingSearch(value => !value)} />}
        {editSearch && <YouTubeLink artist={row.artist} title={row.title} iconOnly />}
      </div>
      {editSearch && editingSearch && <RetryForm row={row} onRetry={() => setEditingSearch(false)} />}
    </div>
  );
}

export function Requests() {
  const store = useStore();
  const [artist, setArtist] = useState("");
  const [title, setTitle] = useState("");
  const [sending, setSending] = useState(false);
  const formRef = useRef<HTMLFormElement>(null);
  const rows = store.requestRows;

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (sending) return;
    setSending(true);
    const ok = await store.createSongRequest({ artist: artist.trim(), title: title.trim() });
    if (ok) {
      setArtist("");
      setTitle("");
    }
    setSending(false);
  };

  return (
    <div className="view requests-view">
      <header className="listening-view-head">
        <h1>Requests</h1>
      </header>
      <FindCopySources button={null} />
      <section className="request-form-section">
        <form id="customRequestForm" aria-label="Request a song" ref={formRef} onSubmit={submit}>
          <Field name="artist">
            <FieldLabel>Artist</FieldLabel>
            <Input
              required
              maxLength={200}
              value={artist}
              onValueChange={setArtist}
            />
          </Field>
          <Field name="title">
            <FieldLabel>Song title</FieldLabel>
            <Input
              required
              maxLength={200}
              value={title}
              onValueChange={setTitle}
            />
          </Field>
          <Button className="primary-action" type="submit" disabled={sending}>
            {sending ? "Requesting…" : "Find copy"}
          </Button>
        </form>
        <p className="form-help">
          Choose a format after adding your song. Verified copies appear here when ready.
        </p>
      </section>
      <section>
        <div className="section-header">
          <h2>Recent requests</h2>
          <div className="request-actions">
            <FindCopyExtensionsButton />
            <RefreshButton target="requests" onRefresh={() => store.loadRequests()} />
          </div>
        </div>
        <div className="requests-list">
          {rows.length ? (
            rows.map((row) => <RequestItem key={row.id} row={row} />)
          ) : store.requestsError ? (
            <StateCard
              kind="error"
              icon="alert"
              title="Requests could not load"
              body={store.requestsError}
              action={
                <button
                  className="quiet-button"
                  type="button"
                  onClick={() => void store.loadRequests()}
                >
                  Try again
                </button>
              }
            />
          ) : !store.requestsLoaded ? (
            <StateCard
              kind="loading"
              icon="search"
              title="Checking your searches"
              body="Fetching the latest requests from the room."
              compact
            />
          ) : (
            <StateCard
              icon="search"
              title="No requests yet"
              body="Enter an artist and song title above to request a copy."
              action={
                <button
                  className="quiet-button"
                  type="button"
                  onClick={() =>
                    formRef.current?.querySelector("input")?.focus()
                  }
                >
                  Add a song
                </button>
              }
            />
          )}
        </div>
      </section>
    </div>
  );
}
