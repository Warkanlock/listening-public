import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from concurrent.futures import ThreadPoolExecutor
import datetime
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock
from urllib.parse import parse_qs, urlparse

from downloads.requests_store import RequestsStore
from metadata import external_metadata as external_metadata_module
from metadata.artist_connections import ArtistConnections
from metadata.external_metadata import ExternalMetadata


# --------------------------------------------------------------------------
# Fakes: a clock whose sleep() advances it, and a transport that routes by
# URL to canned payloads. No test in this file touches the real network.
# --------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now_value = start
        self.monotonic_value = 0.0
        self.sleeps = []

    def now(self):
        return self.now_value

    def monotonic(self):
        return self.monotonic_value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
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
    def __init__(self, records, memberships=None, list_names=None):
        self.records = records
        self.memberships = memberships or {}
        self.list_names = list_names or {}

    def record_at(self, index):
        # The Catalog seam (plan 052), mirrored: a non-bool int in
        # range reads records[index - 1]; anything else is None.
        if isinstance(index, int) and not isinstance(index, bool) \
                and 1 <= index <= len(self.records):
            return self.records[index - 1]
        return None

    def track(self, index):
        record = self.records[index - 1]
        return {"id": index, "artist": record["artist"], "title": record["title"],
                "album": record["album"], "duration": record["length"], "cover": "",
                "status": "verified", "playlists": [], "audio": None,
                "spotify_url": "", "reason": ""}


def record(index, artist, title, album="", length=180, isrc="", key=None, active=True, uri=""):
    return {"index": index, "artist": artist, "title": title, "album": album,
            "length": length, "isrc": isrc, "key": key or f"k{index}",
            "active": active, "uri": uri}


def http_error(code):
    return urllib.error.HTTPError("https://example.invalid", code, "error", None, None)


# --------------------------------------------------------------------------
# The Discover fixture: one catalog artist, a 15-recording radio response
# (rec-01 already in the catalog, rec-02 unfetchable, 13 eligible), so the
# golden payload exercises exclusion, failure skipping, and the 12 cap.
# --------------------------------------------------------------------------

FIXTURE_RELEASE = "01234567-1234-1234-1234-123456789abc"

def rec_payload(title, artist, album, length_ms, genres):
    return {"title": title, "artist-credit": [{"name": artist}],
            "releases": [{"title": album, "id": FIXTURE_RELEASE}], "length": length_ms,
            "genres": [{"name": g} for g in genres]}


DISCOVER_RECORDINGS = {"rec-01": rec_payload("Known Song", "Alpha Artist", "A1", 200000, ["rock"])}
for _n in range(3, 16):
    DISCOVER_RECORDINGS[f"rec-{_n:02d}"] = rec_payload(
        f"Radio Song {_n:02d}", f"Radio Artist {_n:02d}", f"Radio Album {_n:02d}",
        180000 + _n * 1000, ["techno", "house", "electronic", "dance"])

RADIO = {"recordings": [
    {"recording_mbid": f"rec-{n:02d}", "similar_artist_name": f"Radio Artist {n:02d}"}
    for n in range(1, 16)]}

PINNED_NOW = datetime.datetime(2026, 1, 5, 12, 0, tzinfo=datetime.timezone.utc)

# Captured from the pre-extraction server code against this fixture
# (week 2026-W47, workspace "/golden"); generated_at is the pinned clock.
GOLDEN_DISCOVER = {
    "week": "2026-W47",
    "source": "ListenBrainz radio + MusicBrainz",
    "generated_at": PINNED_NOW.isoformat(),
    "tracks": [
        {"musicbrainz_id": mbid, "title": f"Radio Song {n:02d}", "artist": f"Radio Artist {n:02d}",
         "album": f"Radio Album {n:02d}", "duration": 180 + n,
         "reason": "From artists near Alpha Artist", "seed_artist": "Alpha Artist",
         "cover": f"https://coverartarchive.org/release/{FIXTURE_RELEASE}/front-500",
         "genres": ["techno", "house", "electronic"], "art_seed": seed}
        for mbid, n, seed in [
            ("rec-08", 8, "8d375db1ac55"), ("rec-05", 5, "88197b4a23d7"),
            ("rec-15", 15, "740e71e39c2d"), ("rec-14", 14, "9f757110f3bd"),
            ("rec-03", 3, "c36f4b7837b7"), ("rec-04", 4, "ff709560847b"),
            ("rec-10", 10, "31e33f600560"), ("rec-09", 9, "f06e6ab8e058"),
            ("rec-07", 7, "92a41661f1ff"), ("rec-12", 12, "8119f3dddb7b"),
            ("rec-13", 13, "66122e48db88"), ("rec-06", 6, "ed409dc3ee48"),
        ]],
}


def discover_handler(url):
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "api.listenbrainz.org" in url:
        assert "artist-alpha" in url, url
        return RADIO
    if parsed.path == "/ws/2/artist":
        assert params["query"][0] == 'artist:"Alpha Artist"', url
        return {"artists": [{"id": "artist-alpha", "score": 100}]}
    if parsed.path.startswith("/ws/2/recording/"):
        mbid = parsed.path.rsplit("/", 1)[1]
        if mbid == "rec-02":
            raise http_error(404)
        return DISCOVER_RECORDINGS[mbid]
    raise AssertionError("unexpected url: " + url)


