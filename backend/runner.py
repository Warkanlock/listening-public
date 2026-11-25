#!/usr/bin/env python3
"""Prepare, run, and audit a resumable offline library from XSPF lists."""

import argparse
import configparser
import csv
import functools
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from catalog import compact_audio
from catalog.audio_resolution import locate_stored, stored_path
from catalog.audio_store import (MediaStore, blob_key, blob_ref,
                                 blob_targets, custom_request_rows, source_stamp,
                                 dumps_export_status,
                                 export_status_record, is_blob_ref,
                                 materialize, playlist_export_folder, portable_folder_name)
from catalog.catalog import bulk_attempted_indices
from catalog.catalog_store import CatalogStore
from catalog.collection_projection import ListProjection, documents, workspace_paths
from catalog.genres import GenreStore, categorize, missing_genre_records
from catalog.offline_replica import add_phone_records
from catalog.lengths import LengthStore
from catalog.media_reconciliation import (BlobIndex,
                                          dumps_migration_status,
                                          migrate_library,
                                          migration_status_record, unmigrated_files)
from downloads.engine_contract import (CandidateTimeout, EngineBusy, EngineCooldown, EngineError, EngineTimeout,
                                       InsufficientDownloadSpace,
                                       engine_recording, engine_selection, fetch_recording, request_recording, simplify_title, timed_stage)
from downloads.requests_store import COPY_REVIEW_MESSAGE, RequestsStore
from downloads.request_state import COPY_UNAVAILABLE_MESSAGE
from downloads.state_store import StateStore
from downloads.verification import (CLI_LENGTH_TOLERANCE, FULL_DECODE_TIMEOUT, MIN_BITRATE_KBPS,
                                    PROBE_CACHE_VERSION, PROBE_TIMEOUT, decode_audio, judge, judge_bulk_download,
                                    judge_download, probe, request_identity, title_is_generic)
from adapters.spotify_sync import playlist_sources
from library.catalog_imports import is_import_list
from library import shared_music
from library.library_protocol import (canonicalize_directory, playlist_identifier, playlist_title,
                                      read_list, xspf_files)
from metadata.external_metadata import ExternalMetadata
from system.config import host_database
from system.config import collection_databases, default_root, resolve_config_path
from system.formats import (AUDIO_FORMATS, BULK_DOWNLOAD_FORMATS, LOSSLESS_DOWNLOAD_FORMATS,
                            REQUEST_FORMAT_NAMES, REQUEST_SUFFIXES, bulk_download_format)
from system.job_queue import JobQueue
from system.normalize import normalize_title
from system.private_files import private_write
from system.refresh import directory_signature

# Legacy song requests search native lossless candidates.
CLI_FORMATS = ",".join(suffix[1:] for suffix in sorted(LOSSLESS_DOWNLOAD_FORMATS))

ROOT = default_root().resolve()
SOURCE_ROOT = Path(os.environ.get("OFFLINE_MUSIC_SOURCES", str(ROOT / "lists"))).expanduser().resolve()
SOURCES = SOURCE_ROOT if SOURCE_ROOT.is_dir() else ROOT
# Working state lives in the application folder (plan 043): the server
# passes it as OFFLINE_MUSIC_STATE; a hand-started runner falls back to
# the folder holding the resolved settings file, as the server does.
STATE = Path(os.environ.get("OFFLINE_MUSIC_STATE",
                            str(resolve_config_path().parent))).expanduser().resolve()
LIBRARY = Path(os.environ.get("OFFLINE_MUSIC_LIBRARY", str(ROOT / "audio"))).expanduser().resolve()
PLAYLISTS = Path(os.environ.get("OFFLINE_MUSIC_PLAYLISTS", str(ROOT / "playlists"))).expanduser().resolve()
WORK = Path(__file__).resolve().parent
OUTPUTS = Path(os.environ.get("OFFLINE_MUSIC_OUTPUTS", str(ROOT / "reports")))
SOCKSEEK = Path(os.environ.get("SOCKSEEK_BINARY", str(WORK.parent / "bin/sockseek"))).expanduser()
CONFIG = Path(os.environ.get("SOCKSEEK_CONFIG", str(Path.home() / ".config/listening-room/sockseek.conf"))).expanduser()
MASTER = STATE / "master.csv"
REQUESTS_DB = collection_databases(ROOT, STATE)[0]
if REQUESTS_DB == ROOT / "collection.sqlite3":
    try:
        saved_settings = json.loads(resolve_config_path().read_text())
    except (FileNotFoundError, ValueError):
        saved_settings = {}
    workspace = workspace_paths(ROOT, STATE)
    if Path(saved_settings.get("folder") or ROOT).expanduser().resolve() == ROOT:
        workspace.update({key: saved_settings[key] for key in workspace if saved_settings.get(key)})
    SOURCE_ROOT = Path(os.environ.get("OFFLINE_MUSIC_SOURCES") or workspace["lists_folder"]).expanduser().resolve()
    SOURCES = ListProjection(REQUESTS_DB, SOURCE_ROOT, STATE).directory
    LIBRARY = Path(os.environ.get("OFFLINE_MUSIC_LIBRARY") or workspace["music_folder"]).expanduser().resolve()
    PLAYLISTS = Path(os.environ.get("OFFLINE_MUSIC_PLAYLISTS") or workspace["playlist_output"]).expanduser().resolve()
    OUTPUTS = Path(os.environ.get("OFFLINE_MUSIC_OUTPUTS") or workspace["reports_folder"]).expanduser().resolve()
MIN_FREE = 20 * 1024**3
# A single Sockseek request may search and download for minutes, but a
# hung process must not freeze the serial request queue (A2 C-03).
REQUEST_TIMEOUT = 600


def catalog_store():
    """The catalog build and coverage store (plan 036)."""
    return CatalogStore(REQUESTS_DB)


# Plan 060: past this much dead space in the media database, report()
# queues the reclaim job (the job hands the space back in bounded
# bursts; nothing here ever rewrites the file).
RECLAIM_THRESHOLD = 256 * 1024**2


def media_store(create=False):
    """The media database (plan 060), or None when it does not exist.

    The database is library content: it lives in the workspace root
    beside the portable lists. create=True is for the landing paths,
    which make it; readers pass False and treat its absence as "no
    blob tracks", exactly the pre-060 world.
    """
    path = collection_databases(ROOT, STATE)[1]
    if not create and not path.is_file():
        return None
    return MediaStore(path)


