import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import contextlib
import http.client
import io
import json
import random
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from fixture_environment import isolate_room_environment as setUpModule
from runner_fixture import runner
import server
from catalog.audio_store import MediaStore, blob_ref
from catalog.catalog_store import CatalogStore
from catalog.genres import GenreStore, canonical_genre, categorize, group_genre_flow, missing_genre_records
from downloads.requests_store import RequestsStore
from library.library_protocol import write_xspf
from metadata.external_metadata import ExternalMetadata
from playback.playback import PlaybackSession


def record(index, title, artist=None, key=None, isrc=""):
    return {"index": index, "key": key or f"key-{index}", "uri": f"spotify:track:{index}",
            "artist": artist or f"Artist {index}", "title": title, "album": "Album",
            "length": 200, "isrc": isrc, "first_playlist": "mix", "active": True}


class AliasTests(unittest.TestCase):
    """Representative raw strings land in the expected canonical
    buckets, including case and punctuation variants."""

    def test_alias_mapping(self):
        cases = {
            "Techno": "techno", "TECHNO": "techno", "Detroit Techno": "techno",
            "detroit-techno": "techno", "Acid Techno": "techno",
            "Deep House": "house", "tech house": "house", "House Music": "house",
            "Hip-Hop": "hip-hop", "hip hop": "hip-hop", "HIP  HOP": "hip-hop",
            "Drum & Bass": "drum and bass", "DnB": "drum and bass",
            "Rock; Pop": "rock", "Progressive Rock": "rock", "rock & roll": "rock",
            "R&B": "r&b", "Rhythm and Blues": "r&b", "Neo-Soul": "r&b",
            "K-Pop": "pop", "Synthpop": "pop",
            "Jazz Fusion": "jazz", "Bebop": "jazz",
            "Zydeco": "zydeco",  # unmapped: its own normalized form
            "": "", "  ": "", None: "",
        }
        for raw, expected in cases.items():
            self.assertEqual(canonical_genre(raw), expected, raw)


class GenreStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "state" / "requests.sqlite3"

    def test_round_trip_and_canonicalization(self):
        store = GenreStore(self.path)
        entry = store.set("key-1", "Detroit Techno", "file")
        self.assertEqual(entry, {"genre": "Detroit Techno", "canonical": "techno",
                                 "source": "file"})
        reopened = GenreStore(self.path)
        self.assertEqual(reopened.get("key-1"), entry)
        self.assertIsNone(reopened.get("key-2"))

    def test_corrupt_store_reads_as_empty(self):
        # A torn legacy genres.json imports as an empty store.
        backup = self.path.parent / "state-backup"
        backup.mkdir(parents=True)
        (backup / "genres.json").write_text("{torn", encoding="utf-8")
        self.assertEqual(GenreStore(self.path).load(), {})

    def test_empty_genre_is_not_stored(self):
        store = GenreStore(self.path)
        self.assertIsNone(store.set("key-1", "  ", "file"))
        self.assertEqual(store.load(), {})


class FakeMetadata:
    """Counts lookups; serves canned genres per record key."""

    def __init__(self, musicbrainz=None, listenbrainz=None):
        self.musicbrainz = musicbrainz or {}
        self.listenbrainz = listenbrainz or {}
        self.calls = []

    def musicbrainz_genre(self, record):
        self.calls.append(("musicbrainz", record["key"]))
        return self.musicbrainz.get(record["key"], "")

    def listenbrainz_genre(self, record):
        self.calls.append(("listenbrainz", record["key"]))
        return self.listenbrainz.get(record["key"], "")


class CategorizeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = GenreStore(Path(self.folder.name) / "requests.sqlite3")
        self.records = [record(1, "One"), record(2, "Two"), record(3, "Three")]

    def test_file_tag_beats_musicbrainz(self):
        metadata = FakeMetadata(musicbrainz={"key-1": "Rock"})
        filled = categorize(self.records[:1], self.store,
                            media_for=lambda r: {"genre": "Detroit Techno"},
                            metadata=metadata)
        self.assertEqual(filled, 1)
        entry = self.store.get("key-1")
        self.assertEqual((entry["genre"], entry["canonical"], entry["source"]),
                         ("Detroit Techno", "techno", "file"))
        self.assertEqual(metadata.calls, [])

    def test_musicbrainz_beats_listenbrainz(self):
        metadata = FakeMetadata(musicbrainz={"key-2": "Jazz Fusion"},
                                listenbrainz={"key-2": "Bebop"})
        categorize(self.records[1:2], self.store, media_for=lambda r: None,
                   metadata=metadata)
        entry = self.store.get("key-2")
        self.assertEqual((entry["canonical"], entry["source"]), ("jazz", "musicbrainz"))
        self.assertEqual(metadata.calls, [("musicbrainz", "key-2")])

    def test_listenbrainz_is_the_fallback(self):
        metadata = FakeMetadata(listenbrainz={"key-3": "Bebop"})
        categorize(self.records[2:], self.store, media_for=lambda r: {"genre": ""},
                   metadata=metadata)
        entry = self.store.get("key-3")
        self.assertEqual((entry["canonical"], entry["source"]), ("jazz", "listenbrainz"))
        self.assertEqual(metadata.calls, [("musicbrainz", "key-3"),
                                          ("listenbrainz", "key-3")])

    def test_manual_correction_wins_forever(self):
        self.store.set("key-1", "Techno", "manual")
        lookups = []
        metadata = FakeMetadata(musicbrainz={"key-1": "Rock"})
        filled = categorize(self.records[:1], self.store,
                            media_for=lambda r: lookups.append("media") or {"genre": "Rock"},
                            metadata=metadata)
        self.assertEqual(filled, 0)
        self.assertEqual(lookups, [])          # never even probed
        self.assertEqual(metadata.calls, [])   # never looked up
        self.assertEqual(self.store.get("key-1")["genre"], "Techno")
        self.assertEqual(self.store.get("key-1")["source"], "manual")

    def test_mid_scan_manual_edit_wins_and_provider_failure_counts_as_unavailable(self):
        def probe(song):
            if song == self.records[0]:
                GenreStore(self.store.database).set(song["key"], "Jazz", "manual")
                return {"genre": "Rock"}
        metadata = FakeMetadata()
        metadata.musicbrainz_genre = mock.Mock(side_effect=OSError("offline"))
        reports = []
        categorize(self.records[:2], self.store, media_for=probe, metadata=metadata,
                   on_progress=lambda *counters: reports.append(counters))
        self.assertEqual(reports[-1], (2, 2, 0, 1))
        self.assertEqual(self.store.reload()["key-1"]["source"], "manual")

    def test_inactive_and_already_tagged_linked_records_are_not_checked(self):
        self.store.set("key-1", "Jazz", "manual")
        self.records[0].update(active=False, linked_recording="same")
        self.records[1]["linked_recording"] = "same"
        checked = []
        categorize(self.records, self.store, media_for=lambda song: checked.append(song["key"]))
        self.assertEqual(checked, ["key-3"])

    def test_linked_siblings_pick_up_a_genre_discovered_during_the_pass(self):
        songs = self.records[:2]
        for song in songs:
            song["linked_recording"] = "linked"
        probe = mock.Mock(return_value={"genre": "Jazz"})
        self.assertEqual(categorize(songs, self.store, media_for=probe), 1)
        probe.assert_called_once_with(songs[0])
        self.assertEqual(missing_genre_records(songs, self.store.reload()), [])

    def test_second_bulk_run_performs_zero_lookups(self):
        metadata = FakeMetadata(musicbrainz={"key-2": "Rock", "key-3": "Pop"})
        media_calls = []

        def media_for(r):
            media_calls.append(r["key"])
            return {"genre": "Techno"} if r["key"] == "key-1" else None

        first = categorize(self.records, self.store, media_for=media_for, metadata=metadata)
        self.assertEqual(first, 3)
        media_calls.clear()
        metadata.calls.clear()
        second = categorize(self.records, self.store, media_for=media_for, metadata=metadata)
        self.assertEqual(second, 0)
        self.assertEqual(media_calls, [])
        self.assertEqual(metadata.calls, [])

    def test_uncategorizable_record_stays_a_candidate(self):
        metadata = FakeMetadata()
        self.assertEqual(categorize(self.records[:1], self.store,
                                    media_for=lambda r: None, metadata=metadata), 0)
        self.assertIsNone(self.store.get("key-1"))


# ---------------------------------------------------------------------------
# Metadata module genre lookups, driven by a fake transport.
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.value = 1_000_000.0
        self.mono = 0.0

    def now(self):
        return self.value

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        self.value += seconds
        self.mono += seconds


