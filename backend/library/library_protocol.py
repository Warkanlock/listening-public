"""XSPF is the portable list format; CSV and M3U are import adapters."""

import csv
import hashlib
import io
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from library.list_intake import preserve_legacy_copy
from system.private_files import private_write


NS = "http://xspf.org/ns/0/"
ISRC_REL = "https://isrc.ifpi.org/"
SUPPORTED = {".csv", ".m3u", ".m3u8", ".xspf"}
ET.register_namespace("", NS)


def xspf_files(directory):
    directory = Path(directory)
    return sorted((path for path in directory.iterdir() if path.is_file() and path.suffix.casefold() == ".xspf"),
                  key=lambda path: path.name.casefold()) if directory.is_dir() else []


def clean(value):
    return str(value or "").replace("\\,", ",").strip()


def field(row, *names):
    normalized = {re.sub(r"[^a-z0-9]", "", key.casefold()): value for key, value in row.items() if key}
    for name in names:
        key = re.sub(r"[^a-z0-9]", "", name.casefold())
        if key in normalized:
            return clean(normalized[key])
    return ""


def csv_tracks(content):
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    if not reader.fieldnames:
        raise ValueError("CSV needs a header row")
    rows = []
    for row in reader:
        title = field(row, "Track Name", "Title", "Song", "Song Title", "Name")
        artist = field(row, "Artist Name(s)", "Artist", "Artists", "Performer", "Creator")
        if not artist:
            possible_artist, separator, possible_title = title.partition(" - ")
            if separator and possible_artist.strip() and possible_title.strip():
                artist, title = possible_artist.strip(), possible_title.strip()
        duration = field(row, "Track Duration (ms)", "Duration (ms)", "Duration ms")
        seconds = field(row, "Duration", "Length", "Seconds")
        if not duration and seconds:
            try:
                duration = str(round(float(seconds) * 1000))
            except ValueError:
                pass
        rows.append({"title": title, "artist": artist, "album": field(row, "Album Name", "Album"),
                     "duration_ms": int(duration) if duration.isdigit() else None,
                     "identifier": field(row, "Track URI", "URI", "Identifier", "MusicBrainz ID"),
                     "isrc": field(row, "ISRC"), "image": field(row, "Album Image URL", "Image", "Artwork"),
                     "location": field(row, "Location", "File", "Path")})
    if not rows and field({name: name for name in reader.fieldnames}, "Track Name", "Title", "Song", "Song Title", "Name") and field(
            {name: name for name in reader.fieldnames}, "Artist Name(s)", "Artist", "Artists", "Performer", "Creator"):
        return []
    if not any(row["title"] and row["artist"] for row in rows):
        raise ValueError("CSV needs title and artist columns with at least one song")
    return rows


def m3u_tracks(content):
    rows = []
    description = ""
    duration = None
    for raw in content.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            header, _, description = line[8:].partition(",")
            try:
                duration = round(float(header) * 1000) if float(header) >= 0 else None
            except ValueError:
                duration = None
        elif line and not line.startswith("#"):
            label = description or Path(line.replace("\\", "/")).stem.replace("_", " ")
            artist, separator, title = label.partition(" - ")
            if separator and artist.strip() and title.strip():
                rows.append({"artist": artist.strip(), "title": title.strip(), "album": "",
                             "duration_ms": duration, "identifier": "", "isrc": "", "image": "",
                             "location": line})
            else:
                rows.append({"artist": "", "title": label, "album": "", "duration_ms": duration,
                             "identifier": "", "isrc": "", "image": "", "location": line})
            description, duration = "", None
    if not any(row["title"] and row["artist"] for row in rows):
        raise ValueError("M3U needs Artist - Title metadata in EXTINF or file names")
    return rows


