"""Plan 052: the index-guard dialects, pinned at the HTTP layer.

Nine sites in server.py map a URL or body index to a catalog
record, and they do not all speak the same dialect: favorites and
genre accept an inactive record, requests and its batch refuse
one, the path endpoints answer 404 where the body endpoints
answer 400. These tests pin each endpoint's exact status and
error body for a valid index, 0, N+1, a JSON true, and the
inactive record, so the record seam's migration (plan 052 Steps
2-3) cannot flip a dialect without failing here.

The fixture mirrors tests/test_favorites.py (this module imports
no other test module; unittest discovery collects imported
TestCases twice). Seed: five records, record 3 inactive, record 2
verified with a real file in the audio folder, the rest pending.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

from fixture_environment import isolate_room_environment as setUpModule
import server
from catalog.catalog_store import CatalogStore
from downloads.requests_store import RequestsStore


SEED = [
    (1, "Alpha Band", "First Song", True, "pending"),
    (2, "Beta Singer", "Second Song", True, "verified"),
    (3, "Gamma Trio", "Third Song", False, "pending"),
    (4, "Delta Poet", "Fourth Song", True, "pending"),
    (5, "Epsilon Voice", "Fifth Song", True, "pending"),
]
N = len(SEED)
AUDIO_BYTES = b"plan-052 fixture audio bytes"


class DialectFixture(unittest.TestCase):
    """server.init against a temp workspace, one real server on an
    ephemeral loopback port (the host tier every dialect endpoint
    sits at, beside the paired-device tier)."""

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
                    "active": active}
                   for index, artist, title, active, _status in SEED]
        store = CatalogStore(server.DB)
        store.write_build({"records": records,
                           "playlists": {"mix": [row[0] for row in SEED]},
                           "issues": [], "covers": {},
                           "list_names": {"mix": "Mix"}})
        store.write_coverage(
            [{"index": index, "status": status, "artist": artist,
              "title": title, "uri": "",
              "path": "song-2.mp3" if index == 2 else "", "reason": ""}
             for index, artist, title, _active, status in SEED])
        audio_dir = cls.workspace / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / "song-2.mp3").write_bytes(AUDIO_BYTES)
        # The metadata and lyrics answers come from the external
        # cache, pre-seeded so no test leaves the machine (the
        # test_favorites pattern).
        cache = RequestsStore(server.DB)
        cache.cache_put("metadata:key-5", "{}", time.time())
        cache.cache_put("metadata:key-3", "{}", time.time())
        cache.cache_put("lyrics:key-1",
                        json.dumps({"found": False, "plain": "",
                                    "synced": "", "source": "LRCLIB"}),
                        time.time())
        server.catalog.reload()
        server.catalog.refresh()
        cls.loop_server = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.loop_server.server_address[1]
        threading.Thread(target=cls.loop_server.serve_forever,
                         daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.loop_server.shutdown()
        cls.loop_server.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port,
                                                timeout=10)
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path,
                           body=json.dumps(body) if body is not None else None,
                           headers=headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, data)
        connection.close()
        return result

    def error_body(self, method, path, body=None):
        status, data = self.request(method, path, body)
        return status, json.loads(data)


class MetadataDialectTests(DialectFixture):
    """GET /api/metadata/<i>: plain range, 404 "Unknown song",
    no active check."""

    def test_valid_index_answers(self):
        status, data = self.request("GET", "/api/metadata/5")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["title"], "Fifth Song")

    def test_zero_and_beyond_are_unknown_song(self):
        for index in (0, N + 1):
            status, body = self.error_body("GET", f"/api/metadata/{index}")
            self.assertEqual(status, 404, index)
            self.assertEqual(body, {"error": "Unknown song"}, index)

    def test_inactive_record_answers(self):
        status, _ = self.request("GET", "/api/metadata/3")
        self.assertEqual(status, 200)


class LyricsDialectTests(DialectFixture):
    """GET /api/lyrics/<i>: the same guard as metadata; only the
    guard outcomes are pinned (the payload is the cache's)."""

    def test_zero_and_beyond_are_unknown_song(self):
        for index in (0, N + 1):
            status, body = self.error_body("GET", f"/api/lyrics/{index}")
            self.assertEqual(status, 404, index)
            self.assertEqual(body, {"error": "Unknown song"}, index)

    def test_valid_index_is_not_unknown_song(self):
        status, data = self.request("GET", "/api/lyrics/1")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(data)["found"])


class FavoritesDialectTests(DialectFixture):
    """POST /api/favorites: int, not bool, range; 400 "Unknown
    song"; an inactive record is accepted (pinned difference)."""

    def test_valid_index_answers(self):
        status, data = self.request("POST", "/api/favorites",
                                    {"index": 1, "favorite": True})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"index": 1, "favorite": True})

    def test_zero_and_beyond_are_unknown_song(self):
        for index in (0, N + 1):
            status, body = self.error_body(
                "POST", "/api/favorites", {"index": index, "favorite": True})
            self.assertEqual(status, 400, index)
            self.assertEqual(body, {"error": "Unknown song"}, index)

    def test_bool_index_is_unknown_song(self):
        status, body = self.error_body(
            "POST", "/api/favorites", {"index": True, "favorite": True})
        self.assertEqual(status, 400)
        self.assertEqual(body, {"error": "Unknown song"})

    def test_inactive_record_is_accepted(self):
        status, data = self.request("POST", "/api/favorites",
                                    {"index": 3, "favorite": True})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"index": 3, "favorite": True})


