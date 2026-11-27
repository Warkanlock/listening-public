"""Application configuration: paths, settings, and the pairing token.

Owns everything server.py used to do at import time: resolving the
app-data path, loading or creating the settings file, filling defaults,
and the pairing token lifecycle (read the stored token, or generate one
once and persist it at 0600 through system.private_files).

The macOS support folder was renamed from OfflineMusic to
ListeningMusic (plan 032): load() adopts the legacy folder's settings
and device store on first run when the new config file does not exist
yet.

The application folder (the folder holding the settings file) holds
configuration and state: the settings, the device store, the one
SQLite state database, the runner and playback files, and, under
``state-backup/``, the legacy state files the database superseded.
The workspace folder holds the library only: the portable lists, the
audio, the generated playlists, and the reports. This module is the
one resolver of application-folder paths (plan 043); server, runner,
stores, and scripts consume its properties or values derived from
them, and nobody else joins "state" or "requests.sqlite3" onto a
root. load() adopts a workspace ``state/`` folder left by earlier
versions into the application folder on first run, verified before
and after, in the plan 032 pattern.

Pure given a base path: constructing AppConfig and calling load() touch
only the config file it points at, so tests build it against a temp
dir. Nothing in this module touches the filesystem at import.
"""

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from playback.radio import migrate_providers
from system.formats import bulk_download_format
from system.private_files import private_write

SUPPORT_FOLDER_NAME = "ListeningMusic"
LEGACY_SUPPORT_FOLDER_NAME = "OfflineMusic"
CONFIG_FILE_NAME = "listening-room.json"


def collection_databases(root, application_folder):
    """A restored collection holds state and media together. Existing rooms
    keep their existing database layout until explicitly adopted."""
    collection = Path(root) / "collection.sqlite3"
    if collection.is_file():
        return collection, collection
    return Path(application_folder) / "requests.sqlite3", Path(root) / "media.sqlite3"


def host_database(database, application_folder):
    """Host caches and worker jobs stay with this installation."""
    return (Path(application_folder) / "host.sqlite3"
            if Path(database).name == "collection.sqlite3" else database)


