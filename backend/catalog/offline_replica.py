"""Public catalog pages and additive, verified originals for an offline device."""

from contextlib import closing
from functools import lru_cache
import hashlib
import re
import sqlite3
import uuid

from catalog.audio_resolution import resolve_stored
from catalog.audio_store import MediaStore, blob_key, is_blob_ref
from downloads.verification import judge, probe
from system.formats import AUDIO_FORMATS

PHONE_PREFIX = "urn:listening:phone:"
PAGE_SIZE = 80
MAX_UPLOAD = 512 << 20


IDENTITY_SCHEMA = """CREATE TABLE IF NOT EXISTS replica_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL);"""


def collection_id(database):
    if not database.is_file():
        MediaStore(database).count()
    with closing(sqlite3.connect(database, timeout=30)) as db, db:
        db.execute(IDENTITY_SCHEMA)
        row = db.execute("SELECT id FROM replica_identity WHERE singleton=1").fetchone()
        if row:
            return row[0]
        db.execute("INSERT OR IGNORE INTO replica_identity VALUES (1,?)", (uuid.uuid4().hex,))
        return db.execute("SELECT id FROM replica_identity WHERE singleton=1").fetchone()[0]


@lru_cache(maxsize=8192)
def _file_hash(path, stamp):
    with path.open("rb") as handle:
        digest = hashlib.sha256()
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def catalog_page(catalog, requests, cursor):
    section, sep, position = cursor.partition("-")
    if section not in ("tracks", "requests") or not sep or not re.fullmatch(r"[0-9]{1,12}", position):
        raise ValueError("Invalid replica page")
    after = int(position)
    entries = []
    if section == "tracks":
        stop = min(after + PAGE_SIZE, len(catalog.records))
        for index in range(after + 1, stop + 1):
            record = catalog.active_record_at(index)
            if record:
                key = record["key"]
                identity = ("phone:" + key[len(PHONE_PREFIX):] if key.startswith(PHONE_PREFIX)
                            else "track:" + hashlib.sha256(key.encode()).hexdigest())
                item = catalog.track(index)
                entries.append((identity, item, catalog.coverage.get(index, {}).get("path", "")))
        following = f"tracks-{stop}" if stop < len(catalog.records) else "requests-0"
    else:
        rows = requests.replica_customs(after, PAGE_SIZE + 1)
        for row in rows[:PAGE_SIZE]:
            item = {"artist": row["artist"], "title": row["title"], "duration": row["length"] or 0,
                    "status": "verified", "audio": f"/api/audio/custom/{row['id']}"}
            entries.append((f"request:{row['id']}", item, row["audio_path"]))
        following = f"requests-{rows[PAGE_SIZE - 1]['id']}" if len(rows) > PAGE_SIZE else ""
    return entries, following


def manifest(entries, following, library, store):
    blobs = store.original_formats([blob_key(path) for _, item, path in entries
                                    if item["audio"] and is_blob_ref(path)]) if store else {}
    tracks = []
    for identity, item, path in entries:
        original = None
        if item.get("audio"):
            if is_blob_ref(path):
                key = blob_key(path)
                if key in blobs:
                    size, suffix = blobs[key]
                    original = {"hash": key, "size": size, "suffix": suffix}
            else:
                local = resolve_stored(path, library)
                if local:
                    try:
                        stat = local.stat()
                        key = _file_hash(local, (stat.st_dev, stat.st_ino, stat.st_size, stat.st_ctime_ns))
                        original = {"hash": key, "size": stat.st_size, "suffix": local.suffix.lower()}
                    except OSError:
                        pass
        if identity.startswith("request:"):
            # Snapshot rollback can reuse a request number for a different recording.
            digest = hashlib.sha256()
            for field in (item["artist"], item["title"], (original or {}).get("hash", "")):
                digest.update(field.encode() + b"\0")
            identity = "request:" + digest.hexdigest()
        tracks.append({**item, "key": identity, "original": original})
    return {"tracks": tracks, "next": following}


def accept_original(path, expected, title, duration, store):
    if path.suffix not in AUDIO_FORMATS:
        raise ValueError("Unsupported audio format")
    facts = probe(path, local_audio_only=True)
    verdict = judge(title, duration, facts)
    if not verdict.ok:
        raise ValueError(verdict.reason)
    # Keep a filename fallback for untagged personal files in the durable probe.
    facts["display_title"] = facts["title"] or title
    key = PHONE_PREFIX + expected
    store.put_file(path, facts, owners=(("phone", key), ("catalog", key)), expected_hash=expected)


def add_phone_records(records, by_key, store):
    """Include personal imports on every rebuild, even in a room without lists."""
    if store is None:
        return
    for key, facts in store.phone_originals():
        if key in by_key:
            records[by_key[key] - 1]["active"] = True
            continue
        index = len(records) + 1
        records.append({"index": index, "key": key, "uri": key, "active": True,
                        "title": facts.get("display_title") or facts.get("title") or "Untitled",
                        "artist": facts.get("artist") or "Unknown artist", "album": facts.get("album", ""),
                        "length": facts["duration"], "isrc": "", "first_playlist": "Phone imports"})
        by_key[key] = index