def discover_catalog():
    return FakeCatalog([
        record(1, "Alpha Artist", "Known Song", "A1", 200),
        record(2, "Alpha Artist", "Another Known", "A1", 190),
    ])


class MetadataTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RequestsStore(Path(self.tmp.name) / "requests.sqlite3")
        self.clock = FakeClock()

    def make_metadata(self, handler, **overrides):
        transport = FakeTransport(handler)
        options = {"transport": transport, "now": self.clock.now,
                   "monotonic": self.clock.monotonic, "sleep": self.clock.sleep,
                   "today": lambda: datetime.date(2026, 11, 16),
                   "utcnow": lambda: PINNED_NOW}
        options.update(overrides)
        metadata = ExternalMetadata(lambda: self.store, **options)
        return metadata, transport


class ArtistPopularityTests(MetadataTestCase):
    def test_authenticated_popularity_uses_only_artist_id_and_caches_public_result(self):
        artist = "00000000-0000-0000-0000-000000000001"

        def reply(url):
            self.assertEqual(url, f"https://api.listenbrainz.org/1/popularity/top-recordings-for-artist/{artist}")
            return [{"total_listen_count": 20}]

        metadata, transport = self.make_metadata(reply)
        with mock.patch.dict("os.environ", {"LISTENING_ROOM_LISTENBRAINZ_TOKEN": "fixture-token"}):
            first = metadata.artist_popularity(artist)
            self.assertEqual(metadata.artist_popularity(artist), first)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0][1]["Authorization"], "Token fixture-token")
        self.assertNotIn("fixture-token", self.store.cache_get(f"artist-popularity:recordings:{artist}")["body"])

    def test_token_is_never_forwarded_on_redirect(self):
        def reply(request, timeout):
            self.assertEqual(request.get_header("Authorization"), "Token fixture-token")
            self.assertNotIn("Authorization", request.headers)
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = b"[]"
            return response

        with mock.patch.object(external_metadata_module.urllib.request, "urlopen", side_effect=reply):
            self.assertEqual(external_metadata_module._urlopen_json(
                "https://api.listenbrainz.org/", {"Authorization": "Token fixture-token"}), [])


