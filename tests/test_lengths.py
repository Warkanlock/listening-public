import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import contextlib
import io
import json
import tempfile
import unittest
from unittest import mock

from fixture_environment import isolate_room_environment as setUpModule
from runner_fixture import runner
from catalog.catalog import Catalog, CatalogPaths
from catalog.lengths import LengthStore
from library.library_protocol import write_xspf

URI = "spotify:track:" + "A" * 22
URI2 = "spotify:track:" + "B" * 22
URI3 = "spotify:track:" + "C" * 22


def reset_runner_caches():
    """Drop the per-invocation module caches so a test is a fresh run."""
    if hasattr(runner, "_probe_cache"):
        runner._probe_cache = None
    if hasattr(runner, "_probe_cache_dirty"):
        runner._probe_cache_dirty = False
    if hasattr(runner, "_REPORT_DIRTY"):
        runner._REPORT_DIRTY = False


class LengthStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "state" / "requests.sqlite3"

    def test_round_trip(self):
        store = LengthStore(self.path)
        self.assertEqual(store.set("key-1", 337.5), 337.5)
        reopened = LengthStore(self.path)
        self.assertEqual(reopened.get("key-1"), 337.5)
        self.assertIsNone(reopened.get("key-2"))

    def test_non_positive_duration_is_not_stored(self):
        store = LengthStore(self.path)
        self.assertIsNone(store.set("key-1", 0))
        self.assertIsNone(store.set("key-1", -3.0))
        self.assertEqual(store.load(), {})

    def test_stamp_counts_writes(self):
        store = LengthStore(self.path)
        self.assertEqual(store.stamp(), (0, 0.0))
        store.set("key-1", 200.0)
        count, _newest = LengthStore(self.path).stamp()
        self.assertEqual(count, 1)


