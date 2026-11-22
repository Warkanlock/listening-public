"""Read-only inventory, verification and space estimate for a portable copy."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager, ExitStack
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zlib

from catalog.audio_resolution import locate_stored
from catalog.audio_store import blob_key, is_blob_ref, read_original, source_stamp
from catalog.collection_snapshot import RESERVE
from downloads.verification import judge, probe
from library.library_protocol import SUPPORTED
from system.config import collection_databases
from system.formats import AUDIO_FORMATS


def _stamp(path):
    try:
        return source_stamp(path.stat())
    except FileNotFoundError:
        return None


@contextmanager
def _read_db(path):
    """Never create journals in the owner's folder. Include committed WAL pages."""
    wal = Path(str(path) + "-wal")
    journal = Path(str(path) + "-journal")
    files = [path, wal, journal]
    before = [_stamp(f) for f in files]
    with ExitStack() as stack:
        # An immutable handle is safe only while its file stays unchanged.
        # An outstanding WAL needs normal SQLite recovery, on a private copy.
        if any(stamp and stamp[2] for stamp in before[1:]):
            temp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            for f, stamp in zip(files, before):
                if stamp:
                    shutil.copyfile(f, temp / f.name)
            if before != [_stamp(f) for f in files]:
                raise ValueError("The database changed while reading the journal. Retry the preview.")
            db = stack.enter_context(closing(sqlite3.connect(temp / path.name)))
        else:
            db = stack.enter_context(closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)))
        db.execute("PRAGMA query_only=ON")
        yield db
        if before != [_stamp(f) for f in files]:
            raise ValueError("The database changed during the preview. Retry for a consistent result.")


def _measure(chunks):
    """Hash and count the snapshot encoder's preferred lossless representation."""
    digest = hashlib.sha256()
    encoder = zlib.compressobj()
    raw = packed = 0
    for chunk in chunks:
        digest.update(chunk)
        raw += len(chunk)
        packed += len(encoder.compress(chunk))
    packed += len(encoder.flush())
    return digest.hexdigest(), raw, min(raw, packed)


def _legacy_chunks(db, rowid, size):
    chunk_size = 1 << 20
    if hasattr(db, "blobopen"):
        with db.blobopen("audio", "data", rowid, readonly=True) as stream:
            yield from iter(lambda: stream.read(chunk_size), b"")
    else:
        # Python 3.9/3.10 lack incremental SQLite blob handles. Keep
        # individual SQL results bounded on those supported versions.
        for offset in range(0, size, chunk_size):
            yield db.execute("SELECT substr(data, ?, ?) FROM audio WHERE rowid=?",
                             (offset + 1, chunk_size, rowid)).fetchone()[0]


def _stored_probe(payload):
    try:
        facts = json.loads(payload)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(facts, dict) or not isinstance(facts.get("title"), str):
        return None
    for key in ("bitrate", "duration"):
        value = facts.get(key)
        try:
            valid = type(value) in (float, int) and math.isfinite(value)
        except OverflowError:
            valid = False
        if not valid:
            return None
    return facts


def _stored_audio(db, key, rowid, size, actual, compressed, legacy):
    """Measure bytes as copied and check the original digest, in one pass."""
    packed_size = actual
    try:
        if legacy and not compressed:
            digest, original_size, packed_size = _measure(_legacy_chunks(db, rowid, actual))
        else:
            digest, original_size = read_original(db, rowid, compressed)
        return packed_size, (digest, original_size) == (key, size)
    except (ValueError, zlib.error, sqlite3.Error):
        return packed_size, False


def _inspect(path):
    """Measure the same original hash and lossless representation as the intake."""
    try:
        before = source_stamp(path.stat())
        facts = probe(path)
        if facts is None:
            return None, "ffprobe could not read an audio stream", None, 0
        with path.open("rb") as stream:
            key, _, packed = _measure(iter(lambda: stream.read(1 << 20), b""))
        if before != source_stamp(path.stat()):
            return None, "File changed during the preview", None, 0
        return facts, "", key, packed
    except (OSError, UnicodeError) as error:
        return None, str(error), None, 0