class ArtistConnectionsTests(MetadataTestCase):
    def setUp(self):
        super().setUp()
        self.band = {"id": "10000000-0000-0000-0000-000000000000", "name": "Bright Field", "type": "Group"}
        self.person = {"id": "20000000-0000-0000-0000-000000000000", "name": "Ada Bell", "type": "Person"}
        self.project = {"id": "30000000-0000-0000-0000-000000000000", "name": "The Signal", "type": "Group"}
        self.recording_id = "40000000-0000-0000-0000-000000000000"
        self.album = {"id": FIXTURE_RELEASE, "title": "Night Work", "primary-type": "Album",
                      "artist-credit": [{"artist": self.band}]}

    def test_artist_lookup_accepts_an_exact_unambiguous_musicbrainz_alias(self):
        artist = {**self.person, "score": 100, "aliases": [{"name": "Ada Field"}]}
        metadata, transport = self.make_metadata(lambda _: {"artists": [artist]})
        result = ArtistConnections(metadata).artist_match("Ada Field")
        self.assertEqual(result["id"], self.person["id"])
        self.assertIn('alias:"Ada Field"', parse_qs(urlparse(transport.calls[0][0]).query)["query"][0])

    def test_credited_artist_leads_to_band_members_and_their_projects(self):
        edition_id = "70000000-0000-0000-0000-000000000000"
        edition = {"id": edition_id, "title": "Night Work", "status": "Official", "date": "2009-02-01"}
        empty_edition = {**edition, "id": "71000000-0000-0000-0000-000000000000", "date": "2009-01-01"}
        recording = {"id": self.recording_id, "title": "Window", "artist-credit": [{"artist": self.band}],
                     "releases": [{**item, "release-group": self.album} for item in (empty_edition, edition)]}
        band = {**self.band, "relations": [
            {"type": "member of band", "direction": "backward", "artist": self.person,
             "begin": "2004", "end": "2017", "ended": True, "attributes": ["guitar"]},
            {"type": "supporting musician", "direction": "backward", "artist": self.project},
        ]}
        solo = {"id": "50000000-0000-0000-0000-000000000000", "title": "On My Own", "primary-type": "Album",
                "artist-credit": [{"artist": self.person}], "secondary-types": ["Soundtrack"]}
        joint = {"id": "60000000-0000-0000-0000-000000000000", "title": "After Dark", "primary-type": "EP",
                 "artist-credit": [{"artist": self.person}, {"artist": self.project}]}
        person = {**self.person, "relations": [
            {"type": "member of band", "direction": "forward", "artist": self.project},
        ], "release-groups": [solo, joint]}
        pages = {
            "/ws/2/recording": {"recordings": [recording]},
            f"/ws/2/recording/{self.recording_id}": recording,
            f"/ws/2/release-group/{FIXTURE_RELEASE}": {**self.album, "releases": [empty_edition, edition]},
            f"/ws/2/release/{empty_edition['id']}": empty_edition,
            f"/ws/2/release/{edition_id}": {**edition, "relations": [{"type": "producer", "artist": self.person}]},
            f"/ws/2/artist/{self.band['id']}": band,
            f"/ws/2/artist/{self.person['id']}": person,
        }
        metadata, transport = self.make_metadata(lambda url: pages[urlparse(url).path])
        graph = ArtistConnections(metadata)
        local = record(1, self.band["name"], recording["title"], self.album["title"])
        song = graph.lookup("song", None, local)
        self.assertEqual(song["source"], "MusicBrainz")
        self.assertEqual([(edge["node"]["title"], edge["label"]) for edge in song["edges"]],
                         [("Bright Field", "Credited artist"), ("Night Work", "Appears on")])
        album = graph.lookup("album", None, local)
        self.assertEqual((album["root"]["kind"], album["root"]["id"]), ("release-group", FIXTURE_RELEASE))
        self.assertEqual([(edge["node"]["id"], edge["label"]) for edge in album["edges"]],
                         [(self.band["id"], "Credited artist"), (self.person["id"], "Producer")])
        self.assertEqual(graph.lookup("release-group", FIXTURE_RELEASE), album)
        band_graph = graph.lookup("artist", self.band["id"])
        self.assertEqual([(edge["node"]["title"], edge["label"], edge["note"]) for edge in band_graph["edges"]],
                         [("Ada Bell", "Former band member", "guitar · 2004–2017")])
        person_graph = graph.lookup("artist", self.person["id"])
        self.assertEqual({edge["node"]["title"]: edge["label"] for edge in person_graph["edges"]},
                         {"The Signal": "Member of", "On My Own": "Solo release", "After Dark": "Joint release"})
        previous_calls = len(transport.calls)
        self.assertEqual(graph.lookup("artist", self.person["id"]), person_graph)
        self.assertEqual(len(transport.calls), previous_calls)

    def test_non_latin_credits_match_only_the_same_titles(self):
        local = record(1, "宇多田ヒカル", "光")
        credit = [{"artist": {**self.person, "name": "宇多田ヒカル"}}]
        song = {"id": self.recording_id, "title": "光", "artist-credit": credit}
        metadata, _ = self.make_metadata(lambda url: song if urlparse(url).path.endswith(self.recording_id)
                                         else {"recordings": [song]})
        graph = ArtistConnections(metadata)
        self.assertEqual(graph.lookup("song", None, local)["root"]["title"], "光")
        self.assertFalse(graph.matches_recording(local, {**song, "title": "誓い"}))
        self.assertFalse(graph.matches_recording(local, {**song, "artist-credit": [{"artist": self.person}]}))

    def test_same_title_recordings_use_release_and_duration_or_remain_unmatched(self):
        studio = {"id": self.recording_id, "title": "Window", "length": 180000,
                  "artist-credit": [{"artist": self.band}], "releases": [{"title": "Night Work"}]}
        live = {**studio, "id": "80000000-0000-0000-0000-000000000000", "length": 210000,
                "releases": [{"title": "Concert Tape"}]}
        alternate = {**live, "id": "90000000-0000-0000-0000-000000000000",
                     "releases": [{"title": "Live Broadcast"}]}
        pages = {
            "/ws/2/recording": {"recordings": [studio, live, alternate]},
            f"/ws/2/recording/{live['id']}": live,
            f"/ws/2/artist/{self.band['id']}": {**self.band, "relations": [
                {"type": "member of band", "direction": "backward", "artist": self.person}]},
            "/ws/2/artist": {"artists": [{**self.band, "score": 100}]},
        }
        metadata, _ = self.make_metadata(lambda url: pages[urlparse(url).path])
        graph = ArtistConnections(metadata)
        local = record(1, self.band["name"], "Window", "Concert Tape", length=210)
        self.assertEqual(graph.lookup("song", None, local)["root"]["id"], live["id"])
        fallback = graph.lookup("song", None, {**local, "album": ""})
        self.assertEqual(fallback["root"]["id"], self.band["id"])
        self.assertEqual(fallback["edges"][0]["node"]["id"], self.person["id"])
        self.assertFalse(graph.matches_recording({**local, "isrc": "GBABC1000023"},
                                                 {**live, "isrcs": ["GBABC1000024"]}))

    def test_ambiguous_credits_stay_unmatched(self):
        wrong_recording = {"id": self.recording_id, "title": "Window", "artist-credit": [{"artist": self.person}]}
        wrong_album = {**self.album, "artist-credit": [{"artist": self.person}]}
        same_artist_name = {**self.band, "id": self.person["id"], "score": 95}
        pages = {
            "/ws/2/recording": {"recordings": [wrong_recording]},
            f"/ws/2/recording/{self.recording_id}": wrong_recording,
            "/ws/2/release-group": {"release-groups": [wrong_album]},
            "/ws/2/artist": {"artists": [{**self.band, "score": 100}, same_artist_name]},
        }
        metadata, _ = self.make_metadata(lambda url: pages[urlparse(url).path])
        graph = ArtistConnections(metadata)
        local = record(1, self.band["name"], "Window", self.album["title"])
        for context, title in (("song", "Window"), ("album", "Night Work")):
            self.assertEqual(graph.lookup(context, None, local),
                             {"state": "unmatched", "title": title, "artist": "Bright Field", "credit": None})


