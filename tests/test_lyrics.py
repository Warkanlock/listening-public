import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import http.client
import json
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from fixture_environment import isolate_room_environment as setUpModule
import server
from catalog.catalog_store import CatalogStore
from downloads.requests_store import RequestsStore
from metadata.external_metadata import ExternalMetadata


# ---------------------------------------------------------------------------
# Fakes: a transport that routes to a per-test behavior and records calls.
# No test in this file touches the real network.
# ---------------------------------------------------------------------------

class FakeTransport:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        return self.behavior(url)


def http_error(code):
    return urllib.error.HTTPError("https://lrclib.net", code, "error", None, None)


def record(index, title, artist, album="Album", length=200, key=None):
    return {"index": index, "key": key or f"key-{index}", "uri": f"spotify:track:{index}",
            "artist": artist, "title": title, "album": album,
            "length": length, "isrc": "", "first_playlist": "mix", "active": True}


CREEP = record(1, "Creep", "Radiohead", album="Pablo Honey", length=238)

FOUND = {"id": 496, "trackName": "Creep", "artistName": "Radiohead",
         "albumName": "Pablo Honey", "duration": 239.0, "instrumental": False,
         "plainLyrics": "When you were here before\nCouldn't look you in the eye\n",
         "syncedLyrics": "[00:19.16] When you were here before\n"
                         "[00:24.09] Couldn't look you in the eye\n"}


class LyricsModuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RequestsStore(Path(self.tmp.name) / "requests.sqlite3")
        self.now = [1_000_000.0]

    def make_metadata(self, behavior):
        transport = FakeTransport(behavior)
        metadata = ExternalMetadata(lambda: self.store, transport=transport,
                                    now=lambda: self.now[0])
        return metadata, transport

    def test_lookup_builds_the_lrclib_query(self):
        metadata, transport = self.make_metadata(lambda url: FOUND)
        metadata.lyrics(CREEP)
        self.assertEqual(len(transport.calls), 1)
        url, headers = transport.calls[0]
        parsed = urlparse(url)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path),
                         ("https", "lrclib.net", "/api/get"))
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        self.assertEqual(params, {"track_name": "Creep", "artist_name": "Radiohead",
                                  "album_name": "Pablo Honey", "duration": "238"})
        self.assertTrue(headers["User-Agent"].startswith("ListeningRoom"))

    def test_found_payload_shape(self):
        metadata, _ = self.make_metadata(lambda url: FOUND)
        self.assertEqual(metadata.lyrics(CREEP),
                         {"found": True, "plain": FOUND["plainLyrics"],
                          "synced": FOUND["syncedLyrics"], "source": "LRCLIB"})

    def test_found_result_is_cached(self):
        metadata, transport = self.make_metadata(lambda url: FOUND)
        metadata.lyrics(CREEP)
        metadata.lyrics(CREEP)
        self.assertEqual(len(transport.calls), 1)

    def test_not_found_is_cached_and_not_refetched(self):
        def behavior(url):
            raise http_error(404)
        metadata, transport = self.make_metadata(behavior)
        expected = {"found": False, "plain": "", "synced": "", "source": "LRCLIB"}
        self.assertEqual(metadata.lyrics(CREEP), expected)
        self.assertEqual(metadata.lyrics(CREEP), expected)
        self.assertEqual(len(transport.calls), 1)

    def test_duration_mismatch_reads_as_not_found(self):
        # LRCLIB applies the duration rule itself: it answers only when the
        # sent duration matches its record within about two seconds, and
        # 404s otherwise. The client delegates the match to the API.
        def lrclib(url):
            params = {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}
            if abs(float(params["duration"]) - FOUND["duration"]) <= 2:
                return FOUND
            raise http_error(404)
        metadata, transport = self.make_metadata(lrclib)
        wrong_version = record(9, "Creep", "Radiohead", album="Pablo Honey", length=180)
        self.assertFalse(metadata.lyrics(wrong_version)["found"])
        sent = {key: values[0] for key, values in
                parse_qs(urlparse(transport.calls[0][0]).query).items()}
        self.assertEqual(sent["duration"], "180")
        self.assertTrue(metadata.lyrics(CREEP)["found"])

    def test_out_of_range_duration_is_omitted(self):
        # LRCLIB rejects a duration outside 1..3600 with a 400; the client
        # omits the parameter instead of sending an invalid one.
        metadata, transport = self.make_metadata(lambda url: FOUND)
        metadata.lyrics(record(3, "Silent", "A", length=0))
        metadata.lyrics(record(4, "Epic", "B", length=7200))
        for url, _ in transport.calls:
            self.assertNotIn("duration", parse_qs(urlparse(url).query))

    def test_malformed_responses_read_as_not_found(self):
        metadata, _ = self.make_metadata(lambda url: ["not", "a", "record"])
        self.assertFalse(metadata.lyrics(CREEP)["found"])
        metadata, _ = self.make_metadata(lambda url: {"plainLyrics": 42, "syncedLyrics": None})
        self.assertFalse(metadata.lyrics(CREEP)["found"])

        def bad_json(url):
            raise ValueError("No JSON object could be decoded")
        metadata, _ = self.make_metadata(bad_json)
        self.assertFalse(metadata.lyrics(CREEP)["found"])

    def test_instrumental_record_reads_as_not_found(self):
        payload = dict(FOUND, instrumental=True, plainLyrics=None, syncedLyrics=None)
        metadata, _ = self.make_metadata(lambda url: payload)
        self.assertEqual(metadata.lyrics(CREEP),
                         {"found": False, "plain": "", "synced": "", "source": "LRCLIB"})

    def test_synced_only_record_derives_plain_text(self):
        payload = dict(FOUND, plainLyrics=None)
        metadata, _ = self.make_metadata(lambda url: payload)
        result = metadata.lyrics(CREEP)
        self.assertTrue(result["found"])
        self.assertEqual(result["synced"], FOUND["syncedLyrics"])
        self.assertEqual(result["plain"],
                         "When you were here before\nCouldn't look you in the eye")

    def test_transport_failure_propagates_and_is_not_cached(self):
        state = {"failing": True}

        def behavior(url):
            if state["failing"]:
                raise http_error(503)
            return FOUND
        metadata, transport = self.make_metadata(behavior)
        with self.assertRaises(urllib.error.HTTPError):
            metadata.lyrics(CREEP)
        state["failing"] = False
        self.assertTrue(metadata.lyrics(CREEP)["found"])
        self.assertEqual(len(transport.calls), 2)


