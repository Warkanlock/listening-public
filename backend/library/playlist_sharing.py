"""Build a selected playlist's public metadata without contacting a service."""

from html import escape
import math
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

from library.library_protocol import write_xspf, xspf_playlist


_MBID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_EXPLICIT_RECORDING = re.compile(
    rf"(?:https?://musicbrainz\.org/recording/|musicbrainz:recording:)({_MBID})",
    re.IGNORECASE,
)
_RECORDING_URL = "https://musicbrainz.org/recording/"
_INVALID_XML = re.compile(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def _xml_text(value):
    return _INVALID_XML.sub("", value or "")


def _recording_url(mbid="", uri=""):
    if not re.fullmatch(_MBID, mbid, re.IGNORECASE):
        match = _EXPLICIT_RECORDING.fullmatch(uri)
        if not match:
            return None
        mbid = match[1]
    return _RECORDING_URL + mbid.lower()


def playlist_preview(catalog, playlist_key, *, name=None):
    """Allowlist the publicly shareable fields in a playlist's catalog order."""
    tracks = []
    for index in catalog.playlists[playlist_key]:
        record = catalog.active_record_at(index)
        if record is None:
            continue
        length_ms = (record.get("length") or 0) * 1000
        duration_ms = round(length_ms) if math.isfinite(length_ms) and length_ms > 0 else None
        tracks.append({"title": _xml_text(record["title"]), "artist": _xml_text(record["artist"]),
                       "album": _xml_text(record.get("album")), "duration_ms": duration_ms,
                       "musicbrainz_url": _recording_url(record.get("musicbrainz_id") or "", record.get("uri") or "")})
    return {"name": _xml_text(name or catalog.list_names.get(playlist_key)) or "Playlist", "tracks": tracks}


def playlist_xspf(preview):
    """Write a metadata-only XSPF suitable for importing into other rooms."""
    tracks = [{"title": row["title"], "artist": row["artist"], "album": row["album"],
               "duration_ms": row["duration_ms"], "identifier": row["musicbrainz_url"]}
              for row in preview["tracks"]]
    return write_xspf(preview["name"], tracks).encode("utf-8")


def load_shared_playlist(path):
    """Rebuild the export from metadata even when the input is a third-party XSPF."""
    limit = 8 * 1024 * 1024
    if path.suffix.casefold() != ".xspf" or not path.is_file():
        raise ValueError("Choose a playlist in XSPF format.")
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Choose an XSPF file smaller than 8 MiB.")
    name, tracks = xspf_playlist(data.decode("utf-8-sig"))
    return {"name": name or "Playlist", "tracks": [
        {"title": row["title"], "artist": row["artist"], "album": row["album"],
         "duration_ms": row["duration_ms"], "musicbrainz_url": _recording_url(uri=row["identifier"])}
        for row in tracks if row["title"] and row["artist"]
    ]}


def track_count(preview):
    count = len(preview["tracks"])
    return f"{count} {'track' if count == 1 else 'tracks'}"


def write_public_bundle(preview, destination):
    """Write a standalone web page and a portable playlist to a new ZIP file."""
    if destination.suffix.casefold() != ".zip":
        raise ValueError("Use a new .zip destination for the sharing page.")
    name = escape(preview["name"])
    items = []
    for row in preview["tracks"]:
        title = escape(row["title"])
        if row["musicbrainz_url"]:
            title = f'<a href="{escape(row["musicbrainz_url"])}" rel="noreferrer">{title}</a>'
        album = f' · {escape(row["album"])}' if row["album"] else ""
        items.append(f'<li><strong>{title}</strong> · {escape(row["artist"])}{album}</li>')
    listing = "<ol>\n" + "\n".join(items) + "\n</ol>" if items else ""
    page = f'''<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="no-referrer">
<meta property="og:type" content="website">
<meta property="og:title" content="{name}">
<meta property="og:description" content="{track_count(preview)} · Download the playlist as XSPF">
<title>{name}</title>
<style>
  :root {{ color-scheme: light dark; }}
  main {{ max-width: 42rem; margin: 3rem auto; padding: 0 1rem; font: 1rem/1.6 system-ui, sans-serif; overflow-wrap: anywhere; }}
  li {{ margin-block: .55rem; }}
</style>
<main>
<h1>{name}</h1>
<p>{track_count(preview)} · <a href="playlist.xspf" download>Download playlist (XSPF)</a></p>
{listing}
</main>
</html>
'''
    with TemporaryDirectory(dir=destination.parent, prefix=".listening-playlist-") as scratch:
        archive = Path(scratch) / "playlist.zip"
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
            bundle.writestr("index.html", page)
            bundle.writestr("playlist.xspf", playlist_xspf(preview))
        os.link(archive, destination)