def audio_store_mode():
    """Portable rooms always own their audio. Legacy hosts can select file landing."""
    if REQUESTS_DB == ROOT / "collection.sqlite3":
        return "database"
    try:
        settings = json.loads(resolve_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "database"
    if isinstance(settings, dict) and settings.get("audio_store") == "files":
        return "files"
    return "database"


def state_store():
    """The cursor and probe-cache store (plan 036)."""
    return StateStore(host_database(REQUESTS_DB, STATE))


def cursor_offset():
    return state_store().cursor_offset()


def write_cursor(offset):
    state_store().write_cursor(offset)


_REPORT_DIRTY = False


def flush_report():
    """Write the full report once, if per-request work marked it dirty."""
    if _REPORT_DIRTY:
        report()


def source_files():
    canonicalize_directory(SOURCES)
    return xspf_files(SOURCES)


def source_signature():
    return {path.name: [path.stat().st_size, path.stat().st_mtime_ns] for path in source_files()}


def audio_signature():
    return shared_music.audio_signature(LIBRARY)


def _assert_index_stability(previous_records, records):
    """The index invariant prepare manufactures, made explicit (plan 045).

    Coverage, requests, the download cursor, and the "<index> - "
    filename convention all trust record indexes, and the walk above
    addresses records positionally. So: indexes are unique, they are
    exactly the contiguous 1..N, and every previous record's key is
    still at its previous index. A violation raises before any write,
    leaving the previous build authoritative; never a silent re-key.
    """
    seen = {}
    for record in records:
        index = record["index"]
        if index in seen:
            raise RuntimeError(
                f"prepare: index {index} claimed by both "
                f"{seen[index]!r} and {record['key']!r}")
        seen[index] = record["key"]
    if sorted(seen) != list(range(1, len(records) + 1)):
        raise RuntimeError("prepare: record indexes are not the "
                           "contiguous 1..N the build requires")
    for record in previous_records:
        if seen.get(record["index"]) != record["key"]:
            raise RuntimeError(
                f"prepare: record {record['key']!r} moved from "
                f"index {record['index']} to "
                f"{seen.get(record['index'])!r}")


def prepare():
    STATE.mkdir(parents=True, exist_ok=True)
    LIBRARY.mkdir(parents=True, exist_ok=True)
    PLAYLISTS.mkdir(parents=True, exist_ok=True)
    projection = None
    original_lists = None
    if REQUESTS_DB == ROOT / "collection.sqlite3":
        projection = ListProjection(REQUESTS_DB, SOURCE_ROOT, STATE)
        original_lists = projection.reconcile()
    previous = catalog_store().read_build() or {}
    records = previous.get("records", [])
    previous_records = list(records)
    by_key = {record["key"]: record["index"] for record in records}
    for record in records:
        record["active"] = False
    playlists = {}
    issues = []
    covers = {}
    list_names = {}
    try:
        mirrors = playlist_sources(json.loads((STATE / "spotify-sync.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        mirrors = {}
    if projection:
        owner_names = projection.playlist_names()
    else:
        try:
            owner_names = json.loads(resolve_config_path().read_text(encoding="utf-8")).get("playlist_names", {})
        except (OSError, ValueError):
            owner_names = {}
    source_info = {}
    imported = set()
    for source in source_files():
        name = source.name
        list_title = playlist_title(source)
        if list_title:
            list_names[source.stem] = list_title
        key = source.stem
        mirror_id, mirror = mirrors.get(key, ("", {}))
        mirrored = bool(mirror_id)
        provider = "spotify:playlist:" + mirror_id if mirrored else playlist_identifier(source)
        name_hint = mirror.get("name", key) if mirrored else key.replace("_", " ")
        source_info[key] = (owner_names.get(key) or list_title or name_hint, provider, mirrored)
        entries = []
        for source_line, row in enumerate(read_list(source), 1):
            if row.get("identifier") and row.get("image"):
                covers.setdefault(row["identifier"], row["image"])
            title = row["title"]
            artist = row["artist"]
            album = row["album"]
            uri = row["identifier"]
            if not title or not artist:
                issues.append({"playlist": name, "line": source_line, "uri": uri,
                               "reason": "missing title or artist"})
                entries.append(None)
                continue
            key = uri or "\0".join((artist.casefold(), title.casefold(), album.casefold()))
            ms = row.get("duration_ms")
            length = round(ms / 1000, 3) if ms is not None else ""
            if key not in by_key:
                index = len(records) + 1
                by_key[key] = index
                records.append({"index": index, "key": key, "uri": uri,
                                "artist": artist, "title": title, "album": album,
                                "length": length, "isrc": row.get("isrc", ""),
                                "first_playlist": name, "active": True})
            else:
                record = records[by_key[key] - 1]
                record.update({"artist": artist, "title": title, "album": album,
                               "length": length, "isrc": row.get("isrc", ""), "active": True})
            entries.append(by_key[key])
        if is_import_list(source):
            imported.update(entries)
        playlists[source.stem] = entries
    for record in records:
        if record["index"] in imported:
            record["manual_copy"] = True
    add_phone_records(records, by_key, media_store())
    # Merge the durable genre store into the catalog build (plan 029):
    # the store in the state database is the source of truth, so the
    # rebuilt catalog projects it faithfully and a rebuild can never
    # erase a categorization.
    store = genre_store()
    for record in records:
        entry = store.get(record["key"])
        if entry:
            record["genre"] = entry["genre"]
            record["genre_canonical"] = entry["canonical"]
            record["genre_source"] = entry["source"]
        else:
            record.pop("genre", None)
            record.pop("genre_canonical", None)
            record.pop("genre_source", None)
    # Apply the durable lengths store (plan 058): a length the list
    # itself gave always wins, so the store only fills a length the
    # list left empty, with the duration a verified match's probe
    # measured. One field of an existing record changes; membership,
    # order, and keys are untouched, so the assertion below is
    # unaffected.
    lengths = length_store()
    for record in records:
        if not record["length"]:
            stored = lengths.get(record["key"])
            if stored is not None:
                record["length"] = stored
    _assert_index_stability(previous_records, records)
    payload = {"records": records, "playlists": playlists, "issues": issues,
               "covers": covers, "list_names": list_names,
               "source_signature": source_signature(),
               "audio_signature": audio_signature()}
    portable_lists = documents(SOURCES) if projection else None
    catalog_store().write_build(payload, sources=source_info, portable_lists=portable_lists, expected_lists=original_lists)
    if projection:
        projection.published(portable_lists)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=["Artist", "Title", "Album", "Length", "Track URI", "ISRC"])
    writer.writeheader()
    for record in records:
        writer.writerow({"Artist": record["artist"], "Title": record["title"],
                         "Album": record["album"], "Length": record["length"],
                         "Track URI": record["uri"], "ISRC": record["isrc"]})
    private_write(MASTER, buffer.getvalue())
    print(json.dumps({"unique_tracks": len(records), "playlists": {k: len(v) for k, v in playlists.items()},
                      "metadata_issues": issues}, ensure_ascii=False))


def load_manifest():
    """Read the catalog build, rebuilding it from the source lists if
    no build exists yet instead of crashing (A2 C-02)."""
    data = catalog_store().read_build()
    if data is None or "records" not in data:
        print("Catalog build is missing; rebuilding it from the source lists",
              flush=True)
        prepare()
        data = catalog_store().read_build()
    return data


def setup_credentials():
    if CONFIG.exists():
        return
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path.home() / ".config/nicotine/config")
    user = parser.get("server", "login", fallback="").strip()
    password = parser.get("server", "passw", fallback="").strip()
    if not user or not password:
        raise RuntimeError("Soulseek credentials are missing; add them in Listening Room setup")
    private_write(CONFIG, f"username = {user}\npassword = {password}\n", exclusive=True)
    print("Created private Sockseek config from Nicotine+ settings")


def free_bytes():
    volumes = (ROOT, STATE, LIBRARY)
    return min(shutil.disk_usage(path).free for path in volumes if path.is_dir())


def estimated_bytes(records):
    seconds = sum(float(record["length"] or 240) for record in records)
    # Estimate CD-quality stereo PCM before compression; the running
    # space guard also covers higher-resolution transfers.
    return int(seconds * 44_100 * 2 * 2 * 1.2 + 150 * 1024**2)


def requests_store():
    """The shared requests database, owned by downloads.requests_store."""
    return RequestsStore(REQUESTS_DB)


def genre_store():
    """The durable genre store (plan 029), in the state database."""
    return GenreStore(REQUESTS_DB)


def length_store():
    """The durable lengths store (plan 058), in the state database."""
    return LengthStore(REQUESTS_DB)


def _backfill_length(record, media, lengths):
    """Plan 058: an acceptance just measured this record's real
    duration and the list never gave one. Keep the measurement in
    the lengths store; the next prepare fills the record's length
    from it. A record carrying a list length is never written.
    Both acceptance paths (a library file, a blob's stored probe)
    backfill through here."""
    if not record["length"] and media.get("duration"):
        lengths.set(record["key"], media["duration"])


def external_metadata():
    """The external metadata module, cached through the requests store."""
    return ExternalMetadata(lambda: RequestsStore(host_database(REQUESTS_DB, STATE)))


def request_connection():
    """A raw connection to the requests database, for tests and tooling.

    The schema lives in the store (plan 005); the pipeline itself goes
    through the store's operations, never through this connection.
    """
    return requests_store().connect()


_probe_cache = None
_probe_cache_dirty = False


def load_probe_cache():
    global _probe_cache
    if _probe_cache is None:
        _probe_cache = state_store().probes()
    return _probe_cache


def cached_probe(path, *, local_audio_only=False):
    """probe() through the stamp-keyed cache report() maintains (A3 PERF-03).

    The cache is loaded once per runner invocation and shared by
    verified_existing and report(); an entry is reused only while the
    file's size and mtime are unchanged.
    """
    global _probe_cache_dirty
    # Negative versions distinguish restricted probes in the existing SQLite cache.
    version = -PROBE_CACHE_VERSION if local_audio_only else PROBE_CACHE_VERSION

    def read_media():
        return probe(path, local_audio_only=True) if local_audio_only else probe(path)

    try:
        stat = path.stat()
    except OSError:
        return read_media()
    cache = load_probe_cache()
    key = str(path)
    stamp = [stat.st_size, stat.st_mtime_ns]
    entry = cache.get(key)
    # The cache shape version lives with probe() (PROBE_CACHE_VERSION
    # in downloads.verification): older entries re-probe once so the
    # fields a newer probe captures are not missed.
    if not entry or entry.get("stamp") != stamp \
            or entry.get("version") != version:
        entry = {"stamp": stamp, "version": version, "media": read_media()}
        cache[key] = entry
        _probe_cache_dirty = True
    return entry["media"]


def save_probe_cache():
    global _probe_cache_dirty
    if _probe_cache is not None and _probe_cache_dirty:
        state_store().put_probes(_probe_cache)
        _probe_cache_dirty = False


@functools.lru_cache(maxsize=1)
def _blob_index(database, stamp):
    """The verified blob rows as one BlobIndex (plan 061 step 2),
    behind the stamp-keyed cache _blob_rows carried: the index is
    rebuilt only while the coverage stamp moves.

    The row's path is the blob reference itself; the bytes live in
    the media database, so existence is judged against the stored
    probe there, never against the folder.
    """
    return BlobIndex(media_store(),
                     CatalogStore(database).read_coverage())


def verified_existing(index, record):
    store = media_store()
    if store is not None:
        blob = _blob_index(
            str(REQUESTS_DB), catalog_store().coverage_stamp()).get(index)
        if blob is not None:
            reference, facts = blob
            if judge(record["title"], record["length"], facts).ok:
                return Path(reference)
    for path in LIBRARY.glob(f"{index} - *"):
        if not path.is_file() or path.suffix.lower() not in AUDIO_FORMATS:
            continue
        media = cached_probe(path)
        if judge(record["title"], record["length"], media).ok:
            return path
    store = catalog_store()
    candidate = _coverage_paths(str(REQUESTS_DB),
                                store.coverage_stamp()).get(index)
    if candidate and candidate.is_file():
        media = cached_probe(candidate)
        if judge(record["title"], record["length"], media).ok:
            return candidate
    return None


@functools.lru_cache(maxsize=1)
def _coverage_paths(database, stamp):
    paths = {}
    for row in CatalogStore(database).read_coverage():
        if row["status"] == "verified" and row["path"]:
            located = locate_stored(row["path"], LIBRARY)
            if located is not None:
                paths[row["index"]] = located
    return paths


def request_failure(output, returncode):
    if "OutOfMemoryException" in output or "Out of memory" in output:
        return "error", "Sockseek ran out of memory during search; retry this song"
    if "Too many open files" in output:
        return "error", "Sockseek exhausted its open files during download; retry this song"
    if "Can't assign requested address" in output or "Cannot assign requested address" in output:
        return "error", "Sockseek exhausted network connections during search; retry this song"
    if "All downloads failed" in output:
        return "error", "Soulseek found copies, but peers did not complete the transfer; retry this song"
    if returncode:
        return "error", "Sockseek stopped before returning a usable file; check the CLI log and retry"
    return "not_found", "No verified lossless match found; try another search title"


def focused_matches(groups, title, length, requested_format="any_lossless"):
    matches = []
    allowed = REQUEST_SUFFIXES[requested_format]
    if not isinstance(groups, list):
        return matches
    for group in groups:
        if not isinstance(group, dict):
            continue
        user = group.get("User") or {}
        username = user.get("Username")
        if not username:
            continue
        # Sockseek 3 emits one File per song result, or Files for an album result.
        song_file = group.get("File")
        files = [song_file] if isinstance(song_file, dict) else group.get("Files") or []
        for item in files:
            filename = item.get("Filename") or ""
            if not filename.lower().endswith(allowed):
                continue
            if song_file is None:
                # Legacy album results need a track match within the album.
                basename = Path(filename.replace("\\", "/")).stem
                listing = {"bitrate": int(item.get("Bitrate") or 0) * 1000,
                           "duration": float(item.get("Length") or 0), "title": basename}
                if not judge(title, length, listing).ok:
                    continue
            rank = (filename.lower().endswith(allowed[0]), bool(user.get("HasFreeUploadSlot")),
                    float(user.get("UploadSpeed") or 0), int(item.get("Bitrate") or 0))
            matches.append((rank, username, filename))
    matches.sort(reverse=True)
    return matches


def staging_snapshot(staging):
    """Map each file under staging to its (size, mtime) stamp."""
    snapshot = {}
    for path in staging.rglob("*"):
        if path.is_file():
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[path] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def produced_files(staging, before):
    """Files a download produced: new under staging, or changed since
    the `before` snapshot. Files already sitting in staging are not
    evidence that this download delivered anything (A2 S-02)."""
    produced = []
    for path in staging.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if before.get(path) != (stat.st_size, stat.st_mtime_ns):
            produced.append(path)
    return produced


def transfer_with_reserve(command, log, timeout):
    """Bound a transfer and reap Sockseek before its private staging folder goes away."""
    proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if free_bytes() < MIN_FREE:
                raise InsufficientDownloadSpace
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(1)
        return proc.returncode
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def focused_download(subject, title, length, staging, log_path, *, requested_format="any_lossless",
                     expected_title=None, recording_id="", version="", expected_artist="", performance=""):
    # Confirmed requests search once, then inspect the matching peers in preference order.
    individual = expected_title is not None
    if individual:
        query = f"{subject} - {title}" if subject else title
    else:
        album = re.sub(r"\s*\([^)]*\)", "", subject).strip()
        if not album or not title:
            return False
        query = f"{album} {title}"
    formats = [ext[1:] for ext in REQUEST_SUFFIXES[requested_format]]
    command = [str(SOCKSEEK), query, "--config", str(CONFIG), "--print", "json-all",
               "--format", ",".join(formats), "--pref-format", formats[0],
               "--search-timeout", "6000", "--no-progress"]
    if individual:
        # Lossless peers can advertise a zero/absent bitrate. Judge their bytes with ffprobe.
        command.extend(["--input-type", "string", "--song"])
    else:
        command.extend(["--min-bitrate", str(MIN_BITRATE_KBPS)])
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=18)
        if individual and result.returncode:
            raise RuntimeError(request_failure(result.stderr, result.returncode)[1])
        groups = json.loads(result.stdout)
    except subprocess.TimeoutExpired as error:
        if individual:
            raise EngineTimeout("The search timed out.") from error
        groups = []
    except (OSError, ValueError) as error:
        if individual:
            raise RuntimeError("Sockseek could not complete the search; retry this song") from error
        groups = []
    matches = focused_matches(groups, title, length, requested_format)
    if not individual:
        matches = matches[:3]
    deadline = time.monotonic() + REQUEST_TIMEOUT
    if not matches:
        return False
    transfer_timed_out = False
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"Focused search: {query}; {len(matches)} candidate peers\n")
        log.flush()
        for _, username, filename in matches:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if individual:
                    raise EngineTimeout("The search reached the time limit for this recording.")
                raise RuntimeError("Sockseek timed out while checking candidate copies")
            if individual and free_bytes() < MIN_FREE:
                raise InsufficientDownloadSpace
            remote_path = filename.replace("\\", "/")
            link = f"slsk://{quote(username, safe='')}/{quote(remote_path, safe='/')}"
            direct = [str(SOCKSEEK), link, "--config", str(CONFIG),
                      "--output-dir", str(staging), "--max-retries", "2",
                      "--max-stale-time", "30000", "--no-progress"]
            before = staging_snapshot(staging)
            try:
                if individual:
                    returncode = transfer_with_reserve(direct, log, min(90, remaining))
                else:
                    returncode = subprocess.run(direct, stdout=log, stderr=subprocess.STDOUT, timeout=90).returncode
            except subprocess.TimeoutExpired:
                transfer_timed_out = True
                continue
            except OSError:
                continue
            # Only a file this candidate's download produced can answer
            # the request, and only after verification judges it (A2
            # S-02). Anything else is left in staging and the search
            # continues, exactly as a rejected verification does.
            for path in produced_files(staging, before):
                if (returncode == 0 and not path.is_symlink()
                        and path.suffix.lower() in REQUEST_SUFFIXES[requested_format]
                        and judge_download(expected_title or title, length, probe(path),
                                           path.suffix, requested_format, recording_id=recording_id, version=version,
                                           expected_artist=expected_artist, performance=performance).ok):
                    return path
                if individual:
                    # This private staging directory belongs to this search alone.
                    path.unlink(missing_ok=True)
    if individual and transfer_timed_out:
        raise CandidateTimeout("The download timed out while transferring a copy.")
    return False


