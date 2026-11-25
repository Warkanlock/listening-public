import { useState } from "react";
import {
  Progress,
  ProgressIndicator,
  ProgressLabel,
  ProgressTrack,
  ProgressValue,
} from "@ui-sdk/react/progress";
import { Button } from "@ui-sdk/react/button";
import {
  BULK_DOWNLOAD_FORMATS, isBulkDownloadFormat, isHost,
  type BulkDownloadFormat, type JobCard, type JobKind, type WatchdogHealth,
} from "../api";
import { useStore } from "../store";
import { RoomSelect } from "../components/RoomControls";
import { StateCard } from "../components/StateCard";
import { RefreshButton } from "../components/RefreshButton";

const TITLES: Record<JobKind, string> = {
  downloads: "Download missing songs",
  genres: "Categorize genres",
  requests: "Song requests",
  reclaim: "Reclaim database space",
  migrate: "Copy library into the database",
};

const CONSEQUENCES: Record<JobKind, string> = {
  downloads:
    "Stopping turns off background searches. Completed downloads stay in your library.",
  genres:
    "Stopping keeps the genres found so far.",
  requests:
    "Pending requests stay queued for the next run.",
  reclaim:
    "The next run can reclaim the remaining space.",
  migrate:
    "Songs already copied stay in the database. The next run picks up where this one stopped.",
};

const JOB_DETAILS: Record<JobKind, { input: string; source: string; result: string; start: string }> = {
  downloads: {
    input: "Missing songs in the selected playlists",
    source: "Soulseek",
    result: "Saves verified copies to the room library.",
    start: "Download missing songs",
  },
  genres: {
    input: "Songs in this room without a genre",
    source: "Audio file tags, then MusicBrainz and ListenBrainz",
    result: "Fills missing genres for browse, Radio, and the mood map.",
    start: "Find missing genres",
  },
  requests: {
    input: "Requests waiting in this room",
    source: "Room library, then Soulseek",
    result: "Fills requests in order with verified copies.",
    start: "Process requests",
  },
  reclaim: {
    input: "Audio already removed from the room",
    source: "Media database",
    result: "Returns unused database space to the server.",
    start: "Reclaim space",
  },
  migrate: {
    input: "Verified files on the server",
    source: "Library folder",
    result: "Copies audio into the database and keeps the original files.",
    start: "Copy library",
  },
};

function formatCount(value: number): string {
  return value.toLocaleString();
}

function JobProgress({ card }: { card: JobCard }) {
  const progress = card.progress;
  if (card.kind === "reclaim") {
    const dead = progress.dead_bytes ?? 0;
    return (
      <div className="job-progress">
        {card.running ? (
          <Progress aria-label="Reclaim progress">
            <ProgressTrack>
              <ProgressIndicator />
            </ProgressTrack>
          </Progress>
        ) : null}
        <p className="job-counts">
          {dead === 0
            ? "No database space is waiting to be reclaimed."
            : `${(dead / 1024 ** 3).toFixed(2)} GiB of removed songs' space waiting to be reclaimed`}
        </p>
      </div>
    );
  }
  if (card.kind === "requests") {
    const pending = progress.pending ?? 0;
    return (
      <div className="job-progress">
        {card.running ? (
          <Progress aria-label="Song requests progress">
            <ProgressTrack>
              <ProgressIndicator />
            </ProgressTrack>
          </Progress>
        ) : null}
        <p className="job-counts">
          {pending === 0
            ? "No requests are waiting."
            : `${formatCount(pending)} request${pending === 1 ? "" : "s"} waiting`}
        </p>
      </div>
    );
  }
  if (card.kind === "genres") {
    const categorized = progress.categorized ?? 0;
    const total = progress.total ?? 0;
    const scanTotal = progress.scan_total;
    const checked = progress.checked ?? 0;
    const found = progress.found ?? 0;
    const unavailable = progress.unavailable ?? 0;
    return (
      <div className="job-progress">
        {scanTotal !== undefined && scanTotal > 0 && (
          <>
            <Progress value={checked} max={scanTotal} aria-label="Genre scan progress">
              <div className="job-progress-head">
                <ProgressLabel>Songs checked this run</ProgressLabel>
                <ProgressValue />
              </div>
              <ProgressTrack><ProgressIndicator /></ProgressTrack>
            </Progress>
            <p className="job-counts">
              {formatCount(checked)} of {formatCount(scanTotal)} checked · {formatCount(found)} newly tagged
              {unavailable > 0 && ` · ${formatCount(unavailable)} with unavailable sources`}
            </p>
            {card.state === "done" && progress.remaining > 0 &&
              <p className="job-counts">{formatCount(progress.remaining)} still need a genre.</p>}
          </>
        )}
        {card.running && scanTotal === undefined && <p className="job-counts">Preparing the scan…</p>}
        {card.state === "done" && scanTotal === 0 && <p className="job-counts">All songs already had a genre.</p>}
        <p className="job-counts">
          {formatCount(categorized)} of {formatCount(total)} songs in the library have a genre.
        </p>
      </div>
    );
  }
  if (card.kind === "migrate") {
    const done = progress.done ?? 0;
    const total = progress.total ?? 0;
    const copied = (progress.migrated ?? 0) + (progress.already ?? 0);
    const skipped = progress.skipped ?? 0;
    return (
      <div className="job-progress">
        <Progress
          value={done}
          max={Math.max(total, 1)}
          aria-label="Library migration progress"
        >
          <div className="job-progress-head">
            <ProgressLabel>Songs checked</ProgressLabel>
            <ProgressValue />
          </div>
          <ProgressTrack>
            <ProgressIndicator />
          </ProgressTrack>
        </Progress>
        <p className="job-counts">
          {formatCount(copied)} of {formatCount(total)} songs in the
          database
          {skipped
            ? ` · ${formatCount(skipped)} skipped`
            : ""}
        </p>
      </div>
    );
  }
  const attempted = progress.attempted ?? 0;
  const total = progress.total ?? 0;
  const verified = progress.verified ?? 0;
  const scopeName =
    card.scope.type === "list" ? (card.scope.name ?? "One list") : "All lists";
  return (
    <div className="job-progress">
      <Progress
        value={attempted}
        max={Math.max(total, 1)}
        aria-label="Download progress"
      >
        <div className="job-progress-head">
          <ProgressLabel>{scopeName}</ProgressLabel>
          <ProgressValue />
        </div>
        <ProgressTrack>
          <ProgressIndicator />
        </ProgressTrack>
      </Progress>
      <p className="job-counts">
        {formatCount(attempted)} of {formatCount(total)} songs searched ·{" "}
        {formatCount(verified)} ready to play
      </p>
    </div>
  );
}

