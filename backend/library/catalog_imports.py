"""Import a recording's description into a regular portable library playlist."""

import re
import xml.etree.ElementTree as ET

from library import playlist_edits
from library.library_protocol import NS, read_list, write_xspf
from library.list_intake import place_lists
from metadata.artist_connections import valid_id

FILENAME = "listening_catalog_imports.xspf"
LIST_ID = "urn:listening:catalog-imports"


def is_import_list(path):
    if path.name != FILENAME or not path.is_file():
        return False
    try:
        return ET.parse(path).getroot().findtext(f"{{{NS}}}identifier") == LIST_ID
    except ET.ParseError:
        return False


def song(body):
    row = playlist_edits.metadata(body)
    mbid = body.get("musicbrainz_id")
    apple_id = body.get("apple_id")
    if valid_id(mbid) and apple_id is None:
        row["identifier"] = "https://musicbrainz.org/recording/" + mbid
    elif isinstance(apple_id, str) and re.fullmatch(r"[1-9][0-9]{0,18}", apple_id) and mbid is None:
        row["identifier"] = "urn:apple:track:" + apple_id
    else:
        raise ValueError("Choose a recording from a song catalog.")
    duration = body.get("duration_ms")
    if type(duration) is int and 0 < duration <= 86400000:
        row["duration_ms"] = duration
    return row


def existing(records, row):
    prefix = "https://musicbrainz.org/recording/" if row["identifier"].startswith("https://") else "urn:apple:track:"
    candidates = []
    for item in records:
        if not item.get("active", True):
            continue
        uri = item.get("uri") or item.get("identifier", "")
        if uri == row["identifier"]:
            return item
        # Different IDs in the same catalog identify different recordings or editions.
        if uri.startswith(prefix):
            continue
        duration = item.get("duration_ms") or float(item.get("length") or 0) * 1000
        if (item["title"].casefold() == row["title"].casefold() and
                item["artist"].casefold() == row["artist"].casefold() and
                item.get("album", "").casefold() == row["album"].casefold() and
                (not row.get("duration_ms") or not duration or
                 abs(row["duration_ms"] - duration) <= 2000)):
            candidates.append(item)
    return candidates[0] if len(candidates) == 1 else None


def save(folder, projection, row):
    """An existing unrelated playlist at the reserved filename is never replaced."""
    if projection:
        projection.reconcile()
    path = folder / FILENAME
    if path.is_symlink():
        raise ValueError("The catalog imports playlist cannot be a symbolic link.")
    if path.exists():
        def append(current):
            if not is_import_list(current):
                raise ValueError("Rename the existing listening_catalog_imports playlist before importing.")
            if existing(read_list(current), row):
                return None
            return playlist_edits.appended_document(current, row)
        if projection:
            return projection.edit_document(FILENAME, append)
        content = append(path)
        if content is None:
            return False
        place_lists(folder, [(FILENAME, content)], "replace")
    else:
        incoming = [(FILENAME, write_xspf("Catalog imports", [row], LIST_ID))]
        if projection:
            _, conflicts = projection.place(incoming, None)
        else:
            folder.mkdir(parents=True, exist_ok=True)
            _, conflicts = place_lists(folder, incoming, None)
        if conflicts:
            raise ValueError("The import playlist changed. Try the import again.")
    return True
