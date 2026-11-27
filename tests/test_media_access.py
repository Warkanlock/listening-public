import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import json
import tempfile
import zlib
import unittest

from catalog.audio_store import (EXPORT_STATUS_IDLE, MediaStore,
                                 blob_ref, dumps_export_status,
                                 read_export_status)
from catalog.media_access import MediaAccess

PROBE = {"duration": 200.0, "bitrate": 320000, "title": "Song One",
         "artist": "Artist 1", "album": "Album", "genre": "Jazz"}
AUDIO_BYTES = b"fake mp3 audio bytes for plan 061 tests" * 8
CUSTOM_BYTES = b"a custom request's audio" * 4


class FakeSources:
    """MediaAccess's sources seam, bound to plain data (plan 061
    step 1): the server binds its live globals, tests bind this."""

    def __init__(self, coverage=(), records=None, downloaded=(),
                 requests=None):
        self._coverage = list(coverage)
        self._records = dict(records or {})
        self._downloaded = list(downloaded)
        self._requests = dict(requests or {})

    def coverage(self):
        return list(self._coverage)

    def records(self):
        return dict(self._records)

    def downloaded(self):
        return list(self._downloaded)

    def request(self, request_id):
        return self._requests.get(request_id)


class MediaAccessTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.database = self.root / "media.sqlite3"
        self.buffer_dir = self.root / "buffer"
        self.status_path = self.root / "export-status.json"

    def access(self, sources):
        return MediaAccess(self.database, self.buffer_dir,
                           self.status_path, sources)


class NoDatabaseTests(MediaAccessTestCase):
    """No media database file: every read reports absence, in the
    shape each caller already expects."""

    def setUp(self):
        super().setUp()
        self.media = self.access(FakeSources())

    def test_store_and_serving_are_absent(self):
        self.assertIsNone(self.media.store())
        self.assertEqual(self.media.serving(), (None, None))

    def test_facts_and_dead_bytes_are_none(self):
        self.assertIsNone(self.media.facts())
        self.assertIsNone(self.media.dead_bytes())

    def test_no_targets_without_media_database(self):
        self.assertEqual(self.media.targets(), [])

    def test_export_status_is_idle(self):
        self.assertEqual(self.media.export_status(), EXPORT_STATUS_IDLE)


class WithDatabaseTests(MediaAccessTestCase):
    """A media database with one catalog blob and one custom
    request blob, named by fake coverage and downloads."""

    def setUp(self):
        super().setUp()
        store = MediaStore(self.database)
        source = self.root / "seed.mp3"
        source.write_bytes(AUDIO_BYTES)
        self.key = store.put_file(source, dict(PROBE))
        custom_source = self.root / "custom.mp3"
        custom_source.write_bytes(CUSTOM_BYTES)
        self.custom_key = store.put_file(
            custom_source, dict(PROBE, title="Custom Song",
                                artist="Custom Artist"))
        record = {"index": 1, "artist": "Artist 1", "title": "Song One",
                  "uri": "spotify:track:one", "length": 200}
        sources = FakeSources(
            coverage=[{"index": 1, "status": "verified",
                       "artist": "Artist 1", "title": "Song One",
                       "uri": record["uri"], "path": blob_ref(self.key),
                       "reason": ""}],
            records={1: record},
            downloaded=[{"id": 7, "track_index": None,
                         "audio_path": blob_ref(self.custom_key)}],
            requests={7: {"artist": "Custom Artist",
                          "title": "Custom Song"}})
        self.media = self.access(sources)

    def test_store_and_serving_pair(self):
        store = self.media.store()
        self.assertIsNotNone(store)
        self.assertTrue(store.has(self.key))
        serving_store, buffer = self.media.serving()
        self.assertIsNotNone(serving_store)
        self.assertEqual(buffer.directory, self.buffer_dir)

    def test_facts_count_the_database(self):
        self.assertEqual(self.media.facts(), {
            "tracks": 2,
            "audio_bytes": len(AUDIO_BYTES) + len(CUSTOM_BYTES),
            "stored_bytes": sum(min(len(x), len(zlib.compress(x))) for x in (AUDIO_BYTES, CUSTOM_BYTES)),
            "dead_bytes": 0})
        self.assertEqual(self.media.dead_bytes(), 0)

    def test_targets_name_both_blobs(self):
        targets = dict(self.media.targets())
        self.assertEqual(targets, {
            self.key: "00001 - Artist 1 - Song One.mp3",
            self.custom_key: "custom/7 - Custom Artist - Custom Song.mp3"})



class ExportStatusCodecTests(unittest.TestCase):
    """The export-status codec (plan 061 step 1): the runner and
    the server share one shape, and absence reads as idle."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "export-status.json"

    def test_round_trip(self):
        status = {"state": "running", "done": 3, "total": 9,
                  "failed": 1, "stamp": 123.5}
        self.path.write_text(dumps_export_status(status),
                             encoding="utf-8")
        self.assertEqual(read_export_status(self.path), status)
        self.assertEqual(json.loads(dumps_export_status(status)), status)

    def test_missing_file_is_idle(self):
        self.assertEqual(read_export_status(self.path),
                         EXPORT_STATUS_IDLE)

    def test_garbage_file_is_idle(self):
        self.path.write_text("not json at all", encoding="utf-8")
        self.assertEqual(read_export_status(self.path),
                         EXPORT_STATUS_IDLE)

    def test_non_dict_json_is_idle(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(read_export_status(self.path),
                         EXPORT_STATUS_IDLE)

    def test_unreadable_path_is_idle(self):
        self.assertEqual(read_export_status(Path(self.folder.name)),
                         EXPORT_STATUS_IDLE)

    def test_idle_default_is_a_fresh_copy(self):
        first = read_export_status(self.path)
        first["state"] = "mutated"
        self.assertEqual(read_export_status(self.path),
                         EXPORT_STATUS_IDLE)


if __name__ == "__main__":
    unittest.main()