def remove_staged_duplicate(path, staging, store, key=None, stamp=None):
    """Remove only a stable, byte-identical copy in a transfer directory."""
    try:
        entry = path.lstat()
        if (not stat.S_ISREG(entry.st_mode) or
                path.resolve() != staging.parent.resolve() / staging.name / path.relative_to(staging)):
            return False
        current = source_stamp(entry)
        if stamp is not None and stamp != current:
            return False
        if store.matches_original(path, key) and source_stamp(path.lstat()) == current:
            path.unlink()
            return True
    except OSError:
        pass
    return False


def request_pending_dir(request_dir, request_id, revision=None):
    pending = request_dir / "verified" / str(request_id)
    return pending / f"copy-{revision}" if revision is not None else pending


def release_request_duplicates(request_id, audio_path, store, revision=None):
    pending = request_pending_dir(STATE / "requests", request_id, revision)
    for candidate in pending.glob("incoming-*"):
        remove_staged_duplicate(candidate, STATE / "requests", store, blob_key(audio_path))


def reconcile_request_staging(downloaded, store):
    references = {str(row["id"]): row["audio_path"] for row in downloaded if is_blob_ref(row["audio_path"])}
    root = STATE / "requests"
    for directory in (root / "verified").glob("*"):
        if directory.name in references:
            release_request_duplicates(directory.name, references[directory.name], store)
    for directory in root.glob("incoming-*-*"):
        match = re.fullmatch(r"incoming-(\d+)-.+", directory.name)
        if not match or match[1] not in references or directory.is_symlink():
            continue
        for path in directory.rglob("*"):
            if path.suffix.lower() in AUDIO_FORMATS:
                remove_staged_duplicate(path, root, store, blob_key(references[match[1]]))


def reconcile_bulk_staging(directory, store, protected):
    if store is None or directory.is_symlink():
        return
    for path in directory.rglob("*"):
        if path not in protected and path.suffix.lower() in AUDIO_FORMATS:
            remove_staged_duplicate(path, directory, store)


def bulk_staging_has_audio():
    return any(path.suffix.lower() in AUDIO_FORMATS and path.is_file()
               for path in (STATE / "bulk-incoming").rglob("*"))


