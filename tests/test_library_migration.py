import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import hashlib
import tempfile
import unittest
from types import SimpleNamespace

from catalog.audio_store import MediaStore, blob_ref
from catalog.catalog_store import CatalogStore
from catalog.media_reconciliation import (MIGRATION_STATUS_IDLE,
                                          dumps_migration_status,
                                          migrate_library,
                                          migration_status_record,
                                          read_migration_status,
                                          unmigrated_files)
from system.job_queue import JobQueue
from system.jobs import Jobs

AUDIO_BYTES = b"fake mp3 audio bytes for plan 062 tests" * 8
OTHER_BYTES = b"a different song's bytes entirely" * 8
NOT_AUDIO = b"this file is not audio at all"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def media(title="Song One", artist="Artist 1"):
    return {"duration": 200.0, "bitrate": 320000, "title": title,
            "artist": artist, "album": "Album", "genre": "Jazz"}


def coverage(index, path, status="verified"):
    return {"index": index, "status": status, "artist": "Artist 1",
            "title": "Song One", "uri": f"spotify:track:{index}",
            "path": path, "reason": ""}


def downloaded(track_index, audio_path, request_id=1):
    return {"id": request_id, "track_index": track_index, "title": "Song One", "length": 200,
            "audio_path": audio_path}


