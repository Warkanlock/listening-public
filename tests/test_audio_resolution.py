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
from unittest import mock

from fixture_environment import isolate_room_environment as setUpModule
from runner_fixture import runner
import server
from catalog import audio_resolution
from catalog.audio_resolution import (custom_file, locate_stored, resolve_ref,
                                      resolve_stored, source_file, stored_path,
                                      track_file)
from catalog.catalog import Catalog, CatalogPaths
from catalog.catalog_store import CatalogStore
from downloads.requests_store import RequestsStore


class StoredPathTests(unittest.TestCase):
    """The stored form and the read normalization (A2 C-05)."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.library = self.root / "new" / "audio"
        (self.library / "custom").mkdir(parents=True)
        self.old_library = self.root / "old" / "audio"

    def write(self, path, data=b"audio"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_stored_path_is_library_relative(self):
        target = self.write(self.library / "1 - Song One.mp3")
        self.assertEqual(stored_path(target, self.library), "1 - Song One.mp3")
        custom = self.write(self.library / "custom" / "7.mp3")
        self.assertEqual(stored_path(custom, self.library), "custom/7.mp3")

    def test_relative_stored_path_resolves(self):
        target = self.write(self.library / "1 - Song One.mp3")
        self.assertEqual(resolve_stored("1 - Song One.mp3", self.library),
                         target.resolve())

    def test_absolute_path_inside_the_current_library_resolves(self):
        target = self.write(self.library / "1 - Song One.mp3")
        self.assertEqual(resolve_stored(str(target), self.library), target.resolve())

    def test_absolute_path_from_the_old_location_maps_by_tail(self):
        target = self.write(self.library / "1 - Song One.mp3")
        legacy = str(self.old_library / "1 - Song One.mp3")
        self.assertEqual(resolve_stored(legacy, self.library), target.resolve())

    def test_custom_tail_from_the_old_location_maps(self):
        target = self.write(self.library / "custom" / "7.mp3")
        legacy = str(self.old_library / "custom" / "7.mp3")
        self.assertEqual(resolve_stored(legacy, self.library), target.resolve())

    def test_absolute_path_with_no_matching_tail_never_resolves(self):
        self.assertIsNone(resolve_stored(str(self.old_library / "gone.mp3"),
                                         self.library))

    def test_existing_file_outside_the_library_never_resolves(self):
        outside = self.write(self.root / "outside.mp3")
        self.assertIsNone(resolve_stored(str(outside), self.library))

    def test_relative_path_escaping_the_library_never_resolves(self):
        self.write(self.root / "new" / "secret.mp3")
        self.assertIsNone(resolve_stored("../secret.mp3", self.library))

    def test_disallowed_format_inside_the_library_never_resolves(self):
        self.write(self.library / "notes.txt")
        self.assertIsNone(resolve_stored("notes.txt", self.library))

    def test_missing_and_empty_values_never_resolve(self):
        self.assertIsNone(resolve_stored("missing.mp3", self.library))
        self.assertIsNone(resolve_stored("", self.library))
        self.assertIsNone(resolve_stored(None, self.library))

    def test_locate_stored_joins_relative_values(self):
        target = self.library / "1 - Song One.mp3"
        self.assertEqual(locate_stored("1 - Song One.mp3", self.library), target.resolve())

    def test_locate_stored_maps_a_missing_absolute_by_tail(self):
        target = self.write(self.library / "shared.mp3")
        legacy = str(self.old_library / "shared.mp3")
        self.assertEqual(locate_stored(legacy, self.library), target.resolve())

    def test_locate_stored_keeps_an_existing_absolute_as_is(self):
        elsewhere = self.write(self.root / "elsewhere" / "shared.mp3")
        self.assertEqual(locate_stored(str(elsewhere), self.library),
                         elsewhere.resolve())


def make_record(index, title, active=True):
    return {"index": index, "key": f"key-{index}", "uri": f"spotify:track:{index}",
            "artist": f"Artist {index}", "title": title, "album": "Album",
            "length": 200, "isrc": "", "first_playlist": "mix", "active": active}


class CatalogFixture:
    """A real Catalog and RequestsStore over a temp workspace."""

    def build(self, records, coverage_rows):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        state = root / "state"
        state.mkdir()
        self.library = root / "audio"
        self.library.mkdir()
        database = state / "requests.sqlite3"
        store = CatalogStore(database)
        store.write_build({"records": records, "playlists": {},
                           "issues": [], "source_signature": {}})
        store.write_coverage(coverage_rows)
        self.catalog = Catalog(CatalogPaths(database=database,
                                            lists=root / "lists"))
        self.catalog.refresh()
        self.store = RequestsStore(database)
        return root


class ResolveRefTests(CatalogFixture, unittest.TestCase):
    """Resolution-level gates: only verified tracks and downloaded
    custom requests resolve (plan 009, from A6)."""

    def setUp(self):
        records = [make_record(1, "Song One"), make_record(2, "Song Two"),
                   make_record(3, "Song Three")]
        self.audio = self.library_file = None
        root = self.build(records, [
            {"index": 1, "status": "verified", "artist": "Artist 1",
             "title": "Song One", "uri": "spotify:track:1",
             "path": "1 - Song One.mp3", "reason": ""},
            {"index": 2, "status": "pending", "artist": "Artist 2",
             "title": "Song Two", "uri": "spotify:track:2", "path": "", "reason": ""},
            {"index": 3, "status": "rejected", "artist": "Artist 3",
             "title": "Song Three", "uri": "spotify:track:3", "path": "", "reason": ""},
        ])
        self.root = root
        (self.library / "1 - Song One.mp3").write_bytes(b"audio")

    def test_verified_track_resolves(self):
        item = resolve_ref(1, self.catalog, self.store)
        self.assertEqual(item["title"], "Song One")
        self.assertEqual(item["status"], "verified")
        self.assertEqual(item["audio"], "/api/audio/1")

    def test_pending_and_rejected_tracks_do_not_resolve(self):
        self.assertIsNone(resolve_ref(2, self.catalog, self.store))
        self.assertIsNone(resolve_ref(3, self.catalog, self.store))

    def test_unknown_refs_do_not_resolve(self):
        self.assertIsNone(resolve_ref(999, self.catalog, self.store))
        self.assertIsNone(resolve_ref("bogus", self.catalog, self.store))
        self.assertIsNone(resolve_ref("custom-abc", self.catalog, self.store))
        self.assertIsNone(resolve_ref(None, self.catalog, self.store))

    def test_downloaded_custom_request_resolves(self):
        request_id = self.store.enqueue(None, "Singer", "Custom Song", "", 180, "")
        self.assertIsNone(resolve_ref(f"custom-{request_id}", self.catalog, self.store))
        self.store.finish(request_id, "downloaded", "Ready to play", "custom/1.mp3")
        item = resolve_ref(f"custom-{request_id}", self.catalog, self.store)
        self.assertEqual(item["title"], "Custom Song")
        self.assertEqual(item["audio"], f"/api/audio/custom/{request_id}")

    def test_track_file_resolves_a_verified_track(self):
        self.assertEqual(track_file(1, self.catalog, self.library),
                         (self.library / "1 - Song One.mp3").resolve())
        self.assertIsNone(track_file(2, self.catalog, self.library))

    def test_custom_file_resolves_only_after_download(self):
        request_id = self.store.enqueue(None, "Singer", "Custom Song", "", 180, "")
        (self.library / "custom").mkdir(exist_ok=True)
        (self.library / "custom" / "1.mp3").write_bytes(b"audio")
        self.assertIsNone(custom_file(request_id, self.store, self.library))
        self.store.finish(request_id, "downloaded", "Ready to play", "custom/1.mp3")
        self.assertEqual(custom_file(request_id, self.store, self.library),
                         (self.library / "custom" / "1.mp3").resolve())

    def test_source_file_gates_and_messages(self):
        self.assertEqual(source_file(1, self.catalog, self.store, self.library),
                         (self.library / "1 - Song One.mp3").resolve())
        with self.assertRaisesRegex(ValueError, "This song is not ready"):
            source_file(2, self.catalog, self.store, self.library)
        with self.assertRaisesRegex(ValueError, "Unknown song"):
            source_file("bogus", self.catalog, self.store, self.library)
        request_id = self.store.enqueue(None, "Singer", "Custom Song", "", 180, "")
        with self.assertRaisesRegex(ValueError, "This request is not ready"):
            source_file(f"custom-{request_id}", self.catalog, self.store, self.library)

    def test_source_file_refuses_an_escaping_stored_path(self):
        self.catalog.coverage[1]["path"] = "../outside.mp3"
        (self.root / "outside.mp3").write_bytes(b"audio")
        with self.assertRaisesRegex(ValueError, "Audio outside the music folder"):
            source_file(1, self.catalog, self.store, self.library)


class MovedWorkspaceTests(CatalogFixture, unittest.TestCase):
    """Rows written with absolute paths under the old root resolve
    after the workspace moves, with no data migration."""

    def setUp(self):
        root = self.build([make_record(1, "Song One")], [
            {"index": 1, "status": "verified", "artist": "Artist 1",
             "title": "Song One", "uri": "spotify:track:1", "path": "", "reason": ""},
        ])
        self.root = root
        self.old_library = root / "old" / "audio"
        (self.library / "1 - Song One.mp3").write_bytes(b"audio")
        (self.library / "custom").mkdir()
        (self.library / "custom" / "9.mp3").write_bytes(b"custom-audio")
        # The rows still say where the files used to live.
        self.catalog.coverage[1]["path"] = str(self.old_library / "1 - Song One.mp3")
        self.request_id = self.store.enqueue(None, "Singer", "Custom Song", "", 180, "")
        self.store.finish(self.request_id, "downloaded", "Ready to play",
                          str(self.old_library / "custom" / "9.mp3"))

    def test_track_resolves_from_its_legacy_absolute_path(self):
        self.assertEqual(track_file(1, self.catalog, self.library),
                         (self.library / "1 - Song One.mp3").resolve())
        self.assertEqual(source_file(1, self.catalog, self.store, self.library),
                         (self.library / "1 - Song One.mp3").resolve())

    def test_custom_request_resolves_from_its_legacy_absolute_path(self):
        self.assertEqual(custom_file(self.request_id, self.store, self.library),
                         (self.library / "custom" / "9.mp3").resolve())


class RunnerStoredPathTests(unittest.TestCase):
    """The runner writes library-relative paths and reads legacy rows."""

    GOOD = {"duration": 200.0, "bitrate": 320000, "title": "Song One"}

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
        runner._probe_cache = None
        runner._probe_cache_dirty = False
        runner._REPORT_DIRTY = False
        runner._coverage_paths.cache_clear()
        self.addCleanup(runner._coverage_paths.cache_clear)
        patchers = [
            mock.patch.object(runner, "ROOT", self.root),
            mock.patch.object(runner, "resolve_config_path", lambda: self.root / "settings.json"),
            mock.patch.object(runner, "STATE", self.state),
            mock.patch.object(runner, "LIBRARY", self.library),
            mock.patch.object(runner, "PLAYLISTS", self.playlists_dir),
            mock.patch.object(runner, "OUTPUTS", self.root / "reports"),
            mock.patch.object(runner, "SOURCES", self.root / "lists"),
            mock.patch.object(runner, "MASTER", self.state / "master.csv"),
            mock.patch.object(runner, "REQUESTS_DB", self.state / "requests.sqlite3"),
            mock.patch.object(runner, "free_bytes", lambda: 100 * 1024**3),
            # Plan 062: this class characterizes stored file
            # paths; the landing mode is pinned to "files" now
            # that "database" is the default.
            mock.patch.object(runner, "audio_store_mode", lambda: "files"),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_manifest(self, records, playlists=None):
        payload = {"records": records, "playlists": playlists or {},
                   "issues": [], "source_signature": {}}
        CatalogStore(self.state / "requests.sqlite3").write_build(payload)
        runner.write_cursor(1)

    def coverage_rows(self):
        return {row["index"]: row
                for row in runner.catalog_store().read_coverage()}

    def test_report_persists_library_relative_paths(self):
        record = make_record(1, "Song One")
        self.write_manifest([record], playlists={"mix": [1]})
        (self.library / "1 - Song One.mp3").write_bytes(b"audio")
        with mock.patch.object(runner, "probe", lambda path: dict(self.GOOD)):
            with contextlib.redirect_stdout(io.StringIO()):
                runner.report()
        rows = self.coverage_rows()
        self.assertEqual(rows[1]["status"], "verified")
        self.assertEqual(rows[1]["path"], "1 - Song One.mp3")
        m3u8 = (self.playlists_dir / "mix.m3u8").read_text(encoding="utf-8")
        self.assertIn("Song One", m3u8)
        self.assertIn("../audio/1 - Song One.mp3", m3u8.replace("\\", "/"))

    def test_process_requests_persists_a_library_relative_audio_path(self):
        record = make_record(1, "Song One")
        self.write_manifest([record], playlists={"mix": [1]})
        (self.library / "1 - Song One.mp3").write_bytes(b"audio")
        store = runner.requests_store()
        request_id = store.enqueue(1, "Artist 1", "Song One", "Album", 200, "")
        with mock.patch.object(runner, "probe", lambda path: dict(self.GOOD)):
            with contextlib.redirect_stdout(io.StringIO()):
                runner.process_requests([record], limit=10)
        row = store.get(request_id)
        self.assertEqual(row["status"], "downloaded")
        self.assertEqual(row["audio_path"], "1 - Song One.mp3")

    def test_verified_existing_heals_a_legacy_coverage_row(self):
        target = self.library / "shared.mp3"
        target.write_bytes(b"audio")
        legacy = str(self.root / "old" / "audio" / "shared.mp3")
        CatalogStore(self.state / "requests.sqlite3").write_coverage(
            [{"index": 7, "status": "verified", "artist": "Artist",
              "title": "Song", "uri": "spotify:track:7", "path": legacy,
              "reason": ""}])
        record = {"title": "Song One", "length": 200}
        with mock.patch.object(runner, "probe", lambda path: dict(self.GOOD)):
            self.assertEqual(runner.verified_existing(7, record), target.resolve())


class MovedWorkspaceHttpTests(unittest.TestCase):
    """The workspace-move flow over HTTP: state copied to a new root,
    rows still holding old-root absolute paths, tracks serve."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.old_root = base / "old-room"
        cls.new_root = base / "new-room"
        library = cls.new_root / "audio"
        (library / "custom").mkdir(parents=True)
        (library / "1 - Song One.mp3").write_bytes(b"0123456789")
        state = cls.new_root / "state"
        state.mkdir()
        records = [make_record(1, "Song One"), make_record(2, "Song Two")]
        old_library = cls.old_root / "audio"
        store = CatalogStore(state / "requests.sqlite3")
        store.write_build({"records": records, "playlists": {}, "issues": [],
                           "source_signature": {}})
        store.write_coverage([
            {"index": 1, "status": "verified", "artist": "Artist 1",
             "title": "Song One", "uri": "spotify:track:1",
             "path": str(old_library / "1 - Song One.mp3"), "reason": ""},
            {"index": 2, "status": "pending", "artist": "Artist 2",
             "title": "Song Two", "uri": "spotify:track:2",
             "path": "", "reason": ""}])
        config_dir = base / "config"
        config_dir.mkdir()
        cls.config_path = config_dir / "config.json"
        cls.config_path.write_text(json.dumps({"folder": str(cls.new_root)}),
                                   encoding="utf-8")
        server.init(cls.config_path)
        store = server.requests_store()
        cls.request_id = store.enqueue(None, "Singer", "Custom Song", "", 180, "")
        (library / "custom" / f"{cls.request_id}.mp3").write_bytes(b"custom-bytes")
        store.finish(cls.request_id, "downloaded", "Ready to play",
                     str(old_library / "custom" / f"{cls.request_id}.mp3"))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def get(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        connection.close()
        return result

    def test_moved_track_serves(self):
        status, _, data = self.get("/api/audio/1")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"0123456789")

    def test_moved_track_serves_a_range(self):
        status, headers, data = self.get("/api/audio/1", {"Range": "bytes=2-5"})
        self.assertEqual(status, 206)
        self.assertEqual(data, b"2345")
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")

    def test_moved_custom_request_serves(self):
        status, _, data = self.get(f"/api/audio/custom/{self.request_id}")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"custom-bytes")

    def test_unverified_track_does_not_serve(self):
        status, _, _ = self.get("/api/audio/2")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