def configured_engines():
    try:
        settings = json.loads(resolve_config_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise EngineError("The room settings could not be read.") from error
    if not isinstance(settings, dict):
        raise EngineError("The room settings could not be read.")
    return engine_selection(settings)


def download_job_progress():
    job_id = os.environ.get("LISTENING_DOWNLOAD_JOB_ID")
    if not job_id:
        return None, None
    queue = JobQueue(host_database(REQUESTS_DB, STATE))
    return queue, queue.get(int(job_id))


def record_download_progress(*, retry=None, retry_delay=0, scoped_attempted=(), complete=False):
    queue, job = download_job_progress()
    if not job:
        return
    progress = job["progress"]
    if retry is not None:
        progress["engine_retry"] = retry
        if retry_delay:
            progress["retry_after"] = time.time() + retry_delay
    if complete and not progress.get("engine_retry"):
        progress["engine_complete"] = True
    if scoped_attempted:
        progress["scoped_attempted"] = sorted(set(progress.get("scoped_attempted", ())) | set(scoped_attempted))
    queue.record_progress(job["id"], progress)


def verified_engine_audio(path, judge_candidate, remaining_time):
    facts = probe(path, local_audio_only=True, timeout=min(PROBE_TIMEOUT, remaining_time()))
    if facts is not None and not title_is_generic(facts["title"]) and judge_candidate(facts).ok:
        return facts
    return None


def managed_candidate(selection, recording, accept, *, chosen=None, recommended_only=False, on_review=None,
                      on_transfer=None, on_progress=None):
    return fetch_recording(selection, recording, state=STATE, free_bytes=free_bytes, reserve=MIN_FREE,
                           estimate=estimated_bytes([{"length": recording["duration"]}]),
                           accept=accept, timeout=REQUEST_TIMEOUT, chosen=chosen, recommended_only=recommended_only,
                           on_review=on_review, on_transfer=on_transfer, on_progress=on_progress)


def managed_request(selection, request_dir, row, record, title, length, selected):
    choice = row.get("requested_format", "any_lossless")
    recording = request_recording(row, record, choice)
    if selected is not None and (selected["selection"] != selection or selected["recording"] != recording):
        return "error", "The request or its sources changed. Browse copies again.", ""
    store = requests_store()
    review = []
    attempted_verification = False
    transfer_phase = "downloading" if selected else "testing"

    def save_review(candidates):
        review.extend(store.save_copy_matches(
            row["id"], choice, row["format_revision"], recording, selection, candidates, for_review=True))

    def accept(path, remaining_time):
        nonlocal attempted_verification
        attempted_verification = True
        store.set_phase(row["id"], "verifying")
        facts = verified_engine_audio(path, lambda facts: judge_download(
            title, length, facts, path.suffix, choice, **request_identity(row)), remaining_time)
        if facts is None:
            return False
        try:
            budget = min(FULL_DECODE_TIMEOUT, remaining_time())
            with timed_stage(selection["fetch"], "full decode", budget):
                decode_audio(path, probed_duration=facts["duration"], expected_duration=length, timeout=budget)
        except (subprocess.CalledProcessError, ValueError):
            return False
        except subprocess.TimeoutExpired as error:
            raise CandidateTimeout("Verifying the download timed out while decoding the audio.") from error
        except OSError as error:
            raise EngineError("The room could not start the audio decoder.") from error
        return True

    try:
        with managed_candidate(selection, recording, accept, chosen=selected["candidate"] if selected else None,
                               recommended_only=bool(row.get("format_confirmed")), on_review=save_review,
                               on_transfer=lambda: store.set_phase(row["id"], transfer_phase),
                               on_progress=lambda percent: store.set_phase(row["id"], transfer_phase, percent)) as path:
            if path is None:
                if review:
                    return "rejected", COPY_REVIEW_MESSAGE, ""
                if selected:
                    return "rejected", COPY_UNAVAILABLE_MESSAGE, ""
                if attempted_verification:
                    return "rejected", "The copies found did not pass audio verification.", ""
                return "not_found", "No copy found", ""
            return retain_request_file(path, path.parent, request_dir, row, record, title, length,
                                       selected=bool(selected))
    except InsufficientDownloadSpace:
        return "waiting_space", "Waiting for more free space", ""


def run_request(row, record):
    request_id = row["id"]
    artist = record["artist"] if record else row["artist"]
    title = record["title"] if record else row["title"]
    search_title = row["search_title"] or simplify_title(title)
    length = record["length"] if record else row["length"]
    request_dir = STATE / "requests"
    choice = row.get("requested_format", "any_lossless")
    identity = request_identity(row)
    selected = requests_store().selected_copy(request_id, row["format_revision"]) if "format_revision" in row else None
    revision = row["format_revision"] if selected else None
    pending = request_pending_dir(request_dir, request_id, revision)
    # Recover a committed request or release its duplicate even at the space floor.
    # A different retained candidate still needs its original verification and handoff.
    landed = ""
    if audio_store_mode() == "database":
        store = media_store()
        owner = ("catalog", record["key"]) if record else ("request", str(request_id))
        reference = store.owned_audio(*owner) if store is not None else None
        if reference and (not selected or reference == selected.get("audio_path")):
            key = blob_key(reference)
            suffix = store.suffix(key)
            if (judge_download(title, length, store.probe(key), suffix, choice, **identity).ok
                    and store.verifies_original(key, suffix)):
                landed = reference
                release_request_duplicates(request_id, landed, store, revision)
    elif selected and (reference := selected.get("audio_path")):
        path = LIBRARY / reference
        if path.is_file() and judge_download(title, length, probe(path), path.suffix, choice, **identity).ok:
            landed = reference
    for candidate in sorted(pending.glob("incoming-*")):
        if (candidate.is_file() and not candidate.is_symlink()
                and judge_download(title, length, probe(candidate), candidate.suffix, choice, **identity).ok):
            return land_request_file(candidate, row, record, title, length, selected=bool(selected))
    if landed:
        return "downloaded", "Ready to play", landed
    if free_bytes() - estimated_bytes([{"length": length}]) < MIN_FREE:
        return "waiting_space", "Waiting for more free space", ""
    request_dir.mkdir(exist_ok=True)
    selection = configured_engines()
    if selection is not None:
        return managed_request(selection, request_dir, row, record, title, length, selected)
    if selected:
        return "error", "The request or its sources changed. Browse copies again.", ""
    check_legacy_download_tools()
    if row.get("format_confirmed"):
        log_path = request_dir / f"request-{request_id}.log"
        with tempfile.TemporaryDirectory(prefix=f"incoming-{request_id}-", dir=request_dir) as temp:
            try:
                path = focused_download(artist, search_title, length, Path(temp), log_path,
                                        requested_format=choice, expected_title=title, **identity)
            except InsufficientDownloadSpace:
                return "waiting_space", "Waiting for more free space", ""
            if not path:
                return "not_found", f"No verified {REQUEST_FORMAT_NAMES[choice]} copy found", ""
            return retain_request_file(path, Path(temp), request_dir, row, record, title, length)
    # Pre-upgrade rows and bulk requests keep the album search and CSV fallback.
    csv_path = request_dir / f"request-{request_id}.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Artist", "Title", "Album", "Length"])
        writer.writeheader()
        writer.writerow({"Artist": artist, "Title": search_title,
                         "Album": record["album"] if record else row["album"],
                         "Length": length or ""})
    with tempfile.TemporaryDirectory(prefix=f"incoming-{request_id}-", dir=request_dir) as temporary:
        return download_request_candidate(Path(temporary), request_dir, csv_path, row, record, title,
                                          search_title, length)


def download_request_candidate(staging, request_dir, csv_path, row, record, title, search_title, length):
    request_id = row["id"]
    log_path = request_dir / f"request-{request_id}.log"
    log_path.write_text("", encoding="utf-8")
    focused = focused_download(record["album"] if record else row["album"],
                               search_title, length, staging, log_path)
    command = [str(SOCKSEEK), str(csv_path), "--config", str(CONFIG),
               "--output-dir", str(staging), "--index-path", str(request_dir / f"index-{request_id}.csv"),
               "--format", CLI_FORMATS, "--pref-format", "flac", "--min-bitrate", str(MIN_BITRATE_KBPS),
               "--pref-min-bitrate", "256",
               "--name-format", "{sartist} - {stitle}",
               "--artist-maybe-wrong", "--desperate", "--fast-search", "--pref-strict-title",
               "--pref-strict-artist", "--concurrent-jobs", "1", "--concurrent-searches", "1",
               "--max-retries", "3", "--max-stale-time", "15000", "--no-progress"]
    if length:
        command += ["--length-tol", str(CLI_LENGTH_TOLERANCE)]
    returncode = 0
    if not focused:
        with log_path.open("a", encoding="utf-8") as log:
            try:
                returncode = transfer_with_reserve(command, log, REQUEST_TIMEOUT)
            except InsufficientDownloadSpace:
                return "waiting_space", "Stopped at the 20 GiB free space reserve", ""
            except subprocess.TimeoutExpired as error:
                raise EngineTimeout("The download engine timed out during the request.") from error
    candidates = []
    for path in staging.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in AUDIO_FORMATS:
            continue
        stamp = source_stamp(path.stat())
        media = probe(path)
        if source_stamp(path.stat()) != stamp or not judge_download(title, length, media, path.suffix).ok:
            continue
        candidates.append((media["bitrate"], path))
    if not candidates:
        output = log_path.read_text(encoding="utf-8", errors="replace")
        status, message = request_failure(output, returncode)
        return status, message, ""
    _, path = max(candidates, key=lambda item: item[0])
    return retain_request_file(path, staging, request_dir, row, record, title, length)


def retain_request_file(path, staging, request_dir, row, record, title, length, *, selected=False):
    request_id = row["id"]
    pending = request_pending_dir(request_dir, request_id, row["format_revision"] if selected else None)
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged_name = staging.name
    if not staged_name.startswith("incoming-"):
        staged_name = f"incoming-{request_id}-{staged_name}"
    retained = pending / (staged_name + path.suffix.lower())
    path.rename(retained)
    return land_request_file(retained, row, record, title, length, selected=selected)


def land_request_file(path, row, record, title, length, *, selected=False):
    request_id = row["id"]
    if free_bytes() - 3 * path.stat().st_size < MIN_FREE:
        return "waiting_space", "Verified download retained until more space is available", ""
    stamp = source_stamp(path.stat())
    facts = probe(path)
    verdict = judge_download(title, length, facts, path.suffix, row.get("requested_format", "any_lossless"),
                             **request_identity(row))
    if not verdict.ok or source_stamp(path.stat()) != stamp:
        return "error", verdict.reason or "Audio changed during verification", ""
    if audio_store_mode() == "database":
        owner = ("catalog", record["key"]) if record else ("request", str(request_id))
        store = media_store(create=True)
        key = store.put_file(path, facts, owners=(owner,), expected_stamp=stamp)
        reference = blob_ref(key)
        if selected:
            requests_store().remember_selected_audio(request_id, row["format_revision"], reference)
        if not remove_staged_duplicate(path, STATE / "requests", store, key, stamp):
            if path.exists():
                raise OSError("Could not release the verified transfer. Its staged copy was kept.")
        release_request_duplicates(request_id, reference, store, row["format_revision"] if selected else None)
        return "downloaded", "Ready to play", reference
    if record:
        target = LIBRARY / f"{record['index']} - requested-{request_id}{path.suffix.lower()}"
    else:
        custom_dir = LIBRARY / "custom"
        custom_dir.mkdir(exist_ok=True)
        target = custom_dir / f"{request_id}{path.suffix.lower()}"
    saved = publish_verified_download(path, target, stamp)
    reference = stored_path(saved, LIBRARY)
    if selected:
        requests_store().remember_selected_audio(request_id, row["format_revision"], reference)
    if source_stamp(path.stat()) == stamp:
        path.unlink()
    return "downloaded", "Ready to play", reference