class MetadataGenreTests(unittest.TestCase):
    MBID = "12345678-1234-1234-1234-123456789012"

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = RequestsStore(Path(self.folder.name) / "requests.sqlite3")
        self.clock = FakeClock()
        self.calls = []

    def build(self, handler):
        def transport(url, headers):
            self.calls.append(url)
            return handler(url)
        return ExternalMetadata(lambda: self.store, transport=transport,
                                now=self.clock.now, monotonic=self.clock.monotonic,
                                sleep=self.clock.sleep)

    def search_payload(self, score=90):
        return {"recordings": [{"id": self.MBID, "score": score, "title": "One",
                                "artist-credit": [{"name": "Artist 1", "artist": {"name": "Artist 1"}}]}]}

    def test_musicbrainz_genre_from_recording_details(self):
        def handler(url):
            if "recording/" + self.MBID in url:
                return {"title": "One", "artist-credit": [{"name": "Artist 1"}],
                        "genres": [{"name": "Techno"}, {"name": "House"}],
                        "releases": []}
            return self.search_payload()
        metadata = self.build(handler)
        self.assertEqual(metadata.musicbrainz_genre(record(1, "One")), "Techno")
        calls = len(self.calls)
        self.assertEqual(metadata.musicbrainz_genre(record(1, "One")), "Techno")
        self.assertEqual(len(self.calls), calls)  # cached, no new lookups

    def test_musicbrainz_genre_needs_a_confident_match(self):
        metadata = self.build(lambda url: self.search_payload(score=40))
        self.assertEqual(metadata.musicbrainz_genre(record(1, "One")), "")

    def test_expired_misses_retry_and_fall_back_from_isrc_to_title_and_artist(self):
        song = record(1, "One", isrc="USABC1200001")
        for key in ("genre:v2:key-1", "genre-match:v2:key-1"):
            self.store.cache_put(key, '""', self.clock.now() - 86400)

        def handler(url):
            if f"recording/{self.MBID}" in url:
                return {"genres": [{"name": "Techno"}]}
            if "isrc" in url:
                return {"recordings": []}
            payload = self.search_payload()
            payload["recordings"].insert(0, {**payload["recordings"][0], "title": "Another Song",
                                            "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"})
            return payload

        metadata = self.build(handler)
        self.assertEqual(metadata.musicbrainz_genre(song), "Techno")
        self.assertEqual(len(self.calls), 3)
        self.assertIn("isrc", self.calls[0])
        self.assertIn("artist", self.calls[1])

    def test_legacy_match_and_genre_require_new_match_and_missing_details_refresh(self):
        song = record(1, "One")
        self.store.cache_put("genre-match:key-1", '"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"', self.clock.now())
        self.store.cache_put("genre:key-1", '"Wrong genre"', self.clock.now())
        genres = []

        def handler(url):
            if "query=" in url:
                return self.search_payload()
            self.assertIn(self.MBID, url)
            return {"genres": genres}

        metadata = self.build(handler)
        self.assertEqual(metadata.musicbrainz_genre(song), "")
        self.assertEqual(len(self.calls), 2)
        genres.append({"name": "Techno"})
        self.assertEqual(metadata.musicbrainz_genre(song), "")
        self.assertEqual(len(self.calls), 2)
        self.clock.value += 901
        self.assertEqual(metadata.musicbrainz_genre(song), "Techno")
        self.assertEqual(len(self.calls), 3)
        self.clock.value += 901
        self.assertEqual(metadata.musicbrainz_genre(song), "Techno")
        self.assertEqual(len(self.calls), 3)

    def test_listenbrainz_genre_reads_the_entry_tags(self):
        def handler(url):
            if "listenbrainz" in url:
                self.assertIn("inc=tag", url)
                return {self.MBID: {"tag": {
                    "recording": [{"tag": "favorite", "count": 20},
                                  {"tag": "acid", "count": 3, "genre_mbid": self.MBID}],
                    "release_group": [{"tag": "rock", "count": 10, "genre_mbid": self.MBID}]}}}
            return self.search_payload()
        metadata = self.build(handler)
        self.assertEqual(metadata.listenbrainz_genre(record(1, "One")), "acid")

    def test_listenbrainz_retries_unavailable_and_reads_release_group_genre(self):
        replies = iter([OSError("offline"), {self.MBID: {"tag": {"release_group": [
            {"tag": "rock", "count": 2, "genre_mbid": self.MBID}]}}}])
        def handler(url):
            if "listenbrainz" not in url:
                return self.search_payload()
            response = next(replies)
            if isinstance(response, Exception):
                raise response
            return response
        metadata = self.build(handler)
        song = record(1, "One")
        with self.assertRaises(OSError):
            metadata.listenbrainz_genre(song)
        self.assertEqual(metadata.listenbrainz_genre(song), "rock")
        self.assertEqual(metadata.listenbrainz_genre(song), "rock")

    def test_listenbrainz_genre_without_tags_is_empty(self):
        def handler(url):
            if "listenbrainz" in url:
                return {self.MBID: {"recording": {"name": "One"}}}
            return self.search_payload()
        metadata = self.build(handler)
        self.assertEqual(metadata.listenbrainz_genre(record(1, "One")), "")

    def test_listenbrainz_fills_artist_only_genres_for_single_artist_recordings(self):
        artist_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        artists = [{"artist_mbid": artist_id}]

        def handler(url):
            if "listenbrainz" not in url:
                return self.search_payload()
            self.assertIn("inc=tag+artist", url)
            return {self.MBID: {"artist": {"artists": artists}, "tag": {"artist": [
                {"artist_mbid": artist_id, "tag": "favorite", "count": 12},
                {"artist_mbid": self.MBID, "tag": "house", "count": 5, "genre_mbid": self.MBID},
                {"artist_mbid": artist_id, "tag": "techno", "count": 3, "genre_mbid": self.MBID},
            ]}}}

        song = record(1, "One")
        self.store.cache_put("listenbrainz-genre:v2:" + self.MBID, '""', self.clock.now())
        metadata = self.build(handler)
        self.assertEqual(metadata.listenbrainz_genre(song), "techno")
        artists.append({"artist_mbid": self.MBID})
        self.clock.value += 31 * 86400
        self.assertEqual(metadata.listenbrainz_genre(song), "")


