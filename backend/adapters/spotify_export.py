"""Read Spotify account data without connecting to Spotify."""

import io
import json
from dataclasses import dataclass
import re
import stat
import unicodedata
import zipfile
import zlib

from library.library_protocol import track_key, write_xspf


MAX_EXPORT_BYTES = 25 * 1024**2
MAX_EXPANDED_BYTES = 50 * 1024**2
MAX_ARCHIVE_MEMBERS = 512
MAX_PLAYLISTS = 1000
MAX_ITEMS = 100_000
MAX_TEXT = 512
_PLAYLIST_FILE = re.compile(r"playlist(\d{0,6})\.json", re.I | re.ASCII)
_LIBRARY_FILE = "yourlibrary.json"
_SAVED_TRACKS = "spotify-saved-tracks"
_SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{22}")
_INVALID_XML = re.compile(r"[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


@dataclass(frozen=True)
class SpotifyExport:
    lists: tuple[tuple[str, str], ...]
    tracks: int
    skipped: int


def _export_kind(filename):
    if filename.casefold() == _LIBRARY_FILE:
        return (1, 0)
    match = _PLAYLIST_FILE.fullmatch(filename)
    if match:
        return (0, int(match[1] or 0))
    return None


def _json_object(data):
    try:
        document = json.loads(data.decode("utf-8-sig"))
    except (ValueError, RecursionError) as error:
        raise ValueError("Spotify data must contain valid UTF-8 JSON.") from error
    if not isinstance(document, dict):
        raise ValueError("Expected a Spotify account-data JSON object.")
    return document


def _archive_documents(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("The Spotify ZIP contains too many entries.")
            selected = {}
            expanded = 0
            for member in members:
                path = member.orig_filename.rstrip("/").split("/")
                if (member.orig_filename != member.filename or "\\" in member.orig_filename
                        or ":" in path[0] or any(part in {"", ".", ".."} for part in path)):
                    raise ValueError("The Spotify ZIP contains an unsafe path.")
                name = path[-1].casefold()
                kind = _export_kind(name)
                if kind is None or member.is_dir():
                    continue
                if name in selected:
                    raise ValueError("The Spotify ZIP contains duplicate export files.")
                mode = stat.S_IFMT(member.external_attr >> 16)
                if mode not in {0, stat.S_IFREG} or member.flag_bits & 1:
                    raise ValueError("Spotify export files in a ZIP must be regular, unencrypted files.")
                expanded += member.file_size
                if member.file_size > MAX_EXPORT_BYTES or expanded > MAX_EXPANDED_BYTES:
                    raise ValueError("The Spotify ZIP expands past the allowed size.")
                selected[name] = (kind, name, member)
            if not selected:
                raise ValueError("No Playlist or YourLibrary JSON files were found in the Spotify ZIP.")
            for kind, _, member in sorted(selected.values()):
                with archive.open(member) as stream:
                    content = stream.read(MAX_EXPORT_BYTES + 1)
                if len(content) != member.file_size:
                    raise ValueError("A Spotify ZIP entry did not match its declared size.")
                yield kind, _json_object(content)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError) as error:
        raise ValueError("Could not read the Spotify ZIP.") from error


def _text(value):
    if isinstance(value, str) and len(value) <= MAX_TEXT and not _INVALID_XML.search(value):
        return value.strip()
    return ""


def _spotify_uri(value, kind):
    prefix = f"spotify:{kind}:"
    if isinstance(value, str) and value.startswith(prefix) and _SPOTIFY_ID.fullmatch(value[len(prefix):]):
        return value
    return ""


def _track(item, *, saved):
    if not isinstance(item, dict):
        return None
    data = item if saved else item.get("track")
    if not isinstance(data, dict) or item.get("episode") or item.get("localTrack"):
        return None
    title = _text(data.get("track" if saved else "trackName"))
    artist = _text(data.get("artist" if saved else "artistName"))
    if not title or not artist:
        return None
    uri = data.get("uri" if saved else "trackUri")
    if isinstance(uri, str) and uri.startswith(("spotify:episode:", "spotify:local:")):
        return None
    duration = data.get("durationMs", data.get("duration_ms"))
    return {"title": title, "artist": artist, "album": _text(data.get("album" if saved else "albumName")),
            "identifier": _spotify_uri(uri, "track"),
            "duration_ms": duration if type(duration) is int and 1 <= duration <= 86_400_000 else None}


def _safe_filename(title, taken):
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title).strip("-")[:64].rstrip("-")
    stem = f"spotify-{slug or 'playlist'}"
    name = stem
    suffix = 2
    while name in taken:
        name = f"{stem}-{suffix}"
        suffix += 1
    taken.add(name)
    return name + ".xspf"


def _array(document, key):
    rows = document.get(key)
    if not isinstance(rows, list):
        raise ValueError(f"Spotify export JSON must contain a {key} list.")
    return rows


def _unique_saved(tracks):
    seen = set()
    for track in tracks:
        key = track_key(track)
        if key not in seen:
            seen.add(key)
            yield track


def parse_export(filename, data):
    """Return ordered portable music playlists and counts of skipped items."""
    if not data or len(data) > MAX_EXPORT_BYTES:
        raise ValueError("Choose a Spotify export between 1 byte and 25 MiB.")
    kind = _export_kind(filename)
    if filename.casefold().endswith(".zip"):
        documents = _archive_documents(data)
    elif kind is not None:
        documents = [(kind, _json_object(data))]
    else:
        raise ValueError("Choose a Spotify account-data ZIP, Playlist JSON, or YourLibrary.json.")

    lists = []
    taken = {_SAVED_TRACKS}
    track_count = skipped = playlist_count = item_count = 0
    for (saved, _), document in documents:
        playlists = [document] if saved else _array(document, "playlists")
        playlist_count += len(playlists)
        if playlist_count > MAX_PLAYLISTS:
            raise ValueError("The Spotify export contains too many playlists.")
        for playlist in playlists:
            title = "Spotify Saved Tracks" if saved else _text(playlist.get("name") if isinstance(playlist, dict) else None)
            if not title or (not saved and not isinstance(playlist.get("items"), list)):
                skipped += 1
                continue
            items = _array(playlist, "tracks" if saved else "items")
            unsupported = sum(len(entries) for name in ("albums", "artists", "shows", "episodes", "audiobooks")
                              if saved and isinstance(entries := playlist.get(name), list))
            item_count += len(items) + unsupported
            if item_count > MAX_ITEMS:
                raise ValueError("The Spotify export contains too many items.")
            tracks = [track for item in items if (track := _track(item, saved=saved)) is not None]
            skipped += len(items) - len(tracks) + unsupported
            if saved:
                tracks = list(_unique_saved(tracks))
                if not tracks:
                    continue
                output = _SAVED_TRACKS + ".xspf"
                identifier = ""
            else:
                output = _safe_filename(title, taken)
                identifier = _spotify_uri(playlist.get("playlistUri") or playlist.get("uri"), "playlist")
            lists.append((output, write_xspf(title, tracks, identifier=identifier)))
            track_count += len(tracks)
    return SpotifyExport(tuple(lists), track_count, skipped)
