"""Durable genre categorization: once, forever (plan 029).

A catalog record's genre is computed once, from the best source
available, and stored against the recording's own key (its identifier
when it has one, else the normalized artist/title/album key the
runner's prepare already computes). The store lives in the state
database (plan 036: the genres table of the application folder's
``requests.sqlite3``, plan 043),
NOT inside the catalog build that prepare rebuilds, so a rebuild can
never erase a categorization; the runner merges the store into the
catalog on prepare, and the catalog reads the store directly.

Source priority: the audio file's own genre tag, then MusicBrainz,
then ListenBrainz. A record already in the store is never looked up
again, which makes the bulk pass resumable by construction and every
later pass cheap. The owner's manual correction (source "manual")
outranks every automated source permanently, including over later
file-tag changes, because categorize() never touches a stored record.

Each entry keeps the raw genre string and a canonical form: the raw
string case-folded, punctuation-normalized, and mapped through the
curated alias table in genre_aliases.json (data beside this module,
reviewable and extensible, never scattered conditionals). Unmapped
genres canonicalize to their own normalized form.
"""

import json
import random
import re
import time
from pathlib import Path

from system import state_database

ALIASES_PATH = Path(__file__).resolve().parent / "genre_aliases.json"

SOURCE_LABELS = {"file": "from the file", "musicbrainz": "from MusicBrainz",
                 "listenbrainz": "from ListenBrainz", "manual": "set by you"}

_SEPARATORS = re.compile(r"[;/|]")


def normalize_genre_text(raw):
    """The comparable form of a genre string.

    Case-folded, "&" read as "and", and every run of non-alphanumeric
    characters collapsed to one space: "Hip-Hop", "hip hop", and
    "Hip  Hop" are the same text here. A tag carrying several genres
    ("Rock; Pop", "Techno / House") contributes its first one.
    """
    text = str(raw or "").casefold().replace("&", " and ")
    text = _SEPARATORS.split(text)[0]
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _load_aliases():
    try:
        table = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    aliases = {}
    for canonical, variants in table.items():
        aliases[normalize_genre_text(canonical)] = canonical
        for variant in variants:
            aliases[normalize_genre_text(variant)] = canonical
    return aliases


_ALIASES = _load_aliases()


def canonical_genre(raw):
    """The canonical bucket for a raw genre string ("" when empty)."""
    text = normalize_genre_text(raw)
    if not text:
        return ""
    return _ALIASES.get(text, text)


def source_label(source):
    return SOURCE_LABELS.get(source, "")


GENRE_SCHEMA = """
CREATE TABLE IF NOT EXISTS genres (
    key TEXT PRIMARY KEY,
    genre TEXT NOT NULL,
    source TEXT NOT NULL,
    stamp REAL NOT NULL
);
"""


