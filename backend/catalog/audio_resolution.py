"""The one home for turning a stored audio reference into a file.

A verified catalog track or a downloaded custom request becomes a
playable file path here and nowhere else: ref-to-record resolution,
the stored-path form, and the safety checks (containment inside the
audio directory, the shared suffix allowlist) live in this module.
Range serving stays in the server; it consumes the resolved path.

Stored paths are library-relative (relative to the audio directory),
so a workspace move cannot strand them. Rows written before that
change hold absolute paths: reads normalize them (A2 C-05). An
absolute path inside the current audio directory is used as-is; one
from a previous location maps by its relative tail when the same tail
exists in the current audio directory, so an existing database heals
on read without a migration script.

Two read paths, two jobs. resolve_stored() is the serving gate: it
enforces containment and the allowlist, and an absolute path outside
the current audio directory only ever resolves through its tail
inside the library, never as itself. locate_stored() is the runner's
path-form normalization for its own state rows: the runner's gate has
always been judge() on the probed media, never the path, so locating
preserves that and only fixes the form (relative join, tail mapping
for a moved workspace).

A stored value may instead be a blob reference (`blob:<hash>`,
plan 060): the track's bytes live in the media database, and
resolve_playable() is the one place that knows it, staging the
blob into the playback buffer and serving the staged file. File
values never take that path.
"""

import re
from pathlib import Path

from catalog.audio_store import blob_key, is_blob_ref
from system.formats import AUDIO_FORMATS


def stored_path(path, library):
    """The stored form of a library file: relative to the audio directory."""
    return Path(path).resolve().relative_to(Path(library).resolve()).as_posix()


def _candidates(stored, library):
    """Absolute candidate paths for a stored value, best first.

    A relative value names one place: under the audio directory. An
    absolute value inside the current audio directory names itself.
    An absolute value from a previous location yields its relative
    tails, longest first, so the most specific match wins.
    """
    value = Path(stored)
    if not value.is_absolute():
        yield library / value
        # Early request rows were relative to the room, including its audio folder.
        if len(value.parts) > 1 and value.parts[0] == library.name and ".." not in value.parts:
            yield library.joinpath(*value.parts[1:])
        return
    value = value.resolve()
    if value.is_relative_to(library):
        yield value
        return
    parts = value.parts[1:]
    for start in range(len(parts)):
        yield library.joinpath(*parts[start:])


def resolve_stored(stored, library):
    """Resolve a stored audio value to an existing file in the library.

    Returns the resolved path, or None: missing files, paths escaping
    the audio directory, and formats outside the allowlist never
    resolve. Fails closed: the first candidate that exists but fails
    the checks ends the search.
    """
    if not stored:
        return None
    library = Path(library).resolve()
    for candidate in _candidates(stored, library):
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved.is_relative_to(library) and resolved.suffix.lower() in AUDIO_FORMATS:
            return resolved
        return None
    return None


def locate_stored(stored, library):
    """The filesystem path a stored value points at, for the runner.

    Path-form normalization only: relative values join onto the audio
    directory; an absolute value that no longer exists maps by
    relative tail into the current audio directory when the same tail
    is there (a moved workspace). Anything else is the path itself,
    exactly as the runner treated it before. Returns None for an
    empty stored value.
    """
    if not stored:
        return None
    if is_blob_ref(stored):
        # A blob reference names no file; it passes through
        # unchanged (plan 060), and resolve_playable() is where it
        # is turned into one.
        return Path(stored)
    library = Path(library).resolve()
    value = Path(stored)
    if not value.is_absolute():
        return next((p for p in _candidates(stored, library) if p.is_file()), library / value)
    value = value.resolve()
    if value.is_relative_to(library) or value.is_file():
        return value
    parts = value.parts[1:]
    for start in range(len(parts)):
        candidate = library.joinpath(*parts[start:])
        if candidate.is_file():
            return candidate.resolve()
    return value


def resolve_playable(stored, library, media_store=None, buffer=None):
    """The playable file for a stored audio value, or None.

    A blob reference resolves through the playback buffer: the
    staged plain file when the store holds the key, None when it
    does not (or when no store was passed). Anything else is a
    library file and resolves through resolve_stored(), exactly
    as before plan 060. (The media store is `media_store` here
    and in the track_file/custom_file/source_file siblings:
    plain `store` in this module names the requests store.)
    """
    if is_blob_ref(stored):
        if media_store is None or buffer is None:
            return None
        return buffer.stage(media_store, blob_key(stored))
    return resolve_stored(stored, library)


def custom_track(row):
    """Project a downloaded request into the same playback shape as catalog audio."""
    request_id = row["id"]
    return {"id": f"custom-{request_id}", "request_id": request_id, "title": row["title"], "artist": row["artist"],
            "album": row.get("album", "") if row.get("musicbrainz_id") else "",
            "duration": row["length"] or 0, "cover": "", "status": "verified",
            "audio": f"/api/audio/custom/{request_id}",
            **{key: row[key] for key in ("musicbrainz_id", "version") if row.get("musicbrainz_id")}}


def resolve_ref(ref, catalog, store):
    """A playable track dict for a session ref, or None.

    Only verified catalog tracks and downloaded custom requests
    resolve; anything else is not playable.
    """
    if catalog.valid_index(ref):
        item = catalog.track(ref)
        return item if item["status"] == "verified" else None
    if isinstance(ref, str) and ref.startswith("custom-"):
        try:
            request_id = int(ref[7:])
        except ValueError:
            return None
        row = store.downloaded_custom(request_id)
        if row:
            return custom_track(row)
    return None


def track_file(index, catalog, library, media_store=None, buffer=None):
    """The playable file for a catalog index, or None."""
    row = catalog.coverage.get(index, {})
    if row.get("status") != "verified":
        return None
    return resolve_playable(row.get("path", ""), library,
                            media_store, buffer)


def custom_file(request_id, store, library, media_store=None, buffer=None):
    """The playable file for a downloaded custom request, or None."""
    row = store.custom_audio(request_id)
    if not row or row["status"] != "downloaded":
        return None
    return resolve_playable(row.get("audio_path", ""), library,
                            media_store, buffer)


def source_file(ref, catalog, store, library, media_store=None, buffer=None):
    """The playable file for a ref, raising ValueError when not ready."""
    if catalog.valid_index(ref):
        row = catalog.coverage.get(ref, {})
        if row.get("status") != "verified":
            raise ValueError("This song is not ready")
        stored = row.get("path", "")
    elif isinstance(ref, str) and re.fullmatch(r"custom-\d+", ref):
        row = store.custom_audio(int(ref.removeprefix("custom-")))
        if not row or row["status"] != "downloaded":
            raise ValueError("This request is not ready")
        stored = row.get("audio_path", "")
    else:
        raise ValueError("Unknown song")
    path = resolve_playable(stored, library, media_store, buffer)
    if path is None:
        raise ValueError("Audio outside the music folder")
    return path
