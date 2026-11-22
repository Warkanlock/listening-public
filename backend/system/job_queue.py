"""The durable job queue (ADR 0002, plan 050 Step 2).

A job is a unit of background work as the person sees it in the
Jobs console. Start used to spawn the worker immediately and a
busy library answered with a blocker; now Start enqueues a row in
this table, in the state database beside the other stores (plan
036, plan 043), and the watchdog, acting as dispatcher, starts the
oldest queued job whenever no worker is running. Because the queue
is a table, it survives a server restart with its order intact.

States: queued, running, done, failed, cancelled. A job whose
blocker appears between enqueue and dispatch is marked failed with
the blocker as its error, visible on its card; nothing retries
silently. This queue holds work, never user data: the song
requests queue is a different table in downloads.requests_store
and stays untouched.
"""

from contextlib import closing
import json
import time
from pathlib import Path

from system import state_database

JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    enqueued_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS job_progress (
    job_id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS jobs_delete_progress AFTER DELETE ON jobs
BEGIN DELETE FROM job_progress WHERE job_id=OLD.id; END;
CREATE INDEX IF NOT EXISTS jobs_state_idx ON jobs (state, id);
"""

STATES = ("queued", "running", "done", "failed", "cancelled")
TERMINAL = ("done", "failed", "cancelled")


class JobQueue:
    """Queued and running jobs over the shared state database."""

    def __init__(self, database):
        self.database = Path(database)

    def _connect(self):
        return state_database.connect(self.database, JOBS_SCHEMA)

    @staticmethod
    def _row(record):
        return {"id": record[0], "kind": record[1],
                "scope": json.loads(record[2]), "state": record[3],
                "error": record[4], "enqueued_at": record[5],
                "started_at": record[6], "finished_at": record[7],
                "progress": json.loads(record[8]) if record[8] else {}}

    def _fetch(self, where, params=()):
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id, kind, scope, state, error, enqueued_at, "
                "started_at, finished_at, (SELECT payload FROM job_progress WHERE job_id = jobs.id) "
                f"FROM jobs {where} "
                "ORDER BY id", params).fetchall()
            return [self._row(record) for record in rows]
        finally:
            connection.close()

    # -- writes --------------------------------------------------------------

    def record_progress(self, job_id, progress):
        """A late report from a stopped worker cannot update another run."""
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO job_progress(job_id,payload) "
                       "SELECT id,? FROM jobs WHERE id=? AND state IN ('queued','running')",
                       (json.dumps(progress), job_id))

    def enqueue(self, kind, scope):
        """Add a job to the back of the queue; returns its row."""
        connection = self._connect()
        try:
            cursor = connection.execute(
                "INSERT INTO jobs (kind, scope, state, enqueued_at) "
                "VALUES (?, ?, 'queued', ?)",
                (kind, json.dumps(scope or {}), time.time()))
            job_id = cursor.lastrowid
            connection.commit()
        finally:
            connection.close()
        return self.get(job_id)

    def _transition(self, job_id, state, error=None):
        assignments = ["state = ?", "finished_at = ?"]
        params = [state, time.time()]
        if state == "running":
            assignments = ["state = ?", "started_at = ?"]
            params = [state, time.time()]
        if error is not None:
            assignments.append("error = ?")
            params.append(error)
        params.append(job_id)
        connection = self._connect()
        try:
            connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?",
                params)
            connection.commit()
        finally:
            connection.close()
        if state in TERMINAL:
            self._prune(self.get(job_id)["kind"])
        return self.get(job_id)

    def mark_running(self, job_id):
        return self._transition(job_id, "running")

    def requeue(self, job_id):
        return self._transition(job_id, "queued")

    def mark_done(self, job_id):
        return self._transition(job_id, "done")

    def mark_failed(self, job_id, error):
        return self._transition(job_id, "failed", error=str(error)[:300])

    def mark_cancelled(self, job_id):
        return self._transition(job_id, "cancelled")

    def _prune(self, kind):
        """Keep only the newest terminal row per kind.

        The card reads the latest row; older finished jobs are
        history nobody renders, and the table stays the size of
        the work, not of the room's lifetime.
        """
        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM jobs WHERE kind = ? AND state IN "
                "('done', 'failed', 'cancelled') AND id NOT IN "
                "(SELECT MAX(id) FROM jobs WHERE kind = ? AND state IN "
                "('done', 'failed', 'cancelled'))", (kind, kind))
            connection.commit()
        finally:
            connection.close()

    # -- reads ---------------------------------------------------------------

    def get(self, job_id):
        rows = self._fetch("WHERE id = ?", (job_id,))
        return rows[0] if rows else None

    def queued(self):
        """Every waiting job, oldest first: the dispatch order."""
        return self._fetch("WHERE state = 'queued'")

    def next_queued(self):
        rows = self.queued()
        return rows[0] if rows else None

    def queued_for(self, kind):
        rows = self._fetch("WHERE state = 'queued' AND kind = ?", (kind,))
        return rows[0] if rows else None

    def running_for(self, kind):
        rows = self._fetch("WHERE state = 'running' AND kind = ?", (kind,))
        return rows[-1] if rows else None

    def latest_for(self, kind):
        rows = self._fetch("WHERE kind = ?", (kind,))
        return rows[-1] if rows else None

    def position(self, job_id):
        """The 1-based place in line among the queued jobs."""
        for index, row in enumerate(self.queued()):
            if row["id"] == job_id:
                return index + 1
        return None
