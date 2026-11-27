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

import hashlib
import json
import secrets
import sqlite3
import time
import zlib
from pathlib import Path

from system import state_database
from system.formats import REQUEST_SUFFIXES
from system.normalize import song_key
from .request_state import (COPY_FAILURES, COPY_REVIEW_MESSAGE, COPY_REVIEW_STATES, EDITABLE_FORMAT,
                            FAILED_SEARCHES, REPLACEABLE_RECORDING, request_state)


def mp3_retry(row):
    """The request and revision that can be retried after a lossless miss."""
    if (row.get("request_state", request_state(row)) == "no_matches"
            and row["requested_format"] in ("any_lossless", "flac", "alac")):
        return {"id": row["id"], "format_revision": row["format_revision"]}
    return None


def decode_request_row(row):
    """Read the request format while keeping its stored value compatible with older rooms."""
    result = dict(row)
    if "status" in result:
        result["request_state"] = request_state(result)
    result.pop("selected_copy_key", None)
    if (result.get("status") != "searching" or result.get("phase") not in ("downloading", "testing")
            or result.get("download_percent") is None):
        result.pop("download_percent", None)
    # An older room reads the narrower fallback. Revisions bind expanded
    # choices to the confirmation that set them, even after an older room edits it.
    aac_revision = result.pop("aac_revision", None)
    if aac_revision is not None and result["requested_format"] == "mp3" and aac_revision == result["format_revision"]:
        result["requested_format"] = "any_lossy"
    broad_revision = result.pop("broad_revision", None)
    if result["requested_format"] == "any_lossless" and broad_revision == result["format_revision"]:
        result["requested_format"] = "any_format"
    if result.get("status") == "rejected" and result.get("message") == COPY_REVIEW_MESSAGE:
        result["copy_review"] = True
    return result


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
        original_recording_id TEXT NOT NULL DEFAULT '',
        original_track_index INTEGER,
        original_artist TEXT,
        original_title TEXT,
        version TEXT NOT NULL DEFAULT '',
        performance TEXT NOT NULL DEFAULT '',
        requested_format TEXT DEFAULT 'any_lossless',
        format_revision INTEGER NOT NULL DEFAULT 0,
        aac_revision INTEGER,
        broad_revision INTEGER,
        format_confirmed INTEGER NOT NULL DEFAULT 0,
        selected_copy_key TEXT,
        status TEXT NOT NULL DEFAULT 'queued',
        phase TEXT NOT NULL DEFAULT '',
        download_percent INTEGER,
        retry_count INTEGER NOT NULL DEFAULT 0,
        retry_at REAL NOT NULL DEFAULT 0,
        retry_priority REAL NOT NULL DEFAULT 0,
        message TEXT NOT NULL DEFAULT '',
        audio_path TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        resolved_at TEXT
    )""",
    "CREATE TABLE IF NOT EXISTS external_cache (key TEXT PRIMARY KEY, body TEXT NOT NULL, saved_at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS external_cache_age ON external_cache (saved_at)",
    # Plan 045: the queue's hot lookups (by_track, the status scans
    # the activity path polls) stop scanning the whole table.
    "CREATE INDEX IF NOT EXISTS requests_track_index ON requests (track_index)",
    "CREATE INDEX IF NOT EXISTS requests_status ON requests (status)",
    "CREATE INDEX IF NOT EXISTS requests_pick ON requests (artist, title, id DESC) WHERE track_index IS NULL",
    """CREATE TABLE IF NOT EXISTS request_copies (
        request_id INTEGER PRIMARY KEY,
        revision INTEGER NOT NULL,
        format TEXT NOT NULL,
        expires REAL NOT NULL,
        payload TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS request_copy_failures (
        request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
        fingerprint TEXT NOT NULL,
        reason TEXT NOT NULL,
        attempted_at REAL NOT NULL,
        PRIMARY KEY (request_id, fingerprint)
    )""",
)

RETRY_DELAYS = (30, 120, 300)
QUEUE_COLUMNS = {
    "phase": "TEXT NOT NULL DEFAULT ''",
    "download_percent": "INTEGER",
    "retry_count": "INTEGER NOT NULL DEFAULT 0",
    "retry_at": "REAL NOT NULL DEFAULT 0",
    "retry_priority": "REAL NOT NULL DEFAULT 0",
}
# A request the user can still act on or that is already on its way.
ACTIVE_STATUSES = ("waiting_format", "queued", "searching", "waiting_space")


def weekly_request_origin(row, songs_by_recording):
    identity = row["original_recording_id"]
    if identity and identity != row["musicbrainz_id"] and identity in songs_by_recording:
        return songs_by_recording[identity]
    if row["original_artist"] and row["original_title"]:
        return song_key(row["original_artist"], row["original_title"])
    return None


def weekly_request_songs(row, songs_by_recording):
    keys = {song_key(row["artist"], row["title"])}
    if row["musicbrainz_id"] in songs_by_recording:
        keys.add(songs_by_recording[row["musicbrainz_id"]])
    if origin := weekly_request_origin(row, songs_by_recording):
        keys.add(origin)
    return keys


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
            required = {"format_confirmed", "version", "musicbrainz_id", "original_recording_id",
                        "original_track_index", "original_artist", "original_title", "performance", "aac_revision",
                        "broad_revision", "resolved_at", "selected_copy_key", *QUEUE_COLUMNS}
            if not required <= {col[1] for col in conn.execute("PRAGMA table_info(requests)")}:
                # Another server or runner may be upgrading the same room.
                conn.execute("BEGIN IMMEDIATE")
                columns = {col[1] for col in conn.execute("PRAGMA table_info(requests)")}
                if "requested_format" not in columns:
                    conn.execute("ALTER TABLE requests ADD COLUMN requested_format TEXT DEFAULT 'any_lossless'")
                if "format_revision" not in columns:
                    conn.execute("ALTER TABLE requests ADD COLUMN format_revision INTEGER NOT NULL DEFAULT 0")
                if "format_confirmed" not in columns:
                    conn.execute("ALTER TABLE requests ADD COLUMN format_confirmed INTEGER NOT NULL DEFAULT 0")
                if "aac_revision" not in columns:
                    conn.execute("ALTER TABLE requests ADD COLUMN aac_revision INTEGER")
                if "broad_revision" not in columns:
                    conn.execute("ALTER TABLE requests ADD COLUMN broad_revision INTEGER")
                if "selected_copy_key" not in columns:
                    conn.execute("ALTER TABLE requests ADD COLUMN selected_copy_key TEXT")
                if "resolved_at" not in columns:
                    conn.execute("ALTER TABLE requests ADD COLUMN resolved_at TEXT")
                    conn.execute("UPDATE requests SET resolved_at=updated_at "
                                 "WHERE status IN ('downloaded','not_found','rejected','error')")
                for column in ("musicbrainz_id", "original_recording_id", "version", "performance"):
                    if column not in columns:
                        conn.execute(f"ALTER TABLE requests ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
                for column, definition in (("original_track_index", "INTEGER"), ("original_artist", "TEXT"),
                                           ("original_title", "TEXT")):
                    if column not in columns:
                        conn.execute(f"ALTER TABLE requests ADD COLUMN {column} {definition}")
                for column, definition in QUEUE_COLUMNS.items():
                    if column not in columns:
                        conn.execute(f"ALTER TABLE requests ADD COLUMN {column} {definition}")
                conn.commit()
            conn.execute("CREATE INDEX IF NOT EXISTS requests_recording ON requests (musicbrainz_id, id DESC) "
                         "WHERE track_index IS NULL")
            conn.execute("CREATE INDEX IF NOT EXISTS requests_original_recording "
                         "ON requests (original_recording_id, id DESC) WHERE track_index IS NULL")
            conn.execute("CREATE INDEX IF NOT EXISTS requests_original_track ON requests (original_track_index)")
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
                return decode_request_row(row), False
            previous = self._latest(conn, index, artist, title, musicbrainz_id)
            if previous:
                if individual or previous["status"] == "unavailable":
                    return decode_request_row(previous), False
                # A manual batch retry honors a previously confirmed individual format.
                if previous["format_confirmed"] and previous["status"] in FAILED_SEARCHES:
                    self._queue_retry(conn, previous["id"], previous["search_title"])
                    row = conn.execute("SELECT * FROM requests WHERE id=?", (previous["id"],)).fetchone()
                    return decode_request_row(row), True
            status = "waiting_format" if individual else "queued"
            cursor = conn.execute(
                "INSERT INTO requests (track_index,artist,title,album,length,search_title,status,requested_format,musicbrainz_id,version,performance) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (index, artist, title, album, length, search_title,
                                             status, None if individual else "any_lossless", musicbrainz_id, version, performance))
            row = conn.execute("SELECT * FROM requests WHERE id=?", (cursor.lastrowid,)).fetchone()
            return decode_request_row(row), True
        return self._run(work)

    def replace_recording(self, request_id, revision, artist, title, album, length, search_title, *,
                          musicbrainz_id, version, performance):
        """Change the recording on this request, invalidating its previous copy choices."""
        def work(conn):
            row = self._lock_request(conn, request_id)
            if row["format_revision"] == revision + 1 and row["musicbrainz_id"] == musicbrainz_id:
                return decode_request_row(row), False
            if row["format_revision"] != revision or row["status"] not in REPLACEABLE_RECORDING:
                raise ValueError("This request changed. Reopen it before choosing another recording.")
            other = self._latest(conn, None, artist, title, musicbrainz_id)
            if other and other["id"] != request_id:
                raise ValueError("This recording already has a request. Open it in Requests.")
            if row["musicbrainz_id"] == musicbrainz_id:
                return decode_request_row(row), False
            confirmed = row["format_confirmed"] and row["requested_format"] is not None
            status = "queued" if confirmed else "waiting_format"
            conn.execute("DELETE FROM request_copies WHERE request_id=?", (request_id,))
            conn.execute("DELETE FROM request_copy_failures WHERE request_id=?", (request_id,))
            conn.execute(
                "UPDATE requests SET track_index=NULL, artist=?, title=?, album=?, length=?, search_title=?, "
                "original_recording_id=COALESCE(NULLIF(original_recording_id,''),musicbrainz_id), "
                "original_track_index=COALESCE(original_track_index,track_index), "
                "original_artist=COALESCE(original_artist,CASE WHEN track_index IS NULL AND musicbrainz_id='' THEN artist END), "
                "original_title=COALESCE(original_title,CASE WHEN track_index IS NULL AND musicbrainz_id='' THEN title END), "
                "musicbrainz_id=?, version=?, performance=?, status=?, requested_format=?, phase='', download_percent=NULL, "
                "selected_copy_key=NULL, resolved_at=NULL, audio_path='', message='', retry_count=0, "
                "retry_at=0, retry_priority=0, updated_at=CURRENT_TIMESTAMP, "
                "aac_revision=CASE WHEN aac_revision=format_revision THEN format_revision+1 END, "
                "broad_revision=CASE WHEN broad_revision=format_revision THEN format_revision+1 END, "
                "format_revision=format_revision+1 WHERE id=?",
                (artist, title, album, length, search_title, musicbrainz_id, version, performance, status,
                 row["requested_format"] if confirmed else None, request_id))
            updated = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            return decode_request_row(updated), status == "queued"
        return self._run(work)

    def queue_weekly(self, tracks, *, selected_ids=None, audio_available=None):
        """Queue a confirmed MP3/AAC choice, respecting existing manual requests."""
        songs_by_recording = {track["id"]: song_key(track["artist"], track["title"]) for track in tracks}
        selected = [track for track in tracks if selected_ids is None or track["id"] in selected_ids]
        if not selected:
            return 0

        # The restored collection can store audio and requests in the same
        # SQLite file. Check audio before taking the request write lock and
        # only apply that observation to the same row and path.
        missing = set()
        if audio_available is not None:
            selected_songs = {songs_by_recording[track["id"]] for track in selected}
            missing = {(row["id"], row["audio_path"]) for row in self.weekly_requests()
                       if row["status"] == "downloaded" and weekly_request_songs(row, songs_by_recording) & selected_songs
                       and not audio_available(row["audio_path"])}

        def work(conn):
            conn.execute("BEGIN IMMEDIATE")
            queued = 0
            reserved = set()
            for row in self._weekly_requests(conn):
                if row["status"] != "downloaded" or (row["id"], row["audio_path"]) not in missing:
                    reserved.update(weekly_request_songs(row, songs_by_recording))
            for track in selected:
                artist, title, recording_id = track["artist"], track["title"], track["id"]
                song = songs_by_recording[recording_id]
                if (song in reserved or self._active(conn, None, artist, title, recording_id)
                        or self._active(conn, None, artist, title)):
                    continue
                previous = self._latest(conn, None, artist, title, recording_id, include_completed_origin=True)
                if previous:
                    retried_songs = weekly_request_songs(previous, songs_by_recording)
                    if retried_songs & reserved:
                        continue
                    released = self._queue_retry(conn, previous["id"], previous["search_title"])
                    if released and not previous["format_confirmed"]:
                        conn.execute("UPDATE requests SET requested_format='mp3', format_confirmed=1, "
                                     "aac_revision=format_revision, broad_revision=NULL WHERE id=?", (previous["id"],))
                    queued += released
                    if released:
                        reserved.update(retried_songs)
                    continue
                search = " ".join(filter(None, (title, track["version"])))[:200]
                conn.execute(
                    "INSERT INTO requests (artist,title,album,length,search_title,musicbrainz_id,version,"
                    "requested_format,format_revision,aac_revision,format_confirmed) "
                    "VALUES (?,?,?,?,?,?,?,'mp3',1,1,1)",
                    (artist, title, track["album"], track["seconds"], search, recording_id, track["version"]))
                reserved.add(song)
                queued += 1
            return queued
        return self._run(work)

    def _lock_request(self, conn, request_id):
        """Serialize a release/retry with new intake for the same recording."""
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown song request")
        if row["status"] in (*FAILED_SEARCHES, "unavailable"):
            other = self._active(conn, row["track_index"], row["artist"], row["title"], row["musicbrainz_id"])
            if other and other["id"] != request_id:
                raise ValueError("Another request already exists for this song. Open it in Requests.")
        return row

    @staticmethod
    def _copy_fingerprint(candidate):
        return hashlib.sha256(json.dumps([candidate["source"], candidate["id"]]).encode()).hexdigest()

    def _display_copy_matches(self, conn, request_id, payload):
        failures = dict(conn.execute("SELECT fingerprint, reason FROM request_copy_failures WHERE request_id=?",
                                     (request_id,)))
        matches = []
        for match in payload["matches"]:
            candidate = payload["picks"][match["key"]]
            reason = COPY_FAILURES.get(failures.get(self._copy_fingerprint(candidate)))
            matches.append({**match, "failure": reason} if reason else match)
        return sorted(matches, key=lambda match: "failure" in match)

    def save_copy_matches(self, request_id, choice, revision, recording, selection, candidates, *, for_review=False,
                          fresh=False):
        """Return opaque handles with display metadata. Keep transfer IDs server-side."""
        picks, matches = {}, []
        for number, candidate in enumerate(candidates, 1):
            handle = secrets.token_hex(16)
            picks[handle] = {key: candidate[key] for key in ("source", "id")}
            label = candidate.get("label", "").replace("\\", "/").rsplit("/", 1)[-1]
            matches.append({"key": handle, "label": label or f"Copy {number}",
                            **{key: candidate[key] for key in ("format", "duration", "bitrate", "recommended")
                               if key in candidate}})

        def work(conn):
            row = self._lock_request(conn, request_id)
            allowed = ("searching",) if for_review else EDITABLE_FORMAT
            if row["format_revision"] != revision or row["status"] not in allowed:
                raise ValueError("This request changed. Reopen it to browse copies.")
            now = time.time()
            conn.execute("DELETE FROM request_copies WHERE expires > 0 AND expires < ?", (now,))
            if not (for_review or fresh):
                existing = conn.execute("SELECT payload FROM request_copies WHERE request_id=? AND revision=? "
                                        "AND format=? AND expires>?", (request_id, revision, choice, now)).fetchone()
                saved = json.loads(existing["payload"]) if existing else {}
                if saved.get("recording") == recording and saved.get("selection") == selection and "matches" in saved:
                    return self._display_copy_matches(conn, request_id, saved)
                if not matches:
                    return []
            payload = {"recording": recording, "selection": selection, "picks": picks, "matches": matches}
            lifetime = 86400 if for_review else 900
            conn.execute("INSERT OR REPLACE INTO request_copies VALUES (?, ?, ?, ?, ?)",
                         (request_id, revision, choice, now + lifetime, json.dumps(payload)))
            return self._display_copy_matches(conn, request_id, payload)
        return self._run(work)

    def review_matches(self, request_id, choice, revision, recording, selection):
        def work(conn):
            row = conn.execute("SELECT payload FROM request_copies WHERE request_id=? AND format=? "
                               "AND revision=? AND expires>?", (request_id, choice, revision, time.time())).fetchone()
            payload = json.loads(row["payload"]) if row else {}
            if payload.get("recording") == recording and payload.get("selection") == selection and "matches" in payload:
                return self._display_copy_matches(conn, request_id, payload)
            return None
        return self._run(work)

    def selected_copy(self, request_id, revision):
        def work(conn):
            row = conn.execute("SELECT payload FROM request_copies WHERE request_id=? AND revision=? AND expires=0",
                               (request_id, revision)).fetchone()
            return json.loads(row["payload"]) if row else None
        return self._run(work)

    def remember_selected_audio(self, request_id, revision, reference):
        """Bind a verified handoff to the exact selection before removing its staged bytes."""
        def work(conn):
            conn.execute("BEGIN IMMEDIATE")
            saved = conn.execute("SELECT payload FROM request_copies WHERE request_id=? AND revision=? AND expires=0",
                                 (request_id, revision)).fetchone()
            if saved is None:
                raise RuntimeError("The selected copy changed before its verified audio was saved.")
            payload = json.loads(saved["payload"])
            payload["audio_path"] = reference
            conn.execute("UPDATE request_copies SET payload=? WHERE request_id=?", (json.dumps(payload), request_id))
        self._run(work)

    def confirm_format(self, request_id, choice, revision, copy_key=None):
        """Each picker version can release at most one search, even after it finishes."""
        if not isinstance(choice, str) or choice not in REQUEST_SUFFIXES:
            raise ValueError("Choose a supported audio format.")
        if type(revision) is not int or not 0 <= revision < 2**63 - 1:
            raise ValueError("Invalid format revision")
        if copy_key is not None and (not isinstance(copy_key, str) or len(copy_key) != 32):
            raise ValueError("Choose a copy from the current results.")
        def work(conn):
            locked = self._lock_request(conn, request_id)
            row = decode_request_row(locked)
            if row["requested_format"] == choice and row["format_revision"] == revision + 1:
                if copy_key != locked["selected_copy_key"]:
                    raise ValueError("This request already started. Reopen it to choose another copy.")
                return row, False
            if copy_key is not None:
                saved = conn.execute("SELECT payload, expires FROM request_copies WHERE request_id=? AND revision=? "
                                     "AND format=? AND expires>?", (request_id, revision, choice, time.time())).fetchone()
                payload = json.loads(saved["payload"]) if saved else {}
                candidate = payload.get("picks", {}).get(copy_key)
                if not candidate:
                    raise ValueError("These results changed or expired. Browse copies again.")
                payload["listed_until"] = saved["expires"]
                payload["candidate"] = candidate
                conn.execute("UPDATE request_copies SET revision=?, expires=0, payload=? WHERE request_id=?",
                             (revision + 1, json.dumps(payload), request_id))
            else:
                conn.execute("DELETE FROM request_copies WHERE request_id=?", (request_id,))
            stored_format = {"any_lossy": "mp3", "any_format": "any_lossless"}.get(choice, choice)
            changed = conn.execute(
                "UPDATE requests SET requested_format=?, aac_revision=?, broad_revision=?, "
                "selected_copy_key=?, format_confirmed=1, status='queued', message='', "
                "phase='', retry_count=0, retry_at=0, retry_priority=0, resolved_at=NULL, "
                "format_revision=format_revision+1, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND format_revision=? AND status IN ('waiting_format','not_found','error','rejected')",
                (stored_format, revision + 1 if choice == "any_lossy" else None,
                 revision + 1 if choice == "any_format" else None, copy_key, request_id, revision)).rowcount
            if not changed:
                raise ValueError("This request changed. Reopen it to choose a format.")
            row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            return decode_request_row(row), True
        return self._run(work)

    def stop_search(self, request_id, revision):
        """Dismiss a copy review and invalidate any copies still being browsed."""
        def work(conn):
            row = self._lock_request(conn, request_id)
            if row["status"] == "unavailable" and row["format_revision"] == revision + 1:
                return decode_request_row(row)
            if row["format_revision"] != revision or request_state(dict(row)) not in COPY_REVIEW_STATES:
                raise ValueError("This request changed. Reopen it before stopping the search.")
            conn.execute(
                "UPDATE requests SET status='unavailable', message='Search stopped.', "
                "selected_copy_key=NULL, phase='', download_percent=NULL, "
                "retry_count=0, retry_at=0, retry_priority=0, "
                "resolved_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP, "
                "aac_revision=CASE WHEN aac_revision=format_revision THEN format_revision+1 END, "
                "broad_revision=CASE WHEN broad_revision=format_revision THEN format_revision+1 END, "
                "format_revision=format_revision+1 WHERE id=?", (request_id,))
            conn.execute("DELETE FROM request_copies WHERE request_id=?", (request_id,))
            return decode_request_row(conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())
        return self._run(work)

    def get(self, request_id):
        """One request row by id, as a dict, or None."""
        def work(conn):
            row = conn.execute("SELECT * FROM requests WHERE id=?",
                               (request_id,)).fetchone()
            return decode_request_row(row) if row else None
        return self._run(work)

    def queued(self, limit):
        """Due requests, with explicit retries first and deferred work behind new requests."""
        def work(conn):
            return [decode_request_row(row) for row in conn.execute(
                "SELECT * FROM requests WHERE status='queued' AND retry_at<=? "
                "ORDER BY retry_priority DESC, retry_at, id LIMIT ?",
                (time.time(), limit)).fetchall()]
        return self._run(work)

    def claim(self, request_id):
        """Move a queued request to searching (the runner picking it up)."""
        def work(conn):
            return bool(conn.execute(
                "UPDATE requests SET status='searching', phase='', download_percent=NULL, message='', retry_at=0, retry_priority=0, "
                "resolved_at=NULL, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='queued' AND retry_at<=?", (request_id, time.time())).rowcount)
        return self._run(work)

    def set_phase(self, request_id, phase, download_percent=None):
        self._run(lambda conn: conn.execute(
            "UPDATE requests SET phase=?, download_percent=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status='searching' AND (phase!=? OR download_percent IS NOT ?)",
            (phase, download_percent, request_id, phase, download_percent)))

    def defer_attempt(self, row, message, *, provider_delay=0, copy_failure=None):
        """Release the worker while preserving a selected copy and its format."""
        attempts = row["retry_count"]
        if not provider_delay and attempts >= len(RETRY_DELAYS):
            self.finish(row["id"], "error", message, "", copy_failure=copy_failure)
            return "error"
        delay = provider_delay or RETRY_DELAYS[attempts]
        self._run(lambda conn: conn.execute(
            "UPDATE requests SET status='queued', phase='', download_percent=NULL, message=?, retry_at=?, "
            "retry_count=retry_count+?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='searching'",
            (message, time.time() + delay, int(not provider_delay), row["id"])))
        return "queued"

    def finish(self, request_id, status, message, audio_path, *, copy_failure=None):
        """Record a request's outcome: status, message, and audio path."""
        def work(conn):
            conn.execute("UPDATE requests SET status=?, message=?, audio_path=?, "
                         "phase='', download_percent=NULL, retry_count=0, retry_at=0, retry_priority=0, "
                         "updated_at=CURRENT_TIMESTAMP, resolved_at=CURRENT_TIMESTAMP WHERE id=?",
                         (status, message, audio_path, request_id))
            if status not in ("queued", "waiting_space"):
                if status in FAILED_SEARCHES:
                    saved = conn.execute("SELECT payload FROM request_copies WHERE request_id=? AND expires=0",
                                         (request_id,)).fetchone()
                    if saved:
                        payload = json.loads(saved["payload"])
                        candidate = payload.pop("candidate")
                        if copy_failure in COPY_FAILURES:
                            fingerprint = self._copy_fingerprint(candidate)
                            conn.execute("INSERT OR REPLACE INTO request_copy_failures VALUES (?,?,?,?)",
                                         (request_id, fingerprint, copy_failure, time.time()))
                            conn.execute("DELETE FROM request_copy_failures WHERE request_id=? AND fingerprint NOT IN "
                                         "(SELECT fingerprint FROM request_copy_failures WHERE request_id=? "
                                         "ORDER BY attempted_at DESC LIMIT 40)", (request_id, request_id))
                        lifetime = payload.pop("listed_until", 0)
                        if lifetime > time.time() and "matches" in payload and "picks" in payload:
                            payload.pop("audio_path", None)
                            conn.execute("UPDATE request_copies SET expires=?, payload=? WHERE request_id=?",
                                         (lifetime, json.dumps(payload), request_id))
                conn.execute("DELETE FROM request_copies WHERE request_id=? AND expires=0", (request_id,))
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

    def retry(self, request_id, search_title=None, *, revision=None):
        """Prioritize waiting work, or restart a failed search."""
        def work(conn):
            row = self._lock_request(conn, request_id)
            if revision is not None and row["format_revision"] != revision:
                raise ValueError("This request changed. Refresh it before retrying.")
            if row["status"] == "queued" and search_title is None:
                conn.execute("UPDATE requests SET retry_at=0, retry_count=0, retry_priority=?, "
                             "message='', updated_at=CURRENT_TIMESTAMP WHERE id=?", (time.time(), request_id))
                return True
            if request_state(dict(row)) in COPY_REVIEW_STATES:
                return False
            search = search_title if search_title is not None else row["search_title"]
            if self._queue_retry(conn, request_id, search, allow_unavailable=True):
                conn.execute("UPDATE requests SET retry_priority=? WHERE id=?", (time.time(), request_id))
                return True
            return False
        return self._run(work)

    @staticmethod
    def _queue_retry(conn, request_id, search_title, *, missing=False, allow_unavailable=False):
        return bool(conn.execute(
            "UPDATE requests SET search_title=?, status='queued', message='', selected_copy_key=NULL, "
            "aac_revision=CASE WHEN aac_revision=format_revision THEN format_revision+1 END, "
            "broad_revision=CASE WHEN broad_revision=format_revision THEN format_revision+1 END, "
            "phase='', retry_count=0, retry_at=0, retry_priority=0, resolved_at=NULL, "
            "audio_path=CASE WHEN ? THEN '' ELSE audio_path END, "
            "updated_at=CURRENT_TIMESTAMP, format_revision=format_revision+1 "
            "WHERE id=? AND (status IN ('not_found','error','rejected') OR (? AND status='downloaded') "
            "OR (? AND status='unavailable'))",
            (search_title, missing, request_id, missing, allow_unavailable)).rowcount)

    def retry_missing(self, request_id, revision, audio_path):
        """Requeue the still-current request whose playable file the caller found missing."""
        def work(conn):
            row = self._lock_request(conn, request_id)
            if row["status"] != "downloaded" or row["format_revision"] != revision or row["audio_path"] != audio_path:
                return False
            if self._active(conn, row["track_index"], row["artist"], row["title"], row["musicbrainz_id"], excluding=request_id):
                raise ValueError("Another request already exists for this song. Open it in Requests.")
            return self._queue_retry(conn, request_id, row["search_title"], missing=True)
        return self._run(work)

    def reserved_tracks(self, first, last):
        """Catalog indices whose downloads are governed by an individual format choice."""
        def work(conn):
            return {row[0] for row in conn.execute(
                "SELECT track_index FROM requests WHERE track_index BETWEEN ? AND ? "
                "AND (status IN ('waiting_format','unavailable') OR format_confirmed=1) UNION "
                "SELECT original_track_index FROM requests WHERE original_track_index BETWEEN ? AND ? "
                "AND (status IN ('waiting_format','unavailable') OR format_confirmed=1)", (first, last, first, last))}
        return self._run(work)

    def reset_searching(self):
        """Requeue requests left in searching (the bulk run's restart)."""
        def work(conn):
            conn.execute("UPDATE requests SET status='queued', phase='', updated_at=CURRENT_TIMESTAMP "
                         "WHERE status='searching'")
        self._run(work)

    def reset_interrupted(self):
        """Requeue searching and space-waiting requests (requests restart)."""
        def work(conn):
            conn.execute("UPDATE requests SET status='queued', phase='', updated_at=CURRENT_TIMESTAMP "
                         "WHERE status IN ('searching', 'waiting_space')")
        self._run(work)

    # -- request lookups ---------------------------------------------------

    def has_queued(self):
        """Whether any request is due for an attempt."""
        def work(conn):
            return conn.execute("SELECT 1 FROM requests WHERE status='queued' AND retry_at<=? LIMIT 1",
                                (time.time(),)).fetchone() is not None
        return self._run(work)

    def active_count(self):
        """How many requests are queued or searching."""
        def work(conn):
            return conn.execute("SELECT COUNT(*) FROM requests WHERE status IN "
                                "('queued','searching')").fetchone()[0]
        return self._run(work)

    def recent(self, limit=80):
        """Recent requests and older ones that still need a choice or an explicit retry."""
        def work(conn):
            return [decode_request_row(row) for row in conn.execute(
                "SELECT * FROM requests WHERE status IN ('waiting_format','unavailable') OR id IN "
                "(SELECT id FROM requests ORDER BY id DESC LIMIT ?) ORDER BY id DESC", (limit,))]
        return self._run(work)

    def download_activity(self):
        """Recent resolved attempts and recent downloads, in outcome order."""
        def work(conn):
            def read(where, limit):
                return [decode_request_row(row) for row in conn.execute(
                    f"SELECT * FROM requests WHERE {where} "
                    "ORDER BY coalesce(resolved_at, updated_at) DESC, id DESC LIMIT ?", (limit,))]
            return (read("status IN ('downloaded','not_found','rejected','error')", 48),
                    read("status='downloaded' AND message!='Already in the library'", 32))
        return self._run(work)

    def by_track(self):
        """The requests-by-track map the catalog read path needs.

        When a track has several requests, the newest row wins. A
        stopped replacement also holds its original track out of search.
        """
        def work(conn):
            return {row["track_index"] or row["original_track_index"]: decode_request_row(row)
                    for row in conn.execute("SELECT * FROM requests WHERE track_index IS NOT NULL "
                                            "OR (status='unavailable' AND original_track_index IS NOT NULL) "
                                            "ORDER BY id")}
        return self._run(work)

    @staticmethod
    def _latest(conn, index, artist, title, musicbrainz_id, *, include_completed_origin=False):
        if index is not None:
            condition, params = "(track_index=? OR (original_track_index=? AND status!='downloaded'))", (index, index)
        else:
            condition, params = RequestsStore._pick_condition(
                artist, title, musicbrainz_id, include_completed_origin=include_completed_origin)
        return conn.execute(f"SELECT * FROM requests WHERE {condition} ORDER BY id DESC LIMIT 1", params).fetchone()

    @staticmethod
    def _active_filter(statuses):
        placeholders = ",".join("?" for _ in statuses)
        return (f"(status IN ({placeholders}) OR (status='rejected' AND message=?) "
                "OR (status IN ('not_found','error','rejected') AND selected_copy_key IS NOT NULL))",
                (*statuses, COPY_REVIEW_MESSAGE))

    @staticmethod
    def _active(conn, index, artist="", title="", musicbrainz_id="", *, excluding=None):
        statuses = ACTIVE_STATUSES
        if index is None:
            condition, params = RequestsStore._pick_condition(artist, title, musicbrainz_id)
            statuses = (*statuses, "downloaded")
        else:
            condition = "(track_index=? OR original_track_index=?)"
            params = (index, index)
        active, values = RequestsStore._active_filter(statuses)
        return conn.execute(
            f"SELECT * FROM requests WHERE {condition} AND {active} AND id IS NOT ? ORDER BY id DESC LIMIT 1",
            (*params, *values, excluding)).fetchone()

    @staticmethod
    def _weekly_requests(conn):
        active, params = RequestsStore._active_filter((*ACTIVE_STATUSES, "downloaded", "unavailable"))
        return conn.execute(f"SELECT * FROM requests WHERE track_index IS NULL AND {active} ORDER BY id DESC", params)

    def weekly_requests(self):
        """Current, stopped, and downloaded requests that map to a weekly song."""
        return self._run(lambda conn: [decode_request_row(row) for row in self._weekly_requests(conn)])

    def active_for_track(self, track_index):
        row = self._run(lambda conn: self._active(conn, track_index))
        return decode_request_row(row) if row else None

    def has_active_for_track(self, track_index):
        return self.active_for_track(track_index) is not None

    def active_custom(self, artist, title):
        row = self._run(lambda conn: self._active(conn, None, artist, title))
        return decode_request_row(row) if row else None

    @staticmethod
    def _pick_condition(artist, title, musicbrainz_id="", *, include_completed_origin=False):
        origin_status = "" if include_completed_origin else " AND status!='downloaded'"
        if musicbrainz_id:
            return (f"track_index IS NULL AND (musicbrainz_id=? OR (original_recording_id=?{origin_status}))",
                    (musicbrainz_id, musicbrainz_id))
        return ("track_index IS NULL AND ((musicbrainz_id='' AND artist=? AND title=?) "
                f"OR (original_artist=? AND original_title=?{origin_status}))", (artist, title, artist, title))

    def for_picks(self, picks, *, include_audio_path=False):
        """Latest custom request per pick, independent of the recent feed."""
        if not picks:
            return {}
        def work(conn):
            rows = {}
            for pick in picks:
                condition, params = self._pick_condition(*pick, include_completed_origin=True)
                row = conn.execute(
                    "SELECT id, artist, title, status, phase, download_percent, retry_at, message, requested_format, selected_copy_key, "
                    "format_revision, aac_revision, broad_revision, "
                    f"audio_path, musicbrainz_id, original_recording_id, original_artist, original_title, "
                    f"version, performance, length, album, track_index "
                    f"FROM requests WHERE {condition} "
                    "ORDER BY id DESC LIMIT 1", params).fetchone()
                if row:
                    result = decode_request_row(row)
                    if not include_audio_path:
                        path = result.pop("audio_path")
                        if retry := mp3_retry(result):
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
            return decode_request_row(row) if row else None
        return self._run(work)

    def downloaded_customs(self, ids):
        """Playback metadata for only the requested custom downloads."""
        if not ids:
            return []

        def work(conn):
            rows = []
            for start in range(0, len(ids), 800):
                batch = ids[start:start + 800]
                placeholders = ",".join("?" for _ in batch)
                rows.extend(dict(row) for row in conn.execute(
                    "SELECT id,title,artist,album,length,audio_path,musicbrainz_id,version FROM requests "
                    "WHERE status='downloaded' AND track_index IS NULL AND audio_path!='' "
                    f"AND id IN ({placeholders}) ORDER BY id", batch))
            return rows

        return self._run(work)

    def custom_audio(self, request_id):
        """A custom request's status and audio path, as a dict, or None."""
        def work(conn):
            row = conn.execute(
                "SELECT status,audio_path FROM requests WHERE id=? AND track_index IS NULL",
                (request_id,)).fetchone()
            return dict(row) if row else None
        return self._run(work)

    def downloaded_audio(self, *, custom_only=False):
        """Every downloaded request's id, track index, and audio path.

        Plan 060: report() reads this to keep blob-backed downloads
        verified (a database-mode landing leaves no file to find)
        and to know which store keys stay referenced.
        """
        def work(conn):
            scope = "AND track_index IS NULL " if custom_only else ""
            return [decode_request_row(row) for row in conn.execute(
                "SELECT id, track_index, audio_path, artist, title, length, musicbrainz_id, version, performance, "
                "requested_format, format_confirmed, format_revision, aac_revision, broad_revision FROM requests "
                f"WHERE status='downloaded' AND audio_path != '' {scope}ORDER BY id")]
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