def publish_verified_download(source, target, stamp):
    """Confirm the verified version before publishing a file-landing download."""
    completed = []
    materialize(None, [(source, target.name)], target.parent, completed=completed,
                preserve_existing=True, source_stamps={source: stamp}, raise_errors=True)
    return target.parent / completed[0][1]


def process_requests(records, limit=3):
    global _REPORT_DIRTY
    store = requests_store()
    for _ in range(limit):
        due = store.queued(1)
        if not due:
            break
        request_id = due[0]["id"]
        if not store.claim(request_id):
            continue
        row = store.get(request_id)
        record = None
        provider_delay = 0
        retryable = False
        try:
            if row["track_index"]:
                record = records[row["track_index"] - 1]
            existing = (verified_existing(record["index"], record) if record
                        and not store.selected_copy(request_id, row["format_revision"]) else None)
            if is_blob_ref(str(existing)):
                store_key = blob_key(str(existing))
                media = media_store()
                if not media.verifies_original(store_key):
                    existing = None
            if existing and row["format_confirmed"]:
                if is_blob_ref(str(existing)):
                    facts, suffix = media.probe(store_key), media.suffix(store_key)
                else:
                    facts, suffix = cached_probe(existing), existing.suffix
                if not judge_download(record["title"], record["length"], facts,
                                      suffix, row["requested_format"]).ok:
                    existing = None
            if existing:
                # A blob-backed copy's stored form is its reference;
                # a file's is its library-relative path (plan 060).
                audio_path = (str(existing) if is_blob_ref(str(existing))
                              else stored_path(existing, LIBRARY))
                if is_blob_ref(audio_path):
                    release_request_duplicates(request_id, audio_path, media_store())
                status, message, audio_path = "downloaded", "Already in the library", audio_path
            else:
                status, message, audio_path = run_request(row, record)
        except EngineError as error:
            status, message, audio_path = "error", str(error), ""
            retryable = error.retryable
            if isinstance(error, EngineCooldown):
                provider_delay = error.seconds
            elif isinstance(error, EngineBusy):
                provider_delay = 15
        except Exception as error:
            status, message, audio_path = "error", str(error)[:300], ""
        if record and row["format_confirmed"] and status == "downloaded":
            # Publish the verified replacement before the report consults older coverage.
            catalog_store().update_coverage_path(record["index"], audio_path)
        if retryable:
            status = store.defer_attempt(row, message, provider_delay=provider_delay)
        else:
            store.finish(request_id, status, message, audio_path)
        print(f"Request {request_id}: {status}", flush=True)
        # Per-request work only marks the report dirty (A3 PERF-02);
        # the full report is written once, after the loop.
        _REPORT_DIRTY = True
        if provider_delay or (status == "waiting_space" and configured_engines() is not None):
            record_download_progress(retry=message, retry_delay=provider_delay)
            save_probe_cache()
            flush_report()
            return False
    save_probe_cache()
    flush_report()


def run_batch(offset, count, pilot=False, advance_cursor=True):
    # Keep original master offsets (and therefore filenames and cursor positions)
    # while leaving individual requests to the format-aware worker.
    records = load_manifest()["records"]
    reserved = requests_store().reserved_tracks(offset + 1, offset + count)
    reserved.update(row["index"] for row in records[offset:offset + count] if row.get("manual_copy"))
    if not reserved:
        return download_batch(offset, count, pilot, advance_cursor)
    end = min(offset + count, len(records))
    for index in sorted(reserved):
        if index > end:
            break
        if index - 1 > offset and not download_batch(offset, index - 1 - offset, pilot, advance_cursor):
            return False
        offset = index
        if advance_cursor:
            write_cursor(offset)
    return offset >= end or download_batch(offset, end - offset, pilot, advance_cursor)


def bulk_format_options():
    override = os.environ.get("OFFLINE_MUSIC_DOWNLOAD_FORMAT")
    if override is not None:
        mode = bulk_download_format(override)
    else:
        try:
            settings = json.loads(resolve_config_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
        mode = bulk_download_format(settings.get("bulk_download_format") if isinstance(settings, dict) else None)
    suffixes, preferred = BULK_DOWNLOAD_FORMATS[mode]
    return ["--format", ",".join(suffix[1:] for suffix in suffixes), "--pref-format", preferred]


def batch_log_complete(log, expected, exit_code):
    # Sockseek uses 1 for ordinary failed searches as well as infrastructure failures.
    if exit_code not in (0, 1):
        return False
    summary_pattern = r"^(?:\[cli\] )?Completed:[ \t]*(\d+) succeeded,[ \t]*(\d+) failed\.?[ \t]*$"
    summary = re.search(summary_pattern, log, re.MULTILINE)
    if summary is not None:
        succeeded, failed = map(int, summary.groups())
        return succeeded + failed == expected and (exit_code == 0 or failed > 0)
    # Sockseek 3 may log only terminal results. Retry warnings and repeated
    # lines for the same job do not count as additional tracks.
    pattern = r"^\[(\d+)\] SongJob: (succeeded|failed \[[^\]\r\n]+\]):"
    terminal = dict(re.findall(pattern, log, re.MULTILINE))
    return len(terminal) == expected and (
        exit_code == 0 or any(result.startswith("failed") for result in terminal.values())
    )


def check_legacy_download_tools():
    setup_credentials()
    if not SOCKSEEK.is_file():
        raise RuntimeError("Sockseek is missing; set its executable path in Settings")


def managed_batch(selection, offset, batch, landing, advance_cursor):
    _, suffixes, _, preferred = bulk_format_options()
    formats = tuple("." + suffix for suffix in suffixes.split(","))
    attempted = []
    for record in batch:
        recording = engine_recording(record, simplify_title(record["title"]), formats, preferred)
        try:
            with managed_candidate(selection, recording, lambda path, remaining_time: verified_engine_audio(
                path, lambda facts: judge_bulk_download(record["title"], record["length"], facts, path.suffix),
                remaining_time,
            )) as path:
                if path is not None:
                    staging_id = path.parent.name
                    target = landing / f"{record['index']} - {staging_id}{path.suffix}"
                    path.replace(target)
        except InsufficientDownloadSpace:
            message = "Waiting for more free space to download music."
            record_download_progress(retry=message, scoped_attempted=attempted)
            print(message, flush=True)
            return False
        except EngineError as error:
            record_download_progress(retry=str(error), scoped_attempted=attempted)
            print(str(error), flush=True)
            return False
        if advance_cursor:
            offset += 1
            write_cursor(offset)
        else:
            attempted.append(record["key"])
    if attempted:
        record_download_progress(scoped_attempted=attempted)
    return True


def download_batch(offset, count, pilot, advance_cursor):
    selection = configured_engines()
    if selection is None:
        check_legacy_download_tools()
    data = load_manifest()
    batch = data["records"][offset:offset + count]
    if not batch:
        return False
    landing = STATE / "bulk-incoming"
    global _REPORT_DIRTY
    if selection is not None:
        _REPORT_DIRTY = True
        landing.mkdir(parents=True, exist_ok=True)
        return managed_batch(selection, offset, batch, landing, advance_cursor)
    free = free_bytes()
    estimate = estimated_bytes(batch)
    if free - estimate < MIN_FREE and bulk_staging_has_audio():
        report()
        free = free_bytes()
    if free - estimate < MIN_FREE:
        print(f"Space floor reached before batch: free={free / 1024**3:.2f} GiB, "
              f"estimated_batch={estimate / 1024**3:.2f} GiB", flush=True)
        return False
    batch_index = STATE / f"index-{offset + 1:05d}-{offset + len(batch):05d}.csv"
    _REPORT_DIRTY = True
    landing.mkdir(parents=True, exist_ok=True)
    # A previous index can suppress a song before Sockseek logs any result.
    # The library and staged audio already handle resuming verified work.
    with tempfile.TemporaryDirectory(prefix="sockseek-batch-", dir=STATE) as attempt:
        attempt_index = Path(attempt) / batch_index.name
        command = [str(SOCKSEEK), str(MASTER), "--config", str(CONFIG),
                   "--output-dir", str(landing), "--index-path", str(attempt_index),
                   "--offset", str(offset), "--number", str(len(batch)),
                   *bulk_format_options(), "--min-bitrate", str(MIN_BITRATE_KBPS),
                   "--pref-min-bitrate", "256", "--length-tol", str(CLI_LENGTH_TOLERANCE),
                   "--pref-strict-title", "--pref-strict-artist",
                   "--name-format", "{snum} - {sartist} - {stitle}",
                   "--concurrent-jobs", "3", "--concurrent-searches", "1",
                   "--max-retries", "3", "--max-stale-time", "15000",
                   "--no-progress"]
        print(f"Starting tracks {offset + 1}–{offset + len(batch)}; "
              f"free={free / 1024**3:.2f} GiB", flush=True)
        log_path = STATE / ("pilot.log" if pilot else f"batch-{offset + 1:05d}-{offset + len(batch):05d}.log")
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"=== tracks {offset + 1}–{offset + len(batch)} ===\n")
            log.flush()
            proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            while proc.poll() is None:
                if free_bytes() < MIN_FREE:
                    print("Space floor reached during batch; stopping Sockseek", flush=True)
                    proc.send_signal(signal.SIGINT)
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                    return False
                time.sleep(5)
        if attempt_index.is_file():
            attempt_index.replace(batch_index)
    print(f"Batch exit={proc.returncode}; free={free_bytes() / 1024**3:.2f} GiB", flush=True)
    if not batch_log_complete(log_path.read_text(encoding="utf-8"), len(batch), proc.returncode):
        return False
    if advance_cursor:
        write_cursor(offset + len(batch))
    return True


