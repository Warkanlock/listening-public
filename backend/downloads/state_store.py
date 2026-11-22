"""Small durable state: the download cursor and the probe cache.

Plan 036 moved ``cursor.json`` and ``probe-cache.json`` into
the one SQLite database (the application folder's
``requests.sqlite3``, plan 043) beside the request
queue, so the whole state travels as a single file. This
module owns all SQL for those two tables, following the plan 005
pattern: callers see plain values and dicts, never SQL. The legacy
files are read from ``state-backup/`` beside the database by
default, where plan 043 adoption puts them.

The cursor keeps its exact semantics: a JSON object with ``next_offset``
in a single ``kv`` row. The probe cache keeps its stamp-keyed semantics:
an entry counts only while the file's size and mtime still match its
stamp and its shape version is current.

The legacy files are imported once, on first use, when the tables are
empty and the files exist; imported files are left in place as a backup
and never read again. An unreadable legacy file imports nothing and
never raises: the pipeline has always treated a torn cursor or cache as
"start fresh", and that tolerance is preserved.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from system import state_database

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS probe_cache (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    version INTEGER NOT NULL,
    media TEXT NOT NULL
);
"""

CURSOR_KEY = "download_cursor"


class StateStore:
    """Cursor and probe cache over the shared state database."""

    def __init__(self, database, state_dir=None):
        self.database = Path(database)
        self.state_dir = Path(state_dir) if state_dir else \
            self.database.parent / "state-backup"
        self._import_checked = False

    @property
    def cursor_path(self) -> Path:
        return self.state_dir / "cursor.json"

    @property
    def probe_cache_path(self) -> Path:
        return self.state_dir / "probe-cache.json"

    def _connect(self) -> sqlite3.Connection:
        return state_database.connect(self.database, SCHEMA)

    # -- one-time import -------------------------------------------------

    def _ensure_imported(self, connection) -> None:
        if self._import_checked:
            return
        self._import_checked = True
        if self.cursor_path.is_file() and connection.execute(
                "SELECT COUNT(*) FROM kv WHERE key = ?",
                (CURSOR_KEY,)).fetchone()[0] == 0:
            self._import_cursor(connection)
        if self.probe_cache_path.is_file() and connection.execute(
                "SELECT COUNT(*) FROM probe_cache").fetchone()[0] == 0:
            self._import_probes(connection)

    def _import_cursor(self, connection) -> None:
        try:
            payload = json.loads(self.cursor_path.read_text(encoding="utf-8"))
            offset = int(payload["next_offset"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            if connection.execute(
                    "SELECT COUNT(*) FROM kv WHERE key = ?",
                    (CURSOR_KEY,)).fetchone()[0]:
                connection.execute("ROLLBACK")
                return
            connection.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?)",
                (CURSOR_KEY, json.dumps({"next_offset": offset})))
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        print(f"Imported the download cursor ({offset}) from "
              f"{self.cursor_path} into {self.database}", flush=True)

    def _import_probes(self, connection) -> None:
        try:
            payload = json.loads(
                self.probe_cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        entries = []
        for path, entry in payload.items():
            if not isinstance(entry, dict):
                continue
            stamp = entry.get("stamp")
            if (not isinstance(stamp, (list, tuple)) or len(stamp) != 2
                    or "media" not in entry):
                continue
            entries.append((str(path), int(stamp[0]), int(stamp[1]),
                            int(entry.get("version") or 0),
                            json.dumps(entry["media"])))
        connection.execute("BEGIN IMMEDIATE")
        try:
            if connection.execute(
                    "SELECT COUNT(*) FROM probe_cache").fetchone()[0]:
                connection.execute("ROLLBACK")
                return
            connection.executemany(
                "INSERT INTO probe_cache (path, size, mtime_ns, version, "
                "media) VALUES (?, ?, ?, ?, ?)", entries)
            imported = connection.execute(
                "SELECT COUNT(*) FROM probe_cache").fetchone()[0]
            if imported != len(entries):
                raise RuntimeError(
                    f"probe cache import mismatch: file holds "
                    f"{len(entries)} entries, {imported} imported; "
                    f"files left authoritative")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        print(f"Imported the probe cache ({len(entries)} entries) from "
              f"{self.probe_cache_path} into {self.database}", flush=True)

    # -- download cursor ---------------------------------------------------

    def cursor_offset(self) -> int:
        connection = self._connect()
        try:
            self._ensure_imported(connection)
            row = connection.execute(
                "SELECT value FROM kv WHERE key = ?",
                (CURSOR_KEY,)).fetchone()
            if row is None:
                return 0
            try:
                return int(json.loads(row[0])["next_offset"])
            except (ValueError, KeyError, TypeError):
                return 0
        finally:
            connection.close()

    def write_cursor(self, offset) -> None:
        connection = self._connect()
        try:
            self._ensure_imported(connection)
            connection.execute(
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                (CURSOR_KEY, json.dumps({"next_offset": int(offset)})))
            connection.commit()
        finally:
            connection.close()

    # -- probe cache ---------------------------------------------------------

    def probes(self) -> dict:
        """The cache in its in-memory shape: path -> stamp/version/media."""
        connection = self._connect()
        try:
            self._ensure_imported(connection)
            return {path: {"stamp": [size, mtime_ns], "version": version,
                           "media": json.loads(media)}
                    for path, size, mtime_ns, version, media in
                    connection.execute(
                        "SELECT path, size, mtime_ns, version, media "
                        "FROM probe_cache")}
        finally:
            connection.close()

    def put_probes(self, cache) -> None:
        connection = self._connect()
        try:
            self._ensure_imported(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM probe_cache")
                connection.executemany(
                    "INSERT INTO probe_cache (path, size, mtime_ns, "
                    "version, media) VALUES (?, ?, ?, ?, ?)",
                    [(str(path), int(entry["stamp"][0]),
                      int(entry["stamp"][1]), int(entry["version"]),
                      json.dumps(entry["media"]))
                     for path, entry in cache.items()])
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

    def probe_entry(self, path):
        """One cache entry in its in-memory shape, or None.

        The single-row read beside probes(): the HTTP server looks
        up the playing track's facts without loading the whole
        cache the runner maintains.
        """
        connection = self._connect()
        try:
            self._ensure_imported(connection)
            row = connection.execute(
                "SELECT size, mtime_ns, version, media FROM probe_cache "
                "WHERE path = ?", (str(path),)).fetchone()
            if row is None:
                return None
            size, mtime_ns, version, media = row
            return {"stamp": [size, mtime_ns], "version": version,
                    "media": json.loads(media)}
        finally:
            connection.close()

    def put_probe(self, path, entry) -> None:
        """Insert or replace one cache entry.

        The single-row write beside put_probes(): the HTTP server
        writes back the entry its on-demand probe produced, leaving
        the runner's whole-cache cycle untouched.
        """
        connection = self._connect()
        try:
            self._ensure_imported(connection)
            connection.execute(
                "INSERT OR REPLACE INTO probe_cache (path, size, "
                "mtime_ns, version, media) VALUES (?, ?, ?, ?, ?)",
                (str(path), int(entry["stamp"][0]),
                 int(entry["stamp"][1]), int(entry["version"]),
                 json.dumps(entry["media"])))
            connection.commit()
        finally:
            connection.close()
