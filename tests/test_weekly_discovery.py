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
from downloads.requests_store import COPY_REVIEW_MESSAGE, RequestsStore
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
    def test_download_progress_counts_playable_copies_and_distinct_requests(self):
        ready = {"id": 1, "status": "downloaded", "audio": "/api/audio/custom/1"}
        requests = [ready, ready, {"id": 2, "status": "queued"}, {"id": 3, "status": "rejected"},
                    {"id": 4, "status": "downloaded"}, None]
        self.assertEqual(weekly.download_progress([{"request": request} for request in requests]),
                         {"total": 4, "downloaded": 1, "pending": 1, "needs_attention": 2, "percent": 25})

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
            alternate = song(300 + number, f"Discovery {number}", artist)
            alternate["artist-credit"][0]["joinphrase"] = " feat. "
            alternate["artist-credit"].append({"artist": {"id": identity(99), "name": "Guest"}})
            return {"recordings": [song(100 + number, f"Owned {number}", artist),
                                    song(200 + number, f"Discovery {number}", artist),
                                    alternate,
                                    song(400 + number, "Unknown duration", artist, seconds=0)]}
        meta.musicbrainz.side_effect = browse
        known = LibraryRecordings([{"artist": "Artist 1", "title": "Owned 1", "musicbrainz_id": identity(999)},
                                   {"artist": "Artist 2", "title": "Owned 2", "musicbrainz_id": identity(997)}])
        service = weekly.WeeklyDiscovery(meta, connections)
        result = service.generate(["Artist 1"], known, "2026-11-23")
        self.assertEqual({row["depth"] for row in result}, {0, 1, 2, 3})
        self.assertEqual(connections.lookup.call_count, 3)
        self.assertEqual(meta.musicbrainz.call_count, 4)
        self.assertEqual(len({row["title"] for row in result}), len(result))
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

    def test_weekly_queue_reserves_songs_with_featured_credit_across_requests(self):
        with TemporaryDirectory() as tmp:
            store = RequestsStore(Path(tmp) / "room.db")
            solo = pick(101, "+ famoso di Gesù", "Gianluca Grignani")
            featured = pick(102, "+ Famoso di Gesù", "Gianluca Grignani feat. Fabrizio Moro")
            cover = pick(103, "+ Famoso di Gesù", "Other Artist")
            self.assertEqual(store.queue_weekly([solo, featured, cover]), 2)
            self.assertEqual(store.queue_weekly([featured]), 0)
            self.assertEqual([row["musicbrainz_id"] for row in store.queued(5)], [solo["id"], cover["id"]])

    def test_existing_library_credit_matches_for_discovery_only(self):
        featured = pick(102, "+ Famoso di Gesù", "Gianluca Grignani feat. Fabrizio Moro")
        known = LibraryRecordings([pick(101, "+ famoso di Gesù", "Gianluca Grignani")])
        self.assertTrue(known.contains_song(featured))
        self.assertFalse(known.contains(featured))

    def test_missing_downloaded_alias_does_not_reserve_other_recording(self):
        with TemporaryDirectory() as tmp:
            store = RequestsStore(Path(tmp) / "room.db")
            solo = pick(101)
            featured = pick(102, artist="Fixture artist feat. Guest")
            existing, _ = store.create_or_find(None, featured["artist"], featured["title"], "", 120, "",
                                                individual=True, musicbrainz_id=featured["id"])
            store.finish(existing["id"], "downloaded", "", "missing.mp3")
            unavailable = lambda _path: False
            (choice,) = weekly.with_requests([solo], store, LibraryRecordings([]), unavailable)
            self.assertIsNone(choice["request"])
            self.assertFalse(choice["in_library"])
            self.assertEqual(store.queue_weekly([solo], audio_available=unavailable), 1)
            self.assertEqual(store.queued(1)[0]["musicbrainz_id"], solo["id"])

    def test_legacy_replacement_is_visible_from_featured_original_credit(self):
        with TemporaryDirectory() as tmp:
            store = RequestsStore(Path(tmp) / "room.db")
            original, _ = store.create_or_find(None, "Fixture artist", "New song", "", 120, "", individual=True)
            chosen = pick(109, "Chosen performance")
            store.replace_recording(original["id"], original["format_revision"], chosen["artist"], chosen["title"],
                                    "", 150, chosen["title"], musicbrainz_id=chosen["id"], version="Live", performance="")
            featured = pick(102, artist="Fixture artist feat. Guest")
            result = weekly.with_requests([chosen, featured], store, LibraryRecordings([chosen]), lambda _path: True)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["request"]["id"], original["id"])
            self.assertEqual(result[0]["request"]["title"], chosen["title"])
            self.assertFalse(result[0]["in_library"])
            self.assertEqual(store.queue_weekly([featured]), 0)
            store.finish(original["id"], "downloaded", "", "missing.mp3")
            (item,) = weekly.with_requests([featured], store, LibraryRecordings([]), lambda _path: False)
            self.assertIsNone(item["request"])

    def test_replacement_reserves_original_and_current_songs(self):
        with TemporaryDirectory() as tmp:
            store = RequestsStore(Path(tmp) / "room.db")
            original = pick(101, "Original")
            featured_original = pick(102, "Original", "Fixture artist feat. Guest")
            featured_replacement = pick(103, "Live jam", "Other artist feat. Guest")
            edition = [original, featured_original, featured_replacement]
            self.assertEqual(store.queue_weekly([original]), 1)
            row = store.queued(1)[0]
            store.replace_recording(row["id"], row["format_revision"], "Other artist", "Live jam",
                                    "", 120, "Live jam", musicbrainz_id=identity(109), version="", performance="")
            store.finish(row["id"], "not_found", "", "")
            self.assertEqual(store.queue_weekly(edition), 1)
            self.assertEqual([request["id"] for request in store.queued(4)], [row["id"]])
            store.finish(row["id"], "downloaded", "", "replacement.mp3")
            self.assertEqual(store.queue_weekly(edition, selected_ids={featured_original["id"]},
                                                audio_available=lambda _path: True), 0)

    def test_cached_song_resolves_to_explicit_replacement_or_alias_needing_review(self):
        with TemporaryDirectory() as tmp:
            store = RequestsStore(Path(tmp) / "room.db")
            solo = pick(101)
            featured = pick(102, artist="Fixture artist feat. Guest")
            store.queue_weekly([solo])
            direct = store.queued(1)[0]
            store.finish(direct["id"], "not_found", "", "")
            alternate, _ = store.create_or_find(None, featured["artist"], featured["title"], "", 120, "",
                                                 individual=True, musicbrainz_id=featured["id"])
            store.finish(alternate["id"], "rejected", COPY_REVIEW_MESSAGE, "")
            available = lambda _path: True
            resolved = weekly.with_requests([solo, featured], store, LibraryRecordings([]), available)
            self.assertEqual(len(resolved), 1)
            self.assertEqual((resolved[0]["request"]["id"], resolved[0]["request"]["request_state"]),
                             (alternate["id"], "choose_copy"))

            store.replace_recording(direct["id"], direct["format_revision"], solo["artist"], "Selected live edition",
                                    "", 150, "Selected live edition", musicbrainz_id=identity(109),
                                    version="Live", performance="")
            store.finish(alternate["id"], "downloaded", "", "featured.mp3")
            resolved = weekly.with_requests([solo, featured], store, LibraryRecordings([]), available)
            self.assertEqual(len(resolved), 1)
            self.assertEqual(resolved[0]["request"]["id"], direct["id"])
            self.assertEqual(resolved[0]["request"]["title"], "Selected live edition")
            self.assertFalse(resolved[0]["in_library"])

    def test_weekly_playback_keeps_the_downloaded_tracks_in_playlist_order(self):
        refs = ["custom-2", "custom-1"]
        tracks = {ref: {"id": ref, "length": 120} for ref in refs}
        session = PlaybackSession(lambda _: [], tracks.get)
        result = session.apply({"op": "play_requests"}, mood_refs=refs)
        self.assertEqual(result["current_ref"], "custom-2")
        self.assertEqual([track["id"] for track in result["upcoming"]], ["custom-1"])
        self.assertEqual(session.apply({"op": "next"})["current_ref"], "custom-1")


