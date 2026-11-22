import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import tempfile
import time
import unittest

from catalog.catalog import Catalog, CatalogPaths
from catalog.catalog_store import CatalogStore
from catalog.genres import GenreStore
from system.normalize import normalize_title


def make_catalog(folder, records, genres=None):
    state = Path(folder) / "state"
    state.mkdir(parents=True, exist_ok=True)
    database = state / "requests.sqlite3"
    manifest = {"records": records, "playlists": {}, "issues": [],
                "covers": {}, "list_names": {}}
    CatalogStore(database).write_build(manifest)
    if genres is not None:
        store = GenreStore(database)
        for key, entry in genres.items():
            store.set(key, entry["genre"], entry["source"])
    paths = CatalogPaths(database=database, lists=Path(folder) / "lists")
    return Catalog(paths), state


def write_manifest(state, records):
    manifest = {"records": records, "playlists": {}, "issues": [],
                "covers": {}, "list_names": {}}
    CatalogStore(state / "requests.sqlite3").write_build(manifest)


RECORD = {"index": 1, "key": "key-1", "artist": "Alpha Band",
          "title": "First Song", "album": "Debut", "length": 201,
          "uri": "", "active": True}


class SearchTextCacheTests(unittest.TestCase):
    """Plan 018 (PERF-01): the listing's search text is computed once
    per record and cached on the Catalog; reload() and the genre
    reload are the invalidations, because they are the events that
    can change its inputs."""

    def test_search_text_cache_invalidates_on_reload_and_genre_change(self):
        with tempfile.TemporaryDirectory() as folder:
            genres = {"key-1": {"genre": "Rock", "canonical": "rock",
                                "source": "file"}}
            catalog, state = make_catalog(folder, [dict(RECORD)], genres)
            expected = normalize_title("Alpha Band First Song Debut Rock rock")
            self.assertEqual(catalog.search_text(1), expected)
            # The cache serves the second read: an in-place record
            # mutation without a reload does not change the text.
            catalog.records[0]["title"] = "Mutated Song"
            self.assertEqual(catalog.search_text(1), expected)
            # A catalog reload replaces the records: the text follows.
            renamed = dict(RECORD, title="Renamed Song")
            write_manifest(state, [renamed])
            catalog.reload()
            expected = normalize_title("Alpha Band Renamed Song Debut Rock rock")
            self.assertEqual(catalog.search_text(1), expected)
            # A genre reload (the /api/genre correction path) replaces
            # the genre entry: the text follows that too.
            GenreStore(state / "requests.sqlite3").set(
                "key-1", "Jazz Fusion", "manual")
            catalog.reload_genres()
            self.assertEqual(
                catalog.search_text(1),
                normalize_title("Alpha Band Renamed Song Debut Jazz Fusion jazz"))


class StatusOfTests(unittest.TestCase):
    """Plan 018 (PERF-01): status_of is the status half of track(),
    extracted so the listing's cheap phase and track() share the
    one rule."""

    def test_status_of_is_the_status_half_of_track(self):
        with tempfile.TemporaryDirectory() as folder:
            records = [dict(RECORD),
                       dict(RECORD, index=2, key="key-2", title="Second Song")]
            catalog, _state = make_catalog(folder, records)
            catalog.coverage = {1: {"status": "verified", "reason": ""},
                                2: {"status": "unmatched", "reason": "no match"}}
            requests = {1: {"status": "queued"}, 2: {"status": "queued"}}
            # A queued request never overrides a verified record.
            self.assertEqual(catalog.status_of(1, requests), "verified")
            # It overrides any other coverage status.
            self.assertEqual(catalog.status_of(2, requests), "queued")
            missed = {1: {"status": "not_found"}, 2: {"status": "not_found"}}
            self.assertEqual(catalog.track(1, missed)["status"], "verified")
            self.assertEqual(catalog.track(2, missed)["status"], "not_found")
            # Without a request, the coverage status stands.
            self.assertEqual(catalog.status_of(2), "unmatched")
            # No coverage row at all reads as pending.
            self.assertEqual(catalog.status_of(99), "pending")
            # And track() agrees with status_of on the same inputs.
            for index in (1, 2):
                self.assertEqual(catalog.track(index, requests)["status"],
                                 catalog.status_of(index, requests))
                self.assertEqual(catalog.track(index)["status"],
                                 catalog.status_of(index))


class CoverageUpdatedAtTests(unittest.TestCase):
    """Plan 045: coverage.updated_at is NULL from the legacy import
    (the file carries no times) and stamped by every report write."""

    def test_updated_at_round(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "requests.sqlite3"
            backup = root / "state-backup"
            backup.mkdir()
            (backup / "coverage.csv").write_text(
                "index,status,artist,title,uri,path,reason\n"
                "1,verified,Artist,Title,spotify:track:1,1 - Song.mp3,\n",
                encoding="utf-8")
            store = CatalogStore(database, state_dir=backup)
            rows = store.read_coverage()
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["updated_at"])
            before = time.time()
            store.write_coverage([{"index": 1, "status": "verified",
                                   "artist": "Artist", "title": "Title",
                                   "uri": "spotify:track:1",
                                   "path": "1 - Song.mp3", "reason": ""}])
            rows = store.read_coverage()
            self.assertEqual(len(rows), 1)
            self.assertIsNotNone(rows[0]["updated_at"])
            self.assertGreaterEqual(rows[0]["updated_at"], before)


if __name__ == "__main__":
    unittest.main()
