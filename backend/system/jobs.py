"""The jobs layer: the room's background work as startable jobs.

Plan 040 (absorbed into plan 042 as Stage 7) replaced the six
start/stop worker buttons in Settings with a Jobs view: one card
per kind, over the workers that do the room's long work (bulk
downloads, the genre pass, song requests).

Plan 050 Step 2 (ADR 0002) changed what Start means. A start no
longer spawns the worker: it enqueues a durable job in the state
database (system.job_queue), and the watchdog, acting as the
dispatcher through this layer's dispatch(), starts the oldest
queued job whenever no worker is running. Pressing Start with the
library free still starts the job in the same request, because
act() dispatches right after enqueueing; pressing it with the
library busy now answers with a place in line on the card, not a
blocker. Setup blockers (no lists, download setup unfinished) are
still checked at enqueue time, and again at dispatch time: a job
whose blocker appeared while it waited fails visibly on its card
instead of starting, and never retries silently.

The worker machinery underneath stays owned by server.py and is
injected, the way the refresh pipeline and the playback session
take theirs: this module owns the job vocabulary (kinds, scopes,
progress, card state) and the queue's transitions.

One lock serializes every transition: the watchdog thread
dispatches while HTTP threads enqueue, cancel, and snapshot, and
two dispatch passes racing over the same head row would start it
twice or fail the winner's row (the loser sees a busy library and
reads it as a spawn failure). The lock is re-entrant because act()
dispatches what it enqueued.
"""

import time
import threading

from system.formats import BULK_DOWNLOAD_FORMATS, bulk_download_format

# The job kinds, which are also the worker kinds the supervisor
# knows (GLOSSARY: a job is carried out by a worker of the same
# name). Sharing and export are workers too, but they have no
# card and no queue: they start directly, like plan 050's share
# always did. Reclaim (plan 060) is the media database's
# maintenance job: it has a card and a queue like the library
# jobs, but it is normally queued by report(), not by a person,
# and its card exists only in rooms that have a media database.
# Migrate (plan 062) is the one-time move of the library's files
# into the media database: the server queues it at start while
# the completion marker is absent, and its card shows only while
# the migration is still owed or in flight.
KINDS = ("downloads", "genres", "requests", "reclaim", "migrate")

KIND_LABELS = {"downloads": "Download missing songs",
               "genres": "Categorize genres",
               "requests": "Song requests",
               "reclaim": "Reclaim database space",
               "migrate": "Copy library into the database"}

DOWNLOAD_SETUP_BLOCKER = "Choose compatible download sources in Extensions and install FFmpeg on the server."

GENRE_SETUP_BLOCKER = ("The genre pass needs ffprobe on the "
                       "server computer.")

LIST_GONE_BLOCKER = "The list this job was for is no longer there."


