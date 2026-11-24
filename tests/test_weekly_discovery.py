"""Weekly Discovery graph, week rollover, downloads and playback through the room."""

import json
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from fixture_environment import isolate_room_environment as setUpModule
from test_http_server import ServerFixture, server
from downloads.requests_store import RequestsStore
from metadata import weekly_discovery as weekly
from metadata.discovery_trail import LibraryRecordings
from playback.playback import PlaybackSession


def identity(number):
    return f"00000000-0000-0000-0000-{number:012d}"


def pick(number, title="New song", artist="Fixture artist"):
    return {"id": identity(number), "musicbrainz_id": identity(number), "title": title, "artist": artist,
            "album": "", "version": "", "duration": 120, "seconds": 120, "depth": 0,
            "artist_id": identity(1), "trail": [artist]}


class WeeklyTests(unittest.TestCase):
    def test_graph_reaches_three_levels_and_omits_known_and_duplicate_titles(self):
        meta, connections = Mock(), Mock()
        connections.artist_match.return_value = {"id": identity(1), "title": "Artist 1"}
        connections.lookup.side_effect = lambda _, artist: {"edges": [{"node": {
            "kind": "artist", "id": identity(int(artist[-2:]) + 1), "title": f"Artist {int(artist[-2:]) + 1}"
        }}]}
        meta.cached.side_effect = lambda _, __, fetch: fetch()
        meta.listenbrainz_radio.return_value = {}

        def song(recording_id, title, artist, seconds=120):
            return {"id": identity(recording_id), "title": title, "length": seconds * 1000,
                    "artist-credit": [{"artist": {"id": artist, "name": f"Artist {int(artist[-2:])}"}}]}

        def browse(entity, options):
            self.assertEqual(entity, "recording")
            artist = options["artist"]
            number = int(artist[-2:])
            return {"recordings": [song(100 + number, f"Owned {number}", artist),
                                    song(200 + number, f"Discovery {number}", artist),
                                    song(300 + number, f"Discovery {number}", artist),
                                    song(400 + number, "Unknown duration", artist, seconds=0)]}
        meta.musicbrainz.side_effect = browse
        known = LibraryRecordings([{"artist": "Artist 1", "title": "Owned 1", "musicbrainz_id": identity(999)},
                                   {"artist": "Artist 2", "title": "Owned 2", "musicbrainz_id": identity(997)}])
        service = weekly.WeeklyDiscovery(meta, connections)
        result = service.generate(["Artist 1"], known, "2026-11-23")
        self.assertEqual({row["depth"] for row in result}, {0, 1, 2, 3})
        self.assertEqual(connections.lookup.call_count, 3)
        self.assertEqual(meta.musicbrainz.call_count, 4)
        self.assertEqual(len({(row["title"], row["artist"]) for row in result}), len(result))
        self.assertNotIn("Owned 1", [row["title"] for row in result])
        self.assertNotIn("Owned 2", [row["title"] for row in result])
        self.assertNotIn("Unknown duration", [row["title"] for row in result])
        far = next(row for row in result if row["depth"] == 3)
        self.assertEqual(far["trail"], ["Artist 1", "Artist 2", "Artist 3", "Artist 4"])

    def test_week_is_cached_across_instances_and_rolls_over(self):
        with TemporaryDirectory() as tmp:
            today = [date(2026, 11, 25)]
            store = RequestsStore(Path(tmp) / "room.db")
            cache = RequestsStore(Path(tmp) / "host.db")
            catalog, events = Mock(), Mock()
            catalog.records = [{"key": "played", "artist": "Fixture", "title": "Known"}]
            catalog.recording_keys.return_value = ("played",)
            events.history.return_value = {"played": (10, 1)}
            events.recent_started.return_value = []
            service = weekly.WeeklyDiscovery(Mock(), Mock(), today=lambda: today[0])
            release = threading.Event()

            def build(seeds, _known, _week):
                self.assertEqual(seeds, ["Fixture"])
                release.wait(2)
                return [pick(102)]

            with patch.object(service, "generate", side_effect=build) as generate:
                self.assertEqual(service.status(cache, store, "room", catalog, events)["state"], "building")
                self.assertEqual(service.status(cache, store, "room", catalog, events)["state"], "building")
                release.set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    outcome = service.status(cache, store, "room", catalog, events)
                    if outcome["state"] == "ready":
                        break
                    time.sleep(0.02)
                self.assertEqual(outcome["week"], "2026-11-23")
                self.assertEqual(outcome["tracks"], [pick(102)])
                self.assertEqual(generate.call_count, 1)

            reopened = weekly.WeeklyDiscovery(Mock(), Mock(), today=lambda: today[0])
            self.assertEqual(reopened.status(cache, store, "room", catalog, events), outcome)
            self.assertIsNone(reopened.saved(store, "room", "2026-11-23"))
            self.assertIsNone(reopened.saved(cache, "elsewhere", "2026-11-23"))
            today[0] = date(2026, 11, 30)
            self.assertIsNone(reopened.saved(cache, "room", reopened.week()))

    def test_weekly_queue_is_lossy_idempotent_and_does_not_replace_manual_request(self):
        with TemporaryDirectory() as tmp:
            store = RequestsStore(Path(tmp) / "room.db")
            first, second = pick(101, "First"), pick(102, "Second")
            manual, _ = store.create_or_find(None, second["artist"], second["title"], "", 120, "", individual=True)
            self.assertEqual(store.queue_weekly([first, second, first]), 1)
            request = store.queued(5)[0]
            self.assertEqual(request["requested_format"], "any_lossy")
            self.assertTrue(request["format_confirmed"])
            self.assertEqual(store.get(manual["id"])["status"], "waiting_format")
            self.assertEqual(store.queue_weekly([first, second]), 0)
            store.finish(request["id"], "not_found", "", "")
            self.assertEqual(store.queue_weekly([first]), 1)
            self.assertEqual(store.get(request["id"])["requested_format"], "any_lossy")

    def test_weekly_playback_keeps_the_downloaded_tracks_in_playlist_order(self):
        refs = ["custom-2", "custom-1"]
        tracks = {ref: {"id": ref, "length": 120} for ref in refs}
        session = PlaybackSession(lambda _: [], tracks.get)
        result = session.apply({"op": "play_requests"}, mood_refs=refs)
        self.assertEqual(result["current_ref"], "custom-2")
        self.assertEqual([track["id"] for track in result["upcoming"]], ["custom-1"])
        self.assertEqual(session.apply({"op": "next"})["current_ref"], "custom-1")


