"""Plan 050 step 5: Your listening (Recently played, Most played).

Two layers, mirroring the suites they extend: the play_events
store's history() aggregates against a temp database
(test_play_events style), and the /api/tracks history filters over
real HTTP (test_favorites' fixture style, mirrored here so this
module imports no other test module).

The semantics pinned here are the step's: recency reads the newest
`started` event (the read surface plan 045 established), and Most
played counts only `completed` plays. A skipped play is recent,
never most played.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.parse import urlencode

from fixture_environment import isolate_room_environment as setUpModule
import server
from catalog.catalog_store import CatalogStore
from catalog.genres import GenreStore
from playback.play_events import PlayEventsStore


class FakeTime:
    """Deterministic stamps: each time() call lands one second later."""

    def __init__(self):
        self.moment = 1_700_000_000.0

    def time(self):
        self.moment += 1.0
        return self.moment


# ---------------------------------------------------------------------------
# The store aggregates
# ---------------------------------------------------------------------------

class PlayHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = PlayEventsStore(Path(self.tmp.name) / "requests.sqlite3")
        self.clock = FakeTime()

    def record(self, kind, index, key, listened=None):
        with mock.patch("playback.play_events.time", self.clock):
            self.store.record(kind, str(index), index, key, None, None,
                              listened)

    def test_empty_history(self):
        self.assertEqual(self.store.history(), {})

    def test_aggregates_count_completions_and_read_recency(self):
        self.record("started", 1, "key-1")
        self.record("completed", 1, "key-1", 200.0)
        self.record("started", 2, "key-2")
        self.record("skipped", 2, "key-2", 12.0)
        self.record("started", 1, "key-1")
        self.record("completed", 1, "key-1", 200.0)
        # A custom-request play carries no record key: absent.
        with mock.patch("playback.play_events.time", self.clock):
            self.store.record("started", "custom-9", None, None, 9, None, None)
        history = self.store.history()
        self.assertEqual(set(history), {"key-1", "key-2"})
        last_1, completed_1 = history["key-1"]
        self.assertEqual(completed_1, 2)
        last_2, completed_2 = history["key-2"]
        self.assertEqual(completed_2, 0)
        # key-2's only start is older than key-1's newest start.
        self.assertGreater(last_1, last_2)

    def test_a_completion_without_a_start_still_counts(self):
        # The union is deliberate: a terminal row whose start was
        # lost (a database hiccup is swallowed by design) must not
        # vanish from Most played.
        connection = sqlite3.connect(str(self.store.database))
        try:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS play_events ("
                "id INTEGER PRIMARY KEY, ts REAL NOT NULL, "
                "kind TEXT NOT NULL, ref TEXT NOT NULL, "
                "track_index INTEGER, record_key TEXT, "
                "request_id INTEGER, device_id TEXT, listened REAL)")
            connection.execute(
                "INSERT INTO play_events (ts, kind, ref, track_index, "
                "record_key, listened) VALUES (?, 'completed', '7', 7, "
                "'key-7', 180.0)", (1_700_000_500.0,))
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(self.store.history(), {"key-7": (None, 1)})


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------

class YourListeningHttpTests(unittest.TestCase):
    """server.init against a temp workspace, a real server on an
    ephemeral port: the test_favorites fixture, trimmed to what
    the tracks filters need."""

    SEED = [
        (1, "Alpha Band", "First Song"),
        (2, "Beta Singer", "Second Song"),
        (3, "Gamma Trio", "Third Song"),
        (4, "Delta Poet", "Fourth Song"),
    ]

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.workspace = base / "room"
        config_dir = base / "config"
        config_dir.mkdir(parents=True)
        cls.config_path = config_dir / "config.json"
        cls.config_path.write_text(json.dumps({"folder": str(cls.workspace)}),
                                   encoding="utf-8")
        server.init(cls.config_path)
        records = [{"index": index, "key": f"key-{index}", "uri": "",
                    "artist": artist, "title": title, "album": "",
                    "length": 200, "isrc": "", "first_playlist": "mix",
                    "active": True}
                   for index, artist, title in cls.SEED]
        store = CatalogStore(server.DB)
        store.write_build({"records": records,
                           "playlists": {"mix": [row[0] for row in cls.SEED],
                                         "few": [4]},
                           "issues": [], "covers": {},
                           "list_names": {"mix": "Mix", "few": "Few"}})
        store.write_coverage(
            [{"index": index, "status": "pending", "artist": artist,
              "title": title, "uri": "", "path": "", "reason": ""}
             for index, artist, title in cls.SEED])
        GenreStore(server.DB).set("key-3", "Rock", "file")
        server.catalog.reload()
        server.catalog.refresh()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        # Every test starts from an empty play history. The
        # history() call first forces the table into existence:
        # the store creates it lazily on connect.
        self.events = PlayEventsStore(server.DB)
        self.events.history()
        connection = sqlite3.connect(str(server.DB))
        try:
            connection.execute("DELETE FROM play_events")
            connection.commit()
        finally:
            connection.close()
        self.clock = FakeTime()

    def play(self, index, outcome):
        """One full play of a record: a start, then its ending."""
        key = f"key-{index}"
        with mock.patch("playback.play_events.time", self.clock):
            self.events.record("started", str(index), index, key)
            self.events.record(outcome, str(index), index, key,
                               listened=200.0 if outcome == "completed"
                               else 11.0)

    def seed_history(self):
        # Second Song completes, Third Song completes twice,
        # Fourth Song is skipped, then Second Song completes again:
        # last starts land key-2 newest, key-4 next, key-3 oldest;
        # completions tie key-2 and key-3 at two apiece.
        self.play(2, "completed")
        self.play(3, "completed")
        self.play(3, "completed")
        self.play(4, "skipped")
        self.play(2, "completed")

    def fetch_tracks(self, params):
        connection = http.client.HTTPConnection("127.0.0.1", self.port,
                                                timeout=10)
        connection.request("GET", "/api/tracks?" + urlencode(params))
        response = connection.getresponse()
        data = response.read()
        connection.close()
        self.assertEqual(response.status, 200)
        return json.loads(data)

    def ids(self, payload):
        return [row["id"] for row in payload["tracks"]]

    def test_recent_orders_by_last_play_and_includes_skips(self):
        self.seed_history()
        payload = self.fetch_tracks({"filter": "recent"})
        self.assertEqual(payload["total"], 3)
        self.assertEqual(self.ids(payload), [2, 4, 3])

    def test_most_counts_only_completed_plays(self):
        self.seed_history()
        payload = self.fetch_tracks({"filter": "most"})
        self.assertEqual(payload["total"], 2)
        # The completion tie breaks on recency: key-2 played last.
        self.assertEqual(self.ids(payload), [2, 3])

    def test_empty_history_is_an_empty_page_not_an_error(self):
        for filter_name in ("recent", "most"):
            payload = self.fetch_tracks({"filter": filter_name})
            self.assertEqual(payload["total"], 0)
            self.assertEqual(payload["tracks"], [])

    def test_history_filters_compose(self):
        self.seed_history()
        # With a playlist: only record 4 is in "few".
        payload = self.fetch_tracks({"filter": "recent", "playlist": "few"})
        self.assertEqual(self.ids(payload), [4])
        payload = self.fetch_tracks({"filter": "most", "playlist": "few"})
        self.assertEqual(payload["total"], 0)
        # With a query: only record 2's title matches.
        payload = self.fetch_tracks({"filter": "recent", "q": "second"})
        self.assertEqual(self.ids(payload), [2])
        # With a genre: only record 3 carries one.
        payload = self.fetch_tracks({"filter": "most", "genre": "rock"})
        self.assertEqual(self.ids(payload), [3])

    def test_history_order_paginates_without_shuffling(self):
        self.seed_history()
        first = self.fetch_tracks({"filter": "recent", "limit": 2})
        rest = self.fetch_tracks({"filter": "recent", "limit": 2,
                                  "offset": 2})
        self.assertEqual(self.ids(first), [2, 4])
        self.assertEqual(self.ids(rest), [3])
        self.assertEqual(first["total"], 3)
        self.assertEqual(rest["total"], 3)

    def test_the_plain_listing_keeps_catalog_order(self):
        self.seed_history()
        payload = self.fetch_tracks({})
        self.assertEqual(self.ids(payload), [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
