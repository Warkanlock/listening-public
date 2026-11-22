"""Read-only audio intake from the Music share and the private export destination."""

import os
from pathlib import Path

from system.formats import AUDIO_FORMATS
from system.refresh import directory_signature


def music_inbox(settings=None):
    return os.environ.get("LISTENING_ROOM_MUSIC_INBOX") or (settings or {}).get("music_inbox", "")


def runner_inbox(settings):
    folder = music_inbox(settings)
    return {"LISTENING_ROOM_MUSIC_INBOX": folder} if folder else {}


def shared_entries(folder=None):
    """Walk the mounted tree once, ignoring links and files removed mid-scan."""
    inbox = music_inbox() if folder is None else folder
    if not inbox:
        return
    pending = [inbox]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                    elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in AUDIO_FORMATS:
                        yield entry
        except FileNotFoundError:
            continue


def shared_files():
    return [Path(entry.path) for entry in shared_entries()]


def audio_signature(library, folder=None):
    result = directory_signature(library)
    for entry in shared_entries(folder):
        try:
            info = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        result[entry.path] = [info.st_size, info.st_mtime_ns]
    return result


def export_folder(root):
    return Path(os.environ.get("LISTENING_ROOM_EXPORT_DIR") or root / "exported")