class CacheTests(MetadataTestCase):
    def test_cache_hit_avoids_the_network(self):
        calls = []

        def fetch():
            calls.append(1)
            return {"value": len(calls)}

        metadata, _ = self.make_metadata(lambda url: {})
        first = metadata.cached("key", 60, fetch)
        second = metadata.cached("key", 60, fetch)
        self.assertEqual(first, {"value": 1})
        self.assertEqual(second, {"value": 1})
        self.assertEqual(len(calls), 1)

    def test_concurrent_cache_misses_share_one_provider_result(self):
        clients = 5
        missed = threading.Barrier(clients)
        lookup = self.store.cache_get
        callers = threading.local()

        def aligned_lookup(key):
            row = lookup(key)
            if not getattr(callers, "looked_up", False):
                callers.looked_up = True
                missed.wait(timeout=5)
            return row

        metadata, transport = self.make_metadata(lambda url: {"value": ["song"]})
        with mock.patch.object(self.store, "cache_get", side_effect=aligned_lookup):
            with ThreadPoolExecutor(max_workers=clients) as pool:
                results = list(pool.map(
                    lambda _: metadata.cached("key", 60, lambda: metadata.musicbrainz("recording", {})),
                    range(clients)))
        self.assertEqual(results, [{"value": ["song"]}] * clients)
        self.assertEqual(len(transport.calls), 1)
        results[0]["value"].append("changed")
        self.assertEqual(results[1:], [{"value": ["song"]}] * (clients - 1))
        self.assertEqual(metadata.cached("key", 60, lambda: self.fail("cache miss")), {"value": ["song"]})

    def test_provider_failure_allows_a_later_cache_retry(self):
        metadata, _ = self.make_metadata(lambda url: {})

        def unavailable():
            raise OSError("provider unavailable")

        with self.assertRaisesRegex(OSError, "provider unavailable"):
            metadata.cached("key", 60, unavailable)
        self.assertEqual(metadata.cached("key", 60, lambda: {"ready": True}), {"ready": True})

    def test_concurrent_cache_misses_in_different_databases_stay_private(self):
        other_store = RequestsStore(Path(self.tmp.name) / "other.sqlite3")
        local = threading.local()
        metadata = ExternalMetadata(lambda: local.store)
        fetching = threading.Barrier(2)

        def load(store, title):
            local.store = store

            def fetch():
                fetching.wait(timeout=5)
                return {"title": title}

            return metadata.cached("key", 60, fetch)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(load, self.store, "First")
            second = pool.submit(load, other_store, "Second")
            self.assertEqual(first.result(), {"title": "First"})
            self.assertEqual(second.result(), {"title": "Second"})

    def test_cache_expiry_refetches(self):
        calls = []

        def fetch():
            calls.append(1)
            return {"value": len(calls)}

        metadata, _ = self.make_metadata(lambda url: {})
        self.assertEqual(metadata.cached("key", 60, fetch), {"value": 1})
        self.clock.advance(61)
        self.assertEqual(metadata.cached("key", 60, fetch), {"value": 2})
        self.assertEqual(len(calls), 2)

    def test_cache_storage_failure_still_serves_the_provider_result(self):
        metadata, _ = self.make_metadata(lambda url: {})
        offline = sqlite3.OperationalError("database unavailable")
        with mock.patch.object(self.store, "cache_get", side_effect=offline), \
                mock.patch.object(self.store, "cache_put", side_effect=offline):
            self.assertEqual(metadata.cached("test", 60, lambda: ["current song"]), ["current song"])

    def test_expired_rows_are_pruned_on_write(self):
        now = self.clock.now()
        self.store.cache_put("ancient", "old", now - 31 * 86400)
        self.store.cache_put("aging", "kept", now - 29 * 86400)
        metadata, _ = self.make_metadata(lambda url: {})
        metadata.cached("fresh", 60, lambda: {"value": 1})
        self.assertIsNone(self.store.cache_get("ancient"))
        self.assertEqual(self.store.cache_get("aging")["body"], "kept")
        self.assertIsNotNone(self.store.cache_get("fresh"))