class LibraryMigrationTestCase(unittest.TestCase):
    """Plan 062 step 2: the migration routine over a real
    MediaStore and a fabricated library. The two stores' write
    paths are recording seams; probe_file answers from the
    file's bytes, the way ffprobe answers from its content."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.library = self.root / "audio"
        self.library.mkdir()
        self.store = MediaStore(self.root / "media.sqlite3")
        self.coverage_writes = {}
        self.request_writes = {}
        self.progress = []

    def song(self, name, data=AUDIO_BYTES):
        path = self.library / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return name

    def probe_file(self, path):
        if Path(path).read_bytes() == NOT_AUDIO:
            return None
        return media()

    def run_migration(self, rows, entries):
        return migrate_library(
            self.store, rows, entries, self.library,
            rewrite_coverage=lambda index, reference:
                self.coverage_writes.__setitem__(index, reference),
            rewrite_downloaded=lambda request_id, reference:
                self.request_writes.__setitem__(request_id, reference),
            probe_file=self.probe_file,
            on_progress=lambda done, total, counts:
                self.progress.append((done, total, dict(counts))))


class MigrationRoutineTests(LibraryMigrationTestCase):

    def test_a_verified_file_migrates(self):
        name = self.song("1 - Artist 1 - Song One.mp3")
        counts = self.run_migration([coverage(1, name)], [])
        key = sha256(AUDIO_BYTES)
        self.assertEqual(counts, {"migrated": 1, "skipped": 0,
                                  "already": 0})
        self.assertEqual(self.coverage_writes, {1: blob_ref(key)})
        self.assertTrue((self.library / name).exists())
        self.assertTrue(self.store.has(key))
        self.assertEqual(self.store.probe(key), media())
        self.assertEqual(self.progress[-1],
                         (1, 1, {"migrated": 1, "skipped": 0,
                                 "already": 0}))

    def test_each_claim_must_pass_verification_for_its_own_title_and_duration(self):
        name = self.song("source.mp3")
        good = coverage(1, name)
        wrong_title = dict(coverage(2, name), title="Unrelated Music")
        wrong_length = dict(downloaded(None, name), length=12)
        result = self.run_migration([good, wrong_title], [wrong_length])
        self.assertEqual(result, {"migrated": 1, "skipped": 2, "already": 0})
        self.assertEqual(set(self.coverage_writes), {1})
        self.assertFalse(self.request_writes)

    def test_change_between_probe_and_intake_keeps_source_and_claim_untouched(self):
        name = self.song("source.mp3")
        def changed(path):
            path.write_bytes(OTHER_BYTES)
            return media()
        self.probe_file = changed
        self.assertEqual(self.run_migration([coverage(1, name)], [])["skipped"], 1)
        self.assertFalse(self.coverage_writes)
        self.assertEqual(self.store.count(), 0)
        self.assertEqual((self.library / name).read_bytes(), OTHER_BYTES)

    def test_a_custom_request_file_migrates(self):
        name = self.song("custom/7.mp3", OTHER_BYTES)
        counts = self.run_migration(
            [], [downloaded(None, name, request_id=7)])
        key = sha256(OTHER_BYTES)
        self.assertEqual(counts["migrated"], 1)
        self.assertEqual(self.request_writes, {7: blob_ref(key)})
        self.assertTrue((self.library / name).exists())
        self.assertTrue(self.store.has(key))

    def test_a_coverage_row_and_its_request_share_one_move(self):
        # One file, two references: both are rewritten to the
        # same blob reference and the original is retained.
        name = self.song("1 - Artist 1 - Song One.mp3")
        counts = self.run_migration(
            [coverage(1, name)],
            [downloaded(1, name, request_id=3)])
        key = sha256(AUDIO_BYTES)
        self.assertEqual(counts["migrated"], 2)
        self.assertEqual(self.coverage_writes, {1: blob_ref(key)})
        self.assertEqual(self.request_writes, {3: blob_ref(key)})
        self.assertEqual(self.store.count(), 1)
        self.assertTrue((self.library / name).exists())

    def test_an_unprobable_file_stays_and_counts_skipped(self):
        name = self.song("2 - Artist 1 - Song Two.mp3", NOT_AUDIO)
        counts = self.run_migration([coverage(2, name)], [])
        self.assertEqual(counts, {"migrated": 0, "skipped": 1,
                                  "already": 0})
        self.assertEqual(self.coverage_writes, {})
        self.assertTrue((self.library / name).exists())
        self.assertEqual(self.store.count(), 0)

    def test_on_skip_reports_the_unprobable_file_as_settled(self):
        name = self.song("2 - Artist 1 - Song Two.mp3", NOT_AUDIO)
        skipped = []
        migrate_library(
            self.store, [coverage(2, name)], [], self.library,
            rewrite_coverage=lambda index, reference: None,
            rewrite_downloaded=lambda request_id, reference: None,
            probe_file=self.probe_file, on_skip=skipped.append)
        self.assertEqual(skipped, [(self.library / name).resolve()])

    def test_unmigrated_files_excludes_settled_skips(self):
        owed = self.song("1 - Artist 1 - Song One.mp3")
        settled = self.song("2 - Artist 1 - Song Two.mp3", NOT_AUDIO)
        rows = [coverage(1, owed), coverage(2, settled),
                coverage(3, blob_ref(sha256(AUDIO_BYTES))),
                coverage(4, "4 - Artist 1 - Song Four.mp3")]
        self.assertEqual(
            unmigrated_files(rows, [], self.library,
                             settled=[(self.library / settled).resolve()]),
            [(self.library / owed).resolve()])
        # Without the settled list, both existing files are owed.
        self.assertEqual(
            len(unmigrated_files(rows, [], self.library)), 2)

    def test_a_missing_file_counts_skipped(self):
        counts = self.run_migration(
            [coverage(3, "3 - Artist 1 - Song Three.mp3")], [])
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(self.coverage_writes, {})

    def test_an_already_blob_row_is_untouched(self):
        source = self.root / "seed.mp3"
        source.write_bytes(AUDIO_BYTES)
        key = self.store.put_file(source, media())
        counts = self.run_migration([coverage(1, blob_ref(key))], [])
        self.assertEqual(counts, {"migrated": 0, "skipped": 0,
                                  "already": 1})
        self.assertEqual(self.coverage_writes, {})

    def test_a_second_run_is_a_no_op(self):
        name = self.song("1 - Artist 1 - Song One.mp3")
        self.run_migration([coverage(1, name)], [])
        # The post-migration world: the row names the blob, and a
        # second pass finds nothing to move and changes nothing.
        rows = [coverage(1, self.coverage_writes[1])]
        counts = self.run_migration(rows, [])
        self.assertEqual(counts, {"migrated": 0, "skipped": 0,
                                  "already": 1})
        self.assertEqual(self.store.count(), 1)

    def test_duplicate_bytes_become_one_blob_with_two_references(self):
        first = self.song("1 - Artist 1 - Song One.mp3")
        second = self.song("2 - Artist 1 - Song One (copy).mp3")
        counts = self.run_migration(
            [coverage(1, first), coverage(2, second)], [])
        key = sha256(AUDIO_BYTES)
        self.assertEqual(counts["migrated"], 2)
        self.assertEqual(self.coverage_writes,
                         {1: blob_ref(key), 2: blob_ref(key)})
        self.assertEqual(self.store.count(), 1)
        self.assertTrue((self.library / first).exists())
        self.assertTrue((self.library / second).exists())

    def test_adoption_retains_external_file_and_symlink(self):
        outside = self.root / "external.mp3"
        outside.write_bytes(AUDIO_BYTES)
        link = self.library / "link.mp3"
        link.symlink_to(outside)
        counts = self.run_migration([coverage(1, str(outside)), coverage(2, link.name)], [])
        self.assertEqual(counts["migrated"], 2)
        self.assertEqual(outside.read_bytes(), AUDIO_BYTES)
        self.assertTrue(link.is_symlink())

    def test_a_non_verified_row_does_not_participate(self):
        name = self.song("4 - Artist 1 - Song Four.mp3")
        counts = self.run_migration(
            [coverage(4, name, status="pending")], [])
        self.assertEqual(counts, {"migrated": 0, "skipped": 0,
                                  "already": 0})
        self.assertTrue((self.library / name).exists())
        self.assertEqual(self.store.count(), 0)


class MigrationStatusCodecTests(unittest.TestCase):
    """Plan 062: the migration status record's shape is owned by
    its codec, in the export codec's discipline."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "migration-status.json"

    def test_a_record_round_trips(self):
        counts = {"migrated": 5, "skipped": 1, "already": 2}
        record = migration_status_record("running", 6, 8, counts,
                                         1700000000.0)
        self.path.write_text(dumps_migration_status(record),
                             encoding="utf-8")
        self.assertEqual(read_migration_status(self.path), record)

    def test_missing_and_malformed_files_read_as_idle(self):
        self.assertEqual(read_migration_status(self.path),
                         MIGRATION_STATUS_IDLE)
        self.path.write_text("not json", encoding="utf-8")
        self.assertEqual(read_migration_status(self.path),
                         MIGRATION_STATUS_IDLE)
        self.path.write_text('["a", "list"]', encoding="utf-8")
        self.assertEqual(read_migration_status(self.path),
                         MIGRATION_STATUS_IDLE)