class Jobs:
    """Job cards, the queue's transitions, and the dispatcher."""

    def __init__(self, *, queue, worker_running, start_worker,
                 stop_worker, genre_tools_ready, download_ready,
                 lists_ready, refresh_library, save_settings, settings,
                 catalog, pending_requests, playlist_options,
                 media_dead_bytes=None, migration_status=None, draining=None, acquisition_ready=None):
        self._queue = queue
        self._worker_running = worker_running
        self._start_worker = start_worker
        self._stop_worker = stop_worker
        self._genre_tools_ready = genre_tools_ready
        self._download_ready = download_ready
        self._acquisition_ready = acquisition_ready or download_ready
        self._lists_ready = lists_ready
        self._refresh_library = refresh_library
        self._save_settings = save_settings
        self._settings = settings
        self._catalog = catalog
        self._pending_requests = pending_requests
        self._playlist_options = playlist_options
        # The reclaim card's progress: the media database's dead
        # bytes, read live. The callable returns None when the
        # room has no media database; the reclaim card is then
        # omitted from the snapshot entirely.
        self._media_dead_bytes = media_dead_bytes or (lambda: None)
        # The migrate card's progress: the migration's recorded
        # status (catalog.media_reconciliation's codec), read
        # live. The record's shape is the codec's; an absent
        # callable reads as an empty record, all zeros.
        self._migration_status = migration_status or (lambda: {})
        self._lock = threading.RLock()
        self._draining = draining or (lambda: False)

    # -- card state --------------------------------------------------------

    def snapshot(self, host=True):
        """The full Jobs view state: one card per kind, plus the
        playlist choices the downloads scope offers."""
        with self._lock:
            for kind in KINDS:
                self._reconcile(kind)
            kinds = []
            for kind in KINDS:
                if kind == "reclaim" \
                        and self._media_dead_bytes() is None:
                    continue
                if kind == "migrate" and not self._migration_visible():
                    continue
                kinds.append(kind)
            return {"jobs": [self._card(kind, host) for kind in kinds],
                    "playlists": self._playlist_options()}

    def waiting(self, kind):
        return self._queue.queued_for(kind) is not None

    def running_scope(self, kind):
        with self._lock:
            row = self._queue.running_for(kind)
            return self._worker_scope(row) if row is not None else None

    def _worker_scope(self, row):
        if row["kind"] == "genres" or (row["kind"] in ("downloads", "requests")
                                       and "download_engines" in self._settings):
            return {**row["scope"], "job_id": row["id"]}
        return row["scope"]

    def _migration_visible(self):
        """Whether the migrate card shows (plan 062): while the
        one-time migration is still owed (no completion marker in
        settings) or a migrate job is waiting or running. Once
        the marker is written and the queue is quiet, the card
        retires; the migration never runs again."""
        if "library_migrated" not in self._settings:
            return True
        return (self._queue.queued_for("migrate") is not None
                or self._queue.running_for("migrate") is not None)

    def _reconcile(self, kind):
        """A genre worker confirms completion after its final checkpoint."""
        if self._worker_running(kind):
            return
        row = self._queue.running_for(kind)
        if row is None:
            return
        if (kind in ("downloads", "requests") and "download_engines" in self._settings
                and not row["progress"].get("engine_complete")):
            self._queue.record_progress(row["id"], {**row["progress"],
                "engine_retry": row["progress"].get("engine_retry", "Download interrupted. Waiting to resume."),
                "retry_after": row["progress"].get("retry_after", time.time() + 90)})
            self._queue.requeue(row["id"])
        elif kind == "genres" and row["progress"] and not row["progress"].get("complete"):
            self._queue.mark_failed(row["id"], "The genre scan was interrupted. Start it again to check the remaining songs.")
        else:
            self._queue.mark_done(row["id"])

    def _card(self, kind, host):
        running = self._worker_running(kind)
        queued = self._queue.queued_for(kind)
        latest = self._queue.latest_for(kind)
        if running:
            row = self._queue.running_for(kind)
            state = "running"
            scope = row["scope"] if row else self._default_scope(kind)
            started_at = row["started_at"] if row else None
            position = None
            error = ""
        elif queued is not None:
            state = "queued"
            scope = queued["scope"]
            started_at = None
            position = self._queue.position(queued["id"])
            error = queued["progress"].get("engine_retry", "")
        elif latest is not None and latest["state"] == "failed":
            state = "failed"
            scope = latest["scope"]
            started_at = None
            position = None
            error = latest["error"]
        else:
            state = "idle"
            scope = (latest["scope"] if latest is not None
                     else self._default_scope(kind))
            started_at = None
            position = None
            error = ""
        card = {"kind": kind,
                "running": running,
                "state": state,
                "queue_position": position,
                "error": error,
                "started_at": started_at,
                "scope": scope,
                "progress": self._progress(kind, scope),
                "blocker": "" if state == "running" else self._setup_blocker(kind)}
        if kind == "genres" and latest is not None and (not running or latest["state"] == "running"):
            card["progress"].update(latest["progress"])
            if latest["state"] == "done" and state == "idle":
                card["state"] = "done"
            elif latest["state"] == "cancelled" and state == "idle":
                card["state"] = "cancelled"
        if kind in ("downloads", "requests") and "download_engines" in self._settings:
            selected = self._settings["download_engines"]
            selected = selected if isinstance(selected, dict) else {}
            card["engines"] = {role: selected[role] for role in ("search", "fetch") if selected.get(role)}
        if kind == "downloads":
            card["auto_download"] = bool(self._settings.get("auto_download"))
            card["default_format"] = bulk_download_format(self._settings.get("bulk_download_format"))
            # The Audio files destination is a host-local path; a
            # paired caller gets the card without it, the way
            # /api/process withholds its logs from non-host callers.
            card["destination"] = (str(self._settings.get("music_folder") or "")
                                   if host else "")
        return card

    @staticmethod
    def _default_scope(kind):
        if kind == "downloads":
            return {"type": "all"}
        if kind == "genres":
            return {"type": "library"}
        if kind in ("reclaim", "migrate"):
            return {"type": "library"}
        return {"type": "queue"}

    def _progress(self, kind, scope):
        if kind == "reclaim":
            return {"dead_bytes": self._media_dead_bytes() or 0}
        if kind == "migrate":
            status = self._migration_status()
            return {"done": status.get("done", 0),
                    "total": status.get("total", 0),
                    "migrated": status.get("migrated", 0),
                    "skipped": status.get("skipped", 0),
                    "already": status.get("already", 0)}
        catalog = self._catalog()
        if kind == "requests":
            return {"pending": self._pending_requests()}
        total = sum(1 for record in catalog.records
                    if record.get("active", True))
        if kind == "genres":
            categorized = sum(1 for record in catalog.records
                              if record.get("active", True)
                              and catalog.genre_entry(record))
            return {"categorized": categorized, "total": total}
        summary = catalog.summary
        if scope.get("type") == "list":
            stats = summary.get("playlists", {}).get(scope.get("id"), {})
            return {"attempted": stats.get("attempted", 0),
                    "total": stats.get("entries", 0),
                    "verified": stats.get("playable", 0)}
        return {"attempted": summary.get("attempted", 0),
                "total": total,
                "verified": summary.get("verified", 0)}

    def _setup_blocker(self, kind):
        """Why this job cannot start right now, setup causes only.

        A busy library is not here anymore: that is a queue
        position, not a blocker (ADR 0002). What remains is the
        setup the job needs whenever it does start, checked at
        enqueue time and again at dispatch time.
        """
        if kind == "downloads":
            if not self._lists_ready():
                return ("Import or link a music list first "
                        "(Settings, Sources).")
            if not self._download_ready():
                return self._download_blocker()
        elif kind == "genres":
            if not self._genre_tools_ready():
                return GENRE_SETUP_BLOCKER
        elif kind == "requests":
            if not self._download_ready():
                return self._download_blocker()
        return ""

    def _download_blocker(self):
        if "download_engines" in self._settings:
            return "Choose an available search and download engine in Settings → Extensions."
        return DOWNLOAD_SETUP_BLOCKER

    def _dispatch_blocker(self, kind, scope):
        """The setup check as the dispatcher sees it: the enqueue
        checks, plus the scope's own survival (a list-scoped job
        whose list was deleted while it waited fails instead of
        starting against nothing)."""
        blocker = self._setup_blocker(kind)
        if blocker:
            return blocker
        if kind == "downloads" and scope.get("type") == "list":
            names = {entry["id"] for entry in self._playlist_options()}
            if scope.get("id") not in names:
                return LIST_GONE_BLOCKER
        return ""

    # -- actions -----------------------------------------------------------

    def act(self, kind, action, scope=None):
        """Start or stop one job. Raises ValueError with the
        card-ready message when the action cannot run."""
        with self._lock:
            if kind not in KINDS:
                raise ValueError("Unknown job")
            if action == "start":
                self._start(kind, scope)
            elif action == "stop":
                self._stop(kind)
            else:
                raise ValueError("Choose start or stop")

    def _start(self, kind, scope):
        self._reconcile(kind)
        if self._worker_running(kind):
            raise ValueError("That job is already running")
        if (self._queue.queued_for(kind) is not None
                or self._queue.running_for(kind) is not None):
            raise ValueError("That job is already waiting in the queue")
        # The request's own shape is validated before live-state
        # blockers, so a malformed scope fails the same way on any
        # server state.
        record_scope = (self._download_scope(scope) if kind == "downloads"
                        else self._default_scope(kind))
        blocker = self._setup_blocker(kind)
        if blocker:
            raise ValueError(blocker)
        if kind == "downloads" and record_scope["type"] == "all":
            # Exactly the old bulk_start's arming: an all-lists
            # console start turns background downloading on. A
            # one-list job never touches the setting, as before.
            self._settings["auto_download"] = True
            self._save_settings()
        self._queue.enqueue(kind, record_scope)
        # With the library free the job starts in this same request;
        # with it busy, the row waits for the watchdog's dispatch.
        self.dispatch()

    def _download_scope(self, scope):
        scope = scope if isinstance(scope, dict) else {}
        mode = scope.get("format")
        if mode is not None and (not isinstance(mode, str) or mode not in BULK_DOWNLOAD_FORMATS):
            raise ValueError("Choose MP3 first, MP3 only, or lossless only.")
        selected = {"format": mode} if mode is not None else {}
        scope_type = scope.get("type", "all")
        if scope_type == "all":
            return {"type": "all", **selected}
        if scope_type == "list":
            key = str(scope.get("id", ""))
            names = {entry["id"]: entry["name"]
                     for entry in self._playlist_options()}
            if key not in names:
                raise ValueError("Choose one of your lists, or all lists")
            return {"type": "list", "id": key, "name": names[key], **selected}
        raise ValueError("Choose one of your lists, or all lists")

    def _stop(self, kind):
        self._reconcile(kind)
        queued = self._queue.queued_for(kind)
        if queued is not None:
            # Stop on a waiting job cancels it; it never runs.
            self._queue.mark_cancelled(queued["id"])
            if kind == "downloads" and queued["scope"].get("type") == "all":
                self._disarm_downloads()
        row = self._queue.running_for(kind)
        if row is not None:
            # Mark first: the reconcile pass must not read this
            # user stop as a natural finish while the worker dies.
            self._queue.mark_cancelled(row["id"])
        if self._worker_running(kind):
            self._stop_worker(kind)
        if kind == "downloads":
            # Exactly the old bulk_stop: stopping downloads also
            # disarms the watchdog, or it would restart the batch
            # within seconds.
            self._disarm_downloads()

    def _disarm_downloads(self):
        self._settings["auto_download"] = False
        self._save_settings()

    # -- the dispatcher ------------------------------------------------------

    def dispatch(self):
        """Start the oldest queued job if the library is free.

        The watchdog calls this on its cadence; act() calls it
        after an enqueue. Returns True when the pass consumed the
        queue's head (started it, or failed it visibly), False when
        there was nothing to dispatch. Mutual exclusion stays: one
        worker at a time, now with a line instead of a refusal.
        """
        with self._lock:
            for kind in KINDS:
                self._reconcile(kind)
            if self._draining() or any(self._worker_running(kind) for kind in KINDS):
                return False
            for row in self._queue.queued():
                if row["progress"].get("retry_after", 0) > time.time():
                    continue
                kind = row["kind"]
                blocker = self._dispatch_blocker(kind, row["scope"])
                if blocker:
                    waiting_for_engine = (kind in ("downloads", "requests")
                                          and "download_engines" in self._settings
                                          and blocker == self._download_blocker())
                    if waiting_for_engine:
                        continue
                    self._queue.mark_failed(row["id"], blocker)
                    return True
                if (kind in ("downloads", "requests") and "download_engines" in self._settings
                        and not self._acquisition_ready()):
                    continue
                if kind == "downloads":
                    self._refresh_library()
                if kind == "genres":
                    self._queue.record_progress(row["id"], {"checked": 0})
                elif row["progress"].get("engine_retry"):
                    self._queue.record_progress(row["id"], {
                        key: value for key, value in row["progress"].items()
                        if key not in ("engine_retry", "retry_after")
                    })
                if self._start_worker(kind, self._worker_scope(row)):
                    self._queue.mark_running(row["id"])
                elif kind in ("downloads", "requests") and "download_engines" in self._settings and not self._acquisition_ready():
                    continue
                elif not self._draining():
                    self._queue.mark_failed(
                        row["id"], f"{KIND_LABELS[kind]} could not start; "
                                   "try again in a moment")
                return True
            return False
