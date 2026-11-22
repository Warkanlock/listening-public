import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import contextlib
import http.client
import io
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from fixture_environment import isolate_room_environment as setUpModule
from runner_fixture import runner
import server
from catalog.catalog_store import CatalogStore
from library.library_protocol import read_list, track_key, write_xspf
from library.local_sources import mirror_key, sync_files


def track(title, artist, album="", identifier=""):
    return {"title": title, "artist": artist, "album": album,
            "duration_ms": 180000, "identifier": identifier,
            "isrc": "", "image": "", "location": ""}


class Client:
    """A minimal JSON client in the plan 007 harness pattern, with
    cookie capture and an Origin for presented (non-loopback) calls."""

    def __init__(self, port):
        self.port = port
        self.cookie = ""
        self.origin = ""

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if self.origin:
            headers["Origin"] = self.origin
        connection.request(method, path,
                           json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        payload = response.read()
        set_cookie = response.getheader("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";")[0]
        connection.close()
        try:
            return response.status, json.loads(payload)
        except ValueError:
            return response.status, {}


class PresentedHandler(server.Handler):
    presented = ("192.0.2.44", 40000)

    @property
    def client_address(self):
        return self.presented

    @client_address.setter
    def client_address(self, _value):
        pass


class PresentedServer(ThreadingHTTPServer):
    def get_request(self):
        request, _address = super().get_request()
        return request, PresentedHandler.presented


SPOTIFY_ID = "a" * 22
SPOTIFY_KEY = f"spotify_{SPOTIFY_ID}"


class PlaylistMergeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        base = Path(self.folder.name)
        self.workspace = base / "room"
        self.lists = self.workspace / "lists"
        self.lists.mkdir(parents=True)
        self.config_dir = base / "config"
        self.config_dir.mkdir()

        def put(name, title, rows):
            (self.lists / name).write_text(write_xspf(title, rows), encoding="utf-8")

        put("alpha.xspf", "Alpha", [
            track("Alpha Song", "Artist A", "Album A", "spotify:track:AAA"),
            track("Tuple Song", "Tuple Artist", "Tuple Album"),
            track("Gamma", "Artist G", "Album G", "spotify:track:CCC"),
        ])
        put("beta.xspf", "Beta", [
            track("Alpha Song (alt)", "Artist A", "Album A", "spotify:track:AAA"),
            track("TUPLE SONG", "tuple artist", "TUPLE ALBUM"),
            track("Delta", "Artist D", "Album D"),
        ])
        put("gamma.xspf", "Gamma List", [
            track("Target One", "Artist T", "Album T"),
            track("Tuple Song", "Tuple Artist", "Tuple Album"),
        ])
        put(f"{SPOTIFY_KEY}.xspf", "Spotify · Menos", [
            track("Owned One", "Artist S", "Album S", "spotify:track:SSS"),
        ])
        linked = base / "elsewhere" / "linked_list.xspf"
        linked.parent.mkdir()
        linked.write_text(write_xspf("Linked list", [track("Linked Song", "Artist L")]),
                          encoding="utf-8")
        imported, errors = sync_files([linked], self.lists)
        self.assertFalse(errors)
        self.assertEqual(len(imported), 1)
        self.linked_key = mirror_key(linked)

        (self.config_dir / "spotify-sync.json").write_text(json.dumps(
            {"last_success": 1700000000, "last_attempt": 1700000000, "last_error": "",
             "playlists": {SPOTIFY_ID: {"name": "Menos", "snapshot": "s1"}}}), encoding="utf-8")
        config_path = self.config_dir / "config.json"
        config_path.write_text(json.dumps(
            {"folder": str(self.workspace), "lists_folder": str(self.lists),
             "source_files": [str(linked)], "source_folders": []}), encoding="utf-8")
        server.init(config_path)
        server.refresh_library(force=True)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.client = Client(self.httpd.server_address[1])

    def merge(self, body):
        return self.client.request("POST", "/api/playlists/merge", body)

    def membership(self, key):
        return {server.catalog.records[index - 1]["key"]
                for index in server.catalog.playlists[key] if index}

    def test_overview_marks_sources(self):
        status, overview = self.client.request("GET", "/api/overview")
        self.assertEqual(status, 200)
        sources = {item["id"]: item["source"] for item in overview["playlists"]}
        self.assertEqual(sources["alpha"], "local")
        self.assertEqual(sources[SPOTIFY_KEY], "spotify")
        self.assertEqual(sources[self.linked_key], "linked")

    def test_merge_into_new_list_unions_by_prepare_identity(self):
        record_count = len(server.catalog.records)
        union = self.membership("alpha") | self.membership("beta")
        status, body = self.merge({"sources": ["alpha", "beta"],
                                   "target": {"name": "Merged One"}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["file"], "Merged_One.xspf")
        self.assertEqual(body["tracks"], 4)
        self.assertEqual(body["added"], 4)
        rows = read_list(self.lists / "Merged_One.xspf")
        self.assertEqual([track_key(row) for row in rows], [
            "spotify:track:AAA",
            track_key({"artist": "Tuple Artist", "title": "Tuple Song", "album": "Tuple Album"}),
            "spotify:track:CCC",
            track_key({"artist": "Artist D", "title": "Delta", "album": "Album D"}),
        ])
        self.assertEqual(self.membership("Merged_One"), union)
        self.assertEqual(len(server.catalog.records), record_count)

    def test_metadata_add_is_persistent_idempotent_and_does_not_require_audio(self):
        request = {"playlist": "alpha", "artist": "Peppino Gagliardi", "title": "Vattenne"}
        for expected in ("added", "already"):
            status, result = self.client.request("POST", "/api/playlists/add-song", request)
            self.assertEqual(status, 200, result)
            self.assertIn(expected, result["message"])
        rows = read_list(self.lists / "alpha.xspf")
        self.assertEqual([r["title"] for r in rows].count("Vattenne"), 1)
        index = server.catalog.playlists["alpha"][-1]
        self.assertEqual(server.catalog.track(index)["title"], "Vattenne")
        self.assertNotEqual(server.catalog.status_of(index), "verified")
        self.assertEqual(self.client.request("POST", "/api/playlists/add-song",
                                             dict(request, playlist=self.linked_key))[0], 400)

    def test_csv_edition_keeps_its_identity_across_refreshes(self):
        raw = self.lists / "from-disk.csv"
        source = "Title,Artist\nOne,First artist\n"
        raw.write_text(source)
        server.refresh_library(force=True)
        for title in ("Two", "Three"):
            status, result = self.client.request("POST", "/api/playlists/add-song",
                                                {"playlist": "from-disk", "title": title, "artist": "Other"})
            self.assertEqual(status, 200, result)
            server.refresh_library(force=True)
        self.assertEqual(raw.read_text(), source)
        self.assertEqual([p for p in server.catalog.playlists if p.startswith("from-disk")], ["from-disk"])
        self.assertEqual([r["title"] for r in read_list(self.lists / "from-disk.xspf")], ["One", "Two", "Three"])

    def test_saved_edits_survive_refresh_failure_and_reorders_change_the_revision(self):
        def revision():
            status, overview = self.client.request("GET", "/api/overview")
            self.assertEqual(status, 200)
            return next(p["revision"] for p in overview["playlists"] if p["id"] == "alpha")

        request = {"playlist": "alpha", "title": "Vattenne", "artist": "Peppino Gagliardi"}
        with mock.patch.object(server, "refresh_library", side_effect=RuntimeError("retry refresh")):
            for _ in range(2):
                status, result = self.client.request("POST", "/api/playlists/add-song", request)
                self.assertEqual(status, 200, result)
                self.assertIn("refresh is pending", result["message"])
        server.refresh_library(force=True)
        before = revision()
        path = self.lists / "alpha.xspf"
        rows = read_list(path)
        self.assertEqual(sum(row["title"] == "Vattenne" for row in rows), 1)
        path.write_text(write_xspf("Alpha", reversed(rows)))
        server.refresh_library(force=True)
        self.assertNotEqual(before, revision())

    def test_device_addition_grant_is_specific_revocable_and_required(self):
        lan = PresentedServer(("127.0.0.1", 0), PresentedHandler)
        self.addCleanup(lan.server_close)
        self.addCleanup(lan.shutdown)
        threading.Thread(target=lan.serve_forever, daemon=True).start()
        device = Client(lan.server_address[1])
        device.origin = f"http://127.0.0.1:{device.port}"
        device.cookie = "lr_token=" + server.device_store.issue("listener", "Tablet")
        grant = {"id": "listener", "enabled": True}
        song = {"playlist": "beta", "title": "Vattenne", "artist": "Peppino Gagliardi"}
        access = "/api/devices/playlist-access"
        add = "/api/playlists/add-song"
        self.assertEqual(device.request("POST", access, grant)[0], 403)
        self.assertEqual(device.request("POST", add, song)[0], 403)
        self.assertEqual(self.client.request("POST", access, grant)[0], 200)
        playlists = device.request("GET", "/api/overview")[1]["playlists"]
        self.assertTrue(next(p for p in playlists if p["id"] == "beta")["can_add"])
        self.assertEqual(device.request("POST", add, song)[0], 200)
        self.assertEqual(device.request("POST", "/api/spotify/playlists", {"op": "rename"})[0], 403)
        self.assertEqual(self.client.request("POST", access, dict(grant, enabled=False))[0], 200)
        self.assertEqual(device.request("POST", add, song)[0], 403)

    def test_audio_ranges_remain_available_during_spotify_write(self):
        entered, resume = threading.Event(), threading.Event()
        def slow_add(*args):
            entered.set()
            resume.wait(6)
            return {"message": "Added to Spotify."}
        account = {"account_id": "owner", "playlist_id": SPOTIFY_ID}
        request = dict(account, playlist=SPOTIFY_KEY, title="Vattenne", artist="Peppino Gagliardi")
        audio = self.workspace / "fixture.mp3"
        audio.write_bytes(b"fixture audio")
        responses = []
        with mock.patch.object(server.spotify, "playlist_links", return_value={
                SPOTIFY_KEY: dict(account, mode="two_way")}), mock.patch.object(
                server.spotify, "add_metadata", side_effect=slow_add), mock.patch.object(
                server.audio_resolution, "track_file", return_value=audio):
            worker = threading.Thread(target=lambda: responses.append(
                self.client.request("POST", "/api/playlists/add-song", request)))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                connection = http.client.HTTPConnection("127.0.0.1", self.client.port, timeout=2)
                with contextlib.closing(connection):
                    connection.request("GET", "/api/audio/1", headers={"Range": "bytes=0-3"})
                    reply = connection.getresponse()
                    self.assertEqual((reply.status, reply.read()), (206, b"fixt"))
            finally:
                resume.set()
                worker.join(6)
        self.assertEqual(responses[0][0], 200)

    def test_merge_into_existing_list_snapshots_and_extends(self):
        gamma_bytes = (self.lists / "gamma.xspf").read_bytes()
        alpha_bytes = (self.lists / "alpha.xspf").read_bytes()
        status, body = self.merge({"sources": ["alpha"],
                                   "target": {"playlist": "gamma"}})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["tracks"], body["added"]), (4, 2))
        self.assertEqual(body["name"], "Gamma List")
        rows = read_list(self.lists / "gamma.xspf")
        self.assertEqual([row["title"] for row in rows],
                         ["Target One", "Tuple Song", "Alpha Song", "Gamma"])
        self.assertEqual((self.lists / ".previous" / "gamma.xspf").read_bytes(), gamma_bytes)
        self.assertEqual((self.lists / "alpha.xspf").read_bytes(), alpha_bytes)

    def test_merge_refuses_a_source_owned_target(self):
        for owned in (SPOTIFY_KEY, self.linked_key):
            status, body = self.merge({"sources": ["alpha"],
                                       "target": {"playlist": owned}})
            self.assertEqual(status, 400, (owned, body))
            self.assertIn("kept in sync", body["error"])

    def test_merge_name_conflict_then_rename(self):
        status, _body = self.merge({"sources": ["alpha", "beta"],
                                    "target": {"name": "Merged One"}})
        self.assertEqual(status, 200)
        status, body = self.merge({"sources": ["alpha", "gamma"],
                                   "target": {"name": "Merged One"}})
        self.assertEqual(status, 409)
        self.assertEqual(body["conflicts"], ["Merged_One.xspf"])
        status, body = self.merge({"sources": ["alpha", "gamma"],
                                   "target": {"name": "Merged One"},
                                   "on_conflict": "rename"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["file"], "Merged_One (2).xspf")
        self.assertTrue((self.lists / "Merged_One (2).xspf").exists())

    def test_merge_from_a_paired_device_is_forbidden(self):
        lan = PresentedServer(("127.0.0.1", 0), PresentedHandler)
        self.addCleanup(lan.server_close)
        self.addCleanup(lan.shutdown)
        threading.Thread(target=lan.serve_forever, daemon=True).start()
        device = Client(lan.server_address[1])
        device.origin = f"http://127.0.0.1:{lan.server_address[1]}"
        device.cookie = f"lr_token={server.SETTINGS['share_token']}"
        status, _body = device.request("POST", "/api/device",
                                       {"id": "merge-test-ipad", "name": "iPad"})
        self.assertEqual(status, 200)
        before = sorted(path.name for path in self.lists.rglob("*") if path.is_file())
        status, body = device.request("POST", "/api/playlists/merge",
                                      {"sources": ["alpha", "beta"],
                                       "target": {"name": "Sneaky"}})
        self.assertEqual(status, 403)
        self.assertIn("server computer", body["error"])
        after = sorted(path.name for path in self.lists.rglob("*") if path.is_file())
        self.assertEqual(before, after)


class TrackKeyTests(unittest.TestCase):
    def test_identifier_wins(self):
        row = {"identifier": "spotify:track:X", "artist": "A", "title": "T", "album": "B"}
        self.assertEqual(track_key(row), "spotify:track:X")

    def test_tuple_fallback_casefolds(self):
        row = {"identifier": "", "artist": "The Artist", "title": "The Title", "album": "The Album"}
        self.assertEqual(track_key(row), "the artist\0the title\0the album")

    def test_track_key_matches_prepare(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, library = root / "state", root / "audio"
            playlists_dir, sources = root / "playlists", root / "lists"
            for folder in (state, library, playlists_dir, sources):
                folder.mkdir()
            rows = [track("One", "Artist 1", "Album", "spotify:track:1"),
                    track("Two", "Artist 2", "Album")]
            (sources / "mix.xspf").write_text(write_xspf("Mix", rows), encoding="utf-8")
            patchers = [
                mock.patch.object(runner, "ROOT", root),
                mock.patch.object(runner, "resolve_config_path", lambda: root / "settings.json"),
                mock.patch.object(runner, "STATE", state),
                mock.patch.object(runner, "LIBRARY", library),
                mock.patch.object(runner, "PLAYLISTS", playlists_dir),
                mock.patch.object(runner, "OUTPUTS", root / "reports"),
                mock.patch.object(runner, "SOURCES", sources),
                mock.patch.object(runner, "MASTER", state / "master.csv"),
                mock.patch.object(runner, "REQUESTS_DB", state / "requests.sqlite3"),
                mock.patch.object(runner, "free_bytes", lambda: 100 * 1024**3),
            ]
            for patcher in patchers:
                patcher.start()
                self.addCleanup(patcher.stop)
            with contextlib.redirect_stdout(io.StringIO()):
                runner.prepare()
            build = CatalogStore(state / "requests.sqlite3").read_build()
            records = {record["index"]: record for record in build["records"]}
            built = [records[index]["key"] for index in build["playlists"]["mix"]]
            self.assertEqual(built, [track_key(row) for row in rows])


if __name__ == "__main__":
    unittest.main()
