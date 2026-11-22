"""Durable record lengths learned from verified matches (plan 058).

A catalog record's length comes from its list row, and bare list
imports can leave it empty. The verification probe measures the real
duration when it judges a local file against such a record, but a
measurement kept nowhere is lost: records are rebuilt from the
portable lists on every prepare, so a patch written into a stored
record would be wiped by the next rebuild. Like the genre store
(plan 029), the backfill therefore lives in a side store in the state
database, keyed by the record's own key, written once and applied
whenever records are materialized for the catalog build.

The discipline is the genre store's, inverted: there a manual
correction outranks every automated source; here the list always
wins. The store only ever fills a length the list left empty, it is
written only at the moment a probe verifies a match for a length-less
record (with the duration that probe already measured; no new probing
exists for this), and applying it changes one field of an existing
record, never membership, order, or keys.
"""

import time
from pathlib import Path

from system import state_database

LENGTHS_SCHEMA = """
CREATE TABLE IF NOT EXISTS lengths (
    key TEXT PRIMARY KEY,
    duration REAL NOT NULL,
    stamp REAL NOT NULL
);
"""


class LengthStore:
    """The durable lengths store: recording key -> probed duration.

    Plan 058: the backing is the ``lengths`` table of the one state
    database, opened through the shared state-database module
    (plan 054). Writes land immediately, one row per set. There is no
    legacy file: lengths are always re-measurable by the discovery
    that produced them, so the store's own lifecycle is the whole
    story.
    """

    def __init__(self, database):
        self.database = Path(database)
        self._entries = None

    def _connect(self):
        return state_database.connect(self.database, LENGTHS_SCHEMA)

    def load(self):
        if self._entries is None:
            connection = self._connect()
            try:
                self._entries = {
                    key: duration
                    for key, duration in connection.execute(
                        "SELECT key, duration FROM lengths")}
            finally:
                connection.close()
        return self._entries

    def reload(self):
        self._entries = None
        return self.load()

    def stamp(self):
        """(count, newest write): lets a reader spot outside writes."""
        connection = self._connect()
        try:
            return state_database.table_stamp(connection, "lengths")
        finally:
            connection.close()

    def get(self, key):
        return self.load().get(key)

    def set(self, key, duration):
        duration = float(duration)
        if not duration > 0:
            return None
        connection = self._connect()
        try:
            connection.execute(
                "INSERT OR REPLACE INTO lengths (key, duration, stamp) "
                "VALUES (?, ?, ?)", (key, duration, time.time()))
            connection.commit()
        finally:
            connection.close()
        self.load()[key] = duration
        return duration
