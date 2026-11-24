"""The one home for the shared requests database.

The HTTP server and the download runner are separate processes that
share one SQLite file (`requests.sqlite3` in the workspace state
folder): the server enqueues and retries download requests and caches
external metadata in it, the runner claims queued requests and records
their outcomes. Both used to define the schema privately and sprinkle
raw SQL across their files, so any schema change had to be made twice
in lockstep (A1 S2, A3 DEBT-02).

This module owns the schema and every read and write. Operations, not
SQL strings, cross the seam: callers pass values and get plain dicts
back. The DDL below is the server's former text verbatim (the superset:
it creates `external_cache`, the runner's copy did not), applied with
CREATE TABLE IF NOT EXISTS on every connect, exactly as before, so a
database created by either old copy opens unchanged.

Connection behavior is unchanged too: WAL mode, a 30-second timeout,
and one short connection per operation.
"""

import sqlite3
import zlib
from pathlib import Path

from system import state_database
from system.formats import REQUEST_SUFFIXES


def mp3_retry(row):
    """The request and revision that can be retried after a lossless miss."""
    if row["status"] == "not_found" and row["requested_format"] in ("any_lossless", "flac", "alac"):
        return {"id": row["id"], "format_revision": row["format_revision"]}
    return None


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY,
        track_index INTEGER,
        artist TEXT NOT NULL,
        title TEXT NOT NULL,
        album TEXT NOT NULL DEFAULT '',
        length REAL,
        search_title TEXT NOT NULL DEFAULT '',
        musicbrainz_id TEXT NOT NULL DEFAULT '',
        version TEXT NOT NULL DEFAULT '',
        performance TEXT NOT NULL DEFAULT '',
        requested_format TEXT DEFAULT 'any_lossless',
        format_revision INTEGER NOT NULL DEFAULT 0,
        format_confirmed INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'queued',
        message TEXT NOT NULL DEFAULT '',
        audio_path TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE TABLE IF NOT EXISTS external_cache (key TEXT PRIMARY KEY, body TEXT NOT NULL, saved_at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS external_cache_age ON external_cache (saved_at)",
    # Plan 045: the queue's hot lookups (by_track, the status scans
    # the activity path polls) stop scanning the whole table.
    "CREATE INDEX IF NOT EXISTS requests_track_index ON requests (track_index)",
    "CREATE INDEX IF NOT EXISTS requests_status ON requests (status)",
    "CREATE INDEX IF NOT EXISTS requests_pick ON requests (artist, title, id DESC) WHERE track_index IS NULL",
)

# A request the user can still act on or that is already on its way.
ACTIVE_STATUSES = ("waiting_format", "queued", "searching", "waiting_space")

# The longest TTL any cache caller uses (MusicBrainz recording and
# per-track metadata lookups, 30 days). Rows older than that are expired
# under every caller's semantics, so a write prunes them opportunistically
# and the cache cannot grow forever (A3 PERF-08).
CACHE_PRUNE_AGE = 30 * 86400
# A burst of new searches should not evict song details, credits or lyrics.
SEARCH_MAX_ENTRIES = 8192
# A previous release can still use its plain JSON keys after a rollback.
_PACKED = "zlib:v1:"


