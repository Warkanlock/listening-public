"""Plan 048: favorites.

Three layers, mirroring the suites they extend: the store against a
temp database (test_genres style), the endpoint and the tracks
filter over real HTTP (test_http_server's fixture style, mirrored
here so this module imports no other test module), and Deep Dive
seeding plus cache-key movement with the metadata fakes
(test_external_metadata style).
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import datetime
import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from fixture_environment import isolate_room_environment as setUpModule
import server
from catalog.catalog_store import CatalogStore
from catalog.favorites import FavoritesStore
from catalog.genres import GenreStore
from downloads.requests_store import RequestsStore
from metadata.external_metadata import ExternalMetadata


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

class FakeTime:
    """Deterministic stamps: each time() call lands one second later."""

    def __init__(self):
        self.moment = 1_700_000_000.0

    def time(self):
        self.moment += 1.0
        return self.moment


class FavoritesStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / "requests.sqlite3"
        self.store = FavoritesStore(self.database)

    def test_set_unset_and_recent_order(self):
        clock = FakeTime()
        with mock.patch("catalog.favorites.time", clock):
            self.assertEqual(self.store.keys(), set())
            self.assertEqual(self.store.recent(), [])
            self.assertTrue(self.store.set("key-1", True))
            self.store.set("key-2", True)
            self.store.set("key-3", True)
            self.assertEqual(self.store.keys(), {"key-1", "key-2", "key-3"})
            # Most recently favorited first.
            self.assertEqual(self.store.recent(), ["key-3", "key-2", "key-1"])
            # Re-liking makes a song the most recent favorite.
            self.store.set("key-1", True)
            self.assertEqual(self.store.recent(), ["key-1", "key-3", "key-2"])
            self.assertFalse(self.store.set("key-3", False))
            self.assertEqual(self.store.keys(), {"key-1", "key-2"})
            self.assertEqual(self.store.recent(), ["key-1", "key-2"])

    def test_stamp_reports_count_and_newest(self):
        clock = FakeTime()
        with mock.patch("catalog.favorites.time", clock):
            self.assertEqual(self.store.stamp(), (0, 0.0))
            self.store.set("key-1", True)
            self.store.set("key-2", True)
            count, newest = self.store.stamp()
            self.assertEqual(count, 2)
            self.assertEqual(newest, clock.moment)
            self.store.set("key-2", False)
            self.assertEqual(self.store.stamp(), (1, clock.moment - 1.0))

    def test_favorites_survive_a_fresh_store(self):
        self.store.set("key-9", True)
        fresh = FavoritesStore(self.database)
        self.assertEqual(fresh.keys(), {"key-9"})
        self.assertEqual(fresh.recent(), ["key-9"])


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------

LAN_IP = "192.0.2.44"  # TEST-NET-1 (RFC 5737), as in test_http_server.


class PresentedClientServer(ThreadingHTTPServer):
    presented_client = None

    def get_request(self):
        request, address = super().get_request()
        if self.presented_client:
            return request, (self.presented_client, address[1])
        return request, address


class FavoritesHttpTests(unittest.TestCase):
    """server.init against a temp workspace, real servers on
    ephemeral ports: the test_http_server fixture, trimmed to what
    the favorites surface needs."""

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
        cls.token = server.SETTINGS["share_token"]
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
        GenreStore(server.DB).set("key-2", "Rock", "file")
        server.catalog.reload()
        server.catalog.refresh()
        cls.loop_server = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.lan_server = PresentedClientServer(("127.0.0.1", 0), server.Handler)
        cls.lan_server.presented_client = LAN_IP
        cls.loop_port = cls.loop_server.server_address[1]
        cls.lan_port = cls.lan_server.server_address[1]
        for instance in (cls.loop_server, cls.lan_server):
            threading.Thread(target=instance.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for instance in (cls.loop_server, cls.lan_server):
            instance.shutdown()
            instance.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        # Every test starts from no favorites, whatever ran before.
        store = FavoritesStore(server.DB)
        for key in list(store.keys()):
            store.set(key, False)
        server.catalog.reload_favorites()

    def request(self, port, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        all_headers = dict(headers or {})
        if body is not None:
            all_headers.setdefault("Content-Type", "application/json")
        connection.request(method, path,
                           body=json.dumps(body) if body is not None else None,
                           headers=all_headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        connection.close()
        return result

    def lan_cookie(self):
        status, headers, _ = self.request(self.lan_port, "GET",
                                          f"/?token={self.token}")
        self.assertEqual(status, 303)
        return headers["Set-Cookie"].split(";", 1)[0]

    def lan_device_cookie(self, device_id="fav-device-0001", name="Fav device"):
        bootstrap = self.lan_cookie()
        status, headers, _ = self.request(
            self.lan_port, "POST", "/api/device",
            body={"id": device_id, "name": name},
            headers={"Cookie": bootstrap, "Origin": self.lan_origin()})
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0]

    def lan_origin(self):
        return f"http://127.0.0.1:{self.lan_port}"

    def post_favorite(self, index, favorite, port=None, headers=None):
        return self.request(port or self.loop_port, "POST", "/api/favorites",
                            body={"index": index, "favorite": favorite},
                            headers=headers)

    def fetch_tracks(self, params):
        status, _, data = self.request(
            self.loop_port, "GET", "/api/tracks?" + urlencode(params))
        self.assertEqual(status, 200)
        return json.loads(data)

    def test_post_marks_and_unmarks(self):
        status, _, data = self.post_favorite(2, True)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"index": 2, "favorite": True})
        rows = {row["id"]: row for row in self.fetch_tracks({})["tracks"]}
        self.assertTrue(rows[2]["favorite"])
        self.assertFalse(rows[1]["favorite"])
        status, _, data = self.request(self.loop_port, "GET", "/api/overview")
        self.assertEqual(json.loads(data)["favorites"], 1)
        # The metadata surface carries the flag too. Its MusicBrainz
        # lookup is pre-answered through the metadata cache so the
        # test never leaves the machine.
        RequestsStore(server.DB).cache_put("metadata:key-2", "{}", time.time())
        status, _, data = self.request(self.loop_port, "GET", "/api/metadata/2")
        self.assertTrue(json.loads(data)["favorite"])
        status, _, data = self.post_favorite(2, False)
        self.assertEqual(json.loads(data), {"index": 2, "favorite": False})
        status, _, data = self.request(self.loop_port, "GET", "/api/overview")
        self.assertEqual(json.loads(data)["favorites"], 0)

    def test_post_rejects_a_bad_index_or_value(self):
        for body in ({"index": 0, "favorite": True},
                     {"index": 99, "favorite": True},
                     {"index": "2", "favorite": True},
                     {"index": True, "favorite": True},
                     {"index": 2, "favorite": "yes"},
                     {"index": 2}):
            status, _, _ = self.request(self.loop_port, "POST",
                                        "/api/favorites", body=body)
            self.assertEqual(status, 400, body)

    def test_post_is_a_paired_device_action_not_a_host_one(self):
        # The paired-device tier reaches it, like /api/session...
        cookie = self.lan_device_cookie()
        headers = {"Cookie": cookie, "Origin": self.lan_origin()}
        status, _, data = self.post_favorite(3, True, port=self.lan_port,
                                             headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"index": 3, "favorite": True})
        # ...while a host-only action on the same tier is refused,
        # which is what keeps the two tiers distinguishable.
        status, _, _ = self.request(self.lan_port, "POST", "/api/genre",
                                    body={"track_id": 3, "genre": "Rock"},
                                    headers=headers)
        self.assertEqual(status, 403)

    def test_tracks_filter_and_its_compositions(self):
        self.post_favorite(2, True)
        self.post_favorite(4, True)
        payload = self.fetch_tracks({"filter": "favorites"})
        self.assertEqual(payload["total"], 2)
        self.assertEqual([row["id"] for row in payload["tracks"]], [2, 4])
        # Composes with a playlist: only record 4 is in "few".
        payload = self.fetch_tracks({"filter": "favorites", "playlist": "few"})
        self.assertEqual(payload["total"], 1)
        self.assertEqual([row["id"] for row in payload["tracks"]], [4])
        # Composes with a query: only record 2's title matches.
        payload = self.fetch_tracks({"filter": "favorites", "q": "second"})
        self.assertEqual([row["id"] for row in payload["tracks"]], [2])
        # Composes with a genre: only record 2 carries one.
        payload = self.fetch_tracks({"filter": "favorites", "genre": "rock"})
        self.assertEqual([row["id"] for row in payload["tracks"]], [2])


# ---------------------------------------------------------------------------
# Deep Dive seeding
# ---------------------------------------------------------------------------

PINNED_NOW = datetime.datetime(2026, 11, 16, 12, 0, tzinfo=datetime.timezone.utc)


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now_value = start
        self.monotonic_value = 0.0

    def now(self):
        return self.now_value

    def monotonic(self):
        return self.monotonic_value

    def sleep(self, seconds):
        self.now_value += seconds
        self.monotonic_value += seconds

    def advance(self, seconds):
        self.now_value += seconds
        self.monotonic_value += seconds


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        return self.handler(url)


class FakeCatalog:
    def __init__(self, records):
        self.records = records

    def track(self, index):
        record = self.records[index - 1]
        return {"id": index, "artist": record["artist"], "title": record["title"],
                "album": record["album"], "duration": record["length"],
                "cover": "", "status": "verified", "playlists": [],
                "audio": None, "spotify_url": "", "reason": ""}


def dive_record(index, artist):
    return {"index": index, "artist": artist, "title": f"Song {artist}",
            "album": "", "length": 200, "isrc": "", "key": f"k{index}",
            "active": True, "uri": ""}


def dive_handler(url):
    """One artist search and a two-recording radio per artist; every
    recording is credited to a "Near <artist>" act the catalog does
    not hold, so nothing is excluded and every seed contributes."""
    url = unquote(url)
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if parsed.path == "/ws/2/artist":
        query = params["query"][0]
        artist = query[len('artist:"'):-1]
        return {"artists": [{"id": f"mbid-{artist}", "score": 100}]}
    if "api.listenbrainz.org" in url:
        artist = next(name for name in DIVE_ARTISTS if f"mbid-{name}" in url)
        return {"recordings": [
            {"recording_mbid": f"rad-{artist}-{n}",
             "similar_artist_name": f"Near {artist}"} for n in (1, 2)]}
    if parsed.path.startswith("/ws/2/recording/"):
        mbid = parsed.path.rsplit("/", 1)[1]
        artist = mbid.split("-")[1]
        return {"title": f"Radio {mbid}", "length": 180000, "genres": [],
                "artist-credit": [{"name": f"Near {artist}"}],
                "releases": [{"title": "Radio Album"}]}
    raise AssertionError("unexpected url: " + url)


DIVE_ARTISTS = ["Artist A", "Artist B", "Artist C",
                "Artist D", "Artist E", "Artist F"]


def artist_searches(transport):
    return [parse_qs(urlparse(url).query)["query"][0]
            for url, _headers in transport.calls if "/ws/2/artist" in url]


class DiscoverFavoritesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RequestsStore(Path(self.tmp.name) / "requests.sqlite3")
        self.clock = FakeClock()
        self.favorites = FavoritesStore(Path(self.tmp.name) / "requests.sqlite3")
        self.catalog = FakeCatalog([dive_record(index, artist)
                                    for index, artist in enumerate(DIVE_ARTISTS, 1)])

    def make_metadata(self):
        transport = FakeTransport(dive_handler)
        metadata = ExternalMetadata(
            lambda: self.store, transport=transport, now=self.clock.now,
            monotonic=self.clock.monotonic, sleep=self.clock.sleep,
            today=lambda: datetime.date(2026, 11, 16),
            utcnow=lambda: PINNED_NOW)
        return metadata, transport

    def test_favorite_artists_seed_before_the_shuffle(self):
        clock = FakeTime()
        with mock.patch("catalog.favorites.time", clock):
            self.favorites.set("k3", True)  # Artist C, liked first
            self.favorites.set("k5", True)  # Artist E, most recent
        metadata, transport = self.make_metadata()
        payload = metadata.discover_weekly(self.catalog, "/dive", self.favorites)
        searches = artist_searches(transport)
        # Five seeds: the two favorite artists, most recent first,
        # then the week-seeded shuffle fills the remaining slots.
        self.assertEqual(len(searches), 5)
        self.assertEqual(len(set(searches)), 5)
        self.assertEqual(searches[0], 'artist:"Artist E"')
        self.assertEqual(searches[1], 'artist:"Artist C"')
        for search in searches[2:]:
            self.assertNotIn(search, ('artist:"Artist E"', 'artist:"Artist C"'))
        reasons = {track["reason"] for track in payload["tracks"]}
        self.assertIn("From artists near Artist E", reasons)
        self.assertIn("From artists near Artist C", reasons)
        self.assertEqual(len(payload["tracks"]), 10)

    def test_a_new_favorite_moves_the_cache_key(self):
        metadata, transport = self.make_metadata()
        metadata.discover_weekly(self.catalog, "/dive", self.favorites)
        calls = len(transport.calls)
        # Same favorites state: the week is served from the cache.
        metadata.discover_weekly(self.catalog, "/dive", self.favorites)
        self.assertEqual(len(transport.calls), calls)
        # A favorite lands: the fingerprint moves, the week rebuilds,
        # and the new favorite's artist seeds first.
        self.favorites.set("k2", True)
        metadata.discover_weekly(self.catalog, "/dive", self.favorites)
        self.assertGreater(len(transport.calls), calls)
        self.assertEqual(artist_searches(transport)[-5], 'artist:"Artist B"')
        # discover_status agrees on the key: under a fresh favorite
        # it reports the week as building, not the stale payload.
        self.favorites.set("k4", True)
        status = metadata.discover_status(self.catalog, "/dive", self.favorites)
        self.assertEqual(status["status"], "building")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = metadata.discover_status(self.catalog, "/dive",
                                              self.favorites)
            if status.get("tracks"):
                break
            time.sleep(0.01)
        self.assertTrue(status.get("tracks"))


if __name__ == "__main__":
    unittest.main()
