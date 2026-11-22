import { RoomIcon } from "./RoomIcon";
import { RoomDialogContent } from "./WindowContent";
import { useState } from "react";
import {
  Dialog,
  DialogBackdrop,
  DialogClose,
  DialogDescription,
  DialogTitle,
} from "@ui-sdk/react/dialog";
import { isHost } from "../api";
import { useStore } from "../store";
import { StateCard } from "./StateCard";

type EventFilter = "all" | "downloading" | "succeeded" | "failed";

const EVENT_STATES: Record<string, string> = {
  searching: "downloading",
  downloading: "downloading",
  succeeded: "succeeded",
  failed: "failed",
};

const EVENT_LABELS: Record<string, string> = {
  searching: "Searching",
  downloading: "Downloading",
  succeeded: "Ready",
  failed: "No copy found",
};

const FILTERS: { key: EventFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "downloading", label: "Downloading" },
  { key: "succeeded", label: "Ready" },
  { key: "failed", label: "Failed" },
];

// The activity dialog (item 33): the download explorer's overview,
// CLI tail, and recent song events, with event clicks and the
// explore buttons landing on the filtered library.
export function ActivityDialog() {
  const store = useStore();
  const [filter, setFilter] = useState<EventFilter>("all");
  if (!store.activityDialogOpen) return null;
  const data = store.downloadActivity;
  const activity = data?.activity;
  const summary = data?.summary;
  const batch = activity?.batch || "";
  const events = (data?.events ?? []).filter(
    (event) => filter === "all" || EVENT_STATES[event.state] === filter,
  );
  const logs = data?.logs ?? [];

  const manageDownloads = () => {
    store.setActivityDialogOpen(false);
    store.setView("settings");
    store.settingsTab = "downloads";
    store.bump();
    requestAnimationFrame(() => {
      document
        .querySelector(".process-summary")
        ?.scrollIntoView({ block: "center" });
    });
  };

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) store.setActivityDialogOpen(false);
      }}
    >
      <DialogBackdrop />
      <RoomDialogContent className="activity-dialog">
        <div className="activity-dialog-head">
          <div>
            <DialogTitle>Download activity</DialogTitle>
            <DialogDescription>
              {batch ? `Current batch ${batch}` : "Recent library activity"}
            </DialogDescription>
          </div>
          <DialogClose className="dialog-close" aria-label="Close download activity">
            <RoomIcon name="close" />
          </DialogClose>
        </div>
        <div className="activity-dialog-content">
          {activity && summary ? (
            <div className="activity-overview">
              <div className="activity-overview-main">
                <span>{activity.status}</span>
                <strong>
                  {activity.attempted} of {activity.total} searched
                </strong>
                <span>
                  {batch
                    ? `Batch ${batch} in progress`
                    : data.bulk_running
                      ? "Scanner is working"
                      : "Scanner is paused"}
                </span>
                <div className="activity-detail-progress">
                  <span
                    style={{
                      width: `${
                        activity.total
                          ? Math.round(
                              (activity.attempted / activity.total) * 100,
                            )
                          : 0
                      }%`,
                    }}
                  />
                </div>
              </div>
              <div className="activity-metrics">
                <div>
                  <strong>{summary.verified}</strong>
                  <span>Ready to play</span>
                </div>
                <div>
                  <strong>{summary.unmatched}</strong>
                  <span>Not found</span>
                </div>
                <div>
                  <strong>{summary.rejected}</strong>
                  <span>Needs review</span>
                </div>
                <div>
                  <strong>{summary.pending}</strong>
                  <span>Still to search</span>
                </div>
              </div>
            </div>
          ) : null}
          <div className="activity-explore-actions">
            <button
              className="quiet-button"
              type="button"
              onClick={() => store.exploreLibrary("ready")}
            >
              Explore ready songs
            </button>
            <button
              className="quiet-button"
              type="button"
              onClick={() => store.exploreLibrary("missing")}
            >
              Explore missing songs
            </button>
          </div>
          <div className="activity-dialog-grid">
            <section aria-label="Recent song activity">
              <div className="activity-section-heading">
                <div>
                  <h3>Song activity</h3>
                  <p>Select a song to find it in your library.</p>
                </div>
                <span id="activityEventCount">
                  {events.length ? `${events.length} shown` : ""}
                </span>
              </div>
              <div
                className="activity-filters"
                role="group"
                aria-label="Filter download events"
              >
                {FILTERS.map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    aria-pressed={filter === item.key}
                    className={filter === item.key ? "is-active" : undefined}
                    onClick={() => setFilter(item.key)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
              <div className="activity-event-list">
                {store.downloadActivityError ? (
                  <StateCard
                    kind="error"
                    compact
                    icon="alert"
                    label="Activity"
                    title="Activity could not load"
                    body={store.downloadActivityError}
                    action={
                      <button
                        className="quiet-button"
                        type="button"
                        onClick={() => void store.refreshDownloadExplorer()}
                      >
                        Try again
                      </button>
                    }
                  />
                ) : !data ? (
                  <StateCard
                    kind="loading"
                    compact
                    icon="more"
                    label="Activity"
                    title="Loading activity"
                    body="Reading the latest download events."
                  />
                ) : events.length ? (
                  events.map((event, index) => (
                    <button
                      key={`${event.song}-${event.state}-${index}`}
                      className="activity-event"
                      type="button"
                      onClick={() =>
                        store.exploreLibrary(
                          "all",
                          event.song.replace(" - ", " "),
                        )
                      }
                    >
                      <span className="activity-event-song">
                        <strong>{event.song}</strong>
                        <small>
                          {event.reason ||
                            (event.batch
                              ? `Batch ${event.batch}`
                              : "Library download")}
                        </small>
                      </span>
                      <em
                        className={`activity-event-state ${
                          EVENT_STATES[event.state] || event.state
                        }`}
                      >
                        {EVENT_LABELS[event.state] || event.state}
                      </em>
                    </button>
                  ))
                ) : (
                  <StateCard
                    compact
                    icon="music"
                    label="Recent songs"
                    title="No matching activity"
                    body="Try another filter or check again after the next download."
                  />
                )}
              </div>
            </section>
            <aside className="activity-inspector">
              <h3>Scanner</h3>
              {summary ? (
                <p>
                  {typeof summary.free_gib === "number"
                    ? `${summary.free_gib.toFixed(1)} GiB free on the server`
                    : "Storage details unavailable"}
                </p>
              ) : null}
              {activity ? (
                <p>
                  {activity.queued_requests} queued requests ·{" "}
                  {data.requests_running
                    ? "request worker running"
                    : "request worker idle"}
                </p>
              ) : null}
              {isHost(store.settings) ? <button
                className="quiet-button"
                type="button"
                onClick={manageDownloads}
              >
                Manage downloads
              </button> : null}
              {isHost(store.settings) && <details className="activity-cli">
                <summary>CLI output</summary>
                <pre>{logs.length ? logs.join("\n") : "No CLI output yet."}</pre>
              </details>}
            </aside>
          </div>
        </div>
      </RoomDialogContent>
    </Dialog>
  );
}