def run_scoped(scope, batch_size=50):
    """The bulk download restricted to one list's records (plan 040).

    The scope is data, not a session: the same `run` action with
    OFFLINE_MUSIC_SCOPE naming a playlist key, started by the jobs
    layer in the one bulk session. The member set is the manifest's
    own playlist map, the catalog's membership record, so the filter
    can never disagree with the library the server shows.

    The global download cursor belongs to the unscoped pass and is
    never moved here (batches run with advance_cursor=False): an
    interrupted scoped run leaves the main pass exactly where it
    was, and verified_existing makes a rerun resume by skipping
    what already landed. Offsets attempted in this run are
    remembered for the same reason the cursor exists in the main
    pass: a batch whose songs all fail still counts as attempted,
    so the loop terminates instead of retrying it forever.
    """
    if not catalog_store().has_build():
        prepare()
    if bulk_staging_has_audio():
        report()
    if configured_engines() is None:
        check_legacy_download_tools()
    data = load_manifest()
    records = data["records"]
    signature = data.get("source_signature")
    requests_store().reset_searching()
    _, job = download_job_progress()
    attempted = set(job["progress"].get("scoped_attempted", ())) if job else set()
    while True:
        members = {index - 1 for index in data["playlists"].get(scope, []) if index}
        todo = sorted(offset for offset in members
                      if records[offset]["key"] not in attempted
                      and records[offset].get("active", True)
                      and not verified_existing(offset + 1, records[offset]))
        if not todo:
            break
        if process_requests(records) is False:
            return
        start = todo[0]
        end = start + 1
        wanted = set(todo)
        while end - start < batch_size and end in wanted:
            end += 1
        if not run_batch(start, end - start, advance_cursor=False):
            save_probe_cache()
            flush_report()
            return
        attempted.update(record["key"] for record in records[start:end])
        report()
        data = load_manifest()
        records = data["records"]
        if data.get("source_signature") != signature:
            prepare()
            data = load_manifest()
            records = data["records"]
            signature = data.get("source_signature")
    process_requests(records, limit=100000)
    report()


def run(pilot=False, batch_size=50):
    scope = os.environ.get("OFFLINE_MUSIC_SCOPE", "").strip()
    if scope and not pilot:
        run_scoped(scope, batch_size=batch_size)
        return
    if not catalog_store().has_build():
        prepare()
    if bulk_staging_has_audio():
        report()
    start = cursor_offset()
    write_cursor(start)
    if pilot:
        run_batch(start, 20, pilot=True)
        report()
        return
    records = load_manifest()["records"]
    total = len(records)
    requests_store().reset_searching()
    while start < total:
        latest = source_signature()
        if latest != load_manifest().get("source_signature"):
            prepare()
            records = load_manifest()["records"]
            total = len(records)
            report()
        if process_requests(records) is False:
            return
        stage = records[start]["first_playlist"]
        end = start
        while end < total and end - start < batch_size and records[end]["first_playlist"] == stage:
            end += 1
        while start < end:
            if not records[start].get("active", True):
                start += 1
                write_cursor(start)
                continue
            existing = verified_existing(start + 1, records[start])
            if existing:
                start += 1
                write_cursor(start)
                continue
            segment_end = start + 1
            while (segment_end < end and records[segment_end].get("active", True)
                   and not verified_existing(segment_end + 1, records[segment_end])):
                segment_end += 1
            if not run_batch(start, segment_end - start):
                save_probe_cache()
                flush_report()
                return
            start = segment_end
        report()
    process_requests(records, limit=100000)


def run_requests():
    if not catalog_store().has_build():
        prepare()
    records = load_manifest()["records"]
    requests_store().reset_interrupted()
    process_requests(records, limit=100000)


def _write_export_status(state, done, total, failed, **details):
    # The record's shape and serialization belong to the codec in
    # catalog.audio_store (plan 061 step 1); the write itself
    # stays private_write, atomic at mode 0600.
    private_write(STATE / "export-status.json", dumps_export_status(
        {**export_status_record(state, done, total, failed, time.time()), **details}))


def _extinf_line(record):
    """One #EXTINF line for a record, the shape every M3U8 this
    program writes uses (report()'s generated playlists and the
    scoped export's manifest)."""
    return (f"#EXTINF:{round(float(record['length'] or 0))},"
            f"{record['artist']} - {record['title']}")


def _write_export_manifest(folder, base, order, targets, records):
    """The scoped export's playlist file (plan 063): an M3U8 in
    the export folder listing the playlist's tracks in playlist
    order, in the shape report()'s generated playlists use
    (#EXTINF, then the file name relative to the folder the
    manifest sits in), so the folder plays as a playlist outside
    the room. A track whose file is not in the folder after the
    run (its write failed and was removed) is left out; a track
    the playlist lists twice is listed twice, as the playlist
    itself lists it."""
    # A catalog target's portable name opens with its record
    # index, zero-padded (portable_name's contract in
    # catalog.audio_store); the manifest keys names by it.
    names = {}
    for _key, name in targets:
        label = name.split(" - ", 1)[0]
        if label.isdigit():
            names[int(label)] = name
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U"]
    for index in order:
        name = names.get(index)
        if name is None or not (folder / name).is_file():
            continue
        record = records[index]
        lines.append(_extinf_line(record))
        lines.append(name)
    private_write(folder / f"{base}.m3u8", "\n".join(lines) + "\n")


def _export_compact(store, targets, folder, profile, progress, completed, **options):
    written = failed = 0
    for position, (source, name) in enumerate(targets, 1):
        try:
            with tempfile.TemporaryDirectory(dir=STATE) as scratch:
                staging = Path(scratch)
                original = source
                if not isinstance(source, Path):
                    original = staging / Path(name).name
                    if not store.extract(source, original):
                        raise OSError("Audio could not be extracted")
                with original.open("rb") as handle:
                    with compact_audio.open_copy(
                        STATE / "compact-audio", handle, name, profile,
                        source_hash=None if isinstance(source, Path) else source,
                    ) as (copy, export_name):
                        staged = staging / "copy"
                        with staged.open("wb") as output:
                            shutil.copyfileobj(copy, output)
                count, errors = materialize(store, [(staged, export_name)], folder,
                                            completed=completed, **options)
                written += count
                failed += errors
        except OSError as error:
            print(f"Export failed for {name}: {error}", file=sys.stderr)
            failed += 1
        progress(position, len(targets), failed)
    return written, failed


def run_export():
    """Write every referenced blob track back out as plain files
    (plan 060 step 6): the store's exit hatch, and the standing
    proof the library is not locked in. Read-only against the
    store; progress lands in the application folder's
    export-status.json, where the settings snapshot reads it.

    A scoped run (plan 063) names one playlist key in
    OFFLINE_MUSIC_SCOPE, the bulk download's scoping channel:
    only that playlist's tracks are written, into
    exported/<playlist name>/, with an M3U8 manifest in playlist
    order. The folder's name is the playlist's display name as
    the server resolved it (OFFLINE_MUSIC_EXPORT_NAME); the
    manifest's own list name is the fallback.
    """
    store = media_store()
    if not catalog_store().has_build():
        prepare()
    data = load_manifest()
    records = {record["index"]: record for record in data["records"]}
    downloaded = requests_store().downloaded_audio()
    customs = custom_request_rows(downloaded, requests_store().get)
    scope = os.environ.get("OFFLINE_MUSIC_SCOPE", "").strip()
    order = None
    profile = os.environ.get("LISTENING_EXPORT_FORMAT", "original")
    if profile not in ("original", *compact_audio.PROFILES):
        raise ValueError("Unknown export format")
    folder = shared_music.export_folder(ROOT)
    if profile != "original":
        label = "Compact Opus" if profile == "opus" else "Compact AAC"
        folder = playlist_export_folder(folder, label, f"\0compact-{profile}")
    folder_base = ""
    if scope:
        entries = data["playlists"].get(scope)
        if entries is None:
            # The server validated the key before starting this
            # worker; the build has changed under it since. Close
            # the status out so the snapshot does not sit at
            # running, and say so in the log.
            _write_export_status("done", 0, 0, 0)
            print(json.dumps({"exported": 0, "failed": 0, "total": 0,
                              "error": f"Unknown playlist: {scope}"}))
            return
        order = [index for index in entries if index]
        name = os.environ.get("OFFLINE_MUSIC_EXPORT_NAME", "").strip() \
            or (data.get("list_names") or {}).get(scope) \
            or scope.replace("_", " ")
        folder_base = portable_folder_name(name)
        if folder_base.casefold() == "custom":
            # The whole-library export owns exported/custom/ for
            # custom requests; a playlist by that name exports
            # under its key instead of mixing into that folder.
            folder_base = portable_folder_name(scope)
            if folder_base.casefold() == "custom":
                folder_base = "custom-playlist"
        folder = playlist_export_folder(folder, folder_base, scope)
        folder_base = folder.name
    targets = blob_targets(store, catalog_store().read_coverage(),
                           records, downloaded, customs,
                           playlist_order=order,
                           file_lookup=lambda path: locate_stored(path, LIBRARY))
    if not scope and profile == "original" and store is not None and REQUESTS_DB == ROOT / "collection.sqlite3":
        targets.extend(store.original_exports({source for source, _ in targets}))
    _write_export_status("running", 0, len(targets), 0)

    def progress(done, total, failed):
        _write_export_status("running", done, total, failed)

    completed = []
    options = {"preserve_existing": bool(os.environ.get("LISTENING_ROOM_EXPORT_DIR"))}
    if profile == "original":
        written, failed = materialize(store, targets, folder, on_done=progress,
                                      completed=completed, **options)
    else:
        written, failed = _export_compact(store, targets, folder, profile, progress,
                                          completed, **options)
    if order is not None:
        _write_export_manifest(folder, folder_base, order, completed,
                               records)
    _write_export_status("done", len(targets), len(targets), failed, written=written,
                         skipped=len(targets) - written - failed, destination=str(folder))
    print(json.dumps({"exported": written, "failed": failed,
                      "total": len(targets)}))


