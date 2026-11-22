import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from fixture_environment import isolate_room_environment as setUpModule
from runner_fixture import runner
from catalog.catalog import Catalog, CatalogPaths
from catalog.catalog_store import CatalogStore
from library.library_protocol import write_xspf
from system.refresh import RefreshPipeline, manifest_signature, stat_signature


def bump_mtime(path, extra_ns=1_000_000_000):
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + extra_ns))


class StatSignatureTests(unittest.TestCase):
    """Plan 006: the stat signature is cheap, read-only, and content-blind."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.first = self.root / "a.xspf"
        self.second = self.root / "b.xspf"
        self.first.write_text("one", encoding="utf-8")
        self.second.write_text("two", encoding="utf-8")

    def test_changes_on_rename(self):
        before = stat_signature([self.first, self.second])
        self.first.rename(self.root / "renamed.xspf")
        after = stat_signature([self.root / "renamed.xspf", self.second])
        self.assertNotEqual(before, after)

    def test_changes_on_resize(self):
        before = stat_signature([self.first])
        self.first.write_text("one but longer", encoding="utf-8")
        self.assertNotEqual(before, stat_signature([self.first]))

    def test_changes_on_mtime_bump(self):
        before = stat_signature([self.first])
        bump_mtime(self.first)
        self.assertNotEqual(before, stat_signature([self.first]))

    def test_ignores_unrelated_files(self):
        before = stat_signature([self.first])
        unrelated = self.root / "unrelated.xspf"
        unrelated.write_text("noise", encoding="utf-8")
        bump_mtime(unrelated)
        self.assertEqual(before, stat_signature([self.first]))

    def test_directory_contributes_its_files(self):
        signature = stat_signature([self.root])
        self.assertEqual(set(signature), {str(self.first), str(self.second)})

    def test_unreadable_file_still_signatures(self):
        self.first.chmod(0o000)
        self.addCleanup(self.first.chmod, 0o644)
        signature = stat_signature([self.first])
        self.assertIn(str(self.first), signature)

    def test_never_writes(self):
        def snapshot():
            return {path.name: (path.stat().st_size, path.stat().st_mtime_ns)
                    for path in self.root.iterdir()}
        before = snapshot()
        stat_signature([self.root])
        stat_signature([self.first, self.second])
        self.assertEqual(before, snapshot())


class RefreshPipelineTests(unittest.TestCase):
    """Plan 006: one entry point; a burst of triggers pays for one sync."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.canonical = root / "lists"
        self.canonical.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        self.list_file = self.canonical / "mix.xspf"
        self.list_file.write_text("<playlist/>\n", encoding="utf-8")
        # The stored half of the pipeline's world (plan 036): the
        # signature the stored catalog build carries, and whether a
        # report has run. Both live in the state database now; the
        # pipeline reads them through the injected callables. Plan
        # 055 adds the build's audio signature beside them; the
        # current audio fingerprint is a value the tests can move.
        self.stored = {"signature": None, "report": True, "audio": {}}
        self.current_audio = {}
        self.write_manifest()
        self.calls = {"sync": 0, "rebuild": 0, "reload": 0}
        self.pipeline = RefreshPipeline(
            inputs=lambda: [self.list_file],
            canonical_dir=lambda: self.canonical,
            stored_signature=lambda: self.stored["signature"],
            audio_signature=lambda: self.current_audio,
            stored_audio_signature=lambda: self.stored["audio"],
            report_exists=lambda: self.stored["report"],
            sync=self._sync, rebuild=self._rebuild, reload=self._reload)

    def write_manifest(self):
        self.stored["signature"] = manifest_signature(self.canonical)

    def _sync(self):
        self.calls["sync"] += 1

    def _rebuild(self):
        """Mimic prepare+report: store the fresh signatures, mark the report."""
        self.calls["rebuild"] += 1
        self.write_manifest()
        self.stored["audio"] = dict(self.current_audio)
        self.stored["report"] = True

    def _reload(self):
        self.calls["reload"] += 1

    def test_burst_of_triggers_syncs_exactly_once(self):
        results = [self.pipeline.refresh_if_changed() for _ in range(5)]
        self.assertEqual(results, [False] * 5)
        self.assertEqual(self.calls, {"sync": 1, "rebuild": 0, "reload": 0})

    def test_refresh_noops_when_signature_unchanged(self):
        self.pipeline.refresh_if_changed()
        settled = dict(self.calls)
        self.assertFalse(self.pipeline.refresh_if_changed())
        self.assertEqual(self.calls, settled)

    def test_change_rebuilds_and_reloads_once_then_settles(self):
        self.pipeline.refresh_if_changed()
        self.list_file.write_text("<playlist>changed</playlist>\n", encoding="utf-8")
        self.assertTrue(self.pipeline.refresh_if_changed())
        self.assertEqual(self.calls, {"sync": 2, "rebuild": 1, "reload": 1})
        self.assertFalse(self.pipeline.refresh_if_changed())
        self.assertEqual(self.calls, {"sync": 2, "rebuild": 1, "reload": 1})

    def test_synced_only_after_a_pass_completes(self):
        # Plan 062: the migration stamp reads synced() beside
        # settled(); a freshly built pipeline has run no pass,
        # however settled the stored build looks.
        self.assertTrue(self.pipeline.settled())
        self.assertFalse(self.pipeline.synced())
        self.pipeline.refresh_if_changed()
        self.assertTrue(self.pipeline.synced())

    def test_force_rebuilds_when_settled(self):
        self.pipeline.refresh_if_changed()
        self.assertTrue(self.pipeline.refresh_if_changed(force=True))
        self.assertEqual(self.calls["rebuild"], 1)
        self.assertEqual(self.calls["reload"], 1)

    def test_missing_report_forces_rebuild(self):
        self.pipeline.refresh_if_changed()
        self.stored["report"] = False
        self.assertTrue(self.pipeline.refresh_if_changed())
        self.assertEqual(self.calls["rebuild"], 1)

    def test_audio_only_change_rebuilds_once_then_settles(self):
        # Plan 055: the list files never move; only the audio
        # folder's fingerprint does.
        self.assertFalse(self.pipeline.refresh_if_changed())
        self.current_audio = {"song.mp3": [100, 200]}
        self.assertTrue(self.pipeline.refresh_if_changed())
        self.assertEqual(self.calls, {"sync": 2, "rebuild": 1, "reload": 1})
        self.assertFalse(self.pipeline.refresh_if_changed())
        self.assertEqual(self.calls, {"sync": 2, "rebuild": 1, "reload": 1})

    def test_stored_audio_none_rebuilds_exactly_once(self):
        # A pre-plan build carries no audio signature: unsettled
        # exactly once, even with no change anywhere, then the
        # rebuild records one and the pipeline settles.
        self.stored["audio"] = None
        self.assertTrue(self.pipeline.refresh_if_changed())
        self.assertEqual(self.calls["rebuild"], 1)
        self.assertFalse(self.pipeline.refresh_if_changed())
        self.assertEqual(self.calls["rebuild"], 1)

    def test_audio_changed_while_down_rebuilds_on_first_refresh(self):
        # The stored build's audio fingerprint is behind the folder
        # as the (fresh) pipeline first sees it: the change happened
        # while the server was down, and the first refresh catches
        # it instead of taking a baseline.
        self.stored["audio"] = {"old.mp3": [1, 1]}
        self.current_audio = {"new.mp3": [2, 2]}
        fresh = RefreshPipeline(
            inputs=lambda: [self.list_file],
            canonical_dir=lambda: self.canonical,
            stored_signature=lambda: self.stored["signature"],
            audio_signature=lambda: self.current_audio,
            stored_audio_signature=lambda: self.stored["audio"],
            report_exists=lambda: self.stored["report"],
            sync=self._sync, rebuild=self._rebuild, reload=self._reload)
        self.assertTrue(fresh.refresh_if_changed())
        self.assertEqual(self.calls["rebuild"], 1)
        self.assertFalse(fresh.refresh_if_changed())
        self.assertEqual(self.calls["rebuild"], 1)

    def test_burst_after_audio_change_pays_one_sync_one_rebuild(self):
        self.pipeline.refresh_if_changed()  # settle
        self.current_audio = {"song.mp3": [100, 200]}
        results = [self.pipeline.refresh_if_changed() for _ in range(5)]
        self.assertEqual(results, [True, False, False, False, False])
        self.assertEqual(self.calls, {"sync": 2, "rebuild": 1, "reload": 1})