def preview(config, destination, *, check_audio=False):
    settings = json.loads(config.read_text())
    root = Path(settings["folder"]).expanduser().resolve(strict=True)
    library = Path(settings.get("music_folder") or root / "audio").expanduser().resolve()
    lists = Path(settings.get("lists_folder") or root / "lists").expanduser().resolve()
    state, media = collection_databases(root, config.parent)
    if not state.is_file():
        raise ValueError("The room has no initialized catalog database")
    if check_audio and not shutil.which("ffprobe"):
        raise ValueError("Audio verification requires ffprobe from FFmpeg")

    files = {p.resolve() for p in library.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_FORMATS}
    list_bytes = sum(p.stat().st_size for p in lists.glob("*") if p.is_file() and p.suffix.lower() in SUPPORTED)
    coverage = {}
    issues = []
    blockers = []
    missing = set()
    file_claims = {}
    blob_claims = {}
    stored = {}
    db_bytes = 0

    def block(entry):
        blockers.append(entry)
        issues.append(entry)

    def claim(kind, idx, artist, title, length, value):
        entry = {"kind": kind, "id": idx, "artist": artist, "title": title}
        if is_blob_ref(value):
            blob_claims.setdefault(blob_key(value), []).append((entry, length))
            return
        path = locate_stored(value, library)
        if not path or not path.is_file():
            missing.add(value or "(missing path)")
            block({**entry, "status": "missing_file", "path": value, "reason": "Referenced audio is absent"})
            return
        path = path.resolve()
        files.add(path)
        file_claims.setdefault(path, []).append((entry, length))

    with ExitStack() as stack:
        dbs = {p: stack.enter_context(_read_db(p)) for p in {state, media} if p.is_file()}
        for db in dbs.values():
            db_bytes += db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
        db = dbs[state]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        records = dict(db.execute("SELECT idx,record FROM catalog_records")) if "catalog_records" in tables else {}
        if "coverage" in tables:
            for idx, status, artist, title, path, reason in db.execute(
                    "SELECT idx,status,artist,title,path,reason FROM coverage ORDER BY idx"):
                payload = records.pop(idx, None)
                record = json.loads(payload) if payload is not None else None
                coverage[status] = coverage.get(status, 0) + 1
                if status == "verified":
                    if record is None:
                        block({"kind": "catalog", "id": idx, "artist": artist, "title": title,
                               "status": "missing_catalog_record", "path": path,
                               "reason": "Verified coverage has no matching catalog record"})
                    else:
                        claim("catalog", idx, record.get("artist", artist), record["title"], record["length"], path)
                elif status != "retired" and check_audio:
                    issues.append({"kind": "catalog", "id": idx, "artist": artist, "title": title,
                                   "status": status, "reason": reason or "No verified copy recorded"})
        if check_audio:
            for idx, payload in records.items():
                record = json.loads(payload)
                if record.get("active", True):
                    issues.append({"kind": "catalog", "id": idx, "artist": record["artist"],
                                   "title": record["title"], "status": "no_coverage", "reason": "No coverage row"})
        if "requests" in tables:
            for idx, artist, title, length, path, status, reason in db.execute(
                    "SELECT id,artist,title,length,audio_path,status,message FROM requests ORDER BY id"):
                if status == "downloaded":
                    claim("request", idx, artist, title, length, path)
                elif check_audio:
                    issues.append({"kind": "request", "id": idx, "artist": artist, "title": title,
                                   "status": status, "reason": reason})

        media_db = dbs.get(media)
        media_tables = {r[0] for r in media_db.execute("SELECT name FROM sqlite_master WHERE type='table'")} if media_db else set()
        if "audio" in media_tables:
            packed = "EXISTS (SELECT 1 FROM audio_deflate d WHERE d.hash=a.hash)" if "audio_deflate" in media_tables else "0"
            for key, rowid, size, actual, probe_json, compressed in media_db.execute(
                    f"SELECT hash,rowid,size,length(data),probe,{packed} FROM audio a"):
                claims = blob_claims.pop(key, [])
                if not check_audio:
                    continue
                stored[key], intact = _stored_audio(media_db, key, rowid, size, actual, compressed, state != media)
                facts = _stored_probe(probe_json)
                path = "blob:" + key
                if not intact or facts is None:
                    reason = "Stored audio failed integrity check" if not intact else "Invalid stored audio probe"
                    status = "unreadable" if not intact else "invalid_probe"
                    # Every blob must decode. A probe is required for claiming
                    # audio; an unclaimed row's malformed probe is advisory.
                    report_issue = block if not intact or claims else issues.append
                    for entry, _ in claims or [({"kind": "blob"}, None)]:
                        report_issue({**entry, "status": status, "path": path, "reason": reason})
                    continue
                for entry, length in claims:
                    verdict = judge(entry["title"], length, facts)
                    if not verdict.ok:
                        issues.append({**entry, "status": "rejected", "path": path, "reason": verdict.reason})
        for key, claims in blob_claims.items():
            missing.add("blob:" + key)
            for entry, _ in claims:
                block({**entry, "status": "missing_blob", "path": "blob:" + key, "reason": "Referenced database audio is absent"})

    directory = destination.resolve().parent
    volume = next(path for path in (directory, *directory.parents) if path.is_dir())
    audio_bytes = sum(p.stat().st_size for p in files)
    input_bytes = db_bytes + audio_bytes + list_bytes
    # The second input-sized allowance is headroom for encoding and SQLite
    # transactions. It does not become a second published database.
    needed = 2 * input_bytes + RESERVE
    result = {"library": str(library), "state_database": str(state), "coverage": coverage, "source_files": len(files),
              "file_bytes": audio_bytes, "database_bytes": db_bytes, "list_bytes": list_bytes,
              "source_input_bytes": input_bytes, "temporary_working_space_allowance_bytes": input_bytes,
              "missing_files": sorted(missing), "snapshot_blockers": blockers,
              "reserve_bytes": RESERVE, "required_bytes_including_reserve": needed,
              "available_bytes": shutil.disk_usage(volume).free}
    if check_audio:
        paths = sorted(files)
        unreadable = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            for path, (facts, error, key, packed) in zip(paths, pool.map(_inspect, paths)):
                if error:
                    unreadable.append(str(path))
                else:
                    stored.setdefault(key, packed)
                claims = file_claims.get(path, [])
                if error and not claims:
                    issues.append({"kind": "file", "path": str(path), "status": "unreadable", "reason": error})
                for entry, length in claims:
                    verdict = judge(entry["title"], length, facts)
                    if error or not verdict.ok:
                        issues.append({**entry, "status": "unreadable" if error else "rejected_on_preview",
                                       "path": str(path), "reason": error or verdict.reason})
        result.update(unreadable_files=unreadable, tracks_requiring_attention=issues,
                      distinct_audio_payloads=len(stored), estimated_stored_audio_bytes=sum(stored.values()))
    return result