class GenreStore:
    """The durable genre store: recording key -> genre entry.

    An entry is {"genre": raw, "canonical": canonical, "source":
    source}. Plan 036: the backing is the ``genres`` table of the one
    state database; ``canonical`` is derived from the stored raw genre
    on read, so the table holds exactly what each source said. Writes
    land immediately, one row per set. The legacy ``genres.json`` is
    imported once, on first use, when the table is empty, and is left
    in place as a backup; a missing or corrupt legacy file imports as
    an empty store (categorization is always recomputable; a torn file
    must never brick the pipeline). The legacy file is read from
    ``state-backup/`` beside the database by default (plan 043).
    """

    def __init__(self, database, legacy_path=None):
        self.database = Path(database)
        self.legacy_path = (Path(legacy_path) if legacy_path
                            else self.database.parent / "state-backup"
                            / "genres.json")
        self._entries = None
        self._import_checked = False

    def _connect(self):
        return state_database.connect(self.database, GENRE_SCHEMA)

    def _ensure_imported(self, connection):
        if self._import_checked:
            return
        self._import_checked = True
        if not self.legacy_path.is_file():
            return
        if connection.execute(
                "SELECT COUNT(*) FROM genres").fetchone()[0]:
            return
        try:
            payload = json.loads(self.legacy_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        entries = []
        for key, entry in payload.items():
            if not isinstance(entry, dict) or "genre" not in entry:
                continue
            entries.append((str(key), str(entry["genre"]),
                            str(entry.get("source") or ""), time.time()))
        connection.execute("BEGIN IMMEDIATE")
        try:
            if connection.execute(
                    "SELECT COUNT(*) FROM genres").fetchone()[0]:
                connection.execute("ROLLBACK")
                return
            connection.executemany(
                "INSERT INTO genres (key, genre, source, stamp) "
                "VALUES (?, ?, ?, ?)", entries)
            imported = connection.execute(
                "SELECT COUNT(*) FROM genres").fetchone()[0]
            if imported != len(entries):
                raise RuntimeError(
                    f"genre import mismatch: file holds {len(entries)} "
                    f"entries, {imported} imported; files left "
                    f"authoritative")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        print(f"Imported genres ({len(entries)} entries) from "
              f"{self.legacy_path} into {self.database}", flush=True)

    def load(self):
        if self._entries is None:
            connection = self._connect()
            try:
                self._ensure_imported(connection)
                self._entries = {
                    key: {"genre": genre,
                          "canonical": canonical_genre(genre),
                          "source": source}
                    for key, genre, source in connection.execute(
                        "SELECT key, genre, source FROM genres")}
            finally:
                connection.close()
        return self._entries

    def reload(self):
        self._entries = None
        return self.load()

    def stamp(self):
        """(count, newest write): lets the Catalog spot outside writes."""
        connection = self._connect()
        try:
            self._ensure_imported(connection)
            return state_database.table_stamp(connection, "genres")
        finally:
            connection.close()

    def get(self, key):
        return self.load().get(key)

    def set(self, key, raw, source, *, only_missing=False):
        raw = str(raw or "").strip()
        canonical = canonical_genre(raw)
        if not raw or not canonical:
            return None
        entry = {"genre": raw, "canonical": canonical, "source": source}
        connection = self._connect()
        try:
            self._ensure_imported(connection)
            insert = "INSERT OR IGNORE" if only_missing else "INSERT OR REPLACE"
            written = connection.execute(
                f"{insert} INTO genres (key, genre, source, stamp) VALUES (?, ?, ?, ?)",
                (key, raw, source, time.time())).rowcount
            connection.commit()
            if not written:
                return None
        finally:
            connection.close()
        self.load()[key] = entry
        return entry

    def save(self):
        """Writes are immediate now; kept so existing callers keep working."""


def missing_genre_records(records, entries):
    """Active catalog records without an own or linked genre."""
    tagged_groups = {r["linked_recording"] for r in records if r.get("linked_recording") and r["key"] in entries}
    return [r for r in records if r.get("active", True)
            and r["key"] not in entries and r.get("linked_recording") not in tagged_groups]


def _lookup_genre(record, media_for, metadata):
    unavailable = False
    try:
        media = media_for(record) if media_for else None
        candidate = (media or {}).get("genre")
        if canonical_genre(candidate):
            return candidate, "file", False
    except (OSError, ValueError):
        unavailable = True
    if metadata is not None:
        for lookup, source in ((metadata.musicbrainz_genre, "musicbrainz"),
                               (metadata.listenbrainz_genre, "listenbrainz")):
            try:
                candidate = lookup(record)
                if canonical_genre(candidate):
                    return candidate, source, False
            except Exception:
                unavailable = True
    return "", "", unavailable


def categorize(records, store, media_for=None, metadata=None, on_progress=None):
    """Check each candidate once and preserve concurrent and linked genre assignments."""
    missing = missing_genre_records(records, store.load())
    filled = unavailable = 0
    new_groups = set()
    if on_progress:
        on_progress(0, len(missing), 0, 0)
    for position, record in enumerate(missing, 1):
        group = record.get("linked_recording")
        if group not in new_groups:
            raw, source, failed_lookup = _lookup_genre(record, media_for, metadata)
            if raw:
                if store.set(record["key"], raw, source, only_missing=True):
                    filled += 1
                # An ignored insert means a concurrent correction already supplied this genre.
                if group:
                    new_groups.add(group)
            elif failed_lookup:
                unavailable += 1
        if on_progress:
            on_progress(position, len(missing), filled, unavailable)
    return filled


def group_genre_flow(refs, current_genre, genre_of, shuffle=None):
    """Order upcoming refs for Genre flow (plan 029).

    Refs sharing the current track's canonical genre come first,
    shuffled among themselves; the rest follow, also shuffled, so
    playback falls back to the full ready pool when the genre pool
    runs dry instead of stopping or repeating. With no current genre
    the whole list is one shuffled pool, exactly like plain shuffle.
    This is the server's playable-order assembly: the playback session
    only consumes the order it is handed.
    """
    shuffle = shuffle or random.shuffle
    refs = list(refs)
    if not current_genre:
        shuffle(refs)
        return refs
    same = [ref for ref in refs if genre_of(ref) == current_genre]
    rest = [ref for ref in refs if genre_of(ref) != current_genre]
    shuffle(same)
    shuffle(rest)
    return same + rest
