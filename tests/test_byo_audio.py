import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import contextlib
import io
import json
import tempfile
import unittest
from unittest import mock

from fixture_environment import isolate_room_environment as setUpModule
from runner_fixture import runner
import server
from catalog.audio_resolution import track_file
from catalog.catalog import Catalog, CatalogPaths
from catalog.catalog_store import CatalogStore
from system.refresh import (RefreshPipeline, directory_signature,
                            manifest_signature, stat_signature)


def reset_runner_caches():
    """Drop the per-invocation module caches so a test is a fresh run."""
    if hasattr(runner, "_probe_cache"):
        runner._probe_cache = None
    if hasattr(runner, "_probe_cache_dirty"):
        runner._probe_cache_dirty = False
    if hasattr(runner, "_REPORT_DIRTY"):
        runner._REPORT_DIRTY = False


class RunnerWorkspaceTestCase(unittest.TestCase):
    """Point every runner path global at a temp workspace."""

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
        self.outputs = self.root / "reports"
        self.sources = self.root / "lists"
        self.sources.mkdir()
        reset_runner_caches()
        self.addCleanup(reset_runner_caches)
        patchers = [
            mock.patch.object(runner, "ROOT", self.root),
            mock.patch.object(runner, "resolve_config_path", lambda: self.root / "settings.json"),
            mock.patch.object(runner, "audio_store_mode", lambda: "files"),
            mock.patch.object(runner, "STATE", self.state),
            mock.patch.object(runner, "LIBRARY", self.library),
            mock.patch.object(runner, "PLAYLISTS", self.playlists_dir),
            mock.patch.object(runner, "OUTPUTS", self.outputs),
            mock.patch.object(runner, "SOURCES", self.sources),
            mock.patch.object(runner, "REQUESTS_DB", self.state / "requests.sqlite3"),
            mock.patch.object(runner, "free_bytes", lambda: 100 * 1024**3),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def record(self, index, title, artist=None, album="Album", length=200, active=True):
        return {"index": index, "key": f"key-{index}", "uri": f"spotify:track:{index}",
                "artist": artist or f"Artist {index}", "title": title, "album": album,
                "length": length, "isrc": "", "first_playlist": "mix", "active": active}

    def write_manifest(self, records, playlists=None, issues=None):
        payload = {"records": records, "playlists": playlists or {},
                   "issues": issues or [], "source_signature": {}}
        CatalogStore(self.state / "requests.sqlite3").write_build(payload)

    def write_cursor(self, offset):
        runner.write_cursor(offset)

    def run_report(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.report()
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def coverage_rows(self):
        return {row["index"]: row
                for row in runner.catalog_store().read_coverage()}

    def media(self, title, artist, album="Album", duration=200.0, bitrate=320000):
        return {"duration": duration, "bitrate": bitrate, "title": title,
                "artist": artist, "album": album, "genre": "Jazz"}


class BringYourOwnAudioReportTests(RunnerWorkspaceTestCase):
    """Plan 030: report() discovers audio by probe tags, not only by
    the "<index> - " name convention, and judges it by the same rule."""

    def test_plain_name_tagged_file_verifies_and_serves(self):
        record = self.record(1, "Blue in Green", artist="Miles Davis",
                             album="Kind of Blue", length=337)
        self.write_manifest([record], playlists={"mix": [1]})
        self.write_cursor(1)
        audio = self.library / "Blue in Green.mp3"
        audio.write_bytes(b"audio")
        media = self.media("Blue in Green", "Miles Davis", album="Kind of Blue",
                           duration=337.0)
        with mock.patch.object(runner, "probe", lambda path: dict(media)):
            summary = self.run_report()
        rows = self.coverage_rows()
        self.assertEqual(rows[1]["status"], "verified")
        self.assertEqual(rows[1]["path"], "Blue in Green.mp3")
        self.assertEqual(summary["verified"], 1)
        self.assertEqual(summary["unmatched_files"], 0)
        self.assertEqual(summary["ambiguous_files"], 0)
        # The file is never renamed or moved.
        self.assertTrue(audio.is_file())
        # And it serves through the catalog's own playable path.
        catalog = Catalog(CatalogPaths(database=self.state / "requests.sqlite3",
                                       lists=self.sources))
        catalog.refresh()
        self.assertEqual(catalog.track(1)["status"], "verified")
        self.assertEqual(track_file(1, catalog, self.library), audio.resolve())

    def test_matched_file_below_bitrate_floor_is_not_coverage(self):
        record = self.record(1, "Blue in Green", artist="Miles Davis",
                             album="Kind of Blue", length=337)
        self.write_manifest([record], playlists={"mix": [1]})
        self.write_cursor(1)
        (self.library / "Blue in Green.mp3").write_bytes(b"audio")
        media = self.media("Blue in Green", "Miles Davis", album="Kind of Blue",
                           duration=337.0, bitrate=128000)
        with mock.patch.object(runner, "probe", lambda path: dict(media)):
            summary = self.run_report()
        rows = self.coverage_rows()
        self.assertEqual(rows[1]["status"], "rejected")
        self.assertEqual(rows[1]["reason"], "low bitrate 128000")
        self.assertEqual(rows[1]["path"], "")
        self.assertEqual(summary["verified"], 0)

    def test_matched_file_outside_duration_tolerance_is_not_coverage(self):
        record = self.record(1, "Blue in Green", artist="Miles Davis",
                             album="Kind of Blue", length=337)
        self.write_manifest([record], playlists={"mix": [1]})
        self.write_cursor(1)
        (self.library / "Blue in Green.mp3").write_bytes(b"audio")
        media = self.media("Blue in Green", "Miles Davis", album="Kind of Blue",
                           duration=320.0)
        with mock.patch.object(runner, "probe", lambda path: dict(media)):
            summary = self.run_report()
        rows = self.coverage_rows()
        self.assertEqual(rows[1]["status"], "rejected")
        self.assertEqual(rows[1]["reason"], "duration differs by -17.0s")
        self.assertEqual(rows[1]["path"], "")
        self.assertEqual(summary["verified"], 0)

    def test_unmatched_files_are_ignored_and_counted(self):
        record = self.record(1, "Song One")
        self.write_manifest([record], playlists={"mix": [1]})
        self.write_cursor(0)
        (self.library / "Somebody Elses Song.mp3").write_bytes(b"audio")
        (self.library / "Unreadable.mp3").write_bytes(b"audio")
        media = {"Somebody Elses Song.mp3": self.media("Other Title", "Other Artist"),
                 "Unreadable.mp3": None}
        with mock.patch.object(runner, "probe", lambda path: media[path.name]):
            summary = self.run_report()
        rows = self.coverage_rows()
        self.assertEqual(rows[1]["status"], "pending")
        self.assertEqual(rows[1]["path"], "")
        self.assertEqual(summary["unmatched_files"], 2)
        self.assertEqual(summary["ambiguous_files"], 0)
        self.assertEqual(summary["verified"], 0)

    def test_ambiguous_tuple_is_skipped_for_both_records(self):
        first = self.record(1, "Blue in Green", artist="Miles Davis",
                            album="Kind of Blue", length=337)
        second = self.record(2, "Blue in Green", artist="Miles Davis",
                             album="Kind of Blue", length=337)
        self.write_manifest([first, second], playlists={"mix": [1, 2]})
        self.write_cursor(0)
        (self.library / "Blue in Green.mp3").write_bytes(b"audio")
        media = self.media("Blue in Green", "Miles Davis", album="Kind of Blue",
                           duration=337.0)
        with mock.patch.object(runner, "probe", lambda path: dict(media)):
            summary = self.run_report()
        rows = self.coverage_rows()
        self.assertEqual(summary["ambiguous_files"], 1)
        self.assertEqual(summary["verified"], 0)
        for index in (1, 2):
            self.assertEqual(rows[index]["status"], "pending")
            self.assertEqual(rows[index]["path"], "")

    def test_prefix_claimed_file_is_not_probed_a_second_time(self):
        records = [self.record(1, "Song One"), self.record(2, "Song Two")]
        self.write_manifest(records, playlists={"mix": [1, 2]})
        self.write_cursor(2)
        (self.library / "1 - Song One.mp3").write_bytes(b"audio")
        (self.library / "Song Two.mp3").write_bytes(b"audio")
        media = {"1 - Song One.mp3": self.media("Song One", "Artist 1"),
                 "Song Two.mp3": self.media("Song Two", "Artist 2")}
        probe_calls = []

        def counting_probe(path):
            probe_calls.append(path.name)
            return dict(media[path.name])

        cached_calls = []
        real_cached_probe = runner.cached_probe

        def counting_cached_probe(path):
            cached_calls.append(path.name)
            return real_cached_probe(path)

        with mock.patch.object(runner, "probe", counting_probe), \
                mock.patch.object(runner, "cached_probe", counting_cached_probe):
            summary = self.run_report()
        self.assertEqual(summary["verified"], 2)
        # The claimed file is probed once by the prefix pass and never
        # re-examined by the tag pass; the plain file is probed once
        # through the shared cache.
        self.assertEqual(cached_calls.count("1 - Song One.mp3"), 1)
        self.assertEqual(probe_calls.count("1 - Song One.mp3"), 1)
        self.assertEqual(probe_calls.count("Song Two.mp3"), 1)


class FlacAdmissionTests(RunnerWorkspaceTestCase):
    """Plan 050 step 4: FLAC is a first-class format. The admission
    path is the same one every format takes (the one allowlist in
    system.formats, judge()'s unchanged floor), and a verified FLAC
    serves through the catalog's playable path, which is what lets
    the player's lossless readout light up."""

    def test_bulk_download_uses_the_room_default_and_honors_a_run_override(self):
        config = self.state / "download-settings.json"
        config.write_text("{}")
        with mock.patch.object(runner, "resolve_config_path", return_value=config):
            self.assertEqual(runner.bulk_formats(), ((".mp3", ".flac", ".m4a"), "mp3"))
            config.write_text('{"bulk_download_format": "lossless_only"}')
            self.assertEqual(runner.bulk_formats(), ((".flac", ".m4a"), "flac"))
            with mock.patch.dict(runner.os.environ, {"OFFLINE_MUSIC_DOWNLOAD_FORMAT": "mp3_only"}):
                self.assertEqual(runner.bulk_formats(), ((".mp3",), "mp3"))

    def test_flac_above_the_floor_verifies_and_serves(self):
        record = self.record(1, "Blue in Green", artist="Miles Davis",
                             album="Kind of Blue", length=337)
        self.write_manifest([record], playlists={"mix": [1]})
        self.write_cursor(1)
        audio = self.library / "Blue in Green.flac"
        audio.write_bytes(b"audio")
        media = dict(self.media("Blue in Green", "Miles Davis",
                                album="Kind of Blue", duration=337.0,
                                bitrate=900_000),
                     codec="flac", sample_rate=44100, bit_depth=16)
        with mock.patch.object(runner, "probe", lambda path: dict(media)):
            summary = self.run_report()
        rows = self.coverage_rows()
        self.assertEqual(rows[1]["status"], "verified")
        self.assertEqual(rows[1]["path"], "Blue in Green.flac")
        self.assertEqual(summary["verified"], 1)
        catalog = Catalog(CatalogPaths(database=self.state / "requests.sqlite3",
                                       lists=self.sources))
        catalog.refresh()
        self.assertEqual(catalog.track(1)["status"], "verified")
        self.assertEqual(track_file(1, catalog, self.library), audio.resolve())

    def test_flac_below_the_floor_is_not_coverage(self):
        # The 192 kbps floor does not move for a lossless codec: a
        # FLAC-shaped probe under it is rejected on bitrate alone.
        record = self.record(1, "Blue in Green", artist="Miles Davis",
                             album="Kind of Blue", length=337)
        self.write_manifest([record], playlists={"mix": [1]})
        self.write_cursor(1)
        (self.library / "Blue in Green.flac").write_bytes(b"audio")
        media = dict(self.media("Blue in Green", "Miles Davis",
                                album="Kind of Blue", duration=337.0,
                                bitrate=128000),
                     codec="flac", sample_rate=44100, bit_depth=16)
        with mock.patch.object(runner, "probe", lambda path: dict(media)):
            summary = self.run_report()
        rows = self.coverage_rows()
        self.assertEqual(rows[1]["status"], "rejected")
        self.assertEqual(rows[1]["reason"], "low bitrate 128000")
        self.assertEqual(rows[1]["path"], "")
        self.assertEqual(summary["verified"], 0)


class AudioRefreshTests(unittest.TestCase):
    """Plan 030: the audio folder is a refresh input, so an audio-only
    change re-runs prepare and report with no list change anywhere."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.lists = root / "lists"
        self.lists.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        self.audio = root / "audio"
        self.audio.mkdir()
        self.stored = {"signature": None, "report": True,
                       "audio": directory_signature(self.audio)}
        self.write_manifest()
        self.calls = {"sync": 0, "rebuild": 0, "reload": 0}
        pipeline = RefreshPipeline(
            inputs=server.refresh_inputs, canonical_dir=lambda: self.lists,
            stored_signature=lambda: self.stored["signature"],
            audio_signature=lambda: directory_signature(self.audio),
            stored_audio_signature=lambda: self.stored["audio"],
            report_exists=lambda: self.stored["report"],
            sync=self._sync, rebuild=self._rebuild, reload=self._reload)
        patchers = [
            mock.patch.object(server, "ROOT", root),
            mock.patch.object(server, "SETTINGS",
                              {"source_folders": [], "source_files": [],
                               "lists_folder": str(self.lists)}),
            mock.patch.object(server, "LIBRARY", self.audio),
            mock.patch.object(server, "REFRESH", pipeline),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_manifest(self):
        self.stored["signature"] = manifest_signature(self.lists)

    def _sync(self):
        self.calls["sync"] += 1

    def _rebuild(self):
        self.calls["rebuild"] += 1
        self.write_manifest()
        self.stored["audio"] = directory_signature(self.audio)
        self.stored["report"] = True

    def _reload(self):
        self.calls["reload"] += 1

    def test_audio_only_change_triggers_rebuild(self):
        # Settled baseline: no list inputs exist in this scenario at
        # all, and nothing rebuilds.
        self.assertEqual(stat_signature(server.refresh_inputs()), {})
        self.assertFalse(server.refresh_library())
        self.assertEqual(self.calls["rebuild"], 0)
        # Adding one audio file is the only change anywhere.
        song = self.audio / "Blue in Green.mp3"
        song.write_bytes(b"audio")
        self.assertEqual(set(stat_signature(server.refresh_inputs())), {str(song)})
        self.assertTrue(server.refresh_library())
        self.assertEqual(self.calls["rebuild"], 1)
        self.assertEqual(self.calls["reload"], 1)
        # Settled again afterwards.
        self.assertFalse(server.refresh_library())
        self.assertEqual(self.calls["rebuild"], 1)


if __name__ == "__main__":
    unittest.main()
