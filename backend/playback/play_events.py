"""Play events: the room's record of what it played (plan 045).

One append-only table in the shared state database
(``requests.sqlite3`` in the application folder): a ``started``
row when a track becomes the shared session's current play, and
one terminal row (``completed`` or ``skipped``) when that play
ends, with the seconds listened and the device whose action
caused the event, when the action carried one. The session emits
the semantics through an injected callable; this module owns the
SQL, following the plan 005 pattern: operations cross the seam,
never SQL, and one short connection serves each operation.

Events are keyed by the record's stable ``key`` (the audit's rule
for new tables), with the session ref and catalog index kept
because every existing surface addresses tracks by them. Rows are
never updated or deleted: no retention rule exists yet, by the
plan's decision.

Recording is best-effort by design: a state-database hiccup must
never break the room's playback, so ``record`` swallows
``sqlite3.Error`` with one warning line to stderr (the plan 022
one-line failure discipline) and the play goes on unrecorded.
"""

import sqlite3
import sys
import time
from pathlib import Path

from system import state_database

SCHEMA = """
CREATE TABLE IF NOT EXISTS play_events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    track_index INTEGER,
    record_key TEXT,
    request_id INTEGER,
    device_id TEXT,
    listened REAL
);
CREATE INDEX IF NOT EXISTS play_events_record_key
    ON play_events (record_key, ts);
CREATE INDEX IF NOT EXISTS play_events_ts
    ON play_events (ts);
"""


class PlayEventsStore:
    """Every operation on the play_events table at `database`."""

    def __init__(self, database):
        self.database = Path(database)

    def _connect(self) -> sqlite3.Connection:
        return state_database.connect(self.database, SCHEMA)

    def record(self, kind, ref, track_index=None, record_key=None,
               request_id=None, device_id=None, listened=None) -> None:
        """Append one event, stamped now. Never raises sqlite3.Error."""
        try:
            connection = self._connect()
            try:
                connection.execute(
                    "INSERT INTO play_events (ts, kind, ref, track_index, "
                    "record_key, request_id, device_id, listened) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (time.time(), kind, str(ref), track_index, record_key,
                     request_id, device_id, listened))
                connection.commit()
            finally:
                connection.close()
        except sqlite3.Error as error:
            print(f"Play event not recorded ({kind} {ref}): {error}",
                  file=sys.stderr, flush=True)

    def recent_started(self, limit) -> list:
        """The newest `started` events, newest first, as dicts."""
        connection = self._connect()
        try:
            return [{"ts": ts, "ref": ref, "device_id": device_id}
                    for ts, ref, device_id in connection.execute(
                        "SELECT ts, ref, device_id FROM play_events "
                        "WHERE kind = 'started' "
                        "ORDER BY ts DESC, id DESC LIMIT ?",
                        (int(limit),))]
        finally:
            connection.close()

    def stats(self, record_key, *aliases):
        """(play_count, last_played_ts) from `started` events for a key.

        (0, None) when the record was never played.
        """
        if record_key is None:
            return (0, None)
        connection = self._connect()
        try:
            keys = (record_key, *aliases)
            placeholders = ",".join("?" for _ in keys)
            count, last = connection.execute(
                "SELECT COUNT(*), MAX(ts) FROM play_events "
                f"WHERE kind = 'started' AND record_key IN ({placeholders})",
                keys).fetchone()
            return (count, last)
        finally:
            connection.close()

    def history(self):
        """Per-record aggregates for the Your listening lists
        (plan 050 step 5): {record_key: (last_played_ts,
        completed_count)} covering every record the room has
        played.

        last_played_ts is the newest `started` event's stamp, the
        recency `recent_started` already reads by; completed_count
        counts only `completed` terminal events, so a play that
        was skipped counts toward recency but never toward Most
        played. Custom-request plays carry no record key and are
        absent, like every key-keyed surface.
        """
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT record_key, MAX(CASE WHEN kind = 'started' THEN ts END), "
                "COUNT(CASE WHEN kind = 'completed' THEN 1 END) FROM play_events "
                "WHERE kind IN ('started', 'completed') AND record_key IS NOT NULL "
                "GROUP BY record_key")
            return {key: (last_played, completed) for key, last_played, completed in rows}
        finally:
            connection.close()
