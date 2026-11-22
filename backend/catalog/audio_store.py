"""Persist original audio by SHA-256 in the room's SQLite database.

New and restored rooms store catalog and audio together in collection.sqlite3.
Legacy rooms keep their media.sqlite3 until explicitly copied. Each hash owns
one payload, stored raw or losslessly compressed, and its original probe. A
playable reference uses blob:<hash>. The file landing option uses a file path.

A database track is restored into the three-track playback buffer and served
by the same range-capable HTTP path as a loose file. Evicting a staged copy
or rebuilding catalog coverage leaves the stored audio intact.

Fresh databases enable incremental auto-vacuum before enabling WAL so a
reclaim job can return freed pages without rewriting the collection.
"""

from contextlib import contextmanager
import ctypes
import errno
import hashlib
import json
import os
import zlib
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from downloads.verification import judge_claim

BLOB_PREFIX = "blob:"

SCHEMA = """
CREATE TABLE IF NOT EXISTS audio (
    hash TEXT PRIMARY KEY,
    data BLOB NOT NULL,
    size INTEGER NOT NULL,
    suffix TEXT NOT NULL,
    probe TEXT NOT NULL,
    inserted_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audio_deflate (
    hash TEXT PRIMARY KEY REFERENCES audio(hash) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS audio_owners (
    kind TEXT NOT NULL,
    identity TEXT NOT NULL,
    hash TEXT NOT NULL REFERENCES audio(hash) ON DELETE CASCADE,
    PRIMARY KEY(kind, identity)
);
"""


def is_blob_ref(value):
    """Whether a stored audio value names a blob, not a file."""
    return isinstance(value, str) and value.startswith(BLOB_PREFIX)


def blob_key(value):
    """The store key inside a blob reference ("" for a file path)."""
    return value[len(BLOB_PREFIX):] if is_blob_ref(value) else ""


def blob_ref(key):
    """The stored form of a store key, as coverage rows carry it."""
    return BLOB_PREFIX + key


