"""Copy a stopped room into one portable, verified SQLite collection.

The source databases are attached read-only. The server and every runner hold
shared locks on their application folder and portable room, including workers
surviving a server stop.
Publication never replaces a destination selected by an earlier run.
"""
from contextlib import ExitStack, closing, contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from catalog.collection_lists import PREVIOUS_SCHEMA
from catalog import catalog_store
from catalog.audio_resolution import locate_stored
from catalog.offline_replica import IDENTITY_SCHEMA
from catalog.audio_store import MediaStore, adopt_legacy_owner, encode_audio, insert_audio, blob_key, blob_ref, is_blob_ref, read_original, source_stamp
from catalog.favorites import FAVORITES_SCHEMA
from adapters.spotify_playlists import SCHEMA as SPOTIFY_LINK_SCHEMA
from catalog.genres import GENRE_SCHEMA
from catalog.moods import SCHEMA as MOOD_SCHEMA
from catalog.lengths import LENGTHS_SCHEMA
from downloads.requests_store import SCHEMA as REQUEST_SCHEMA
from downloads.verification import judge_claim, probe
from library.library_protocol import SUPPORTED
from catalog import legacy_spoken
from library import playlist_links
from playback.play_events import SCHEMA as EVENTS_SCHEMA
from system.config import collection_databases
from system.formats import AUDIO_FORMATS
from system.private_files import private_write

RESERVE = 20 * 1024 ** 3
STATE_TABLES = ("catalog_records", "catalog_meta", "coverage", "requests",
                "genres", "mood_genres", "spotify_mirrors", "favorites", "lengths", "play_events", "collection_list_previous",
                *playlist_links.TABLES, *legacy_spoken.PODCAST_TABLES)
MEDIA_TABLES = ("replica_identity", "audio", "audio_deflate", "audio_owners", *legacy_spoken.BOOK_TABLES)

COLLECTION_SCHEMA = """
CREATE TABLE collection_info (version INTEGER PRIMARY KEY, playlist_names TEXT NOT NULL);
CREATE TABLE collection_lists (name TEXT PRIMARY KEY, content BLOB NOT NULL);
CREATE TABLE collection_inventory (name TEXT PRIMARY KEY, hash TEXT, reason TEXT NOT NULL);
"""