# ---------------------------------------------------------------------------
# Runner integration: prepare merge, bulk mode.
# ---------------------------------------------------------------------------

class RunnerGenreTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.library = self.root / "audio"
        self.library.mkdir()
        self.playlists_dir = self.root / "playlists"
        self.playlists_dir.mkdir()
        self.sources = self.root / "lists"
        self.sources.mkdir()
        runner._probe_cache = None
        runner._probe_cache_dirty = False
        runner._REPORT_DIRTY = False
        self.addCleanup(self._reset_caches)
        patchers = [
            mock.patch.object(runner, "ROOT", self.root),
            mock.patch.object(runner, "resolve_config_path", lambda: self.root / "settings.json"),
            mock.patch.object(runner, "STATE", self.state),
            mock.patch.object(runner, "LIBRARY", self.library),
            mock.patch.object(runner, "PLAYLISTS", self.playlists_dir),
            mock.patch.object(runner, "OUTPUTS", self.root / "reports"),
            mock.patch.object(runner, "SOURCES", self.sources),
            mock.patch.object(runner, "MASTER", self.state / "master.csv"),
            mock.patch.object(runner, "REQUESTS_DB", self.state / "requests.sqlite3"),
            mock.patch.object(runner, "free_bytes", lambda: 100 * 1024**3),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _reset_caches(self):
        runner._probe_cache = None
        runner._probe_cache_dirty = False
        runner._REPORT_DIRTY = False

    def write_manifest(self, records, playlists=None):
        payload = {"records": records, "playlists": playlists or {},
                   "issues": [], "source_signature": {}}
        CatalogStore(self.state / "requests.sqlite3").write_build(payload)

    def store(self):
        return GenreStore(self.state / "requests.sqlite3")


class PrepareMergeTests(RunnerGenreTestCase):
    def write_source(self):
        tracks = [{"title": "One", "artist": "Artist 1", "album": "Album",
                   "duration_ms": 200000, "identifier": "spotify:track:1"}]
        (self.sources / "mix.xspf").write_text(write_xspf("Mix", tracks), encoding="utf-8")

    def test_genre_survives_prepare_reruns(self):
        self.write_source()
        with contextlib.redirect_stdout(io.StringIO()):
            runner.prepare()
        payload = runner.catalog_store().read_build()
        key = payload["records"][0]["key"]
        self.store().set(key, "Detroit Techno", "file")
        with contextlib.redirect_stdout(io.StringIO()):
            runner.prepare()
            runner.prepare()
        payload = runner.catalog_store().read_build()
        merged = payload["records"][0]
        self.assertEqual(merged["genre"], "Detroit Techno")
        self.assertEqual(merged["genre_canonical"], "techno")
        self.assertEqual(merged["genre_source"], "file")
        self.assertEqual(self.store().get(key)["genre"], "Detroit Techno")

    def test_manual_correction_survives_prepare_and_a_changed_file_tag(self):
        self.write_source()
        with contextlib.redirect_stdout(io.StringIO()):
            runner.prepare()
        payload = runner.catalog_store().read_build()
        key = payload["records"][0]["key"]
        self.store().set(key, "Techno", "manual")
        # The file is retagged Rock and a bulk-style pass runs: the
        # manual entry is not a candidate, so nothing overwrites it.
        filled = categorize(payload["records"], self.store(),
                            media_for=lambda r: {"genre": "Rock"},
                            metadata=FakeMetadata(musicbrainz={key: "Rock"}))
        self.assertEqual(filled, 0)
        with contextlib.redirect_stdout(io.StringIO()):
            runner.prepare()
        payload = runner.catalog_store().read_build()
        self.assertEqual(payload["records"][0]["genre"], "Techno")
        self.assertEqual(payload["records"][0]["genre_source"], "manual")


class BulkModeTests(RunnerGenreTestCase):
    def test_bulk_fills_missing_and_a_second_run_looks_up_nothing(self):
        records = [record(1, "One"), record(2, "Two")]
        self.write_manifest(records, playlists={"mix": [1, 2]})
        metadata = FakeMetadata(musicbrainz={"key-2": "Rock"})
        probes = []

        def fake_probe(path):
            probes.append(str(path))
            genre = "Techno" if path.name.startswith("1 -") else ""
            return {"duration": 200.0, "bitrate": 320000, "title": "One", "genre": genre}

        with mock.patch.object(runner, "verified_existing",
                               lambda index, rec: self.library / f"{index} - x.mp3"), \
                mock.patch.object(runner, "cached_probe", fake_probe), \
                mock.patch.object(runner, "external_metadata", lambda: metadata):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                runner.run_genres()
            summary = json.loads(out.getvalue().strip().splitlines()[-1])
            self.assertEqual(summary["categorized"], 2)
            self.assertEqual(self.store().get("key-1")["source"], "file")
            self.assertEqual(self.store().get("key-2")["source"], "musicbrainz")
            probes.clear()
            metadata.calls.clear()
            with contextlib.redirect_stdout(io.StringIO()) as out:
                runner.run_genres()
            summary = json.loads(out.getvalue().strip().splitlines()[-1])
            self.assertEqual(summary["categorized"], 0)
            self.assertEqual(summary["already_categorized"], 2)
        self.assertEqual(probes, [])
        self.assertEqual(metadata.calls, [])


class BlobGenreTests(RunnerGenreTestCase):
    """Plan 063 settlement 1: a blob-backed record's stored probe
    feeds the bulk pass exactly as a file's probe does. The room
    is seeded the way plan 060 leaves it: the bytes and their
    landing probe in the media database, the coverage row naming
    the blob reference, no file in the library."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(runner, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed_blob_track(self, genre="Detroit Techno"):
        rec = record(1, "One")
        self.write_manifest([rec], playlists={"mix": [1]})
        source = self.root / "seed.mp3"
        source.write_bytes(b"fake mp3 audio bytes for plan 063 tests" * 8)
        probe = {"duration": 200.0, "bitrate": 320000, "title": "One",
                 "artist": "Artist 1", "album": "Album", "genre": genre}
        key = MediaStore(self.root / "media.sqlite3").put_file(source, probe)
        runner.catalog_store().write_coverage([{
            "index": 1, "status": "verified", "artist": rec["artist"],
            "title": rec["title"], "uri": rec["uri"],
            "path": blob_ref(key), "reason": ""}])
        return rec

    def test_a_blob_backed_record_categorizes_from_its_stored_probe(self):
        self.seed_blob_track()
        metadata = FakeMetadata(musicbrainz={"key-1": "Rock"})
        probed = []

        def fake_cached_probe(path):
            probed.append(str(path))
            return None

        with mock.patch.object(runner, "cached_probe", fake_cached_probe), \
                mock.patch.object(runner, "external_metadata",
                                  lambda: metadata):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                runner.run_genres()
        summary = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(summary["categorized"], 1)
        entry = self.store().get("key-1")
        self.assertEqual((entry["genre"], entry["canonical"],
                          entry["source"]),
                         ("Detroit Techno", "techno", "file"))
        # The stored probe answered first: no network lookup ran,
        # and no file probe was attempted against the reference.
        self.assertEqual(metadata.calls, [])
        self.assertEqual(probed, [])

    def test_a_blob_backed_record_without_a_tag_falls_to_network(self):
        self.seed_blob_track(genre="")
        metadata = FakeMetadata(musicbrainz={"key-1": "Jazz Fusion"})
        with mock.patch.object(runner, "external_metadata",
                               lambda: metadata):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                runner.run_genres()
        summary = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(summary["categorized"], 1)
        entry = self.store().get("key-1")
        self.assertEqual((entry["genre"], entry["source"]),
                         ("Jazz Fusion", "musicbrainz"))
        self.assertEqual(metadata.calls, [("musicbrainz", "key-1")])


# ---------------------------------------------------------------------------
# Genre flow: the order assembly and the session behavior.
# ---------------------------------------------------------------------------

class GenreFlowOrderTests(unittest.TestCase):
    def test_same_genre_first_then_the_rest(self):
        genres = {1: "techno", 2: "rock", 3: "techno", 4: "jazz", 5: "techno"}
        ordered = group_genre_flow([2, 3, 4, 5], "techno", genres.get,
                                   shuffle=lambda items: None)
        self.assertEqual(ordered, [3, 5, 2, 4])

    def test_no_current_genre_is_one_shuffled_pool(self):
        ordered = group_genre_flow([1, 2, 3], "", lambda ref: "techno",
                                   shuffle=lambda items: items.reverse())
        self.assertEqual(ordered, [3, 2, 1])


class GenreFlowSessionTests(unittest.TestCase):
    GENRES = {1: "techno", 2: "rock", 3: "techno", 4: "jazz", 5: "techno", 6: "rock"}

    def build(self, with_hook=True):
        tracks = {index: {"id": index, "title": str(index)} for index in self.GENRES}
        order = None
        if with_hook:
            order = lambda refs, current: group_genre_flow(
                refs, self.GENRES.get(current, ""), self.GENRES.get,
                shuffle=lambda items: None)
        return PlaybackSession(lambda playlist: [1, 2, 3, 4, 5, 6], tracks.get,
                               now=lambda: 100.0, genre_order=order)

    def upcoming_ids(self, session):
        return [track["id"] for track in session.snapshot()["upcoming"]]

    def test_genre_flow_stays_in_genre_until_the_pool_empties(self):
        session = self.build()
        session.apply({"op": "genre_flow"})
        session.apply({"op": "play", "playlist": "all", "track_id": 1})
        self.assertEqual(self.upcoming_ids(session), [3, 5, 2, 4, 6])
        session.apply({"op": "next"})
        self.assertEqual(session.snapshot()["current_ref"], 3)
        session.apply({"op": "next"})
        self.assertEqual(session.snapshot()["current_ref"], 5)
        session.apply({"op": "next"})  # genre pool dry: full pool fallback
        self.assertEqual(session.snapshot()["current_ref"], 2)
        self.assertTrue(session.snapshot()["genre_flow"])

    def test_genre_flow_toggles_on_mid_session(self):
        session = self.build()
        session.apply({"op": "play", "playlist": "all", "track_id": 1})
        self.assertEqual(self.upcoming_ids(session), [2, 3, 4, 5, 6])
        session.apply({"op": "genre_flow"})
        self.assertEqual(self.upcoming_ids(session), [3, 5, 2, 4, 6])

    def test_off_mode_matches_plain_shuffle(self):
        session = self.build()
        session.apply({"op": "shuffle"})
        expected = [2, 3, 4, 5, 6]
        random.seed(42)
        random.shuffle(expected)
        random.seed(42)
        session.apply({"op": "play", "playlist": "all", "track_id": 1})
        self.assertEqual(self.upcoming_ids(session), expected)
        self.assertFalse(session.snapshot()["genre_flow"])

    def test_genre_flow_without_an_order_hook_is_a_flag_only(self):
        session = self.build(with_hook=False)
        session.apply({"op": "genre_flow"})
        session.apply({"op": "play", "playlist": "all", "track_id": 1})
        self.assertEqual(self.upcoming_ids(session), [2, 3, 4, 5, 6])
        self.assertTrue(session.snapshot()["genre_flow"])


# ---------------------------------------------------------------------------
# HTTP: tracks filter/search/facet, the manual correction, dialog fields.
# ---------------------------------------------------------------------------

class PresentedClientServer(ThreadingHTTPServer):
    daemon_threads = True
    presented_client = None

    def get_request(self):
        request, address = super().get_request()
        if self.presented_client:
            return request, (self.presented_client, address[1])
        return request, address


class HttpGenreTests(unittest.TestCase):
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
        state = cls.workspace / "state"
        state.mkdir(parents=True, exist_ok=True)
        records = [record(1, "Techno Song"), record(2, "Rock Song"), record(3, "Quiet Song")]
        manifest = {"records": records, "playlists": {"mix": [1, 2, 3]}, "issues": [],
                    "covers": {}, "list_names": {}}
        database = server.DB
        store = CatalogStore(database)
        store.write_build(manifest)
        store.write_coverage(
            [{"index": item["index"], "status": "verified",
              "artist": item["artist"], "title": item["title"],
              "uri": item["uri"], "path": "", "reason": ""}
             for item in records])
        genre_store = GenreStore(database)
        genre_store.set("key-1", "Detroit Techno", "file")
        genre_store.set("key-2", "Rock", "musicbrainz")
        server.catalog.reload()
        server.catalog.refresh()
        cls.loop_server = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.lan_server = PresentedClientServer(("127.0.0.1", 0), server.Handler)
        cls.lan_server.presented_client = "192.0.2.44"
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

    def request(self, port, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        all_headers = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body)
            all_headers.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=payload, headers=all_headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        connection.close()
        return result

    def get_tracks(self, query=""):
        status, _, data = self.request(self.loop_port, "GET", f"/api/tracks?{query}")
        self.assertEqual(status, 200)
        return json.loads(data)

    def test_mood_uses_canonical_genres_before_paging_and_combines_with_filters(self):
        result = self.get_tracks("mood=energetic&genre=techno&genre=rock&limit=1&offset=1")
        self.assertEqual(result["total"], 2)
        self.assertEqual([track["id"] for track in result["tracks"]], [2])
        self.assertEqual(self.get_tracks("mood=energetic&genre=techno&q=detroit")["total"], 1)
        self.assertEqual(self.get_tracks("mood=energetic&genre=techno&filter=missing")["total"], 0)
        self.assertEqual(self.request(self.loop_port, "GET", "/api/tracks?mood=unknown")[0], 400)

    def test_blended_mood_starts_only_ready_matches_and_rejects_invalid_tunings(self):
        body = {"op": "play_mood", "mood": "energetic", "blend": "focused", "blend_percent": 40}
        status, _, data = self.request(self.loop_port, "POST", "/api/session", body=body)
        self.assertEqual(status, 200, data)
        session = json.loads(data)
        self.assertCountEqual([session["current_ref"], *[row["id"] for row in session["upcoming"]]], [1, 2])
        status, _, data = self.request(self.loop_port, "POST", "/api/session", body=body | {"genre": "minimal"})
        self.assertEqual((status, json.loads(data)["code"]), (400, "empty_mood"))
        for invalid in ({"blend_percent": True}, {"blend_percent": 51}, {"blend": "energetic"},
                        {"blend": "unknown"}, {"genre": "reggae"}):
            status, _, _ = self.request(self.loop_port, "POST", "/api/session", body=body | invalid)
            self.assertEqual(status, 400)
        status, _, data = self.request(self.loop_port, "GET", "/api/session")
        self.assertEqual(json.loads(data)["occurrence"], session["occurrence"])

    def test_blend_weights_change_the_order_without_repeating_shared_tracks(self):
        def draw(weight):
            with mock.patch("catalog.moods.random.randrange", return_value=30), \
                    mock.patch("catalog.moods.random.shuffle"):
                return server.moods.shuffled_mix([1, 2, 3], [1, 4, 5], weight)
        self.assertEqual(draw(25), [3, 2, 1, 5, 4])
        self.assertEqual(draw(45), [5, 4, 1, 3, 2])

    def test_room_mood_edits_normalize_aliases_and_empty_means_no_songs(self):
        for tags, expected, count in [(["Detroit Techno", "techno"], ["techno"], 1), ([], [], 0)]:
            status, _, data = self.request(self.loop_port, "POST", "/api/moods",
                                            body={"mood": "calm", "genres": tags})
            self.assertEqual(status, 200)
            group = next(m for m in json.loads(data)["moods"] if m["id"] == "calm")
            self.assertEqual(group["genres"], expected)
            self.assertEqual(self.get_tracks("mood=calm")["total"], count)
        self.assertEqual(GenreStore(server.DB).get("key-1")["genre"], "Detroit Techno")

    def test_genre_filter_and_facet(self):
        payload = self.get_tracks("genre=techno")
        self.assertEqual([track["id"] for track in payload["tracks"]], [1])
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["genres"], ["rock", "techno"])
        track = payload["tracks"][0]
        self.assertEqual((track["genre"], track["genre_canonical"], track["genre_source"]),
                         ("Detroit Techno", "techno", "file"))

    def test_genre_filter_composes_with_availability_and_search(self):
        self.assertEqual(self.get_tracks("genre=techno&filter=ready")["total"], 1)
        self.assertEqual(self.get_tracks("genre=techno&filter=missing")["total"], 0)
        self.assertEqual(self.get_tracks("genre=rock&q=techno")["total"], 0)
        self.assertEqual(self.get_tracks("genre=techno&q=techno")["total"], 1)

    def test_genre_filter_accepts_a_repeated_parameter_as_a_union(self):
        payload = self.get_tracks("genre=rock&genre=techno")
        self.assertEqual([track["id"] for track in payload["tracks"]], [1, 2])
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["genres"], ["rock", "techno"])

    def test_genre_filter_repeated_value_matches_the_single_value(self):
        self.assertEqual(self.get_tracks("genre=rock&genre=rock"),
                         self.get_tracks("genre=rock"))

    def test_genre_filter_empty_value_filters_nothing(self):
        payload = self.get_tracks("genre=")
        self.assertEqual([track["id"] for track in payload["tracks"]], [1, 2, 3])
        self.assertEqual(payload["total"], 3)

    def test_genre_union_composes_with_availability_and_search(self):
        self.assertEqual(self.get_tracks("genre=rock&genre=techno&filter=ready")["total"], 2)
        self.assertEqual(self.get_tracks("genre=rock&genre=techno&filter=missing")["total"], 0)
        self.assertEqual(self.get_tracks("genre=rock&genre=techno&q=techno")["total"], 1)
        self.assertEqual(self.get_tracks("genre=rock&genre=techno&q=quiet")["total"], 0)

    def test_search_text_matches_genre(self):
        self.assertEqual([t["id"] for t in self.get_tracks("q=detroit")["tracks"]], [1])
        self.assertEqual([t["id"] for t in self.get_tracks("q=rock")["tracks"]], [2])

    def test_manual_correction_endpoint(self):
        status, _, data = self.request(self.loop_port, "POST", "/api/genre",
                                       body={"track_id": 3, "genre": "Deep House"})
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertEqual((payload["genre_canonical"], payload["genre_source"]),
                         ("house", "manual"))
        stored = GenreStore(server.DB).get("key-3")
        self.assertEqual(stored, {"genre": "Deep House", "canonical": "house",
                                  "source": "manual"})
        self.assertEqual([t["id"] for t in self.get_tracks("genre=house")["tracks"]], [3])

    def test_manual_correction_is_host_only(self):
        status, headers, _ = self.request(self.lan_port, "GET", f"/?token={self.token}")
        self.assertEqual(status, 303)
        bootstrap = headers["Set-Cookie"].split(";", 1)[0]
        # Plan 021a: the paired caller is a registered device; the
        # bootstrap cookie alone no longer reaches this route.
        status, headers, _ = self.request(
            self.lan_port, "POST", "/api/device",
            body={"id": "genre-lan-device", "name": "Genre LAN"},
            headers={"Cookie": bootstrap,
                     "Origin": f"http://127.0.0.1:{self.lan_port}"})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, data = self.request(self.lan_port, "POST", "/api/genre",
                                       body={"track_id": 3, "genre": "Pop"},
                                       headers={"Cookie": cookie,
                                                "Origin": f"http://127.0.0.1:{self.lan_port}"})
        self.assertEqual(status, 403)
        self.assertIn("host settings", json.loads(data)["error"])
        status, _, _ = self.request(self.lan_port, "POST", "/api/moods",
                                     body={"mood": "energetic", "genres": []},
                                     headers={"Cookie": cookie, "Origin": f"http://127.0.0.1:{self.lan_port}"})
        self.assertEqual(status, 403)

    def test_song_dialog_payload_carries_genre_and_source(self):
        server.requests_store().cache_put("metadata:key-2", "{}", time.time())
        status, _, data = self.request(self.loop_port, "GET", "/api/metadata/2")
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertEqual(payload["genre"], "Rock")
        self.assertEqual(payload["genre_canonical"], "rock")
        self.assertEqual(payload["genre_source_label"], "from MusicBrainz")


if __name__ == "__main__":
    unittest.main()