def xspf_playlist(content):
    if "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper():
        raise ValueError("XSPF declarations are not supported")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as error:
        raise ValueError("XSPF is not valid XML") from error
    if root.tag != f"{{{NS}}}playlist" or root.get("version") != "1":
        raise ValueError("Choose an XSPF version 1 playlist")
    tracks = []
    for track in root.findall(f"{{{NS}}}trackList/{{{NS}}}track"):
        value = lambda tag: clean(track.findtext(f"{{{NS}}}{tag}"))
        title, artist = value("title"), value("creator")
        identifiers = [clean(item.text) for item in track.findall(f"{{{NS}}}identifier")]
        isrc = next((clean(item.text) for item in track.findall(f"{{{NS}}}meta")
                     if item.get("rel") == ISRC_REL), "")
        duration = value("duration")
        tracks.append({"title": title, "artist": artist, "album": value("album"),
                       "duration_ms": int(duration) if duration.isdigit() else None,
                       "identifier": identifiers[0] if identifiers else "", "isrc": isrc,
                       "image": value("image"), "location": value("location")})
    if tracks and not any(track["title"] and track["artist"] for track in tracks):
        raise ValueError("XSPF needs at least one track with title and creator")
    return clean(root.findtext(f"{{{NS}}}title")), tracks


def xspf_tracks(content):
    return xspf_playlist(content)[1]


def parse_list(name, content):
    suffix = Path(name).suffix.casefold()
    if suffix == ".csv":
        return csv_tracks(content)
    if suffix in {".m3u", ".m3u8"}:
        return m3u_tracks(content)
    if suffix == ".xspf":
        return xspf_tracks(content)
    raise ValueError("Use CSV, M3U, M3U8, or XSPF")


def read_list(path):
    return parse_list(Path(path).name, Path(path).read_text(encoding="utf-8-sig"))


def track_key(row):
    """A list row's identity, exactly the rule the catalog build
    (runner.prepare) uses: the identifier when the row carries one,
    else the casefolded artist, title, and album tuple joined with
    NUL. The playlist merge unions on this key, so a merged list's
    membership in the next build is exactly the union of its
    sources' memberships and no merge can invent a catalog record
    the build would not have made anyway."""
    identifier = row.get("identifier") or ""
    if identifier:
        return identifier
    return "\0".join(((row.get("artist") or "").casefold(),
                      (row.get("title") or "").casefold(),
                      (row.get("album") or "").casefold()))


def playlist_identifier(path):
    """An explicit playlist identity, never a track identifier or filename."""
    path = Path(path)
    if path.suffix.lower() == ".xspf":
        root = ET.parse(path).getroot()
        values = [root.findtext(f"{{{NS}}}identifier") or "", root.findtext(f"{{{NS}}}location") or ""]
    elif path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = [h for h in reader.fieldnames or () if h.casefold().strip()
                       in {"spotify playlist id", "playlist uri", "playlist url", "spotify playlist uri", "spotify playlist url"}]
            if not headers:
                return ""
            values = {(("spotify:playlist:" + row[h].strip()) if h.casefold().strip() == "spotify playlist id"
                       and re.fullmatch(r"[A-Za-z0-9]{22}", (row.get(h) or "").strip()) else row.get(h, ""))
                      for row in reader for h in headers}
    else:
        return ""
    ids = set()
    for value in values:
        match = re.fullmatch(r"(?:spotify:playlist:|https://open\.spotify\.com/playlist/)([A-Za-z0-9]{22})(?:\?[^\s]+)?",
                             (value or "").strip())
        if match:
            ids.add(match.group(1))
    return "spotify:playlist:" + ids.pop() if len(ids) == 1 else ""


def playlist_title(path):
    path = Path(path)
    if path.suffix.casefold() != ".xspf":
        return ""
    try:
        root = ET.parse(path).getroot()
        title = clean(root.findtext(f"{{{NS}}}title"))
    except (OSError, ET.ParseError):
        title = ""
    return title if title and title != path.stem else ""