class MusicBrainzTests(MetadataTestCase):
    def test_url_and_headers(self):
        metadata, transport = self.make_metadata(lambda url: {"ok": True})
        metadata.musicbrainz("recording", {"query": "x", "fmt": "json"})
        url, headers = transport.calls[0]
        self.assertTrue(url.startswith("https://musicbrainz.org/ws/2/recording?"))
        self.assertIn("query=x", url)
        self.assertEqual(headers["User-Agent"],
                         "ListeningRoom/1.2 (personal local music manager)")
        self.assertEqual(headers["Accept"], "application/json")

    def test_calls_are_paced_a_second_apart(self):
        metadata, _ = self.make_metadata(lambda url: {"ok": True})
        metadata.musicbrainz("recording", {"query": "a", "fmt": "json"})
        metadata.musicbrainz("recording", {"query": "b", "fmt": "json"})
        self.assertEqual(self.clock.sleeps, [1.1, 1.1])

    def test_a_slow_call_no_longer_blocks_the_next_lookup(self):
        # Plan 050 F1: the pacing lock used to be held across the
        # sleep, the request, and every retry, so one slow response
        # serialized all other lookups behind it. Only the start
        # slot is reserved under the lock now; requests overlap.
        entered = []
        release = threading.Event()

        def handler(url):
            entered.append(url)
            release.wait(10)
            return {"ok": True}

        metadata, _ = self.make_metadata(handler)
        results = {}

        def call(name, query):
            try:
                results[name] = metadata.musicbrainz(
                    "recording", {"query": query, "fmt": "json"})
            except Exception as error:  # surfaced by the asserts below
                results[name] = error

        first = threading.Thread(target=call, args=("first", "a"))
        second = threading.Thread(target=call, args=("second", "b"))
        first.start()
        deadline = time.monotonic() + 10
        while len(entered) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        second.start()
        while len(entered) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        # The second request entered while the first is still
        # waiting on its response.
        self.assertEqual(len(entered), 2)
        release.set()
        first.join(10)
        second.join(10)
        self.assertEqual(results, {"first": {"ok": True},
                                   "second": {"ok": True}})

    def test_429_is_retried_with_backoff_then_succeeds(self):
        attempts = []

        def handler(url):
            attempts.append(url)
            if len(attempts) < 3:
                raise http_error(429)
            return {"ok": True}

        metadata, _ = self.make_metadata(handler)
        self.assertEqual(metadata.musicbrainz("recording", {"query": "x", "fmt": "json"}),
                         {"ok": True})
        self.assertEqual(len(attempts), 3)
        self.assertIn(2, self.clock.sleeps)
        self.assertIn(3, self.clock.sleeps)

    def test_429_gives_up_after_three_attempts(self):
        attempts = []

        def handler(url):
            attempts.append(url)
            raise http_error(429)

        metadata, _ = self.make_metadata(handler)
        with self.assertRaises(urllib.error.HTTPError):
            metadata.musicbrainz("recording", {"query": "x", "fmt": "json"})
        self.assertEqual(len(attempts), 3)

    def test_other_http_errors_are_not_retried(self):
        attempts = []

        def handler(url):
            attempts.append(url)
            raise http_error(404)

        metadata, _ = self.make_metadata(handler)
        with self.assertRaises(urllib.error.HTTPError):
            metadata.musicbrainz("recording", {"query": "x", "fmt": "json"})
        self.assertEqual(len(attempts), 1)


def search_item(mbid, artist, title, album, score):
    return {"id": mbid, "title": title, "artist-credit": [{"name": artist}],
            "releases": [{"title": album}], "score": score}


SEARCH_ARTISTNAME = {"recordings": [
    search_item("s1", "Radio One", "First", "Al1", 90),
    search_item("s2", "Radio Two", "Second", "Al2", 80),
    search_item("s3", "radio one", "FIRST", "Al1", 70),
    search_item("s4", "Radio Three", "Third", "Al3", 60),
    search_item("s5", "Radio Four", "Fourth", "Al4", 50),
    search_item("s6", "Radio Five", "Fifth", "Al5", 40),
]}
SEARCH_RECORDING = {"recordings": [
    search_item("s7", "Radio Six", "Sixth", "Al6", 90),
    search_item("s2", "Radio Two", "Second", "Al2", 80),
    search_item("s8", "Radio Seven", "Seventh", "Al7", 70),
    search_item("s9", "Radio Eight", "Eighth", "Al8", 60),
    search_item("s10", "Radio Nine", "Ninth", "Al9", 50),
]}


def search_handler(url):
    query = parse_qs(urlparse(url).query)["query"][0]
    if query == 'artistname:"radio"':
        return SEARCH_ARTISTNAME
    if query == 'recording:"radio"':
        return SEARCH_RECORDING
    if query == 'artistname:"Radio Artist 03" AND recording:"Radio Song 03"':
        return {"recordings": [
            search_item("rec-03", "Radio Artist 03", "Radio Song 03", "Radio Album 03", 95)]}
    raise AssertionError("unexpected query: " + query)