def _write_migration_status(state, done, total, counts,
                            skipped_files=()):
    # The record's shape and serialization belong to the codec in
    # catalog.media_reconciliation (plan 062); the write itself
    # stays private_write, atomic at mode 0600, as the export
    # status's does.
    private_write(STATE / "migration-status.json",
                  dumps_migration_status(migration_status_record(
                      state, done, total, counts, time.time(),
                      skipped_files)))


def run_migrate():
    """Explicitly copy file references, preserving originals and skipped files."""
    store = media_store(create=True)
    counts = {"migrated": 0, "skipped": 0, "already": 0}
    skipped_files = []
    _write_migration_status("running", 0, 0, counts)

    def progress(done, total, current):
        _write_migration_status("running", done, total, current,
                                skipped_files)

    catalog = catalog_store()
    requests = requests_store()
    coverage = catalog.read_coverage()
    downloaded = requests.downloaded_audio()
    owed = unmigrated_files(coverage, downloaded, LIBRARY)
    if shutil.disk_usage(ROOT).free < MIN_FREE + 2 * sum(path.stat().st_size for path in owed):
        _write_migration_status("failed", 0, len(owed), counts)
        raise RuntimeError("Copy needs more space for the audio and the 20 GiB reserve")
    counts = migrate_library(
        store, coverage, downloaded, LIBRARY,
        rewrite_coverage=catalog.update_coverage_path,
        rewrite_downloaded=requests.set_audio_path,
        records=(catalog.read_build() or {}).get("records", ()),
        probe_file=probe, on_progress=progress,
        on_skip=lambda resolved: skipped_files.append(str(resolved)))
    save_probe_cache()
    total = sum(counts.values())
    _write_migration_status("done", total, total, counts,
                            skipped_files)
    print(json.dumps({**counts, "total": total}))


def run_reclaim():
    """Hand the media database's dead space back, progressively
    (plan 060 step 8): bounded bursts with a breath between them,
    until the dead bytes fall under the floor or ten minutes pass.
    Never a full-file rewrite.
    """
    store = media_store()
    if store is None:
        print(json.dumps({"reclaimed_pages": 0}))
        return
    deadline = time.monotonic() + 600
    freed = 0
    while time.monotonic() < deadline:
        if store.dead_bytes() <= 16 * 1024**2:
            break
        freed += store.reclaim_burst(2000, 5)
        time.sleep(0.5)
    print(json.dumps({"reclaimed_pages": freed,
                      "dead_bytes": store.dead_bytes()}))


def share_recording_matches(records, rows, checked):
    """Re-verify available audio against every equivalent recording's own metadata."""
    def identities(record):
        if record.get("isrc"):
            yield ("isrc", record["isrc"].casefold())
        if record.get("linked_recording"):
            yield ("linked", record["linked_recording"])

    matches = {}
    for row in rows:
        media = checked.get(row["path"], {}).get("media")
        if row["status"] == "verified" and media:
            for key in identities(records[int(row["index"]) - 1]):
                matches.setdefault(key, {})[row["path"]] = media
    for row in rows:
        if row["status"] in {"verified", "retired"}:
            continue
        record = records[int(row["index"]) - 1]
        candidates = {path: media for key in identities(record)
                      for path, media in matches.get(key, {}).items()}
        for path, media in candidates.items():
            verdict = judge(record["title"], record["length"], media)
            if verdict.ok:
                row.update(status="verified", path=path, reason=verdict.reason)
                break


def _stored_form(path):
    """The persisted coverage form of an accepted path: library-relative.

    Rows carry resolved paths in memory (the probe cache and the ISRC
    sharing pass key on them); only the persisted form is relative, so
    a workspace move cannot strand it (plan 009). A path with no
    library-relative form (a symlink target outside the library) is
    stored as-is.
    """
    if not path:
        return ""
    try:
        return stored_path(path, LIBRARY)
    except ValueError:
        return path