class RequestsStore:
    """Every operation on the shared requests database at `path`."""

    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        """Open a connection with the schema in place.

        This is the low-level seam (startup, tests, tooling); the two
        processes otherwise go through the operations below.
        """
        conn = state_database.connect(self.path, SCHEMA, row_factory=sqlite3.Row)
        try:
            if not {"format_confirmed", "version", "musicbrainz_id", "performance"} <= {col[1] for col in conn.execute("PRAGMA table_info(requests)")}:
                # Another server or runner may be upgrading the same room.
                conn.execute("BEGIN IMMEDIATE")
                columns = {col[1] for col in conn.execute("PRAGMA table_info(requests)")}
                if "requested_format" not in columns:
                    conn.execute("ALTER TABLE requests ADD COLUMN requested_format TEXT DEFAULT 'any_lossless'")
                if "format_revision" not in columns:
                    conn.execute("ALTER TABLE requests ADD COLUMN format_revision INTEGER NOT NULL DEFAULT 0")
                if "format_confirmed" not in columns:
                    conn.execute("ALTER TABLE requests ADD COLUMN format_confirmed INTEGER NOT NULL DEFAULT 0")
                for column in ("musicbrainz_id", "version", "performance"):
                    if column not in columns:
                        conn.execute(f"ALTER TABLE requests ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
                conn.commit()
            conn.execute("CREATE INDEX IF NOT EXISTS requests_recording ON requests (musicbrainz_id, id DESC) "
                         "WHERE track_index IS NULL")
            return conn
        except Exception:
            conn.close()
            raise

    def initialize(self):
        """Create the database and schema if they do not exist yet."""
        self.connect().close()

    def _run(self, work):
        conn = self.connect()
        try:
            result = work(conn)
            conn.commit()
            return result
        finally:
            conn.close()

    # -- request lifecycle -------------------------------------------------

    def enqueue(self, track_index, artist, title, album, length, search_title):
        """Insert a queued request and return its new id."""
        def work(conn):
            cursor = conn.execute(
                "INSERT INTO requests (track_index,artist,title,album,length,search_title) "
                "VALUES (?,?,?,?,?,?)",
                (track_index, artist, title, album, length, search_title))
            return cursor.lastrowid
        return self._run(work)

    def create_or_find(self, index, artist, title, album, length, search_title, *, individual, musicbrainz_id="", version="", performance=""):
        """Serialize lookup and insert across HTTP handlers, including batch intake."""
        def work(conn):
            conn.execute("BEGIN IMMEDIATE")
            row = self._active(conn, index, artist, title, musicbrainz_id)
            if row:
                return dict(row), False
            if not individual:
                # A manual batch retry honors a previously confirmed individual format.
                previous = conn.execute(
                    "SELECT * FROM requests WHERE track_index=? ORDER BY id DESC LIMIT 1", (index,)).fetchone()
                if previous and previous["format_confirmed"] and previous["status"] in ("not_found", "error", "rejected"):
                    self._queue_retry(conn, previous["id"], previous["search_title"])
                    row = conn.execute("SELECT * FROM requests WHERE id=?", (previous["id"],)).fetchone()
                    return dict(row), True
            status = "waiting_format" if individual else "queued"
            cursor = conn.execute(
                "INSERT INTO requests (track_index,artist,title,album,length,search_title,status,requested_format,musicbrainz_id,version,performance) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (index, artist, title, album, length, search_title,
                                             status, None if individual else "any_lossless", musicbrainz_id, version, performance))
            row = conn.execute("SELECT * FROM requests WHERE id=?", (cursor.lastrowid,)).fetchone()
            return dict(row), True
        return self._run(work)

    def _lock_request(self, conn, request_id):
        """Serialize a release/retry with new intake for the same recording."""
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown song request")
        if row["status"] in ("not_found", "error", "rejected"):
            other = self._active(conn, row["track_index"], row["artist"], row["title"], row["musicbrainz_id"])
            if other:
                raise ValueError("Another request already exists for this song. Open it in Requests.")
        return row

    def confirm_format(self, request_id, choice, revision):
        """Each picker version can release at most one search, even after it finishes."""
        if not isinstance(choice, str) or choice not in REQUEST_SUFFIXES:
            raise ValueError("Choose Any lossless, FLAC, Apple Lossless, or MP3.")
        if type(revision) is not int or not 0 <= revision < 2**63 - 1:
            raise ValueError("Invalid format revision")
        def work(conn):
            row = self._lock_request(conn, request_id)
            if row["requested_format"] == choice and row["format_revision"] == revision + 1:
                return dict(row), False
            changed = conn.execute(
                "UPDATE requests SET requested_format=?, format_confirmed=1, status='queued', message='', "
                "format_revision=format_revision+1, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND format_revision=? AND status IN ('waiting_format','not_found','error','rejected')",
                (choice, request_id, revision)).rowcount
            if not changed:
                raise ValueError("This request changed. Reopen it to choose a format.")
            row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            return dict(row), True
        return self._run(work)

    def get(self, request_id):
        """One request row by id, as a dict, or None."""
        def work(conn):
            row = conn.execute("SELECT * FROM requests WHERE id=?",
                               (request_id,)).fetchone()
            return dict(row) if row else None
        return self._run(work)

    def queued(self, limit):
        """Queued requests, oldest first, up to `limit`, as dicts."""
        def work(conn):
            return [dict(row) for row in conn.execute(
                "SELECT * FROM requests WHERE status = 'queued' ORDER BY id LIMIT ?",
                (limit,)).fetchall()]
        return self._run(work)

    def claim(self, request_id):
        """Move a queued request to searching (the runner picking it up)."""
        def work(conn):
            return bool(conn.execute(
                "UPDATE requests SET status='searching', updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='queued'", (request_id,)).rowcount)
        return self._run(work)

    def finish(self, request_id, status, message, audio_path):
        """Record a request's outcome: status, message, and audio path."""
        def work(conn):
            conn.execute("UPDATE requests SET status=?, message=?, audio_path=?, "
                         "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (status, message, audio_path, request_id))
        self._run(work)

    def set_audio_path(self, request_id, audio_path):
        """Rewrite one request's audio path, nothing else.

        Plan 062: the library migration moves a downloaded
        request's home from a file to the media database; finish()
        would rewrite its status and message too, which are not
        the migration's to touch.
        """
        def work(conn):
            conn.execute("UPDATE requests SET audio_path=?, "
                         "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (audio_path, request_id))
        self._run(work)

    def retry(self, request_id, search_title):
        """Requeue a failed request under a new search title."""
        def work(conn):
            self._lock_request(conn, request_id)
            return self._queue_retry(conn, request_id, search_title)
        return self._run(work)

    @staticmethod
    def _queue_retry(conn, request_id, search_title):
        return bool(conn.execute(
            "UPDATE requests SET search_title=?, status='queued', message='', "
            "updated_at=CURRENT_TIMESTAMP, format_revision=format_revision+1 "
            "WHERE id=? AND status IN ('not_found','error','rejected')",
            (search_title, request_id)).rowcount)

    def reserved_tracks(self, first, last):
        """Catalog indices whose downloads are governed by an individual format choice."""
        def work(conn):
            return {row[0] for row in conn.execute(
                "SELECT DISTINCT track_index FROM requests WHERE track_index BETWEEN ? AND ? "
                "AND (status='waiting_format' OR format_confirmed=1)", (first, last))}
        return self._run(work)

    def reset_searching(self):
        """Requeue requests left in searching (the bulk run's restart)."""
        def work(conn):
            conn.execute("UPDATE requests SET status='queued', updated_at=CURRENT_TIMESTAMP "
                         "WHERE status='searching'")
        self._run(work)

    def reset_interrupted(self):
        """Requeue searching and space-waiting requests (requests restart)."""
        def work(conn):
            conn.execute("UPDATE requests SET status='queued', updated_at=CURRENT_TIMESTAMP "
                         "WHERE status IN ('searching', 'waiting_space')")
        self._run(work)

    # -- request lookups ---------------------------------------------------

    def has_queued(self):
        """Whether any request is waiting to be claimed."""
        def work(conn):
            return conn.execute("SELECT 1 FROM requests WHERE status='queued' LIMIT 1"
                                ).fetchone() is not None
        return self._run(work)

    def active_count(self):
        """How many requests are queued or searching."""
        def work(conn):
            return conn.execute("SELECT COUNT(*) FROM requests WHERE status IN "
                                "('queued','searching')").fetchone()[0]
        return self._run(work)

    def recent(self, limit=80):
        """The most recent requests, newest first, as dicts."""
        def work(conn):
            return [dict(row) for row in conn.execute(
                "SELECT * FROM requests WHERE status='waiting_format' OR id IN "
                "(SELECT id FROM requests ORDER BY id DESC LIMIT ?) ORDER BY id DESC", (limit,))]
        return self._run(work)

    def by_track(self):
        """The requests-by-track map the catalog read path needs.

        Every request that names a library track, keyed by track index;
        when a track has several, the newest row wins.
        """
        def work(conn):
            return {row["track_index"]: dict(row) for row in conn.execute(
                "SELECT * FROM requests WHERE track_index IS NOT NULL ORDER BY id")}
        return self._run(work)

    @staticmethod
    def _active(conn, index, artist="", title="", musicbrainz_id=""):
        statuses = ACTIVE_STATUSES
        if index is None:
            condition, params = RequestsStore._pick_condition(artist, title, musicbrainz_id)
            statuses = (*statuses, "downloaded")
        else:
            condition = "track_index=?"
            params = (index,)
        placeholders = ",".join("?" for _ in statuses)
        return conn.execute(
            f"SELECT * FROM requests WHERE {condition} AND status IN ({placeholders}) "
            "ORDER BY id DESC LIMIT 1", (*params, *statuses)).fetchone()

    def active_for_track(self, track_index):
        row = self._run(lambda conn: self._active(conn, track_index))
        return dict(row) if row else None

    def has_active_for_track(self, track_index):
        return self.active_for_track(track_index) is not None

    def active_custom(self, artist, title):
        row = self._run(lambda conn: self._active(conn, None, artist, title))
        return dict(row) if row else None

    @staticmethod
    def _pick_condition(artist, title, musicbrainz_id=""):
        if musicbrainz_id:
            return "track_index IS NULL AND musicbrainz_id=?", (musicbrainz_id,)
        return "track_index IS NULL AND musicbrainz_id='' AND artist=? AND title=?", (artist, title)

    def for_picks(self, picks):
        """Latest custom request per weekly pick, independent of the recent feed."""
        if not picks:
            return {}
        def work(conn):
            rows = {}
            for pick in picks:
                condition, params = self._pick_condition(*pick)
                row = conn.execute(
                    "SELECT id, artist, title, status, message, requested_format, format_revision, "
                    f"audio_path, musicbrainz_id, version, performance, length, album, track_index FROM requests WHERE {condition} "
                    "ORDER BY id DESC LIMIT 1", params).fetchone()
                if row:
                    result = dict(row)
                    path = result.pop("audio_path")
                    if retry := mp3_retry(row):
                        result["mp3_retry"] = retry
                    if result["status"] == "downloaded" and path:
                        result["audio"] = f"/api/audio/custom/{row['id']}"
                    rows[pick] = result
            return rows
        return self._run(work)

    def replica_customs(self, after, limit):
        return self._run(lambda db: [dict(row) for row in db.execute(
            "SELECT id,artist,title,length,audio_path FROM requests WHERE id>? AND track_index IS NULL "
            "AND status='downloaded' AND audio_path!='' ORDER BY id LIMIT ?", (after, limit))])

    def downloaded_custom(self, request_id):
        """A downloaded custom request by id, as a dict, or None."""
        def work(conn):
            row = conn.execute(
                "SELECT * FROM requests WHERE id=? AND status='downloaded' "
                "AND track_index IS NULL", (request_id,)).fetchone()
            return dict(row) if row else None
        return self._run(work)

    def custom_audio(self, request_id):
        """A custom request's status and audio path, as a dict, or None."""
        def work(conn):
            row = conn.execute(
                "SELECT status,audio_path FROM requests WHERE id=? AND track_index IS NULL",
                (request_id,)).fetchone()
            return dict(row) if row else None
        return self._run(work)

    def downloaded_audio(self):
        """Every downloaded request's id, track index, and audio path.

        Plan 060: report() reads this to keep blob-backed downloads
        verified (a database-mode landing leaves no file to find)
        and to know which store keys stay referenced.
        """
        def work(conn):
            return [dict(row) for row in conn.execute(
                "SELECT id, track_index, audio_path, artist, title, length, musicbrainz_id, version, performance, requested_format, format_confirmed FROM requests "
                "WHERE status='downloaded' AND audio_path != '' ORDER BY id")]
        return self._run(work)

    # -- external metadata cache -------------------------------------------

    def cache_get(self, key):
        """A cached response as {"body", "saved_at"}, or None.

        Expiry is the caller's decision: `saved_at` comes back with the
        body so each caller can apply its own maximum age.
        """
        packed = _PACKED + key

        def work(conn):
            row = conn.execute("SELECT key,body,saved_at FROM external_cache WHERE key IN (?,?) "
                               "ORDER BY saved_at DESC, key=? DESC LIMIT 1", (key, packed, packed)).fetchone()
            if row is None:
                return None
            body, stamp = row["body"], row["saved_at"]
            if row["key"] == packed:
                try:
                    body = zlib.decompress(body).decode("utf-8")
                except (zlib.error, UnicodeError, TypeError):
                    return None
            else:
                # Convert a legacy hit without renewing its TTL or replacing a concurrent refresh.
                conn.execute("UPDATE OR REPLACE external_cache SET key=?, body=? "
                             "WHERE key=? AND saved_at=? AND body=? AND NOT EXISTS "
                             "(SELECT 1 FROM external_cache WHERE key=? AND saved_at>?)",
                             (packed, zlib.compress(body.encode("utf-8")), key, stamp, body, packed, stamp))
            return {"body": body, "saved_at": stamp}
        return self._run(work)

    def cache_put(self, key, body, saved_at):
        """Store compressed UTF-8 and evict expired or excess entries, oldest first."""
        payload = zlib.compress(body.encode("utf-8"))

        def work(conn):
            conn.execute("DELETE FROM external_cache WHERE key=? OR saved_at < ?",
                         (key, saved_at - CACHE_PRUNE_AGE))
            conn.execute("INSERT OR REPLACE INTO external_cache (key,body,saved_at) "
                         "VALUES (?,?,?)", (_PACKED + key, payload, saved_at))
            if key.startswith("catalog-search:"):
                conn.execute("DELETE FROM external_cache WHERE rowid IN "
                             "(SELECT rowid FROM external_cache WHERE key GLOB 'zlib:v1:catalog-search:*' "
                             "ORDER BY saved_at DESC, rowid DESC LIMIT -1 OFFSET ?)", (SEARCH_MAX_ENTRIES,))
        self._run(work)