def write_xspf(name, tracks, identifier=""):
    root = ET.Element(f"{{{NS}}}playlist", {"version": "1"})
    ET.SubElement(root, f"{{{NS}}}title").text = name
    if identifier:
        ET.SubElement(root, f"{{{NS}}}identifier").text = identifier
    listing = ET.SubElement(root, f"{{{NS}}}trackList")
    for track in tracks:
        item = ET.SubElement(listing, f"{{{NS}}}track")
        for tag, value in (("title", track.get("title")), ("creator", track.get("artist")),
                           ("album", track.get("album")), ("duration", track.get("duration_ms")),
                           ("identifier", track.get("identifier")), ("image", track.get("image")),
                           ("location", track.get("location"))):
            if value not in (None, ""):
                ET.SubElement(item, f"{{{NS}}}{tag}").text = str(value)
        if track.get("isrc"):
            ET.SubElement(item, f"{{{NS}}}meta", {"rel": ISRC_REL}).text = track["isrc"]
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def write_csv(tracks):
    """A simple vendor-neutral table for spreadsheet and library importers."""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=["Title", "Artist", "Album", "Duration", "Identifier", "ISRC", "Image"])
    writer.writeheader()
    for track in tracks:
        duration = track.get("duration_ms")
        writer.writerow({"Title": track.get("title", ""), "Artist": track.get("artist", ""),
                         "Album": track.get("album", ""), "Duration": duration / 1000 if duration is not None else "",
                         "Identifier": track.get("identifier", ""), "ISRC": track.get("isrc", ""),
                         "Image": track.get("image", "")})
    return output.getvalue()


def portable_m3u8(path, audio_directory):
    """Rewrite verified local paths for the bundle's playlists/ and audio/ layout."""
    path, audio_directory = Path(path), Path(audio_directory).resolve()
    lines = ["#EXTM3U"]
    description = ""
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            description = line
        elif line and not line.startswith("#"):
            resolved = (path.parent / line).resolve()
            if resolved.is_relative_to(audio_directory):
                if description:
                    lines.append(description)
                lines.append("../audio/" + resolved.relative_to(audio_directory).as_posix())
            description = ""
    return "\n".join(lines) + "\n"


def _stamp(source, target, body):
    # A trailing XML processing instruction survives a portable backup. Bind
    # it to both names and the exact document so edited/renamed copies become
    # independent owner lists. Never act on a filename or timestamp alone.
    digest = hashlib.sha256(source.name.encode() + b"\0" + target.name.encode() + b"\0" + body).hexdigest()
    return b"<?listening-derived " + digest.encode() + b"?>\n"


def _owned_copy(source, target):
    if target.is_symlink() or not target.is_file():
        return False
    return owns_derived_bytes(source, target, target.read_bytes())


def owns_derived_bytes(source, target, data):
    body, separator, tag = data.rpartition(b"<?listening-derived ")
    return bool(separator) and separator + tag == _stamp(source, target, body)


def _edit_stamp(source, target, content):
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    return _stamp(source, target, content + checksum.encode()).replace(b"listening-derived ", b"listening-edited ")


def _edited_copy(source, target, data):
    body, separator, tag = data.rpartition(b"<?listening-edited ")
    return bool(separator) and separator + tag == _edit_stamp(source, target, body)


def retain_import_origin(target, original, edited):
    """Keep a one-time import as one playlist after editing its generated XSPF."""
    for source in target.parent.iterdir():
        if (source.suffix.casefold() in SUPPORTED - {".xspf"} and source.is_file()
                and (target.name == source.stem + ".xspf" or target.name.startswith(source.name + "."))
                and (owns_derived_bytes(source, target, original) or _edited_copy(source, target, original))):
            return edited + _edit_stamp(source, target, edited.encode()).decode()
    return edited


