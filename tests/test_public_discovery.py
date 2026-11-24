"""Public discovery using HTTP against a disposable private room."""

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from unittest import TestCase, mock
from urllib.parse import parse_qs, urlsplit

from fixture_environment import isolate_room_environment as setUpModule
from test_http_server import ServerFixture, server
from adapters import public_discovery
from system.owner_auth import OwnerAuth


PASSWORD = "Disposable Discovery Password"


class PublicDiscoveryTests(ServerFixture, TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        auth = OwnerAuth(cls.config_path.parent)
        auth.set_password("discover-owner", PASSWORD)
        auth.household.add_member("discover-member", PASSWORD)

    def call(self, method, path, body=None, cookie="", *, origin=None):
        return self.request(self.lan_port, method, path, body=body, headers={
            "Cookie": cookie, "Origin": origin or self.lan_origin(),
        })

    def sign_in(self, username):
        status, headers, body = self.call("POST", "/api/account/login", {"username": username, "password": PASSWORD})
        self.assertEqual(status, 200, body)
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_listeners_can_see_sources_without_extension_management_access(self):
        path = "/api/extensions/sources"
        self.assertEqual(self.call("GET", path)[0], 401)
        member = self.sign_in("discover-member")
        device = self.lan_device_cookie()
        with mock.patch.dict(server.SETTINGS, {"download_engines": {}}), mock.patch.dict(os.environ, {"LISTENING_ENGINE_SOCKET": ""}):
            for cookie in (member, device, self.sign_in("discover-owner")):
                status, _, data = self.call("GET", path, cookie=cookie)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(data), {"state": "unconfigured", "selection": {}, "installed": [], "inventory_available": False})
        for cookie in (member, device):
            self.assertEqual(self.call("GET", "/api/extensions", cookie=cookie)[0], 403)

    def test_paired_devices_and_members_can_check_health_without_managing_extensions(self):
        path = "/api/extensions/health"
        self.assertEqual(self.call("POST", path, {})[0], 401)
        with mock.patch.dict(server.SETTINGS, {"download_engines": {}}):
            for cookie in (self.sign_in("discover-member"), self.lan_device_cookie(), self.sign_in("discover-owner")):
                self.assertEqual(self.call("POST", path, {}, cookie, origin="https://unrelated.example")[0], 403)
                self.assertEqual(self.call("POST", path, {"engine": "unknown"}, cookie)[0], 400)
                status, _, body = self.call("POST", path, {}, cookie)
                self.assertEqual(status, 200, body)
                self.assertEqual(json.loads(body)["checks"][-1]["state"], "failed")
                self.assertEqual(server.SETTINGS["download_engines"], {})

    def test_owner_can_add_music_source_and_members_can_discover_it(self):
        sources = "/api/discovery/sources"
        browse = "/api/discovery/search"
        owner = self.sign_in("discover-owner")
        member = self.sign_in("discover-member")
        self.assertEqual(self.call("GET", sources)[0], 401)
        self.assertEqual(self.call("POST", browse, {"media": "music"})[0], 401)
        custom = {"op": "add", "name": "Fixture Radio", "kind": "funkwhale", "media": "music",
                  "address": "https://fixture.example"}
        self.assertEqual(self.call("POST", sources, custom, member)[0], 403)
        self.assertEqual(self.call("POST", sources, custom, owner, origin="https://unrelated.example")[0], 403)
        prohibited = ("http://127.0.0.1/feed", "https://room.local/feed", "https://foo:bar@example.org/feed",
                      "https://fixture.example/feed?access_token=secret", "https://[::ffff:10.0.0.1]/feed")
        for address in prohibited:
            with self.subTest(address=address):
                self.assertEqual(self.call("POST", sources, {**custom, "address": address}, owner)[0], 400)

        with mock.patch.object(public_discovery, "_json", return_value={"results": [{"id": 42, "title": "Night set"}], "count": 1}) as outbound:
            status, _, raw = self.call("POST", sources, custom, owner)
            self.assertEqual(status, 200, raw)
            save, = [source for source in json.loads(raw)["sources"] if source["custom"]]
            self.assertTrue(save["address"])
            stored = public_discovery.DiscoverySources(server.host_database(server.DB, server.STATE)).list()
            self.assertIn(save["id"], [item.id for item in stored])
            status, _, raw = self.call("GET", sources, cookie=member)
            self.assertEqual(status, 200, raw)
            reply = json.loads(raw)
            self.assertFalse(reply["can_manage"])
            self.assertEqual([item["address"] for item in reply["sources"] if item["custom"]], [""])
            outbound.assert_not_called()

            args = {"query": "station transmissions", "media": "music", "source": save["id"], "page": 0}
            self.assertEqual(self.call("POST", browse, args, member, origin="https://unrelated.example")[0], 403)
            self.assertEqual(self.call("POST", browse, {**args, "source": "https://unrelated.example"}, member)[0], 400)
            outbound.assert_not_called()
            status, _, raw = self.call("POST", browse, args, member)
            self.assertEqual(status, 200, raw)
            group, = json.loads(raw)["groups"]
            item, = group["items"]
            self.assertEqual((item["title"], item["url"]),
                             ("Night set", "https://fixture.example/library/tracks/42"))
            self.assertEqual(urlsplit(outbound.call_args.args[0]).hostname, "fixture.example")
            self.assertEqual(self.call("POST", browse, args, owner)[2], raw)
            outbound.assert_called_once()
            outbound.side_effect = OSError("provider unavailable")
            next_page = {**args, "page": 1}
            self.assertTrue(json.loads(self.call("POST", browse, next_page, member)[2])["groups"][0]["error"])
            outbound.side_effect = None
            self.assertFalse(json.loads(self.call("POST", browse, next_page, member)[2])["groups"][0]["error"])
            self.assertEqual(outbound.call_count, 3)

        self.assertEqual(self.call("POST", sources, {"op": "remove", "id": save["id"]}, owner)[0], 200)
        self.assertEqual(self.call("POST", browse, args, member)[0], 400)

    def test_artist_search_imports_metadata_and_leaves_acquisition_explicit(self):
        owner = self.sign_in("discover-owner")
        member = self.sign_in("discover-member")
        mbid = "82212620-cbfc-40df-b700-cfa49405beeb"
        query = {"media": "music", "query": "PEPPINO GAGLIARDI"}
        def songs(path, params):
            self.assertEqual(path, "recording")
            self.assertIn('artist:"PEPPINO GAGLIARDI"', params["query"])
            return {"count": 145, "recordings": [{"id": mbid, "title": "Settembre", "length": 212000,
                    "artist-credit": [{"name": "Peppino Gagliardi", "artist": {"name": "Peppino Gagliardi"}}],
                    "releases": [{"title": "Settembre"}]}]}
        def apple(url):
            self.assertEqual(urlsplit(url).hostname, "itunes.apple.com")
            return {"results": [{"trackName": "Invalid row", "trackId": 123, "trackViewUrl": "https://music.apple.com/123"},
                                {"trackName": "Valid title" * 45, "artistName": "Peppino Gagliardi", "trackId": 124,
                                 "trackViewUrl": "https://music.apple.com/124"}]}
        with mock.patch.object(server.metadata, "musicbrainz", side_effect=songs) as mb, mock.patch.object(public_discovery, "_json", side_effect=apple):
            status, _, data = self.call("POST", "/api/discovery/search", query, owner)
            self.assertEqual(status, 200, data)
            result, other = json.loads(data)["groups"]
            self.assertEqual((result["source"], other["source"]), ("musicbrainz", "apple-music"))
            self.assertEqual([row["title"] for row in other["items"]], ["Valid title" * 45])
            self.assertTrue(result["more"])
            song, = result["items"]
            self.assertEqual(song["creator"], "Peppino Gagliardi")
            body = {**song, "artist": song["creator"]}
            self.assertEqual(self.call("POST", "/api/discovery/import", body, member)[0], 403)
            self.assertEqual(self.call("POST", "/api/discovery/import", body, owner, origin="https://elsewhere.example")[0], 403)
            with mock.patch.object(server, "worker_running", side_effect=lambda kind: kind == "genres"):
                status, _, data = self.call("POST", "/api/discovery/import", body, owner)
                self.assertEqual(status, 200, data)
                self.assertEqual(json.loads(data), {"imported": True, "track_id": None, "ready": False})
                alternate = {k: v for k, v in body.items() if k != "musicbrainz_id"}
                status, _, data = self.call("POST", "/api/discovery/import", {**alternate, "apple_id": "123"}, owner)
                self.assertEqual(status, 200, data)
                self.assertFalse(json.loads(data)["imported"])
            status, _, data = self.call("POST", "/api/discovery/import", body, owner)
            self.assertEqual(status, 200, data)
            saved = json.loads(data)
            self.assertFalse(saved["imported"])
            record = server.catalog.record_at(saved["track_id"])
            self.assertEqual((record["title"], record["length"], record["manual_copy"]), ("Settembre", 212, True))
            self.assertEqual(record["uri"], f"https://musicbrainz.org/recording/{mbid}")
            self.assertEqual(self.call("POST", "/api/discovery/import", body, owner)[0], 200)
            self.assertEqual(server.catalog.playlists["listening_catalog_imports"], [record["index"]])
            self.assertEqual(server.requests_store().active_count(), 0)
            _, _, data = self.call("POST", "/api/discovery/search", {**query, "page": 11}, owner)
            self.assertEqual(mb.call_args.args[1]["offset"], 132)
            self.assertEqual(json.loads(data)["groups"][0]["items"][0]["track_id"], record["index"])
            status, _, data = self.call("POST", "/api/requests", {"track_id": record["index"]}, owner)
            self.assertEqual(status, 201, data)
            self.assertEqual(json.loads(data)["status"], "waiting_format")

    def test_retired_sources_do_not_fill_the_music_limit(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "discovery.sqlite3"
            sources = public_discovery.DiscoverySources(database)
            sources.list()
            with closing(sqlite3.connect(database)) as db, db:
                db.executemany("INSERT INTO discovery_sources VALUES (?,'Legacy','rss','podcasts','https://fixture.example')",
                               ((str(old),) for old in range(public_discovery.MAX_SOURCES)))
            sources.change({"op": "add", "name": "Music", "kind": "funkwhale", "media": "music",
                            "address": "https://music.example"})
            self.assertEqual([row.name for row in sources.list() if row.custom], ["Music"])
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM discovery_sources WHERE kind='rss'").fetchone()[0],
                                 public_discovery.MAX_SOURCES)

    def test_federated_failure_keeps_archive_results_and_scope(self):
        lockup = []

        def providers(url):
            parsed = urlsplit(url)
            if parsed.hostname == "itunes.apple.com":
                return {"results": []}
            if parsed.hostname == "sepiasearch.org":
                raise ValueError("Fixture outage")
            self.assertEqual(parsed.hostname, "archive.org")
            lockup.append(parse_qs(parsed.query))
            return {"response": {"numFound": 40, "docs": [
                {"identifier": "fixture-live-2026", "title": "Night Set\ud800", "creator": ["Fixture Trio"], "date": "2026"},
                {"identifier": "javascript:bad", "title": "Refused"},
                {"identifier": "fixture-live-2026", "title": "Night Set", "creator": ["Fixture Trio"]},
            ]}}

        with mock.patch.object(public_discovery, "_json", side_effect=providers):
            result = public_discovery.search(public_discovery.DEFAULTS, {"query": 'name" OR collection:*',
                "source": "all-public", "page": 1, "media": "music"}, recordings=lambda *_: ([], 0))
        _, archive, fediverse, apple = result["groups"]
        self.assertEqual([item["url"] for item in archive["items"]], ["https://archive.org/details/fixture-live-2026"])
        self.assertEqual(archive["items"][0]["title"], "Night Set?")
        json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.assertTrue(archive["more"])
        self.assertTrue(fediverse["error"])
        self.assertFalse(apple["error"])
        self.assertEqual(lockup[0]["page"], ["2"])
        self.assertTrue(lockup[0]["q"][0].startswith(
            'collection:"etree" AND (mediatype:audio OR mediatype:etree) AND (title:"name\\""'))
        self.assertIn('title:"collection:*"', lockup[0]["q"][0])

    def test_apple_pagination_keeps_the_extra_result_for_the_next_page(self):
        page_size = public_discovery.PAGE_SIZE
        rows = [{"trackName": f"Track {index}", "trackViewUrl": f"https://music.apple.com/song/{index}"}
                for index in range(page_size + 1)]
        query = {"source": "apple-music", "media": "music", "query": "Track"}
        with mock.patch.object(public_discovery, "_json", return_value={"results": rows}):
            first, = public_discovery.search(public_discovery.DEFAULTS, query)["groups"]
            second, = public_discovery.search(public_discovery.DEFAULTS, {**query, "page": 1})["groups"]
        self.assertEqual([row["title"] for row in first["items"]], [f"Track {i}" for i in range(page_size)])
        self.assertTrue(first["more"])
        self.assertEqual([row["title"] for row in second["items"]], [f"Track {page_size}"])
        self.assertFalse(second["more"])

    def test_peertube_keeps_public_links_and_funkwhale_uses_track_pages(self):
        video = {"data": [
            {"name": "Open conversation", "url": "https://talks.example/videos/watch/a",
             "privacy": {"id": 1}, "duration": 87, "licence": {"label": "CC BY"}},
            {"name": "Private URL", "url": "https://127.0.0.1/video", "privacy": {"id": 1}},
            {"name": "Unlisted", "url": "https://talks.example/videos/watch/b", "privacy": {"id": 2}},
        ], "total": 3}
        with mock.patch.object(public_discovery, "_json", return_value=video) as fetch:
            result = public_discovery.search(public_discovery.DEFAULTS,
                                             {"source": "sepia", "media": "music", "query": "open"})
            request = parse_qs(urlsplit(fetch.call_args.args[0]).query)
        item, = result["groups"][0]["items"]
        self.assertEqual((item["title"], item["detail"], item["license"]), ("Open conversation", "1:27", "CC BY"))
        self.assertEqual(request["categoryOneOf[]"], ["1"])

        pod = public_discovery.new_source({"kind": "funkwhale", "media": "music", "name": "Public tracks",
                                           "address": "https://tracks.example/"})
        with mock.patch.object(public_discovery, "_json", return_value={"results": [
            {"title": "Song", "id": 45, "artist": {"name": "Artist"}, "album": {"title": "Release"}},
            {"title": "Current track", "id": 46, "artist_credit": [
                {"credit": "Credited name", "artist": {"name": "Other name"}},
                {"credit": "", "artist": {"name": "Second artist"}},
            ]},
        ], "count": 2}) as fetch:
            result = public_discovery.search([pod], {"media": "music", "query": "Song"})
        request = urlsplit(fetch.call_args.args[0])
        self.assertEqual(request.path, "/api/v1/tracks/")
        self.assertEqual(parse_qs(request.query)["playable"], ["true"])
        legacy, current = result["groups"][0]["items"]
        self.assertEqual(legacy["url"], "https://tracks.example/library/tracks/45")
        self.assertEqual((legacy["creator"], current["creator"]), ("Artist", "Credited name, Second artist"))

    def test_internal_catalogs_search_only_the_selected_scope_and_keep_tokens_at_the_service(self):
        requests = []
        token = "disposable-catalog-token"

        class Catalog(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlsplit(self.path)
                query = parse_qs(url.query)
                authorization = self.headers.get("Authorization")
                requests.append((url.path, query, authorization))
                if url.path.startswith("/jellyfin/") and authorization != f'MediaBrowser Token="{token}"':
                    self.send_error(401)
                    return
                if query.get("SearchTerm") == ["redirect"]:
                    self.send_response(302)
                    self.send_header("Location", "/unexpected")
                    self.end_headers()
                    return
                responses = {
                    "/jellyfin/Items": {"Items": [
                        {"Id": "abc", "Name": "Quiet Set", "Artists": ["Fixture Trio"], "Type": "MusicAlbum"},
                        {"Id": "def", "Name": "Opening", "Artists": ["Fixture Trio"], "Type": "Audio", "Album": "Quiet Set"}],
                        "TotalRecordCount": 2},
                }
                if url.path not in responses:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(responses[url.path]).encode())

            def log_message(self, *_args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Catalog) as fixture:
            worker = threading.Thread(target=fixture.serve_forever)
            worker.start()
            try:
                origin = f"http://127.0.0.1:{fixture.server_port}"
                with mock.patch.dict(os.environ, {
                    "LISTENING_JELLYFIN_URL": origin + "/jellyfin",
                    "LISTENING_JELLYFIN_WEB_URL": "http://media.home:8096",
                    "LISTENING_JELLYFIN_TOKEN": token,
                    "LISTENING_AUDIOBOOKSHELF_URL": origin + "/abs",
                    "LISTENING_AUDIOBOOKSHELF_WEB_URL": "http://media.home:13378",
                    "LISTENING_AUDIOBOOKSHELF_TOKEN": token,
                }), mock.patch.object(public_discovery, "_json") as public:
                    member = self.sign_in("discover-member")
                    owner = self.sign_in("discover-owner")
                    status, _, raw = self.call("GET", "/api/discovery/sources", cookie=owner)
                    self.assertEqual(status, 200)
                    self.assertNotIn(token, raw.decode())
                    room = [source for source in json.loads(raw)["sources"] if source["scope"] == "room"]
                    self.assertEqual({source["id"] for source in room}, {"jellyfin"})
                    self.assertTrue(all(not source["address"] for source in room))
                    browse = "/api/discovery/search"
                    args = {"source": "jellyfin", "media": "music", "query": "quiet"}
                    self.assertEqual(self.call("POST", browse, args, member)[0], 400)
                    status, _, raw = self.call("POST", browse, {**args, "scope": "room"}, member)
                    self.assertEqual(status, 200)
                    music, song = json.loads(raw)["groups"][0]["items"]
                    self.assertEqual(music["url"], "http://media.home:8096/web/index.html#/details?id=abc")
                    self.assertNotIn("song", music)
                    self.assertEqual((song["song"], song["album"]), (True, "Quiet Set"))
                    self.assertEqual(self.call("POST", browse, {**args, "scope": "room"}, owner)[2], raw)
                    self.assertEqual(len(requests), 1)
                    with mock.patch.dict(os.environ, {"LISTENING_JELLYFIN_USER_ID": "another-library"}):
                        self.assertEqual(self.call("POST", browse, {**args, "scope": "room"}, member)[0], 200)
                        self.assertEqual(requests[-1][1]["UserId"], ["another-library"])
                    _, _, raw = self.call("POST", browse, {**args, "scope": "room", "query": "redirect"}, member)
                    self.assertTrue(json.loads(raw)["groups"][0]["error"])
                    self.assertNotIn("/unexpected", [request[0] for request in requests])
                    self.assertEqual(requests[0][1]["SearchTerm"], ["quiet"])
                    self.assertEqual(requests[0][2], f'MediaBrowser Token="{token}"')
                    public.assert_not_called()
            finally:
                fixture.shutdown()
                worker.join()