# ---------------------------------------------------------------------------
# HTTP: the /api/lyrics/<index> route against a seeded catalog, with the
# server's metadata module swapped for one driven by a fake transport.
# ---------------------------------------------------------------------------

class LyricsHttpTests(unittest.TestCase):
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
        state = cls.workspace / "state"
        state.mkdir(parents=True, exist_ok=True)
        records = [record(1, "Creep", "Radiohead", album="Pablo Honey", length=238),
                   record(2, "Obscure Song", "Unknown Artist"),
                   record(3, "Spare Song", "Spare Artist")]
        manifest = {"records": records, "playlists": {"mix": [1, 2, 3]}, "issues": [],
                    "covers": {}, "list_names": {}}
        store = CatalogStore(server.DB)
        store.write_build(manifest)
        store.write_coverage(
            [{"index": item["index"], "status": "verified",
              "artist": item["artist"], "title": item["title"],
              "uri": item["uri"], "path": "", "reason": ""}
             for item in records])
        server.catalog.reload()
        server.catalog.refresh()
        cls.loop_server = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.loop_port = cls.loop_server.server_address[1]
        threading.Thread(target=cls.loop_server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.loop_server.shutdown()
        cls.loop_server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.calls = []
        self.behavior = lambda url: FOUND

        def transport(url, headers):
            self.calls.append(url)
            return self.behavior(url)
        self.original_metadata = server.metadata
        server.metadata = ExternalMetadata(server.requests_store, transport=transport)
        self.addCleanup(setattr, server, "metadata", self.original_metadata)

    def get(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.loop_port, timeout=10)
        connection.request("GET", path)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, data)
        connection.close()
        return result

    def test_unknown_ref_404s_cleanly(self):
        status, data = self.get("/api/lyrics/99")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(data))
        self.assertEqual(self.calls, [])

    def test_known_track_with_lyrics(self):
        status, data = self.get("/api/lyrics/1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data),
                         {"found": True, "plain": FOUND["plainLyrics"],
                          "synced": FOUND["syncedLyrics"], "source": "LRCLIB"})
        params = {key: values[0] for key, values in
                  parse_qs(urlparse(self.calls[0]).query).items()}
        self.assertEqual(params["track_name"], "Creep")
        self.assertEqual(params["artist_name"], "Radiohead")

    def test_downloaded_custom_request_lyrics_require_a_room_profile(self):
        requests = server.requests_store()
        request_id = requests.enqueue(None, "Room artist", "Room song", "", 238, "Room song")
        url = f"/api/lyrics/custom-{request_id}"
        self.assertEqual(self.get(url)[0], 404)
        requests.finish(request_id, "downloaded", "", "custom.mp3")
        with patch.object(server.Handler, "_member_profile", return_value=3):
            self.assertEqual(self.get(url)[0], 403)
        self.assertEqual(self.calls, [])
        status, data = self.get(url)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["found"])
        self.assertEqual(parse_qs(urlparse(self.calls[0]).query)["track_name"], ["Room song"])

    def test_known_track_without_lyrics_serves_the_not_found_state(self):
        def behavior(url):
            raise http_error(404)
        self.behavior = behavior
        status, data = self.get("/api/lyrics/2")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data),
                         {"found": False, "plain": "", "synced": "", "source": "LRCLIB"})
        self.get("/api/lyrics/2")
        self.assertEqual(len(self.calls), 1)  # cached: not refetched per open


if __name__ == "__main__":
    unittest.main()
