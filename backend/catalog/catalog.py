"""The in-memory catalog: records, playlists, covers, and coverage.

State loads from the one state database through the catalog store
(plan 036), never from module globals, so the refresh pipeline and
tests drive reload() directly. The cover map and list names are built
by the runner's prepare step and persisted in the catalog build;
reload reads them from there instead of re-parsing every list file on
the request path (A3 PERF-07). Builds written before they were
persisted fall back to deriving them from the list files, exactly as
the old reload did. The summary is derived from the build, the
coverage table, and the last report's facts on every read; it is
never stored (plan 036).
"""

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

from catalog.catalog_store import CatalogStore
from catalog.favorites import FavoritesStore
from catalog.genres import GenreStore
from library.library_protocol import playlist_title, read_list, xspf_files
from system.normalize import normalize_title


_SPOTIFY_TRACK_URI = re.compile(r"spotify:track:([A-Za-z0-9]{22})")


def bulk_attempted_indices(records, cursor):
    """Indices included in the bulk search count, independent of individual requests."""
    return {row["index"] for row in records if row["index"] <= cursor
            and not row.get("manual_copy")}


def spotify_share_url(uri):
    """The shareable Spotify link for a track identifier, or "".

    Derived locally from the identifier the track itself carries; no
    network call, no adapter. Only a complete `spotify:track:<id>`
    identifier yields a link: MusicBrainz UUIDs, ISRC-only rows, other
    Spotify entity URIs, and malformed values all yield "".
    """
    match = _SPOTIFY_TRACK_URI.fullmatch(uri or "")
    return "https://open.spotify.com/track/" + match.group(1) if match else ""


class CatalogPaths(NamedTuple):
    database: Path
    lists: Path