class BackfillWorkspaceTestCase(unittest.TestCase):
    """Point every runner path global at a temp workspace, with one
    XSPF list in the sources folder."""

    TRACKS = ()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.state = root / "state"
        self.state.mkdir()
        self.library = root / "audio"
        self.library.mkdir()
        self.playlists_dir = root / "playlists"
        self.playlists_dir.mkdir()
        self.outputs = root / "reports"
        self.sources = root / "lists"
        self.sources.mkdir()
        self.write_list(self.TRACKS)
        reset_runner_caches()
        self.addCleanup(reset_runner_caches)
        patchers = [
            mock.patch.object(runner, "ROOT", root),
            mock.patch.object(runner, "resolve_config_path", lambda: root / "settings.json"),
            mock.patch.object(runner, "STATE", self.state),
            mock.patch.object(runner, "LIBRARY", self.library),
            mock.patch.object(runner, "PLAYLISTS", self.playlists_dir),
            mock.patch.object(runner, "OUTPUTS", self.outputs),
            mock.patch.object(runner, "SOURCES", self.sources),
            mock.patch.object(runner, "REQUESTS_DB", self.state / "requests.sqlite3"),
            mock.patch.object(runner, "free_bytes", lambda: 100 * 1024**3),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_list(self, tracks, name="mix"):
        (self.sources / f"{name}.xspf").write_text(
            write_xspf("My Mix", tracks), encoding="utf-8")

    def run_prepare(self):
        with contextlib.redirect_stdout(io.StringIO()):
            runner.prepare()
        return runner.catalog_store().read_build()

    def run_report(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.report()
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def lengths(self):
        return LengthStore(self.state / "requests.sqlite3")

    def media(self, title, artist, album="Album", duration=200.0, bitrate=320000):
        return {"duration": duration, "bitrate": bitrate, "title": title,
                "artist": artist, "album": album, "genre": "Jazz"}


class BackfillFromVerifiedMatchTests(BackfillWorkspaceTestCase):
    """Plan 058: a length-less record matched by a verified file gains
    the probed duration, and the next rebuild carries it."""

    TRACKS = ({"title": "Blue in Green", "artist": "Miles Davis",
               "album": "Kind of Blue", "identifier": URI,
               "image": "", "isrc": "", "location": ""},)

    def test_gains_probed_duration_and_keeps_it_across_rebuilds(self):
        build = self.run_prepare()
        self.assertEqual(build["records"][0]["length"], "")
        (self.library / "Blue in Green.mp3").write_bytes(b"audio")
        media = self.media("Blue in Green", "Miles Davis",
                           album="Kind of Blue", duration=337.0)
        with mock.patch.object(runner, "probe", lambda path: dict(media)):
            summary = self.run_report()
        self.assertEqual(summary["verified"], 1)
        # The measurement is kept in the store, keyed by the record key.
        self.assertEqual(self.lengths().get(URI), 337.0)
        # A rebuild materializes it into the record...
        build = self.run_prepare()
        self.assertEqual(build["records"][0]["length"], 337.0)
        # ...where the read surfaces see it through the record seam.
        catalog = Catalog(CatalogPaths(database=self.state / "requests.sqlite3",
                                       lists=self.sources))
        catalog.refresh()
        self.assertEqual(catalog.track(1)["duration"], 337.0)
        # And it survives further rebuilds unchanged.
        build = self.run_prepare()
        self.assertEqual(build["records"][0]["length"], 337.0)


class ListLengthWinsTests(BackfillWorkspaceTestCase):
    """Plan 058: a length from the list is never overridden, and the
    store is never written for a record that carries one."""

    TRACKS = ({"title": "Blue in Green", "artist": "Miles Davis",
               "album": "Kind of Blue", "duration_ms": 200000,
               "identifier": URI, "image": "", "isrc": "", "location": ""},)

    def test_stored_value_never_overrides_a_list_length(self):
        self.lengths().set(URI, 123.0)
        build = self.run_prepare()
        self.assertEqual(build["records"][0]["length"], 200.0)

    def test_verified_match_writes_nothing_for_a_list_length(self):
        build = self.run_prepare()
        self.assertEqual(build["records"][0]["length"], 200.0)
        (self.library / "Blue in Green.mp3").write_bytes(b"audio")
        media = self.media("Blue in Green", "Miles Davis",
                           album="Kind of Blue", duration=201.0)
        with mock.patch.object(runner, "probe", lambda path: dict(media)):
            summary = self.run_report()
        self.assertEqual(summary["verified"], 1)
        self.assertIsNone(self.lengths().get(URI))
        build = self.run_prepare()
        self.assertEqual(build["records"][0]["length"], 200.0)


class UnmatchedRecordTests(BackfillWorkspaceTestCase):
    """Plan 058: a record no verified file matches stays length-less."""

    TRACKS = ({"title": "Blue in Green", "artist": "Miles Davis",
               "album": "Kind of Blue", "identifier": URI,
               "image": "", "isrc": "", "location": ""},)

    def test_unmatched_record_stays_lengthless(self):
        self.run_prepare()
        (self.library / "Somebody Elses Song.mp3").write_bytes(b"audio")
        media = self.media("Other Title", "Other Artist")
        with mock.patch.object(runner, "probe", lambda path: dict(media)):
            summary = self.run_report()
        self.assertEqual(summary["verified"], 0)
        self.assertIsNone(self.lengths().get(URI))
        build = self.run_prepare()
        self.assertEqual(build["records"][0]["length"], "")


class IndexStabilityWithBackfillsTests(BackfillWorkspaceTestCase):
    """Plan 058: backfills change one field of an existing record;
    prepare's index assertion (unique, contiguous, no key moved)
    still passes with them present, and later records append."""

    TRACKS = ({"title": "Blue in Green", "artist": "Miles Davis",
               "album": "Kind of Blue", "identifier": URI,
               "image": "", "isrc": "", "location": ""},
              {"title": "So What", "artist": "Miles Davis",
               "album": "Kind of Blue", "identifier": URI2,
               "image": "", "isrc": "", "location": ""})

    def test_indexes_and_keys_hold_with_backfills_present(self):
        build = self.run_prepare()
        before = [(r["index"], r["key"]) for r in build["records"]]
        self.assertEqual(before, [(1, URI), (2, URI2)])
        (self.library / "Blue in Green.mp3").write_bytes(b"audio")
        media = self.media("Blue in Green", "Miles Davis",
                           album="Kind of Blue", duration=337.0)
        with mock.patch.object(runner, "probe", lambda path: dict(media)):
            self.run_report()
        self.assertEqual(self.lengths().get(URI), 337.0)
        # Rebuild with the backfill present: prepare asserts stability
        # internally; the mapping and the filled length both hold.
        build = self.run_prepare()
        self.assertEqual([(r["index"], r["key"]) for r in build["records"]],
                         before)
        self.assertEqual(build["records"][0]["length"], 337.0)
        self.assertEqual(build["records"][1]["length"], "")
        # A record arriving later appends at the next index; nothing moves.
        self.write_list(self.TRACKS + (
            {"title": "Freddie Freeloader", "artist": "Miles Davis",
             "album": "Kind of Blue", "identifier": URI3,
             "image": "", "isrc": "", "location": ""},))
        build = self.run_prepare()
        self.assertEqual([(r["index"], r["key"]) for r in build["records"]],
                         [(1, URI), (2, URI2), (3, URI3)])
        self.assertEqual(build["records"][0]["length"], 337.0)


if __name__ == "__main__":
    unittest.main()
