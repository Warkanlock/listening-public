"""Favorites: the room's liked songs (plan 048).

A favorite is a durable mark on a recording, stored against the
record's own key (the identity genres, lyrics, and play events
already use), never its positional catalog index: indexes can move
when a rebuild lands the same recordings in a different order, and
a favorite must survive that. Downloaded requests outside the catalog use their
persistent playback reference (custom-<id>) as the key.
Both are stored in the state database (plan 036: the ``favorites`` table of the application
folder's ``requests.sqlite3``, plan 043), NOT inside the catalog
build that prepare rebuilds, so a rebuild can never erase a like.
No legacy import: the feature is new, there is nothing to import.

Favorites are the room's, not a device's: one household, one set.
Besides the UI they feed Deep Dive, which seeds its weekly radio
from favorite artists first, most recently favorited first.
"""

import time
from pathlib import Path

from system import state_database

FAVORITES_SCHEMA = """
CREATE TABLE IF NOT EXISTS favorites (
    key TEXT PRIMARY KEY,
    stamp REAL NOT NULL
);
"""


class FavoritesStore:
    """The durable favorites store: recording key -> liked stamp.

    Modeled on GenreStore: writes land immediately, one row per
    set, and the in-memory map mirrors the table for this process,
    updated on every write through it. ``stamp()`` reads the table
    directly, so the Catalog can spot writes from another process
    and Deep Dive can fingerprint its cache with the newest like.
    """

    def __init__(self, database):
        self.database = Path(database)
        self._stamps = None

    def _connect(self):
        return state_database.connect(self.database, FAVORITES_SCHEMA)

    def load(self):
        if self._stamps is None:
            connection = self._connect()
            try:
                self._stamps = {key: stamp for key, stamp in connection.execute(
                    "SELECT key, stamp FROM favorites")}
            finally:
                connection.close()
        return self._stamps

    def reload(self):
        self._stamps = None
        return self.load()

    def stamp(self):
        """(count, newest write): lets the Catalog spot outside writes."""
        connection = self._connect()
        try:
            return state_database.table_stamp(connection, "favorites")
        finally:
            connection.close()

    def keys(self):
        """The current favorite keys, as a set."""
        return set(self.load())

    def recent(self):
        """Favorite keys, most recently favorited first."""
        return [key for key, _stamp in sorted(
            self.load().items(), key=lambda item: item[1], reverse=True)]

    def set(self, key, on, *, aliases=()):
        """Mark or unmark a recording; returns the new state.

        Marking stamps the row with the write time, so re-liking a
        song makes it the most recent favorite; unmarking deletes
        the row outright, there is no tombstone state to read back.
        """
        connection = self._connect()
        try:
            if on:
                stamp = time.time()
                connection.execute(
                    "INSERT OR REPLACE INTO favorites (key, stamp) "
                    "VALUES (?, ?)", (key, stamp))
            else:
                stamp = None
                connection.executemany("DELETE FROM favorites WHERE key = ?",
                                       ((value,) for value in {key, *aliases}))
            connection.commit()
        finally:
            connection.close()
        stamps = self.load()
        if on:
            stamps[key] = stamp
        else:
            for value in {key, *aliases}:
                stamps.pop(value, None)
        return bool(on)