class CatalogAudioSignatureTests(unittest.TestCase):
    """Plan 055: a build stores its audio signature in the store."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = CatalogStore(
            Path(self.folder.name) / "requests.sqlite3")

    def test_write_build_records_audio_signature(self):
        self.store.write_build(
            {"records": [], "playlists": {}, "issues": [],
             "source_signature": {},
             "audio_signature": {"a.mp3": [10, 20]}})
        self.assertEqual(self.store.audio_signature(), {"a.mp3": [10, 20]})

    def test_payload_without_audio_signature_reads_back_none(self):
        self.store.write_build(
            {"records": [], "playlists": {}, "issues": [],
             "source_signature": {}})
        self.assertIsNone(self.store.audio_signature())


def make_record(index, uri):
    return {"index": index, "key": f"key-{index}", "uri": uri, "artist": "Artist",
            "title": "Song", "album": "Album", "length": 200, "isrc": "",
            "first_playlist": "mix", "active": True}


COVER_URI = "spotify:track:" + "A" * 22
COVER_URL = "https://img.example/cover.jpg"


class CatalogReloadTests(unittest.TestCase):
    """Plan 006: Catalog.reload(paths) with the cover map in the build."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.state = root / "state"
        self.state.mkdir()
        self.lists = root / "lists"
        self.lists.mkdir()
        self.paths = CatalogPaths(database=self.state / "requests.sqlite3",
                                  lists=self.lists)

    def write_manifest(self, records, **extra):
        payload = {"records": records, "playlists": {"mix": [r["index"] for r in records]},
                   "issues": [], "source_signature": {}}
        payload.update(extra)
        CatalogStore(self.paths.database).write_build(payload)

    def test_reload_reads_covers_from_manifest_without_rescanning(self):
        self.write_manifest([make_record(1, COVER_URI)],
                            covers={COVER_URI: COVER_URL}, list_names={"mix": "My Mix"})
        catalog = Catalog(self.paths)  # the lists directory is empty
        self.assertEqual(catalog.covers, {COVER_URI: COVER_URL})
        self.assertEqual(catalog.list_names, {"mix": "My Mix"})
        self.assertEqual(catalog.track(1)["cover"], COVER_URL)

    def test_reload_with_injected_paths_replaces_state(self):
        self.write_manifest([make_record(1, COVER_URI)], covers={}, list_names={})
        catalog = Catalog(self.paths)
        other_state = Path(self.folder.name) / "other"
        other_state.mkdir()
        other = CatalogPaths(database=other_state / "requests.sqlite3",
                             lists=self.lists)
        payload = {"records": [make_record(1, COVER_URI), make_record(2, "spotify:track:" + "B" * 22)],
                   "playlists": {"mix": [1, 2]}, "issues": [], "source_signature": {},
                   "covers": {}, "list_names": {}}
        CatalogStore(other.database).write_build(payload)
        catalog.reload(other)
        self.assertEqual(len(catalog.records), 2)
        self.assertEqual(catalog.paths, other)

    def test_reload_after_manifest_rewrite(self):
        self.write_manifest([make_record(1, COVER_URI)], covers={}, list_names={})
        catalog = Catalog(self.paths)
        self.write_manifest([make_record(1, COVER_URI), make_record(2, "spotify:track:" + "B" * 22)],
                            covers={}, list_names={})
        catalog.reload()
        self.assertEqual(len(catalog.records), 2)

    def test_legacy_manifest_derives_covers_from_lists(self):
        (self.lists / "mix.xspf").write_text(
            write_xspf("My Mix", [{"title": "Song", "artist": "Artist", "album": "Album",
                                   "duration_ms": 200000, "identifier": COVER_URI,
                                   "image": COVER_URL, "isrc": "", "location": ""}]),
            encoding="utf-8")
        self.write_manifest([make_record(1, COVER_URI)])  # no covers/list_names keys
        catalog = Catalog(self.paths)
        self.assertEqual(catalog.covers, {COVER_URI: COVER_URL})
        self.assertEqual(catalog.list_names, {"mix": "My Mix"})

    def test_catalog_refresh_adopts_in_range_coverage_despite_count_mismatch(self):
        # Plan 036's hotfix: adoption must not demand row-count
        # equality (a build can hold a record the last report never
        # covered; the owner's library is 9,789 records against
        # 9,788 coverage rows). In-range rows are adopted as they
        # stand; only a non-empty set with NO in-range row is
        # another build's coverage and refused.
        records = [make_record(1, COVER_URI), make_record(2, "spotify:track:" + "B" * 22)]
        self.write_manifest(records, covers={}, list_names={})
        catalog = Catalog(self.paths)
        store = CatalogStore(self.paths.database)

        def coverage_row(index, status):
            return {"index": index, "status": status, "artist": "Artist",
                    "title": "Song", "uri": "", "path": "", "reason": ""}

        store.write_coverage([coverage_row(1, "verified")])
        catalog.refresh()
        self.assertEqual(len(catalog.coverage), 1)
        self.assertEqual(catalog.coverage[1]["status"], "verified")
        store.write_coverage([coverage_row(1, "verified"),
                              coverage_row(2, "pending")])
        catalog.refresh()
        self.assertEqual(len(catalog.coverage), 2)
        self.assertEqual(catalog.coverage[1]["status"], "verified")
        # A foreign write (no in-range row at all) is refused: the
        # refresh keeps the coverage it already adopted and leaves
        # its stamp alone so a later write is tried again, while a
        # catalog built over the foreign rows starts empty.
        store.write_coverage([coverage_row(3, "verified"),
                              coverage_row(4, "pending")])
        catalog.refresh()
        self.assertEqual(len(catalog.coverage), 2)
        self.assertEqual(Catalog(self.paths).coverage, {})