function JobCardView({ card }: { card: JobCard }) {
  const store = useStore();
  const [sending, setSending] = useState(false);
  const [scopeChoice, setScopeChoice] = useState("all");
  const [format, setFormat] = useState<BulkDownloadFormat | "default">("default");
  const defaultFormat = BULK_DOWNLOAD_FORMATS[card.default_format ?? "mp3_preferred"];
  const host = isHost(store.settings);
  const playlists = store.jobs?.playlists ?? [];
  const actionError =
    store.jobActionError?.kind === card.kind
      ? store.jobActionError.message
      : null;

  const act = async (action: "start" | "stop") => {
    setSending(true);
    try {
      if (action === "start" && card.kind === "downloads") {
        const started = await store.runJob(
          "downloads",
          "start",
          {
            ...(scopeChoice === "all" ? { type: "all" } : { type: "list", id: scopeChoice }),
            format: format === "default" ? undefined : format,
          },
        );
        if (started) setFormat("default");
      } else {
        await store.runJob(card.kind, action);
      }
    } finally {
      setSending(false);
    }
  };

  const details = JOB_DETAILS[card.kind];
  const engineSource = card.engines && (
    card.engines.search && card.engines.fetch
      ? card.engines.search === card.engines.fetch
        ? card.engines.search
        : `${card.engines.search} searches · ${card.engines.fetch} downloads`
      : "Choose engines in Extensions"
  );
  const status = {
    running: "Running", queued: "Waiting", failed: "Failed", cancelled: "Stopped",
    idle: card.kind === "genres" ? "Ready" : "Stopped",
    done: !card.progress.complete ? "Ended"
      : card.progress.unavailable ? "Completed with lookup errors"
      : card.progress.scan_total > 0 && card.progress.found === 0 ? "No genres found"
      : "Completed",
  }[card.state];

  return (
    <section className="job-card" data-state={card.state} aria-label={TITLES[card.kind]}>
      <header className="job-heading">
        <div>
          <h2>{TITLES[card.kind]}</h2>
          <p>{details.result}</p>
        </div>
        <span
          className={`status-pill job-state${card.state === "running" ? " job-state--running" : card.state === "queued" ? " job-state--queued" : card.state === "failed" ? " job-state--failed" : ""}`}
        >{status}</span>
      </header>
      <div className="job-details">
        <dl className="job-sources">
          <div><dt>Checks</dt><dd>{details.input}</dd></div>
          <div><dt>Sources</dt><dd>{engineSource ?? details.source}</dd></div>
          {card.kind === "downloads" && (card.state === "running" || card.state === "queued") && (
            <div><dt>Format</dt><dd>{card.scope.format ? BULK_DOWNLOAD_FORMATS[card.scope.format] : `Room default (${defaultFormat})`}</dd></div>
          )}
        </dl>
        <div className="job-run">
          {card.running && card.started_at ? (
            <p className="job-started">
              Started {new Date(card.started_at * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
            </p>
          ) : null}
          {card.state === "queued" ? (
            <p className="job-started">
              In line
              {card.queue_position
                ? `, position ${card.queue_position}`
                : ""}
              . It starts on its own when the library is free.
            </p>
          ) : null}
          <JobProgress card={card} />
          {host && card.kind === "downloads" && card.state !== "running" && card.state !== "queued" ? (
            <>
              <div className="job-scope">
                <RoomSelect label="Playlists to search" value={scopeChoice} onChange={setScopeChoice}
                  options={[{ value: "all", label: "All lists" }, ...playlists.map(entry => ({ value: entry.id, label: entry.name }))]} />
              </div>
              <div className="job-scope">
                <RoomSelect label="Download format" value={format}
                  onChange={value => setFormat(isBulkDownloadFormat(value) ? value : "default")}
                  options={[{ value: "default", label: `Room default (${defaultFormat})` },
                    ...Object.entries(BULK_DOWNLOAD_FORMATS).map(([value, label]) => ({ value, label }))]} />
              </div>
            </>
          ) : null}
          {card.state === "failed" && card.error ? (
            <p className="job-blocker" role="alert">
              {card.error}
            </p>
          ) : null}
          {!card.running && card.blocker ? (
            <p className="job-blocker">{card.blocker}</p>
          ) : null}
          {host && actionError ? (
            <p className="job-blocker" role="alert">
              {actionError}
            </p>
          ) : null}
          {host && (
            <div className="job-actions">
              {card.state === "running" ? (
                <Button
                  className="primary-action"
                  disabled={sending}
                  onClick={() => void act("stop")}
                >
                  {sending ? "Stopping…" : "Stop"}
                </Button>
              ) : card.state === "queued" ? (
                <Button
                  className="primary-action"
                  disabled={sending}
                  onClick={() => void act("stop")}
                >
                  {sending ? "Cancelling…" : "Cancel"}
                </Button>
              ) : (
                <Button
                  className="primary-action"
                  disabled={sending || Boolean(card.blocker)}
                  onClick={() => void act("start")}
                >
                  {sending ? "Starting…" : details.start}
                </Button>
              )}
            </div>
          )}
          {host && card.running ? (
            <p className="job-consequence">{CONSEQUENCES[card.kind]}</p>
          ) : null}
          {card.kind === "downloads" && card.auto_download ? (
            <p className="job-consequence">New songs in your lists are searched automatically.</p>
          ) : null}
        </div>
      </div>
    </section>
  );
}

// Plan 022's watchdog health, rendered by plan 063 settlement 3:
// the loop behind the jobs above records its failed passes in
// the activity snapshot the store already polls. A readout in
// the same card language; there is nothing to start or stop,
// so the card carries no action.
function WatchdogCard({ health }: { health: WatchdogHealth }) {
  const failing = health.failures > 0;
  return (
    <section className="job-card job-upkeep" aria-label="Room upkeep">
      <div className="job-heading">
        <div>
          <h2>Room upkeep</h2>
          <p>Checks for changed playlists and starts queued jobs.</p>
        </div>
        <span
          className={`status-pill job-state${failing ? " job-state--failed" : " job-state--running"}`}
        >
          {failing ? "Failed" : "Running"}
        </span>
      </div>
      <div className="job-progress">
        <p className="job-counts">
          {failing
            ? `${formatCount(health.failures)} failed check${health.failures === 1 ? "" : "s"} in a row · last at ${new Date(health.last_error_at * 1000).toLocaleString()}`
            : "Every check is passing."}
        </p>
      </div>
      {failing && health.last_error ? (
        <p className="job-blocker" role="alert">
          {health.last_error}
        </p>
      ) : null}
      {failing ? (
        <p className="job-consequence">The room will retry automatically.</p>
      ) : null}
    </section>
  );
}

export function Jobs() {
  const store = useStore();
  const result = store.jobs;
  return (
    <div className="view">
      <header className="listening-view-head">
        <div>
          <h1>Jobs</h1>
          <p>
            Jobs run on the server, one at a time.
            {store.settings && !isHost(store.settings) && " Manage jobs from a room owner session."}
          </p>
        </div>
        <RefreshButton target="jobs" onRefresh={() => store.refreshJobs()} />
      </header>
      <div className="job-queue">
        {result ? (
          <>
            {result.jobs.map((card) => (
              <JobCardView key={card.kind} card={card} />
            ))}
            {store.activity?.watchdog ? (
              <WatchdogCard health={store.activity.watchdog} />
            ) : null}
          </>
        ) : store.jobsError ? (
          <StateCard
            kind="error"
            icon="alert"
            title="Jobs could not load"
            body={store.jobsError}
            action={
              <Button
                className="primary-action"
                onClick={() => void store.refreshJobs()}
              >
                Try again
              </Button>
            }
          />
        ) : (
          <StateCard
            kind="loading"
            title="Loading jobs"
            body="Checking the room’s queue."
          />
        )}
      </div>
    </div>
  );
}