class GenreDialectTests(DialectFixture):
    """POST /api/genre: int, not bool, range; 400 "Unknown song";
    an inactive record is accepted, like favorites."""

    def test_valid_index_answers(self):
        status, data = self.request("POST", "/api/genre",
                                    {"track_id": 4, "genre": "Rock"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["genre"], "Rock")

    def test_zero_and_beyond_are_unknown_song(self):
        for index in (0, N + 1):
            status, body = self.error_body(
                "POST", "/api/genre", {"track_id": index, "genre": "Rock"})
            self.assertEqual(status, 400, index)
            self.assertEqual(body, {"error": "Unknown song"}, index)

    def test_bool_index_is_unknown_song(self):
        status, body = self.error_body(
            "POST", "/api/genre", {"track_id": True, "genre": "Rock"})
        self.assertEqual(status, 400)
        self.assertEqual(body, {"error": "Unknown song"})

    def test_inactive_record_is_accepted(self):
        status, _ = self.request("POST", "/api/genre",
                                 {"track_id": 3, "genre": "Jazz"})
        self.assertEqual(status, 200)


class RequestsDialectTests(DialectFixture):
    """POST /api/requests (track_id): int, not bool, range, and
    active; 400 "Unknown track"; a verified record is a 409."""

    def test_valid_active_unverified_proceeds(self):
        status, _ = self.request("POST", "/api/requests", {"track_id": 1})
        self.assertEqual(status, 201)

    def test_zero_and_beyond_are_unknown_track(self):
        for index in (0, N + 1):
            status, body = self.error_body(
                "POST", "/api/requests", {"track_id": index})
            self.assertEqual(status, 400, index)
            self.assertEqual(body, {"error": "Unknown track"}, index)

    def test_bool_index_is_unknown_track(self):
        status, body = self.error_body(
            "POST", "/api/requests", {"track_id": True})
        self.assertEqual(status, 400)
        self.assertEqual(body, {"error": "Unknown track"})

    def test_inactive_record_is_refused(self):
        status, body = self.error_body(
            "POST", "/api/requests", {"track_id": 3})
        self.assertEqual(status, 400)
        self.assertEqual(body, {"error": "Unknown track"})

    def test_verified_record_is_already_available(self):
        status, body = self.error_body(
            "POST", "/api/requests", {"track_id": 2})
        self.assertEqual(status, 409)
        self.assertEqual(body, {"error": "Already available"})


class RequestsBatchDialectTests(DialectFixture):
    """POST /api/requests/batch: the list shape is validated
    first ("Choose up to 50 library songs"), then every item must
    be in range and active, the whole batch refused with "One of
    those songs is no longer in the library"."""

    def test_all_valid_answers(self):
        status, data = self.request("POST", "/api/requests/batch",
                                    {"track_ids": [4, 5]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["added"], 2)

    def test_one_out_of_range_item_refuses_the_batch(self):
        status, body = self.error_body(
            "POST", "/api/requests/batch", {"track_ids": [4, N + 1]})
        self.assertEqual(status, 400)
        self.assertEqual(
            body, {"error": "One of those songs is no longer in the library"})

    def test_one_inactive_item_refuses_the_batch(self):
        status, body = self.error_body(
            "POST", "/api/requests/batch", {"track_ids": [3]})
        self.assertEqual(status, 400)
        self.assertEqual(
            body, {"error": "One of those songs is no longer in the library"})

    def test_a_bool_item_is_a_shape_error(self):
        status, body = self.error_body(
            "POST", "/api/requests/batch", {"track_ids": [True]})
        self.assertEqual(status, 400)
        self.assertEqual(body, {"error": "Choose up to 50 library songs"})


class PlaylistCopiesTests(DialectFixture):
    def test_entire_playlist_skips_playable_inactive_and_already_queued_songs(self):
        records = [
            {"index": number, "key": f"song-{number}", "uri": "", "artist": "Fixture Artist",
             "title": f"Song {number}", "album": "", "length": 180, "active": number != 3}
            for number in range(1, 64)
        ]
        store = CatalogStore(server.DB)
        store.write_build({"records": records, "playlists": {"all-songs": [*range(1, 63), 4]},
                           "issues": [], "covers": {}, "list_names": {"all-songs": "All songs"}})
        store.write_coverage([{"index": 2, "status": "verified", "artist": "Fixture Artist",
                              "title": "Song 2", "uri": "", "path": "song-2.mp3", "reason": ""}])
        server.catalog.reload()
        path = "/api/requests/batch"
        payload = {"playlist": "all-songs"}
        for expected in (60, 0):
            status, data = self.request("POST", path, payload)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data)["added"], expected)
        requests = RequestsStore(server.DB).by_track()
        self.assertEqual(set(requests), {1, *range(4, 63)})


class AudioDialectTests(DialectFixture):
    """GET /api/audio/<i>: plain range, 404 "Audio unavailable";
    a valid verified index serves the file."""

    def test_zero_and_beyond_are_audio_unavailable(self):
        for index in (0, N + 1):
            status, body = self.error_body("GET", f"/api/audio/{index}")
            self.assertEqual(status, 404, index)
            self.assertEqual(body, {"error": "Audio unavailable"}, index)

    def test_valid_verified_index_serves_the_file(self):
        status, data = self.request("GET", "/api/audio/2")
        self.assertEqual(status, 200)
        self.assertEqual(data, AUDIO_BYTES)


if __name__ == "__main__":
    unittest.main()
