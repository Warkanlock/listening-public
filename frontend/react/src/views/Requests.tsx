import { FindCopySources } from "./Extensions";
import { RoomIcon } from "../components/RoomIcon";
import { useRef, useState, type FormEvent } from "react";
import { Button } from "@ui-sdk/react/button";
import { Field, FieldLabel } from "@ui-sdk/react/field";
import { Input } from "@ui-sdk/react/input";
import { REQUEST_FORMATS, statusLabel, type RequestRow } from "../api";
import { useStore } from "../store";
import { StateCard } from "../components/StateCard";
import { YouTubeLink } from "../components/YouTubeLink";

const RETRYABLE = ["not_found", "error", "rejected"];

function RetryForm({ row }: { row: RequestRow }) {
  const store = useStore();
  const [value, setValue] = useState(row.search_title || row.title);
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void store.retryRequest(row.id, value);
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
      <Button className="quiet-button" type="submit">
        Retry search
      </Button>
    </form>
  );
}

function RequestItem({ row }: { row: RequestRow }) {
  const store = useStore();
  return (
    <div className="request-item">
      <span className="request-symbol">
        <RoomIcon name={row.status === "downloaded" ? "music" : "search"} />
      </span>
      <span className="request-details">
        <strong>{row.title}</strong>
        <span>
          {row.artist}{row.requested_format ? ` · ${REQUEST_FORMATS[row.requested_format]}` : ""}{row.message && row.message !== statusLabel(row.status) ? ` · ${row.message}` : ""}
        </span>
      </span>
      <span className={`status-pill ${row.status}`}>
        {statusLabel(row.status)}
      </span>
      {row.audio ? (
        <>
          <button
            className="quiet-button"
            type="button"
            onClick={() => store.playRequest(row)}
          >
            Play
          </button>
          <button className="quiet-button" type="button" onClick={event => void store.openRoomPlayer(row.track_index ?? `custom-${row.id}`, event.currentTarget)}>
            Open in room player
          </button>
          {store.settings?.desktop_player_available && (
            <button
              className="quiet-button"
              type="button"
              onClick={() => void store.openSource(row.track_index ?? `custom-${row.id}`)}
            >
              Open in server’s desktop music app
            </button>
          )}
        </>
      ) : null}
      {(row.status === "waiting_format" || RETRYABLE.includes(row.status)) && (
        <button type="button" className="quiet-button" onClick={() => store.requestsSlice.chooseFormat(row)}>
          {row.status === "waiting_format" ? "Choose format" : "Change format"}
        </button>
      )}
      {RETRYABLE.includes(row.status) ? (
        <>
          <YouTubeLink artist={row.artist} title={row.title} />
          <RetryForm row={row} />
        </>
      ) : null}
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
    <div className="view">
      <header className="listening-view-head">
        <h1>Requests</h1>
      </header>
      <FindCopySources />
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
          <button
            className="quiet-button"
            type="button"
            onClick={() => void store.loadRequests()}
          >
            Refresh
          </button>
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