class PrepareCoverMapTests(unittest.TestCase):
    """Plan 006: prepare persists the cover map; it survives rebuilds."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.state = root / "state"
        self.state.mkdir()
        self.library = root / "audio"
        self.library.mkdir()
        self.playlists_dir = root / "playlists"
        self.playlists_dir.mkdir()
        self.sources = root / "lists"
        self.sources.mkdir()
        tracks = [{"title": "Song", "artist": "Artist", "album": "Album",
                   "duration_ms": 200000, "identifier": COVER_URI,
                   "image": COVER_URL, "isrc": "", "location": ""},
                  {"title": "Other", "artist": "Artist", "album": "Album",
                   "duration_ms": 100000, "identifier": "spotify:track:" + "B" * 22,
                   "image": "", "isrc": "", "location": ""}]
        (self.sources / "mix.xspf").write_text(write_xspf("My Mix", tracks),
                                               encoding="utf-8")
        patchers = [
            mock.patch.object(runner, "ROOT", root),
            mock.patch.object(runner, "resolve_config_path", lambda: root / "settings.json"),
            mock.patch.object(runner, "STATE", self.state),
            mock.patch.object(runner, "LIBRARY", self.library),
            mock.patch.object(runner, "PLAYLISTS", self.playlists_dir),
            mock.patch.object(runner, "SOURCES", self.sources),
            mock.patch.object(runner, "MASTER", self.state / "master.csv"),
            mock.patch.object(runner, "REQUESTS_DB", self.state / "requests.sqlite3"),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_prepare(self):
        with contextlib.redirect_stdout(io.StringIO()):
            runner.prepare()
        return runner.catalog_store().read_build()

    def test_prepare_persists_cover_map_and_list_names(self):
        manifest = self.run_prepare()
        self.assertEqual(manifest["covers"], {COVER_URI: COVER_URL})
        self.assertEqual(manifest["list_names"], {"mix": "My Mix"})

    def test_cover_map_survives_a_manifest_rebuild(self):
        self.run_prepare()
        manifest = self.run_prepare()
        self.assertEqual(manifest["covers"], {COVER_URI: COVER_URL})
        empty_lists = Path(self.folder.name) / "elsewhere"
        empty_lists.mkdir()
        catalog = Catalog(CatalogPaths(database=self.state / "requests.sqlite3",
                                       lists=empty_lists))
        self.assertEqual(catalog.covers, {COVER_URI: COVER_URL})
        self.assertEqual(catalog.track(1)["cover"], COVER_URL)


if __name__ == "__main__":
    unittest.main()