def report():
    global _REPORT_DIRTY, _probe_cache_dirty
    _REPORT_DIRTY = False
    build_stamp = catalog_store().build_stamp()
    data = load_manifest()
    attempted_through = cursor_offset()
    attempted = bulk_attempted_indices(data["records"], attempted_through)
    found = {}
    library_files = []
    incoming = STATE / "bulk-incoming"
    shared = set(shared_music.shared_files())
    candidates_on_disk = [*incoming.rglob("*"), *LIBRARY.rglob("*"), *sorted(shared)]
    for path in candidates_on_disk:
        if not path.is_file() or path.suffix.lower() not in AUDIO_FORMATS:
            continue
        library_files.append(path)
        match = re.match(r"^(\d+) - ", path.name)
        if match and path not in shared:
            found.setdefault(int(match.group(1)), []).append(path)
    checked = load_probe_cache()
    # Prune the probe cache to the walk's path set (plan 045): a file
    # that left the library loses its cache row in the same report
    # that notices it gone, instead of the row lingering forever.
    # Entries are keyed by str(path) of the walk's own Path objects
    # (cached_probe), so the comparison is like-for-like; marking the
    # cache dirty makes the wholesale save below persist the pruned
    # set.
    present = {str(path) for path in library_files}
    for key in list(checked):
        if key not in present:
            del checked[key]
            _probe_cache_dirty = True
    # Bring-your-own-audio discovery (plan 030): a file no record
    # claimed through the "<index> - " name convention is probed and
    # matched to a record by its normalized artist/title/album tags,
    # then judged below by the same rule as a download. A file whose
    # tags match no record is ignored; a file matching several records
    # is skipped, never guessed. The album takes part as a value: an
    # untagged album only matches a record whose album is also empty.
    claimed = {path for group in found.values() for path in group}
    def recording_tags(tags):
        return tuple(normalize_title(tags.get(field, "")) for field in ("artist", "title", "album"))

    by_tuple = {}
    for record in data["records"]:
        key = recording_tags(record)
        by_tuple.setdefault(key, []).append(record["index"])
    tag_matched = {}
    unmatched_files = 0
    ambiguous_files = 0
    for path in library_files:
        if path in claimed:
            continue
        media = cached_probe(path, local_audio_only=True) if path in shared else cached_probe(path)
        matches = []
        if media is not None:
            key = recording_tags(media)
            matches = by_tuple.get(key, [])
        linked = {data["records"][i - 1].get("linked_recording", i) for i in matches}
        if len(linked) == 1:
            tag_matched.setdefault(matches[0], []).append(path)
        elif matches:
            ambiguous_files += 1
        else:
            unmatched_files += 1
    # Plan 060: blob-backed records. A record whose audio lives in
    # the media database has no file for the walk above to find, so
    # its candidacy comes from the store, through the one BlobIndex
    # (plan 061 step 2): the previous coverage row's blob reference,
    # re-judged against the probe stored at landing, with a
    # downloaded request's reference bridging a fresh landing into
    # its first report. A record the blob verifies leaves file
    # matching entirely.
    database_mode = audio_store_mode() == "database"
    store = media_store(create=database_mode or bool(shared))
    blob_index = None
    downloaded = requests_store().downloaded_audio()
    selected_files = {entry["track_index"]: entry for entry in downloaded
                      if entry["format_confirmed"] and entry["track_index"]
                      and not is_blob_ref(entry["audio_path"])}
    previous_rows = catalog_store().read_coverage() if store is not None or selected_files else []
    if store is not None:
        store.adopt_catalog_references(previous_rows, data["records"])
        blob_index = BlobIndex(store, previous_rows, downloaded, data["records"])
    # Only follow the current coverage handoff. Superseded request history
    # cannot replace a later owner-selected copy on subsequent scans.
    previous_paths = {row["index"]: row["path"] for row in previous_rows}
    summary = {"verified": 0, "unmatched": 0, "pending": 0, "rejected": 0,
               "metadata_issues": len(data["issues"]),
               "unmatched_files": unmatched_files, "ambiguous_files": ambiguous_files,
               "attempted": sum(row["index"] in attempted for row in data["records"] if row.get("active", True))}
    rows = []
    lengths = length_store()
    protected = set()
    bulk_resume_free = 0
    verified_by_path = {}
    for record in data["records"]:
        index = record["index"]
        disk_candidates = found.get(index, []) + tag_matched.get(index, [])
        accepted = None
        deferred = False
        accepted_media = None
        verified_stamp = None
        candidates = []
        reasons = []
        blob = blob_index.get(index) if blob_index is not None else None
        selected = selected_files.get(index)
        if selected and previous_paths.get(index) == selected["audio_path"]:
            preferred = locate_stored(selected["audio_path"], LIBRARY)
            if preferred and preferred.is_file():
                facts = cached_probe(preferred)
                verdict = judge_download(record["title"], record["length"], facts,
                                         preferred.suffix, selected["requested_format"])
                if verdict.ok and not database_mode:
                    accepted, accepted_media = preferred, facts
                    _backfill_length(record, facts, lengths)
                    if verdict.reason:
                        reasons.append(verdict.reason)
        if accepted is None and blob is not None:
            reference, facts = blob
            verdict = judge(record["title"], record["length"], facts)
            if verdict.ok and (not any(incoming in path.parents for path in disk_candidates)
                               or store.verifies_original(blob_key(reference))):
                accepted = reference
                accepted_media = facts
                _backfill_length(record, facts, lengths)
                if verdict.reason:
                    reasons.append(verdict.reason)
            else:
                reasons.append(verdict.reason or "Stored audio failed its checksum")
        if accepted is None:
            candidates = disk_candidates
            for path in candidates:
                peer_download = incoming in path.parents
                ingest = peer_download or path in shared or (database_mode and (ROOT / "collection.sqlite3").is_file())
                # Before committing new owned bytes, bind fresh probe facts to this file version.
                stamp = source_stamp(path.stat()) if ingest else None
                if path in shared:
                    media = probe(path, local_audio_only=True)
                    if media and recording_tags(media) != recording_tags(record):
                        reasons.append("Recording tags changed during verification")
                        continue
                else:
                    media = probe(path) if ingest else cached_probe(path)
                if ingest and source_stamp(path.stat()) != stamp:
                    reasons.append("Audio changed during verification")
                    continue
                if peer_download:
                    verdict = judge_bulk_download(record["title"], record["length"], media, path.suffix)
                else:
                    verdict = judge(record["title"], record["length"], media)
                if verdict.ok:
                    if peer_download:
                        required = MIN_FREE + 3 * stamp[2]
                        if free_bytes() < required:
                            reference = (store.link_stored_original(path, ("catalog", record["key"]), stamp)
                                         if database_mode else None)
                            if reference:
                                accepted, accepted_media = reference, store.probe(blob_key(reference))
                                _backfill_length(record, accepted_media, lengths)
                                break
                            reasons.append("Verified transfer is waiting for storage space")
                            protected.add(path)
                            bulk_resume_free = min(bulk_resume_free or required, required)
                            deferred = True
                            continue
                    accepted = path
                    verified_stamp = stamp
                    accepted_media = media
                    _backfill_length(record, media, lengths)
                    if verdict.reason:
                        reasons.append(verdict.reason)
                    break
                reasons.append(verdict.reason)
        status = ("retired" if not record.get("active", True) else
                  "verified" if accepted else "pending" if deferred else "rejected" if candidates else
                  "unmatched" if index in attempted else "pending")
        if accepted is None or isinstance(accepted, str):
            path_value = accepted or ""
        elif verified_stamp is not None:
            if database_mode or accepted in shared:
                key = store.put_file(accepted, accepted_media, owners=(("catalog", record["key"]),),
                                     expected_stamp=verified_stamp)
                path_value = blob_ref(key)
            else:
                # Stage peer transfers outside the owner's files until verified.
                # Publish a verified copy without overwriting a name collision.
                path_value = str(publish_verified_download(accepted, LIBRARY / accepted.name, verified_stamp))
            if incoming in accepted.parents:
                if is_blob_ref(path_value):
                    if not remove_staged_duplicate(accepted, incoming, store, key, verified_stamp) and accepted.exists():
                        raise OSError("Could not release the verified bulk transfer. Its staged copy was kept.")
                elif (not incoming.is_symlink()
                      and incoming.resolve() / accepted.relative_to(incoming) == accepted.resolve()
                      and source_stamp(accepted.stat()) == verified_stamp):
                    accepted.unlink()
        else:
            path_value = str(accepted.resolve())
        if status == "verified":
            verified_by_path[path_value] = {"media": accepted_media}
        rows.append({"index": index, "status": status, "artist": record["artist"],
                     "title": record["title"], "uri": record["uri"],
                     "path": path_value,
                     "reason": "; ".join(reasons)})
    share_recording_matches(data["records"], rows, verified_by_path)
    for row in rows:
        if row["status"] in {"verified", "unmatched", "pending", "rejected"}:
            summary[row["status"]] += 1
    STATE.mkdir(exist_ok=True)
    save_probe_cache()
    catalog_store().write_coverage(
        [{**row, "path": _stored_form(row["path"])} for row in rows])
    if store is not None:
        reconcile_bulk_staging(incoming, store, protected)
        reconcile_request_staging(downloaded, store)
    paths = {r["index"]: r["path"] for r in rows if r["status"] == "verified"}
    per_playlist = {}
    for name, entries in data["playlists"].items():
        lines = ["#EXTM3U"]
        included = 0
        for index in entries:
            if index is not None and index in paths:
                included += 1
                if is_blob_ref(paths[index]):
                    # Plan 060: a blob track counts as playable, but
                    # a store reference is meaningless to an external
                    # player, so no file line is written for it.
                    continue
                record = data["records"][index - 1]
                lines.append(_extinf_line(record))
                lines.append(os.path.relpath(paths[index], PLAYLISTS.resolve()))
        (PLAYLISTS / f"{name}.m3u8").write_text("\n".join(lines) + "\n", encoding="utf-8")
        per_playlist[name] = {"entries": len(entries), "attempted": sum(
            index in attempted for index in entries),
            "playable": included}
    summary["playlists"] = per_playlist
    # Plan 060: the library's size is its files plus the store's
    # audio bytes. Ingested files were deleted above, so the stat
    # pass only counts files still on disk and nothing doubles.
    summary["library_gib"] = round(
        (sum(p.stat().st_size for p in library_files if p not in shared and p.is_file())
         + (store.audio_bytes() if store is not None else 0)) / 1024**3, 3)
    summary["free_gib"] = round(free_bytes() / 1024**3, 3)
    # The summary itself is derived on read now (plan 036); only the
    # figures a report run alone can know are stored, as report facts.
    catalog_store().write_report_facts({
        "build_stamp": build_stamp,
        "attempted_through": attempted_through,
        "unmatched_files": summary["unmatched_files"],
        "ambiguous_files": summary["ambiguous_files"],
        "bulk_resume_free": bulk_resume_free,
        "library_gib": summary["library_gib"],
        "free_gib": summary["free_gib"]})
    if store is not None:
        # Coverage and request history are projections, never deletion authority.
        dead = store.dead_bytes()
        if dead > RECLAIM_THRESHOLD:
            queue = JobQueue(host_database(REQUESTS_DB, STATE))
            if queue.queued_for("reclaim") is None \
                    and queue.running_for("reclaim") is None:
                queue.enqueue("reclaim", {"type": "library"})
    lines = ["# Offline music download status", "",
             f"Searches attempted: {summary['attempted']:,} of {len(data['records']):,} unique tracks.",
             f"Verified audio: {summary['verified']:,}; unmatched: {summary['unmatched']:,}; "
             f"rejected: {summary['rejected']:,}; waiting: {summary['pending']:,}.",
             f"Library size: {summary['library_gib']:.2f} GiB; free space: {summary['free_gib']:.2f} GiB "
             "(20 GiB reserve).", "", "| Playlist | Entries | Searched | Playable |",
             "|---|---:|---:|---:|"]
    for name, values in per_playlist.items():
        lines.append(f"| {name} | {values['entries']:,} | {values['attempted']:,} | {values['playable']:,} |")
    lines.extend(["", f"Portable XSPF lists are in `{SOURCES}`.",
                  f"Audio is in `{LIBRARY}`; ordered M3U8 files are in `{PLAYLISTS}`.",
                  f"Detailed match and rejection reasons are in the coverage table of `{REQUESTS_DB}`.",
                  "The downloader resumes from its saved cursor. Nicotine+ should stay closed while it uses "
                  "the same Soulseek account.", ""])
    try:
        OUTPUTS.mkdir(exist_ok=True)
        (OUTPUTS / "offline_music_status.md").write_text("\n".join(lines), encoding="utf-8")
    except PermissionError:
        pass
    print(json.dumps(summary, ensure_ascii=False))


def run_genres():
    """Bulk genre pass (plan 029): fill every missing genre, once.

    File tags first (no network), then MusicBrainz at its paced rate,
    then ListenBrainz. Records already categorized are never looked up
    again, so interrupting is safe and rerunning resumes where the
    last pass stopped. Progress lines land in the runner log the
    activity view reads.
    """
    if not catalog_store().has_build():
        prepare()
    records = load_manifest()["records"]
    total = sum(1 for record in records if record.get("active", True))
    store = genre_store()
    job_id = os.environ.get("LISTENING_GENRE_JOB_ID")
    queue = JobQueue(host_database(REQUESTS_DB, STATE)) if job_id else None
    last_write = 0.0
    counts = {}

    def media_for(record):
        path = verified_existing(record["index"], record)
        if path is None:
            return None
        if is_blob_ref(str(path)):
            # A blob-backed record's home is the media database
            # (plan 063 settlement 1): its probe facts are the
            # ones stored beside its bytes, read the way the
            # BlobIndex reads them. cached_probe cannot stat a
            # reference, so without this leg a database song
            # categorized only from network sources.
            store = media_store()
            return store.probe(blob_key(str(path))) if store else None
        return cached_probe(path)

    def progress(checked, total, found, unavailable):
        nonlocal last_write
        counts.update(checked=checked, scan_total=total, found=found, unavailable=unavailable)
        now = time.monotonic()
        if queue and (checked in (0, total) or now - last_write >= 2):
            queue.record_progress(int(job_id), counts)
            last_write = now
        if checked == total or (checked and checked % 25 == 0):
            print(f"Categorizing genres: {checked} of {total}", flush=True)

    filled = categorize(records, store, media_for=media_for,
                        metadata=external_metadata(), on_progress=progress)
    save_probe_cache()
    remaining = len(missing_genre_records(records, store.reload()))
    if queue:
        queue.record_progress(int(job_id), {**counts, "remaining": remaining, "complete": 1})
    print(json.dumps({"categorized": filled, "already_categorized": total - counts["scan_total"],
                      "still_missing": remaining, "total": total},
                     ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "credentials", "pilot", "run", "requests", "report", "genres", "export", "reclaim", "migrate"])
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
    elif args.action == "genres":
        run_genres()
    elif args.action == "export":
        run_export()
    elif args.action == "reclaim":
        run_reclaim()
    elif args.action == "migrate":
        run_migrate()
    elif args.action == "credentials":
        setup_credentials()
    elif args.action == "pilot":
        run(pilot=True)
    elif args.action == "run":
        run(batch_size=args.batch_size)
        record_download_progress(complete=True)
    elif args.action == "requests":
        run_requests()
        record_download_progress(complete=True)
    else:
        report()


if __name__ == "__main__":
    from catalog.collection_snapshot import collection_access, portable_room_access
    with collection_access(STATE), portable_room_access(ROOT, STATE):
        main()
