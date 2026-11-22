#!/usr/bin/env python3
"""Local music library, download status, metadata, and shared playback API."""

from contextlib import ExitStack

import argparse
import base64
import binascii
import configparser
import csv
import gzip
import hmac
import ipaddress
import json
import logging
import math
import tempfile
import mimetypes
import os
import random
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import zipfile
from io import BytesIO
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from catalog import audio_resolution, compact_audio, hiking, offline_replica
from catalog.audio_store import MediaStore, blob_key, is_blob_ref
from catalog.catalog import Catalog, CatalogPaths, spotify_share_url
from catalog.media_access import MediaAccess
from catalog.media_reconciliation import (MIGRATION_STATUS_IDLE,
                                          read_migration_status,
                                          unmigrated_files)
from catalog.catalog_store import CatalogStore
from catalog.collection_projection import ListProjection
from catalog.favorites import FavoritesStore
from catalog import moods
from catalog.genres import GenreStore, canonical_genre, group_genre_flow, source_label
from playback.playback import PlaybackSession
from playback.jams import JamDirectory, nearby_network
from playback.outputs import output_from, valid_output_id
from playback.insights import PlaybackTraffic
from playback.member_profiles import MemberProfiles, ProfileConflict
from playback.play_events import PlayEventsStore
from playback import radio_resume
from playback.radio import (PROVIDERS, RADIO_PRESETS, clean_stations, live_catalog,
                            provider_enabled, source_enabled, stations_from)
from downloads.engine_contract import EngineError, engine_selection, engines_ready, manager_call, source_summary
from system import client_artifacts
from devices.announcements import AnnouncementBoard
from devices.device_store import DeviceStore, invitation_room_proof
from devices.presence import Presence
from downloads.download_activity import recent_events
from downloads.requests_store import RequestsStore
from downloads.state_store import StateStore
from downloads.verification import PROBE_CACHE_VERSION, probe
from metadata.artist_connections import ArtistConnections, CONNECTION_PATH, ConnectionsBusy, valid_id
from metadata.music_profiles import PROFILE_PATH, MusicProfiles
from metadata.external_metadata import ExternalMetadata
from metadata import discovery_board, discovery_trail
from library.library_protocol import (SUPPORTED, canonicalize_directory, canonicalize_source, migrate_legacy_lists, parse_list,
                              playlist_title, portable_m3u8, read_list, track_key, write_csv, write_xspf,
                              xspf_files)
from library.list_intake import place_lists
from library import catalog_imports, playlist_edits, playlist_links, playlist_sharing, shared_music
from library.local_sources import (discover_folders, is_music_list, list_files, source_mirror_kinds,
                                   sync_files, sync_folders)
from adapters import opensubsonic, public_discovery, youtube_search
from adapters.spotify_sync import SpotifyError, SpotifySync
from system.formats import BULK_DOWNLOAD_FORMATS, bulk_download_format
from system.config import host_database
from system.config import AppConfig, default_root, legacy_config_path, resolve_config_path
from system import server_control
from system.formats import AUDIO_FORMATS
from system.hosts import configured_origin, host_permitted, parse_host
from system.jobs import Jobs
from system.job_queue import JobQueue
from system.logtail import tail_lines
from system.normalize import normalize_title
from system.owner_auth import (AccountRole, COOKIE_NAME, MEMBER_COOKIE_NAME, DATABASE_NAME as OWNER_DATABASE_NAME,
                               OwnerAuth, OwnerLoginThrottled, RETRY_DELAY, SESSION_AGE, SESSION_RENEWAL_WINDOW)
from system import container_updates, tailscale
from system.pairing import home_server, invitation_authority, is_tailnet_host
from system.private_files import private_write
from system.refresh import (RefreshPipeline, directory_signature,
                            manifest_signature, stat_signature)
from system.supervisor import Supervisor
from system.updater import (Updater, UpdateError, REBUILD_FAILED,
                            REBUILD_NO_TOOLCHAIN, frontend_build_needed,
                            install_kind, rebuild_frontend, spawn_restarter)

APP = Path(__file__).resolve().parent
REPO_ROOT = APP.parent
FRONTEND = REPO_ROOT / "frontend"
RUNNER = APP / "runner.py"

# Application state, built by init() (called from main(), or by tests
# against a temp workspace). Importing this module creates no
# directories, writes no files, binds no sockets, and starts no
# threads; configuration loading lives in system.config.
CONFIG = None
CONFIG_PATH = None
SETTINGS = {}
ROOT = None
STATE = None
LIBRARY = None
PLAYLIST_OUTPUT = None
DB = None
RUNNER_LOG = None
SUPERVISOR = None  # Built by init() (plan 050 Step 2, ADR 0001).
ROOT_LOCK = threading.RLock()
PLAYLIST_EDIT_LOCK = threading.RLock()
PLAYBACK_TRAFFIC = PlaybackTraffic()
def requests_store():
    """The shared requests database, owned by downloads.requests_store."""
    return RequestsStore(DB)


def recent_requests():
    """Expose Play only after the playback catalog can resolve an indexed request."""
    rows = requests_store().recent(80)
    awaiting_catalog = []
    with ROOT_LOCK:
        for row in rows:
            if row["status"] != "downloaded" or not row["audio_path"]:
                continue
            index = row["track_index"]
            if index is None:
                row["audio"] = f"/api/audio/custom/{row['id']}"
            elif (catalog.active_record_at(index) and catalog.status_of(index) == "verified"
                  and catalog.coverage[index].get("path") == row["audio_path"]):
                row["audio"] = f"/api/audio/{index}"
            else:
                awaiting_catalog.append(row)
        if awaiting_catalog:
            changed = catalog.coverage_stamp != catalog_store().coverage_stamp()
            if changed:
                MAINTENANCE_WAKE.set()
            preparing = changed or worker_running("requests") or worker_running("downloads")
            for row in awaiting_catalog:
                index = row["track_index"]
                if not changed and catalog.active_record_at(index) and catalog.status_of(index) == "verified":
                    # An older request can refer to a copy subsequently replaced in the library.
                    row["audio"] = f"/api/audio/{index}"
                    continue
                row.update(status="searching" if preparing else "pending",
                           message="Preparing playback" if preparing else "No playable copy in the library")
    return rows


def catalog_store():
    """The catalog build and coverage store (plan 036)."""
    return CatalogStore(DB)


def configured_lists(root=None):
    root = root or ROOT
    if root != ROOT:
        return root / "lists"
    return Path(SETTINGS.get("lists_folder") or root / "lists").expanduser().resolve()


def list_projection():
    if ROOT is not None and CONFIG_PATH is not None and (ROOT / "collection.sqlite3").is_file():
        return ListProjection(ROOT / "collection.sqlite3", configured_lists(), CONFIG_PATH.parent)
    return None


def source_dir(root=None):
    projection = list_projection() if root is None or root == ROOT else None
    return projection.directory if projection is not None else configured_lists(root)


def import_lists(incoming, on_conflict):
    """Publish canonical documents before reporting an import to its caller."""
    with ROOT_LOCK, REFRESH_LOCK:
        projection = list_projection()
        folder = source_dir()
        if projection is None:
            folder.mkdir(parents=True, exist_ok=True)
            names, conflicts = place_lists(folder, incoming, on_conflict)
        else:
            names, conflicts = projection.place(incoming, on_conflict)
        for name in names or ():
            if Path(name).suffix.casefold() != ".xspf":
                canonicalize_source(folder / name)
        return names, conflicts


def publish_adapter_edits():
    projection = list_projection()
    if projection is not None:
        projection.publish_adapter_edits()


def catalog_paths():
    return CatalogPaths(database=DB, lists=source_dir())


def sync_sources():
    """Mirror watched folders and files into the list directory, then canonicalize."""
    projection = list_projection()
    if projection is not None:
        projection.reconcile()
    sync_folders(SETTINGS.get("source_folders", []), source_dir())
    sync_files(SETTINGS.get("source_files", []), source_dir())
    canonicalize_directory(source_dir())


def rebuild_library(environment=None):
    """Regenerate the manifest and report through the runner; raises on failure."""
    environment = environment or runner_environment()
    for action in ("prepare", "report"):
        timeout = None if action == "report" and environment.get("LISTENING_ROOM_MUSIC_INBOX") else 180
        result = subprocess.run([sys.executable, str(RUNNER), action], env=environment,
                                capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip()[-300:] or f"Could not {action} library")


def refresh_inputs():
    """The paths whose stat signature gates a refresh: watched candidates
    (by extension only, contents never opened), linked files, the
    list directory's own files, and the audio folder (a directory
    contributes its files directly inside it). The audio folder rides
    the stat gate so a change walks the full sync path; whether it
    unsettles the build is the pipeline's own comparison against the
    stored build's audio signature (plan 055)."""
    paths = []
    for folder in SETTINGS.get("source_folders", []):
        paths.extend(list_files(folder, sniff=False))
    paths.extend(Path(path) for path in SETTINGS.get("source_files", []))
    projection = list_projection()
    if projection is not None and projection.source is not None:
        paths.append(projection.source)
    directory = source_dir()
    if directory.is_dir():
        paths.extend(path for path in directory.iterdir()
                     if path.is_file() and path.suffix.casefold() in SUPPORTED)
    if LIBRARY is not None:
        paths.append(LIBRARY)
    return paths


REFRESH = None  # Built by init(); refresh_library() is the entry point.

REFRESH_LOCK = threading.Lock()


def refresh_library(force=False):
    """The one refresh entry point (system.refresh.RefreshPipeline).

    Serialized (plan 038): the pipeline itself is not thread-safe, and
    its callers sit on several threads at once (the watchdog, /api/sync,
    the import and settings handlers, the Spotify change callback).
    Without the lock, two callers that both see changed inputs run two
    prepare+report subprocesses concurrently against the same state
    database; with it, the second caller re-checks settled state after
    the first finishes and usually finds nothing to do.
    """
    with REFRESH_LOCK:
        if update_draining():
            return False
        return REFRESH.refresh_if_changed(force=force)


def valid_folder(raw, require_csv=True):
    root = Path(str(raw)).expanduser().resolve()
    if not root.is_dir():
        if require_csv:
            raise ValueError("Choose an existing folder")
        root.mkdir(parents=True, exist_ok=True)
    sources = xspf_files(source_dir(root))
    if require_csv and not sources:
        raise ValueError("Import at least one music list")
    for source in sources:
        read_list(source)
    return root


# The four workers (GLOSSARY), by the kind the supervisor knows
# them under. The three library workers take turns on the library;
# sharing does not touch it and may run alongside. Plan 060 adds
# two maintenance kinds the supervisor also owns: reclaim (a job,
# queued like the library jobs) and export (started directly,
# like sharing). Plan 062 adds migrate, the one-time library
# migration, a job like reclaim.
WORKER_KINDS = ("downloads", "requests", "genres", "share", "reclaim", "export", "migrate")
# The workers that write library state, and so take turns alone:
# the three pipeline workers, plus migrate (plan 062). The
# migration rewrites coverage and request references and unlinks
# files while report() rebuilds coverage from its own read, and
# its unlinks change the audio signature the refresh pipeline
# watches, so a refresh rebuild must not fire mid-migration any
# more than a second pipeline worker may start.
LIBRARY_WORKERS = ("downloads", "requests", "genres", "migrate")


def worker_running(kind):
    """Whether a worker is up, asked of the supervisor (ADR 0001).

    The plan 018 memo existed to keep a tmux subprocess off the poll
    path; the supervisor's answer is a pidfile read and a liveness
    probe, cheap enough to ask directly. The function stays the one
    seam every caller (and the tests) reads worker state through.
    """
    if SUPERVISOR is None:
        return False
    return SUPERVISOR.status(kind)


def any_library_worker_running():
    return any(worker_running(kind) for kind in LIBRARY_WORKERS)


def stop_worker(kind):
    """Stop a worker through the supervisor: SIGINT to its process
    group, SIGKILL after 10 seconds (ADR 0001)."""
    if SUPERVISOR is None or not worker_running(kind):
        return
    if not SUPERVISOR.stop(kind):
        raise RuntimeError("The current worker is still stopping; try again shortly")


def runner_environment(root=None):
    root = root or ROOT
    return dict(os.environ, **shared_music.runner_inbox(SETTINGS), LISTENING_ROOM_CONFIG=str(CONFIG_PATH),
                OFFLINE_MUSIC_ROOT=str(root),
                OFFLINE_MUSIC_STATE=str(STATE),
                OFFLINE_MUSIC_SOURCES=str(configured_lists(root)),
                OFFLINE_MUSIC_LIBRARY=str(LIBRARY if root == ROOT else root / "audio"),
                OFFLINE_MUSIC_PLAYLISTS=str(PLAYLIST_OUTPUT if root == ROOT else root / "playlists"),
                OFFLINE_MUSIC_OUTPUTS=str((SETTINGS.get("reports_folder") if root == ROOT else None) or root / "reports"),
                SOCKSEEK_BINARY=str(SETTINGS["sockseek_binary"]),
                SOCKSEEK_CONFIG=str(SETTINGS["credentials_path"]))


class _MediaSources:
    """MediaAccess's view of the server's live state (plan 061).

    Every method reads the module globals as they are now: the
    catalog is rebuilt in place when the library root moves and
    the settings are the loaded dict, so nothing here may capture
    a value when the adapter is constructed.
    """

    def coverage(self):
        return list(catalog.coverage.values())

    def records(self):
        return {record["index"]: record for record in catalog.records}

    def downloaded(self):
        return requests_store().downloaded_audio()

    def request(self, request_id):
        return requests_store().get(request_id)

    def share_folder(self):
        return SETTINGS.get("share_folder", "")


MEDIA = None  # Built by init() (plan 061 step 1).


def migration_status():
    """The library migration's recorded progress (plan 062),
    read through the codec in catalog.media_reconciliation: the
    migrate job card's progress source."""
    if STATE is None:
        return dict(MIGRATION_STATUS_IDLE)
    return read_migration_status(STATE / "migration-status.json")


def has_file_backed_library_entries():
    """Whether the library still holds a song in a plain file the
    one-time migration must move (plan 062), read through
    media_reconciliation.unmigrated_files so this check and the
    migration itself share one definition of participation.
    Files a completed run already settled as skipped (they
    cannot be probed as audio; the run's status names them) are
    settled, not owed: without that, one unprobable file would
    requeue the migration on every pass and the completion
    marker would never stamp."""
    status = migration_status()
    settled = status.get("skipped_files", []) \
        if status.get("state") == "done" else []
    return bool(unmigrated_files(
        catalog_store().read_coverage(),
        requests_store().downloaded_audio(), LIBRARY, settled))


def refresh_settled():
    """Whether the stored catalog build reflects the lists and
    audio folder as they stand (the RefreshPipeline's own test),
    and a refresh pass has run in this process so the watched
    sources are folded into what that test compares: coverage
    is only trustworthy for the migration's decisions once
    both hold."""
    return REFRESH is not None and REFRESH.settled() \
        and REFRESH.synced()


def advance_library_migration():
    """Mark a settled room complete. Adoption of file sources requires an
    explicit owner action and never changes the selected landing setting."""
    if jobs is None or "library_migrated" in SETTINGS:
        return
    if not refresh_settled():
        return
    if has_file_backed_library_entries():
        return
    if worker_running("migrate"):
        return
    queue = JobQueue(host_database(DB, STATE))
    if queue.queued_for("migrate") is not None \
            or queue.running_for("migrate") is not None:
        return
    if not catalog_store().has_build() \
            and manifest_signature(source_dir()):
        # The catalog has never been built, but lists exist to
        # build it from: the files those lists name are not
        # library entries yet, so "nothing to move" is not yet
        # knowable. The refresh pipeline runs the first build on
        # this same cadence; the stamp waits for it. A room with
        # no lists has nothing that can become an entry, and is
        # stamped at once.
        return
    SETTINGS["library_migrated"] = time.time()
    save_settings()


def download_ready(*, for_acquisition=False):
    if engine_selection(SETTINGS) is not None:
        return bool(shutil.which("ffprobe")) and engines_ready(SETTINGS, for_acquisition=for_acquisition)
    return (Path(SETTINGS["credentials_path"]).is_file()
            and Path(SETTINGS["sockseek_binary"]).is_file()
            and os.access(SETTINGS["sockseek_binary"], os.X_OK)
            and bool(shutil.which("ffprobe")))


def runner_invocation(action, root=None, scope=None, download_format=None):
    """The runner as an (argv, env) pair for the supervisor.

    The tmux-era command was a shell string: environment assignments
    in front, output appended to the runner log. The supervisor
    takes the same two facts separately (the environment dict, the
    log it appends to), so nothing is quoted and no shell is
    involved. A list-scoped bulk download (plan 040) stays data in
    the runner's environment, never a separate worker.
    """
    root = root or ROOT
    env = runner_environment(root)
    if scope:
        env["OFFLINE_MUSIC_SCOPE"] = scope
    if download_format:
        env["OFFLINE_MUSIC_DOWNLOAD_FORMAT"] = download_format
    argv = [sys.executable, str(RUNNER), action]
    if Path("/usr/bin/caffeinate").is_file():
        argv = ["/usr/bin/caffeinate", "-i", *argv]
    return argv, env


def genre_tools_ready():
    """The genre pass's tool gate: the runner file and ffprobe.
    Shared by start_genre_worker and the jobs layer, whose blocker
    text names this exact condition."""
    return RUNNER.is_file() and bool(shutil.which("ffprobe"))


def start_genre_worker(job_id=None):
    """Run the bulk genre pass (plan 029) as its own worker.

    Mutually exclusive with the download workers, like they are with
    each other: all three write pipeline state. The pass needs ffprobe
    for file tags but not Sockseek, so download_ready() does not gate
    it; MusicBrainz pacing lives in the metadata module.
    """
    with _START_LOCK:
        if update_draining() or not genre_tools_ready() or SUPERVISOR is None:
            return False
        if any_library_worker_running():
            return False
        argv, env = runner_invocation("genres", ROOT)
        if job_id is not None:
            env["LISTENING_GENRE_JOB_ID"] = str(job_id)
        return SUPERVISOR.start("genres", argv, env=env, log=RUNNER_LOG)


# Serializes the starts: the busy check and the spawn are one step,
# so two callers on different threads (the watchdog, a request kick,
# the dispatcher) cannot both see a free library and start two
# workers into it.
_START_LOCK = threading.Lock()
UPDATE_DRAIN_UNTIL = 0.0
RADIO_PLAYERS = Presence(time.monotonic, 55, 128)
ACTIVE_AUDIO_TRANSFERS = 0


def update_drain(action, *, lease_seconds=180):
    global UPDATE_DRAIN_UNTIL
    with ExitStack() as gates:
        for lock in (ROOT_LOCK, REFRESH_LOCK, _START_LOCK):
            if not lock.acquire(blocking=False):
                return {"idle": False}
            gates.callback(lock.release)
        if action == "release":
            UPDATE_DRAIN_UNTIL = 0.0
        else:
            busy = (update_draining() or playback.snapshot()["session_playing"]
                    or member_profiles.playback_active() or bool(RADIO_PLAYERS.active())
                    or ACTIVE_AUDIO_TRANSFERS or any(worker_running(kind) for kind in WORKER_KINDS))
            if busy:
                return {"idle": False}
            UPDATE_DRAIN_UNTIL = time.monotonic() + lease_seconds
        return {"idle": True}


def update_draining():
    return time.monotonic() < UPDATE_DRAIN_UNTIL

LAST_WORKER_START = 0.0


def start_worker(action, root=None, force=False, scope=None, download_format=None, job_id=None):
    """Start a download worker (bulk for "run", song requests for
    "requests") through the supervisor.

    The imperative starter: the watchdog's standing duties and the
    request kicks call here. Console starts go through the job
    queue instead (ADR 0002); the dispatcher's starter is
    start_job_worker below. The 90-second throttle keeps a worker
    that exits immediately from being restarted on every watchdog
    pass; forced starts (a person asked, an update restarting what
    was running) bypass it.
    """
    global LAST_WORKER_START
    with _START_LOCK:
        if update_draining() or SUPERVISOR is None or not RUNNER.is_file() or not download_ready(for_acquisition=True):
            return False
        if (action == "requests" and job_id is None and engine_selection(SETTINGS) is not None
                and jobs is not None and jobs.waiting("requests")):
            return False
        if any_library_worker_running():
            return False
        if not force and time.monotonic() - LAST_WORKER_START < 90:
            return False
        kind = "downloads" if action == "run" else "requests"
        argv, env = runner_invocation(action, root or ROOT, scope=scope, download_format=download_format)
        if job_id is not None:
            env["LISTENING_DOWNLOAD_JOB_ID"] = str(job_id)
        if SUPERVISOR.start(kind, argv, env=env, log=RUNNER_LOG):
            LAST_WORKER_START = time.monotonic()
            return True
        return False


def start_reclaim_worker():
    """Run the media database's reclaim (plan 060) as its own
    worker. It touches only the media database, never the library,
    but it starts through the job queue like the library workers,
    so the dispatcher's one-at-a-time rule already covers it."""
    with _START_LOCK:
        if update_draining() or SUPERVISOR is None or not RUNNER.is_file():
            return False
        if any_library_worker_running() or worker_running("reclaim"):
            return False
        argv, env = runner_invocation("reclaim", ROOT)
        return SUPERVISOR.start("reclaim", argv, env=env, log=RUNNER_LOG)


def playlist_display_name(key):
    """A playlist's display name, in the overview's precedence:
    the catalog's list names, then Spotify's names, then the
    owner's playlist_names setting, then the key itself with
    underscores as spaces. (The playlist-listing loops walk the
    same precedence with the maps hoisted out of the loop.)"""
    return catalog.list_names.get(
        key, spotify.playlist_names().get(
            key, (SETTINGS.get("playlist_names") or {}).get(
                key, key.replace("_", " "))))


