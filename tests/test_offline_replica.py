import hashlib
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from fixture_environment import isolate_room_environment as setUpModule
from test_http_server import ServerFixture, server
from catalog import offline_replica
from catalog.collection_snapshot import create_snapshot, restore_snapshot


class OfflineReplicaTests(ServerFixture, unittest.TestCase):
    def test_permission_is_per_pairing_and_host_controlled(self):
        cookie = self.lan_device_cookie("offline-test-phone")
        headers = {"Cookie": cookie, "Origin": self.lan_origin()}
        allow = {"id": "offline-test-phone", "allowed": True}
        self.assertEqual(self.request(self.lan_port, "GET", "/api/replica", headers=headers)[0], 403)
        self.assertEqual(self.request(self.lan_port, "POST", "/api/devices/offline", allow, headers)[0], 403)
        self.assertEqual(self.request(self.loop_port, "POST", "/api/devices/offline", allow)[0], 200)
        self.assertEqual(self.request(self.lan_port, "GET", "/api/replica", headers=headers)[0], 200)
        other = self.lan_device_cookie("other-test-phone")
        self.assertEqual(self.request(self.lan_port, "GET", "/api/replica", headers={"Cookie": other})[0], 403)
        self.assertEqual(self.request(self.loop_port, "POST", "/api/devices/offline", {**allow, "allowed": False})[0], 200)
        self.assertEqual(self.request(self.lan_port, "GET", "/api/replica", headers=headers)[0], 403)

    def test_identity_belongs_to_music_collection_and_survives_a_portable_copy(self):
        first = offline_replica.collection_id(server.CONFIG.media_database)
        other = Path(self.tmp.name) / "other-room" / "media.sqlite3"
        self.assertNotEqual(offline_replica.collection_id(other), first)
        snapshot = Path(self.tmp.name) / "replica-snapshot.sqlite3"
        create_snapshot(self.config_path, snapshot)
        restored = Path(self.tmp.name) / "restored-room"
        restore_snapshot(snapshot, restored)
        self.assertEqual(offline_replica.collection_id(restored / "collection.sqlite3"), first)

    def test_reused_custom_request_number_keeps_both_recordings(self):
        root = Path(self.tmp.name)
        first = root / "first.mp3"
        second = root / "second.mp3"
        first.write_bytes(b"previous original")
        second.write_bytes(b"different original")
        def key(path):
            entry = ("request:7", {"audio": "/api/audio/custom/7", "artist": "A", "title": "Song"}, path.name)
            return offline_replica.manifest([entry], "", root, None)["tracks"][0]["key"]
        self.assertNotEqual(key(first), key(second))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs audio fixtures")
    def test_phone_original_survives_restart_and_replay_with_one_copy(self):
        cookie = self.lan_device_cookie("replica-roundtrip")
        headers = {"Cookie": cookie, "Origin": self.lan_origin()}
        self.request(self.loop_port, "POST", "/api/devices/offline", {"id": "replica-roundtrip", "allowed": True})
        def page():
            status, _, data = self.request(self.lan_port, "GET", "/api/replica", headers=headers)
            self.assertEqual(status, 200)
            return json.loads(data)
        original = Path(self.tmp.name) / "phone.mp3"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=240:duration=2",
                        "-metadata", "title=Off Grid", "-metadata", "artist=Fixture", "-b:a", "320k", str(original)], check=True)
        payload = original.read_bytes()
        key = hashlib.sha256(payload).hexdigest()
        upload = {**headers, "Content-Type": "application/octet-stream", "X-Audio-Sha256": key,
                  "X-Audio-Suffix": ".mp3", "X-Audio-Title": "Off Grid", "X-Audio-Duration": "2",
                  "X-Listening-Collection": page()["collection"]}
        def send(changes=None):
            return self.request(self.lan_port, "POST", "/api/replica/original", raw=payload, headers={**upload, **(changes or {})})
        for changes in ({"X-Listening-Collection": "another"}, {"X-Audio-Sha256": "0" * 64},
                        {"X-Audio-Duration": "700"}, {"X-Audio-Title": "Oranges"}):
            self.assertEqual(send(changes)[0], 400)
        # A worker must use the active server config even when the caller has another one.
        unrelated = Path(self.tmp.name) / "unrelated.json"
        unrelated.write_text('{"audio_store": "files"}')
        with mock.patch.dict("os.environ", {"LISTENING_ROOM_CONFIG": str(unrelated)}):
            self.assertEqual(server.runner_environment()["LISTENING_ROOM_CONFIG"], str(self.config_path))
            for _ in range(2):
                status, _, data = send()
                self.assertEqual(status, 200, data)
            server.init(self.config_path)
            server.publish_phone_imports()
        self.assertEqual(server.MEDIA.store().count(), 1)
        result = page()
        self.assertEqual(result["collection"], upload["X-Listening-Collection"])
        self.assertEqual(len(result["tracks"]), 1)
        track = result["tracks"][0]
        self.assertEqual(track["key"], "phone:" + key)
        self.assertEqual(track["original"], {"hash": key, "size": len(payload), "suffix": ".mp3"})
        route = track["audio"].replace("/api/audio/", "/api/replica/audio/")
        self.assertEqual(self.request(self.lan_port, "GET", route, headers=headers)[0], 409)
        self.assertEqual(self.request(self.lan_port, "GET", route, headers=upload)[2], payload)
        self.request(self.loop_port, "POST", "/api/devices/offline", {"id": "replica-roundtrip", "allowed": False})
        self.assertEqual(self.request(self.lan_port, "GET", route, headers=upload)[0], 403)
        self.assertEqual(self.request(self.lan_port, "GET", track["audio"], headers=headers)[2], payload)
