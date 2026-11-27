import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import sqlite3
import tempfile
import unittest
from pathlib import Path

from playback.play_events import PlayEventsStore


class PlayEventsStoreTest(unittest.TestCase):
    """Plan 045: the play_events store's record, recent, and stats."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = PlayEventsStore(Path(self.folder.name) / "requests.sqlite3")

    def rows(self):
        connection = sqlite3.connect(str(self.store.database))
        try:
            return connection.execute(
                "SELECT kind, ref, track_index, record_key, request_id, "
                "device_id, listened FROM play_events ORDER BY id").fetchall()
        finally:
            connection.close()

    def test_record_shapes_for_catalog_and_custom_refs(self):
        self.store.record("started", "412", 412, "key-412", None, "device-a", None)
        self.store.record("skipped", "412", 412, "key-412", None, "device-b", 31.5)
        self.store.record("started", "custom-17", None, None, 17, None, None)
        rows = self.rows()
        self.assertEqual(rows[0], ("started", "412", 412, "key-412", None,
                                   "device-a", None))
        self.assertEqual(rows[1], ("skipped", "412", 412, "key-412", None,
                                   "device-b", 31.5))
        self.assertEqual(rows[2], ("started", "custom-17", None, None, 17,
                                   None, None))

    def test_recent_started_newest_first_with_limit(self):
        self.store.record("started", "1", 1, "key-1", None, None, None)
        self.store.record("completed", "1", 1, "key-1", None, None, 200.0)
        self.store.record("started", "2", 2, "key-2", None, "device-a", None)
        self.store.record("started", "1", 1, "key-1", None, None, None)
        recent = self.store.recent_started(10)
        self.assertEqual([event["ref"] for event in recent], ["1", "2", "1"])
        self.assertEqual(recent[1]["device_id"], "device-a")
        self.assertIsNone(recent[0]["device_id"])
        self.assertEqual(len(self.store.recent_started(2)), 2)

    def test_stats_counts_started_events_only(self):
        self.assertEqual(self.store.recent_started(10), [])
        self.assertEqual(self.store.stats("key-1"), (0, None))
        self.store.record("started", "1", 1, "key-1", None, None, None)
        self.store.record("completed", "1", 1, "key-1", None, None, 200.0)
        self.store.record("started", "1", 1, "key-1", None, None, None)
        count, last = self.store.stats("key-1")
        self.assertEqual(count, 2)
        self.assertIsNotNone(last)
        count, last = self.store.stats("key-2")
        self.assertEqual(count, 0)
        self.assertIsNone(last)

    def test_record_never_raises_on_a_broken_database(self):
        directory = Path(self.folder.name) / "a-directory"
        directory.mkdir()
        broken = PlayEventsStore(directory)
        # A state-database hiccup must not break playback (plan 045):
        # recording against an unopenable path is swallowed.
        broken.record("started", "1", 1, "key-1", None, None, None)

    def test_history_uses_starts_for_recency_and_completions_for_counts(self):
        self.store.record("started", "1", 1, "repeated")
        self.store.record("completed", "1", 1, "repeated")
        self.store.record("started", "1", 1, "repeated")
        self.store.record("skipped", "1", 1, "repeated")
        self.store.record("completed", "2", 2, "completion-only")
        self.store.record("started", "3", 3, "start-only")
        self.store.record("skipped", "4", 4, "skip-only")
        self.store.record("started", "custom-17", request_id=17)

        self.assertEqual(self.store.history(), {
            "repeated": (self.store.stats("repeated")[1], 1),
            "completion-only": (None, 1),
            "start-only": (self.store.stats("start-only")[1], 0),
        })


if __name__ == "__main__":
    unittest.main()