def start_export_worker(scope=None, folder_name=None, profile="original"):
    """Start the audio export (plan 060 step 6) through the
    supervisor, the share worker's shape: a direct start from a
    host action, not a queued job. The supervisor refuses a second
    start while one runs. A scoped start (plan 063) names one
    playlist key in the runner's environment, the bulk download's
    channel, with the playlist's display name beside it for the
    export folder."""
    with _START_LOCK:
        if update_draining() or SUPERVISOR is None or not RUNNER.is_file():
            return False
        argv, env = runner_invocation("export", ROOT, scope=scope)
        if folder_name:
            env["OFFLINE_MUSIC_EXPORT_NAME"] = folder_name
        env["LISTENING_EXPORT_FORMAT"] = profile
        return SUPERVISOR.start("export", argv, env=env, log=RUNNER_LOG)


def start_migrate_worker():
    """Run the one-time library migration (plan 062) as its own
    worker, through the job queue like reclaim: it rewrites
    coverage and request references and deletes library files, so
    it never runs alongside a library worker."""
    with _START_LOCK:
        if update_draining() or SUPERVISOR is None or not RUNNER.is_file():
            return False
        if any_library_worker_running() or worker_running("migrate"):
            return False
        argv, env = runner_invocation("migrate", ROOT)
        return SUPERVISOR.start("migrate", argv, env=env, log=RUNNER_LOG)


def start_job_worker(kind, scope=None):
    """The dispatcher's starter (system.jobs): one entry for the
    three library workers, scope dict in, started or not out. The
    jobs layer has already re-checked the setup blockers, so a
    False here is a spawn failure, reported on the job's card."""
    if kind == "genres":
        return start_genre_worker((scope or {}).get("job_id"))
    if kind == "reclaim":
        return start_reclaim_worker()
    if kind == "migrate":
        return start_migrate_worker()
    if kind == "downloads":
        scope_id = scope.get("id") if scope and scope.get("type") == "list" else None
        return start_worker("run", force=True, scope=scope_id,
                            download_format=(scope or {}).get("format"),
                            **({"job_id": scope["job_id"]} if scope and "job_id" in scope else {}))
    return start_worker("requests", force=True,
                        **({"job_id": scope["job_id"]} if scope and "job_id" in scope else {}))


def schedule_update_restart():
    """Give the HTTP response time to finish, then launch the updated server."""
    argv = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
    if (os.environ.get("LISTENING_ROOM_SERVICE") == "systemd"
            or os.environ.get("SYSTEMD_EXEC_PID") == str(os.getpid())):
        # Keep systemd's MainPID, cgroup and journal streams. The PID check
        # also covers installed units that predate our explicit service marker.
        threading.Timer(1.0, os.execv, args=(sys.executable, argv)).start()
        return
    spawn_restarter(os.getpid(), argv, REPO_ROOT, STATE / "server.log")
    threading.Timer(1.0, os._exit, args=(0,)).start()


def perform_update():
    """Fast-forward and restart a checkout only after it grants an idle lease."""
    claimed = restarted = False

    def before_merge():
        nonlocal claimed
        claimed = update_drain("claim", lease_seconds=1200)["idle"]
        if not claimed:
            raise UpdateError("Update waiting for idle.")

    try:
        before = updater.current()
        target = updater.update(before_merge=before_merge)
        if target == before:
            return {"status": "You are on the latest version", "current": target, "updating_to": ""}
        rebuild = rebuild_frontend(updater.repo)
        schedule_update_restart()
        restarted = True
        status = f"Updating to {target}; the room will restart"
        if rebuild in (REBUILD_NO_TOOLCHAIN, REBUILD_FAILED):
            status += (". Build the interface to complete the update.")
        return {"status": status, "current": before, "updating_to": target}
    finally:
        if claimed and not restarted:
            update_drain("release")


def _watchdog_pass():
    """One maintenance pass: advance read-path state, then decide.

    The catalog refresh lives here and only here (plan 018 PERF-07):
    GET handlers serve the catalog as of the last pass, so coverage,
    manifest, and genre-store changes become visible within one
    watchdog interval (10 s) instead of being parsed synchronously on
    a request. The write paths that check coverage to make a decision
    keep their own explicit refresh calls, and the paths that force
    freshness through refresh_library() reload the catalog directly.
    """
    with ROOT_LOCK:
        if update_draining():
            return
        publish_phone_imports()
        catalog.refresh()
        advance_library_migration()
        MEDIA.release_playback_buffer(idle_only=True)
        root = ROOT
        if not any_library_worker_running():
            # A settled catalog can still have transfer files from a crash.
            first_pass = not REFRESH.synced()
            if first_pass:
                requests_store().reset_searching()
            startup_transfers = first_pass and (
                any((STATE / "bulk-incoming").glob("*")) or
                any((STATE / "requests").glob("incoming-*-*")) or
                any((STATE / "requests" / "verified").glob("*/incoming-*"))
            )
            retry_free = catalog.bulk_resume_free
            space_recovered = retry_free and min(
                shutil.disk_usage(path).free for path in (root, STATE, LIBRARY) if path.is_dir()
            ) >= retry_free
            if startup_transfers or space_recovered:
                refresh_library(force=True)
            else:
                refresh_library()
            if jobs is not None and jobs.dispatch():
                # The queue had a job waiting: it started (or failed
                # visibly on its card). The standing duties below are
                # the dispatcher's fallback for an empty queue (ADR
                # 0002) and wait for the next pass.
                pass
            elif requests_store().has_queued() and (jobs is None or not jobs.waiting("requests")):
                start_worker("requests", root)
            else:
                position = StateStore(host_database(DB, STATE)).cursor_offset()
                if (SETTINGS.get("auto_download") and download_ready() and position < len(catalog.records)
                        and (jobs is None or not jobs.waiting("downloads"))
                        and os.statvfs(root).f_bavail * os.statvfs(root).f_frsize > 20 * 1024**3):
                    start_worker("run", root)
    if spotify.status()["connected"]:
        spotify.start()


# Watchdog failure record (plan 022): mutated only by the watchdog
# loop, read by activity_snapshot(). The empty form is "healthy".
WATCHDOG_HEALTH = {"last_error": "", "last_error_at": 0.0, "failures": 0}
WATCHDOG_LOGGER = logging.getLogger("listening.watchdog")
_WATCHDOG_LOG_INTERVAL = 60.0
_WATCHDOG_LOG_STATE = {"last_at": 0.0, "suppressed": 0}


def _watchdog_run_once():
    """One watchdog pass with failure recording (plan 022).

    A failed pass never propagates: the failure is recorded in
    WATCHDOG_HEALTH and logged at most once a minute, with failures
    suppressed by the rate limit counted into the next emitted line.
    The first healthy pass after one or more failures logs one
    recovery line and resets the record. KeyboardInterrupt and
    SystemExit are not Exception subclasses and still end the
    process, as they should.
    """
    try:
        _watchdog_pass()
    except Exception as error:
        WATCHDOG_HEALTH["failures"] += 1
        WATCHDOG_HEALTH["last_error"] = f"{type(error).__name__}: {error}"[:300]
        WATCHDOG_HEALTH["last_error_at"] = time.time()
        now = time.monotonic()
        if now - _WATCHDOG_LOG_STATE["last_at"] >= _WATCHDOG_LOG_INTERVAL:
            suppressed = _WATCHDOG_LOG_STATE["suppressed"]
            _WATCHDOG_LOG_STATE["suppressed"] = 0
            _WATCHDOG_LOG_STATE["last_at"] = now
            WATCHDOG_LOGGER.warning(
                "watchdog pass failed: %s (failures: %d, suppressed since last log line: %d)",
                WATCHDOG_HEALTH["last_error"], WATCHDOG_HEALTH["failures"], suppressed)
        else:
            _WATCHDOG_LOG_STATE["suppressed"] += 1
        return
    if WATCHDOG_HEALTH["failures"]:
        WATCHDOG_LOGGER.info("watchdog recovered after %d failures",
                             WATCHDOG_HEALTH["failures"])
        WATCHDOG_HEALTH.update({"last_error": "", "last_error_at": 0.0, "failures": 0})
        _WATCHDOG_LOG_STATE["suppressed"] = 0


PHONE_IMPORTS = threading.Event()
MAINTENANCE_WAKE = threading.Event()


def publish_phone_imports():
    """Publish a batch of committed originals. A restart checks unindexed owners again."""
    if not PHONE_IMPORTS.is_set():
        return
    try:
        with ROOT_LOCK:
            PHONE_IMPORTS.clear()
            store, current = MEDIA.store(), catalog
            if store is None:
                return
            environment = runner_environment()
        with REFRESH_LOCK, _START_LOCK:
            if any_library_worker_running():
                PHONE_IMPORTS.set()
                return
            keys = store.phone_keys()
            if not keys:
                return
            published = {r["key"] for r in current.records if r.get("active", True)
                         and current.coverage.get(r["index"], {}).get("status") == "verified"}
            if current is catalog and keys - published:
                rebuild_library(environment)
                current.reload()
    except Exception:
        PHONE_IMPORTS.set()
        raise


def maintenance_watchdog():
    """Refresh changed lists and keep one downloader worker running."""
    while True:
        _watchdog_run_once()
        MAINTENANCE_WAKE.wait(10)
        MAINTENANCE_WAKE.clear()


catalog = None  # Built by init().


def member_collection_identity(root):
    portable = root / "collection.sqlite3"
    return "collection:" + offline_replica.collection_id(portable) if portable.is_file() else str(root)


def apply_root(root):
    # STATE and DB are deliberately not recomputed here (plan 043):
    # they name the application folder, set once in init() from
    # CONFIG, and a library-folder change must never move the state.
    global ROOT, LIBRARY, PLAYLIST_OUTPUT, catalog, MEDIA
    member_profiles.select_room(root, member_collection_identity(root))
    ROOT = root
    PHONE_IMPORTS.set()
    CONFIG.root = root
    MEDIA = MediaAccess(CONFIG.media_database, CONFIG.buffer_dir, STATE / "export-status.json", _MediaSources())
    LIBRARY = Path(SETTINGS.get("music_folder") or root / "audio").expanduser().resolve()
    PLAYLIST_OUTPUT = Path(SETTINGS.get("playlist_output") or root / "playlists").expanduser().resolve()
    catalog = Catalog(catalog_paths())
    playback.reset()


DEVICES = Presence(time.time, ttl=45, limit=512)
DEVICES_LOCK = threading.Lock()
ANNOUNCEMENTS = AnnouncementBoard()


def device_snapshot():
    with DEVICES_LOCK:
        recent = {}
        for item in DEVICES.active().values():
            recent[item["id"]] = item
        return sorted(recent.values(), key=lambda item: item["seen_at"], reverse=True)



def save_settings():
    CONFIG.save()


def write_sockseek_credentials(username, password):
    if not username or not password or any("\n" in x or "\r" in x for x in (username, password)):
        raise ValueError("Enter a Soulseek username and password without line breaks")
    target = Path(SETTINGS["credentials_path"])
    private_write(target, f"username = {username}\npassword = {password}\n")


def share_config_path():
    return CONFIG_PATH.parent / "nicotine-share.conf"


def write_share_profile(username, password, folder):
    if not username or not password or any("\n" in x or "\r" in x for x in (username, password)):
        raise ValueError("Enter a separate sharing account and password")
    config = configparser.ConfigParser(interpolation=None)
    config.read(share_config_path())
    for section in ("server", "transfers"):
        if not config.has_section(section):
            config.add_section(section)
    config.set("server", "login", username)
    config.set("server", "passw", password)
    config.set("transfers", "shared", repr([("Listening Room", str(folder))]))
    from io import StringIO
    serialized = StringIO()
    config.write(serialized)
    private_write(share_config_path(), serialized.getvalue())


def settings_snapshot(port, request_host=None, bind_host="0.0.0.0"):
    files = sorted(manifest_signature(source_dir()))
    collection = None
    if DB == ROOT / "collection.sqlite3":
        collection = {"path": str(DB), "bytes": DB.stat().st_size,
                      "list_workspace": str(source_dir()), "export_folder": str(shared_music.export_folder(ROOT))}
    link = lan_address(port, request_host, bind_host)
    network = ""
    if link:
        selected_network = os.environ.get("LISTENING_ROOM_NETWORK")
        if selected_network in {"lan", "tailscale", "wireguard"}:
            network = selected_network
        elif home_server():
            network = "home"
        else:
            network = "tailscale" if is_tailnet_host(urlparse(link).hostname) else "lan"
    return {"collection": collection, "folder": str(ROOT), "lists_folder": str(configured_lists()), "csv_folder": str(configured_lists()),
            "source_folders": [{"path": path, "count": len(list_files(path))}
                               for path in SETTINGS.get("source_folders", [])],
            "source_files": [{"path": path, "available": Path(path).is_file()}
                             for path in SETTINGS.get("source_files", [])],
            "version": updater.current() if updater is not None else "",
            "install_kind": install_kind(),
            "desktop_player_available": bool(desktop_player_opener()),
            "managed_updates": (install_kind() == "docker"
                                and (REQUIRE_OWNER or (owner_auth is not None and owner_auth.enabled))
                                and container_updates.available()),
            "frontend_rebuild_needed": (install_kind() == "checkout"
                                        and frontend_build_needed(REPO_ROOT)),
            "music_folder": str(LIBRARY), "playlist_output": str(PLAYLIST_OUTPUT),
            "share_folder": SETTINGS.get("share_folder", ""),
            "share_username": SETTINGS.get("share_username", ""),
            "sockseek_binary": SETTINGS["sockseek_binary"],
            "nicotine_binary": SETTINGS.get("nicotine_binary", ""),
            "has_credentials": Path(SETTINGS["credentials_path"]).is_file(),
            "has_share_credentials": share_config_path().is_file(),
            "download_ready": download_ready(), "auto_download": bool(SETTINGS.get("auto_download")),
            "audio_store": SETTINGS.get("audio_store", "database"),
            "bulk_download_format": bulk_download_format(SETTINGS.get("bulk_download_format")),
            "media": MEDIA.facts(),
            "export_status": MEDIA.export_status(),
            "list_files": files, "csv_files": files, "lan_url": link, "pairing_network": network,
            "local_url": f"http://127.0.0.1:{port}/",
            "onboarding": not files,
            "spotify": spotify.status(),
            "spotify_redirect_uri": f"http://127.0.0.1:{port}/api/spotify/callback",
            "dependencies": {"ffprobe": bool(shutil.which("ffprobe")),
                             "sockseek": Path(SETTINGS["sockseek_binary"]).is_file(),
                             "nicotine": Path(SETTINGS.get("nicotine_binary", "")).is_file()}}


def redact_settings(snapshot):
    """The settings payload a paired (non-host) caller may see.

    Plan 010 (A2 S-01): the full snapshot carries host filesystem paths,
    binary locations, and the token-bearing pairing URL. Paired devices
    legitimately read settings state (the Settings view, onboarding,
    dependency and credential presence), so non-host callers get the
    same shape with every host-local value blanked instead of a refusal.
    """
    redacted = dict(snapshot)
    redacted["desktop_player_available"] = False
    redacted["pairing_network"] = ""
    redacted["collection"] = None
    if "destination" in snapshot["export_status"]:
        redacted["export_status"] = {**snapshot["export_status"], "destination": ""}
    for key in ("folder", "lists_folder", "csv_folder", "music_folder",
                "playlist_output", "share_folder", "sockseek_binary",
                "nicotine_binary", "lan_url", "local_url", "version"):
        redacted[key] = ""
    redacted["source_folders"] = [{"path": "", "count": entry["count"]}
                                  for entry in snapshot["source_folders"]]
    redacted["source_files"] = [{"path": "", "available": entry["available"]}
                                for entry in snapshot["source_files"]]
    return redacted


def recent_process_lines():
    paths = [RUNNER_LOG]
    batches = list(STATE.glob("batch-*.log"))
    if batches:
        paths.append(max(batches, key=lambda path: path.stat().st_mtime_ns))
    rows = []
    for path in paths:
        if not path.is_file():
            continue
        for line in tail_lines(path, 35):
            rows.append(f"{path.name}: {line[:300]}")
    return rows[-60:]


def resolve_ref(ref):
    return audio_resolution.resolve_ref(ref, catalog, requests_store())


# The descriptive probe fields the player's quality readout and the
# Normalize setting consume (plan 042 stage 6): the file's codec,
# sample rate, and bit depth, and its ReplayGain values. Descriptive
# only; verification never reads them.
FACT_FIELDS = ("codec", "sample_rate", "bit_depth", "track_gain",
               "album_gain", "track_peak", "album_peak",
               "r128_track_gain")


def audio_facts(index):
    """The probe facts for a verified catalog track, {} when unknown.

    Reads the runner's probe cache through the state store; an entry
    counts only while its stamp matches the file and its shape
    version is current. A missing or stale entry probes the file
    once and writes the fresh entry back, so repeat reads are cache
    reads. Only the fields the probe actually found are returned.
    """
    row = catalog.coverage.get(index, {})
    reference = row.get("path", "")
    if row.get("status") == "verified" and is_blob_ref(reference):
        store = MEDIA.store()
        media = (store.probe(blob_key(reference)) if store else None) or {}
        return {field: media[field] for field in FACT_FIELDS
                if media.get(field) not in (None, "", 0)}
    store, buffer = MEDIA.serving()
    path = audio_resolution.track_file(index, catalog, LIBRARY, store, buffer)
    if path is None:
        return {}
    try:
        stat = path.stat()
    except OSError:
        return {}
    stamp = [stat.st_size, stat.st_mtime_ns]
    store = StateStore(host_database(DB, STATE))
    entry = store.probe_entry(str(path))
    if entry and entry.get("stamp") == stamp \
            and entry.get("version") == PROBE_CACHE_VERSION \
            and isinstance(entry.get("media"), dict):
        media = entry["media"]
    else:
        media = probe(path)
        if media is None:
            return {}
        store.put_probe(str(path), {"stamp": stamp,
                                    "version": PROBE_CACHE_VERSION,
                                    "media": media})
    return {field: media[field] for field in FACT_FIELDS
            if media.get(field) not in (None, "", 0)}


def with_audio_facts(snapshot):
    """A session snapshot with the current track's probe facts
    merged into its track dict (plan 042 stage 6). The snapshot's
    dicts are built fresh by resolve_ref on every call, so the
    merge mutates no shared state."""
    current = snapshot.get("current")
    if isinstance(current, dict) and type(current.get("id")) is int:
        current.update(audio_facts(current["id"]))
    return snapshot


def record_play_event(kind, ref, device_id, listened):
    """The recorder PlaybackSession emits into (plan 045).

    Owns the ref-to-row derivation for one play event: a catalog ref
    gains its index and stable record key, a custom ref its request
    id; the store itself does not resolve. Reads the catalog global
    at event time, so a rebuild between plays cannot hand it a stale
    record list; an out-of-range ref records with a NULL key rather
    than failing.
    """
    track_index = record_key = request_id = None
    if isinstance(ref, int):
        track_index = ref
        record = catalog.record_at(ref)
        if record is not None:
            record_key = record["key"]
    elif isinstance(ref, str) and ref.startswith("custom-"):
        try:
            request_id = int(ref[7:])
        except ValueError:
            request_id = None
    play_events.record(kind, str(ref), track_index, record_key,
                       request_id, device_id, listened)


def available_tracks(playlist):
    if playlist != "all" and playlist not in catalog.playlists:
        raise ValueError("Unknown playlist")
    indices = range(1, len(catalog.records) + 1) if playlist == "all" else catalog.playlists[playlist]
    return [index for index in indices if catalog.active_record_at(index) is not None
            and catalog.track(index)["status"] == "verified"]


def ready_tracks_by_genre():
    tracks = {}
    for index, record in enumerate(catalog.records, 1):
        if not record.get("active", True) or catalog.status_of(index) != "verified":
            continue
        genre = catalog.genre_canonical(record)
        if genre:
            tracks.setdefault(genre, []).append(index)
    return tracks


def selected_playback_tracks(body, *, custom=True):
    if body.get("op") == "play_mood":
        return mood_queue(body)
    if body.get("op") != "play_album":
        return None
    refs = body.get("track_ids")
    if (not isinstance(refs, list) or not 1 <= len(refs) <= 120
            or any(type(ref) is not int and not (custom and isinstance(ref, str)
                       and re.fullmatch(r"custom-[1-9][0-9]{0,17}", ref)) for ref in refs)):
        raise ValueError("Choose up to 120 verified album tracks.")
    return [ref for ref in dict.fromkeys(refs)
            if (catalog.active_record_at(ref) is not None and catalog.status_of(ref) == "verified"
                if type(ref) is int else resolve_ref(ref) is not None)]


def mood_queue(body):
    first, second, genre = moods.resolve(moods.groups(DB), body.get("mood"),
                                         body.get("blend", ""), body.get("genre", ""))
    weight = body.get("blend_percent", 0)
    if type(weight) is not int or not (1 <= weight <= 50 if second else weight == 0):
        raise ValueError("Choose a blend between 1 and 50 percent.")
    available = ready_tracks_by_genre()

    def pool(group):
        tags = [genre] if genre else group["genres"]
        return [ref for tag in tags for ref in available.get(tag, ())]

    refs = moods.shuffled_mix(pool(first), pool(second) if second and not genre else (), weight)
    if not refs:
        raise moods.EmptyMix("No ready songs match this mood")
    return refs


def _ref_genre(ref):
    """The canonical genre of a session ref ("" for custom requests)."""
    if isinstance(ref, int):
        record = catalog.record_at(ref)
        if record is not None:
            return catalog.genre_canonical(record)
    return ""


def genre_flow_order(refs, current_ref):
    """The playable order for Genre flow (plan 029): the current
    track's genre first, then the full ready pool, assembled here from
    the catalog so the playback session stays genre-agnostic."""
    return group_genre_flow(refs, _ref_genre(current_ref), _ref_genre)


def genre_fields(record):
    """The stored genre of a catalog record, for API payloads."""
    entry = catalog.genre_entry(record) or {}
    return {"genre": entry.get("genre", ""), "genre_canonical": entry.get("canonical", ""),
            "genre_source": entry.get("source", ""),
            "genre_source_label": source_label(entry.get("source", ""))}