class ExternalSearchTests(MetadataTestCase):
    def test_dedupe_and_eight_result_cap(self):
        metadata, _ = self.make_metadata(search_handler)
        results = metadata.external_search("radio", FakeCatalog([]))
        self.assertEqual([item["title"] for item in results],
                         ["First", "Second", "Third", "Fourth", "Fifth",
                          "Sixth", "Seventh", "Eighth"])
        self.assertEqual(len(results), 8)

    def test_catalog_match_is_annotated(self):
        metadata, _ = self.make_metadata(search_handler)
        catalog = FakeCatalog([
            record(1, "Someone", "Else"),
            record(2, "Radio Artist 03", "Radio Song 03", "Radio Album 03", 183),
        ])
        results = metadata.external_search("Radio Artist 03 - Radio Song 03", catalog)
        self.assertEqual(results, [{"artist": "Radio Artist 03", "title": "Radio Song 03",
                                    "album": "Radio Album 03", "musicbrainz_id": "rec-03",
                                    "score": 95, "catalog_id": 2, "status": "verified"}])

    def test_blank_query_never_calls_out(self):
        metadata, transport = self.make_metadata(search_handler)
        self.assertEqual(metadata.external_search("   ", FakeCatalog([])), [])
        self.assertEqual(transport.calls, [])

    def test_one_failed_search_falls_through_to_the_other(self):
        def handler(url):
            query = parse_qs(urlparse(url).query)["query"][0]
            if query == 'artistname:"radio"':
                raise http_error(500)
            return SEARCH_RECORDING

        metadata, _ = self.make_metadata(handler)
        results = metadata.external_search("radio", FakeCatalog([]))
        self.assertEqual([item["title"] for item in results],
                         ["Sixth", "Second", "Seventh", "Eighth", "Ninth"])

    def test_malformed_search_page_is_retried(self):
        for malformed in ([], {"recordings": None}, {"recordings": [None]}):
            with self.subTest(malformed=malformed):
                pages = iter([malformed, SEARCH_ARTISTNAME])
                metadata, transport = self.make_metadata(lambda url: next(pages))
                query = f"Artist - {malformed}"
                self.assertEqual(metadata.external_search(query, FakeCatalog([])), [])
                self.assertEqual(metadata.external_search(query, FakeCatalog([]))[0]["title"], "First")
                self.assertEqual(len(transport.calls), 2)

    def test_partial_search_retries_only_the_failed_query(self):
        def temporarily_unavailable(url):
            if 'artistname:' in parse_qs(urlparse(url).query)["query"][0]:
                raise http_error(500)
            return search_handler(url)

        metadata, transport = self.make_metadata(temporarily_unavailable)
        metadata.external_search("radio", FakeCatalog([]))
        transport.handler = search_handler
        results = metadata.external_search("radio", FakeCatalog([]))
        self.assertEqual(results[0]["title"], "First")
        self.assertEqual(len(transport.calls), 3)

    def test_search_results_are_cached_for_a_day(self):
        metadata, transport = self.make_metadata(search_handler)
        metadata.external_search("radio", FakeCatalog([]))
        calls = len(transport.calls)
        metadata.external_search("radio", FakeCatalog([]))
        self.assertEqual(len(transport.calls), calls)
        self.clock.advance(86401)
        metadata.external_search("radio", FakeCatalog([]))
        self.assertGreater(len(transport.calls), calls)

    def test_external_search_reuses_the_catalog_lookup(self):
        # Plan 018 (PERF-01 rider): the (artist, title) -> index map
        # is built once per catalog records list, not once per call.
        # Building it costs two normalize calls per record; matching
        # the one result item costs two more.
        metadata, _ = self.make_metadata(search_handler)
        catalog = FakeCatalog([
            record(1, "Someone", "Else"),
            record(2, "Radio Artist 03", "Radio Song 03", "Radio Album 03", 183),
        ])
        real_normalize = external_metadata_module.normalize_title
        calls = []

        def counting(value):
            calls.append(value)
            return real_normalize(value)

        with mock.patch.object(external_metadata_module, "normalize_title", counting):
            metadata.external_search("Radio Artist 03 - Radio Song 03", catalog)
            first = len(calls)
            metadata.external_search("Radio Artist 03 - Radio Song 03", catalog)
            second = len(calls) - first
            # A records-list replacement (a catalog reload) rebuilds.
            catalog.records = list(catalog.records)
            metadata.external_search("Radio Artist 03 - Radio Song 03", catalog)
            third = len(calls) - first - second
        self.assertEqual(first, 2 * 2 + 2)
        self.assertEqual(second, 2)
        self.assertEqual(third, first)


