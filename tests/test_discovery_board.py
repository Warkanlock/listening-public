import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from metadata.discovery_board import headlines, listening_activity, weekly_press
from metadata.discovery_trail import LibraryRecordings, search_leads


class DiscoveryBoardTests(unittest.TestCase):
    def test_recency_uses_stable_keys_and_active_linked_copies(self):
        catalog = Mock()
        catalog.records = [{"key": "old", "active": False, "linked_recording": "group"},
                           {"index": 2, "key": "new", "artist": "Band", "title": "Take", "linked_recording": "group"},
                           {"index": 3, "key": "solo", "artist": "Solo", "title": "Mix"}]
        catalog.recording_keys.side_effect = lambda r: ("old", "new") if r.get("linked_recording") else ("solo",)
        likes, plays = Mock(), Mock()
        likes.load.return_value = {"old": 3, "solo": 1}
        plays.history.return_value = {"old": (2, 0), "solo": (4, 1)}
        activity = listening_activity(catalog, likes, plays)
        self.assertEqual([row["id"] for row in activity["liked"]], [2, 3])
        self.assertEqual([row["id"] for row in activity["played"]], [3, 2])

    def test_press_filters_dates_on_cache_reads_and_only_links_to_publisher(self):
        def item(path, day, host="www.theguardian.com"):
            return f"<item><link>https://{host}/{path}</link><title>{path}</title><pubDate>{day} Nov 2026 12:00:00 GMT</pubDate></item>"
        body = "<rss><channel>" + "".join([item("past", 1), item("new", 19), item("new", 19),
                                          item("untrusted", 20, "other.example"), item("future", 22)]) + "</channel></rss>"
        meta = Mock()
        meta.cached.return_value = headlines(body.encode())
        result = weekly_press(meta, datetime(2026, 11, 20, 12, tzinfo=timezone.utc))
        self.assertEqual([s["title"] for s in result["stories"]], ["new"])
        with self.assertRaises(ValueError):
            headlines(b"not a feed")

    def test_search_rechecks_catalog_and_deduplicates_recording_ids(self):
        meta = Mock()
        row = {"musicbrainz_id": "one", "title": "Fire", "creator": "Band", "duration_ms": 180000}
        live = {**row, "musicbrainz_id": "two", "version": "Live", "duration_ms": 260000}
        meta.catalog_recordings.return_value = ([row, live, live], 3)
        known = LibraryRecordings([{"artist": "Band", "title": "Fire", "length": 180}])
        result = search_leads(meta, "Band", known)
        self.assertEqual([lead["id"] for lead in result["leads"]], ["two"])
        self.assertEqual(result["leads"][0]["track"]["duration"], 260)