def _connect(path, *, synchronous="FULL"):
    """Open the media database with its own ritual (see the module
    docstring for why the pragma order is load-bearing)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute(f"PRAGMA synchronous={synchronous}")
    connection.executescript(SCHEMA)
    connection.commit()
    return connection


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_stamp(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


@contextmanager
def encode_audio(source, directory):
    """Choose the smaller representation of a stable, seekable source."""
    start = source.tell()
    digest = hashlib.sha256()
    encoder = zlib.compressobj()
    size = 0
    with tempfile.SpooledTemporaryFile(4 << 20, dir=directory) as packed:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
            packed.write(encoder.compress(chunk))
        packed.write(encoder.flush())
        stored_size = packed.tell()
        if stored_size < size:
            packed.seek(0)
            yield digest.hexdigest(), size, packed, stored_size, True
        else:
            source.seek(start)
            yield digest.hexdigest(), size, source, size, False


def insert_audio(connection, encoded, suffix, probe_json, inserted_at, *, restore=False):
    key, size, payload, stored_size, compressed = encoded
    cursor = connection.execute(
        "INSERT OR IGNORE INTO audio "
        "(hash,data,size,suffix,probe,inserted_at) VALUES (?,zeroblob(?),?,?,?,?)",
        (key, stored_size, size, suffix, probe_json, inserted_at))
    if cursor.rowcount:
        rowid = cursor.lastrowid
    elif restore:
        rowid, old_size, packed = connection.execute(
            "SELECT a.rowid,a.size,d.hash FROM audio a LEFT JOIN audio_deflate d ON d.hash=a.hash "
            "WHERE a.hash=?", (key,)).fetchone()
        try:
            if read_original(connection, rowid, bool(packed)) == (key, old_size):
                # A newer probe may have read identity tags unavailable at first intake.
                connection.execute("UPDATE audio SET probe=json_patch(probe, ?) WHERE rowid=?",
                                   (probe_json, rowid))
                return
        except (ValueError, zlib.error):
            pass
        # Rebuild a damaged payload from the verified incoming original.
        # Updating in place retains every existing owner of the hash.
        connection.execute("UPDATE audio SET data=zeroblob(?),size=?,suffix=?,probe=? WHERE rowid=?",
                           (stored_size, size, suffix, probe_json, rowid))
        connection.execute("DELETE FROM audio_deflate WHERE hash=?", (key,))
    else:
        return
    if hasattr(connection, "blobopen"):
        with connection.blobopen("audio", "data", rowid) as blob:
            shutil.copyfileobj(payload, blob)
    else:
        connection.execute("UPDATE audio SET data=? WHERE hash=?", (payload.read(), key))
    if compressed:
        connection.execute("INSERT INTO audio_deflate VALUES (?)", (key,))


def read_original(connection, rowid, compressed, output=None):
    """Hash the decoded byte stream, optionally copying it to an open file."""
    digest = hashlib.sha256()
    size = 0
    decoder = zlib.decompressobj() if compressed else None
    def accept(chunk):
        nonlocal size
        data = decoder.decompress(chunk) if decoder else chunk
        digest.update(data)
        size += len(data)
        if output is not None:
            output.write(data)

    if hasattr(connection, "blobopen"):
        with connection.blobopen("audio", "data", rowid, readonly=True) as stream:
            for chunk in iter(lambda: stream.read(32768), b""):
                accept(chunk)
    else:
        # sqlite3.Blob was added in Python 3.11. Keep the older runtime
        # supported without allocating a second full decoded copy.
        raw = connection.execute("SELECT data FROM audio WHERE rowid=?", (rowid,)).fetchone()[0]
        for index in range(0, len(raw), 32768):
            accept(raw[index:index + 32768])
    if decoder and (not decoder.eof or decoder.unused_data):
        raise ValueError("Invalid compressed audio stream")
    return digest.hexdigest(), size


def adopt_legacy_owner(db, kind, identity, key, title, length, request=None):
    """Save a verified legacy reference without replacing an existing durable owner."""
    row = db.execute("SELECT probe,suffix FROM audio WHERE hash=?", (key,)).fetchone()
    verdict = judge_claim(title, length, json.loads(row[0]) if row else None, row[1] if row else "", request)
    if verdict.ok:
        db.execute("INSERT OR IGNORE INTO audio_owners VALUES (?,?,?)", (kind, identity, key))
    return verdict


class MediaStore:
    """The media database: verified audio blobs keyed by content hash.

    One short connection per operation, the requests store's
    discipline: the server is threaded and the runner is a second
    process, so no connection is ever shared or held.
    """

    def __init__(self, path):
        self.path = Path(path)

    def _run(self, work, *, synchronous="FULL"):
        connection = _connect(self.path, synchronous=synchronous)
        try:
            result = work(connection)
            connection.commit()
            return result
        finally:
            connection.close()

    def put_file(self, path, probe, *, owners=(), expected_stamp=None, expected_hash=None):
        """Commit the exact byte stream and its stable song identity together."""
        path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as source:
            before = source_stamp(os.fstat(source.fileno()))
            if expected_stamp is not None and before != expected_stamp:
                raise OSError(f"Audio changed after verification: {path.name}")
            with encode_audio(source, self.path.parent) as encoded:
                key, size = encoded[:2]
                if expected_hash is not None and key != expected_hash:
                    raise ValueError("Original audio checksum does not match")
                if size != before[2] or source_stamp(os.fstat(source.fileno())) != before:
                    raise OSError(f"Audio changed during intake: {path.name}")

                def work(connection):
                    insert_audio(connection, encoded, path.suffix.lower(), json.dumps(probe or {}),
                                 time.time(), restore=True)
                    if source_stamp(os.fstat(source.fileno())) != before:
                        raise OSError(f"Audio changed during intake: {path.name}")
                    connection.executemany("INSERT OR REPLACE INTO audio_owners VALUES (?,?,?)",
                                           ((kind, identity, key) for kind, identity in owners))
                self._run(work)
        return key

    def adopt_catalog_references(self, coverage, records):
        """Preserve legacy catalog claims before report rewrites their coverage."""
        candidates = {row["index"]: blob_key(row["path"]) for row in coverage
                      if row["status"] in ("verified", "retired") and is_blob_ref(row["path"])}
        if not candidates:
            return

        def work(db):
            owned = {identity for identity, in db.execute("SELECT identity FROM audio_owners WHERE kind='catalog'")}
            for record in records:
                key = candidates.get(record["index"])
                if key and record["key"] not in owned:
                    adopt_legacy_owner(db, "catalog", record["key"], key, record["title"], record["length"])
        self._run(work)

    def owned_audio(self, kind, identity):
        def work(connection):
            row = connection.execute("SELECT hash FROM audio_owners WHERE kind=? AND identity=?",
                                     (kind, identity)).fetchone()
            return blob_ref(row[0]) if row else None
        return self._run(work)

    def owned_catalog_audio(self):
        def work(connection):
            return {identity: (blob_ref(key), json.loads(probe))
                    for identity, key, probe in connection.execute(
                        "SELECT o.identity,a.hash,a.probe FROM audio_owners o "
                        "JOIN audio a ON a.hash=o.hash WHERE o.kind='catalog'")}
        return self._run(work)

    def original_formats(self, keys):
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        return self._run(lambda db: {key: (size, suffix) for key, size, suffix in db.execute(
            f"SELECT hash,size,suffix FROM audio WHERE hash IN ({placeholders})", keys)})

    def phone_keys(self):
        return self._run(lambda db: {row[0] for row in db.execute(
            "SELECT identity FROM audio_owners WHERE kind='phone'")})

    def phone_originals(self):
        return self._run(lambda db: [(identity, json.loads(facts)) for identity, facts in db.execute(
            "SELECT o.identity,a.probe FROM audio_owners o JOIN audio a ON a.hash=o.hash "
            "WHERE o.kind='phone' ORDER BY o.identity")])

    def has(self, key):
        def work(connection):
            return connection.execute(
                "SELECT 1 FROM audio WHERE hash = ?", (key,)).fetchone() is not None
        return self._run(work)

    def probe(self, key):
        """The stored probe dict for a key, or None when absent."""
        def work(connection):
            row = connection.execute(
                "SELECT probe FROM audio WHERE hash = ?", (key,)).fetchone()
            return json.loads(row[0]) if row else None
        return self._run(work)

    def suffix(self, key):
        """The stored file suffix (".mp3") for a key, or None."""
        def work(connection):
            row = connection.execute(
                "SELECT suffix FROM audio WHERE hash = ?", (key,)).fetchone()
            return row[0] if row else None
        return self._run(work)

    def size(self, key):
        """The stored byte count for a key, or None."""
        def work(connection):
            row = connection.execute(
                "SELECT size FROM audio WHERE hash = ?", (key,)).fetchone()
            return row[0] if row else None
        return self._run(work)

    def verifies_original(self, key, suffix=None, *, owned=False):
        """Confirm the durable copy before discarding a persistent playback cache file."""
        def work(connection):
            row = connection.execute(
                "SELECT a.rowid,a.size,d.hash FROM audio a LEFT JOIN audio_deflate d ON d.hash=a.hash "
                "WHERE a.hash=? AND (? IS NULL OR a.suffix=?) "
                "AND (NOT ? OR EXISTS (SELECT 1 FROM audio_owners WHERE hash=a.hash))",
                (key, suffix, suffix, owned)).fetchone()
            if row is None:
                return False
            try:
                return read_original(connection, row[0], bool(row[2])) == (key, row[1])
            except (ValueError, zlib.error):
                return False
        return self._run(work)

    def matches_original(self, path, key=None):
        """Check an on-disk copy against intact, owned bytes in SQLite."""
        actual = _sha256_file(path)
        return (key is None or actual == key) and self.verifies_original(actual, owned=True)

    def link_stored_original(self, path, owner, stamp):
        """Attach a verified transfer to an intact existing payload without a new blob write."""
        key = _sha256_file(path)
        if (source_stamp(path.stat()) != stamp or
                not self.verifies_original(key, path.suffix.lower(), owned=True)):
            return None

        def work(db):
            return db.execute(
                "INSERT OR REPLACE INTO audio_owners "
                "SELECT ?, ?, hash FROM audio WHERE hash=? "
                "AND EXISTS (SELECT 1 FROM audio_owners WHERE hash=audio.hash)",
                (*owner, key)).rowcount == 1
        return blob_ref(key) if self._run(work) else None

    def extract(self, key, dest, *, scratch_prefix=None):
        """Write a blob's bytes to dest; False when the key is absent.

        The caller hash-checks the written file: extraction is a
        plain write, and the buffer's staging is where a mismatch
        is refused.
        """
        dest = Path(dest)
        def work(connection):
            row = connection.execute(
                "SELECT a.rowid, a.size, d.hash FROM audio a LEFT JOIN audio_deflate d ON a.hash=d.hash "
                "WHERE a.hash=?", (key,)).fetchone()
            if row is None:
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=scratch_prefix, delete=False) as output:
                scratch = Path(output.name)
                try:
                    actual = read_original(connection, row[0], bool(row[2]), output)
                    if actual != (key, row[1]):
                        return False
                    output.flush()
                    scratch.replace(dest)
                    return True
                except (ValueError, zlib.error):
                    return False
                finally:
                    scratch.unlink(missing_ok=True)
        return self._run(work)

    @contextmanager
    def open_original(self, key):
        """Yield a verified seekable copy that disappears when its handle closes."""
        with tempfile.TemporaryFile(dir=self.path.parent) as output:
            def work(connection):
                row = connection.execute(
                    "SELECT a.rowid, a.size, d.hash, a.suffix FROM audio a LEFT JOIN audio_deflate d ON a.hash=d.hash "
                    "WHERE a.hash=?", (key,)).fetchone()
                if row is None:
                    raise FileNotFoundError("Audio is no longer stored")
                try:
                    if read_original(connection, row[0], bool(row[2]), output) == (key, row[1]):
                        return row[3]
                except (ValueError, zlib.error):
                    pass
                raise OSError("Stored audio failed verification")

            suffix = self._run(work)
            output.seek(0)
            yield output, key + suffix

    def delete(self, key):
        def work(connection):
            connection.execute("DELETE FROM audio WHERE hash = ?", (key,))
        self._run(work)

    def keys(self):
        def work(connection):
            return [row[0] for row in connection.execute("SELECT hash FROM audio")]
        return self._run(work)

    def original_exports(self, already_named):
        """Include every other stored original in a complete portable-room export."""
        def work(connection):
            return [(key, f"originals/{key}{suffix}")
                    for key, suffix in connection.execute("SELECT hash,suffix FROM audio ORDER BY hash")
                    if key not in already_named]
        return self._run(work)

    def count(self):
        def work(connection):
            return connection.execute("SELECT COUNT(*) FROM audio").fetchone()[0]
        return self._run(work)

    def audio_bytes(self):
        """The total bytes of audio the store holds (live rows only)."""
        def work(connection):
            return connection.execute(
                "SELECT COALESCE(SUM(size), 0) FROM audio").fetchone()[0]
        return self._run(work)

    def byte_counts(self):
        """Original and stored byte totals; both exclude SQLite overhead."""
        def work(connection):
            return connection.execute(
                "SELECT COALESCE(SUM(size),0),COALESCE(SUM(length(data)),0) FROM audio").fetchone()
        return self._run(work)

    def dead_bytes(self):
        """Bytes held by freed pages, waiting for the reclaim job."""
        def work(connection):
            free = connection.execute("PRAGMA freelist_count").fetchone()[0]
            page = connection.execute("PRAGMA page_size").fetchone()[0]
            return free * page
        return self._run(work)

    def vacuum(self):
        """Rewrite the whole database file, compactly (plan 062
        step 4).

        The one deliberate exception to this module's rule against
        a full rewrite at library scale: it runs once, at the end
        of the one-time library migration, while the migration
        worker still holds the floor, so the file the owner
        carries to another machine is as small as its contents.
        Progressive reclaim stays the rule afterwards. VACUUM
        cannot run inside a transaction; the connection's ritual
        leaves none open, and the statement runs bare.
        """
        connection = _connect(self.path)
        try:
            connection.execute("VACUUM")
            connection.commit()
        finally:
            connection.close()

    def reclaim_burst(self, max_calls, seconds):
        """Hand freed pages back, in one bounded burst; returns the
        number of pages freed.

        The spike's mechanics: on this SQLite, one incremental
        vacuum call frees exactly one page whatever count it is
        handed, so the burst is bounded by calls and wall time,
        never by pages. The connection runs at synchronous=NORMAL
        (see the module docstring). A call that frees nothing ends
        the burst early: the freelist is empty or the file was not
        created with incremental vacuum, and either way more calls
        would only spin.
        """
        deadline = time.monotonic() + seconds
        freed = 0
        connection = _connect(self.path, synchronous="NORMAL")
        try:
            while freed < max_calls and time.monotonic() < deadline:
                before = connection.execute(
                    "PRAGMA freelist_count").fetchone()[0]
                if before == 0:
                    break
                connection.execute("PRAGMA incremental_vacuum")
                connection.commit()
                after = connection.execute(
                    "PRAGMA freelist_count").fetchone()[0]
                if after >= before:
                    break
                freed += before - after
        finally:
            connection.close()
        return freed


class PlaybackBuffer:
    """The playback buffer: staged plain files for blob tracks.

    A staged file is named `<key><suffix>` and its bytes hash to
    its name; staging verifies that, so a corrupt extraction is
    refused instead of served. The window keeps at most three
    recent staged copies and releases them after idle time.
    """

    WINDOW = 3
    IDLE_SECONDS = 5 * 60
    # Reserved for temporary extracts in this private cache. Each name embeds
    # the verified original identity, even if a killed worker wrote only a prefix.
    SCRATCH = ".listening-playback-"

    def __init__(self, directory):
        self.directory = Path(directory)
        self._verified = {}

    def release(self, store):
        """Release staged originals and interrupted extracts after checking the database."""
        if not self.directory.is_symlink() and self.directory.is_dir():
            for path in self.directory.iterdir():
                self._release_file(store, path)

    def expire(self, store):
        # Python 3.9 and 3.10 lack incremental BLOB reads. Keep their
        # rolling window without adding a full BLOB read to the watchdog.
        if not hasattr(sqlite3.Connection, "blobopen"):
            return
        cutoff = time.time() - self.IDLE_SECONDS
        if not self.directory.is_symlink() and self.directory.is_dir():
            for path in self.directory.iterdir():
                try:
                    if path.lstat().st_mtime < cutoff:
                        self._release_file(store, path)
                except FileNotFoundError:
                    self._verified.pop(path, None)

    def _release_file(self, store, path):
        if path.is_symlink() or not path.is_file():
            return
        name = path.name
        scratch = name.startswith(self.SCRATCH)
        if scratch:
            name, separator, nonce = name[len(self.SCRATCH):].rpartition(".")
            if not separator or not re.fullmatch(r"[a-z0-9_]+", nonce):
                return
        key, suffix = os.path.splitext(name)
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            return
        stamp = source_stamp(path.stat())
        if (store.verifies_original(key, suffix) and (scratch or _sha256_file(path) == key)
                and source_stamp(path.lstat()) == stamp):
            path.unlink()
            self._verified.pop(path, None)

    def path_for(self, key):
        """The staged path for a key when one is staged, else None."""
        if not key or self.directory.is_symlink() or not self.directory.is_dir():
            return None
        for candidate in sorted(self.directory.glob(f"{key}.*")):
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        return None

    def stage(self, store, key):
        """Ensure a key's bytes are staged as a verified plain file.

        Returns the staged path, or None when the store does not
        hold the key or the extracted bytes fail their hash check.
        The store is consulted first: a key the store no longer
        holds (swept as unreferenced) must not be resurrected by a
        staged copy left in the window, so the copy is dropped.
        """
        if self.directory.is_symlink():
            return None
        suffix = store.suffix(key)
        if suffix is None:
            stale = self.path_for(key)
            if stale is not None and _sha256_file(stale) == key:
                stale.unlink(missing_ok=True)
                self._verified.pop(stale, None)
            return None
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self.directory / f"{key}{suffix}"
        if target.is_symlink():
            return None
        if target.exists():
            if not target.is_file():
                return None
            if self._verified.get(target) == source_stamp(target.stat()) or _sha256_file(target) == key:
                target.touch()
                self._verified[target] = source_stamp(target.stat())
                return target
            # Keep an unexpected edit outside the eviction window, then serve
            # the recording from SQLite again. The fresh directory prevents collisions.
            retained = Path(tempfile.mkdtemp(prefix="retained-", dir=self.directory))
            target.rename(retained / target.name)
        try:
            if not store.extract(key, target, scratch_prefix=f"{self.SCRATCH}{target.name}."):
                return None
        except OSError:
            return None
        self._verified[target] = source_stamp(target.stat())
        self._evict(store)
        return target

    def _evict(self, store):
        """Drop staged copies beyond the window, oldest first.

        Only files shaped like staged copies (a 64-hex name plus a
        suffix) are ever removed; anything else in the directory is
        not the buffer's to delete.
        """
        staged = [path for path in self.directory.iterdir()
                  if path.is_file()
                  and re.fullmatch(r"[0-9a-f]{64}\..+", path.name)]
        staged.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        for path in staged[self.WINDOW:]:
            self._release_file(store, path)


def blob_targets(store, coverage_rows, records_by_index, downloaded,
                 custom_requests, playlist_order=None, file_lookup=None):
    """The referenced blob tracks, as (key, export name) pairs.

    The export and share work list (plan 060 steps 6-7): every
    store key a verified coverage row or a custom request names,
    with the portable name its file gets outside the store.
    Catalog tracks name from their record; custom requests name
    from the request, under `custom/`. A downloaded catalog
    request bridges a fresh landing whose coverage row does not
    carry the reference yet, but only into a gap: when the
    record's verified coverage names a different home, the
    request is stale and contributes nothing (the same rule
    report()'s sweep and candidacy follow). The same (key, name)
    pair appears once; one key shared by two records appears
    under both names, since each is a referenced track. A key
    the store no longer holds is skipped.

    With `playlist_order` (plan 063), a playlist's member
    indices in playlist order, the work list is that playlist's export:
    restricted to those tracks and returned in playlist order.
    Custom requests belong to no playlist and drop out.
    """
    targets = []
    seen = set()
    wanted = None
    position = {}
    order_of = {}
    if playlist_order is not None:
        wanted = set(playlist_order)
        for place, index in enumerate(playlist_order):
            position.setdefault(index, place)

    def add(reference, label, record, index=None):
        if is_blob_ref(reference):
            source = blob_key(reference)
            suffix = store.suffix(source) if store else None
            if suffix is None:
                return
        else:
            source = file_lookup(reference) if reference and file_lookup else None
            if source is None or not source.is_file():
                return
            suffix = source.suffix
        name = portable_name(label.split("/")[-1], record["artist"], record["title"], suffix)
        if label.startswith("custom/"):
            name = "custom/" + name
        if name in seen:
            return
        seen.add(name)
        targets.append((source, name))
        if index is not None and wanted is not None:
            order_of[(source, name)] = position[index]

    coverage_by_index = {row["index"]: row for row in coverage_rows}
    for row in coverage_rows:
        if row.get("status") != "verified":
            continue
        if wanted is not None and row["index"] not in wanted:
            continue
        record = records_by_index.get(row["index"])
        if record is None:
            continue
        add(row["path"], f"{row['index']:05d}", record, row["index"])
    for entry in downloaded:
        reference = entry.get("audio_path", "")
        index = entry.get("track_index")
        if index and index in records_by_index:
            if wanted is not None and index not in wanted:
                continue
            if request_is_stale(coverage_by_index.get(index),
                                entry["audio_path"]):
                continue
            record = records_by_index[index]
            add(reference, f"{index:05d}", record, index)
        elif not index and wanted is None:
            record = custom_requests.get(entry["id"])
            if record is not None:
                add(reference, f"custom/{entry['id']}", record)
    if playlist_order is not None:
        targets.sort(key=lambda pair: order_of[pair])
    return targets


def request_is_stale(coverage_row, audio_path):
    """Whether a downloaded request's stored path is superseded:
    the record's verified coverage row names a different home
    now. report()'s candidacy and blob_targets both apply this
    one rule, so a stale request can neither reinstate an old
    key nor export its superseded bytes."""
    return coverage_row is not None \
        and coverage_row.get("status") == "verified" \
        and coverage_row.get("path") != audio_path


def custom_request_rows(downloaded, get_request):
    """The full request rows behind the custom (track-less)
    downloaded entries, keyed by request id.

    blob_targets names a custom track from its request, so both
    processes build this map the same way from the downloaded
    audio list and a request lookup.
    """
    customs = {}
    for entry in downloaded:
        if not entry.get("track_index"):
            row = get_request(entry["id"])
            if row is not None:
                customs[entry["id"]] = row
    return customs


def _publish_new_file(stage, dest):
    """Publish complete bytes under a new name, including on volumes without links."""
    try:
        os.link(stage, dest)
        return
    except OSError as error:
        if error.errno not in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM, errno.ENOSYS}:
            raise
    _rename_without_replacing(stage, dest)


def _rename_without_replacing(stage, dest):
    """Rename the verified stage only if the destination is absent at commit time."""
    if os.name == "nt":
        # Windows rename refuses an existing destination.
        stage.rename(dest)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    source, target = os.fsencode(stage), os.fsencode(dest)
    path_type = ctypes.c_char_p
    if sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        rename = libc.renamex_np
        rename.argtypes = [path_type, path_type, ctypes.c_uint]
        args = (source, target, 0x00000004)  # Darwin RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, path_type, ctypes.c_int, path_type, ctypes.c_uint]
        args = (-100, source, -100, target, 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "Atomic no-overwrite rename is unavailable", str(dest))
    rename.restype = ctypes.c_int
    if rename(*args) != 0:
        number = ctypes.get_errno()
        if number in {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL}:
            raise OSError(number, "Filesystem cannot atomically publish without overwriting", str(dest))
        raise OSError(number, os.strerror(number), str(dest))


def materialize(store, targets, folder, on_done=None, completed=None, *, preserve_existing=False, source_stamps=None, raise_errors=False):
    """Publish verified copies, preserving prior exports on failure."""
    folder = Path(folder)
    written = failed = 0
    for position, (source, name) in enumerate(targets, 1):
        dest = folder / name
        try:
            if not dest.parent.resolve().is_relative_to(folder.resolve()):
                raise OSError("Export destination escapes its folder")
            dest.parent.mkdir(parents=True, exist_ok=True)
            key = _sha256_file(source) if isinstance(source, Path) else source
            if source_stamps is not None and source_stamp(source.stat()) != source_stamps[source]:
                raise OSError("Audio changed after verification")
            if preserve_existing and (dest.exists() or dest.is_symlink()) and (
                    dest.is_symlink() or not dest.is_file() or _sha256_file(dest) != key):
                dest = dest.with_name(f"{dest.stem} - {key[:16]}{dest.suffix}")
            if not (dest.is_file() and not dest.is_symlink() and _sha256_file(dest) == key):
                with tempfile.TemporaryDirectory(dir=dest.parent) as temporary:
                    stage = Path(temporary) / "audio"
                    if isinstance(source, Path):
                        shutil.copyfile(source, stage)
                    elif not store.extract(key, stage):
                        raise OSError("Audio could not be extracted")
                    if _sha256_file(stage) != key:
                        raise OSError("Audio changed during export")
                    if preserve_existing:
                        _publish_new_file(stage, dest)
                    else:
                        stage.replace(dest)
                written += 1
            if completed is not None:
                completed.append((source, dest.relative_to(folder).as_posix()))
        except OSError:
            if raise_errors:
                raise
            failed += 1
        if on_done is not None:
            on_done(position, len(targets), failed)
    return written, failed


def playlist_export_folder(parent, base, scope):
    """Reserve the display name for one playlist, disambiguating collisions."""
    digest = hashlib.sha256(scope.encode()).hexdigest()[:12]
    for name in (base, f"{base} - {digest}"):
        folder = parent / name
        if folder.is_symlink():
            continue
        folder.mkdir(parents=True, exist_ok=True)
        marker = folder / ".listening-playlist"
        if not marker.exists() and not any(folder.iterdir()):
            try:
                with marker.open("x", encoding="utf-8") as output:
                    output.write(scope)
            except FileExistsError:
                pass
        if marker.is_file() and not marker.is_symlink() and marker.read_text(encoding="utf-8") == scope:
            return folder
    raise OSError("The playlist export destination is occupied")


_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def portable_name(label, artist, title, suffix):
    """The export name for a track (plan 060 step 6): the portable
    shape the library's own files use, made filesystem-safe. The
    label is the catalog track's padded index, or a custom
    request's id (its caller adds the `custom/` prefix)."""
    base = f"{label} - {artist} - {title}"
    return _UNSAFE_NAME.sub("-", base).strip() + suffix


def portable_folder_name(name):
    """A playlist's name made filesystem-safe for its export
    folder (plan 063): portable_name's character rule applied to
    the whole name. A name that leaves nothing usable behind
    falls back to "playlist", so the folder is never the
    exported/ root itself or a dot path."""
    cleaned = _UNSAFE_NAME.sub("-", str(name)).strip()
    return cleaned if cleaned not in ("", ".", "..") else "playlist"


# The export's recorded progress (plan 060 step 6): one small JSON
# file in the application folder. The runner writes it as an
# export runs; the server reads it for the settings snapshot. The
# shape is owned here so the two processes cannot drift apart on
# what an idle or partial record means.

EXPORT_STATUS_IDLE = {"state": "idle", "done": 0, "total": 0,
                      "failed": 0, "stamp": 0}


def export_status_record(state, done, total, failed, stamp):
    """One export run's progress record, in the shape
    read_export_status reads back. The codec owns the shape in
    both directions: writers build records here, readers parse
    them here."""
    return {"state": state, "done": done, "total": total,
            "failed": failed, "stamp": stamp}


def read_export_status(path):
    """The recorded export progress at path, or the idle default.

    A missing, unreadable, malformed, or non-dict file reads as
    idle: the snapshot reports "no export yet" rather than
    failing on a file another process may be mid-write on.
    """
    try:
        status = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(EXPORT_STATUS_IDLE)
    return status if isinstance(status, dict) else dict(EXPORT_STATUS_IDLE)


def dumps_export_status(status):
    """The serialized form of an export status record.

    Serialization lives here with the shape; the write itself
    stays with each process's own writer, because the runner's
    private_write is atomic at mode 0600 and a status file must
    never be observed torn.
    """
    return json.dumps(status)