DETAILS_MBID = "12345678-1234-4234-9234-1234567890ab"
DETAILS_PAYLOAD = {
    "title": "Deep Song",
    "artist-credit": [{"name": "Deep Artist"}],
    "releases": [{"date": "2001-05-01"}, {"date": "1999-03-02"}, {}],
    "genres": [{"name": g} for g in ["g1", "g2", "g3", "g4", "g5", "g6", "g7"]],
    "relations": [
        {"type": "samples material", "direction": "forward",
         "recording": {"title": "Orig", "id": "r1", "artist-credit": [{"name": "Orig Artist"}]}},
        {"type": "samples material", "direction": "backward",
         "recording": {"title": "Sampler", "id": "r2", "artist-credit": [{"name": "Sampler Artist"}]}},
        {"type": "remix", "direction": "forward",
         "recording": {"title": "Remix Target", "id": "r3", "artist-credit": [{"name": "Remixer"}]}},
        {"type": "other version", "direction": "forward",
         "recording": {"title": "Ignored", "id": "r4", "artist-credit": [{"name": "Nobody"}]}},
        {"type": "remix", "direction": "forward", "recording": {}},
    ],
}
GOLDEN_DETAILS = {
    "musicbrainz_id": DETAILS_MBID,
    "recording_title": "Deep Song",
    "recording_artist": "Deep Artist",
    "first_release": "1999-03-02",
    "releases": 3,
    "genres": ["g1", "g2", "g3", "g4", "g5"],
    "connections": [
        {"relationship": "Samples", "title": "Orig", "artist": "Orig Artist",
         "musicbrainz_id": "r1"},
        {"relationship": "Sampled by", "title": "Sampler", "artist": "Sampler Artist",
         "musicbrainz_id": "r2"},
        {"relationship": "Remix of", "title": "Remix Target", "artist": "Remixer",
         "musicbrainz_id": "r3"},
    ],
    "musicbrainz_url": f"https://musicbrainz.org/recording/{DETAILS_MBID}",
}

META_MBID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
META_PAYLOAD = {"title": "Found Song", "artist-credit": [{"name": "Found Artist"}],
                "releases": [{"date": "2010-01-01", "title": "Found Album"}],
                "genres": [{"name": "jazz"}], "relations": []}


def details_handler(url):
    parsed = urlparse(url)
    if parsed.path == f"/ws/2/recording/{DETAILS_MBID}":
        return DETAILS_PAYLOAD
    if parsed.path == f"/ws/2/recording/{META_MBID}":
        return META_PAYLOAD
    if parsed.path == "/ws/2/recording":
        query = parse_qs(parsed.query)["query"][0]
        if query == "isrc:ISRC001":
            return {"recordings": [search_item(META_MBID, "Found Artist", "Found Song", "Found Album", 88)]}
        if query == 'recording:"Obscure Song" AND artist:"Obscure Artist"':
            return {"recordings": [search_item("low1", "Obscure Artist", "Obscure Song", "", 40)]}
        if query == 'recording:"Ghost Song" AND artist:"Ghost Artist"':
            return {"recordings": []}
    raise AssertionError("unexpected url: " + url)


class RecordingDetailsTests(MetadataTestCase):
    def test_details_payload_shape(self):
        metadata, _ = self.make_metadata(details_handler)
        self.assertEqual(metadata.recording_details(DETAILS_MBID), GOLDEN_DETAILS)

    def test_invalid_mbid_is_rejected_without_a_call(self):
        metadata, transport = self.make_metadata(details_handler)
        with self.assertRaises(ValueError):
            metadata.recording_details("not-an-mbid")
        self.assertEqual(transport.calls, [])

    def test_details_are_cached_for_thirty_days(self):
        metadata, transport = self.make_metadata(details_handler)
        metadata.recording_details(DETAILS_MBID)
        calls = len(transport.calls)
        metadata.recording_details(DETAILS_MBID)
        self.assertEqual(len(transport.calls), calls)
        self.clock.advance(30 * 86401)
        metadata.recording_details(DETAILS_MBID)
        self.assertGreater(len(transport.calls), calls)


class TrackMetadataTests(MetadataTestCase):
    def catalog(self):
        return FakeCatalog([
            record(1, "Any Artist", "Any Song", "Any Album", 210, isrc="ISRC001", key="m1"),
            record(2, "Obscure Artist", "Obscure Song", "Obscure Album", 150, key="m2"),
            record(3, "Ghost Artist", "Ghost Song", "", 0, key="m3"),
        ])

    def test_isrc_match_merges_musicbrainz_details(self):
        metadata, _ = self.make_metadata(details_handler)
        self.assertEqual(metadata.track_metadata(1, self.catalog()), {
            "title": "Any Song", "artist": "Any Artist", "album": "Any Album",
            "duration": 210, "isrc": "ISRC001", "identifier": "", "spotify_url": "", "provenance": "",
            "lists": [], "source": "MusicBrainz + portable list",
            "musicbrainz_id": META_MBID, "recording_title": "Found Song",
            "recording_artist": "Found Artist", "first_release": "2010-01-01",
            "releases": 1, "genres": ["jazz"], "connections": [],
            "musicbrainz_url": f"https://musicbrainz.org/recording/{META_MBID}"})

    def test_low_score_match_stays_local(self):
        metadata, _ = self.make_metadata(details_handler)
        self.assertEqual(metadata.track_metadata(2, self.catalog()), {
            "title": "Obscure Song", "artist": "Obscure Artist", "album": "Obscure Album",
            "duration": 150, "isrc": "", "identifier": "", "spotify_url": "", "provenance": "",
            "lists": [], "source": "Portable music list"})

    def test_provenance_comes_from_the_tracks_identifier(self):
        metadata, _ = self.make_metadata(details_handler)
        catalog = FakeCatalog(
            [record(1, "Any Artist", "Any Song", "Any Album", 210, isrc="ISRC001",
                    key="m1", uri="spotify:track:4uLU6hMCjMI75M1A2tKUQC")],
            memberships={1: ["liked", "road"]}, list_names={"liked": "Liked songs"})
        result = metadata.track_metadata(1, catalog)
        self.assertEqual(result["identifier"], "spotify:track:4uLU6hMCjMI75M1A2tKUQC")
        self.assertEqual(result["provenance"], "Spotify")
        self.assertEqual(result["lists"], ["Liked songs", "road"])

    def test_not_found_result_is_cached(self):
        metadata, transport = self.make_metadata(details_handler)
        metadata.track_metadata(3, self.catalog())
        calls = len(transport.calls)
        again = metadata.track_metadata(3, self.catalog())
        self.assertEqual(again["source"], "Portable music list")
        self.assertEqual(len(transport.calls), calls)

    def test_lookup_failure_stays_local(self):
        def handler(url):
            raise http_error(503)

        metadata, _ = self.make_metadata(handler)
        result = metadata.track_metadata(1, self.catalog())
        self.assertEqual(result["source"], "Portable music list")
        self.assertNotIn("musicbrainz_id", result)


