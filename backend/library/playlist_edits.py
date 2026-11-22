"""Playlist metadata edits, independent of audio acquisition and playback."""

import unicodedata
import xml.etree.ElementTree as ET

from library.library_protocol import NS, retain_import_origin, track_key, write_xspf, xspf_playlist


def metadata(body):
    row = {}
    for field in ("artist", "title", "album"):
        value = body.get(field, "")
        if not isinstance(value, str) or len(value) > 512 or (value and not value.isprintable()):
            raise ValueError(f"Enter a valid {field} of up to 512 characters.")
        row[field] = value.strip()
    if not row["artist"] or not row["title"]:
        raise ValueError("Enter the artist and song title.")
    return row


def title_artist(row):
    return tuple(unicodedata.normalize("NFC", row[k].strip()).casefold() for k in ("title", "artist"))


def matching_metadata(rows, song):
    pair = title_artist(song)
    return [row for row in rows if title_artist(row) == pair and
            (not song.get("album") or row.get("album", "").casefold() == song["album"].casefold())]


def catalog_row(record):
    return {"artist": record["artist"], "title": record["title"], "album": record.get("album", ""),
            "identifier": record.get("uri", ""), "isrc": record.get("isrc", ""),
            "duration_ms": round(float(record["length"]) * 1000) if record.get("length") else None}


def appended_document(path, song):
    """Append to the current edition, retaining existing order, extensions and playlist ID."""
    original = path.read_bytes()
    content = original.decode("utf-8-sig")
    _, rows = xspf_playlist(content)
    if any(track_key(row) == track_key(song) for row in rows):
        return None
    if not song.get("identifier") and matching_metadata(rows, song):
        return None
    document = ET.fromstring(content, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True)))
    listing = document.find(f"{{{NS}}}trackList")
    if listing is None:
        raise ValueError("This playlist has no track list.")
    addition = ET.fromstring(write_xspf("", [song])).find(f".//{{{NS}}}track")
    listing.append(addition)
    edited = ET.tostring(document, encoding="unicode", xml_declaration=True) + "\n"
    return retain_import_origin(path, original, edited)