class WeeklyHttpTests(ServerFixture, unittest.TestCase):
    def test_paired_client_only_queues_issued_picks_and_sees_lifecycle(self):
        new = pick(104)
        store = server.requests_store()
        week = server.weekly.week()
        store.cache_put(server.weekly._key(str(server.ROOT), week), json.dumps([new]), time.time())
        self.assertEqual(self.request(self.lan_port, "GET", weekly.PATH)[0], 401)
        cookie = self.lan_device_cookie("weekly-device")
        origin = self.lan_origin()
        headers = {"Cookie": cookie, "Origin": origin}

        def call(method, body=None):
            status, _, data = self.request(self.lan_port, method, weekly.PATH, body=body, headers=headers)
            return status, json.loads(data)

        with patch.object(server, "download_ready", return_value=True), patch.object(server, "start_worker") as start:
            self.assertEqual(call("POST", {"ids": [identity(777)]})[0], 409)
            self.assertEqual(call("POST", {"ids": [new["id"]]})[1], {"queued": 1, "existing": 0})
            self.assertEqual(call("POST", {"ids": [new["id"]]})[1], {"queued": 0, "existing": 1})
            status, result = call("GET")
            self.assertEqual(status, 200)
            request = result["tracks"][0]["request"]
            self.assertEqual(request["status"], "queued")
            self.assertEqual(store.get(request["id"])["requested_format"], "any_lossy")
            self.assertTrue(result["can_request"])
            start.assert_called_once_with("requests", force=True)
            audio = server.LIBRARY / "fixture.mp3"
            audio.parent.mkdir(parents=True, exist_ok=True)
            store.finish(request["id"], "downloaded", "", audio.name)
            self.assertEqual(call("GET")[1]["tracks"][0]["request"], {"id": request["id"], "status": "missing"})
            refs = {"op": "play_requests", "request_ids": [request["id"]]}
            self.assertEqual(server.selected_playback_tracks(refs), [])
            audio.touch()
            self.assertEqual(call("GET")[1]["tracks"][0]["request"],
                             {"id": request["id"], "status": "downloaded", "audio": f"/api/audio/custom/{request['id']}"})
            lossless, _ = store.create_or_find(None, "Artist", "Lossless", "", 120, "", individual=True)
            store.confirm_format(lossless["id"], "flac", 0)
            flac = audio.with_suffix(".flac")
            flac.touch()
            store.finish(lossless["id"], "downloaded", "", flac.name)
            refs["request_ids"].append(lossless["id"])
            self.assertEqual(server.selected_playback_tracks(refs), [f"custom-{request['id']}"])