class DiscoverTests(MetadataTestCase):
    def test_golden_payload_for_a_fixed_week_and_catalog(self):
        metadata, _ = self.make_metadata(discover_handler)
        self.assertEqual(metadata.discover_weekly(discover_catalog(), "/golden"),
                         GOLDEN_DISCOVER)

    def test_deterministic_across_instances(self):
        first, _ = self.make_metadata(discover_handler)
        payload = first.discover_weekly(discover_catalog(), "/golden")
        other_store = RequestsStore(Path(self.tmp.name) / "other.sqlite3")
        second = ExternalMetadata(lambda: other_store,
                                  transport=FakeTransport(discover_handler),
                                  now=self.clock.now, monotonic=self.clock.monotonic,
                                  sleep=self.clock.sleep,
                                  today=lambda: datetime.date(2026, 11, 16),
                                  utcnow=lambda: PINNED_NOW)
        self.assertEqual(second.discover_weekly(discover_catalog(), "/golden"), payload)

    def test_catalog_tracks_are_excluded_and_cap_is_twelve(self):
        metadata, _ = self.make_metadata(discover_handler)
        payload = metadata.discover_weekly(discover_catalog(), "/golden")
        ids = [track["musicbrainz_id"] for track in payload["tracks"]]
        self.assertEqual(len(ids), 12)
        self.assertNotIn("rec-01", ids)  # already in the catalog
        self.assertNotIn("rec-02", ids)  # lookup failed, skipped
        self.assertEqual(len(set(ids)), 12)

    def test_weekly_result_is_cached(self):
        metadata, transport = self.make_metadata(discover_handler)
        metadata.discover_weekly(discover_catalog(), "/golden")
        calls = len(transport.calls)
        again = metadata.discover_weekly(discover_catalog(), "/golden")
        self.assertEqual(again, GOLDEN_DISCOVER)
        self.assertEqual(len(transport.calls), calls)

    def test_status_serves_the_cached_week_without_building(self):
        metadata, transport = self.make_metadata(discover_handler)
        metadata.discover_weekly(discover_catalog(), "/golden")
        calls = len(transport.calls)
        status = metadata.discover_status(discover_catalog(), "/golden")
        self.assertEqual(status, GOLDEN_DISCOVER)
        self.assertEqual(len(transport.calls), calls)

    def test_status_builds_in_the_background_then_serves(self):
        metadata, _ = self.make_metadata(discover_handler)
        status = metadata.discover_status(discover_catalog(), "/golden")
        self.assertEqual(status, {"week": "2026-W47", "status": "building", "tracks": [],
                                  "source": "ListenBrainz radio + MusicBrainz"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = metadata.discover_status(discover_catalog(), "/golden")
            if status.get("tracks"):
                break
            time.sleep(0.01)
        self.assertEqual(status, GOLDEN_DISCOVER)

    def test_failure_backoff_window(self):
        def handler(url):
            raise TypeError("malformed upstream payload")

        metadata, _ = self.make_metadata(handler)
        status = metadata.discover_status(discover_catalog(), "/golden")
        self.assertEqual(status["status"], "building")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = metadata.discover_status(discover_catalog(), "/golden")
            if status.get("status") == "error":
                break
            time.sleep(0.01)
        self.assertEqual(status, {"week": "2026-W47", "status": "error", "tracks": [],
                                  "source": "ListenBrainz radio + MusicBrainz"})
        self.clock.advance(299)
        self.assertEqual(metadata.discover_status(discover_catalog(), "/golden")["status"],
                         "error")
        self.clock.advance(2)
        self.assertEqual(metadata.discover_status(discover_catalog(), "/golden")["status"],
                         "building")
        # Drain the rebuild the last call started, so no build thread
        # outlives the test's temp workspace.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if metadata.discover_status(discover_catalog(), "/golden")["status"] == "error":
                break
            time.sleep(0.01)


if __name__ == "__main__":
    unittest.main()