class VacuumTests(LibraryMigrationTestCase):
    """Plan 062 step 4: the finishing VACUUM runs and the store
    stays whole through it."""

    def test_vacuum_keeps_the_store_whole(self):
        kept = self.root / "kept.mp3"
        kept.write_bytes(AUDIO_BYTES)
        kept_key = self.store.put_file(kept, media())
        dropped = self.root / "dropped.mp3"
        dropped.write_bytes(OTHER_BYTES)
        dropped_key = self.store.put_file(dropped, media())
        self.store.delete(dropped_key)
        self.store.vacuum()
        self.assertTrue(self.store.has(kept_key))
        self.assertEqual(self.store.probe(kept_key), media())
        self.assertFalse(self.store.has(dropped_key))
        again = self.root / "again.mp3"
        again.write_bytes(OTHER_BYTES)
        self.assertEqual(self.store.put_file(again, media()),
                         dropped_key)


class ServerMigrationWiringTests(unittest.TestCase):
    """Plan 062 step 3: the server's settle at start. While
    file-backed entries remain, advance_library_migration
    persists the database landing and queues the migrate job;
    once none remain and the queue is quiet, it stamps the
    completion marker. A room with nothing to move is stamped
    without a job."""

    def setUp(self):
        import server
        self.server = server
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        base = Path(self.folder.name)
        self.library = base / "audio"
        self.library.mkdir()
        self.database = base / "requests.sqlite3"
        self.starts = []
        self.saves = 0
        self.settings = {}
        queue = JobQueue(self.database)
        self.queue = queue
        jobs = Jobs(
            queue=queue,
            worker_running=lambda kind: False,
            start_worker=self._start,
            stop_worker=lambda kind: None,
            genre_tools_ready=lambda: True,
            download_ready=lambda: True,
            lists_ready=lambda: True,
            refresh_library=lambda force=False: None,
            save_settings=lambda: None,
            settings=self.settings,
            catalog=lambda: SimpleNamespace(records=[], summary={}),
            pending_requests=lambda: 0,
            playlist_options=lambda: [])
        self.jobs = jobs
        self._patch("SETTINGS", self.settings)
        self._patch("DB", self.database)
        self._patch("LIBRARY", self.library)
        self._patch("jobs", jobs)
        # The catalog build reflects the folders as they stand;
        # one test below unpatches this to prove the deferral.
        self._patch("refresh_settled", lambda: True)
        self._patch("save_settings", self._save)
        # No lists in this room: nothing can become a library
        # entry later, so the stamp never waits for a first build.
        self._patch("source_dir", lambda root=None: self.library)
        self._patch("manifest_signature", lambda path: "")

    def _patch(self, name, value):
        original = getattr(self.server, name)
        self.addCleanup(setattr, self.server, name, original)
        setattr(self.server, name, value)

    def _start(self, kind, scope=None):
        self.starts.append((kind, scope))
        return True

    def _save(self):
        self.saves += 1

    def test_existing_room_never_queues_adoption_or_changes_file_landing(self):
        self.settings["audio_store"] = "files"
        song = self.library / "1 - Artist 1 - Song One.mp3"
        song.write_bytes(AUDIO_BYTES)
        CatalogStore(self.database).write_coverage(
            [coverage(1, song.name)])
        self.server.advance_library_migration()
        self.assertEqual(self.settings["audio_store"], "files")
        self.assertEqual(self.starts, [])
        self.assertIsNone(self.queue.running_for("migrate"))
        self.assertNotIn("library_migrated", self.settings)

    def test_completion_stamps_the_marker(self):
        song = self.library / "1 - Artist 1 - Song One.mp3"
        song.write_bytes(AUDIO_BYTES)
        store = CatalogStore(self.database)
        store.write_coverage([coverage(1, song.name)])
        self.server.advance_library_migration()
        # Only an explicit action starts adoption, and the file stays.
        self.jobs.act("migrate", "start")
        store.update_coverage_path(1, blob_ref(sha256(AUDIO_BYTES)))
        row = self.queue.running_for("migrate")
        self.queue.mark_done(row["id"])
        self.server.advance_library_migration()
        self.assertIn("library_migrated", self.settings)
        # And it never runs again.
        self.server.advance_library_migration()
        self.assertEqual(len(self.starts), 1)

    def test_a_room_with_nothing_to_move_is_stamped_without_a_job(self):
        self.server.advance_library_migration()
        self.assertIn("library_migrated", self.settings)
        self.assertEqual(self.starts, [])
        self.assertIsNone(self.queue.queued_for("migrate"))

    def test_a_completed_runs_skips_settle_and_stamp(self):
        # The one unprobable file stays a file forever; the
        # completed run's status names it as settled, so the
        # marker stamps instead of the job requeueing forever.
        song = self.library / "1 - Artist 1 - Song One.mp3"
        song.write_bytes(NOT_AUDIO)
        CatalogStore(self.database).write_coverage(
            [coverage(1, song.name)])
        state = Path(self.folder.name) / "state"
        state.mkdir()
        self._patch("STATE", state)
        record = migration_status_record(
            "done", 1, 1, {"migrated": 0, "skipped": 1, "already": 0},
            1700000000.0, [str(song.resolve())])
        (state / "migration-status.json").write_text(
            dumps_migration_status(record))
        self.server.advance_library_migration()
        self.assertIn("library_migrated", self.settings)
        self.assertEqual(self.starts, [])
        self.assertTrue(song.exists())

    def test_a_stale_catalog_build_defers_both_decisions(self):
        # Files changed while the server was down and the owed
        # rebuild has not run: coverage can under-report, so
        # neither the queue nor the forever stamp may fire.
        self._patch("refresh_settled", lambda: False)
        song = self.library / "1 - Artist 1 - Song One.mp3"
        song.write_bytes(AUDIO_BYTES)
        CatalogStore(self.database).write_coverage(
            [coverage(1, song.name)])
        self.server.advance_library_migration()
        self.assertEqual(self.starts, [])
        self.assertNotIn("library_migrated", self.settings)
        # Once settled, adoption is still an explicit action.
        self._patch("refresh_settled", lambda: True)
        self.server.advance_library_migration()
        self.assertEqual(self.starts, [])

    def test_the_stamp_waits_for_the_first_build_when_lists_exist(self):
        # Lists are present but the catalog has never been built:
        # the files those lists name are not entries yet, so the
        # migration is neither queued nor stamped off.
        self._patch("manifest_signature", lambda path: {"mix": 1})
        self.server.advance_library_migration()
        self.assertNotIn("library_migrated", self.settings)
        self.assertEqual(self.starts, [])


if __name__ == "__main__":
    unittest.main()
