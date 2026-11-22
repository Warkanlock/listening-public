"""The catalog's durable state: build, coverage, and report facts.

Plan 036 moved the catalog out of ``manifest.json``,
``coverage.csv``, and ``summary.json`` into the one SQLite
database that already holds the request queue and the metadata cache
(the application folder's ``requests.sqlite3``, plan 043), so the
whole state travels as a single file. This module owns all SQL for the catalog tables, following
the plan 005 pattern: callers see records and rows as plain dicts, never
SQL. The Catalog keeps its in-memory shape (records, playlist membership,
issues, covers); the summary is derived from these tables on read and is
never stored.

The legacy files are imported once, on first use, when the tables are
empty and the files exist. The import is count-verified (records,
coverage rows); on a mismatch it aborts loudly and the files stay
authoritative. Imported files are left in place as a backup and are
never read again. The same discipline as the plan 032 support-folder
adoption: the database wins once both exist. The files are read from
``state-backup/`` beside the database by default (plan 043: adoption
moves them there); an explicit ``state_dir`` overrides the location.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from pathlib import Path

from library import playlist_links
from system import state_database

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_records (
    key TEXT PRIMARY KEY,
    idx INTEGER NOT NULL,
    record TEXT NOT NULL,
    memberships TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage (
    idx INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    uri TEXT NOT NULL,
    path TEXT NOT NULL,
    reason TEXT NOT NULL
);
"""

COVERAGE_FIELDS = ("status", "artist", "title", "uri", "path", "reason")


def connect(database) -> sqlite3.Connection:
    """Open the shared state database with the queue store's discipline."""
    connection = state_database.connect(database, SCHEMA)
    # Plan 045: coverage gained updated_at (when the row's status was
    # written). CREATE TABLE IF NOT EXISTS never adds a column to an
    # existing table, so the add is explicit and in place; existing
    # rows read NULL, "verified at an unknown time".
    columns = {row[1] for row in connection.execute("PRAGMA table_info(coverage)")}
    if "updated_at" not in columns:
        connection.execute("ALTER TABLE coverage ADD COLUMN updated_at REAL")
        connection.commit()
    return connection