def playlist_source(key, kinds=None, owned=None):
    """Where a playlist comes from (plan 046): "spotify" when the
    adapter's sync state owns it, "linked" or "watched" when a
    current source setting owns its mirror, else "local". Derived
    from state at call time, never stored and never guessed from
    the file name: a mirror whose source leaves the settings is
    local from that moment on. Callers naming many playlists pass
    the kinds and owned sets they already computed, so watched
    folders are walked once per request, not once per playlist."""
    if key.startswith("linked-") and playlist_links.is_group(DB, key):
        return "linked"
    if owned is None:
        owned = spotify.playlist_keys()
    if key in owned:
        return "spotify"
    if kinds is None:
        kinds = source_mirror_kinds(SETTINGS, source_dir())
    return kinds.get(key, "local")


def spotify_destinations():
    """Map displayed playlists to proven provider mirrors, including projected groups."""
    mirrors = spotify.playlist_links()
    targets = {key: {key: link} for key, link in mirrors.items()}
    if mirrors:
        for group, keys in playlist_links.grouped_sources(DB).items():
            matched = {key: mirrors[key] for key in keys if key in mirrors}
            if matched:
                targets[group] = matched
    return targets


def playlist_access(source, destinations, names, allowed):
    if not destinations:
        return {"can_add": allowed and source == "local"}
    links = []
    for key, dest in destinations.items():
        link = {"mode": dest["mode"], "name": names.get(key, key)}
        if allowed:
            link.update(account_id=dest["account_id"], playlist_id=dest["playlist_id"])
        links.append(link)
    return {"can_add": allowed and any(item["mode"] == "two_way" for item in links),
            "spotify_link": links[0] if len(links) == 1 else {"mode": "multiple", "targets": links}}


def playlist_options():
    """The downloads job's scope choices: every playlist with the
    display name /api/overview gives it, computed the same way."""
    spotify_names = spotify.playlist_names()
    owner_names = SETTINGS.get("playlist_names") or {}
    return [{"id": key,
             "name": catalog.list_names.get(key, spotify_names.get(
                 key, owner_names.get(key, key.replace("_", " "))))}
            for key in catalog.playlists]


playback = None  # Built by init().
play_events = None  # Built by init().
favorites = None  # Built by init().


def spotify_source_removed(path, expected_hash):
    projection = list_projection()
    if projection is not None and path.parent == projection.directory:
        projection.remove_source(path.name, expected_hash)


def spotify_library_changed():
    with PLAYLIST_EDIT_LOCK:
        with REFRESH_LOCK:
            publish_adapter_edits()
        if not worker_running("downloads") and not worker_running("requests"):
            refresh_library()


spotify = None  # Built by init().


metadata = None  # Built by init().
artist_connections = None


updater = None  # Built by init().


jobs = None  # Built by init() (plan 040, plan 042 Stage 7).


device_store = None  # Built by init() (plan 021a).
owner_auth = None  # Built by init().
member_profiles = None  # Built by init().
ROOM_ORIGIN = ""
REQUIRE_OWNER = False
OWNER_SELECTION_COOKIE = "lr_owner_selected"


def owner_selection(token):
    return hmac.digest(token.encode(), b"listening-owner-selection", "sha256").hex()


def lan_address(port, request_host=None, bind_host="0.0.0.0"):
    origin = ROOM_ORIGIN
    if not origin:
        authority = invitation_authority(request_host, port, bind_host)
        origin = f"http://{authority}" if authority else ""
    if not origin:
        return ""
    return f"{origin}/?token={SETTINGS['share_token']}"


def activity_snapshot():
    summary = catalog.summary
    bulk = worker_running("downloads")
    requests = worker_running("requests")
    genres_running = worker_running("genres")
    free_gib = summary.get("free_gib", 0)
    total = sum(record.get("active", True) for record in catalog.records)
    attempted = summary.get("attempted", 0)
    if bulk:
        status = "Downloading library"
    elif requests:
        status = "Searching requests"
    elif genres_running:
        status = "Categorizing genres"
    elif free_gib and free_gib < 20:
        status = "Waiting for free space"
    elif attempted >= total and total:
        status = "Library scanned"
    else:
        status = "Downloader restarting" if RUNNER.is_file() else "Downloader unavailable"
    files = list(STATE.glob("batch-*.log"))
    recent = recent_events(STATE, limit=6, batches=1)
    batch = ""
    if files:
        latest = max(files, key=lambda path: path.stat().st_mtime_ns)
        batch = latest.stem.removeprefix("batch-").replace("-", "–")
    categorized = sum(1 for record in catalog.records
                      if record.get("active", True) and catalog.genre_entry(record))
    return {"status": status, "running": bulk or requests or genres_running, "attempted": attempted,
            "total": total, "verified": summary.get("verified", 0), "free_gib": free_gib,
            "batch": batch if bulk else "", "recent": recent if bulk else [],
            "queued_requests": count_queued_requests(),
            "genres": {"categorized": categorized, "total": total, "running": genres_running},
            "watchdog": dict(WATCHDOG_HEALTH)}


def count_queued_requests():
    return requests_store().active_count()


def desktop_player_opener():
    if install_kind() == "docker":
        return None
    if sys.platform == "darwin":
        try:
            if os.stat("/dev/console").st_uid != os.geteuid():
                return None
        except OSError:
            return None
        return shutil.which("open")
    if sys.platform.startswith("linux") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return shutil.which("xdg-open")
    return None


def source_audio(ref):
    with ROOT_LOCK:
        store, buffer = MEDIA.serving()
        return audio_resolution.source_file(ref, catalog, requests_store(), LIBRARY, store, buffer)


DIST = FRONTEND / "dist"
DIST_PUBLIC = {"index.html", "about/index.html", "manifest.webmanifest", "service-worker.js",
               "licenses/Figtree-OFL.txt"}

# Text assets worth gzipping for a paired client's first load
# (plan 025). The built files change only on a rebuild, so the
# compressed bytes are cached per file version instead of being
# recomputed on every request.
_COMPRESSIBLE_SUFFIXES = {".js", ".css", ".html", ".webmanifest", ".svg"}
GZIP_JSON_MIN_BYTES = 512
GZIP_STATIC_MIN_BYTES = 1024
_GZIP_CACHE = {}


def _gzip_cached(target, payload):
    stat = target.stat()
    key = (str(target), stat.st_mtime_ns, stat.st_size)
    compressed = _GZIP_CACHE.get(key)
    if compressed is None:
        if len(_GZIP_CACHE) > 32:
            _GZIP_CACHE.clear()
        compressed = gzip.compress(payload, compresslevel=6)
        _GZIP_CACHE[key] = compressed
    return compressed


def dist_relative(path):
    if path in {"", "/"}:
        return "index.html"
    if path in {"/about", "/about/"}:
        return "about/index.html"
    return path.lstrip("/")


def is_public_static(relative):
    """The static allowlist serve_static serves; the bootstrap tier
    of authorized() admits exactly the same set (plan 021a). Since
    the plan 042 cutover the set is the built frontend's dist set:
    the two HTML entry points, manifest and service worker, and anything
    under the dist assets/ prefix."""
    return relative in DIST_PUBLIC or relative.startswith("assets/")


MEMBER_GET = {"/api/room", "/api/overview", "/api/tracks", "/api/settings", "/api/session", "/api/radio/search", "/api/radio/library", "/api/stations",
              "/api/status", "/api/activity", "/api/discovery/sources", "/api/extensions/sources", "/api/hiking", "/api/insights",
              "/api/jams", "/api/jams/session"}
JAM_ROUTES = {"/api/jams", "/api/jams/session"}
PERSONAL_ROUTES = {"/api/my/playlists", "/api/my/session", "/api/my/radio"}
MEMBER_POST = {"/api/radio/presence", "/api/session", "/api/favorites", "/api/device", "/api/discovery/search", "/api/jams"} | PERSONAL_ROUTES
MEMBER_TRACK_URL = re.compile(r"/api/(audio|metadata|lyrics)/([1-9][0-9]{0,9})")
PRIVATE_PLAYLIST = re.compile(r"private:([a-f0-9]{32})")


def hiking_origin():
    try:
        origin = configured_origin(os.environ.get("LISTENING_ROOM_HIKING_ORIGIN", ""))
    except ValueError:
        return ""
    return origin if origin.startswith("https://") else ""


def member_playlist(key):
    # A projected group can contain Spotify sources under a new key.
    return not key.startswith(("spotify_", "linked-"))


def member_record(index, current=None):
    current = catalog if current is None else current
    record = current.active_record_at(index)
    if record is not None and current.status_of(index) != "verified":
        memberships = current.memberships.get(index, ())
        if not any(member_playlist(key) for key in memberships):
            return None
    return record


def member_catalog_track(index, favorite_keys=()):
    current = catalog
    record = member_record(index, current)
    if record is None:
        return None
    track = current.track(index)
    track.update(favorite=any(key in favorite_keys for key in current.recording_keys(record)),
                 spotify_url="", reason="", playlists=[key for key in track["playlists"] if member_playlist(key)])
    if track["status"] != "verified":
        track["status"] = "pending"
    return track


def member_summary():
    counts = dict.fromkeys(("verified", "unmatched", "pending", "rejected"), 0)
    for index in range(1, len(catalog.records) + 1):
        if member_record(index) is not None:
            status = catalog.status_of(index)
            counts[status if status in counts else "pending"] += 1
    counts["total"] = sum(counts.values())
    counts["attempted"] = counts["total"] - counts["pending"]
    counts["playlists"] = {key: values for key, values in catalog.summary["playlists"].items() if member_playlist(key)}
    return counts