class WeeklyHttpTests(ServerFixture, unittest.TestCase):
    def test_cached_credit_variants_show_one_playable_song_and_do_not_queue_again(self):
        solo = pick(101, "+ famoso di Gesù", "Gianluca Grignani")
        featured = pick(102, "+ Famoso di Gesù", "Gianluca Grignani feat. Fabrizio Moro")
        store = server.requests_store()
        week = server.weekly.week()
        store.cache_put(server.weekly._key(str(server.ROOT), week), json.dumps([featured, solo]), time.time())
        store.queue_weekly([featured])
        duplicate = store.queued(1)[0]
        ready, _ = store.create_or_find(None, solo["artist"], solo["title"], "", 120, "",
                                        individual=True, musicbrainz_id=solo["id"])
        audio = server.LIBRARY / "weekly-credits-fixture.mp3"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.touch()
        store.finish(ready["id"], "downloaded", "", audio.name)
        cookie = self.lan_device_cookie("weekly-credits")
        headers = {"Cookie": cookie, "Origin": self.lan_origin()}

        with patch.object(server, "download_ready", return_value=True), patch.object(server, "start_worker") as start:
            status, _, data = self.request(self.lan_port, "GET", weekly.PATH, headers=headers)
            self.assertEqual(status, 200)
            result = json.loads(data)
            self.assertEqual(len(result["tracks"]), 1)
            (song,) = result["tracks"]
            self.assertTrue(song["in_library"])
            self.assertEqual(song["request"]["id"], ready["id"])
            self.assertEqual(song["request"]["audio"], f"/api/audio/custom/{ready['id']}")
            self.assertEqual(result["download_progress"]["downloaded"], 1)

            status, _, data = self.request(self.lan_port, "POST", weekly.PATH,
                                           body={"ids": [featured["id"], solo["id"]]}, headers=headers)
            self.assertEqual((status, json.loads(data)), (200, {"queued": 0, "existing": 2}))
            start.assert_not_called()
            self.assertEqual([row["id"] for row in store.queued(5)], [duplicate["id"]])

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
            alternate = identity(105)
            store.replace_recording(
                request["id"], request["format_revision"], new["artist"], new["title"], "Live album",
                125, new["title"], musicbrainz_id=alternate, version="Live", performance="")
            changed = call("GET")[1]["tracks"][0]["request"]
            self.assertEqual((changed["id"], changed["musicbrainz_id"], changed["requested_format"], changed["version"]),
                             (request["id"], alternate, "any_lossy", "Live"))
            self.assertEqual(call("POST", {"ids": [new["id"]]})[1], {"queued": 0, "existing": 1})
            audio = server.LIBRARY / "fixture.mp3"
            audio.parent.mkdir(parents=True, exist_ok=True)
            store.finish(request["id"], "downloaded", "", audio.name)
            missing = call("GET")[1]
            unavailable = missing["tracks"][0]["request"]
            self.assertEqual((unavailable["id"], unavailable["status"], unavailable["request_state"]),
                             (request["id"], "missing", "copy_missing"))
            self.assertNotIn("audio", unavailable)
            self.assertEqual(missing["download_progress"],
                             {"total": 1, "downloaded": 0, "pending": 0, "needs_attention": 1, "percent": 0})
            refs = {"op": "play_requests", "request_ids": [request["id"]]}
            self.assertEqual(server.selected_playback_tracks(refs), [])
            audio.touch()
            playable = call("GET")[1]["tracks"][0]["request"]
            self.assertEqual((playable["id"], playable["status"], playable["request_state"], playable["audio"]),
                             (request["id"], "downloaded", "ready", f"/api/audio/custom/{request['id']}"))
            lossless, _ = store.create_or_find(None, "Artist", "Lossless", "", 120, "", individual=True)
            store.confirm_format(lossless["id"], "flac", 0)
            flac = audio.with_suffix(".flac")
            flac.touch()
            store.finish(lossless["id"], "downloaded", "", flac.name)
            refs["request_ids"].append(lossless["id"])
            self.assertEqual(server.selected_playback_tracks(refs), [f"custom-{request['id']}", f"custom-{lossless['id']}"])