@contextmanager
def collection_access(application_folder, *, exclusive=False):
    """Keep snapshot reads apart from all room writes without a pidfile."""
    folder = Path(application_folder)
    if not exclusive:
        folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(folder, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("The room is busy. Stop the server and its workers before taking a snapshot.") from error
        yield
    finally:
        os.close(fd)


@contextmanager
def portable_room_access(root, application_folder, *, exclusive=False):
    """Serialize maintenance even when two installations select one portable room."""
    root = Path(root).resolve()
    if root != Path(application_folder).resolve() and (root / "collection.sqlite3").is_file():
        with collection_access(root, exclusive=exclusive):
            yield
    else:
        yield


def _copy_legacy_audio(db, tables, directory):
    """Keep encoded rows intact and encode legacy raw blobs during adoption."""
    condition = ""
    if "audio_deflate" in tables:
        db.execute("INSERT INTO main.audio SELECT a.* FROM original.audio a "
                   "JOIN original.audio_deflate d ON a.hash=d.hash")
        condition = "WHERE hash NOT IN (SELECT hash FROM original.audio_deflate)"
    for rowid, key, size, suffix, probe_json, inserted_at in db.execute(
            f"SELECT rowid,hash,size,suffix,probe,inserted_at FROM original.audio {condition}"):
        if hasattr(db, "blobopen"):
            source = db.blobopen("audio", "data", rowid, readonly=True, name="original")
        else:
            source = io.BytesIO(db.execute("SELECT data FROM original.audio WHERE rowid=?", (rowid,)).fetchone()[0])
        with source, encode_audio(source, directory) as encoded:
            if encoded[:2] != (key, size):
                raise ValueError("Legacy audio does not match its original hash")
            insert_audio(db, encoded, suffix, probe_json, inserted_at)


def _copy_tables(db, source, tables, *, encode_in=None):
    if not source.is_file():
        return
    db.execute("ATTACH DATABASE ? AS original", (source.as_uri() + "?mode=ro",))
    try:
        with db:
            existing = {r[0] for r in db.execute(
                "SELECT name FROM original.sqlite_master WHERE type='table'")}
            for table in tables:
                if table not in existing:
                    continue
                if table == "audio" and encode_in is not None:
                    _copy_legacy_audio(db, existing, encode_in)
                    continue
                old = {r[1] for r in db.execute(f"PRAGMA original.table_info({table})")}
                columns = ",".join(r[1] for r in db.execute(f"PRAGMA main.table_info({table})")
                                   if r[1] in old)
                db.execute(f"INSERT INTO main.{table} ({columns}) SELECT {columns} FROM original.{table}")
    finally:
        db.execute("DETACH DATABASE original")


def copy_recording_profiles(db, source):
    """Preserve profiles from hosts predating favorites and event history."""
    if not source.is_file():
        return
    db.execute("ATTACH DATABASE ? AS profile_source", (source.as_uri() + "?mode=ro",))
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM profile_source.sqlite_master WHERE type='table'")}
        if "track_profile" not in tables:
            return
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS track_profile (
                track_key TEXT PRIMARY KEY, like_level INTEGER NOT NULL, play_count INTEGER NOT NULL,
                last_played_at REAL NOT NULL, styles TEXT NOT NULL, like_changed_at INTEGER NOT NULL)""")
            db.execute("""INSERT INTO track_profile SELECT track_key,like_level,play_count,last_played_at,
                       styles,like_changed_at FROM profile_source.track_profile""")
            if "favorites" not in tables:
                # That schema recorded changes with time.time_ns(). A modern
                # favorites table, including an empty one, owns subsequent edits.
                db.execute("""INSERT INTO favorites(key,stamp)
                           SELECT track_key,like_changed_at/1000000000.0 FROM track_profile WHERE like_level>0""")
    finally:
        db.execute("DETACH DATABASE profile_source")


def _initialize(path, state, media):
    # Initialize incremental vacuum before the catalog connection enables WAL.
    MediaStore(path).count()
    with closing(catalog_store.connect(path)) as db:
        db.executescript(IDENTITY_SCHEMA + SPOTIFY_LINK_SCHEMA + MOOD_SCHEMA + GENRE_SCHEMA + FAVORITES_SCHEMA + LENGTHS_SCHEMA +
                         EVENTS_SCHEMA + COLLECTION_SCHEMA + PREVIOUS_SCHEMA + ";" + playlist_links.SCHEMA
                         + legacy_spoken.SCHEMA)
        for statement in REQUEST_SCHEMA:
            db.execute(statement)
        db.commit()
        _copy_tables(db, state, STATE_TABLES)
        copy_recording_profiles(db, state)
        _copy_tables(db, media, MEDIA_TABLES, encode_in=path.parent if state != media else None)


def _verify(db, *, allow_file_landing=False):
    if db.execute("SELECT version FROM collection_info").fetchall() != [(1,)]:
        raise ValueError("Unsupported or incomplete collection snapshot")
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite integrity check failed")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("Collection contains a dangling audio owner")
    for key, rowid, size, packed in db.execute(
        "SELECT a.hash,a.rowid,a.size,d.hash FROM audio a LEFT JOIN audio_deflate d ON d.hash=a.hash"
    ):
        if read_original(db, rowid, bool(packed)) != (key, size):
            raise ValueError("Stored audio does not match its original hash")
    for table, column, condition in (("coverage", "path", "status='verified'"),
                                      ("requests", "audio_path", "status='downloaded'")):
        # An open room can deliberately use file landing. Refresh checks
        # those paths against the selected host's settings. A portable
        # snapshot must independently resolve every playable reference.
        file_filter = f"AND r.{column} LIKE 'blob:%'" if allow_file_landing else ""
        if db.execute(f"SELECT 1 FROM {table} r WHERE {condition} {file_filter} AND NOT EXISTS "
                      f"(SELECT 1 FROM audio a WHERE 'blob:' || a.hash = r.{column}) LIMIT 1").fetchone():
            raise ValueError("Snapshot contains an unresolved playable track")


def _request_claim(db, kind, idx):
    if kind != "request":
        return None
    cursor = db.execute("SELECT * FROM requests WHERE id=?", (idx,))
    return dict(zip((column[0] for column in cursor.description), cursor.fetchone()))


def _files_and_claims(db, library):
    """Collect all audio files, including legacy references outside the room."""
    inputs = {p.resolve(): p.relative_to(library).as_posix() for p in library.rglob("*")
              if p.is_file() and p.suffix.lower() in AUDIO_FORMATS}
    claims = {}
    rows = db.execute("SELECT c.idx,c.path,r.record,c.status FROM coverage c JOIN catalog_records r ON r.idx=c.idx "
                      "WHERE c.status='verified' OR (c.status='retired' AND c.path LIKE 'blob:%')")
    entries = []
    for idx, path, payload, status in rows:
        record = json.loads(payload)
        if status == "retired":
            adopt_legacy_owner(db, "catalog", record["key"], blob_key(path), record["title"], record["length"])
        else:
            entries.append(("catalog", idx, record["key"], path, record["title"], record["length"]))
    entries.extend(("request", idx, str(idx), path, title, length)
                   for idx, path, title, length in db.execute(
                       "SELECT id,audio_path,title,length FROM requests WHERE status='downloaded'"))
    for kind, idx, identity, path, title, length in entries:
        if is_blob_ref(path):
            if db.execute("SELECT 1 FROM audio WHERE hash=?", (blob_key(path),)).fetchone() is None:
                raise ValueError("Legacy reference names missing database audio")
            verdict = adopt_legacy_owner(db, kind, identity, blob_key(path), title, length, _request_claim(db, kind, idx))
            if not verdict.ok and kind == "catalog":
                db.execute("UPDATE coverage SET path='',status='rejected',reason=? WHERE idx=?", (verdict.reason, idx))
            if not verdict.ok and kind == "request":
                db.execute("UPDATE requests SET audio_path='',status='error',message=? WHERE id=?", (verdict.reason, idx))
            continue
        located = locate_stored(path, library) if path else None
        if located is None or not located.is_file():
            raise ValueError(f"Missing {kind} audio for entry {idx}. Restore its source and retry.")
        resolved = located.resolve()
        inputs.setdefault(resolved, f"external/{kind}-{idx}/{resolved.name}")
        claims.setdefault(resolved, []).append((kind, idx, identity, title, length))
    return inputs, claims


def _stamp(path):
    return source_stamp(path.stat())


def _pack_files(path, work):
    store = MediaStore(path)
    with closing(sqlite3.connect(path)) as db:
        settled = {name for (name,) in db.execute("SELECT name FROM collection_inventory")}
        required = sum(item["stamp"][2] for item in work if item["name"] not in settled)
        if shutil.disk_usage(path.parent).free < required * 2 + RESERVE:
            raise ValueError("Insufficient destination space for source copies, SQLite writes and the 20 GiB reserve")
        for item in work:
            source = Path(item["source"])
            name = item["name"]
            before = _stamp(source)
            if before != item["stamp"]:
                raise ValueError("A source changed since the copy began. Start a snapshot at a new destination.")
            if name in settled:
                continue
            facts = probe(source)
            key = None
            reason = ""
            if facts is None:
                reason = "Unreadable audio"
            else:
                key = store.put_file(source, facts, expected_stamp=before)
            if _stamp(source) != before:
                raise ValueError(f"Source changed while copying {name}. Retry at a new destination.")
            with db:
                db.execute("INSERT INTO collection_inventory VALUES (?,?,?)", (name, key, reason))
                for kind, idx, identity, title, length in item["claims"]:
                    verdict = judge_claim(title, length, facts, source.suffix, _request_claim(db, kind, idx))
                    if verdict.ok:
                        db.execute("INSERT OR REPLACE INTO audio_owners VALUES (?,?,?)", (kind, identity, key))
                    if kind == "catalog":
                        db.execute("UPDATE coverage SET path=?,status=?,reason=? WHERE idx=?",
                                   (blob_ref(key) if verdict.ok else "", "verified" if verdict.ok else "rejected",
                                    verdict.reason, idx))
                    else:
                        db.execute("UPDATE requests SET audio_path=?,status=?,message=? WHERE id=?",
                                   (blob_ref(key) if verdict.ok else "", "downloaded" if verdict.ok else "error",
                                    verdict.reason or "Ready to play", idx))


def create_snapshot(config_path, destination):
    """Build a standalone snapshot. Report unreadable inventory in its ledger."""
    config_path = Path(config_path).expanduser().resolve(strict=True)
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Snapshot destination already exists")
    with collection_access(config_path.parent, exclusive=True), ExitStack() as locks:
        settings = json.loads(config_path.read_text(encoding="utf-8"))
        root = Path(settings["folder"]).expanduser().resolve(strict=True)
        locks.enter_context(portable_room_access(root, config_path.parent, exclusive=True))
        library = Path(settings.get("music_folder") or root / "audio").expanduser().resolve()
        lists = Path(settings.get("lists_folder") or root / "lists").expanduser().resolve()
        state, media = collection_databases(root, config_path.parent)
        if not state.is_file():
            raise ValueError("The room has no initialized catalog database")
        destination.parent.mkdir(parents=True, exist_ok=True)
        sources = set(p for p in (state, media) if p.is_file())
        # Keep the ledger outside the portable database. Paths and source
        # stamps help resume interrupted copies and belong only to this host.
        staging = destination.with_name("." + destination.name + ".in-progress")
        if staging.is_symlink():
            raise ValueError("Snapshot staging path is occupied")
        staging.mkdir(mode=0o700, exist_ok=True)
        with collection_access(staging, exclusive=True):
            path = staging / "collection.sqlite3"
            ledger = staging / "progress.json"
            inputs = [] if state == media else sorted(
                p for p in lists.glob("*") if p.is_file() and p.suffix.lower() in SUPPORTED)
            tracked = [config_path, *inputs, *sorted(sources)]
            tracked += [wal for p in sorted(sources) if (wal := Path(str(p) + "-wal")).exists() and wal.stat().st_size]
            paths = [(str(p), _stamp(p)) for p in tracked]
            audio_names = sorted(str(p.relative_to(library)) for p in library.rglob("*")
                                 if p.is_file() and p.suffix.lower() in AUDIO_FORMATS)
            fingerprint = hashlib.sha256(json.dumps([paths, audio_names]).encode()).hexdigest()
            if ledger.exists():
                saved = json.loads(ledger.read_text())
                if saved.get("fingerprint") != fingerprint:
                    raise ValueError("The room changed since the copy began. Choose a new snapshot destination.")
                work = saved["files"]
            else:
                if shutil.disk_usage(destination.parent).free < RESERVE + sum(p.stat().st_size for p in sources) * 2:
                    raise ValueError("Insufficient destination space for the databases and 20 GiB reserve")
                if any(staging.iterdir()):
                    raise ValueError("Snapshot staging is occupied or initialization was interrupted. Choose a new destination.")
                with tempfile.TemporaryDirectory(dir=staging) as temp:
                    initialized = Path(temp) / "initial.sqlite3"
                    _initialize(initialized, state, media)
                    with closing(sqlite3.connect(initialized)) as db, db:
                        files, claims = _files_and_claims(db, library)
                        work = [{"source": str(p), "name": name, "claims": claims.get(p, []),
                                 "stamp": _stamp(p)} for p, name in files.items()]
                    os.chmod(initialized, 0o600)
                    initialized.rename(path)
                    private_write(ledger, json.dumps({"fingerprint": fingerprint, "files": work}))
            _pack_files(path, work)
            with closing(sqlite3.connect(path)) as db:
                if state == media:
                    db.execute("ATTACH DATABASE ? AS prior", (state.as_uri() + "?mode=ro",))
                    db.execute("INSERT OR IGNORE INTO collection_inventory SELECT * FROM prior.collection_inventory")
                    db.execute("INSERT OR REPLACE INTO collection_info SELECT * FROM prior.collection_info")
                    db.execute("INSERT OR REPLACE INTO collection_lists SELECT * FROM prior.collection_lists")
                with db:
                    if state != media:
                        db.execute("INSERT OR REPLACE INTO collection_info VALUES (1,?)",
                                   (json.dumps(settings.get("playlist_names", {})),))
                    for source in inputs:
                        db.execute("INSERT OR REPLACE INTO collection_lists VALUES (?,?)",
                                   (source.name, source.read_bytes()))
                    _verify(db)
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            os.link(path, destination)
            path.unlink()
            ledger.unlink()
        staging.rmdir()
    return destination


def hydrate_lists(collection, lists):
    """Recreate a missing portable list directory from the collection."""
    lists = Path(lists)
    if lists.exists():
        return
    lists.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=lists.parent) as temporary:
        stage = Path(temporary) / "lists"
        stage.mkdir()
        with closing(sqlite3.connect(Path(collection).as_uri() + "?mode=ro", uri=True)) as db:
            for name, data in db.execute("SELECT name,content FROM collection_lists"):
                if Path(name).name != name or Path(name).suffix.lower() not in SUPPORTED:
                    raise ValueError("Invalid portable playlist name")
                with (stage / name).open("xb") as output:
                    output.write(data)
        # Another boot or an import may already have initialized this folder.
        try:
            lists.mkdir()
        except FileExistsError:
            return
        os.rename(stage, lists)


def create_empty_collection(room):
    """Initialize an explicitly new collection without replacing any file."""
    room = Path(room)
    room.mkdir(parents=True, exist_ok=True)
    if any(room.iterdir()):
        raise ValueError("Create requires a new or empty room folder")
    with tempfile.TemporaryDirectory(dir=room) as staging:
        path = Path(staging) / "collection.sqlite3"
        # Only pre-existing files are attached by _initialize.
        _initialize(path, Path(staging) / "no-state", Path(staging) / "no-media")
        with closing(sqlite3.connect(path)) as db:
            with db:
                db.execute("INSERT INTO collection_info VALUES (1,'{}')")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        os.chmod(path, 0o600)
        os.link(path, room / "collection.sqlite3")


def verify_collection(room):
    path = (Path(room) / "collection.sqlite3").resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        _verify(db, allow_file_landing=True)


def restore_snapshot(snapshot, room, *, unpack_lists=True):
    """Verify and open the snapshot as a new room without modifying the source."""
    snapshot = Path(snapshot).expanduser().resolve(strict=True)
    room = Path(room).expanduser().absolute()
    if room.exists() or room.is_symlink():
        raise ValueError("Restore requires a new room folder")
    room.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(room.parent).free < snapshot.stat().st_size + RESERVE:
        raise ValueError("Insufficient destination space for restore and the 20 GiB reserve")
    with tempfile.TemporaryDirectory(dir=room.parent) as temp:
        stage = Path(temp) / "room"
        stage.mkdir(mode=0o700)
        path = stage / "collection.sqlite3"
        with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(path)) as db:
                source.backup(db)
                _verify(db)
        if unpack_lists:
            hydrate_lists(path, stage / "lists")
        os.chmod(path, 0o600)
        # Reserve the final name without replacing a concurrently created room.
        room.mkdir(mode=0o700)
        try:
            os.rename(stage, room)
        except BaseException:
            room.rmdir()
            raise
    return room
