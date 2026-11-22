"""Find local playlist folders and mirror watched sources into portable lists."""

import csv
import hashlib
import os
import re
from pathlib import Path

from library.library_protocol import (SUPPORTED, derived_path, playlist_identifier, playlist_title,
                                      read_list, write_derived, write_xspf)


SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "cache", "caches"}


def is_music_list(path):
    """Recognize playlist files by content, not only by extension."""
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            sample = handle.read(8192)
    except OSError:
        return False
    suffix = path.suffix.casefold()
    if suffix == ".csv":
        try:
            header = next(csv.reader(sample.splitlines()))
        except (StopIteration, csv.Error):
            return False
        fields = {re.sub(r"[^a-z0-9]", "", name.casefold()) for name in header}
        return bool(fields & {"trackname", "title", "song", "songtitle", "name"}) and bool(
            fields & {"artistnames", "artist", "artists", "performer", "creator"})
    if suffix == ".xspf":
        return "<playlist" in sample and "http://xspf.org/ns/0/" in sample
    if suffix in {".m3u", ".m3u8"}:
        return "#EXTINF:" in sample or any(" - " in line for line in sample.splitlines() if not line.startswith("#"))
    return False


def list_files(folder, max_depth=2, max_files=500, sniff=True):
    """Return a bounded list of supported playlists below one selected folder.

    With sniff=False the content check is skipped and every candidate
    by extension is returned; the refresh pipeline uses that to stat
    watched folders without opening any file contents.
    """
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        return []
    found = []
    for current, directories, files in os.walk(folder, followlinks=False):
        depth = len(Path(current).relative_to(folder).parts)
        directories[:] = sorted(name for name in directories
                                if depth < max_depth and not name.startswith(".")
                                and name.casefold() not in SKIP_DIRS
                                and not (Path(current) / name).is_symlink())
        for name in sorted(files):
            path = Path(current) / name
            if name.startswith(".") or path.suffix.casefold() not in SUPPORTED or path.is_symlink() or (sniff and not is_music_list(path)):
                continue
            found.append(path)
            if len(found) >= max_files:
                return found
    return found


def discover_folders(home, current_folder=None, excluded=(), max_directories=600):
    """Find likely list folders in common user locations, without a whole-disk scan."""
    home = Path(home).expanduser().resolve()
    current_folder = Path(current_folder).resolve() if current_folder else None
    excluded = {Path(path).resolve() for path in excluded}
    if current_folder:
        excluded.add(current_folder)
    roots = [home / name for name in ("Downloads", "Music", "Documents", "Desktop")]
    results = {}
    seen = 0
    for root in roots:
        if not root.is_dir():
            continue
        for current, directories, files in os.walk(root, followlinks=False):
            seen += 1
            if seen > max_directories:
                break
            path = Path(current)
            if any(path == blocked or path.is_relative_to(blocked) for blocked in excluded):
                directories[:] = []
                continue
            depth = len(path.relative_to(root).parts)
            directories[:] = sorted(name for name in directories
                                    if depth < 3 and not name.startswith(".")
                                    and name.casefold() not in SKIP_DIRS
                                    and not (path / name).is_symlink()
                                    and not any((path / name) == blocked for blocked in excluded))
            count = sum(1 for name in files if not name.startswith(".")
                        and Path(name).suffix.casefold() in SUPPORTED
                        and is_music_list(path / name))
            if count and path != current_folder:
                results[str(path)] = {"path": str(path), "count": count,
                                      "name": path.name or str(path)}
        if seen > max_directories:
            break
    return sorted(results.values(), key=lambda item: (-item["count"], item["path"]))[:12]


def mirror_name(source):
    """The lists-folder file name a watched or linked source mirrors
    into: the sanitized stem plus the first 8 hex of the SHA-256 of
    the source's resolved path string, so two sources sharing a file
    name never collide. sync_files() writes through this name and
    source_mirror_kinds() reads the same names back, so the sync and
    the classifier can never drift apart."""
    source = Path(source).expanduser().resolve()
    digest = hashlib.sha256(str(source).encode()).hexdigest()[:8]
    stem = re.sub(r"[^\w-]+", "_", source.stem, flags=re.UNICODE).strip("_")[:55] or "playlist"
    return f"source_{stem}_{digest}.xspf"


def mirror_path(source, destination):
    """The full mirror path of a source inside the lists folder."""
    return derived_path(Path(mirror_name(source)), Path(destination).expanduser().resolve())


def mirror_key(source, destination=None):
    """The catalog key (file stem) of a source's mirror."""
    if destination is not None:
        return mirror_path(source, destination).stem
    return Path(mirror_name(source)).stem


def source_mirror_kinds(settings, destination=None):
    """The catalog keys the current sources own, and how (plan 046).

    Linked files claim "linked"; lists inside watched folders claim
    "watched", never overwriting a "linked" entry (a file both
    linked and inside a watched folder is linked: the file-level
    link is the tighter claim). Keys come from mirror_key(), the
    same computation the sync writes through. A source that leaves
    the settings stops claiming its mirror here, and the mirror
    classifies as a local list from that moment on."""
    kinds = {}
    for raw in settings.get("source_files", []):
        kinds[mirror_key(raw, destination)] = "linked"
    for folder in settings.get("source_folders", []):
        for source in list_files(folder):
            kinds.setdefault(mirror_key(source, destination), "watched")
    return kinds


def sync_folders(folders, destination):
    """Copy changed external lists into the canonical XSPF directory."""
    sources = (source for folder in folders for source in list_files(folder))
    return sync_files(sources, destination)


def sync_files(sources, destination):
    """Mirror individually linked list files, preserving their source paths."""
    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    imported, errors = [], []
    for source in sources:
        source = Path(source).expanduser().resolve()
        if source.is_relative_to(destination) or not source.is_file() or not is_music_list(source):
            continue
        try:
            tracks = read_list(source)
            title = playlist_title(source) or source.stem
            identifier = playlist_identifier(source)
            contents = write_xspf(title, tracks, identifier).encode()
            legacy = write_xspf(title, tracks).encode() if identifier else contents
            target = write_derived(Path(mirror_name(source)), destination, contents, legacy_body=legacy)
            if target is not None:
                imported.append(target)
        except (OSError, UnicodeError, ValueError, csv.Error) as error:
            errors.append({"source": str(source), "error": str(error)})
    return imported, errors
