import { RoomIcon } from "./RoomIcon";
import { useMemo } from "react";
import { createParts } from "@ui-sdk/react/primitive";
import { type SessionTrack } from "../api";
import { useStore } from "../store";
import { queueList } from "./queueList";
import { CoverArt } from "./Artwork";
import { StateCard } from "./StateCard";

const {
  Root: QueueRoot,
  Item: QueueItem,
  Handle: QueueHandle,
} = createParts(queueList, { root: "div", item: "div", handle: "button" });

interface QueueEntry {
  key: string;
  index: number;
  track: SessionTrack;
}

// Queue actions address the position in the server snapshot, including gaps.
// A track queued twice gets an occurrence suffix so reorder keys
// stay unique.
function buildEntries(upcoming: (SessionTrack | null)[]): QueueEntry[] {
  const seen = new Map<string, number>();
  const entries: QueueEntry[] = [];
  for (const [index, track] of upcoming.entries()) {
    if (!track) continue;
    const base = String(track.id);
    const count = (seen.get(base) ?? 0) + 1;
    seen.set(base, count);
    entries.push({
      key: count === 1 ? base : `${base}#${count}`,
      index,
      track,
    });
  }
  return entries;
}

function findMoved(oldKeys: string[], newKeys: string[]) {
  if (oldKeys.length !== newKeys.length) return null;
  // An adjacent swap can be represented in either direction. Bring the later
  // song forward so unresolved entries between the two never move ahead of it.
  for (const key of [...oldKeys].reverse()) {
    const rest = (list: string[]) => list.filter((item) => item !== key).join("\n");
    if (rest(oldKeys) === rest(newKeys)) {
      return { from: oldKeys.indexOf(key), to: newKeys.indexOf(key) };
    }
  }
  return null;
}

export function QueuePanel() {
  const store = useStore();
  const session = store.session;
  const entries = useMemo(
    () => buildEntries(session?.upcoming ?? []),
    [session?.upcoming],
  );
  if (!store.queueOpen) return null;
  const current = session?.current ?? null;
  const keys = entries.map((entry) => entry.key);
  const byKey = new Map(entries.map((entry) => [entry.key, entry]));
  const version = session?.version;

  const browseReady = () => {
    store.setQueueOpen(false);
    if (store.query) store.clearSearch();
    if (store.view !== "all") store.setView("all");
    store.setFilter("ready");
  };

  return (
    <aside className="queue-panel" aria-label="Playback queue">
      <div className="queue-header">
        <div>
          <p>Shared listening</p>
          <h2>Up next</h2>
        </div>
        <button
          type="button"
          aria-label="Close queue"
          onClick={() => store.setQueueOpen(false)}
        >
          <RoomIcon name="close" />
        </button>
      </div>
      <div className="queue-current">
        {current ? (
          <>
            <span className="queue-current-art">
              <CoverArt cover={current.cover} />
            </span>
            <span>
              <small>{session?.playing ? "Playing now" : "Paused"}</small>
              <strong>{current.title}</strong>
              <span>{current.artist}</span>
            </span>
          </>
        ) : (
          <span className="queue-empty-current">
            Choose a ready song to start listening together.
          </span>
        )}
      </div>
      {!session?.queue_length && <div className="queue-summary">The queue is empty</div>}
      {entries.length ? (
        <QueueRoot
          className="queue-list"
          value={keys}
          itemLabel={(key: string) => byKey.get(key)?.track.title}
          onValueChange={(next: string[]) => {
            const move = findMoved(keys, next);
            if (move && move.from !== move.to) {
              void store.sendSession(
                "queue_move",
                { index: entries[move.from].index, to: entries[move.to].index, expected_version: version },
                "Queue updated",
              );
            }
          }}
        >
          {({ order }: { order: string[] }) =>
            order.map((key) => {
              const entry = byKey.get(key);
              if (!entry) return null;
              const { track, index } = entry;
              const position = entries.indexOf(entry);
              const previous = entries[position - 1];
              const next = entries[position + 1];
              const nextIndex = next?.index ?? index + 1;
              return (
                <QueueItem key={key} value={key} className="queue-item">
                  <QueueHandle
                    value={key}
                    className="queue-grip"
                    aria-label={`Reorder ${track.title}`}
                    aria-describedby="queue-reorder-help"
                  >
                    <RoomIcon name="grip" />
                  </QueueHandle>
                  <button
                    className="queue-song"
                    type="button"
                    aria-label={`Play ${track.title} now`}
                    onClick={() =>
                      void store.sendSession("jump", {
                        index,
                        expected_version: version,
                      })
                    }
                  >
                    <span className="track-art">
                      <CoverArt cover={track.cover} />
                    </span>
                    <span>
                      <strong>{track.title}</strong>
                      <small>{track.artist}</small>
                    </span>
                  </button>
                  <span className="queue-item-actions">
                    <button
                      type="button"
                      aria-label={`Move ${track.title} up`}
                      disabled={!previous}
                      onClick={() => {
                        if (previous) void store.sendSession("queue_move",
                          { index, to: previous.index, expected_version: version }, "Queue updated");
                      }}
                    >
                      <RoomIcon name="up" />
                    </button>
                    <button
                      type="button"
                      aria-label={`Move ${track.title} down`}
                      disabled={nextIndex >= (session?.queue_length ?? 0)}
                      onClick={() =>
                        void store.sendSession("queue_move",
                          { index: next?.index ?? index, to: next ? index : nextIndex, expected_version: version }, "Queue updated")
                      }
                    >
                      <RoomIcon name="down" />
                    </button>
                    <button
                      type="button"
                      aria-label={`Remove ${track.title} from queue`}
                      onClick={() =>
                        void store.sendSession(
                          "queue_remove",
                          { index, expected_version: version },
                          "Removed from queue",
                        )
                      }
                    >
                      <RoomIcon name="close" />
                    </button>
                  </span>
                </QueueItem>
              );
            })
          }
        </QueueRoot>
      ) : (
        <div className="queue-list">
          <StateCard
            compact
            icon="menu"
            label="Shared queue"
            title="Nothing up next"
            body="Choose a ready song or open its menu to add it here."
            action={
              <button className="quiet-button" type="button" onClick={browseReady}>
                Browse ready songs
              </button>
            }
          />
        </div>
      )}
      <p className="visually-hidden" id="queue-reorder-help">
        Press Space on a song's reorder handle to pick it up, use the arrow
        keys to move it, and press Space again to drop it.
      </p>
    </aside>
  );
}