def default_config_path(platform=None):
    """The per-platform app-data location of the settings file."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        support = Path.home() / "Library/Application Support"
        current = support / "Listening" / "host" / CONFIG_FILE_NAME
        previous = support / SUPPORT_FOLDER_NAME / CONFIG_FILE_NAME
        # Publishing the new host config selects the verified collection. An
        # in-progress copy on its own must never switch an existing host.
        if current.is_file() or current.with_suffix(".json.room").is_file():
            return current
        if previous.parent.exists() or (support / LEGACY_SUPPORT_FOLDER_NAME / CONFIG_FILE_NAME).is_file():
            return previous
        return current
    return Path.home() / ".config/listening-room/config.json"


def legacy_config_path(config_path):
    """The pre-rename counterpart of a config path, or None.

    Only the macOS support folder was renamed: a config file inside a
    ListeningMusic folder has its legacy counterpart in the sibling
    OfflineMusic folder. Any other location has none.
    """
    config_path = Path(config_path)
    if config_path.parent.name != SUPPORT_FOLDER_NAME:
        return None
    return (config_path.parent.parent / LEGACY_SUPPORT_FOLDER_NAME
            / config_path.name)


def rewrite_support_paths(settings, legacy_dir, new_dir):
    """Repoint settings values that lived in the legacy support folder.

    A value is rewritten when it is the legacy folder itself or a path
    inside it: the prefix is swapped for the new folder. The workspace
    `folder` setting is not a support path and is never rewritten.
    Returns the settings dict, modified in place.
    """
    legacy_dir = str(Path(legacy_dir))
    new_dir = str(Path(new_dir))
    for key, value in list(settings.items()):
        if key == "folder" or not isinstance(value, str):
            continue
        if value == legacy_dir:
            settings[key] = new_dir
        elif value.startswith(legacy_dir + os.sep):
            settings[key] = new_dir + value[len(legacy_dir):]
    return settings


def resolve_config_path(environ=None):
    """The settings file location: $LISTENING_ROOM_CONFIG or the default."""
    environ = os.environ if environ is None else environ
    return Path(environ.get("LISTENING_ROOM_CONFIG", str(default_config_path())))


def default_root(environ=None):
    """Prefer the unified macOS home while retaining an existing legacy room."""
    environ = os.environ if environ is None else environ
    if environ.get("OFFLINE_MUSIC_ROOT"):
        return Path(environ["OFFLINE_MUSIC_ROOT"]).expanduser()
    old = Path.home() / "Music/ListeningRoom"
    if sys.platform == "darwin":
        config = default_config_path()
        if config.parent.name == "host":
            if not old.exists() or config.is_file() or config.with_suffix(".json.room").is_file():
                return config.parent.parent / "collection"
    return old


def merge_playlist_names(base, edited, canonical):
    """Apply owner edits only to names unchanged in the portable room."""
    merged = dict(canonical)
    for key in set(base) | set(edited):
        if edited.get(key) == base.get(key) or canonical.get(key) != base.get(key):
            continue
        if key in edited:
            merged[key] = edited[key]
        else:
            merged.pop(key, None)
    return merged


class AppConfig:
    """The settings dict plus every path derived from it.

    load() reads the file and fills defaults. save() writes it back. Workspace creation and legacy migration are
    the caller's job, in the caller's order.
    """

    def __init__(self, config_path, repo_root, environ=None):
        self.config_path = Path(config_path)
        self.repo_root = Path(repo_root)
        self.environ = os.environ if environ is None else environ
        self.settings = {}
        self.root = None

    @classmethod
    def from_environment(cls, repo_root):
        return cls(resolve_config_path(), repo_root)

    def _adopt_legacy_config(self):
        """Adopt the pre-rename support folder's data on first run.

        When this config file does not exist yet but the legacy
        OfflineMusic one does, copy it (and the device store beside
        it) into the new folder before loading, repointing
        support-folder paths at the new location, and leave the legacy
        folder untouched as a backup. When the new file exists it wins
        and nothing is copied. An unreadable legacy file adopts
        nothing: load() then falls back to fresh defaults, exactly as
        it would have reading the legacy file directly.
        """
        if self.config_path.exists():
            return
        legacy = legacy_config_path(self.config_path)
        if legacy is None or not legacy.is_file():
            return
        try:
            settings = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(settings, dict):
            return
        new_dir = self.config_path.parent
        rewrite_support_paths(settings, legacy.parent, new_dir)
        private_write(self.config_path, json.dumps(settings, indent=2))
        legacy_devices = legacy.parent / "devices.json"
        new_devices = new_dir / "devices.json"
        if legacy_devices.is_file() and not new_devices.exists():
            try:
                private_write(new_devices,
                              legacy_devices.read_text(encoding="utf-8"))
            except OSError:
                pass

    def load(self):
        """Read or create the settings, fill defaults, ensure the token.

        Creates the config directory. A missing or unreadable file
        falls back to a fresh settings dict rooted at the default
        workspace, with a newly generated pairing token.
        """
        self._adopt_legacy_config()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        fallback = str(default_root(self.environ))
        try:
            settings = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = None
        previous_room = isinstance(settings, dict)
        if not previous_room:
            settings = {"folder": fallback, "share_token": secrets.token_urlsafe(24)}
        settings.setdefault("download_engines", {})
        migrate_providers(settings, previous_room=previous_room)
        if not settings.get("share_token"):
            settings["share_token"] = secrets.token_urlsafe(24)
        settings.setdefault("auto_download", False)
        settings["bulk_download_format"] = bulk_download_format(settings.get("bulk_download_format"))
        # Plan 062: where new downloads land, "database" (the
        # media database, the library's source of truth for audio
        # and the default) or "files" (the library folder).
        # Landing only: playback honors blob references whenever
        # the media database exists, whatever this says.
        settings.setdefault("audio_store", "database")
        inbox = self.environ.get("LISTENING_ROOM_MUSIC_INBOX")
        settings.setdefault("source_folders", [str(Path(inbox) / "Playlists")] if inbox else [])
        settings.setdefault("source_files", [])
        settings.setdefault("allowed_hosts", [])
        settings.setdefault("spotify_client_id", "")
        self.settings = settings
        self.root = Path(settings.get("folder", fallback)).expanduser().resolve()
        collection = self.root / "collection.sqlite3"
        if collection.is_file():
            settings["audio_store"] = "database"
            from catalog.collection_projection import ListProjection, workspace_paths
            for key, value in workspace_paths(self.root, self.app_dir).items():
                if not settings.get(key):
                    settings[key] = value
            projection = ListProjection(collection, settings["lists_folder"], self.app_dir)
            projection.reconcile()
            self.loaded_names = projection.playlist_names()
            baseline = settings.get("portable_name_baseline") or {}
            local = settings.get("playlist_names")
            if baseline.get("room") == str(self.root) and isinstance(local, dict):
                settings["playlist_names"] = merge_playlist_names(baseline["names"], local, self.loaded_names)
            else:
                settings["playlist_names"] = dict(self.loaded_names)
        settings.setdefault("playlist_names", {})
        self._adopt_workspace_state()
        return settings

    # Copy legacy state into the application folder while retaining the source.

    LEGACY_STATE_FILES = ("manifest.json", "coverage.csv", "genres.json",
                          "summary.json", "cursor.json", "probe-cache.json")

    def _adopt_workspace_state(self):
        """Copy legacy state into the application folder, publishing the
        verified SQLite backup last. Every original stays at its old path."""
        source = self.root / "state"
        target = self.database
        if not source.is_dir() or target.exists() or source.resolve() == self.app_dir.resolve():
            return
        if self.app_dir.resolve().is_relative_to(source.resolve()):
            raise ValueError("Choose an application folder outside the legacy state folder")
        database = source / "requests.sqlite3"
        with tempfile.TemporaryDirectory(dir=self.app_dir) as temporary:
            staging = Path(temporary)
            backup = staging / "requests.sqlite3"
            if database.is_file():
                try:
                    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as old:
                        with closing(sqlite3.connect(backup)) as copy:
                            old.backup(copy)
                            if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                                raise RuntimeError("Legacy state backup failed its integrity check")
                except sqlite3.Error as error:
                    raise RuntimeError(f"Cannot adopt the state database at {database}: {error}") from error
            for entry in sorted(source.iterdir()):
                if entry.name in {"requests.sqlite3", "requests.sqlite3-wal", "requests.sqlite3-shm"}:
                    continue
                parent = self.state_backup_dir if entry.name in self.LEGACY_STATE_FILES else self.app_dir
                destination = parent / entry.name
                if destination.exists() or destination.is_symlink():
                    continue
                staged = staging / entry.name
                if entry.is_dir():
                    shutil.copytree(entry, staged)
                elif entry.is_file():
                    shutil.copy2(entry, staged)
                else:
                    continue
                parent.mkdir(parents=True, exist_ok=True)
                staged.rename(destination)
            if backup.is_file():
                os.chmod(backup, 0o600)
                os.link(backup, target)

    def save(self):
        collection = self.root / "collection.sqlite3" if self.root is not None else None
        if collection and collection.is_file() and hasattr(self, "loaded_names"):
            names = self.settings["playlist_names"]
            if names != self.loaded_names:
                with closing(sqlite3.connect(collection)) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    remote = json.loads(db.execute("SELECT playlist_names FROM collection_info WHERE version=1").fetchone()[0])
                    remote = merge_playlist_names(self.loaded_names, names, remote)
                    db.execute("UPDATE collection_info SET playlist_names=? WHERE version=1", (json.dumps(remote),))
                    self.settings["playlist_names"] = remote
                    self.loaded_names = dict(remote)
            self.settings["portable_name_baseline"] = {"room": str(self.root), "names": dict(self.loaded_names)}
        private_write(self.config_path, json.dumps(self.settings, indent=2))

    @property
    def app_dir(self):
        """The application folder: the folder holding the settings file.

        The one notion every application-folder path derives from
        (plan 043): settings, devices, the state database, logs, and
        the legacy state files under ``state-backup/`` all live here.
        """
        return self.config_path.parent

    @property
    def state_dir(self):
        """Where the working state lives: the application folder.

        The name is kept because STATE in both processes means the
        app's working-state folder (the database, batch logs,
        the ``requests/`` staging dir); since plan
        043 all of it lives in the application folder, beside the
        settings, not in a ``state/`` folder inside the workspace.
        """
        return self.app_dir

    @property
    def state_backup_dir(self):
        """The legacy state files' home after adoption (plan 043)."""
        return self.app_dir / "state-backup"

    @property
    def library(self):
        return Path(self.settings.get("music_folder") or self.root / "audio").expanduser().resolve()

    @property
    def playlist_output(self):
        return Path(self.settings.get("playlist_output")
                    or self.root / "playlists").expanduser().resolve()

    @property
    def database(self):
        return collection_databases(self.root, self.state_dir)[0]

    @property
    def media_database(self):
        """The media database (plan 060): library content, so it
        lives in the library folder beside the portable lists,
        never in the application folder with the state."""
        return collection_databases(self.root, self.state_dir)[1]

    @property
    def buffer_dir(self):
        """The playback buffer (plan 060): staged plain files for
        blob tracks, a cache in the application folder."""
        return self.app_dir / "buffer"

    @property
    def manifest(self):
        return self.state_backup_dir / "manifest.json"

    @property
    def summary(self):
        return self.state_backup_dir / "summary.json"

    @property
    def coverage(self):
        return self.state_backup_dir / "coverage.csv"

    @property
    def runner_log(self):
        return self.config_path.parent / "runner.log"

    @property
    def session_suffix(self):
        return hashlib.sha256(str(self.config_path.resolve()).encode()).hexdigest()[:8]
