"""Household profiles through the real fixture room HTTP server."""

import base64
import hashlib
import http.client
import json
import unittest
import xml.etree.ElementTree as ET
from tempfile import TemporaryDirectory
from contextlib import closing
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from fixture_environment import isolate_room_environment as setUpModule
from test_http_server import ServerFixture, server
from catalog.catalog_store import CatalogStore
from catalog.audio_store import MediaStore
from catalog.collection_snapshot import create_empty_collection
from playback.member_profiles import MemberProfiles
from system.owner_auth import OwnerAuth


PASSWORD = "Local fixture password"
FLAC_FIXTURE = base64.b64decode(
    "ZkxhQwAAACIDwAPAAAAAAAeVBdwA8AAAAAAAAAAAAAAAAAAAAAAAAAAAhAAADgYAAABmZm1wZWcAAAAA//h3CAADvzgAAAC/eg=="
)


class HouseholdProfileTests(ServerFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.auth = OwnerAuth(cls.config_path.parent)
        cls.auth.set_password("owner", PASSWORD)
        store = CatalogStore(server.DB)
        records = [{"index": index, "key": f"track-{index}", "uri": "", "artist": "Fixture artist",
                    "title": f"Track {index}", "album": "Fixture album", "length": 1,
                    "isrc": "", "first_playlist": "spotify_liked" if index in (5, 6, 8, 9) else "mix", "active": index != 4}
                   for index in range(1, 10)]
        store.write_build({"records": records,
                           "playlists": {"mix": [1, 2, 3, 7], "spotify_liked": [2, 5, 6, 7], "linked-private": [8]},
                           "issues": [], "covers": {},
                           "list_names": {"mix": "Room mix", "spotify_liked": "Owner likes", "linked-private": "Owner links"}})
        Path(server.LIBRARY).mkdir(parents=True, exist_ok=True)
        (Path(server.LIBRARY) / "fixture.flac").write_bytes(FLAC_FIXTURE)
        store.write_coverage([{**record, "status": "verified", "path": "fixture.flac", "reason": "owner diagnostic"}
                              for record in records if record["index"] not in (5, 7, 8, 9)])
        server.catalog.reload()

    @property
    def headers(self):
        return {"Origin": f"http://127.0.0.1:{self.loop_port}"}

    def call(self, method, route, cookie="", body=None, **headers):
        return self.request(self.loop_port, method, route, body=body,
                            headers={**self.headers, "Cookie": cookie, **headers})

    def sign_in(self, username):
        status, headers, body = self.call("POST", "/api/account/login", body={"username": username, "password": PASSWORD})
        self.assertEqual(status, 200, body)
        return headers["Set-Cookie"].partition(";")[0], json.loads(body)

    def result(self, route, cookie):
        status, _, body = self.call("GET", route, cookie)
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def ids(self, cookie, filter_name):
        return [entry["id"] for entry in self.result(f"/api/tracks?filter={filter_name}", cookie)["tracks"]]

    def play(self, cookie, track):
        status, _, body = self.call("POST", "/api/session", cookie, {"op": "play", "track_id": track})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["current_ref"], track)

    def test_appearance_is_private_to_the_signed_in_account(self):
        alice = self.auth.household.add_member("appearance-alice", PASSWORD)
        self.auth.household.add_member("appearance-bob", PASSWORD)
        first, _ = self.sign_in(alice.username)
        owner, _ = self.sign_in("owner")
        other, _ = self.sign_in("appearance-bob")
        path = "/api/my/appearance"
        self.assertEqual(self.result(path, first), {"theme": "sea-glass"})
        status, _, response = self.call("POST", path, first, {"theme": "night"})
        self.assertEqual((status, json.loads(response)), (200, {"theme": "night"}))
        second, _ = self.sign_in(alice.username)
        self.assertEqual(self.result(path, second), {"theme": "night"})
        self.assertEqual(self.result(path, other), {"theme": "sea-glass"})
        self.assertEqual(self.result(path, owner), {"theme": "sea-glass"})
        self.assertEqual(self.call("POST", path, owner, {"theme": "paper"})[0], 200)
        self.assertEqual(self.result(path, owner), {"theme": "paper"})
        self.assertEqual(self.call("POST", path, other, {"theme": "custom"})[0], 400)
        self.assertEqual(self.call("POST", path, other, {"theme": []})[0], 400)
        self.assertEqual(self.call("POST", path, other, {"theme": "paper"}, **{"X-Listening-Account": alice.id})[0], 409)
        self.assertEqual(self.result(path, other), {"theme": "sea-glass"})
        self.assertIn(self.call("GET", path)[0], (401, 403))

    def test_favorites_queues_and_histories_are_private_and_durable(self):
        alice = self.auth.household.add_member("Alice", PASSWORD)
        bob = self.auth.household.add_member("Bob", PASSWORD)
        owner_cookie, owner = self.sign_in("owner")
        alice_cookie, alice_details = self.sign_in("alice")
        bob_cookie, bob_details = self.sign_in("bob")
        self.assertEqual((owner["role"], alice_details["id"], bob_details["id"]), ("owner", alice.id, bob.id))
        self.assertEqual(self.result("/api/account", alice_cookie)["role"], "member")

        for cookie, track in ((owner_cookie, 1), (alice_cookie, 2), (bob_cookie, 3)):
            response = self.call("POST", "/api/favorites", cookie,
                                 {"index": track, "favorite": True, "member_id": alice.id})
            self.assertEqual(response[0], 200, response[2])
            self.play(cookie, track)

        for cookie, track in ((owner_cookie, 1), (alice_cookie, 2), (bob_cookie, 3)):
            self.assertEqual(self.ids(cookie, "favorites"), [track])
            self.assertEqual(self.ids(cookie, "recent"), [track])
            overview = self.result("/api/overview", cookie)
            self.assertEqual((overview["favorites"], overview["recently_played"][0]["track"]["id"]), (1, track))
            session = self.result("/api/session", cookie)
            self.assertEqual(session["current_ref"], track)
            self.assertEqual([entry["id"] for entry in session["upcoming"]], [*range(track + 1, 4), 6])

        with mock.patch.object(server.metadata, "track_metadata", return_value={"title": "Fixture"}):
            alice_own = self.result("/api/metadata/2", alice_cookie)
            alice_other = self.result("/api/metadata/3", alice_cookie)
            self.assertEqual((alice_own["favorite"], alice_own["play_count"]), (True, 1))
            self.assertEqual((alice_other["favorite"], alice_other["play_count"]), (False, 0))

        completed = self.call("POST", "/api/session", alice_cookie, {"op": "next", "expected_ref": 2})
        self.assertEqual(completed[0], 200, completed[2])
        self.assertEqual(self.ids(alice_cookie, "most"), [2])
        self.assertEqual(self.ids(bob_cookie, "most"), [])

        reopened = MemberProfiles(server.member_profiles.database, server.ROOT, server.member_collection_identity(server.ROOT))
        with mock.patch.object(server, "member_profiles", reopened):
            alice_state = self.result("/api/session", alice_cookie)
            self.assertEqual((alice_state["current_ref"], alice_state["playing"]), (3, False))
            self.assertEqual(self.ids(alice_cookie, "favorites"), [2])
            self.assertEqual(self.ids(bob_cookie, "recent"), [3])
            self.assertEqual(self.result("/api/session", owner_cookie)["current_ref"], 1)

    def test_radio_resume_is_private_durable_and_stale_devices_cannot_clear_it(self):
        path = "/api/my/radio"
        self.auth.household.add_member("radio-owner", PASSWORD)
        self.auth.household.add_member("radio-guest", PASSWORD)
        browser, account = self.sign_in("radio-owner")
        tablet, _ = self.sign_in("radio-owner")
        guest, _ = self.sign_in("radio-guest")
        owner, _ = self.sign_in("owner")
        token = server.device_store.issue("radio-paired-device", "Paired only")
        paired = f"lr_token={token}"
        self.assertEqual(self.call("GET", path, paired)[0], 403)
        self.assertEqual(self.call("POST", path, paired, {})[0], 403)
        self.assertEqual(self.call("GET", path, guest, **{"X-Listening-Account": account["id"]})[0], 409)
        self.assertIsNone(self.result(path, browser)["history"])
        revision = self.result(path, browser)["revision"]
        current_queue = self.result("/api/session", browser)["current_ref"]
        ambient = {"source": {"kind": "live", "name": "Ambient",
                              "stream": {"url": "https://example.invalid"}}, "title": "Drone Zone",
                   "channel": "SomaFM", "current": None}
        noise = {"source": {"kind": "noise", "name": "Brown noise", "profile": "brown"},
                 "title": "Brown noise", "channel": "Generated on this device", "current": None}

        def write(cookie, op, tuning, history=None, expected_revision=None):
            return self.call("POST", path, cookie, {"op": op, "tuning": tuning, "history": history,
                                                    "expected_revision": expected_revision})[0]

        def retry(cookie, tuning, history, expected_revision):
            status, _, payload = self.call("POST", path, cookie, {"op": "tune", "tuning": tuning,
                "history": history, "expected_revision": expected_revision})
            self.assertEqual(status, 200, payload)
            return json.loads(payload)["accepted"]

        self.assertTrue(retry(browser, "a" * 32, ambient, revision))
        self.assertTrue(retry(browser, "a" * 32, ambient, revision))  # Its first response might have been lost.
        selection = self.result(path, tablet)["history"]
        self.assertEqual(selection["title"], "Drone Zone")
        self.assertEqual(selection["source"]["stream"]["url"], "https://ice2.somafm.com/dronezone-128-mp3")
        with mock.patch.dict(server.SETTINGS, {"somafm_stations": [{"name": "Ambient", "channel": "lush"}]}):
            selection = self.result(path, tablet)["history"]
            self.assertEqual(selection["title"], "Lush")
            self.assertEqual(selection["source"]["stream"]["url"], "https://ice2.somafm.com/lush-128-mp3")
        with mock.patch.dict(server.SETTINGS, {"somafm_stations": []}):
            self.assertIsNone(self.result(path, tablet)["history"])
        self.assertEqual(write(tablet, "tune", "b" * 32, noise), 200)
        self.assertEqual(write(browser, "update", "a" * 32, ambient), 200)
        self.assertEqual(write(browser, "clear", "a" * 32), 200)
        self.assertFalse(retry(browser, "a" * 32, ambient, revision))
        self.assertEqual(write(browser, "tune", "c" * 32, {**ambient, "current": 42}), 400)
        self.assertIsNone(self.result(path, guest)["history"])
        self.assertIsNone(self.result(path, owner)["history"])
        self.assertEqual(write(owner, "tune", "d" * 32, ambient), 200)
        self.assertEqual(self.result(path, owner)["history"]["title"], "Drone Zone")
        self.assertEqual(self.result("/api/session", tablet)["current_ref"], current_queue)

        reopened = MemberProfiles(server.member_profiles.database, server.ROOT, server.member_collection_identity(server.ROOT))
        with mock.patch.object(server, "member_profiles", reopened):
            restored = self.result(path, browser)["history"]
            self.assertGreater(restored.pop("saved_at"), 0)
            self.assertEqual(restored, {**noise, "tuning": "b" * 32})
            self.assertEqual(write(tablet, "clear", "b" * 32), 200)
            self.assertFalse(retry(tablet, "b" * 32, noise, revision))
            self.assertIsNone(self.result(path, browser)["history"])

    def test_radio_resume_keeps_the_newest_clock_sample_for_the_active_tuning(self):
        path = "/api/my/radio"
        self.auth.household.add_member("radio-position", PASSWORD)
        browser, _ = self.sign_in("radio-position")
        tablet, _ = self.sign_in("radio-position")
        track = {"source": {"kind": "library", "name": "electronic"}, "title": "Track 1",
                 "channel": "Fixture artist", "current": 1}
        video = {"source": {"kind": "youtube", "name": "House", "seeds": ["vugodYOKeis"]},
                 "current": "vugodYOKeis", "title": "House set", "channel": "Fixture artist"}

        def write(op, history, **clock):
            return self.call("POST", path, browser, {
                "op": op, "tuning": tuning, "history": {**history, **clock},
            })[0]

        for index, item in enumerate([track, video], start=1):
            with self.subTest(kind=item["source"]["kind"]):
                tuning = str(index) * 32
                self.assertEqual(write("tune", item, sample=1, position=1.5), 200)
                self.assertEqual(write("update", item, sample=3, position=1200.125), 200)
                self.assertEqual(write("update", item, sample=2, position=6.25), 200)
                self.assertEqual(write("update", item, sample=4, position=-1), 400)
                history = self.result(path, tablet)["history"]
                self.assertEqual((history["current"], history["position"]), (item["current"], 1200.125))
                self.assertEqual(write("update", item, sample=5, position=2.0), 200)
                self.assertEqual(self.result(path, tablet)["history"]["position"], 2.0)
        self.assertEqual(write("update", video), 200)
        self.assertEqual(self.result(path, tablet)["history"]["position"], 2.0)
        tuning = "a" * 32
        self.assertEqual(write("tune", video), 200)
        self.assertEqual(write("update", {**video, "title": "Another set"}), 200)
        self.assertEqual(self.result(path, tablet)["history"]["title"], "Another set")

    def test_jams_require_explicit_nearby_join_and_leave_private_queues_alone(self):
        self.auth.household.add_member("Gabby", PASSWORD)
        self.auth.household.add_member("friend", PASSWORD)
        host, _ = self.sign_in("gabby")
        guest, _ = self.sign_in("friend")
        clock = [100.0]
        host_output = "host-output"
        guest_output = "guest-output"

        def request(cookie, output, path="/api/jams", body=None, **headers):
            status, _, payload = self.call("GET" if body is None else "POST", path, cookie, body,
                **{"X-Listening-Output": output, "X-Listening-Output-Epoch": "100", **headers})
            return status, json.loads(payload)

        def action(cookie, output, op, **fields):
            status, result = request(cookie, output, body={"op": op, **fields})
            self.assertEqual(status, 200, result)
            return result

        with mock.patch.object(server, "jams", server.JamDirectory(lambda: clock[0])):
            self.play(guest, 3)
            self.assertEqual(request(guest, guest_output)[1]["people"], [])
            request(host, host_output, "/api/session", {"op": "play", "track_id": 2})
            self.assertEqual(request(host, host_output, body={"op": "start", "listening": True})[0], 400)
            action(host, host_output, "share", listening=True)
            action(host, host_output, "start", listening=True)
            action(host, "another-host-tab", "share")
            _, nearby = request(guest, guest_output)
            self.assertEqual([(p["name"], p["song"]["title"]) for p in nearby["people"]], [("Gabby", "Track 2")])
            invite, = nearby["invites"]
            details = f"/api/jams/session?id={invite['id']}"
            self.assertEqual(request(guest, guest_output, details)[0], 410)
            self.assertFalse(request(guest, guest_output, **{"X-Forwarded-For": "192.168.2.10"})[1]["available"])
            with mock.patch.object(server, "nearby_network", return_value="192.168.8.0/24"):
                self.assertEqual(request(guest, guest_output, body={"op": "join", "id": invite["id"]})[0], 410)
                self.assertEqual(request(guest, guest_output)[1]["people"], [])

            self.call("POST", "/api/favorites", guest, {"index": 2, "favorite": True})
            joined = action(guest, guest_output, "join", id=invite["id"])
            self.assertTrue(joined["current"]["favorite"])
            self.assertEqual((joined["current_ref"], joined["playing"], joined["upcoming"]), (2, True, []))
            self.assertEqual(joined["current"]["playlists"], ["mix"])
            self.assertEqual((joined["current"]["reason"], joined["current"]["spotify_url"]), ("", ""))
            self.assertEqual(self.result("/api/session", guest)["current_ref"], 3)
            self.assertEqual(request(guest, "unknown-output", details)[0], 410)
            request(host, host_output, "/api/session", {"op": "seek", "position": 12})
            self.assertGreaterEqual(request(guest, guest_output, details)[1]["position"], 12)
            # Local Radio can silence the library while its server clock still runs.
            action(host, host_output, "pulse", listening=False)
            self.assertTrue(request(host, host_output, "/api/session")[1]["playing"])
            _, nearby = request(guest, guest_output)
            self.assertFalse(nearby["people"][0]["playing"])
            self.assertEqual(nearby["people"][0]["song"]["title"], "Track 2")
            idle = request(guest, guest_output, details)[1]
            self.assertEqual((idle["playing"], idle["session_playing"], idle["current"]), (False, False, None))
            action(host, host_output, "pulse", listening=True)
            resumed = request(guest, guest_output, details)[1]
            self.assertTrue(resumed["playing"])
            self.assertEqual(resumed["current"]["playlists"], ["mix"])
            self.assertTrue(resumed["current"]["favorite"])
            action(host, host_output, "hide")
            self.assertEqual(request(guest, guest_output, details)[0], 410)
            clock[0] += 36
            self.assertEqual(request(guest, guest_output)[1]["people"], [])

    def test_member_permissions_expose_only_shared_catalog_and_profile(self):
        self.auth.household.add_member("listener", PASSWORD)
        cookie, _ = self.sign_in("listener")
        self.assertIn(self.result("/api/room", cookie)["state"], {"ready", "catalog_loading"})
        self.assertEqual(self.call("POST", "/api/device", cookie, {"id": "unrelated-device", "name": "False device"})[0], 200)
        self.assertIsNone(server.device_store.get("unrelated-device"))
        overview = self.result("/api/overview", cookie)
        self.assertEqual([playlist["id"] for playlist in overview["playlists"]], ["mix"])
        self.assertEqual((overview["total"], overview["summary"]["pending"]), (5, 1))
        self.assertEqual(self.result("/api/settings", cookie)["lan_url"], "")
        self.assertEqual(self.result("/api/activity", cookie)["queued_requests"], 0)
        self.assertEqual(self.ids(cookie, "all"), [1, 2, 3, 6, 7])
        self.assertEqual(self.result("/api/tracks?q=track+2", cookie)["tracks"][0]["playlists"], ["mix"])
        self.assertEqual(self.result("/api/tracks?q=track+2", cookie)["tracks"][0]["reason"], "")
        self.assertEqual(self.result("/api/tracks?q=track+5", cookie)["tracks"], [])
        self.assertEqual(self.result("/api/tracks?q=track+6", cookie)["tracks"][0]["playlists"], [])
        for track in (5, 8, 9):
            with self.subTest(track=track):
                self.assertEqual(self.result(f"/api/tracks?q=track+{track}", cookie)["tracks"], [])
                self.assertEqual(self.call("GET", f"/api/metadata/{track}", cookie)[0], 404)
                self.assertEqual(self.call("GET", f"/api/lyrics/{track}", cookie)[0], 404)
                self.assertEqual(self.call("POST", "/api/favorites", cookie, {"index": track, "favorite": True})[0], 400)
        self.assertEqual(self.call("GET", "/api/tracks?playlist=spotify_liked", cookie)[0], 400)
        self.assertEqual(self.call("GET", "/api/tracks?playlist=linked-private", cookie)[0], 400)
        self.assertEqual(self.call("GET", "/api/tracks?spotify=1", cookie)[0], 403)
        self.assertEqual(self.call("GET", "/api/tracks?filter=requests", cookie)[0], 403)
        status, _, payload = self.call("GET", "/api/audio/1", cookie, Range="bytes=0-3")
        self.assertEqual((status, payload), (206, b"fLaC"))
        self.assertEqual(self.call("GET", "/api/audio/4", cookie)[0], 404)
        self.assertEqual(self.call("POST", "/api/session", cookie, {"op": "play_request", "request_id": 1})[0], 400)
        self.assertEqual(self.call("POST", "/api/session", cookie, {"op": "play_available", "playlist": "spotify_liked"})[0], 400)

        reads = ("/api/announcements?device_id=paired-device-1", "/api/browse", "/api/replica", "/api/requests",
                 "/api/audio/custom/1", "/api/spotify/status", "/api/devices", "/api/jobs",
                 "/api/export", "/api/activity/explore", "/api/playlists/share", "/api/podcasts", "/api/audiobooks", "/api/my/progress")
        self.assertIn("Study", self.result("/api/stations", cookie)["presets"])
        writes = ("/api/pairing/rotate", "/api/settings", "/api/requests", "/api/requests/format", "/api/stations",
                  "/api/replica/original", "/api/announcements", "/api/spotify/connect", "/api/sync",
                  "/api/podcasts/delete", "/api/export")
        for route in reads:
            with self.subTest(route=route):
                self.assertEqual(self.call("GET", route, cookie)[0], 403)
        for route in writes:
            with self.subTest(route=route):
                self.assertEqual(self.call("POST", route, cookie, {})[0], 403)

    def test_open_subsonic_key_only_exposes_shared_verified_music(self):
        identifier, key = self.auth.create_stream_key("Fixture OpenSubsonic client")

        def call(method, use_key=key, **params):
            query = {"f": "json", "v": "1.16.1", "c": "fixture", **params}
            if use_key is not None:
                query["apiKey"] = use_key
            return self.call("GET", f"/rest/{method}.view?{urlencode(query)}")

        def data(method, use_key=key, **params):
            status, _, payload = call(method, use_key, **params)
            self.assertEqual(status, 200, payload)
            return json.loads(payload)["subsonic-response"]

        self.addCleanup(self.auth.revoke_stream_key, identifier)
        self.assertEqual(data("getOpenSubsonicExtensions", None)["openSubsonicExtensions"],
                         [{"name": "apiKeyAuthentication", "versions": [1]}])
        self.assertEqual(data("ping")["status"], "ok")
        self.assertEqual(data("ping", None)["error"]["code"], 44)
        self.assertEqual(data("ping", u="owner")["error"]["code"], 43)
        self.assertEqual(data("ping", None, u="owner", p=PASSWORD)["error"]["code"], 42)
        self.assertEqual(data("createPlaylist", name="Should fail")["error"]["code"], 50)
        playlists = data("getPlaylists")["playlists"]["playlist"]
        self.assertEqual(len(playlists), 1)
        self.assertEqual(playlists[0]["name"], "Room mix")
        self.assertTrue(playlists[0]["readonly"])
        self.assertIn("created", playlists[0])
        self.assertIn("changed", playlists[0])
        self.assertIn("ignoredArticles", data("getArtists")["artists"])
        self.assertEqual(data("getSong", id="5")["error"]["code"], 70)
        status, headers, missing = call("stream", id="5")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/xml"))
        self.assertEqual(ET.fromstring(missing).find("{http://subsonic.org/restapi}error").get("code"), "70")
        matches = data("search3", query="fixture album")["searchResult3"]["song"]
        self.assertEqual([song["id"] for song in matches], ["1", "2", "3", "6"])
        _, _, xml = call("getArtist", id=matches[0]["artistId"], f="xml")
        artist = ET.fromstring(xml).find("{http://subsonic.org/restapi}artist")
        self.assertEqual(artist.get("name"), "Fixture artist")
        self.assertIsNotNone(artist.find("{http://subsonic.org/restapi}album").get("created"))
        track_id = matches[0]["id"]
        url = "/rest/stream?" + urlencode({"apiKey": key, "id": track_id})
        status, headers, payload = self.call("GET", url, Range="bytes=0-3")
        self.assertEqual((status, payload), (206, FLAC_FIXTURE[:4]))
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(self.call("GET", "/api/my/playlists?" + urlencode({"apiKey": key}))[0], 401)
        self.assertEqual(self.call("POST", "/rest/createPlaylist?" + urlencode({"apiKey": key}), body={})[0], 401)
        self.assertTrue(self.auth.revoke_stream_key(identifier))
        self.assertEqual(data("ping")["error"]["code"], 44)

    def test_private_playlist_edits_and_queue_are_scoped_for_members_and_owner(self):
        self.auth.household.add_member("playlist-alice", PASSWORD)
        self.auth.household.add_member("playlist-bob", PASSWORD)
        alice, _ = self.sign_in("playlist-alice")
        bob, _ = self.sign_in("playlist-bob")
        owner, _ = self.sign_in("owner")
        paired = self.lan_device_cookie("private-playlist-paired")
        path = "/api/my/playlists"

        def edit(cookie, **body):
            status, _, payload = self.call("POST", path, cookie, body)
            self.assertEqual(status, 200, payload)
            return json.loads(payload)

        playlist = edit(alice, op="create", name="Afternoon")
        identity = playlist["id"]
        self.assertEqual(self.result(path, bob)["playlists"], [])
        self.assertEqual(self.result(path, owner)["playlists"], [])
        self.assertEqual(self.call("GET", path + f"?id={identity}", bob)[0], 404)
        self.assertEqual(self.call("POST", path, bob, {"op": "delete", "id": identity, "revision": 1})[0], 404)
        for index in (1, 2):
            playlist = edit(alice, op="add", id=identity, revision=playlist["revision"], track_id=index)
        previous = playlist["revision"]
        playlist = edit(alice, op="move", id=identity, revision=previous, position=1, target=0)
        stale = self.call("POST", path, alice, {"op": "rename", "id": identity, "revision": previous, "name": "Lost"})
        self.assertEqual(stale[0], 409)
        self.assertEqual([song["id"] for song in playlist["tracks"]], [2, 1])
        for listener in (alice, owner):
            status, _, payload = self.call("POST", "/api/my/session", listener,
                                           {"op": "play_mood", "mood": "focused", "genre": "minimal"})
            self.assertEqual((status, json.loads(payload)["code"]), (400, "empty_mood"))
        self.assertEqual(self.call("POST", "/api/my/session", owner, {"op": []})[0], 400)
        shared_history = self.result("/api/overview", paired)["recently_played"]
        for cookie, key in ((alice, identity), (owner, edit(owner, op="create", name="Owner private")["id"])):
            if cookie == owner:
                edit(owner, op="add", id=key, revision=1, track_id=3)
            before = self.result("/api/session", bob if cookie == alice else owner)
            status, _, payload = self.call("POST", "/api/my/session", cookie,
                                           {"op": "play_available", "playlist": f"private:{key}"})
            self.assertEqual(status, 200, payload)
            self.assertEqual(json.loads(payload)["current_ref"], 2 if cookie == alice else 3)
            after = self.result("/api/session", bob if cookie == alice else owner)
            self.assertEqual(after["current_ref"], before["current_ref"])
        self.assertEqual(self.result("/api/overview", paired)["recently_played"], shared_history)
        self.assertEqual(self.call("POST", "/api/session", bob,
                                   {"op": "play_available", "playlist": f"private:{identity}"})[0], 400)
        edit(alice, op="delete", id=identity, revision=playlist["revision"])
        self.assertEqual(self.result(path, alice)["playlists"], [])

    def test_private_metadata_waits_for_a_visible_recording_then_keeps_its_identity(self):
        account = self.auth.household.add_member("personal-drafts", PASSWORD)
        cookie, _ = self.sign_in("personal-drafts")
        path = "/api/my/playlists"
        _, _, result = self.call("POST", path, cookie, {"op": "create", "name": "For later"})
        playlist = json.loads(result)
        song = {"artist": "Fixture artist", "title": "Vattenne"}
        status, _, result = self.call("POST", path, cookie, {
            "op": "add_metadata", "id": playlist["id"], "revision": playlist["revision"], **song})
        self.assertEqual(status, 200, result)
        self.assertEqual(json.loads(result)["tracks"][0]["title"], "Vattenne")
        self.assertIsNone(json.loads(result)["tracks"][0]["id"])
        detail = path + "?id=" + playlist["id"]
        first, second = server.catalog.records[:2]
        with mock.patch.dict(first, song):
            self.assertEqual(self.result(detail, cookie)["tracks"][0]["id"], 1)
            with mock.patch.dict(second, song):
                self.assertEqual(self.result(detail, cookie)["tracks"][0]["id"], 1)
        self.assertEqual(server.member_profiles.profile(account.id).playlist(playlist["id"])["tracks"],
                         [first["key"]])

    def test_stale_member_cookie_cannot_fall_back_to_a_paired_or_owner_session(self):
        account = self.auth.household.add_member("old-browser", PASSWORD)
        paired = self.lan_device_cookie("old-browser-pairing")
        owner_cookie, _ = self.sign_in("owner")
        with closing(http.client.HTTPConnection("127.0.0.1", self.loop_port, timeout=10)) as connection:
            body = json.dumps({"username": account.username, "password": PASSWORD})
            connection.request("POST", "/api/account/login", body=body,
                               headers={**self.headers, "Content-Type": "application/json", "Cookie": f"{paired}; {owner_cookie}"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200, response.read())
            cookies = response.headers.get_all("Set-Cookie")
        expired = {cookie.partition("=")[0] for cookie in cookies if "Max-Age=0" in cookie}
        self.assertEqual(expired, {"lr_owner", "lr_token"})
        member_cookie = next(cookie.partition(";")[0] for cookie in cookies if cookie.startswith("lr_member="))
        self.assertEqual(self.result("/api/account", f"{member_cookie}; {paired}")["role"], "member")
        self.assertEqual(self.call("GET", "/api/settings", owner_cookie)[0], 401)

        self.auth.household.set_enabled(account.id, False)
        other_owner, _ = self.sign_in("owner")
        self.assertEqual(self.call("GET", "/api/overview", f"{member_cookie}; {paired}; {other_owner}")[0], 401)
        self.assertEqual(self.call("GET", "/api/overview", paired)[0], 200)

    def test_sign_out_revokes_only_the_member_session_in_that_browser(self):
        self.auth.household.add_member("member-logout", PASSWORD)
        first, _ = self.sign_in("member-logout")
        other, _ = self.sign_in("member-logout")
        self.assertEqual(self.call("POST", "/api/account/logout", first, {})[0], 200)
        self.assertEqual(self.call("GET", "/api/session", first)[0], 401)
        self.assertEqual(self.result("/api/account", other)["role"], "member")

    def test_stale_tab_cannot_read_or_mutate_another_account(self):
        first = self.auth.household.add_member("former-tab", PASSWORD)
        current = self.auth.household.add_member("current-tab", PASSWORD)
        cookie, _ = self.sign_in(current.username)
        stale = {"X-Listening-Account": first.id}
        for method, path, body in (("GET", "/api/session", None),
                                   ("POST", "/api/session", {"op": "play", "track_id": 1}),
                                   ("POST", "/api/favorites", {"index": 1, "favorite": True}),
                                   ("POST", "/api/account/logout", {})):
            with self.subTest(path=path):
                status, _, payload = self.call(method, path, cookie, body, **stale)
                self.assertEqual((status, json.loads(payload)["code"]), (409, "account_changed"))
        self.assertEqual(self.result("/api/session", cookie)["current_ref"], None)
        self.assertEqual(self.ids(cookie, "favorites"), [])
        status, _, payload = self.call("GET", "/api/account", cookie, **stale)
        self.assertEqual((status, json.loads(payload)["id"]), (200, current.id))
        self.assertEqual(self.call("GET", "/api/session", cookie, **{"X-Listening-Account": current.id})[0], 200)
        paired = self.lan_device_cookie("account-tab-device")
        self.assertEqual(self.call("GET", "/api/session", paired, **{"X-Listening-Account": "paired"})[0], 200)
        self.assertEqual(self.call("GET", "/api/session", paired, **{"X-Listening-Account": "owner"})[0], 409)
        self.assertEqual(self.call("GET", "/api/settings", **{"X-Listening-Account": "owner"})[0], 401)

    def test_portable_profile_scope_survives_a_collection_move(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            original = base / "original"
            original.mkdir()
            (original / "collection.sqlite3").touch()
            identity = server.member_collection_identity(original)
            favorite = "record-key"
            member = "fixture-member"
            profiles = MemberProfiles(base / "host.sqlite3", original, identity)
            profiles.profile(member).set_favorite(favorite, True, ())
            profiles.profile(member).save({"current": 2})

            moved = original.rename(base / "moved")
            profiles.select_room(moved, server.member_collection_identity(moved))
            self.assertEqual(profiles.profile(member).keys(), {favorite})
            self.assertEqual(profiles.profile(member).load(), {"current": 2})
            profiles.select_room(original, server.member_collection_identity(original))
            self.assertEqual(profiles.profile(member).keys(), set())