class Catalog:
    def __init__(self, paths):
        self.paths = CatalogPaths(*(Path(path) for path in paths))
        self.build_stamp = None
        self.records = []
        self.playlists = {}
        self.playlist_revisions = {}
        self.issues = []
        self.covers = {}
        self.list_names = {}
        self.coverage = {}
        self.coverage_stamp = None
        self._report_facts = None
        self.facts_stamp = None
        self.memberships = {}
        self.genres = {}
        self.genres_stamp = None
        self.favorites = set()
        self.favorites_stamp = None
        self._search_texts = {}
        self.reload()

    def catalog_store(self):
        return CatalogStore(self.paths.database)

    def genre_store(self):
        return GenreStore(self.paths.database)

    def favorite_store(self):
        return FavoritesStore(self.paths.database)

    def reload(self, paths=None):
        """Reload build-derived state, optionally from new paths.

        This is the only reload entry point; the refresh pipeline and
        tests call it, nothing re-runs __init__ to reload.
        """
        if paths is not None:
            self.paths = CatalogPaths(*(Path(path) for path in paths))
        store = self.catalog_store()
        manifest = store.read_build()
        if manifest is None:
            manifest = {"records": [], "playlists": {}, "issues": [],
                        "covers": None, "list_names": None}
        build_stamp = store.build_stamp()
        self.records = manifest["records"]
        linked_keys = defaultdict(list)
        for record in self.records:
            if record.get("linked_recording"):
                linked_keys[record["linked_recording"]].append(record["key"])
        self._linked_keys = {group: tuple(keys) for group, keys in linked_keys.items()}
        self.playlists = manifest["playlists"]
        self.playlist_revisions = {
            key: hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()
            for key, entries in self.playlists.items()
        }
        self.issues = manifest.get("issues", [])
        covers = manifest.get("covers")
        list_names = manifest.get("list_names")
        if covers is None or list_names is None:
            derived_covers, derived_names = self._derive_list_data()
            if covers is None:
                covers = derived_covers
            if list_names is None:
                list_names = derived_names
        self.covers = covers
        self.list_names = list_names
        # Adopt the coverage rows the build was made with (plan 038):
        # reload() runs right after a rebuild, when the report half
        # has just written those rows, and leaving the map empty until
        # the next watchdog refresh made every track read as pending
        # and every audio request 404 for up to a watchdog interval.
        # The replacement is built in locals and assigned once, so a
        # concurrent reader never sees the map mid-swap either.
        coverage = self._adopt_coverage(store.read_coverage())
        if coverage is None:
            self.coverage = {}
            self.coverage_stamp = None
        else:
            self.coverage = coverage
            self.coverage_stamp = store.coverage_stamp()
        self._report_facts = store.report_facts()
        self.facts_stamp = store.facts_stamp()
        self.memberships = {record["index"]: [] for record in self.records}
        for playlist, entries in self.playlists.items():
            for index in entries:
                if index and playlist not in self.memberships[index]:
                    self.memberships[index].append(playlist)
        self._search_texts = {}
        self._load_genres()
        self._load_favorites()
        # A fresh room becomes ready once its build and derived maps are present.
        self.build_stamp = build_stamp

    @property
    def bulk_resume_free(self):
        return (self._report_facts or {}).get("bulk_resume_free", 0)

    @property
    def summary(self):
        """The report rollup, computed on read (plan 036).

        Status counts come from the coverage rows in memory, per-list
        rollups from memberships plus coverage, and the figures only a
        report run can know (attempted horizon, library and free size,
        unmatched and ambiguous file counts) from the facts the last
        report stored. Before the first report those read as zero,
        exactly as an absent summary file did.
        """
        counts = {}
        for row in self.coverage.values():
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        facts = self._report_facts or {}
        attempted = bulk_attempted_indices(self.records, facts.get("attempted_through", 0))
        playlists = {}
        for name, entries in self.playlists.items():
            members = [index for index in entries if index]
            playlists[name] = {
                "entries": len(entries),
                "attempted": sum(index in attempted for index in members),
                "playable": sum(1 for index in members
                                if self.coverage.get(index, {}).get(
                                    "status") == "verified"),
            }
        return {
            "verified": counts.get("verified", 0),
            "unmatched": counts.get("unmatched", 0),
            "pending": counts.get("pending", 0),
            "rejected": counts.get("rejected", 0),
            "metadata_issues": len(self.issues),
            "unmatched_files": facts.get("unmatched_files", 0),
            "ambiguous_files": facts.get("ambiguous_files", 0),
            "attempted": sum(row["index"] in attempted for row in self.records if row.get("active", True)),
            "playlists": playlists,
            "library_gib": facts.get("library_gib", 0),
            "free_gib": facts.get("free_gib", 0),
        }

    def _load_genres(self):
        self._search_texts = {}
        store = self.genre_store()
        self.genres = store.load()
        self.genres_stamp = store.stamp()

    def reload_genres(self):
        self._load_genres()

    def _load_favorites(self):
        store = self.favorite_store()
        self.favorites = store.keys()
        self.favorites_stamp = store.stamp()

    def reload_favorites(self):
        self._load_favorites()

    def recording_keys(self, record):
        return self._linked_keys.get(record.get("linked_recording"), (record["key"],))

    def is_favorite(self, record):
        return any(key in self.favorites for key in self.recording_keys(record))

    def linked_history(self, history):
        """Project durable events onto each source key without duplicating event rows."""
        result = history.copy()
        for keys in self._linked_keys.values():
            played = [history[key] for key in keys if key in history]
            if played:
                last = max((ts for ts, _ in played if ts is not None), default=None)
                result.update(dict.fromkeys(keys, (last, sum(count for _, count in played))))
        return result

    def genre_entry(self, record):
        own = self.genres.get(record["key"])
        if "linked_recording" not in record or (own and own["source"] == "manual"):
            return own
        entries = [self.genres[key] for key in self.recording_keys(record) if key in self.genres]
        return next((entry for entry in entries if entry["source"] == "manual"),
                    own or next(iter(entries), None))

    def genre_canonical(self, record):
        entry = self.genre_entry(record)
        return entry["canonical"] if entry else ""

    def canonical_genres(self):
        """The canonical genres present among active records, sorted."""
        return sorted({entry["canonical"] for record in self.records
                       if record.get("active", True)
                       for entry in (self.genre_entry(record),) if entry})

    def _derive_list_data(self):
        """Legacy fallback: build covers and list names from the list files."""
        covers = {}
        list_names = {}
        for source in xspf_files(self.paths.lists):
            try:
                tracks = read_list(source)
            except (ValueError, OSError):
                continue
            title = playlist_title(source)
            if title:
                list_names[source.stem] = title
            for row in tracks:
                if row.get("identifier") and row.get("image"):
                    covers.setdefault(row["identifier"], row["image"])
        return covers, list_names

    def _adopt_coverage(self, rows):
        """The coverage map for stored rows, or None to refuse them.

        Rows are keyed by record index, and a row counts only when
        its index names a record in the current build. A build can
        legitimately hold a record the last report never covered
        (the owner's library: 9,789 records, 9,788 coverage rows),
        so adoption must not demand row-count equality; that demand
        is what left an imported library reading zero verified after
        plan 036. The guard that matters is that the rows belong to
        this build at all: a non-empty set with no in-range row is
        another build's coverage and is refused, leaving the stamp
        unset so a later refresh retries.
        """
        valid = {record["index"] for record in self.records}
        adopted = {row["index"]: row for row in rows
                   if row["index"] in valid}
        if rows and not adopted:
            return None
        return adopted

    def refresh(self):
        """Pick up outside writes, reloading only what moved.

        Change detection is the stores' write counters (plan 036):
        a new build reloads everything, a genre write reloads the
        genre map, a favorite write reloads the favorite set, new
        report facts refresh the summary inputs, and
        a coverage write is adopted when its rows name records in
        the current build (see _adopt_coverage).
        """
        store = self.catalog_store()
        if store.build_stamp() != self.build_stamp:
            self.reload()
        genre_store = self.genre_store()
        if genre_store.stamp() != self.genres_stamp:
            self._load_genres()
        favorite_store = self.favorite_store()
        if favorite_store.stamp() != self.favorites_stamp:
            self._load_favorites()
        if store.facts_stamp() != self.facts_stamp:
            self._report_facts = store.report_facts()
            self.facts_stamp = store.facts_stamp()
        coverage_stamp = store.coverage_stamp()
        if coverage_stamp != self.coverage_stamp:
            coverage = self._adopt_coverage(store.read_coverage())
            if coverage is not None:
                self.coverage = coverage
                self.coverage_stamp = coverage_stamp

    def status_of(self, index, request_by_track=None):
        """The status half of track() and nothing else (plan 018
        PERF-01): the coverage status, overridden by a queued,
        searching, or space-waiting request unless already verified.
        The listing's cheap phase filters on this; track() builds on
        it, so the rule lives in exactly one place."""
        status = self.coverage.get(index, {}).get("status", "pending")
        request = (request_by_track or {}).get(index)
        if request and request["status"] in {"waiting_format", "queued", "searching", "waiting_space"} and status != "verified":
            status = request["status"]
        return status

    def valid_index(self, index) -> bool:
        """The strict body-input dialect: a non-bool int in range."""
        return (isinstance(index, int) and not isinstance(index, bool)
                and 1 <= index <= len(self.records))

    def record_at(self, index):
        """The record for a 1-based catalog index, or None.

        The one place the 1-based-URL to 0-based-list mapping
        lives. Range only: whether an inactive record counts is
        the caller's dialect, expressed by choosing
        active_record_at instead."""
        if isinstance(index, int) and not isinstance(index, bool) \
                and 1 <= index <= len(self.records):
            return self.records[index - 1]
        return None

    def active_record_at(self, index):
        """record_at, for the dialects that refuse inactive records."""
        record = self.record_at(index)
        if record is not None and record.get("active", True):
            return record
        return None

    def search_text(self, index):
        """The normalized text the track listing matches a query
        against (plan 018 PERF-01): artist, title, album, and the
        record's genre and canonical genre, joined and normalized
        exactly as the handler used to compute it per request.
        Computed once per record and cached on the Catalog; the cache
        clears in reload() and in _load_genres(), the two events that
        can change its inputs."""
        text = self._search_texts.get(index)
        if text is None:
            record = self.record_at(index)
            if record is None:
                raise IndexError(f"catalog index out of range: {index!r}")
            entry = self.genre_entry(record) or {}
            text = normalize_title(" ".join((record["artist"], record["title"], record["album"],
                                             entry.get("genre", ""), entry.get("canonical", ""))))
            self._search_texts[index] = text
        return text

    def track(self, index, request_by_track=None):
        record = self.record_at(index)
        if record is None:
            raise IndexError(f"catalog index out of range: {index!r}")
        status = self.status_of(index, request_by_track)
        genre = self.genre_entry(record) or {}
        return {
            "id": index,
            "artist": record["artist"],
            "title": record["title"],
            "album": record["album"],
            "genre": genre.get("genre", ""),
            "genre_canonical": genre.get("canonical", ""),
            "genre_source": genre.get("source", ""),
            "favorite": self.is_favorite(record),
            "duration": record["length"] or 0,
            "cover": self.covers.get(record["uri"], ""),
            "status": status,
            "playlists": self.memberships[index],
            "audio": f"/api/audio/{index}" if status == "verified" else None,
            "spotify_url": spotify_share_url(record.get("uri", "")),
            "reason": self.coverage.get(index, {}).get("reason", ""),
        }