def derived_path(source, directory=None):
    """Stable XSPF destination for one input, respecting existing independent lists."""
    source = Path(source)
    directory = Path(directory) if directory is not None else source.parent
    preferred = directory / (source.stem + ".xspf")
    if _owned_copy(source, preferred):
        return preferred
    # Check previously allocated alternatives before reusing a newly vacant
    # preferred name. Names containing glob metacharacters are ordinary input.
    prefix = source.name + "."
    for target in sorted(directory.iterdir()) if directory.is_dir() else ():
        if target.name.startswith(prefix) and target.suffix.lower() == ".xspf" and _owned_copy(source, target):
            return target
    candidate = preferred
    count = 1
    while candidate.exists() or candidate.is_symlink():
        suffix = "xspf" if count == 1 else f"{count}.xspf"
        candidate = directory / (source.name + "." + suffix)
        count += 1
    return candidate


def write_derived(source, directory, body, *, initial_only=False, legacy_body=None):
    """Publish a generated XSPF without replacing independent or edited lists."""
    while True:
        target = derived_path(source, directory)
        existed = _owned_copy(source, target)
        if existed and initial_only:
            return None
        if not existed and legacy_body is not None:
            preferred = directory / (source.stem + ".xspf")
            if preserve_legacy_copy(preferred, legacy_body):
                target, existed = preferred, True
        contents = body + _stamp(source, target, body)
        if existed and target.read_bytes() == contents:
            return None
        try:
            private_write(target, contents.decode("utf-8"), exclusive=not existed)
        except FileExistsError:
            continue
        return target


def retire_derived(source):
    """Retire only an unchanged canonical copy of this source."""
    target = derived_path(source)
    if _owned_copy(source, target):
        target.unlink()


def canonicalize_source(source):
    # An owner-edited edition supersedes this exact input. An explicit
    # replacement with different source bytes still produces a fresh import.
    for target in source.parent.iterdir():
        if ((target.name == source.stem + ".xspf" or target.name.startswith(source.name + "."))
                and target.suffix.lower() == ".xspf" and target.is_file() and not target.is_symlink()
                and _edited_copy(source, target, target.read_bytes())):
            return None
    tracks = read_list(source)
    identifier = playlist_identifier(source)
    legacy = write_xspf(source.stem, tracks).encode()
    body = write_xspf(source.stem, tracks, identifier).encode() if identifier else legacy
    return write_derived(source, source.parent, body, legacy_body=legacy)


def canonicalize_directory(directory):
    """Create independent canonical lists, preserving every unclaimed input."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    converted = []
    for source in sorted(directory.iterdir()):
        if not source.is_file() or source.suffix.casefold() not in SUPPORTED - {".xspf"}:
            continue
        target = canonicalize_source(source)
        if target is not None:
            converted.append(target)
    return converted


def migrate_legacy_lists(old_directory, new_directory):
    """Make owned XSPF copies of existing lists before switching directories."""
    old_directory, new_directory = Path(old_directory), Path(new_directory)
    if old_directory.resolve() == new_directory.resolve() or not old_directory.is_dir():
        return []
    new_directory.mkdir(parents=True, exist_ok=True)
    migrated = []
    sources = sorted(old_directory.iterdir(), key=lambda p: (p.suffix.lower() != ".xspf", p.name))
    # A legacy generated sibling has the same bytes as the old writer's output.
    # Prefer the CSV/M3U as its source so one logical playlist survives relocation.
    redundant = set()
    for source in sources:
        sibling = source.with_suffix(".xspf")
        if source.suffix.lower() in SUPPORTED - {".xspf"} and source.is_file() and sibling.is_file():
            if sibling.read_bytes() == write_xspf(source.stem, read_list(source)).encode():
                redundant.add(sibling)
    for source in sources:
        if source in redundant:
            continue
        if source.is_file() and source.suffix.casefold() in SUPPORTED:
            tracks = read_list(source)
            legacy = write_xspf(source.stem, tracks).encode()
            if source.suffix.lower() == ".xspf":
                body = source.read_bytes()
            else:
                body = write_xspf(source.stem, tracks, playlist_identifier(source)).encode()
            target = write_derived(source, new_directory, body, initial_only=True, legacy_body=legacy)
            if target is not None:
                migrated.append(target)
    return migrated
