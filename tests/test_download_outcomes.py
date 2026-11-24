import json
import unittest

from fixture_environment import isolate_room_environment as setUpModule
from test_http_server import ServerFixture, server


class DownloadActivityTests(ServerFixture, unittest.TestCase):
    def test_recent_downloads_only_include_resolvable_audio_and_outcomes(self):
        self.assertEqual(self.request(self.lan_port, "GET", "/api/requests?activity=1")[0], 401)
        headers = {"Cookie": self.lan_device_cookie("download-activity-fixture")}
        store = server.requests_store()
        ready = store.enqueue(None, "Fixture", "Playable song", "", 30, "Playable song")
        missing = store.enqueue(None, "Fixture", "Missing file", "", 30, "Missing file")
        existing = store.enqueue(None, "Fixture", "Already saved", "", 30, "Already saved")
        no_match = store.enqueue(None, "Fixture", "No match", "", 30, "No match")
        store.enqueue(None, "Fixture", "Still queued", "", 30, "Still queued")
        server.LIBRARY.mkdir(parents=True, exist_ok=True)
        payload = b"test audio payload"
        (server.LIBRARY / "copy.mp3").write_bytes(payload)
        store.finish(ready, "downloaded", "", "copy.mp3")
        store.finish(missing, "downloaded", "", "missing.mp3")
        store.finish(existing, "downloaded", "Already in the library", "copy.mp3")
        store.finish(no_match, "not_found", "", "")
        status, _, raw = self.request(self.lan_port, "GET", "/api/requests?activity=1", headers=headers)
        self.assertEqual(status, 200)
        activity = json.loads(raw)
        self.assertEqual(activity["downloads"], [{"id": ready, "track_index": None,
                         "title": "Playable song", "artist": "Fixture", "audio": f"/api/audio/custom/{ready}"}])
        self.assertEqual([row["status"] for row in activity["outcomes"]], ["not_found", "downloaded", "downloaded"])
        ready_key = activity["outcomes"][2]["key"]
        self.assertTrue(ready_key.startswith(f"{ready}/downloaded/"))
        self.assertNotIn("audio_path", raw.decode())
        streamed = self.request(self.lan_port, "GET", f"/api/audio/custom/{ready}", headers=headers)
        self.assertEqual((streamed[0], streamed[2]), (200, payload))
        store.retry(no_match, "No match")
        store.finish(no_match, "not_found", "", "")
        refreshed = json.loads(self.request(self.lan_port, "GET", "/api/requests?activity=1", headers=headers)[2])
        self.assertNotEqual(activity["outcomes"][0]["key"], refreshed["outcomes"][0]["key"])
        self.assertEqual(refreshed["outcomes"][2]["key"], ready_key)