class Handler(BaseHTTPRequestHandler):
    server_version = "ListeningRoom/1.2"

    def log_message(self, format, *args):
        path = getattr(self, "path", "")
        request = getattr(self, "requestline", "")
        if any(fragment in request or fragment in path for fragment in ("/rest/", "lr_sub_", "apiKey=", "token=")):
            return
        if not path.startswith((
                "/api/room", "/api/status", "/api/session", "/api/insights", "/api/activity", "/api/spotify/callback")):
            super().log_message(format, *args)

    def _subsonic(self, parsed):
        query = {}
        method = ""
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=40)
            match = opensubsonic.ROUTE.fullmatch(parsed.path)
            if match is None:
                raise opensubsonic.SubsonicError(70, "Unknown endpoint.")
            method, = match.groups()
            output = opensubsonic.argument(query, "f", "xml")
            if output not in {"xml", "json"}:
                raise opensubsonic.SubsonicError(10, "Choose XML or JSON.")
            if method == "getOpenSubsonicExtensions":
                payload = {"openSubsonicExtensions": [{"name": "apiKeyAuthentication", "versions": [1]}]}
            else:
                old_auth = {"u", "p", "t", "s"}.intersection(query)
                if "apiKey" in query and old_auth:
                    raise opensubsonic.SubsonicError(43, "Multiple authentication mechanisms.")
                if old_auth:
                    raise opensubsonic.SubsonicError(41 if "t" in old_auth else 42, "Use an OpenSubsonic API key.")
                key = opensubsonic.argument(query, "apiKey", "")
                if owner_auth is None or not owner_auth.verify_stream_key(key):
                    raise opensubsonic.SubsonicError(44, "Invalid API key.")
                if method == "ping":
                    payload = {}
                elif method == "stream":
                    identifier = opensubsonic.argument(query, "id")
                    if not re.fullmatch(r"[1-9][0-9]{0,9}", identifier):
                        raise opensubsonic.SubsonicError(70, "Song not found.")
                    if opensubsonic.argument(query, "format", "raw") != "raw":
                        raise opensubsonic.SubsonicError(0, "Only original audio streams are available.")
                    handle, name = self._subsonic_audio(int(identifier))
                    with handle:
                        return self.send_audio_range(handle, name)
                elif method in opensubsonic.METADATA_METHODS:
                    with ROOT_LOCK, REFRESH_LOCK:
                        payload = opensubsonic.Library(catalog, member_playlist).dispatch(method, query)
                else:
                    raise opensubsonic.SubsonicError(50, "This endpoint is outside the read-only library.")
        except opensubsonic.SubsonicError as error:
            payload = {"error": {"code": error.code, "message": str(error)}}
        except ValueError:
            payload = {"error": {"code": 10, "message": "Invalid request."}}
        except (OSError, sqlite3.Error):
            payload = {"error": {"code": 0, "message": "Library unavailable."}}
        content_type, data = opensubsonic.response(payload, method != "stream" and query.get("f") == ["json"])
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def _subsonic_audio(index):
        with ROOT_LOCK, REFRESH_LOCK:
            if catalog.active_record_at(index) is not None and catalog.status_of(index) == "verified":
                store, buffer = MEDIA.serving()
                audio = audio_resolution.track_file(index, catalog, LIBRARY, store, buffer)
                if audio is not None:
                    try:
                        return audio.open("rb"), audio.name
                    except FileNotFoundError:
                        pass
        raise opensubsonic.SubsonicError(70, "Song unavailable.")

    def _client_is_loopback(self):
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _cookie(self, name):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            return cookie[name].value if name in cookie else ""
        except Exception:
            return ""

    def _presented_cookie(self):
        return self._cookie("lr_token")

    def _explicit_owner(self):
        return REQUIRE_OWNER or (owner_auth is not None and owner_auth.enabled)

    def _auth_tier(self):
        if getattr(self, "_request_role", None) is None:
            self._request_role = self._resolve_role()
        return self._request_role

    def _resolve_role(self):
        if self._legacy_local_owner():
            return "host"
        member_cookie = self._cookie(MEMBER_COOKIE_NAME)
        if owner_auth is not None and owner_auth.enabled:
            try:
                scheme, _, token = self.headers.get("Authorization", "").partition(" ")
                if self._client_is_loopback() and scheme.lower() == "bearer" and owner_auth.verify_cli(token):
                    return "control"
                if member_cookie:
                    self._member_account = owner_auth.household.session(member_cookie)
                    if self._member_account:
                        return "member"
                owner_cookie = self._cookie(COOKIE_NAME)
                if member_cookie and not hmac.compare_digest(
                    self._cookie(OWNER_SELECTION_COOKIE).encode(), owner_selection(owner_cookie).encode()
                ):
                    return ""
                self._owner_username = owner_auth.session_username(owner_cookie)
                if self._owner_username:
                    return "host"
            except (OSError, sqlite3.Error):
                # A damaged or missing owner store never grants host privileges.
                pass
        if member_cookie:
            return ""
        cookie = self._presented_cookie()
        if cookie:
            device = device_store.verify(cookie) if device_store is not None else None
            if device is not None:
                self._authenticated_device = device["id"]
                return "device"
            if cookie.isascii() and hmac.compare_digest(cookie, SETTINGS["share_token"]):
                return "bootstrap"
        return ""

    def _traffic_key(self, output):
        if not valid_output_id(output):
            return None
        role = self._auth_tier()
        if role == "member":
            return "member", self._member_account.id, output
        if role == "device":
            return "device", self._authenticated_device, output
        if role in {"host", "control"}:
            return "owner", output
        return None

    def _is_owner(self):
        return self._auth_tier() in {"host", "control"}

    def _can_add_room_song(self):
        if self._is_owner():
            return True
        if self._auth_tier() != "device":
            return False
        record = device_store.verify(self._presented_cookie())
        return bool(record and record.get("playlist_access"))

    def _account_matches(self, path):
        expected = self.headers.get_all("X-Listening-Account", [])
        if not expected or not path.startswith("/api/") or path in {"/api/account", "/api/account/login"}:
            return True
        role = self._auth_tier()
        if not role:
            return True
        if role == "member":
            account = self._member_account.id
        elif self._is_owner() and self._explicit_owner():
            account = "owner"
        else:
            account = "paired" if role in {"host", "device", "bootstrap"} else None
        if expected == [account]:
            return True
        self.json_response({"code": "account_changed", "error": "The room account changed. Reload this tab."},
                           HTTPStatus.CONFLICT)
        return False

    def _member_profile(self):
        return (member_profiles.profile(self._member_account.id)
                if self._auth_tier() == "member" else None)

    def _personal_profile(self):
        role = self._auth_tier()
        if role == "member":
            return self._member_profile()
        if role == "host" and self._owner_username:
            return member_profiles.profile("owner")
        return None

    @staticmethod
    def _private_tracks(profile, playlist):
        keys = {item for item in playlist["tracks"] if isinstance(item, str)}
        drafts = {playlist_edits.title_artist(item) for item in playlist["tracks"] if isinstance(item, dict)}
        candidates = {}
        owner = profile.scope[1] == "owner"
        records = {}
        for index, record in enumerate(catalog.records, 1):
            tags = playlist_edits.title_artist(record) if drafts else None
            if record.get("key") in keys or tags in drafts:
                visible = catalog.active_record_at(index) if owner else member_record(index)
                if visible is not None:
                    records[record["key"]] = {
                        "id": index, "title": record["title"], "artist": record["artist"],
                        "ready": catalog.status_of(index) == "verified",
                    }
                    if tags in drafts:
                        candidates.setdefault(tags, []).append(record)
        result = []
        bindings = {}
        for position, entry in enumerate(playlist["tracks"]):
            if isinstance(entry, str):
                result.append(records.get(entry))
            else:
                matches = playlist_edits.matching_metadata(candidates.get(playlist_edits.title_artist(entry), ()), entry)
                if len(matches) == 1:
                    key = matches[0]["key"]
                    bindings[position] = key
                    result.append(records[key])
                else:
                    result.append({"id": None, **entry, "ready": False})
        if bindings:
            profile.bind_recordings(playlist, bindings)
        return result

    def _member_player(self, profile, *, owner=False):
        def resolve(ref):
            if owner:
                return resolve_ref(ref)
            track = member_catalog_track(ref)
            return track if track is not None and track["status"] == "verified" else None

        def available(playlist):
            private = PRIVATE_PLAYLIST.fullmatch(playlist)
            if private:
                try:
                    tracks = Handler._private_tracks(profile, profile.playlist(private[1]))
                except KeyError:
                    raise ValueError("Unknown playlist") from None
                return [track["id"] for track in tracks if track and track["ready"]]
            if owner or member_playlist(playlist):
                return available_tracks(playlist)
            raise ValueError("Unknown playlist")

        def key(ref):
            record = catalog.active_record_at(ref)
            return record["key"] if record is not None else None

        return member_profiles.session(profile, available, resolve, genre_flow_order, key)

    def _jam_playback(self, peer, source, *, audible=True, favorite_keys=()):
        _, _, account, output_id = peer
        player = playback if source == "room" else self._member_player(
            member_profiles.profile(account), owner=account == "owner")
        session = player.observe(output_id)
        ref = session["current_ref"]
        # A Jam can expose only verified library tracks available to members.
        track = member_catalog_track(ref, favorite_keys) if audible and type(ref) is int else None
        if track and track["status"] != "verified":
            track = None
        session.update(current=track, current_ref=ref if track else None,
                       queue_length=0, upcoming=[], genre_flow=False, shuffle=False, output_selected=True)
        session["playing"] = bool(track and session["playing"])
        session["session_playing"] = session["playing"]
        return session

    def _guest_playback(self, host, viewer):
        peer, listener = host
        favorites = catalog.favorites if viewer.scope[1] == "owner" else viewer.keys()
        return self._jam_playback(peer, listener.source, audible=listener.playing, favorite_keys=favorites)

    def _jam_request(self, path, parameters, body=None):
        profile = self._personal_profile()
        name = self._owner_username or (self._member_account.username if self._member_account else "")
        output = output_from(self.headers)
        proxied = any(self.headers.get(header) for header in ("Forwarded", "X-Forwarded-For", "X-Real-IP"))
        network = None if proxied else nearby_network(
            self.client_address[0], parse_host(self.headers.get("Host")))
        if not profile or not network or not output or output[2] is None:
            if path == "/api/jams" and body is None:
                return self.json_response({"available": False, "sharing": False, "hosting": False, "people": [], "invites": []})
            return self.error_json("Jams are available to signed-in listeners on the same local network.",
                                   HTTPStatus.FORBIDDEN)
        key = (profile.scope[0], network, profile.scope[1], output[0])
        epoch = output[2]
        try:
            with ROOT_LOCK:
                if not member_profiles.selected(profile):
                    raise ValueError("The room changed. Try again.")
                if path == "/api/jams/session":
                    host = jams.follow(key, epoch, parameters.get("id", [""])[0])
                    result = self._guest_playback(host, profile) if host else None
                else:
                    result = self._jam_change(key, epoch, name, body, profile)
        except ValueError as error:
            return self.error_json(str(error))
        except sqlite3.Error:
            return self.error_json("Nearby listening is unavailable. Try again.", HTTPStatus.SERVICE_UNAVAILABLE)
        if result is None:
            return self.error_json("That Jam has ended or is no longer nearby.", HTTPStatus.GONE)
        return self.json_response(result)

    def _jam_change(self, key, epoch, name, body, profile):
        if body is None:
            return jams.view(key)
        if not isinstance(body, dict):
            raise ValueError("Choose a Jam action.")
        operation = body.get("op")
        if operation in ("share", "pulse", "start"):
            source = "room" if key[2] == "owner" and body.get("source") == "room" else "personal"
            following = body.get("following")
            if following and operation == "start":
                raise ValueError("Leave your current Jam before hosting.")
            host = jams.follow(key, epoch, following) if following else None
            session = None
            if body.get("listening") is True:
                if host:
                    session = self._jam_playback(host[0], host[1].source, audible=host[1].playing)
                elif not following:
                    session = self._jam_playback(key, source)
            song = ({field: session["current"][field] for field in ("title", "artist")}
                    if session and session["playing"] else None)
            jams.publish(key, epoch, name, source, song, enabled=operation == "share")
            if operation == "start":
                jams.start(key)
        elif operation == "hide":
            jams.stop_sharing(key)
        elif operation == "end":
            jams.end(key)
        elif operation == "join":
            host = jams.join(key, epoch, body.get("id"))
            if host:
                session = self._guest_playback(host, profile)
                if not session["playing"]:
                    jams.leave(key, body.get("id"))
                    return None
                jams.end(key)
                return session
            return None
        elif operation == "leave":
            jams.leave(key, body.get("id"))
        else:
            raise ValueError("Choose a Jam action.")
        return jams.view(key)

    def _member_session_snapshot(self, profile, body=None):
        with ROOT_LOCK:
            # A room change between authorization and playback must not
            # write this room's track references into the previous room.
            if not member_profiles.selected(profile):
                raise ValueError("The room changed. Try again.")
            if body is not None:
                operation = body.get("op")
                if not isinstance(operation, str):
                    raise ValueError("Choose a player action.")
                if operation in {"play", "play_available"} and not isinstance(body.get("playlist", "all"), str):
                    raise ValueError("Unknown playlist.")
            owner = profile.scope[1] == "owner"
            player = self._member_player(profile, owner=owner)
            output = output_from(self.headers)
            snapshot = player.snapshot(output=output) if body is None else player.apply(
                body, output=output, mood_refs=selected_playback_tracks(body, custom=owner))
            if owner:
                return with_audio_facts(snapshot)
            favorite_keys = profile.keys()
            for track in (snapshot.get("current"), *snapshot.get("upcoming", [])):
                if track is not None:
                    record = catalog.active_record_at(track["id"])
                    track["favorite"] = bool(record and any(key in favorite_keys for key in catalog.recording_keys(record)))
            return with_audio_facts(snapshot)

    def _member_can_read(self, path):
        if (path in MEMBER_GET or path in PERSONAL_ROUTES
                or is_public_static(dist_relative(path))):
            return True
        connection = CONNECTION_PATH.fullmatch(path)
        if connection or PROFILE_PATH.fullmatch(path):
            # The handler checks household visibility for library IDs.
            return True
        track = MEMBER_TRACK_URL.fullmatch(path)
        if track and member_record(int(track[2])) is not None:
            return True
        self.error_json("Song unavailable" if track else "This feature is unavailable to household members.",
                        HTTPStatus.NOT_FOUND if track else HTTPStatus.FORBIDDEN)
        return False

    def _personal_data(self, path, arguments, body=None):
        profile = self._personal_profile()
        if profile is None:
            return self.error_json("Sign in to a household account.", HTTPStatus.FORBIDDEN)
        try:
            with ROOT_LOCK, REFRESH_LOCK:
                if not member_profiles.selected(profile):
                    raise ProfileConflict("The room changed. Try again.")
                if path == "/api/my/session":
                    if body is not None and update_draining():
                        return self.error_json("The room is restarting. Try again shortly.", HTTPStatus.SERVICE_UNAVAILABLE)
                    return self.json_response(self._member_session_snapshot(profile, body))
                if path == "/api/my/radio":
                    if body is not None and update_draining():
                        return self.error_json("The room is restarting. Try again shortly.", HTTPStatus.SERVICE_UNAVAILABLE)
                    if body is not None:
                        action = radio_resume.validate_action(body)
                        if action[2] and not source_enabled(SETTINGS, action[2]["source"]["kind"]):
                            return self.error_json("This Radio source is disabled.", HTTPStatus.FORBIDDEN)
                        return self.json_response(profile.write_radio_resume(*action))
                    resume = profile.radio_resume()
                    if resume["history"] and not source_enabled(SETTINGS, resume["history"]["source"]["kind"]):
                        resume["history"] = None
                    return self.json_response(resume)
                if path == "/api/my/playlists":
                    if body is None:
                        identity = arguments.get("id", [None])[0]
                        if identity is None:
                            return self.json_response({"playlists": profile.playlists()})
                        playlist = profile.playlist(identity)
                    else:
                        track_key = None
                        operation = body.get("op")
                        if not isinstance(operation, str):
                            raise ValueError("Choose a playlist action.")
                        if operation == "add":
                            index = body.get("track_id")
                            record = (catalog.active_record_at(index) if profile.scope[1] == "owner"
                                      else member_record(index))
                            track_key = record["key"] if record else None
                        elif operation == "add_metadata":
                            track_key = playlist_edits.metadata(body)
                            matches = playlist_edits.matching_metadata(catalog.records, track_key)
                            visible = [row for row in matches if row.get("active", True) and
                                       (profile.scope[1] == "owner" or member_record(row["index"]) is not None)]
                            if len(visible) == 1:
                                track_key = visible[0]["key"]
                        playlist = profile.edit_playlist(operation, body, track_key)
                    if playlist is None:
                        return self.json_response({"deleted": True})
                    return self.json_response({**playlist, "tracks": self._private_tracks(profile, playlist)})
        except ProfileConflict as error:
            return self.error_json(str(error), HTTPStatus.CONFLICT)
        except KeyError:
            return self.error_json("Item not found.", HTTPStatus.NOT_FOUND)
        except moods.EmptyMix as error:
            return self.json_response({"error": str(error), "code": "empty_mood"}, HTTPStatus.BAD_REQUEST)
        except (ValueError, TypeError) as error:
            return self.error_json(str(error))
        except (sqlite3.Error, OSError):
            return self.error_json("Your listening data is unavailable. Try again.", HTTPStatus.SERVICE_UNAVAILABLE)

    def _hiking_request_origin(self):
        origin = hiking_origin()
        hosts = self.headers.get_all("Host", [])
        if not origin or len(hosts) != 1:
            return ""
        try:
            return origin if configured_origin(f"https://{hosts[0]}") == origin else ""
        except ValueError:
            return ""

    def _legacy_local_owner(self):
        return not self._explicit_owner() and self._client_is_loopback() and not self._hiking_request_origin()

    def _pairing_request_host(self):
        if self._auth_tier() == "control" or (
            self._legacy_local_owner() and self.headers.get("X-Listening-Local-Control") == "1"
        ):
            return None
        return self.headers.get("Host")

    def _request_origin(self):
        host = self.headers.get("Host", "")
        if not host:
            return ""
        hiking_request = self._hiking_request_origin()
        if hiking_request:
            return hiking_request
        try:
            if ROOM_ORIGIN:
                scheme = urlparse(ROOM_ORIGIN).scheme
                if configured_origin(f"{scheme}://{host}") == ROOM_ORIGIN:
                    return ROOM_ORIGIN
            return configured_origin(f"http://{host}")
        except ValueError:
            return ""

    def _session_cookie(self, name, value, age=None, same_site="Strict"):
        cookie = f"{name}={value}; HttpOnly; SameSite={same_site}; Path=/"
        if age is not None:
            cookie += f"; Max-Age={age}"
        if self._request_origin().startswith("https://"):
            cookie += "; Secure"
        return cookie

    def _spotify_redirect_uri(self):
        origin = self._request_origin() if self._explicit_owner() else ""
        origin = origin or f"http://127.0.0.1:{self.server.server_port}"
        return origin + "/api/spotify/callback"

    def _bootstrap_allowed(self):
        """The bootstrap surface: enough for the app shell to load and
        register its device, and nothing that reads the catalog,
        controls playback, or touches settings."""
        parsed = urlparse(self.path)
        if self.command == "POST":
            return parsed.path == "/api/device"
        if self.command != "GET":
            return False
        if parsed.path.startswith("/api/"):
            return False
        return is_public_static(dist_relative(parsed.path))

    def host_rejected(self):
        """Plan 021b: the Host header is the DNS-rebinding signal, so
        it is checked before authorization, for loopback callers too.
        Only IP literals, localhost, .local names, and the owner's
        allowed_hosts are served; a missing Host is a tooling case
        allowed from loopback only. Forwarding headers are never
        consulted."""
        name = parse_host(self.headers.get("Host"))
        if not name:
            return self._explicit_owner() or not self._client_is_loopback()
        if self._explicit_owner() and (len(self.headers.get_all("Host", [])) != 1 or not self._request_origin()):
            return True
        if ROOM_ORIGIN and name == urlparse(ROOM_ORIGIN).hostname:
            return False
        return not host_permitted(name, SETTINGS.get("allowed_hosts", [])) and not self._hiking_request_origin()

    def authorized(self):
        if self._is_owner() or self._auth_tier() == "member":
            return True
        parsed = urlparse(self.path)
        token = parse_qs(parsed.query).get("token", [""])[0]
        invited = (parsed.path in {"/", "/pair"} and token.isascii()
                   and hmac.compare_digest(token, SETTINGS["share_token"]))
        if self.command == "GET" and parsed.path == "/" and invited:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            if self._cookie(MEMBER_COOKIE_NAME):
                self.send_header("Set-Cookie", self._session_cookie(MEMBER_COOKIE_NAME, "", 0))
            self.send_header("Set-Cookie", self._session_cookie("lr_token", SETTINGS["share_token"]))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            return False
        if self.command == "GET" and parsed.path == "/pair" and invited:
            # Commit a same-site document before using the Strict cookie.
            # This URL is outside the service worker's cached app shell.
            payload = (b'<!doctype html><meta charset="utf-8"><title>Listening Room</title>'
                       b'<script>location.replace("/")</script><a href="/">Open Listening Room</a>')
            self.send_response(HTTPStatus.OK)
            if self._cookie(MEMBER_COOKIE_NAME):
                self.send_header("Set-Cookie", self._session_cookie(MEMBER_COOKIE_NAME, "", 0))
            self.send_header("Set-Cookie", self._session_cookie("lr_token", SETTINGS["share_token"]))
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(payload)
            return False
        tier = self._auth_tier()
        if tier == "device" and parsed.path != "/pair":
            return True
        if tier == "bootstrap" and self._bootstrap_allowed():
            return True
        if (self._explicit_owner() and self.command == "GET" and parsed.path == "/"
                and parse_qs(parsed.query).get("pair") == ["1"]):
            recovery = DIST / "pairing.html"
            if recovery.is_file():
                self._send_static_file(recovery, "no-store")
                return False
        if self._explicit_owner() and self.command == "GET" and self._bootstrap_allowed():
            return True
        if (self.command == "GET" and parsed.path in {"/", "/pair"}
                and "text/html" in self.headers.get("Accept", "")):
            recovery = DIST / "pairing.html"
            if recovery.is_file():
                self._send_static_file(recovery, "no-store", HTTPStatus.UNAUTHORIZED)
                return False
        self.json_response({"error": "Open this app using the paired link from Settings",
                            "code": "pairing_required"}, HTTPStatus.UNAUTHORIZED)
        return False

    def _accepts_gzip(self):
        """Whether the client offered gzip (plan 025): a paired
        client's first load moves the whole interface over whatever
        link it has, so JSON and the built assets travel compressed
        whenever the client can take it. An explicit gzip;q=0 is a
        refusal and wins over a wildcard offer."""
        offered = {}
        for part in self.headers.get("Accept-Encoding", "").split(","):
            token, _, params = part.partition(";")
            quality = 1.0
            for param in params.split(";"):
                param = param.strip()
                if param.startswith("q="):
                    try:
                        quality = float(param[2:])
                    except ValueError:
                        quality = 0.0
            offered[token.strip().lower()] = quality
        if "gzip" in offered:
            return offered["gzip"] > 0
        return offered.get("*", 0) > 0

    def _send_encoding_headers(self, compressed):
        if compressed:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")

    def json_response(self, value, status=HTTPStatus.OK, *, cookie=None, cookies=(), retry_after=None):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        compressed = len(payload) >= GZIP_JSON_MIN_BYTES and self._accepts_gzip()
        if compressed:
            payload = gzip.compress(payload, compresslevel=6)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._send_encoding_headers(compressed)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        for extra_cookie in cookies:
            self.send_header("Set-Cookie", extra_cookie)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(payload)

    def error_json(self, message, status=HTTPStatus.BAD_REQUEST):
        self.json_response({"error": message}, status)

    def _device_response(self, credential):
        """Keep this device paired across app restarts. The owner can revoke it."""
        return self.json_response({"ok": True}, cookie=self._session_cookie("lr_token", credential, 31536000))

    def _account_status(self):
        role = {"host": "owner", "control": "owner", "device": "listener", "bootstrap": "bootstrap", "member": "member"}
        status = {"enabled": self._explicit_owner(), "configured": bool(owner_auth and owner_auth.enabled),
                  "role": role.get(self._auth_tier(), "anonymous")}
        if getattr(self, "_owner_username", None):
            status["username"] = self._owner_username
        if getattr(self, "_member_account", None):
            status.update(username=self._member_account.username, id=self._member_account.id)
        cookie = None
        if "username" in status:
            member = status["role"] == "member"
            name = MEMBER_COOKIE_NAME if member else COOKIE_NAME
            token = self._cookie(name)
            try:
                age = owner_auth.renew_session(token, member=member)
                if age is not None:
                    cookie = self._session_cookie(name, token, age, "Lax")
            except (OSError, sqlite3.Error):
                # The current session was already validated. Retry renewal on a later launch.
                pass
        return self.json_response(status, cookie=cookie)

    def _account_action(self, body):
        if not owner_auth.enabled:
            return self.error_json("Set the owner password on the server with the Listening Room CLI.",
                                   HTTPStatus.SERVICE_UNAVAILABLE)
        try:
            if self.path == "/api/account/logout":
                owner_auth.logout(self._cookie(COOKIE_NAME))
                owner_auth.household.logout(self._cookie(MEMBER_COOKIE_NAME))
                cookies = (self._session_cookie(MEMBER_COOKIE_NAME, "", 0, "Lax"),
                           self._session_cookie(COOKIE_NAME, "", 0, "Lax"),
                           self._session_cookie(OWNER_SELECTION_COOKIE, "", 0, "Lax"))
                return self.json_response({"ok": True}, cookies=cookies)
            result = owner_auth.sign_in(body.get("username"), body.get("password")) if isinstance(body, dict) else None
            if result:
                account, token = result
                if account.role == AccountRole.MEMBER:
                    owner_auth.logout(self._cookie(COOKIE_NAME))
                    cookies = (self._session_cookie(COOKIE_NAME, "", 0, "Lax"), self._session_cookie("lr_token", "", 0))
                    if self._cookie(OWNER_SELECTION_COOKIE):
                        cookies += (self._session_cookie(OWNER_SELECTION_COOKIE, "", 0, "Lax"),)
                    name = MEMBER_COOKIE_NAME
                else:
                    owner_auth.household.logout(self._cookie(MEMBER_COOKIE_NAME))
                    # This login selection outlives any delayed member renewal cookie.
                    cookies = (self._session_cookie(MEMBER_COOKIE_NAME, "", 0, "Lax"),
                               self._session_cookie(OWNER_SELECTION_COOKIE, owner_selection(token),
                                                    SESSION_AGE + SESSION_RENEWAL_WINDOW, "Lax"))
                    name = COOKIE_NAME
        except OwnerLoginThrottled:
            return self.json_response({"error": "Wait a moment before trying to sign in again."},
                                      HTTPStatus.TOO_MANY_REQUESTS, retry_after=RETRY_DELAY)
        except (OSError, sqlite3.Error):
            return self.error_json("Owner login is unavailable. Check the owner password setup on the server.",
                                   HTTPStatus.SERVICE_UNAVAILABLE)
        if not result:
            return self.error_json("Check your username and password.", HTTPStatus.UNAUTHORIZED)
        # Lax permits Spotify's validated top-level OAuth callback.
        return self.json_response({"enabled": True, "role": account.role.value, "username": account.username, "id": account.id},
                                  cookie=self._session_cookie(name, token, SESSION_AGE, "Lax"), cookies=cookies)

    def discover_room(self):
        device = self.headers.get("X-Listening-Device", "")
        nonce = self.headers.get("X-Listening-Challenge", "")
        proof = None
        if device or nonce:
            if (device and not re.fullmatch(r"[0-9a-f-]{36}", device)) or not re.fullmatch(r"[0-9a-f]{64}", nonce):
                return self.error_json("Invalid room challenge")
            if device:
                proof = device_store.room_proof(device, nonce) if device_store else None
            else:
                proof = invitation_room_proof(SETTINGS["share_token"], nonce)
            if proof is None:
                return self.error_json("Pair with this room first", HTTPStatus.UNAUTHORIZED)
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("X-Listening-Room", "1")
        if proof:
            self.send_header("X-Listening-Proof", proof)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _public_discovery(self, path, body=None):
        manage = self._is_owner()
        store = public_discovery.DiscoverySources(host_database(DB, STATE))
        try:
            if path == "/api/discovery/search":
                result = public_discovery.search(store.list(), body, metadata.catalog_recordings, cached=metadata.cached)
                if self._can_add_room_song():
                    for group in result["groups"]:
                        for item in group["items"]:
                            if "musicbrainz_id" in item or "apple_id" in item:
                                song = catalog_imports.song({**item, "artist": item["creator"]})
                                record = catalog_imports.existing(catalog.records, song)
                                if record:
                                    item.update(track_id=record["index"], ready=catalog.status_of(record["index"]) == "verified")
                return self.json_response(result)
            if body is not None:
                if not manage:
                    return self.error_json("Only the room owner can change public sources.", HTTPStatus.FORBIDDEN)
                store.change(body)
            return self.json_response({"sources": [source.view(addresses=manage) for source in store.list()],
                                       "can_manage": manage, "can_import": self._can_add_room_song()})
        except public_discovery.DiscoveryBusy as error:
            return self.error_json(str(error), HTTPStatus.SERVICE_UNAVAILABLE)
        except ValueError as error:
            return self.error_json(str(error))
        except (OSError, sqlite3.Error):
            return self.error_json("The room's discovery settings are unavailable.", HTTPStatus.SERVICE_UNAVAILABLE)

    def _music_profile(self, match, profile):
        context, index, kind, identity = match.groups()
        current = catalog
        seed = None
        if index:
            if index.startswith("custom-"):
                seed = resolve_ref(index) if profile is None else None
                if seed and valid_id(seed.get("musicbrainz_id")):
                    seed = {**seed, "uri": "https://musicbrainz.org/recording/" + seed["musicbrainz_id"],
                            "length": seed["duration"], "isrc": ""}
            else:
                ref = int(index)
                seed = current.active_record_at(ref) if profile is None else member_record(ref, current)
            if seed is None:
                return self.error_json("Unknown song", HTTPStatus.NOT_FOUND)
        elif not valid_id(identity):
            return self.error_json("Choose a MusicBrainz artist or album.")
        records = [row for row in current.records if row.get("active", True)
                   and (profile is None or member_record(row["index"], current) is not None)]
        try:
            def cover(record):
                return current.covers.get(record.get("uri"), "")

            if kind == "artist-highlights":
                result = music_profiles.highlights(identity, records, current.status_of, cover)
            else:
                history = None
                if (context or kind) == "artist":
                    events = play_events if profile is None else profile
                    try:
                        history = current.linked_history(events.history())
                    except sqlite3.Error:
                        pass
                result = music_profiles.lookup(context or kind, identity, seed, records, current.status_of, cover, history)
            if profile is None:
                picks = requests_store().for_picks([("", "", track["musicbrainz_id"]) for track in result["tracks"]
                                                    if track["musicbrainz_id"] and not track.get("track_id")])
                for track in result["tracks"]:
                    if request := picks.get(("", "", track["musicbrainz_id"])):
                        track["request"] = request
            return self.json_response(result)
        except (ConnectionsBusy, OSError, ValueError, sqlite3.Error):
            return self.error_json("This music profile could not load. Try again.", HTTPStatus.SERVICE_UNAVAILABLE)

    def _artist_connections(self, match, profile):
        context, index, kind, identity = match.groups()
        record = None
        if index:
            current = catalog
            index = int(index)
            record = current.active_record_at(index) if profile is None else member_record(index, current)
            if record is None:
                return self.error_json("Unknown song", HTTPStatus.NOT_FOUND)
        try:
            result = artist_connections.lookup(context or kind, identity, record)
        except (ConnectionsBusy, OSError, ValueError, sqlite3.Error) as error:
            if record is None:
                if isinstance(error, urllib.error.HTTPError) and error.code == 404:
                    return self.error_json("MusicBrainz no longer lists this credit", HTTPStatus.NOT_FOUND)
                return self.error_json("Artist credits are temporarily unavailable", HTTPStatus.SERVICE_UNAVAILABLE)
            result = {"state": "unmatched", "title": record["title"], "artist": record["artist"],
                      "credit": None, "retry": True}
        if record is not None:
            result = {**result, "library": {field: record.get(field) or "" for field in ("artist", "title", "album")}}
        return self.json_response(result)

    def _extensions(self, body=None):
        if not self._is_owner():
            return self.error_json("Only the room owner can manage extensions.", HTTPStatus.FORBIDDEN)
        if body is None:
            installed, available, error = [], [], ""
            if os.environ.get("LISTENING_ENGINE_SOCKET"):
                try:
                    installed = manager_call("status")["engines"]
                    available = manager_call("catalog")["engines"]
                except EngineError as failure:
                    error = str(failure)
            return self.json_response({"radio": SETTINGS.get("radio_providers", []),
                                       "installed": installed, "available": available, "error": error,
                                       "configured": bool(os.environ.get("LISTENING_ENGINE_SOCKET")),
                                       "selection": engine_selection(SETTINGS)})
        try:
            action = body.get("action")
            if action == "radio":
                provider, enabled = body.get("provider"), body.get("enabled")
                if not isinstance(provider, str) or provider not in PROVIDERS or type(enabled) is not bool:
                    raise ValueError("Choose a Radio provider.")
                with ROOT_LOCK:
                    selected = set(SETTINGS.get("radio_providers", []))
                    (selected.add if enabled else selected.discard)(provider)
                    SETTINGS["radio_providers"] = sorted(selected)
                    save_settings()
            elif action == "select":
                selection = body.get("selection")
                if not isinstance(selection, dict):
                    raise ValueError("Choose search and download engines.")
                if selection and not engines_ready({"download_engines": selection}):
                    raise ValueError("Enable compatible search and download engines first.")
                with ROOT_LOCK:
                    if worker_running("downloads") or worker_running("requests"):
                        return self.error_json("Wait for active downloads before switching engines.", HTTPStatus.CONFLICT)
                    SETTINGS["download_engines"] = engine_selection({"download_engines": selection})
                    save_settings()
            elif action == "configure":
                manager_call(action, engine=body.get("engine"), settings=body.get("settings"), timeout=30)
            elif action in ("install", "test", "enable", "disable", "update", "rollback", "remove"):
                manager_call(action, engine=body.get("engine"), timeout=225)
            else:
                raise ValueError("Choose an extension action.")
            return self._extensions()
        except ValueError as error:
            return self.error_json(str(error))
        except EngineError as error:
            return self.error_json(str(error), HTTPStatus.SERVICE_UNAVAILABLE)

    def do_GET(self):
        self._request_role = self._owner_username = self._member_account = None
        if self.host_rejected():
            return self.error_json("Unknown host", HTTPStatus.BAD_REQUEST)
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/rest/"):
            return self._subsonic(parsed)
        if not self._account_matches(path):
            return
        if path == "/api/room/discover":
            return self.discover_room()
        if path == "/api/account":
            return self._account_status()
        if not self.authorized():
            return
        if client_artifacts.is_route(path):
            return client_artifacts.serve(self, path, ROOM_ORIGIN or self._request_origin(), REPO_ROOT, CONFIG_PATH.parent)
        if path == "/api/extensions":
            return self._extensions()
        profile = self._member_profile()
        if profile is not None and not self._member_can_read(path):
            return
        if path == "/api/extensions/sources":
            return self.json_response(source_summary(SETTINGS))
        if path in JAM_ROUTES:
            return self._jam_request(path, parse_qs(parsed.query))
        if path in PERSONAL_ROUTES:
            return self._personal_data(path, parse_qs(parsed.query))
        if path == "/api/discovery/sources":
            return self._public_discovery(path)
        if path == "/api/playlists/share":
            if not self._is_owner():
                return self.error_json("Share a playlist from an owner session", HTTPStatus.FORBIDDEN)
            query = parse_qs(parsed.query)
            key = query.get("playlist", [""])[0]
            with ROOT_LOCK, REFRESH_LOCK:
                if key not in catalog.playlists:
                    return self.error_json("Playlist not found", HTTPStatus.NOT_FOUND)
                name = (catalog.list_names.get(key) or spotify.playlist_names().get(key)
                        or (SETTINGS.get("playlist_names") or {}).get(key))
                preview = playlist_sharing.playlist_preview(catalog, key, name=name)
            if query.get("download") != ["1"]:
                return self.json_response(preview)
            payload = playlist_sharing.playlist_xspf(preview)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/xspf+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Disposition", 'attachment; filename="listening-playlist.xspf"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/api/spotify/callback":
            if not self._is_owner():
                return self.error_json("Connect Spotify on the host computer", HTTPStatus.FORBIDDEN)
            try:
                spotify.complete(parse_qs(parsed.query))
                destination = "/?spotify=connected"
            except (SpotifyError, OSError, ValueError) as error:
                spotify.record_error(str(error))
                destination = "/?spotify=error"
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", destination)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            return
        if path == "/api/export/status":
            if not self._is_owner():
                return self.error_json("Read export progress on the host", HTTPStatus.FORBIDDEN)
            return self.json_response({**MEDIA.export_status(), "active": worker_running("export")})
        if path == "/api/replica/identity":
            if self.replica_allowed():
                with ROOT_LOCK:
                    identity = offline_replica.collection_id(CONFIG.media_database)
                return self.json_response({"collection": identity})
            return
        if path == "/api/replica":
            if not self.replica_allowed():
                return
            cursor = parse_qs(parsed.query).get("cursor", ["tracks-0"])[0]
            try:
                with ROOT_LOCK:
                    collection = offline_replica.collection_id(CONFIG.media_database)
                    library, store, current = LIBRARY, MEDIA.store(), catalog
                with REFRESH_LOCK:
                    entries, following = offline_replica.catalog_page(current, requests_store(), cursor)
                page = offline_replica.manifest(entries, following, library, store)
                page["collection"] = collection
                return self.json_response(page)
            except ValueError as error:
                return self.error_json(str(error))
        if path == "/api/room":
            ready = bool(catalog.build_stamp) or REFRESH.synced()
            return self.json_response({"state": "ready" if ready else "catalog_loading"})
        if path == "/api/status":
            return self.json_response(member_summary() if profile is not None else catalog.summary)
        if path == "/api/activity":
            if profile is not None:
                summary = member_summary()
                return self.json_response({"status": "Library ready", "running": False, "attempted": summary["attempted"],
                                           "total": summary["total"],
                                           "verified": summary["verified"], "batch": "", "recent": [], "queued_requests": 0})
            return self.json_response(activity_snapshot())
        if path == "/api/activity/explore":
            return self.json_response({"activity": activity_snapshot(), "summary": catalog.summary,
                                       "events": recent_events(STATE),
                                       "logs": recent_process_lines() if not self._explicit_owner() or self._is_owner() else [],
                                       "bulk_running": worker_running("downloads"),
                                       "requests_running": worker_running("requests"),
                                       "auto_download": bool(SETTINGS.get("auto_download"))})
        if path == "/api/session":
            if profile is not None:
                try:
                    return self.json_response(self._member_session_snapshot(profile))
                except (ValueError, sqlite3.Error):
                    return self.error_json("Your playback state is unavailable. Try again.", HTTPStatus.SERVICE_UNAVAILABLE)
            return self.json_response(with_audio_facts(playback.snapshot(output=output_from(self.headers))))
        if path == "/api/stations":
            enabled = provider_enabled(SETTINGS, "youtube")
            return self.json_response(
                {"stations": stations_from(SETTINGS) if enabled else [],
                 "presets": RADIO_PRESETS if enabled else (),
                 "genres": catalog.canonical_genres(),
                 "live": live_catalog() if provider_enabled(SETTINGS, "somafm") else {},
                 "ready_genres": {genre: len(refs) for genre, refs in ready_tracks_by_genre().items()},
                 "youtube_enabled": enabled, "youtube_search": enabled and youtube_search.available()})
        if path == "/api/insights":
            key = self._traffic_key(self.headers.get("X-Listening-Output"))
            if key is None:
                return self.error_json("Open Insights from a paired playback device.")
            return self.json_response(PLAYBACK_TRAFFIC.snapshot(key))
        if path == "/api/hiking":
            parameters = parse_qs(parsed.query)
            counts = parameters.get("songs", ["30"])
            if len(counts) != 1 or counts[0] not in {str(count) for count in hiking.COUNTS}:
                return self.error_json("Choose 30, 60, or 90 songs.")
            with ROOT_LOCK, REFRESH_LOCK:
                trip = hiking.plan(catalog, int(counts[0]), member_collection_identity(ROOT))
            trip["offline_origin"] = hiking_origin()
            return self.json_response(trip)
        if path == "/api/radio/library":
            genre = parse_qs(parsed.query).get("genre", [""])[0]
            if not genre or len(genre) > 120:
                return self.error_json("Choose a library genre.")
            with ROOT_LOCK:
                refs = ready_tracks_by_genre().get(canonical_genre(genre), ())
                tracks = [{"id": ref, "title": record["title"], "artist": record["artist"],
                           "album": record.get("album", ""), "audio": f"/api/audio/{ref}"}
                          for ref in refs if (record := catalog.active_record_at(ref)) is not None]
            return self.json_response({"tracks": tracks})
        if path == "/api/radio/search":
            if not provider_enabled(SETTINGS, "youtube"):
                return self.error_json("Enable YouTube in Settings to search for videos.", HTTPStatus.FORBIDDEN)
            query = parse_qs(parsed.query)
            name = query.get("station", [""])[0]
            if "mood" in query:
                try:
                    first, second, genre = moods.resolve(moods.groups(DB), query["mood"][0],
                                                         query.get("blend", [""])[0], query.get("genre", [""])[0])
                except ValueError as error:
                    return self.error_json(str(error))
                name = genre or " ".join(group["name"] for group in (first, second) if group)
            else:
                available = {station["name"] for station in stations_from(SETTINGS)}
                available.update(RADIO_PRESETS)
                available.update(catalog.canonical_genres())
                if name not in available:
                    return self.error_json("Choose a Radio station.")
            mode = query.get("mode", ["mix"])[0]
            raw_page = query.get("page", ["0"])[0]
            if mode not in ("mix", "long") or raw_page not in map(str, range(youtube_search.MAX_PAGE + 1)):
                return self.error_json("Choose a radio format and page.")
            try:
                videos, more = youtube_search.find(name, mode, int(raw_page), cached=metadata.cached)
                return self.json_response({"videos": videos, "more": more})
            except (OSError, ValueError):
                return self.error_json("Radio search is unavailable. Open the station on YouTube.",
                                       HTTPStatus.SERVICE_UNAVAILABLE)
        if path == "/api/settings":
            if profile is not None:
                return self.json_response({"onboarding": False, "lan_url": "", "pairing_network": "", "local_url": "",
                                           "can_invite": False, "download_ready": False, "has_credentials": False,
                                           "auto_download": False, "source_folders": [], "source_files": [],
                                           "spotify": {"connected": False, "configured": False}})
            snapshot = settings_snapshot(self.server.server_port,
                                         self._pairing_request_host(), self.server.server_address[0])
            snapshot["spotify_redirect_uri"] = self._spotify_redirect_uri()
            if not self._is_owner():
                device = device_store.verify(self._presented_cookie())
                invitation = snapshot["lan_url"]
                network = snapshot["pairing_network"]
                snapshot = redact_settings(snapshot)
                if device and device.get("can_invite"):
                    snapshot.update(lan_url=invitation, pairing_network=network, can_invite=True)
            return self.json_response(snapshot)
        if path == "/api/discover-local":
            if not self._is_owner():
                return self.error_json("Search local files on the server computer", HTTPStatus.FORBIDDEN)
            # Detection candidates (plan 044): PATH, then the managed
            # binary the package routes provision into the application
            # folder's bin/, then a copy at the application folder's
            # root (where the owner's working copy has always lived),
            # then the workspace bin/, and the saved setting last, so
            # "Use detected Sockseek" repairs a wrong saved path.
            return self.json_response({"folders": discover_folders(Path.home(), source_dir(), excluded=(ROOT,)),
                                       "sockseek": next((str(path) for path in
                                                         (Path(shutil.which("sockseek") or ""),
                                                          CONFIG_PATH.parent / "bin/sockseek",
                                                          CONFIG_PATH.parent / "sockseek",
                                                          ROOT / "bin/sockseek", Path(SETTINGS["sockseek_binary"]))
                                                         if path.is_file() and os.access(path, os.X_OK)), "")})
        if path == "/api/browse":
            # Item 36 (plan 042): the PathField directory browser.
            # Host-only like discovery, and confined to the home tree
            # plus the standard application locations the server
            # already serves (the app folder, the workspace, and the
            # checkout's frontend/dist). Folder mode lists directories
            # only; file mode lists files too.
            if not self._is_owner():
                return self.error_json("Browse on the server computer", HTTPStatus.FORBIDDEN)
            query = parse_qs(parsed.query)
            mode = query.get("mode", ["folder"])[0]
            home = Path.home().resolve()
            allowed = [home, CONFIG_PATH.parent.resolve(), ROOT.resolve(),
                       source_dir().resolve(), LIBRARY.resolve(),
                       PLAYLIST_OUTPUT.resolve(), FRONTEND.resolve(),
                       DIST.resolve(), REPO_ROOT.resolve()]

            def browsable(candidate):
                return any(candidate == root or candidate.is_relative_to(root)
                           for root in allowed)

            raw = query.get("path", [""])[0].strip()
            target = Path(raw).expanduser().resolve() if raw else home
            if not browsable(target):
                return self.error_json("That folder is outside the locations Listening Room can browse",
                                       HTTPStatus.FORBIDDEN)
            if not target.is_dir():
                return self.error_json("Choose an existing folder")
            entries = []
            try:
                children = sorted(target.iterdir(), key=lambda item: item.name.casefold())
            except OSError as error:
                return self.error_json(str(error)[:300])
            for child in children:
                if child.name.startswith("."):
                    continue
                if child.is_dir():
                    entries.append({"name": child.name, "path": str(child), "kind": "folder"})
                elif mode == "file" and child.is_file():
                    entries.append({"name": child.name, "path": str(child), "kind": "file"})
                if len(entries) >= 500:
                    break
            parent = target.parent
            return self.json_response({"path": str(target),
                                       "parent": str(parent) if browsable(parent) else "",
                                       "home": str(home), "entries": entries})
        if path == "/api/spotify/playlists":
            if not self._is_owner():
                return self.error_json("Manage Spotify playlists on the host", HTTPStatus.FORBIDDEN)
            params = parse_qs(parsed.query)
            try:
                if "playlist" in params:
                    return self.json_response(spotify.playlist_tracks(
                        params.get("account_id", [""])[0], params["playlist"][0],
                        params.get("q", [""])[0], int(params.get("offset", ["0"])[0])))
                return self.json_response(spotify.management())
            except (SpotifyError, ValueError, OSError) as error:
                return self.error_json(str(error))
        if path == "/api/spotify/status":
            return self.json_response(spotify.status())
        if path == "/api/devices":
            presence = device_snapshot()
            present = {item["id"] for item in presence}
            paired = [{"id": record["id"], "name": record["name"],
                       "paired_at": record["paired_at"], "last_seen_at": record["last_seen_at"],
                       "offline_sync": record.get("offline_sync", False),
                       "playlist_access": record.get("playlist_access", False), "present": record["id"] in present}
                      for record in (device_store.records() if device_store else [])]
            paired.sort(key=lambda item: item["paired_at"])
            payload = {"devices": presence, "paired_devices": paired,
                       "server": socket.gethostname()}
            if self._is_owner():
                payload["manage"] = True
            return self.json_response(payload)
        if path == "/api/announcements":
            device_id = parse_qs(parsed.query).get("device_id", [""])[0]
            if not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", device_id):
                return self.error_json("Invalid device ID")
            return self.json_response({"announcements": ANNOUNCEMENTS.for_device(device_id)})
        if path == "/api/jobs":
            with ROOT_LOCK:
                snapshot = jobs.snapshot(host=self._is_owner())
            return self.json_response(snapshot)
        if path == "/api/process":
            host = self._is_owner()
            return self.json_response({"activity": activity_snapshot(), "bulk_running": worker_running("downloads"),
                                       "requests_running": worker_running("requests"),
                                       "genres_running": worker_running("genres"),
                                       "share_running": worker_running("share"),
                                       "auto_download": bool(SETTINGS.get("auto_download")),
                                       "logs": recent_process_lines() if host else []})
        if path == "/api/update":
            if not self._is_owner():
                return self.error_json("Check for updates on the host computer", HTTPStatus.FORBIDDEN)
            try:
                if install_kind() == "docker" and container_updates.available():
                    if not self._explicit_owner():
                        return self.error_json("Set up an owner login to manage updates.", HTTPStatus.FORBIDDEN)
                    action = "status" if parse_qs(parsed.query).get("status") == ["1"] else "check"
                    return self.json_response(container_updates.request(action))
                return self.json_response(updater.check())
            except UpdateError as error:
                return self.error_json(str(error))
        if path == "/api/export/settings":
            if not self._is_owner():
                return self.error_json("Export on the host computer", HTTPStatus.FORBIDDEN)
            safe = {key: SETTINGS.get(key, "") for key in ("folder", "lists_folder", "music_folder",
                                                             "playlist_output", "source_folders", "source_files",
                                                             "share_folder", "auto_download")}
            payload = json.dumps(safe, indent=2).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition", 'attachment; filename="listening-room-settings.json"')
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(payload)
        if path == "/api/export":
            if not self._is_owner():
                return self.error_json("Export on the host computer", HTTPStatus.FORBIDDEN)
            buffer = BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("bundle.json", json.dumps({"format": "listening-library-bundle", "version": 1,
                                                            "lists": sorted(manifest_signature(source_dir()))}, indent=2))
                for file in xspf_files(source_dir()):
                    tracks = [{**track, "location": ""} for track in read_list(file)]
                    archive.writestr("lists/" + file.stem + ".xspf",
                                     write_xspf(playlist_title(file) or file.stem, tracks))
                    archive.writestr("csv/" + file.stem + ".csv", write_csv(tracks))
                if PLAYLIST_OUTPUT.is_dir():
                    for file in PLAYLIST_OUTPUT.glob("*.m3u8"):
                        archive.writestr("playlists/" + file.name, portable_m3u8(file, LIBRARY))
            payload = buffer.getvalue()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="listening-room-export.zip"')
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(payload)
        if path == "/api/search":
            query = parse_qs(parsed.query).get("q", [""])[0]
            try:
                return self.json_response({"results": metadata.external_search(query, catalog)})
            except (OSError, urllib.error.HTTPError, ValueError, TimeoutError):
                return self.error_json("MusicBrainz search is unavailable right now", HTTPStatus.SERVICE_UNAVAILABLE)
        if path == "/api/discover/activity":
            return self.json_response(discovery_board.listening_activity(catalog, favorites, play_events))
        if path == "/api/discover/press":
            try:
                return self.json_response(discovery_board.weekly_press(metadata))
            except (OSError, ValueError, sqlite3.Error):
                return self.error_json("The music desk is unavailable right now", HTTPStatus.SERVICE_UNAVAILABLE)
        if path == "/api/discover":
            try:
                shelf = metadata.discover_status(catalog, str(ROOT), favorites)
                known = metadata.discovery_index(catalog)
                tracks = [item for item in shelf["tracks"] if not known.contains(item)]
                keys = [(item["artist"], item["title"], item["musicbrainz_id"]) for item in tracks]
                picks = requests_store().for_picks(keys)
                return self.json_response({**shelf, "tracks": [
                    {**item, "request": picks.get(key)} for item, key in zip(tracks, keys)]})
            except (OSError, urllib.error.HTTPError, ValueError, TimeoutError):
                return self.error_json("Deep Dive is unavailable right now", HTTPStatus.SERVICE_UNAVAILABLE)
        trail = discovery_trail.BRANCH_PATH.fullmatch(path)
        if trail or path == "/api/discover/matches":
            try:
                if trail:
                    kind, identity = trail.groups()
                else:
                    kind, identity = "search", parse_qs(parsed.query).get("q", [""])[0].strip()
                    if not 1 <= len(identity) <= 160:
                        return self.error_json("Enter a music search of up to 160 characters")
                result = discovery_trail.branch(metadata, artist_connections, kind, identity,
                                                metadata.discovery_index(catalog))
                if kind == "recording":
                    key = ("", "", identity)
                    result["request"] = requests_store().for_picks([key]).get(key)
                return self.json_response(result)
            except (ConnectionsBusy, OSError, ValueError, sqlite3.Error):
                return self.error_json("This branch could not load. Try again.", HTTPStatus.SERVICE_UNAVAILABLE)
        music_profile = PROFILE_PATH.fullmatch(path)
        if music_profile:
            return self._music_profile(music_profile, profile)
        connection = CONNECTION_PATH.fullmatch(path)
        if connection:
            return self._artist_connections(connection, profile)
        if path.startswith("/api/metadata/recording/"):
            try:
                details = metadata.recording_details(path.rsplit("/", 1)[1])
                return self.json_response({**details, "title": details["recording_title"],
                                           "artist": details["recording_artist"], "album": "",
                                           "source": "MusicBrainz"})
            except (OSError, urllib.error.HTTPError, ValueError, TimeoutError):
                return self.error_json("Song DNA is unavailable right now", HTTPStatus.SERVICE_UNAVAILABLE)
        if path.startswith("/api/metadata/"):
            current = catalog
            try:
                index = int(path.rsplit("/", 1)[1])
                record = current.record_at(index) if profile is None else member_record(index, current)
                if record is None:
                    raise ValueError
            except ValueError:
                return self.error_json("Unknown song", HTTPStatus.NOT_FOUND)
            events = play_events if profile is None else profile
            play_count, last_played = events.stats(*current.recording_keys(record))
            detail = metadata.track_metadata(index, current)
            if profile is None:
                favorite = current.is_favorite(record)
            else:
                keys = profile.keys()
                favorite = any(key in keys for key in current.recording_keys(record))
                detail.update(identifier="", spotify_url="", provenance="",
                              lists=[current.list_names.get(key, key) for key in current.memberships.get(index, [])
                                     if member_playlist(key)])
            return self.json_response({**detail,
                                       **genre_fields(record),
                                       **audio_facts(index),
                                       "favorite": favorite,
                                       "play_count": play_count,
                                       "last_played": last_played})
        if path.startswith("/api/lyrics/"):
            current = catalog
            try:
                ref = path.rsplit("/", 1)[1]
                if ref.startswith("custom-"):
                    if profile is not None or not re.fullmatch(r"custom-[1-9][0-9]{0,17}", ref):
                        raise ValueError
                    row = requests_store().downloaded_custom(int(ref[7:]))
                    record = ({"title": row["title"], "artist": row["artist"], "length": row["length"],
                               "key": "request:" + json.dumps([ref, row["artist"], row["title"], row["length"]])}
                              if row else None)
                else:
                    index = int(ref)
                    record = current.record_at(index) if profile is None else member_record(index, current)
                if record is None:
                    raise ValueError
            except ValueError:
                return self.error_json("Unknown song", HTTPStatus.NOT_FOUND)
            try:
                return self.json_response(metadata.lyrics(record))
            except (OSError, urllib.error.HTTPError, ValueError, TimeoutError):
                return self.error_json("Lyrics are unavailable right now", HTTPStatus.SERVICE_UNAVAILABLE)
        if path == "/api/playlist-links":
            if not self._is_owner():
                return self.error_json("Review playlist links on the host", HTTPStatus.FORBIDDEN)
            return self.json_response(playlist_links.review(DB, catalog.records))
        if path == "/api/overview":
            playlists = []
            spotify_names = spotify.playlist_names() if profile is None else {}
            owner_names = SETTINGS.get("playlist_names") or {}
            mirror_kinds = source_mirror_kinds(SETTINGS, source_dir()) if profile is None else {}
            spotify_owned = spotify.playlist_keys() if profile is None else ()
            paused_spotify = spotify.paused_keys() if profile is None else ()
            destinations = spotify_destinations() if profile is None else {}
            allowed = self._can_add_room_song()
            # The summary is computed on read over every record
            # (plan 036), so it is read once per request, never once
            # per playlist (plan 025: at 10k records the in-loop read
            # cost ~14 ms x the playlist count on every overview).
            summary = catalog.summary if profile is None else member_summary()
            playlist_stats = summary.get("playlists", {})
            for key, entries in catalog.playlists.items():
                if profile is not None and not member_playlist(key):
                    continue
                first = next((catalog.record_at(index) for index in entries
                              if catalog.record_at(index) is not None), None)
                stats = playlist_stats.get(key, {})
                source = "spotify-paused" if key in paused_spotify else playlist_source(key, mirror_kinds, spotify_owned)
                playlists.append({"id": key, "name": catalog.list_names.get(key, spotify_names.get(key, owner_names.get(key, key.replace("_", " ")))),
                                  "entries": len(entries), "playable": stats.get("playable", 0),
                                  "revision": catalog.playlist_revisions.get(key, ""),
                                  "cover": catalog.covers.get(first["uri"], "") if first else "",
                                  "source": source, **playlist_access(source, destinations.get(key), spotify_names, allowed)})
            recently_played = []
            seen_refs = set()
            events = play_events if profile is None else profile
            favorite_keys = catalog.favorites if profile is None else profile.keys()
            for event in events.recent_started(30):
                ref = event["ref"]
                if ref in seen_refs:
                    continue
                seen_refs.add(ref)
                try:
                    track_ref = int(ref)
                except ValueError:
                    track_ref = ref
                track = (resolve_ref(track_ref) if profile is None
                         else member_catalog_track(track_ref, favorite_keys))
                if track is None or track["status"] != "verified":
                    continue
                recently_played.append({"ref": ref, "track": track,
                                        "played_at": event["ts"],
                                        "device_id": event["device_id"]})
                if len(recently_played) >= 10:
                    break
            return self.json_response({"playlists": playlists, "summary": summary,
                                       "total": summary["total"] if profile is not None else
                                           sum(record.get("active", True) for record in catalog.records),
                                       "favorites": len(favorite_keys),
                                       "recently_played": recently_played})
        if path == "/api/tracks":
            return self.get_tracks(parse_qs(parsed.query), profile)
        if path == "/api/requests":
            params = parse_qs(parsed.query, keep_blank_values=True)
            if "title" in params:
                pick = (params.get("artist", [""])[0], params["title"][0])
                if not pick[1] or any(len(value) > 200 for value in pick):
                    return self.json_response({"requests": []})
                if "musicbrainz_id" in params:
                    pick += (params["musicbrainz_id"][0],)
                row = requests_store().for_picks([pick]).get(pick)
                return self.json_response({"requests": [row] if row else []})
            return self.json_response({"requests": recent_requests()})
        if path.startswith("/api/replica/audio/"):
            return self.serve_audio(path.replace("/api/replica/", "/api/", 1), replica=True)
        if path.startswith("/api/audio/"):
            params = parse_qs(parsed.query)
            profile = params.get("format", ["original"])[0]
            if profile not in ("original", *compact_audio.PROFILES):
                return self.error_json("Unknown audio format")
            recording = params.get("recording")
            if recording is not None and (len(recording) != 1 or not re.fullmatch(r"[a-f0-9]{64}", recording[0])):
                return self.error_json("Invalid recording")
            save = params.get("save") == ["1"]
            output = params.get("output", [])
            traffic_key = self._traffic_key(output[0]) if len(output) == 1 and not save else None
            return self.serve_audio(path, profile=profile, save=save, traffic_key=traffic_key,
                                    recording=recording[0] if recording else None)
        return self.serve_static(path)

    def get_tracks(self, params, profile=None):
        playlist = params.get("playlist", ["all"])[0]
        query = normalize_title(params.get("q", [""])[0])[:200]
        filter_name = params.get("filter", ["all"])[0]
        spotify_only = params.get("spotify", [""])[0] == "1"
        genre_filters = []
        for value in params.get("genre", []):
            if value and value not in genre_filters:
                genre_filters.append(value)
        genre_filter_set = set(genre_filters)
        mood = params.get("mood", [""])[0]
        include_moods = "moods" in params.get("facets", [])
        groups = moods.groups(DB) if include_moods or mood else []
        mood_genres = next((set(group["genres"]) for group in groups if group["id"] == mood), None)
        if mood and mood_genres is None:
            return self.error_json("Unknown mood")
        try:
            offset = max(0, int(params.get("offset", ["0"])[0]))
            limit = min(100, max(1, int(params.get("limit", ["60"])[0])))
        except ValueError:
            return self.error_json("Invalid page")
        if profile is not None and (spotify_only or filter_name == "requests"):
            return self.error_json("This filter is unavailable to household members.", HTTPStatus.FORBIDDEN)
        if playlist != "all" and (playlist not in catalog.playlists or (profile is not None and not member_playlist(playlist))):
            return self.error_json("Unknown playlist")
        indices = range(1, len(catalog.records) + 1) if playlist == "all" else catalog.playlists[playlist]
        requests = requests_store().by_track() if profile is None else {}
        favorite_keys = catalog.favorites if profile is None else profile.keys()
        # Your listening (plan 050 step 5): the two history views
        # filter and order by the play-event aggregates, read once
        # per request and only for these filters.
        events = play_events if profile is None else profile
        history = catalog.linked_history(events.history()) if filter_name in ("recent", "most") else None
        # Two phases (plan 018 PERF-01): every filter runs on cheap
        # per-record data first, and the full track dict is built
        # only for the sliced page, not for every match.
        matching = []
        for index in indices:
            if index is None:
                continue
            record = catalog.active_record_at(index) if profile is None else member_record(index)
            if record is None:
                continue
            if spotify_only and not spotify_share_url(record.get("uri", "")):
                continue
            if query and query not in catalog.search_text(index):
                continue
            if mood_genres is not None or genre_filter_set:
                genre = catalog.genre_canonical(record)
                if mood_genres is not None and genre not in mood_genres:
                    continue
                if genre_filter_set and genre not in genre_filter_set:
                    continue
            if filter_name == "favorites" and not any(key in favorite_keys for key in catalog.recording_keys(record)):
                continue
            if history is not None:
                played = history.get(record["key"])
                if played is None:
                    continue
                if filter_name == "most" and played[1] == 0:
                    continue
                if filter_name == "recent" and played[0] is None:
                    # Recently played is over started events (plan
                    # 045): a completion whose start row was lost
                    # counts in Most played, not here.
                    continue
            status = catalog.status_of(index, requests)
            if filter_name == "ready" and status != "verified":
                continue
            if filter_name == "missing" and status == "verified":
                continue
            if filter_name == "requests" and status not in {"waiting_format", "queued", "searching", "waiting_space"}:
                continue
            matching.append(index)
        if history is not None:
            # The history views order by the events, not the
            # catalog; the index tiebreak keeps the order total, so
            # offset paging cannot shuffle rows between pages.
            def history_key(index):
                last_played, completed = history[catalog.record_at(index)["key"]]
                if filter_name == "most":
                    return (-completed, -(last_played or 0), index)
                return (-(last_played or 0), index)
            matching.sort(key=history_key)
        page = matching[offset:offset + limit]
        tracks = ([catalog.track(index, requests) for index in page] if profile is None
                  else [track for index in page if (track := member_catalog_track(index, favorite_keys)) is not None])
        availability = ({"ready_genres": {genre: len(refs) for genre, refs in ready_tracks_by_genre().items()}}
                        if "mood-availability" in params.get("facets", []) else {})
        return self.json_response({"tracks": tracks,
                                   "total": len(matching), "offset": offset, "limit": limit,
                                   "genres": catalog.canonical_genres(),
                                   **({"moods": groups} if include_moods else {}), **availability})

    def do_POST(self):
        self._request_role = self._owner_username = self._member_account = None
        if self.host_rejected():
            return self.error_json("Unknown host", HTTPStatus.BAD_REQUEST)
        if not self._account_matches(urlparse(self.path).path):
            return
        account_action = self.path in {"/api/account/login", "/api/account/logout"}
        if account_action and not self._explicit_owner():
            return self.error_json("Owner sign-in is disabled.", HTTPStatus.NOT_FOUND)
        if not account_action and not self.authorized():
            return
        profile = self._member_profile() if not account_action else None
        if profile is not None and self.path not in MEMBER_POST:
            return self.error_json("This action is unavailable to household members.", HTTPStatus.FORBIDDEN)
        if not account_action and self.path not in PERSONAL_ROUTES and self.path not in {"/api/radio/presence", "/api/extensions", "/api/jams", "/api/discovery/sources", "/api/discovery/search", "/api/replica/original", "/api/requests", "/api/requests/format", "/api/requests/retry", "/api/requests/batch", "/api/session", "/api/favorites", "/api/stations", "/api/settings", "/api/sources",
                             "/api/sync", "/api/import-csv", "/api/import-list", "/api/import-bundle", "/api/device", "/api/devices/revoke", "/api/devices/offline", "/api/devices/invites", "/api/pairing/rotate",
                             "/api/announcements", "/api/process", "/api/jobs", "/api/open-source", "/api/playlists/merge",
                             "/api/playlists/add-song", "/api/discovery/import", "/api/devices/playlist-access",
                             "/api/moods", "/api/genre", "/api/export", "/api/spotify/playlists", "/api/spotify/connect", "/api/spotify/sync", "/api/spotify/disconnect", "/api/playlist-links"}:
            return self.error_json("Unknown action", HTTPStatus.NOT_FOUND)
        origins = self.headers.get_all("Origin", [])
        if origins:
            valid_origin = len(origins) == 1 and origins[0] == self._request_origin()
        else:
            valid_origin = not account_action and (self._auth_tier() == "control"
                            or self._legacy_local_owner())
        if not valid_origin:
            return self.error_json("Invalid origin", HTTPStatus.FORBIDDEN)
        if self.path == "/api/replica/original":
            if self.replica_allowed():
                self.receive_original()
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            return self.error_json("Send JSON")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > (50 * 1024 * 1024 if self.path == "/api/import-bundle" else
                                   25 * 1024 * 1024 if self.path in {"/api/import-csv", "/api/import-list"} else 16384):
                return self.error_json("Invalid request size")
            payload = self.rfile.read(size)
            body = json.loads(payload)
        except (ValueError, json.JSONDecodeError):
            return self.error_json("Invalid JSON")
        if self.path in {"/api/session", "/api/my/session"} and isinstance(body, dict):
            key = self._traffic_key(self.headers.get("X-Listening-Output"))
            if key is not None:
                PLAYBACK_TRAFFIC.record(key, received=len(payload))
        if account_action:
            return self._account_action(body)
        if self.path == "/api/jams":
            if body is None:
                return self.error_json("Choose a Jam action.")
            return self._jam_request(self.path, {}, body)
        if self.path in {"/api/discovery/sources", "/api/discovery/search"}:
            if not isinstance(body, dict):
                return self.error_json("Send a JSON object.")
            return self._public_discovery(self.path, body)
        if self.path in PERSONAL_ROUTES:
            if not isinstance(body, dict):
                return self.error_json("Send a JSON object.")
            return self._personal_data(self.path, {}, body)
        if self.path == "/api/discovery/import":
            if not self._can_add_room_song():
                return self.error_json("Ask the room owner for playlist access to import songs.", HTTPStatus.FORBIDDEN)
            if not isinstance(body, dict):
                return self.error_json("Choose a recording to import.")
            try:
                with PLAYLIST_EDIT_LOCK:
                    with REFRESH_LOCK, _START_LOCK:
                        if update_draining():
                            return self.error_json("The room is restarting. Try again shortly.", HTTPStatus.SERVICE_UNAVAILABLE)
                        if not self._can_add_room_song():
                            return self.error_json("Playlist access is required to import songs.", HTTPStatus.FORBIDDEN)
                        row = catalog_imports.song(body)
                        record = catalog_imports.existing(catalog.records, row)
                        changed = False if record else catalog_imports.save(source_dir(), list_projection(), row)
                        if not record and not any_library_worker_running():
                            try:
                                REFRESH.refresh_if_changed()
                            except (RuntimeError, OSError, sqlite3.Error, subprocess.SubprocessError):
                                MAINTENANCE_WAKE.set()
                            record = catalog_imports.existing(catalog.records, row)
                    if not record:
                        MAINTENANCE_WAKE.set()
                    return self.json_response({"imported": changed, "track_id": record["index"] if record else None,
                                               "ready": bool(record and catalog.status_of(record["index"]) == "verified")})
            except ValueError as error:
                return self.error_json(str(error))
            except (RuntimeError, OSError, sqlite3.Error, subprocess.TimeoutExpired):
                return self.error_json("Could not finish updating the catalog. Retry this import.", HTTPStatus.CONFLICT)
        if self.path == "/api/playlists/add-song":
            if not self._can_add_room_song():
                return self.error_json("Ask the room owner to allow playlist additions for this device.", HTTPStatus.FORBIDDEN)
            if not isinstance(body, dict):
                return self.error_json("Send a song and a playlist.")
            try:
                return self.json_response(self.playlist_add_song(body))
            except (ValueError, SpotifyError) as error:
                return self.error_json(str(error))
            except (RuntimeError, OSError, sqlite3.Error):
                return self.error_json("Could not refresh the playlist. Reload it before retrying.", HTTPStatus.CONFLICT)
        if self.path == "/api/session":
            try:
                if not isinstance(body, dict):
                    raise ValueError("Choose a player action")
                # Session actions carry the acting device's id when the
                # client sends one (plan 045); an absent or malformed id
                # is not an error, the action proceeds unattributed,
                # exactly as every action did before the field existed.
                device_id = body.get("device_id")
                if not (isinstance(device_id, str)
                        and re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", device_id)):
                    device_id = None
                output = output_from(self.headers)
                with ROOT_LOCK:
                    if update_draining():
                        return self.error_json("The room is restarting. Try again shortly.", HTTPStatus.SERVICE_UNAVAILABLE)
                    if profile is not None:
                        return self.json_response(self._member_session_snapshot(profile, body))
                    refs = selected_playback_tracks(body)
                    snapshot = playback.apply(body, device_id=device_id, output=output, mood_refs=refs)
                return self.json_response(with_audio_facts(snapshot))
            except moods.EmptyMix as error:
                return self.json_response({"error": str(error), "code": "empty_mood"}, HTTPStatus.BAD_REQUEST)
            except (ValueError, TypeError) as error:
                return self.error_json(str(error) or "That player action is not available")
            except sqlite3.Error:
                return self.error_json("Your playback state is unavailable. Try again.", HTTPStatus.SERVICE_UNAVAILABLE)
        if self.path == "/api/favorites":
            # Liking a song is a listening action, so this sits at
            # the paired-device tier beside /api/session, not with
            # the host-only settings actions (plan 048). The index
            # is resolved to the record's own key here; the store
            # never sees a positional index.
            try:
                if not isinstance(body, dict):
                    raise ValueError("Choose a song")
                index = body.get("index")
                if not catalog.valid_index(index):
                    raise ValueError("Unknown song")
                favorite = body.get("favorite")
                if not isinstance(favorite, bool):
                    raise ValueError("Say whether the song is a favorite")
                record = catalog.record_at(index)
                aliases = catalog.recording_keys(record)
                if profile is None:
                    favorites.set(record["key"], favorite, aliases=aliases)
                    catalog.reload_favorites()
                else:
                    if member_record(index) is None or not member_profiles.selected(profile):
                        raise ValueError("Unknown song")
                    profile.set_favorite(record["key"], favorite, aliases)
                return self.json_response({"index": index, "favorite": favorite})
            except (ValueError, TypeError) as error:
                return self.error_json(str(error))
            except sqlite3.Error:
                return self.error_json("Your favorites are unavailable. Try again.", HTTPStatus.SERVICE_UNAVAILABLE)
        if self.path == "/api/radio/presence":
            if not isinstance(body, dict):
                return self.error_json("Choose a Radio output.")
            identity = self._traffic_key(body.get("output"))
            source = body.get("source")
            if identity is None or source not in ("library", "live", "noise", "youtube", None):
                return self.error_json("Choose a Radio output.")
            with ROOT_LOCK:
                if source is None:
                    RADIO_PLAYERS.forget(identity)
                elif update_draining():
                    return self.error_json("The room is restarting. Try again shortly.", HTTPStatus.SERVICE_UNAVAILABLE)
                elif not source_enabled(SETTINGS, source):
                    RADIO_PLAYERS.forget(identity)
                    return self.error_json("This Radio source is disabled.", HTTPStatus.FORBIDDEN)
                else:
                    RADIO_PLAYERS.arrive(identity, None, {"source": source})
            return self.json_response({"ok": True})
        if self.path == "/api/extensions":
            if not isinstance(body, dict):
                return self.error_json("Choose an extension action.")
            return self._extensions(body)
        if self.path == "/api/stations":
            if not provider_enabled(SETTINGS, "youtube"):
                return self.error_json("Enable YouTube in Settings to edit mixes.", HTTPStatus.FORBIDDEN)
            try:
                stations = clean_stations(body.get("stations"))
                with ROOT_LOCK:
                    SETTINGS["radio_stations"] = stations
                    save_settings()
                return self.json_response({"stations": stations})
            except (ValueError, TypeError) as error:
                return self.error_json(str(error))
        if self.path == "/api/device":
            if profile is not None:
                return self.json_response({"ok": True})
            if not isinstance(body, dict):
                return self.error_json("Choose a device")
            device_id = str(body.get("id", ""))[:80]
            name = str(body.get("name", ""))[:60].strip()
            if not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", device_id):
                return self.error_json("Invalid device ID")
            owner = self._is_owner()
            output = output_from(self.headers)
            tab, _, epoch = output if output and output[2] is not None else ("", "", None)
            leaving = body.get("op") == "leave"
            if leaving and epoch is None:
                return self.error_json("Choose a current Listening client")
            credential = None
            with DEVICES_LOCK:
                if not owner and device_store is not None:
                    presented = self._presented_cookie()
                    record = device_store.verify(presented)
                    if record is None or record["id"] != device_id:
                        if leaving or not hmac.compare_digest(presented, SETTINGS["share_token"]):
                            return self.error_json("Device identity mismatch", HTTPStatus.FORBIDDEN)
                        credential = device_store.issue(device_id, name or "Listening device")
                if leaving:
                    DEVICES.leave((device_id, tab), epoch)
                else:
                    arrived = DEVICES.arrive((device_id, tab), epoch, {
                        "id": device_id, "name": name or "Listening device",
                        "address": self.client_address[0], "seen_at": time.time(), "host": owner,
                    })
                    if arrived and not owner and device_store is not None and credential is None:
                        device_store.touch(device_id, name or "Listening device")
            if credential:
                return self._device_response(credential)
            return self.json_response({"ok": True})
        if self.path == "/api/announcements":
            if not self._is_owner():
                return self.error_json("Announce from the server computer", HTTPStatus.FORBIDDEN)
            recipient = str(body.get("recipient", ""))
            if recipient and recipient not in {item["id"] for item in device_snapshot()}:
                return self.error_json("Choose a connected device")
            try:
                return self.json_response(ANNOUNCEMENTS.post(body.get("message"), recipient), HTTPStatus.CREATED)
            except ValueError as error:
                return self.error_json(str(error))
        if self.path == "/api/requests/format":
            try:
                request_id = body.get("request_id") if isinstance(body, dict) else None
                if type(request_id) is not int or not 0 < request_id < 2**63:
                    raise ValueError("Invalid request ID")
                row, released = requests_store().confirm_format(
                    request_id, body.get("requested_format"), body.get("format_revision"))
                if released:
                    with ROOT_LOCK:
                        start_worker("requests", force=True)
                return self.json_response(row)
            except ValueError as error:
                return self.error_json(str(error))
        if self.path == "/api/requests/retry":
            try:
                request_id = int(body.get("request_id"))
                search_title = str(body.get("search_title", "")).strip()[:200]
                if not search_title:
                    raise ValueError("Enter another search phrase")
                store = requests_store()
                if not store.retry(request_id, search_title):
                    raise ValueError("Only failed searches can be retried")
                with ROOT_LOCK:
                    start_worker("requests", force=True)
                return self.json_response({"status": "Queued with a broader search"})
            except (ValueError, TypeError) as error:
                return self.error_json(str(error))
        if self.path == "/api/requests/batch":
            indices = body.get("track_ids")
            if (not isinstance(indices, list) or not indices or len(indices) > 50
                    or any(not isinstance(index, int) or isinstance(index, bool) for index in indices)):
                return self.error_json("Choose up to 50 library songs")
            catalog.refresh()
            if any(catalog.active_record_at(index) is None for index in indices):
                return self.error_json("One of those songs is no longer in the library")
            added = 0
            store = requests_store()
            for index in dict.fromkeys(indices):
                if catalog.coverage.get(index, {}).get("status") == "verified":
                    continue
                record = catalog.record_at(index)
                _, created = store.create_or_find(index, record["artist"], record["title"], record["album"],
                                                  record["length"], record["title"], individual=False)
                added += int(created)
            if added:
                with ROOT_LOCK:
                    start_worker("requests", force=True)
            return self.json_response({"added": added, "status": "Queued broader searches"})
        if self.path in {"/api/settings", "/api/sources", "/api/import-csv", "/api/import-list", "/api/import-bundle", "/api/process", "/api/jobs", "/api/open-source",
                         "/api/moods", "/api/genre", "/api/devices/revoke", "/api/devices/offline", "/api/devices/playlist-access", "/api/devices/invites", "/api/pairing/rotate", "/api/playlists/merge", "/api/playlist-links", "/api/export",
                         "/api/spotify/playlists", "/api/spotify/connect", "/api/spotify/sync", "/api/spotify/disconnect"}:
            if not self._is_owner():
                return self.error_json("Change host settings on the server computer", HTTPStatus.FORBIDDEN)
        if self.path == "/api/playlist-links":
            if any_library_worker_running():
                return self.error_json("Wait for the active library job before changing playlist links", HTTPStatus.CONFLICT)
            try:
                playlist_links.review(DB, catalog.records, body)
                refresh_library(force=True)
                return self.json_response(playlist_links.review(DB, catalog.records))
            except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
                return self.error_json(str(error))
        if self.path == "/api/devices/invites":
            try:
                if not isinstance(body, dict):
                    raise ValueError("Choose a paired device and its invitation permission")
                device_store.allow_invites(str(body.get("id", "")), body.get("allowed"))
                return self.json_response({"ok": True})
            except ValueError as error:
                return self.error_json(str(error))
        if self.path == "/api/devices/offline":
            try:
                device_store.allow_offline_sync(str(body.get("id", "")), body.get("allowed"))
                return self.json_response({"ok": True})
            except ValueError as error:
                return self.error_json(str(error))
        if self.path == "/api/devices/playlist-access":
            try:
                if not isinstance(body, dict):
                    raise ValueError("Choose a paired device.")
                device_store.allow_playlist_additions(str(body.get("id", "")), body.get("enabled"))
                return self.json_response({"ok": True})
            except ValueError as error:
                return self.error_json(str(error))
        if self.path == "/api/devices/revoke":
            device_id = str(body.get("id", ""))
            with DEVICES_LOCK:
                if device_store is not None:
                    device_store.delete(device_id)
                for key in DEVICES.active():
                    if key[0] == device_id:
                        DEVICES.forget(key)
            return self.json_response({"ok": True})
        if self.path == "/api/pairing/rotate":
            # Rotation (plan 021a): the old link and every bootstrap or
            # legacy cookie holding the old token die at once; device
            # records are untouched, so registered devices stay paired.
            SETTINGS["share_token"] = secrets.token_urlsafe(24)
            save_settings()
            return self.json_response({"lan_url": lan_address(self.server.server_port,
                                                              self._pairing_request_host(), self.server.server_address[0])})
        if self.path == "/api/moods":
            try:
                if not isinstance(body, dict):
                    raise ValueError("Choose a mood and genres")
                moods.save(DB, body.get("mood"), body.get("genres"))
                return self.json_response({"moods": moods.groups(DB)})
            except ValueError as error:
                return self.error_json(str(error))
        if self.path == "/api/genre":
            try:
                index = body.get("track_id")
                if not catalog.valid_index(index):
                    raise ValueError("Unknown song")
                raw = str(body.get("genre", "")).strip()[:120]
                if not raw:
                    raise ValueError("Enter a genre")
                record = catalog.record_at(index)
                store = GenreStore(DB)
                entry = store.set(record["key"], raw, "manual")
                if entry is None:
                    raise ValueError("Enter a genre")
                catalog.reload_genres()
                return self.json_response({"genre": entry["genre"], "genre_canonical": entry["canonical"],
                                           "genre_source": "manual", "genre_source_label": source_label("manual")})
            except (ValueError, TypeError) as error:
                return self.error_json(str(error))
        if self.path == "/api/spotify/playlists":
            try:
                return self.json_response(spotify.manage(body))
            except (SpotifyError, ValueError, OSError) as error:
                return self.error_json(str(error))
        if self.path == "/api/spotify/connect":
            client_id = str(body.get("client_id", "")).strip()
            if not re.fullmatch(r"[A-Fa-f0-9]{32}", client_id):
                return self.error_json("Enter a valid 32-character Spotify Client ID")
            if client_id != SETTINGS.get("spotify_client_id"):
                spotify.disconnect()
            SETTINGS["spotify_client_id"] = client_id
            private_write(CONFIG_PATH, json.dumps(SETTINGS, indent=2))
            return self.json_response({"url": spotify.begin(self._spotify_redirect_uri())})
        if self.path == "/api/spotify/sync":
            try:
                started = spotify.start(force=True)
                return self.json_response({"started": started, "spotify": spotify.status()})
            except SpotifyError as error:
                return self.error_json(str(error))
        if self.path == "/api/spotify/disconnect":
            spotify.disconnect()
            return self.json_response({"spotify": spotify.status()})
        if self.path == "/api/sources":
            operation = body.get("op")
            raw_path = str(body.get("path", "")).strip()
            if not raw_path:
                return self.error_json("Enter a folder path")
            path = Path(raw_path).expanduser().resolve()
            if operation not in {"watch", "unwatch", "link", "unlink"}:
                return self.error_json("Choose watch, link, or remove")
            if operation == "watch" and (not path.is_dir() or path == source_dir()):
                return self.error_json("Choose an existing folder outside portable storage")
            if operation == "link" and (not path.is_file() or path.is_relative_to(source_dir())
                                         or not is_music_list(path)):
                return self.error_json("Choose an existing CSV, M3U, or XSPF music list outside portable storage")
            with ROOT_LOCK:
                setting = "source_files" if operation in {"link", "unlink"} else "source_folders"
                adding = operation in {"watch", "link"}
                old = list(SETTINGS.get(setting, []))
                updated = list(old)
                if adding and str(path) not in updated:
                    if len(updated) >= 20:
                        return self.error_json("You can link up to 20 paths of this type")
                    updated.append(str(path))
                elif not adding and str(path) in updated:
                    updated.remove(str(path))
                if updated != old:
                    try:
                        SETTINGS[setting] = updated
                        imported, errors = (sync_folders([path], source_dir()) if operation == "watch" else
                                            sync_files([path], source_dir()) if operation == "link" else ([], []))
                        if adding:
                            with REFRESH_LOCK:
                                publish_adapter_edits()
                        if not worker_running("downloads") and not worker_running("requests"):
                            refresh_library(force=True)
                        save_settings()
                    except (OSError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired, ValueError) as error:
                        SETTINGS[setting] = old
                        return self.error_json(str(error)[:300])
                else:
                    imported, errors = [], []
            return self.json_response({"settings": settings_snapshot(self.server.server_port,
                                                                     self.headers.get("Host"), self.server.server_address[0]),
                                       "imported": len(imported), "errors": errors[:10]})
        if self.path in {"/api/import-csv", "/api/import-list"}:
            try:
                filename = str(body.get("name", ""))
                content = str(body.get("content", ""))
                if (len(filename) > 180 or filename in {"", ".", ".."} or Path(filename).name != filename
                        or "\\" in filename
                        or any(ord(character) < 32 for character in filename) or Path(filename).suffix.casefold() not in SUPPORTED):
                    raise ValueError("Choose a CSV, M3U, M3U8, or XSPF file with a simple file name")
                tracks = parse_list(filename, content)
                if Path(filename).suffix.casefold() == ".xspf":
                    filename = Path(filename).stem + ".xspf"
                names, conflicts = import_lists([(filename, content)], body.get("on_conflict"))
                if conflicts:
                    return self.json_response(
                        {"error": "Lists already exist with different content: "
                                  + ", ".join(conflicts),
                         "conflicts": conflicts}, HTTPStatus.CONFLICT)
                filename = names[0]
                if not worker_running("downloads") and not worker_running("requests"):
                    refresh_library()
                return self.json_response({"name": filename, "tracks": len(tracks),
                                           "status": "Imported as a portable music list; library sync queued"})
            except (ValueError, OSError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired, csv.Error) as error:
                return self.error_json(str(error)[:300])
        if self.path == "/api/import-bundle":
            try:
                payload = base64.b64decode(str(body.get("content", "")), validate=True)
                with zipfile.ZipFile(BytesIO(payload)) as archive:
                    members = [item for item in archive.infolist() if item.filename.startswith("lists/")
                               and item.filename.endswith(".xspf") and not item.is_dir()]
                    if not members or len(members) > 500 or sum(item.file_size for item in members) > 30 * 1024 * 1024:
                        raise ValueError("Bundle needs up to 500 XSPF lists within 30 MB")
                    lists = []
                    for item in members:
                        filename = item.filename.removeprefix("lists/")
                        if (Path(filename).name != filename or "\\" in filename or filename in {"", ".", ".."}
                                or len(filename) > 180 or item.file_size > 25 * 1024 * 1024):
                            raise ValueError("Bundle contains an invalid list name")
                        content = archive.read(item).decode("utf-8")
                        parse_list(filename, content)
                        lists.append((filename, content))
                names, conflicts = import_lists(lists, body.get("on_conflict"))
                if conflicts:
                    return self.json_response(
                        {"error": "Lists already exist with different content: "
                                  + ", ".join(conflicts),
                         "conflicts": conflicts}, HTTPStatus.CONFLICT)
                if not worker_running("downloads") and not worker_running("requests"):
                    refresh_library()
                return self.json_response({"lists": len(lists), "names": names,
                                           "status": "Portable library imported"})
            except (ValueError, OSError, RuntimeError, sqlite3.Error, binascii.Error, zipfile.BadZipFile, UnicodeError, subprocess.TimeoutExpired, csv.Error) as error:
                return self.error_json(str(error)[:300])
        if self.path == "/api/jobs":
            try:
                with ROOT_LOCK:
                    jobs.act(str(body.get("kind", "")), str(body.get("action", "")),
                             body.get("scope"))
                    snapshot = jobs.snapshot()
            except (ValueError, OSError, RuntimeError) as error:
                return self.error_json(str(error)[:300])
            return self.json_response({"status": "Updated", **snapshot})
        if self.path == "/api/export":
            # Plan 060 step 6: start the audio export. The GET on
            # this same path is the portable bundle download; the
            # POST is the store's exit hatch, host-tier only (the
            # gate above has already refused non-host callers).
            # Plan 063: a body naming one playlist scopes the
            # export to that playlist's own folder.
            try:
                if worker_running("export"):
                    raise ValueError("An export is already running")
                playlist = str(body.get("playlist", "") or "")
                profile = body.get("format", "original")
                if profile not in ("original", *compact_audio.PROFILES):
                    raise ValueError("Unknown export format")
                if profile != "original" and (not shutil.which("ffmpeg") or not shutil.which("ffprobe")):
                    raise ValueError("Install FFmpeg on the server to export Compact audio")
                if playlist:
                    if playlist not in catalog.playlists:
                        raise ValueError("That playlist does not exist")
                    name = playlist_display_name(playlist)
                    started = start_export_worker(scope=playlist, folder_name=name,
                                                  **({"profile": profile} if profile != "original" else {}))
                else:
                    started = start_export_worker(**({"profile": profile} if profile != "original" else {}))
                if not started:
                    raise RuntimeError("The export could not start")
                return self.json_response({"status": "Export started",
                                           "export_status": MEDIA.export_status()})
            except (ValueError, OSError, RuntimeError) as error:
                return self.error_json(str(error)[:300])
        if self.path == "/api/process":
            try:
                if not isinstance(body, dict):
                    raise ValueError("Choose a process action")
                action = body.get("action")
                share_materialized = None
                if action == "share_start":
                    with _START_LOCK:
                        if update_draining():
                            return self.error_json("The room is restarting. Try again shortly.", HTTPStatus.SERVICE_UNAVAILABLE)
                        binary = Path(SETTINGS.get("nicotine_binary", ""))
                        if not binary.is_file() or not share_config_path().is_file() or not SETTINGS.get("share_folder"):
                            raise ValueError("Configure Nicotine+, a share folder, and a separate sharing account first")
                        # Plan 060 Q1: sharing starts by materializing
                        # the database-held tracks into the share
                        # folder, so a shared library is whole.
                        share_materialized = MEDIA.materialize_share()
                        if not worker_running("share"):
                            argv = [str(binary), "--headless",
                                    "--config", str(share_config_path()),
                                    "--user-data", str(CONFIG_PATH.parent / "nicotine-data")]
                            if SUPERVISOR is None or not SUPERVISOR.start(
                                    "share", argv, log=CONFIG_PATH.parent / "share.log"):
                                raise RuntimeError("Nicotine+ did not start")
                elif action == "share_stop":
                    stop_worker("share")
                elif action == "update":
                    if install_kind() == "docker":
                        if not self._explicit_owner():
                            return self.error_json("Set up an owner login to manage updates.", HTTPStatus.FORBIDDEN)
                        return self.json_response(container_updates.request("apply", revision=body.get("revision")))
                    return self.json_response(perform_update())
                else:
                    raise ValueError("Unknown process action")
                response = {"status": "Updated", "activity": activity_snapshot()}
                if share_materialized is not None:
                    response["share_materialized"] = share_materialized
                return self.json_response(response)
            except (ValueError, OSError, RuntimeError) as error:
                return self.error_json(str(error)[:300])
        if self.path == "/api/open-source":
            try:
                opener = desktop_player_opener()
                if not opener:
                    return self.error_json("This server has no desktop music player. Open the song in the room player.", HTTPStatus.CONFLICT)
                audio = source_audio(body.get("ref"))
                process = subprocess.Popen([opener, str(audio)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    if process.wait(timeout=0.5) != 0:
                        return self.error_json("The desktop music app declined this song. Open it in the room player.", HTTPStatus.CONFLICT)
                except subprocess.TimeoutExpired:
                    pass  # Some desktop launchers stay attached until playback ends.
                return self.json_response({"status": "Sent to the server’s desktop music app"})
            except OSError:
                return self.error_json("The desktop music app could not open. Use the room player instead.", HTTPStatus.SERVICE_UNAVAILABLE)
            except ValueError as error:
                return self.error_json(str(error)[:300])
        if self.path == "/api/settings":
            try:
                folder = valid_folder(body.get("folder", str(ROOT)), require_csv=False)
                lists_folder = Path(str(body.get("lists_folder") or body.get("csv_folder") or configured_lists(folder))).expanduser().resolve()
                music_folder = Path(str(body.get("music_folder") or (LIBRARY if folder == ROOT else folder / "audio"))).expanduser().resolve()
                playlist_output = Path(str(body.get("playlist_output") or (PLAYLIST_OUTPUT if folder == ROOT else folder / "playlists"))).expanduser().resolve()
                source_folders = body.get("source_folders", SETTINGS.get("source_folders", []))
                if not isinstance(source_folders, list) or len(source_folders) > 20:
                    raise ValueError("Choose up to 20 source folders")
                source_folders = list(dict.fromkeys(str(Path(str(path)).expanduser().resolve()) for path in source_folders))
                if any(not Path(path).is_dir() or Path(path) == lists_folder for path in source_folders):
                    raise ValueError("Choose existing source folders outside the portable lists folder")
                source_files = body.get("source_files", SETTINGS.get("source_files", []))
                if not isinstance(source_files, list) or len(source_files) > 20:
                    raise ValueError("Choose up to 20 linked playlist files")
                source_files = list(dict.fromkeys(str(Path(str(path)).expanduser().resolve()) for path in source_files))
                if any(Path(path).is_relative_to(lists_folder) for path in source_files) or (
                        "source_files" in body and any(not Path(path).is_file() or not is_music_list(path)
                                                       for path in source_files)):
                    raise ValueError("Choose existing playlist files outside the portable lists folder")
                share_folder = str(body.get("share_folder", SETTINGS.get("share_folder", ""))).strip()
                if share_folder and not Path(share_folder).expanduser().is_dir():
                    raise ValueError("Choose an existing folder to share")
                audio_store = str(body.get("audio_store", SETTINGS.get("audio_store", "database")))
                if audio_store not in ("files", "database"):
                    raise ValueError("Choose where new downloads land: files or the database")
                download_format = body.get("bulk_download_format", bulk_download_format(SETTINGS.get("bulk_download_format")))
                if not isinstance(download_format, str) or download_format not in BULK_DOWNLOAD_FORMATS:
                    raise ValueError("Choose MP3 first, MP3 only, or lossless only.")
                if DB == ROOT / "collection.sqlite3":
                    audio_store = "database"
                if folder != ROOT and (DB == ROOT / "collection.sqlite3" or (folder / "collection.sqlite3").exists()):
                    raise ValueError("Stop the server and select this portable room in the configuration before restarting")
                if folder == ROOT and manifest_signature(source_dir()) and lists_folder == configured_lists() and music_folder == LIBRARY and playlist_output == PLAYLIST_OUTPUT:
                    valid_folder(folder)
                with PLAYLIST_EDIT_LOCK, ROOT_LOCK:
                    changed_paths = (folder != ROOT or lists_folder != configured_lists() or music_folder != LIBRARY
                                     or playlist_output != PLAYLIST_OUTPUT or source_folders != SETTINGS.get("source_folders", [])
                                     or source_files != SETTINGS.get("source_files", []))
                    if changed_paths:
                        stop_worker("downloads")
                        stop_worker("requests")
                    previous = SETTINGS.copy()
                    old_root = ROOT
                    try:
                        SETTINGS.update({"folder": str(folder), "lists_folder": str(lists_folder),
                                         "source_folders": source_folders,
                                         "source_files": source_files,
                                         "music_folder": str(music_folder), "playlist_output": str(playlist_output),
                                         "share_folder": str(Path(share_folder).expanduser().resolve()) if share_folder else "",
                                         "sockseek_binary": str(Path(body.get("sockseek_binary") or SETTINGS["sockseek_binary"]).expanduser().resolve()),
                                         "nicotine_binary": str(Path(body.get("nicotine_binary") or SETTINGS.get("nicotine_binary") or "").expanduser().resolve()) if body.get("nicotine_binary") or SETTINGS.get("nicotine_binary") else "",
                                         "auto_download": bool(body.get("auto_download", SETTINGS.get("auto_download"))),
                                         "audio_store": audio_store,
                                         "bulk_download_format": download_format})
                        SETTINGS.pop("csv_folder", None)
                        # In a portable room an explicit list folder is an input:
                        # an offline drive must stay absent when saving host settings.
                        if DB != folder / "collection.sqlite3":
                            lists_folder.mkdir(parents=True, exist_ok=True)
                        for directory in (music_folder, playlist_output):
                            directory.mkdir(parents=True, exist_ok=True)
                        if changed_paths:
                            with REFRESH_LOCK:
                                apply_root(folder)
                        if body.get("soulseek_username") or body.get("soulseek_password"):
                            write_sockseek_credentials(str(body.get("soulseek_username", "")).strip(), str(body.get("soulseek_password", "")))
                        if body.get("share_password"):
                            if not SETTINGS["share_folder"]:
                                raise ValueError("Choose a share folder before saving the sharing account")
                            write_share_profile(str(body.get("share_username", "")).strip(), str(body.get("share_password", "")), SETTINGS["share_folder"])
                            SETTINGS["share_username"] = str(body.get("share_username", "")).strip()
                        elif SETTINGS.get("share_folder") and share_config_path().exists():
                            config = configparser.ConfigParser(interpolation=None)
                            config.read(share_config_path())
                            saved_user = config.get("server", "login", fallback="")
                            entered_user = str(body.get("share_username", "")).strip()
                            if entered_user and entered_user != saved_user:
                                raise ValueError("Enter the sharing password to change the account")
                            write_share_profile(saved_user,
                                                config.get("server", "passw", fallback=""), SETTINGS["share_folder"])
                        if not worker_running("downloads") and not worker_running("requests"):
                            refresh_library(force=changed_paths)
                        save_settings()
                    except Exception:
                        SETTINGS.clear()
                        SETTINGS.update(previous)
                        if changed_paths:
                            with REFRESH_LOCK:
                                apply_root(old_root)
                        raise
                snapshot = settings_snapshot(self.server.server_port, self.headers.get("Host"), self.server.server_address[0])
                snapshot["spotify_redirect_uri"] = self._spotify_redirect_uri()
                return self.json_response(snapshot)
            except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
                return self.error_json(str(error)[:300])
        if self.path == "/api/sync":
            try:
                if worker_running("downloads") or worker_running("requests"):
                    return self.json_response({"status": "Changes will sync after the current batch"}, HTTPStatus.ACCEPTED)
                refresh_library()
                return self.json_response({"status": "Library synced"})
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                return self.error_json(str(error)[:300])
        if self.path == "/api/playlists/merge":
            try:
                with PLAYLIST_EDIT_LOCK:
                    return self.playlist_merge(body)
            except (ValueError, OSError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired, csv.Error) as error:
                return self.error_json(str(error)[:300])
        index = body.get("track_id")
        recording_id = body.get("musicbrainz_id", "")
        version = performance = ""
        if index is not None:
            if catalog.active_record_at(index) is None:
                return self.error_json("Unknown track")
            catalog.refresh()
            if catalog.coverage.get(index, {}).get("status") == "verified":
                return self.error_json("Already available", HTTPStatus.CONFLICT)
            record = catalog.record_at(index)
            artist, title, album, length = (record[key] for key in ("artist", "title", "album", "length"))
        else:
            artist = str(body.get("artist", "")).strip()
            title = str(body.get("title", "")).strip()
            album = ""
            length = None
            if not title or len(artist) > 200 or len(title) > 200:
                return self.error_json("Enter a song title, up to 200 characters")
        search_title = str(body.get("search_title", "")).strip()
        if "musicbrainz_id" in body:
            if index is not None or not discovery_trail.valid_id(recording_id):
                return self.error_json("Choose a recording from Deep Dive.")
            length = body.get("duration")
            if type(length) not in (int, float) or not 0 < length <= 86400:
                return self.error_json("This recording needs a known duration to find a verified copy.")
            version = discovery_trail.clean(body.get("version"))
            performance = discovery_trail.clean(body.get("performance"))
            album = discovery_trail.clean(body.get("album"))
            search_title = " ".join(part for part in (title, version, performance) if part)[:200]
        if len(search_title) > 200:
            return self.error_json("Search title is too long")
        row, created = requests_store().create_or_find(
            index, artist, title, album, length, search_title, individual=True, musicbrainz_id=recording_id, version=version, performance=performance)
        if recording_id and row["status"] == "downloaded" and row["audio_path"]:
            row["audio"] = f"/api/audio/custom/{row['id']}"
        return self.json_response(row, HTTPStatus.CREATED if created else HTTPStatus.OK)

    def playlist_add_song(self, body):
        key = body.get("playlist")
        # A folder switch waits for the edit. Audio range requests use ROOT_LOCK
        # and remain available throughout a Spotify lookup or a catalog rebuild.
        with PLAYLIST_EDIT_LOCK:
            if not self._can_add_room_song():
                raise ValueError("Playlist permission changed. Reopen the playlist.")
            with REFRESH_LOCK:
                if not isinstance(key, str) or key not in catalog.playlists:
                    raise ValueError("Reopen the playlist and try again.")
                destinations = spotify_destinations().get(key, {})
                record = None
                if body.get("track_id") is not None:
                    record = catalog.active_record_at(body["track_id"])
                    if not record:
                        raise ValueError("This library song is no longer available.")
                song = playlist_edits.catalog_row(record) if record else playlist_edits.metadata(body)
                if destinations:
                    source_key = next((source for source, link in destinations.items()
                                       if link["playlist_id"] == body.get("playlist_id")
                                       and link["account_id"] == body.get("account_id")), None)
                    if source_key is None:
                        raise ValueError("Select the linked Spotify playlist again.")
                else:
                    if body.get("playlist_id") or body.get("account_id") or playlist_source(key) != "local":
                        raise ValueError("This playlist follows its source. Reopen it to check the link.")
                    if not record:
                        matches = playlist_edits.matching_metadata(
                            (row for row in catalog.records if row.get("active", True)), song)
                        if len(matches) == 1:
                            song = playlist_edits.catalog_row(matches[0])
                    path = next((file for file in xspf_files(source_dir()) if file.stem == key), None)
                    if not path or path.is_symlink():
                        raise ValueError("This playlist cannot be edited in the room.")
                    def append(path):
                        return playlist_edits.appended_document(path, song)
                    projection = list_projection()
                    if projection is None:
                        content = append(path)
                        changed = content is not None
                        if changed:
                            place_lists(path.parent, [(path.name, content)], "replace")
                    else:
                        changed = projection.edit_document(path.name, append)
            if destinations:
                request = {**body, **song}
                if record and not request.get("track") and record.get("uri", "").startswith("spotify:track:"):
                    request["track"] = record["uri"]
                result = spotify.add_metadata(source_key, body.get("account_id"), request)
                message = result["message"]
            else:
                message = "Song added to the playlist." if changed else "This song is already in the playlist."
            if worker_running("downloads") or worker_running("requests"):
                message += " The library view will refresh after the current job."
            elif not destinations:
                try:
                    refresh_library()
                except (OSError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired):
                    MAINTENANCE_WAKE.set()
                    message += " The song is saved. A library refresh is pending."
            return {"message": message}

    def playlist_merge(self, body):
        """POST /api/playlists/merge (plan 046): union playlists into
        a portable list file, written through plan 011's import
        machinery. Host-only: the do_POST host set refuses paired
        callers before this runs. Sources are read, never written;
        a source-owned (magnet) playlist can feed a merge but never
        receive one, because its owning sync would overwrite
        whatever was merged into it."""
        raw_sources = body.get("sources")
        if not isinstance(raw_sources, list):
            raise ValueError("Unknown playlist")
        sources = []
        for key in raw_sources:
            if not isinstance(key, str) or key not in catalog.playlists:
                raise ValueError("Unknown playlist")
            if key not in sources:
                sources.append(key)
        target = body.get("target")
        if not isinstance(target, dict):
            raise ValueError("Choose where the merged songs go")
        folder = source_dir()
        folder.mkdir(parents=True, exist_ok=True)
        if "playlist" in target:
            target_key = target.get("playlist")
            if not isinstance(target_key, str) or target_key not in catalog.playlists:
                raise ValueError("Unknown playlist")
            if playlist_source(target_key) != "local":
                raise ValueError("That playlist is kept in sync from its source; merging into it "
                                 "would be overwritten by the next sync. Choose one of your own "
                                 "lists, or merge into a new list.")
            if not any(key != target_key for key in sources):
                raise ValueError("Choose at least one other playlist to merge into this list")
            ordered = [target_key] + [key for key in sources if key != target_key]
            filename = f"{target_key}.xspf"
            # The rewrite carries the target's current resolved
            # display name (the overview's precedence), so a merge
            # can never rename the list it extends.
            title = playlist_display_name(target_key)
        elif "name" in target:
            title = str(target.get("name") or "").strip()
            if not title or len(title) > 120:
                raise ValueError("Enter a name for the merged list")
            if len(sources) < 2:
                raise ValueError("Choose at least two playlists to merge")
            ordered = sources
            target_key = None
            stem = re.sub(r"[^\w-]+", "_", title, flags=re.UNICODE).strip("_")[:55] or "playlist"
            filename = f"{stem}.xspf"
        else:
            raise ValueError("Choose where the merged songs go")
        # The union, computed from the list files so it is exact
        # even between refreshes: target's rows first, then each
        # source's unseen rows in that source's order, keyed by
        # prepare's identity rule. A row without a title or an
        # artist has no identity; it is skipped and counted.
        rows = []
        seen = set()
        skipped = 0
        base = 0
        for key in ordered:
            if key.startswith("linked-") and playlist_links.is_group(DB, key):
                source_rows = []
                for index in catalog.playlists[key]:
                    record = catalog.record_at(index) if index else None
                    if record:
                        source_rows.append(dict(record, identifier=record["uri"],
                                                duration_ms=round(float(record["length"] or 0) * 1000)))
            else:
                source_rows = read_list(folder / f"{key}.xspf")
            for row in source_rows:
                if not row.get("title") or not row.get("artist"):
                    skipped += 1
                    continue
                identity = track_key(row)
                if identity in seen:
                    continue
                seen.add(identity)
                rows.append(row)
            if key == target_key:
                base = len(seen)
        if not rows:
            raise ValueError("There is nothing to merge: the selected lists have no songs "
                             "with both a title and an artist.")
        identity = catalog_imports.LIST_ID if catalog_imports.is_import_list(folder / filename) else ""
        content = write_xspf(title, rows, identity)
        mode = "replace" if target_key is not None else body.get("on_conflict")
        names, conflicts = import_lists([(filename, content)], mode)
        if conflicts:
            return self.json_response(
                {"error": f"A list already exists with that name: {conflicts[0]}",
                 "conflicts": conflicts}, HTTPStatus.CONFLICT)
        filename = names[0]
        if not worker_running("downloads") and not worker_running("requests"):
            refresh_library(force=True)
        return self.json_response({"file": filename, "name": title, "tracks": len(rows),
                                   "added": len(rows) - base, "skipped_rows": skipped,
                                   "status": "Merged into a portable music list; library sync queued"})

    def replica_allowed(self):
        device = device_store.verify(self._presented_cookie())
        if device and device.get("offline_sync"):
            return True
        self.error_json("On the server, open Settings → Devices and allow offline sync for this phone.",
                        HTTPStatus.FORBIDDEN)
        return False

    def receive_original(self):
        key = self.headers.get("X-Audio-Sha256", "")
        suffix = self.headers.get("X-Audio-Suffix", "")
        collection = self.headers.get("X-Listening-Collection", "")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            duration = float(self.headers.get("X-Audio-Duration", "0"))
            title = unquote(self.headers.get("X-Audio-Title", "")).strip()
            if (not 0 < size <= offline_replica.MAX_UPLOAD or not math.isfinite(duration) or duration < 0
                    or not 0 < len(title) <= 512 or not re.fullmatch(r"[a-f0-9]{64}", key)
                    or suffix not in AUDIO_FORMATS or self.headers.get("Content-Type") != "application/octet-stream"):
                raise ValueError("Invalid original audio upload")
            with ROOT_LOCK:
                if collection != offline_replica.collection_id(CONFIG.media_database):
                    raise ValueError("The paired server is serving a different collection")
                scratch = CONFIG_PATH.parent
            if shutil.disk_usage(scratch).free < size:
                return self.error_json("Server has insufficient space for this original", HTTPStatus.INSUFFICIENT_STORAGE)
            with tempfile.NamedTemporaryFile(dir=scratch, suffix=suffix) as temp:
                remaining = size
                self.connection.settimeout(30)
                while remaining:
                    chunk = self.rfile.read(min(1 << 18, remaining))
                    if not chunk:
                        raise ValueError("Original audio transfer interrupted")
                    temp.write(chunk)
                    remaining -= len(chunk)
                temp.flush()
                with ROOT_LOCK:
                    if not self.replica_allowed():
                        return
                    if collection != offline_replica.collection_id(CONFIG.media_database):
                        raise ValueError("The paired server is serving a different collection")
                    store = MediaStore(CONFIG.media_database)
                # Write only to the collection captured above. Encoding and probe I/O
                # do not occupy the lock needed to open a playback stream.
                offline_replica.accept_original(Path(temp.name), key, title, duration, store)
                PHONE_IMPORTS.set()
                MAINTENANCE_WAKE.set()
            return self.json_response({"hash": key, "collection": collection})
        except (ValueError, OSError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired) as error:
            return self.error_json(str(error))

    def serve_audio(self, path, *, replica=False, profile="original", save=False, recording=None, traffic_key=None):
        parts = path.split("/")
        # Plan 060: a blob-backed track resolves to its staged
        # buffer file here; range serving below consumes the
        # resolved path exactly as it consumes a library file.
        try:
            with ROOT_LOCK:
                if replica:
                    if not self.replica_allowed():
                        return
                    if self.headers.get("X-Listening-Collection") != offline_replica.collection_id(CONFIG.media_database):
                        return self.error_json("The server is serving a different collection", HTTPStatus.CONFLICT)
                if recording is not None:
                    record = catalog.active_record_at(int(parts[3])) if len(parts) == 4 else None
                    if record is None or hiking.recording_binding(member_collection_identity(ROOT), record) != recording:
                        return self.error_json("This song changed. Discard this trip and make new mixes.", HTTPStatus.CONFLICT)
                store, buffer = MEDIA.serving()
                if len(parts) == 4:
                    audio = audio_resolution.track_file(int(parts[3]), catalog, LIBRARY, store, buffer)
                elif len(parts) == 5 and parts[3] == "custom":
                    audio = audio_resolution.custom_file(int(parts[4]), requests_store(), LIBRARY, store, buffer)
                else:
                    audio = None
                if audio is None:
                    raise ValueError
                handle = audio.open("rb")
                source_hash = audio.stem if buffer and audio.parent == buffer.directory else None
        except (ValueError, OSError):
            return self.error_json("Audio unavailable", HTTPStatus.NOT_FOUND)
        with handle:
            if profile == "original":
                if save:
                    return self.send_audio_range(handle, audio.name, checksum=source_hash or compact_audio.digest(handle))
                return self.send_audio_range(handle, audio.name, traffic_key=traffic_key)
            try:
                with compact_audio.open_copy(
                    STATE / "compact-audio", handle, audio.name, profile, source_hash=source_hash,
                ) as (copy, name):
                    checksum = compact_audio.digest(copy) if save else None
                    return self.send_audio_range(copy, name, checksum=checksum, traffic_key=traffic_key)
            except OSError:
                return self.error_json("Compact audio could not be prepared. Retry or choose Original.",
                                       HTTPStatus.SERVICE_UNAVAILABLE)

    def send_audio_range(self, handle, name, *, checksum=None, traffic_key=None):
        global ACTIVE_AUDIO_TRANSFERS
        with ROOT_LOCK:
            if update_draining():
                return self.error_json("The room is restarting. Try again shortly.", HTTPStatus.SERVICE_UNAVAILABLE)
            ACTIVE_AUDIO_TRANSFERS += 1
        try:
            return self._send_audio_range(handle, name, checksum=checksum, traffic_key=traffic_key)
        finally:
            with ROOT_LOCK:
                ACTIVE_AUDIO_TRANSFERS -= 1

    def _send_audio_range(self, handle, name, *, checksum, traffic_key):
        size = os.fstat(handle.fileno()).st_size
        start, end = 0, size - 1
        range_header = self.headers.get("Range", "")
        etag = f'"{checksum}"' if checksum else None
        if self.headers.get("If-Range") not in (None, etag):
            range_header = ""
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not match or (not match[1] and not match[2]):
                return self.error_json("Invalid range", HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            if match[1]:
                start = int(match[1])
                end = int(match[2]) if match[2] else end
            else:
                start = max(0, size - int(match[2]))
            if start >= size or end < start:
                return self.error_json("Range outside file", HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            end = min(end, size - 1)
        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if range_header else HTTPStatus.OK)
        self.send_header("Content-Type", {"m4a": "audio/mp4", "m4b": "audio/mp4", "opus": "audio/ogg"}.get(
            Path(name).suffix.lower().lstrip(".")) or mimetypes.guess_type(name)[0] or "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        if etag:
            self.send_header("ETag", etag)
            self.send_header("X-Audio-Extension", Path(name).suffix.lower())
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Content-Length", str(length))
        if range_header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        handle.seek(start)
        while length:
            chunk = handle.read(min(length, 256 * 1024))
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
            if traffic_key is not None:
                PLAYBACK_TRAFFIC.record(traffic_key, sent=len(chunk))
            length -= len(chunk)

    def serve_static(self, path):
        relative = dist_relative(path)
        if not is_public_static(relative):
            return self.error_json("Not found", HTTPStatus.NOT_FOUND)
        root = DIST.resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return self.error_json("Not found", HTTPStatus.NOT_FOUND)
        is_showcase = target.is_relative_to(root / "assets/about-showcase")
        is_about = target == root / "about/index.html" or is_showcase
        if is_about and os.environ.get("LISTENING_ROOM_ABOUT_TAILNET_ONLY") == "1":
            peer = ipaddress.ip_address(self.client_address[0])
            if not (peer.is_loopback or peer in tailscale.IPV4 or peer in tailscale.IPV6):
                return self.error_json("Not found", HTTPStatus.NOT_FOUND)
            return self._send_static_file(target, "private, no-store")
        if is_showcase:
            return self._send_static_file(target, "no-store")
        cache = ("public, max-age=31536000, immutable" if relative.startswith("assets/")
                 else "no-cache" if target.suffix in {".html", ".js", ".css"}
                 else "public, max-age=86400")
        return self._send_static_file(target, cache)

    def _send_static_file(self, target, cache_control, status=HTTPStatus.OK):
        payload = target.read_bytes()
        compressed = (target.suffix in _COMPRESSIBLE_SUFFIXES
                      and len(payload) >= GZIP_STATIC_MIN_BYTES
                      and self._accepts_gzip())
        if compressed:
            payload = _gzip_cached(target, payload)
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self._send_encoding_headers(compressed)
        self.send_header("Cache-Control", cache_control)
        self.send_header("Referrer-Policy", "no-referrer")
        if target.suffix == ".html" and self._explicit_owner():
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)


def init(config_path=None):
    """Build the application state, in the old import-time order.

    Config load, workspace creation, legacy list migration, config
    write-back, then the refresh pipeline, the Catalog, the playback
    session, and the Spotify sync. Called once by main(); tests call it
    against a temp workspace. Importing this module does none of it.
    """
    global CONFIG, CONFIG_PATH, SETTINGS, ROOT, STATE, LIBRARY, PLAYLIST_OUTPUT
    global DB, RUNNER_LOG
    global SUPERVISOR, REFRESH, MEDIA, catalog, playback, play_events, favorites, spotify, metadata, updater, jobs, device_store, artist_connections
    global owner_auth, member_profiles, ROOM_ORIGIN, REQUIRE_OWNER, jams, UPDATE_DRAIN_UNTIL, RADIO_PLAYERS, music_profiles
    jams = JamDirectory()
    REQUIRE_OWNER = os.environ.get("LISTENING_ROOM_REQUIRE_OWNER") == "1"
    CONFIG = (AppConfig.from_environment(REPO_ROOT) if config_path is None
              else AppConfig(Path(config_path), REPO_ROOT))
    SETTINGS = CONFIG.load()
    UPDATE_DRAIN_UNTIL = 0.0
    RADIO_PLAYERS = Presence(time.monotonic, 55, 128)
    ROOM_ORIGIN = configured_origin(os.environ.get("LISTENING_ROOM_ORIGIN") or SETTINGS.get("advertised_origin", ""))
    CONFIG_PATH = CONFIG.config_path
    owner_auth = OwnerAuth(CONFIG_PATH.parent)
    ROOT = CONFIG.root
    ROOT.mkdir(parents=True, exist_ok=True)
    if not SETTINGS.get("lists_folder"):
        old_lists = Path(SETTINGS.get("csv_folder")
                         or (ROOT / "playlist" if (ROOT / "playlist").is_dir() else ROOT))
        migrate_legacy_lists(old_lists, ROOT / "lists")
        SETTINGS["lists_folder"] = str(ROOT / "lists")
    CONFIG.save()
    STATE = CONFIG.state_dir
    LIBRARY = CONFIG.library
    PLAYLIST_OUTPUT = CONFIG.playlist_output
    DB = CONFIG.database
    member_profiles = MemberProfiles(host_database(DB, STATE), ROOT, member_collection_identity(ROOT))
    RUNNER_LOG = CONFIG.runner_log
    # The supervisor (ADR 0001): constructing it runs the boot-time
    # adoption pass over the application folder's pidfiles, so a
    # worker still alive from before a server restart is re-adopted
    # here instead of being started a second time.
    SUPERVISOR = Supervisor(CONFIG_PATH.parent)
    # The media database's one door for this process (plan 061
    # step 1): the adapter reads the globals live, so the catalog
    # rebuild in apply_root() needs no rewiring here.
    MEDIA = MediaAccess(CONFIG.media_database, CONFIG.buffer_dir,
                        STATE / "export-status.json", _MediaSources())
    REFRESH = RefreshPipeline(inputs=refresh_inputs, canonical_dir=source_dir,
                              stored_signature=lambda: catalog_store().source_signature(require_source_index=True),
                              audio_signature=lambda: shared_music.audio_signature(LIBRARY, shared_music.music_inbox(SETTINGS)) if LIBRARY is not None else {},
                              stored_audio_signature=lambda: catalog_store().audio_signature(),
                              report_exists=lambda: catalog_store().has_report_for_build() if shared_music.music_inbox(SETTINGS) else catalog_store().has_report(),
                              sync=sync_sources, rebuild=rebuild_library,
                              reload=lambda: catalog.reload())
    catalog = Catalog(catalog_paths())
    catalog.refresh()
    play_events = PlayEventsStore(DB)
    favorites = FavoritesStore(DB)
    playback = PlaybackSession(available_tracks, resolve_ref,
                               state_path=CONFIG_PATH.parent / "playback.json",
                               genre_order=genre_flow_order,
                               record_event=record_play_event)
    device_store = DeviceStore(CONFIG_PATH.parent / "devices.json")
    spotify = SpotifySync(CONFIG_PATH.parent, lambda: SETTINGS.get("spotify_client_id", ""),
                          lambda: source_dir(), spotify_library_changed, on_remove=spotify_source_removed, database=DB)
    metadata = ExternalMetadata(lambda: RequestsStore(host_database(DB, STATE)))
    artist_connections = ArtistConnections(metadata)
    music_profiles = MusicProfiles(artist_connections)
    updater = Updater(REPO_ROOT)
    jobs = Jobs(queue=JobQueue(host_database(DB, STATE)), worker_running=worker_running,
                start_worker=start_job_worker, stop_worker=stop_worker,
                genre_tools_ready=genre_tools_ready, download_ready=download_ready,
                acquisition_ready=lambda: download_ready(for_acquisition=True),
                lists_ready=lambda: bool(manifest_signature(source_dir())),
                refresh_library=refresh_library, save_settings=save_settings,
                settings=SETTINGS, catalog=lambda: catalog,
                pending_requests=lambda: requests_store().active_count(),
                playlist_options=playlist_options,
                media_dead_bytes=lambda: MEDIA.dead_bytes(),
                draining=update_draining,
                migration_status=migration_status)
    # Plan 062: the one-time library migration is owed or it is
    # not; init settles which (queueing the job, or stamping the
    # completion marker on a room with nothing to move).
    PHONE_IMPORTS.set()
    advance_library_migration()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    from catalog.collection_snapshot import collection_access, create_empty_collection, portable_room_access
    config = resolve_config_path().expanduser().resolve()
    with server_control.own_listener(config) as control, collection_access(config.parent):
        with ThreadingHTTPServer((args.host, args.port), Handler) as httpd:
            root = default_root()
            legacy = legacy_config_path(config)
            if (not config.exists() and not (legacy and legacy.exists())
                    and not any(path.name != OWNER_DATABASE_NAME for path in config.parent.glob("*.sqlite3"))
                    and (not root.exists() or (root.is_dir() and not any(root.iterdir())))):
                create_empty_collection(root)
            # Lock the selected portable root before init hydrates lists or opens SQLite.
            selected = config if config.is_file() else legacy
            if selected and selected.is_file():
                try:
                    root = Path(json.loads(selected.read_text()).get("folder") or root).expanduser()
                except (ValueError, OSError):
                    pass
            with portable_room_access(root, config.parent):
                init()
                run_listener(httpd, control)


def run_listener(httpd, control):
    # The one logger this app configures (plan 022): watchdog failures
    # and recoveries, to stderr (launchers redirect it to server.log,
    # and the plan 023 restarter appends to the same file).
    if not WATCHDOG_LOGGER.handlers:
        watchdog_handler = logging.StreamHandler(sys.stderr)
        watchdog_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        WATCHDOG_LOGGER.addHandler(watchdog_handler)
    WATCHDOG_LOGGER.setLevel(logging.INFO)
    WATCHDOG_LOGGER.propagate = False
    # Recover the previous process's playback window after an unclean stop.
    MEDIA.release_playback_buffer()
    requests_store().initialize()
    threading.Thread(target=maintenance_watchdog, daemon=True, name="library-watchdog").start()
    print(f"State database: {DB} (SQLite is the source of state)", flush=True)
    print(f"Listening room: http://127.0.0.1:{httpd.server_port}", flush=True)
    try:
        server_control.serve(httpd, control, drain=update_drain)
    finally:
        with ROOT_LOCK:
            MEDIA.release_playback_buffer()


if __name__ == "__main__":
    main()