class CatalogStore:
    """Catalog build, coverage, and report facts over the state database."""

    def __init__(self, database, state_dir=None):
        self.database = Path(database)
        self.state_dir = Path(state_dir) if state_dir else \
            self.database.parent / "state-backup"
        self._import_checked = False

    # -- paths of the legacy files this store supersedes ----------------

    @property
    def manifest_path(self) -> Path:
        return self.state_dir / "manifest.json"

    @property
    def coverage_path(self) -> Path:
        return self.state_dir / "coverage.csv"

    @property
    def summary_path(self) -> Path:
        return self.state_dir / "summary.json"

    # -- one-time import -------------------------------------------------

    def _ensure_imported(self, connection) -> None:
        if self._import_checked:
            return
        self._import_checked = True
        build_empty = connection.execute(
            "SELECT COUNT(*) FROM catalog_records").fetchone()[0] == 0
        coverage_empty = connection.execute(
            "SELECT COUNT(*) FROM coverage").fetchone()[0] == 0
        if build_empty and self.manifest_path.is_file():
            self._import_manifest(connection)
        if coverage_empty and self.coverage_path.is_file():
            self._import_coverage(connection)
        if self._meta_get(connection, "report_facts") is None \
                and self.summary_path.is_file():
            self._import_report_facts(connection)

    def _import_manifest(self, connection) -> None:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # unreadable legacy file: nothing authoritative to import
        if not isinstance(payload, dict):
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            if connection.execute(
                    "SELECT COUNT(*) FROM catalog_records").fetchone()[0]:
                connection.execute("ROLLBACK")
                return
            self._write_build_rows(connection, payload)
            expected = len(payload.get("records", []))
            imported = connection.execute(
                "SELECT COUNT(*) FROM catalog_records").fetchone()[0]
            if imported != expected:
                raise RuntimeError(
                    f"catalog import mismatch: manifest holds {expected} "
                    f"records, {imported} imported; files left authoritative")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        print(f"Imported the catalog ({expected} records) from "
              f"{self.manifest_path} into {self.database}", flush=True)

    def _import_coverage(self, connection) -> None:
        try:
            text = self.coverage_path.read_text(encoding="utf-8")
            parsed = list(csv.DictReader(io.StringIO(text)))
        except (OSError, csv.Error, ValueError):
            return
        rows = []
        for raw in parsed:
            try:
                index = int(raw.get("index") or 0)
            except (TypeError, ValueError):
                continue
            rows.append({"index": index,
                         **{field: raw.get(field) or ""
                            for field in COVERAGE_FIELDS}})
        connection.execute("BEGIN IMMEDIATE")
        try:
            if connection.execute(
                    "SELECT COUNT(*) FROM coverage").fetchone()[0]:
                connection.execute("ROLLBACK")
                return
            self._write_coverage_rows(connection, rows, None)
            self._bump(connection, "coverage_stamp")
            imported = connection.execute(
                "SELECT COUNT(*) FROM coverage").fetchone()[0]
            if imported != len(rows):
                raise RuntimeError(
                    f"coverage import mismatch: file holds {len(rows)} "
                    f"rows, {imported} imported; files left authoritative")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        print(f"Imported coverage ({len(rows)} rows) from "
              f"{self.coverage_path} into {self.database}", flush=True)

    def _import_report_facts(self, connection) -> None:
        """Seed the report facts from the legacy summary file.

        Only the figures a report run alone can know are facts; the
        rest of the old summary was derived from the manifest and the
        coverage and is derived again on read. The attempted horizon
        was never stored in the file, so it is not recoverable here:
        attempted reads zero until the next report run, as it does
        before any first report.
        """
        try:
            payload = json.loads(self.summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        facts = {key: payload[key]
                 for key in ("unmatched_files", "ambiguous_files",
                             "library_gib", "free_gib")
                 if isinstance(payload.get(key), (int, float))}
        if not facts:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            if self._meta_get(connection, "report_facts") is not None:
                connection.execute("ROLLBACK")
                return
            self._meta_set(connection, "report_facts", facts)
            self._bump(connection, "facts_stamp")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        print(f"Imported report facts from {self.summary_path} "
              f"into {self.database}", flush=True)

    # -- catalog build ---------------------------------------------------

    def _meta_get(self, connection, key):
        row = connection.execute(
            "SELECT value FROM catalog_meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _meta_set(self, connection, key, value) -> None:
        if value is None:
            connection.execute(
                "DELETE FROM catalog_meta WHERE key = ?", (key,))
        else:
            connection.execute(
                "INSERT OR REPLACE INTO catalog_meta (key, value) "
                "VALUES (?, ?)", (key, json.dumps(value)))

    def _bump(self, connection, key) -> None:
        value = self._meta_get(connection, key) or 0
        self._meta_set(connection, key, value + 1)

    def _counter(self, key) -> int:
        connection = connect(self.database)
        try:
            self._ensure_imported(connection)
            return self._meta_get(connection, key) or 0
        finally:
            connection.close()

    def build_stamp(self) -> int:
        """Bumped by every build write; the Catalog reloads when it moves."""
        return self._counter("build_stamp")

    def coverage_stamp(self) -> int:
        """Bumped by every coverage write."""
        return self._counter("coverage_stamp")

    def facts_stamp(self) -> int:
        """Bumped by every report-facts write."""
        return self._counter("facts_stamp")

    def source_signature(self, require_source_index=False):
        connection = connect(self.database)
        try:
            self._ensure_imported(connection)
            if require_source_index and not self._meta_get(connection, "playlist_source_index"):
                return None
            return self._meta_get(connection, "source_signature")
        finally:
            connection.close()

    def audio_signature(self):
        connection = connect(self.database)
        try:
            self._ensure_imported(connection)
            return self._meta_get(connection, "audio_signature")
        finally:
            connection.close()

    def has_build(self) -> bool:
        connection = connect(self.database)
        try:
            self._ensure_imported(connection)
            return connection.execute(
                "SELECT COUNT(*) FROM catalog_records").fetchone()[0] > 0 \
                or self._meta_get(connection, "source_signature") is not None
        finally:
            connection.close()

    def read_build(self):
        """The manifest payload dict, or None when no build exists yet."""
        connection = connect(self.database)
        try:
            self._ensure_imported(connection)
            rows = connection.execute(
                "SELECT idx, record, memberships FROM catalog_records "
                "ORDER BY idx").fetchall()
            sizes = self._meta_get(connection, "playlist_sizes")
            if not rows and sizes is None:
                return None
            playlists = {name: [None] * size
                         for name, size in (sizes or {}).items()}
            records = []
            for index, record_text, memberships_text in rows:
                records.append(json.loads(record_text))
                for name, positions in json.loads(memberships_text).items():
                    entries = playlists.setdefault(name, [])
                    for position in positions:
                        while len(entries) <= position:
                            entries.append(None)
                        entries[position] = index
            return {
                "records": records,
                "playlists": playlists,
                "issues": self._meta_get(connection, "issues") or [],
                "covers": self._meta_get(connection, "covers"),
                "list_names": self._meta_get(connection, "list_names"),
                "source_signature": self._meta_get(
                    connection, "source_signature"),
            }
        finally:
            connection.close()

    def _write_build_rows(self, connection, payload) -> None:
        records = payload.get("records", [])
        playlists = payload.get("playlists", {})
        memberships = {}
        for name, entries in playlists.items():
            for position, index in enumerate(entries):
                if index is not None:
                    memberships.setdefault(index, {}).setdefault(
                        name, []).append(position)
        connection.execute("DELETE FROM catalog_records")
        connection.executemany(
            "INSERT INTO catalog_records (key, idx, record, memberships) "
            "VALUES (?, ?, ?, ?)",
            [(record.get("key", ""), record["index"],
              json.dumps(record),
              json.dumps(memberships.get(record["index"], {})))
             for record in records])
        self._meta_set(connection, "source_signature",
                       payload.get("source_signature"))
        self._meta_set(connection, "audio_signature",
                       payload.get("audio_signature"))
        self._meta_set(connection, "issues", payload.get("issues", []))
        self._meta_set(connection, "covers", payload.get("covers"))
        self._meta_set(connection, "list_names", payload.get("list_names"))
        self._meta_set(connection, "playlist_sizes",
                       {name: len(entries)
                        for name, entries in playlists.items()})
        self._bump(connection, "build_stamp")

    def write_build(self, payload, *, sources=None, portable_lists=None, expected_lists=None) -> None:
        connection = connect(self.database)
        try:
            self._ensure_imported(connection)
            if sources is not None:
                connection.executescript(playlist_links.SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            try:
                if sources is not None:
                    self._meta_set(connection, "playlist_source_index", 1)
                    playlist_links.project(connection, payload["records"], payload["playlists"],
                                           payload["list_names"], sources)
                if portable_lists is not None:
                    previous = dict(connection.execute("SELECT name,content FROM collection_lists"))
                    if previous != expected_lists:
                        raise RuntimeError("Portable lists changed during refresh. Retry the catalog build.")
                    connection.executemany("DELETE FROM collection_lists WHERE name=?",
                                           ((name,) for name in previous.keys() - portable_lists.keys()))
                    connection.executemany("INSERT OR REPLACE INTO collection_lists VALUES (?,?)",
                                           ((name, content) for name, content in portable_lists.items()
                                            if previous.get(name) != content))
                self._write_build_rows(connection, payload)
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

    # -- coverage ----------------------------------------------------------

    def _write_coverage_rows(self, connection, rows, stamp) -> None:
        connection.execute("DELETE FROM coverage")
        connection.executemany(
            "INSERT INTO coverage (idx, status, artist, title, uri, path, "
            "reason, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(int(row["index"]),)
             + tuple(str(row.get(field) or "") for field in COVERAGE_FIELDS)
             + (stamp,)
             for row in rows])

    def write_coverage(self, rows) -> None:
        connection = connect(self.database)
        try:
            self._ensure_imported(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._write_coverage_rows(connection, rows, time.time())
                self._bump(connection, "coverage_stamp")
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

    def update_coverage_path(self, index, path) -> None:
        """Rewrite one coverage row's path in place (plan 062).

        The library migration moves songs one at a time, reference
        by reference; write_coverage's whole-table rewrite is the
        report's shape, not a per-file one. The coverage stamp
        bumps exactly as a full write's does, so readers notice.
        """
        connection = connect(self.database)
        try:
            self._ensure_imported(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "UPDATE coverage SET path = ?, updated_at = ? "
                    "WHERE idx = ?",
                    (str(path), time.time(), int(index)))
                self._bump(connection, "coverage_stamp")
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

    def read_coverage(self) -> list:
        connection = connect(self.database)
        try:
            self._ensure_imported(connection)
            return [{"index": index,
                     **dict(zip(COVERAGE_FIELDS, values)),
                     "updated_at": updated_at}
                    for index, *values, updated_at in connection.execute(
                        "SELECT idx, status, artist, title, uri, path, "
                        "reason, updated_at FROM coverage ORDER BY idx")]
        finally:
            connection.close()

    # -- report facts (the derived summary's stored inputs) ---------------

    def write_report_facts(self, facts) -> None:
        connection = connect(self.database)
        try:
            self._ensure_imported(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._meta_set(connection, "report_facts", facts)
                self._bump(connection, "facts_stamp")
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

    def report_facts(self):
        connection = connect(self.database)
        try:
            self._ensure_imported(connection)
            return self._meta_get(connection, "report_facts")
        finally:
            connection.close()

    def has_report_for_build(self) -> bool:
        facts = self.report_facts()
        return facts is not None and facts.get("build_stamp") == self.build_stamp()

    def has_report(self) -> bool:
        return self.report_facts() is not None
