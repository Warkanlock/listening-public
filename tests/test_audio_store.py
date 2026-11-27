import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from concurrent.futures import ThreadPoolExecutor
import contextlib
import ctypes
import errno
import hashlib
import http.client
import io
import json
import os
import sqlite3
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

from fixture_environment import isolate_room_environment as setUpModule
from runner_fixture import runner
from catalog import audio_store
import server
from catalog.audio_resolution import resolve_playable
from catalog.audio_store import (MediaStore, PlaybackBuffer, blob_ref,
                                 is_blob_ref, materialize)
from catalog.catalog_store import CatalogStore
from catalog.collection_snapshot import create_empty_collection
from downloads.requests_store import RequestsStore
from system.job_queue import JobQueue
from system.jobs import Jobs

PROBE = {"duration": 200.0, "bitrate": 320000, "title": "Song One",
         "artist": "Artist 1", "album": "Album", "genre": "Jazz"}
AUDIO_BYTES = b"fake mp3 audio bytes for plan 060 tests" * 8


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class MediaStoreTests(unittest.TestCase):
    """Plan 060 step 1: the media database's own contract."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.path = self.root / "media.sqlite3"
        self.source = self.root / "song.mp3"
        self.source.write_bytes(AUDIO_BYTES)
        self.store = MediaStore(self.path)

    def test_put_returns_the_content_hash_and_reads_back(self):
        key = self.store.put_file(self.source, dict(PROBE))
        self.assertEqual(key, sha256(AUDIO_BYTES))
        self.assertTrue(self.store.has(key))
        self.assertEqual(self.store.probe(key), PROBE)
        self.assertEqual(self.store.suffix(key), ".mp3")
        self.assertEqual(self.store.audio_bytes(), len(AUDIO_BYTES))
        self.assertEqual(self.store.count(), 1)

    def test_legacy_adoption_verifies_each_claim_and_preserves_newer_ownership(self):
        old = self.store.put_file(self.source, dict(PROBE))
        self.source.write_bytes(AUDIO_BYTES + b"new")
        new = self.store.put_file(self.source, dict(PROBE), owners=[("catalog", "newer")])
        rows = [{"index": i, "path": blob_ref(old), "status": "retired"} for i in (1, 2, 3)]
        records = [{"index": i, "key": name, "title": title, "length": 200} for i, name, title in
                   [(1, "newer", "Song One"), (2, "invalid", "Different"), (3, "valid", "Song One")]]
        self.store.adopt_catalog_references(rows, records)
        self.assertEqual(self.store.owned_audio("catalog", "newer"), blob_ref(new))
        self.assertIsNone(self.store.owned_audio("catalog", "invalid"))
        self.assertEqual(self.store.owned_audio("catalog", "valid"), blob_ref(old))

    def test_the_same_bytes_are_one_row(self):
        first = self.store.put_file(self.source, dict(PROBE))
        twin = self.root / "twin.mp3"
        twin.write_bytes(AUDIO_BYTES)
        second = self.store.put_file(twin, {**PROBE, "version": "Live"})
        self.assertEqual(self.store.probe(first)["version"], "Live")
        self.assertEqual(first, second)
        self.assertEqual(self.store.count(), 1)

    def test_auto_vacuum_is_incremental_after_creation(self):
        # The spike's load-bearing fact: the creation order in
        # MediaStore leaves auto_vacuum INCREMENTAL (2), where the
        # house ritual's order would have locked it to NONE.
        self.store.count()  # materialize the file through the ritual
        connection = sqlite3.connect(self.path)
        try:
            mode = connection.execute("PRAGMA auto_vacuum").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(mode, 2)

    def test_extract_writes_identical_bytes(self):
        key = self.store.put_file(self.source, dict(PROBE))
        dest = self.root / "out" / "song.mp3"
        self.assertTrue(self.store.extract(key, dest))
        self.assertEqual(dest.read_bytes(), AUDIO_BYTES)
        self.assertEqual(sha256(dest.read_bytes()), key)
        self.assertFalse(self.store.extract("0" * 64, self.root / "no.mp3"))

    def test_delete_removes_the_row(self):
        key = self.store.put_file(self.source, dict(PROBE))
        self.store.delete(key)
        self.assertFalse(self.store.has(key))
        self.assertEqual(self.store.count(), 0)

    def test_dead_bytes_fall_as_reclaim_bursts_run(self):
        big = self.root / "big.mp3"
        big.write_bytes(os.urandom(300_000))
        key = self.store.put_file(big, dict(PROBE))
        self.store.delete(key)
        before = self.store.dead_bytes()
        self.assertGreater(before, 0)
        freed = self.store.reclaim_burst(100000, 60)
        self.assertGreater(freed, 0)
        self.assertLess(self.store.dead_bytes(), before)


class PlaybackBufferTests(unittest.TestCase):
    """Plan 060 step 1/Q2: the staged window around playback."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.store = MediaStore(self.root / "media.sqlite3")
        self.buffer = PlaybackBuffer(self.root / "buffer")

    def put(self, data):
        source = self.root / f"src-{sha256(data)[:8]}.mp3"
        source.write_bytes(data)
        return self.store.put_file(source, dict(PROBE))

    def test_stage_extracts_the_exact_bytes(self):
        key = self.put(AUDIO_BYTES)
        staged = self.buffer.stage(self.store, key)
        self.assertIsNotNone(staged)
        self.assertEqual(staged.read_bytes(), AUDIO_BYTES)
        self.assertEqual(self.buffer.path_for(key), staged)

    def test_a_hash_mismatch_is_refused(self):
        key = self.put(AUDIO_BYTES)
        connection = sqlite3.connect(self.root / "media.sqlite3")
        connection.execute("UPDATE audio SET data = ? WHERE hash = ?",
                           (b"corrupted bytes", key))
        connection.commit()
        connection.close()
        self.assertIsNone(self.buffer.stage(self.store, key))
        self.assertIsNone(self.buffer.path_for(key))

    def test_only_the_three_most_recent_stay_staged(self):
        keys = [self.put(f"track {n}".encode() * 16) for n in range(4)]
        staged = []
        for position, key in enumerate(keys[:3]):
            path = self.buffer.stage(self.store, key)
            os.utime(path, ns=(1000 * (position + 1),) * 2)
            staged.append(path)
        fourth = self.buffer.stage(self.store, keys[3])
        self.assertIsNotNone(fourth)
        self.assertFalse(staged[0].exists())
        self.assertTrue(staged[1].exists())
        self.assertTrue(staged[2].exists())
        remaining = [p for p in (self.root / "buffer").iterdir()
                     if p.is_file()]
        self.assertEqual(len(remaining), 3)

    @unittest.skipUnless(hasattr(sqlite3.Connection, "blobopen"), "requires incremental BLOB reads")
    def test_idle_staging_releases_only_intact_duplicates_and_leaves_active_readers_open(self):
        keys = [self.put(data) for data in (b"idle", b"fresh", b"only copy")]
        idle, fresh, recovered = [self.buffer.stage(self.store, key) for key in keys]
        os.utime(idle, (1, 1))
        os.utime(recovered, (1, 1))
        with contextlib.closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE audio SET data=? WHERE hash=?", (b"corrupt", keys[2]))
        # A settings change can recreate the cache object over the same files.
        self.buffer = PlaybackBuffer(self.buffer.directory)
        with idle.open("rb") as reader:
            self.buffer.expire(self.store)
            self.assertFalse(idle.exists())
            self.assertEqual(reader.read(), b"idle")
        self.assertEqual(fresh.read_bytes(), b"fresh")
        self.assertEqual(recovered.read_bytes(), b"only copy")
        self.assertEqual(self.buffer.stage(self.store, keys[0]).read_bytes(), b"idle")

    def test_dropping_a_staged_copy_never_touches_the_store(self):
        key = self.put(AUDIO_BYTES)
        staged = self.buffer.stage(self.store, key)
        staged.unlink()
        self.assertTrue(self.store.has(key))
        again = self.buffer.stage(self.store, key)
        self.assertEqual(again.read_bytes(), AUDIO_BYTES)

    def test_release_only_removes_complete_duplicates_of_readable_database_audio(self):
        keys = [self.put(data) for data in (b"intact", b"only copy", b"edited")]
        complete, recovered, edited = [self.buffer.stage(self.store, key) for key in keys]
        with contextlib.closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE audio SET data=? WHERE hash=?", (b"corrupt", keys[1]))
        edited.write_bytes(b"owner edit")
        self.buffer.release(self.store)
        self.assertFalse(complete.exists())
        self.assertTrue(self.store.has(keys[0]))
        self.assertEqual(recovered.read_bytes(), b"only copy")
        self.assertEqual(edited.read_bytes(), b"owner edit")
        self.assertEqual(self.buffer.stage(self.store, keys[2]).read_bytes(), b"edited")
        for index in range(4):
            self.buffer.stage(self.store, self.put(f"new song {index}".encode()))
        self.assertEqual(recovered.read_bytes(), b"only copy")
        self.assertEqual([p.read_bytes() for p in self.buffer.directory.glob("retained-*/*")], [b"owner edit"])

    def test_staging_refuses_a_symlink_to_a_source_directory(self):
        key = self.put(AUDIO_BYTES)
        other = self.root / "originals"
        other.mkdir()
        self.buffer.directory.symlink_to(other, target_is_directory=True)
        self.assertIsNone(self.buffer.stage(self.store, key))
        self.assertEqual(list(other.iterdir()), [])

    def test_restart_clears_an_interrupted_extract_and_retains_unidentified_files(self):
        key = self.put(AUDIO_BYTES)
        self.buffer.directory.mkdir()
        partial = self.buffer.directory / f".listening-playback-{key}.mp3.u9ldk126"
        partial.write_bytes(AUDIO_BYTES[:10])
        other = self.buffer.directory / "previous-recording.part"
        other.write_bytes(b"keep this file")
        self.buffer.release(self.store)
        self.assertFalse(partial.exists())
        self.assertEqual(other.read_bytes(), b"keep this file")

    def test_a_staged_copy_does_not_resurrect_a_deleted_key(self):
        key = self.put(AUDIO_BYTES)
        staged = self.buffer.stage(self.store, key)
        self.assertTrue(staged.exists())
        self.store.delete(key)
        self.assertIsNone(self.buffer.stage(self.store, key))
        self.assertFalse(staged.exists())


class MaterializeTests(unittest.TestCase):
    """Plan 060 steps 6-7: export and share writes are hash-honest:
    a present file is only skipped when its bytes hash to the key."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.store = MediaStore(self.root / "media.sqlite3")
        source = self.root / "src.mp3"
        source.write_bytes(AUDIO_BYTES)
        self.key = self.store.put_file(source, dict(PROBE))
        self.out = self.root / "out"
        self.out.mkdir()

    def test_sharing_keeps_a_colliding_owner_file_and_reuses_its_own_copy(self):
        key = self.key
        folder = self.root / "shared"
        folder.mkdir()
        original = folder / "track.mp3"
        original.write_bytes(b"owners original bytes")
        for count in (1, 0):
            self.assertEqual(materialize(self.store, [(key, "track.mp3")], folder,
                                         preserve_existing=True), (count, 0))
        self.assertEqual(original.read_bytes(), b"owners original bytes")
        self.assertEqual((folder / f"track - {key[:16]}.mp3").read_bytes(), AUDIO_BYTES)

    def test_a_same_size_corrupt_file_is_rewritten_not_skipped(self):
        target = self.out / "track.mp3"
        target.write_bytes(b"x" * len(AUDIO_BYTES))
        written, failed = materialize(
            self.store, [(self.key, "track.mp3")], self.out)
        self.assertEqual((written, failed), (1, 0))
        self.assertEqual(target.read_bytes(), AUDIO_BYTES)

    def test_file_landing_without_hard_links_publishes_original_and_keeps_owner_collision(self):
        source = self.root / "src.mp3"
        conflict = self.out / "taken.mp3"
        conflict.write_bytes(b"owner audio")
        with mock.patch("os.link", side_effect=OSError(errno.ENOTSUP, "unsupported")):
            for name in ("fresh.mp3", "taken.mp3"):
                saved = runner.publish_verified_download(source, self.out / name, runner.source_stamp(source.stat()))
                self.assertEqual(saved.read_bytes(), AUDIO_BYTES)
        self.assertEqual((self.out / "fresh.mp3").read_bytes(), AUDIO_BYTES)
        self.assertEqual(conflict.read_bytes(), b"owner audio")
        self.assertEqual((self.out / f"taken - {self.key[:16]}.mp3").read_bytes(), AUDIO_BYTES)

    def test_native_rename_keeps_an_owner_file_saved_immediately_before_publication(self):
        source = self.root / "src.mp3"
        target = self.out / "racing.mp3"
        owner_stage = self.out / "owner.tmp"
        owner_stage.write_bytes(b"owner audio")
        rename = audio_store._rename_without_replacing

        def owner_saves_first(stage, dest):
            owner_stage.replace(dest)
            rename(stage, dest)

        with mock.patch("os.link", side_effect=OSError(errno.ENOTSUP, "unsupported")), \
                mock.patch.object(audio_store, "_rename_without_replacing", side_effect=owner_saves_first), \
                self.assertRaises(FileExistsError):
            runner.publish_verified_download(source, target, runner.source_stamp(source.stat()))
        self.assertEqual(target.read_bytes(), b"owner audio")
        self.assertEqual(source.read_bytes(), AUDIO_BYTES)

    @unittest.skipIf(os.name == "nt", "Windows provides exclusive rename without libc")
    def test_volume_refusing_exclusive_rename_leaves_verified_source_for_retry(self):
        source = self.root / "src.mp3"
        target = self.out / "unsupported.mp3"

        def unsupported(*args):
            ctypes.set_errno(errno.ENOTSUP)
            return -1

        libc = mock.Mock()
        libc.renamex_np.side_effect = libc.renameat2.side_effect = unsupported
        with mock.patch("os.link", side_effect=OSError(errno.ENOTSUP, "unsupported")), \
                mock.patch.object(ctypes, "CDLL", return_value=libc), \
                self.assertRaisesRegex(OSError, "cannot atomically publish"):
            runner.publish_verified_download(source, target, runner.source_stamp(source.stat()))
        self.assertFalse(target.exists())
        self.assertEqual(source.read_bytes(), AUDIO_BYTES)

    def test_worker_killed_before_rename_leaves_no_playable_partial_file(self):
        source = self.root / "src.mp3"
        target = self.out / "interrupted.mp3"
        worker = """
import errno, os, sys
from pathlib import Path
from unittest.mock import patch
from catalog.audio_store import materialize
source, dest = map(Path, sys.argv[1:])
with patch("os.link", side_effect=OSError(errno.ENOTSUP, "unsupported")):
    with patch("catalog.audio_store._rename_without_replacing", side_effect=lambda *args: os._exit(42)):
        materialize(None, [(source, dest.name)], dest.parent, preserve_existing=True)
"""
        result = subprocess.run([sys.executable, "-c", worker, str(source), str(target)],
                                cwd=Path(__file__).resolve().parents[1] / "backend")
        self.assertEqual(result.returncode, 42)
        self.assertFalse(target.exists())
        self.assertEqual(source.read_bytes(), AUDIO_BYTES)

    def test_a_present_verified_file_is_skipped(self):
        (self.out / "track.mp3").write_bytes(AUDIO_BYTES)
        written, failed = materialize(
            self.store, [(self.key, "track.mp3")], self.out)
        self.assertEqual((written, failed), (0, 0))


class ResolutionSeamTests(unittest.TestCase):
    """Plan 060 step 2: blob references resolve through the buffer;
    file rows resolve exactly as before."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.library = self.root / "audio"
        self.library.mkdir()
        self.store = MediaStore(self.root / "media.sqlite3")
        self.buffer = PlaybackBuffer(self.root / "buffer")
        source = self.root / "song.mp3"
        source.write_bytes(AUDIO_BYTES)
        self.key = self.store.put_file(source, dict(PROBE))

    def test_a_blob_row_resolves_to_a_buffer_path(self):
        resolved = resolve_playable(blob_ref(self.key), self.library,
                                    self.store, self.buffer)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.read_bytes(), AUDIO_BYTES)

    def test_a_file_row_resolves_as_before(self):
        song = self.library / "plain.mp3"
        song.write_bytes(b"plain")
        self.assertEqual(resolve_playable("plain.mp3", self.library),
                         song.resolve())
        self.assertEqual(
            resolve_playable("plain.mp3", self.library,
                             self.store, self.buffer),
            song.resolve())

    def test_a_missing_key_resolves_to_none(self):
        self.assertIsNone(resolve_playable(blob_ref("0" * 64),
                                           self.library,
                                           self.store, self.buffer))
        self.assertIsNone(resolve_playable(blob_ref(self.key),
                                           self.library))
        self.assertTrue(is_blob_ref(blob_ref(self.key)))


def reset_runner_caches():
    if hasattr(runner, "_probe_cache"):
        runner._probe_cache = None
    if hasattr(runner, "_probe_cache_dirty"):
        runner._probe_cache_dirty = False
    if hasattr(runner, "_REPORT_DIRTY"):
        runner._REPORT_DIRTY = False


class RunnerStoreTestCase(unittest.TestCase):
    """The byo-audio workspace pattern, plus plan 060's two extra
    globals: ROOT (the media database's home) and the settings
    file the landing mode is read from."""

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
        self.settings_path = self.state / "listening-room.json"
        reset_runner_caches()
        self.addCleanup(reset_runner_caches)
        patchers = [
            mock.patch.object(runner, "STATE", self.state),
            mock.patch.object(runner, "ROOT", self.root),
            mock.patch.object(runner, "LIBRARY", self.library),
            mock.patch.object(runner, "PLAYLISTS", self.playlists_dir),
            mock.patch.object(runner, "OUTPUTS", self.outputs),
            mock.patch.object(runner, "SOURCES", self.sources),
            mock.patch.object(runner, "REQUESTS_DB",
                              self.state / "requests.sqlite3"),
            mock.patch.object(runner, "free_bytes", lambda: 100 * 1024**3),
            mock.patch.object(runner, "resolve_config_path",
                              lambda: self.settings_path),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def record(self, index, title, artist=None, length=200):
        return {"index": index, "key": f"key-{index}",
                "uri": f"spotify:track:{index}",
                "artist": artist or f"Artist {index}", "title": title,
                "album": "Album", "length": length, "isrc": "",
                "first_playlist": "mix", "active": True}

    def write_manifest(self, records, playlists=None):
        payload = {"records": records, "playlists": playlists or {},
                   "issues": [], "source_signature": {}}
        CatalogStore(self.state / "requests.sqlite3").write_build(payload)

    def set_mode(self, mode):
        self.settings_path.write_text(json.dumps({"audio_store": mode}),
                                      encoding="utf-8")

    def media(self, title="Song One", artist="Artist 1"):
        return {"duration": 200.0, "bitrate": 320000, "title": title,
                "artist": artist, "album": "Album", "genre": "Jazz"}

    def run_report(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.report()
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def coverage_rows(self):
        return {row["index"]: row
                for row in runner.catalog_store().read_coverage()}

    def write_coverage(self, rows):
        runner.catalog_store().write_coverage(rows)

    def media_store(self):
        path = self.root / "media.sqlite3"
        return MediaStore(path) if path.is_file() else None

    def receive_candidate(self, source, row, record):
        @contextlib.contextmanager
        def transfer(selection, recording, accept, **options):
            with tempfile.TemporaryDirectory(dir=self.state) as directory:
                candidate = Path(directory) / source.name
                shutil.copyfile(source, candidate)
                options["on_transfer"]()
                yield candidate if accept(candidate, lambda: 45) else None

        with mock.patch.object(runner, "managed_candidate", transfer), mock.patch.object(
                runner, "configured_engines", return_value={"search": "fixture", "fetch": "fixture"}):
            return runner.run_request(row, record)


class ReportBlobTests(RunnerStoreTestCase):
    """Plan 060 step 4: blob-backed records in the report."""

    def seed_blob_track(self):
        record = self.record(1, "Song One")
        self.write_manifest([record], playlists={"mix": [1]})
        source = self.root / "seed.mp3"
        source.write_bytes(AUDIO_BYTES)
        store = MediaStore(self.root / "media.sqlite3")
        key = store.put_file(source, self.media())
        self.write_coverage([{
            "index": 1, "status": "verified", "artist": record["artist"],
            "title": record["title"], "uri": record["uri"],
            "path": blob_ref(key), "reason": ""}])
        runner.write_cursor(1)
        return record, key

    def test_a_blob_backed_record_survives_a_rebuild(self):
        _record, key = self.seed_blob_track()
        self.run_report()
        rows = self.coverage_rows()
        self.assertEqual(rows[1]["status"], "verified")
        self.assertEqual(rows[1]["path"], blob_ref(key))
        self.assertTrue(self.media_store().has(key))

    def test_database_mode_ingests_a_name_convention_acceptance(self):
        self.set_mode("database")
        record = self.record(1, "Song One")
        self.write_manifest([record], playlists={"mix": [1]})
        runner.write_cursor(1)
        incoming = self.state / "bulk-incoming"
        incoming.mkdir()
        landed = incoming / "1 - Artist 1 - Song One.flac"
        landed.write_bytes(AUDIO_BYTES)
        with mock.patch.object(runner, "probe",
                               lambda path: dict(self.media(), codec="flac", container="flac")):
            self.run_report()
        rows = self.coverage_rows()
        self.assertEqual(rows[1]["status"], "verified")
        self.assertEqual(rows[1]["path"], blob_ref(sha256(AUDIO_BYTES)))
        self.assertFalse(landed.exists())
        self.assertEqual(self.media_store().count(), 1)

    def test_report_releases_an_exact_bulk_duplicate_and_preserves_sources(self):
        record = self.record(1, "Song One")
        self.write_manifest([record])
        incoming = self.state / "bulk-incoming"
        incoming.mkdir()
        original = self.library / "1 - source.flac"
        original.write_bytes(AUDIO_BYTES)
        store = MediaStore(self.root / "media.sqlite3")
        key = store.put_file(original, dict(self.media(), codec="flac", container="flac"),
                             owners=(("catalog", record["key"]),))
        duplicate = incoming / "1 - downloaded.flac"
        duplicate.write_bytes(AUDIO_BYTES)
        alternate = incoming / "1 - alternate.flac"
        alternate.write_bytes(b"some other audio")
        alias = incoming / "1 - symlink.flac"
        alias.symlink_to(original)

        self.run_report()

        self.assertFalse(duplicate.exists())
        self.assertEqual(original.read_bytes(), AUDIO_BYTES)
        self.assertEqual(alternate.read_bytes(), b"some other audio")
        self.assertTrue(alias.is_symlink())
        self.assertEqual(self.coverage_rows()[1]["path"], blob_ref(key))
        self.assertEqual(store.count(), 1)

    def test_restarted_bulk_preflight_releases_unmapped_duplicates_at_the_reserve(self):
        self.write_manifest([self.record(1, "Song One")])
        incoming = self.state / "bulk-incoming"
        incoming.mkdir()
        different_name = incoming / "no longer indexed.m4a"
        different_name.write_bytes(AUDIO_BYTES)
        unique = incoming / "1 - untouched.flac"
        unique.write_bytes(b"different recording")
        original = self.library / "original.flac"
        original.write_bytes(AUDIO_BYTES)
        store = MediaStore(self.root / "media.sqlite3")
        store.put_file(original, self.media(), owners=(("catalog", "other-track"),))
        with mock.patch.object(runner, "free_bytes", return_value=runner.MIN_FREE), \
                mock.patch.object(runner, "probe", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(runner.download_batch(0, 1, False))
        self.assertFalse(different_name.exists())
        self.assertEqual(unique.read_bytes(), b"different recording")
        self.assertEqual(original.read_bytes(), AUDIO_BYTES)

    def test_bulk_defers_ingestion_until_it_can_keep_the_reserve(self):
        self.write_manifest([self.record(1, "Song One")])
        incoming = self.state / "bulk-incoming"
        incoming.mkdir()
        staged = incoming / "1 - Song One.flac"
        staged.write_bytes(AUDIO_BYTES)
        with mock.patch.object(runner, "free_bytes", return_value=runner.MIN_FREE), \
                mock.patch.object(runner, "probe", return_value=dict(self.media(), codec="flac", container="flac")):
            self.run_report()
        self.assertEqual(self.coverage_rows()[1]["status"], "pending")
        self.assertEqual(staged.read_bytes(), AUDIO_BYTES)
        self.assertEqual(self.media_store().count(), 0)

    def test_bulk_can_link_a_second_track_to_existing_bytes_at_the_reserve(self):
        record = self.record(1, "Song One")
        self.write_manifest([record])
        staged = self.state / "bulk-incoming" / "1 - Song One.flac"
        staged.parent.mkdir()
        staged.write_bytes(AUDIO_BYTES)
        facts = dict(self.media(), codec="flac", container="flac")
        store = MediaStore(self.root / "media.sqlite3")
        key = store.put_file(staged, facts, owners=(("catalog", "another-track"),))
        with mock.patch.object(runner, "free_bytes", return_value=runner.MIN_FREE), \
                mock.patch.object(runner, "probe", return_value=facts):
            self.run_report()
        self.assertEqual(self.coverage_rows()[1]["path"], blob_ref(key))
        self.assertEqual(store.owned_audio("catalog", record["key"]), blob_ref(key))
        self.assertFalse(staged.exists())
        self.assertEqual(store.count(), 1)

    def test_bulk_run_lands_retained_audio_even_with_a_completed_cursor(self):
        self.write_manifest([self.record(1, "Song One")])
        runner.write_cursor(1)
        staged = self.state / "bulk-incoming" / "1 - Song One.flac"
        staged.parent.mkdir()
        staged.write_bytes(AUDIO_BYTES)
        with mock.patch.object(runner, "probe", return_value=dict(self.media(), codec="flac", container="flac")), \
                mock.patch.object(runner, "free_bytes", return_value=runner.MIN_FREE) as available:
            self.run_report()
            self.assertTrue(staged.exists())
            available.return_value = 100 * 1024**3
            with contextlib.redirect_stdout(io.StringIO()):
                runner.run()
        self.assertEqual(self.coverage_rows()[1]["path"], blob_ref(sha256(AUDIO_BYTES)))
        self.assertFalse(staged.exists())

    def test_verified_staging_recovers_a_damaged_blob_in_place(self):
        record, key = self.seed_blob_track()
        store = self.media_store()
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE audio SET data=x'00' WHERE hash=?", (key,))
        incoming = self.state / "bulk-incoming"
        incoming.mkdir()
        staged = incoming / "1 - Song One.flac"
        staged.write_bytes(AUDIO_BYTES)
        with mock.patch.object(runner, "probe", return_value=dict(self.media(), codec="flac", container="flac")):
            self.run_report()
        self.assertTrue(store.verifies_original(key))
        self.assertEqual(store.owned_audio("catalog", record["key"]), blob_ref(key))
        self.assertEqual(self.coverage_rows()[1]["path"], blob_ref(key))
        self.assertFalse(staged.exists())

    def test_shared_original_survives_rebuild_and_private_card_export(self):
        self.write_manifest([self.record(1, "Song One")], playlists={"mix": [1]})
        runner.write_cursor(1)
        share = self.root / "Music"
        album = share / "Artist" / "Album"
        album.mkdir(parents=True)
        song = album / "02 - Song One.mp3"
        original = b"a" * (2 * 1024**2)
        song.write_bytes(original)
        (album / "link.mp3").symlink_to(song)
        destination = self.root / "Offline" / "Music" / "Listening"
        with mock.patch.dict(os.environ, {"LISTENING_ROOM_MUSIC_INBOX": str(share),
                                          "LISTENING_ROOM_EXPORT_DIR": str(destination)}), \
                mock.patch.object(runner, "probe", return_value=self.media()) as probe:
            self.assertEqual(runner.shared_music.shared_files(), [song])
            summary = self.run_report()
            self.assertEqual(summary["library_gib"], round(len(original) / 1024**3, 3))
            self.assertTrue(runner.catalog_store().has_report_for_build())
            self.write_manifest([self.record(1, "Song One")], playlists={"mix": [1]})
            self.assertFalse(runner.catalog_store().has_report_for_build())
            probe_calls = probe.call_count
            reset_runner_caches()
            self.run_report()
            self.assertEqual(probe.call_count, probe_calls)
            self.assertTrue(runner.catalog_store().has_report_for_build())
            self.assertTrue(all(call.kwargs == {"local_audio_only": True} for call in probe.call_args_list))
            self.assertEqual(self.coverage_rows()[1]["path"], blob_ref(sha256(original)))
            self.assertEqual(self.media_store().count(), 1)
            self.assertEqual(song.read_bytes(), original)
            destination.mkdir(parents=True)
            previous = destination / "00001 - Artist 1 - Song One.mp3"
            previous.write_bytes(b"earlier file")
            with contextlib.redirect_stdout(io.StringIO()):
                runner.run_export()
                runner.run_export()
            self.assertEqual(previous.read_bytes(), b"earlier file")
            self.assertEqual([p.read_bytes() for p in destination.glob("*.mp3") if p != previous], [original])

    def test_shared_original_rechecks_artist_before_adoption(self):
        self.write_manifest([self.record(1, "Song One")], playlists={"mix": [1]})
        share = self.root / "Music"
        share.mkdir()
        original = share / "song.mp3"
        original.write_bytes(AUDIO_BYTES)
        with mock.patch.dict(os.environ, {"LISTENING_ROOM_MUSIC_INBOX": str(share)}), \
                mock.patch.object(runner, "probe", side_effect=[self.media(), self.media(artist="Another artist")]):
            self.run_report()
        self.assertEqual(self.coverage_rows()[1]["status"], "rejected")
        self.assertEqual(self.media_store().count(), 0)
        self.assertEqual(original.read_bytes(), AUDIO_BYTES)

    def test_database_mode_leaves_a_tag_matched_acceptance_a_file(self):
        self.set_mode("database")
        record = self.record(1, "Song One")
        self.write_manifest([record], playlists={"mix": [1]})
        runner.write_cursor(1)
        dropped = self.library / "Song One.mp3"
        dropped.write_bytes(AUDIO_BYTES)
        with mock.patch.object(runner, "probe",
                               lambda path: self.media()):
            self.run_report()
        rows = self.coverage_rows()
        self.assertEqual(rows[1]["status"], "verified")
        self.assertEqual(rows[1]["path"], "Song One.mp3")
        self.assertTrue(dropped.is_file())
        store = self.media_store()
        self.assertTrue(store is None or store.count() == 0)

    def test_a_stale_catalog_request_retains_bytes_without_reinstating_a_key(self):
        # Files mode is pinned explicitly (plan 062): it used to
        # be the implicit default this test silently relied on.
        self.set_mode("files")
        record = self.record(1, "Song One")
        self.write_manifest([record], playlists={"mix": [1]})
        runner.write_cursor(1)
        landed = self.library / "1 - Artist 1 - Song One.mp3"
        landed.write_bytes(AUDIO_BYTES)
        self.write_coverage([{
            "index": 1, "status": "verified", "artist": record["artist"],
            "title": record["title"], "uri": record["uri"],
            "path": "1 - Artist 1 - Song One.mp3", "reason": ""}])
        store = MediaStore(self.root / "media.sqlite3")
        source = self.root / "old.mp3"
        source.write_bytes(b"the previous download of this song" * 4)
        stale_key = store.put_file(source, self.media())
        requests = RequestsStore(self.state / "requests.sqlite3")
        request_id = requests.enqueue(1, record["artist"],
                                      record["title"], "", 0,
                                      record["title"])
        requests.finish(request_id, "downloaded", "Ready to play",
                        blob_ref(stale_key))
        with mock.patch.object(runner, "probe",
                               lambda path: self.media()):
            self.run_report()
        rows = self.coverage_rows()
        self.assertEqual(rows[1]["status"], "verified")
        self.assertEqual(rows[1]["path"], "1 - Artist 1 - Song One.mp3")
        self.assertTrue(self.media_store().has(stale_key))

    def test_files_mode_ingests_nothing(self):
        self.set_mode("files")
        record = self.record(1, "Song One")
        self.write_manifest([record], playlists={"mix": [1]})
        runner.write_cursor(1)
        landed = self.library / "1 - Artist 1 - Song One.mp3"
        landed.write_bytes(AUDIO_BYTES)
        with mock.patch.object(runner, "probe",
                               lambda path: self.media()):
            self.run_report()
        rows = self.coverage_rows()
        self.assertEqual(rows[1]["status"], "verified")
        self.assertEqual(rows[1]["path"], "1 - Artist 1 - Song One.mp3")
        self.assertTrue(landed.is_file())
        self.assertFalse((self.root / "media.sqlite3").exists())


class RequestLandingTests(RunnerStoreTestCase):
    """Plan 060 step 3: a request's verified download lands as a
    blob reference in database mode."""

    def run_one_request(self, record):
        source = self.root / "Artist 1 - Song One.flac"
        source.write_bytes(AUDIO_BYTES)
        row = {"id": 7, "artist": "Artist 1", "title": "Song One",
               "album": "Album", "length": 200, "search_title": ""}

        with mock.patch.object(runner, "decode_audio"), \
                mock.patch.object(runner, "probe",
                                  return_value=dict(self.media(), codec="flac", container="flac")):
            return self.receive_candidate(source, row, record)

    def test_retry_uses_the_verified_file_after_a_failed_commit(self):
        self.set_mode("database")
        with mock.patch.object(MediaStore, "put_file", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "disk full"):
                self.run_one_request(None)
        pending = self.state / "requests/verified/7"
        self.assertEqual([p.read_bytes() for p in pending.iterdir()], [AUDIO_BYTES])
        with mock.patch.object(runner, "managed_candidate", side_effect=AssertionError("unexpected transfer")), \
                mock.patch.object(runner, "probe", return_value=dict(self.media(), codec="flac", container="flac")):
            row = {"id": 7, "artist": "Artist 1", "title": "Song One", "album": "Album", "length": 200,
                   "search_title": ""}
            self.assertEqual(runner.run_request(row, None), ("downloaded", "Ready to play", blob_ref(sha256(AUDIO_BYTES))))
        self.assertEqual(list(pending.iterdir()), [])

    def test_retry_releases_a_committed_request_duplicate_at_the_space_floor(self):
        candidate = self.state / "requests/verified/7/incoming-stopped.flac"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(AUDIO_BYTES)
        store = MediaStore(self.root / "media.sqlite3")
        key = store.put_file(candidate, dict(self.media(), codec="flac", container="flac"),
                             owners=(("request", "7"),))
        row = {"id": 7, "artist": "Artist 1", "title": "Song One", "length": 200, "search_title": ""}

        with mock.patch.object(runner, "free_bytes", return_value=0), \
                mock.patch.object(runner, "managed_candidate", side_effect=AssertionError("unexpected transfer")):
            self.assertEqual(runner.run_request(row, None), ("downloaded", "Ready to play", blob_ref(key)))

        self.assertFalse(candidate.exists())
        self.assertEqual(store.count(), 1)

    def test_rebuild_releases_only_exact_finished_request_staging(self):
        self.write_manifest([self.record(1, "Song One")])
        requests = runner.requests_store()
        request_id = requests.enqueue(None, "Artist 1", "Song One", "", 200, "")
        pending = self.state / "requests/verified" / str(request_id) / "incoming-stopped.flac"
        orphan = self.state / "requests" / f"incoming-{request_id}-abandoned" / "song.flac"
        alternate = orphan.with_name("alternate.flac")
        for path in (pending, orphan):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(AUDIO_BYTES)
        alternate.write_bytes(b"unique bytes")
        store = MediaStore(self.root / "media.sqlite3")
        key = store.put_file(pending, self.media(), owners=(("request", str(request_id)),))
        requests.finish(request_id, "downloaded", "Ready to play", blob_ref(key))

        self.run_report()

        self.assertFalse(pending.exists())
        self.assertFalse(orphan.exists())
        self.assertEqual(alternate.read_bytes(), b"unique bytes")
        self.assertTrue(store.verifies_original(key))

    def test_a_custom_request_lands_as_a_blob_ref(self):
        self.set_mode("database")
        status, _message, audio_path = self.run_one_request(None)
        self.assertEqual(status, "downloaded")
        self.assertEqual(audio_path, blob_ref(sha256(AUDIO_BYTES)))
        self.assertTrue(self.media_store().has(sha256(AUDIO_BYTES)))

    def test_a_catalog_request_lands_as_a_blob_ref(self):
        self.set_mode("database")
        status, _message, audio_path = self.run_one_request(
            self.record(3, "Song One"))
        self.assertEqual(status, "downloaded")
        self.assertEqual(audio_path, blob_ref(sha256(AUDIO_BYTES)))

    def check_format_replacement(self, mode):
        self.set_mode(mode)
        record = self.record(1, "Song One")
        self.write_manifest([record], {"mix": [1]})
        requests = runner.requests_store()
        row, _ = requests.create_or_find(1, "Artist 1", "Song One", "", 200, "", individual=True)
        old_file = self.library / "1 - original.flac"
        old_file.write_bytes(b"original FLAC")
        previous = old_file.name
        if mode == "database":
            previous = blob_ref(runner.media_store(create=True).put_file(
                old_file, dict(self.media(), codec="flac", container="flac"),
                owners=(("catalog", record["key"]),)))
        self.write_coverage([{**record, "status": "verified", "path": previous}])
        requests.confirm_format(row["id"], "alac", 0)
        pending = self.state / "requests/verified" / str(row["id"])
        pending.mkdir(parents=True)
        (pending / "incoming-new.m4a").write_bytes(b"new ALAC")
        def probe(path):
            codec, container = ("alac", "mov") if path.suffix == ".m4a" else ("flac", "flac")
            return dict(self.media(), codec=codec, container=container)
        with mock.patch.object(runner, "probe", probe), contextlib.redirect_stdout(io.StringIO()):
            runner.process_requests([record])
            runner.report()
            runner.run_export()
        selected = requests.get(row["id"])["audio_path"]
        self.assertEqual(self.coverage_rows()[1]["path"], selected)
        self.assertEqual(old_file.read_bytes(), b"original FLAC")
        if mode == "database":
            self.assertTrue(self.media_store().has(sha256(b"original FLAC")))
        self.assertEqual(next((self.root / "exported").glob("*.m4a")).read_bytes(), b"new ALAC")

    def test_confirmed_replacement_becomes_playable_and_exports_original_alac(self):
        self.check_format_replacement("database")

    def test_file_landing_preserves_and_prefers_the_confirmed_replacement(self):
        self.check_format_replacement("files")

    def test_bulk_worker_skips_metadata_imports_before_and_after_a_cursor_advance(self):
        tracks = [self.record(i, f"Song {i}") for i in range(1, 5)]
        tracks[1]["manual_copy"] = tracks[3]["manual_copy"] = True
        self.write_manifest(tracks, {"mix": [1, 2, 3, 4]})
        with mock.patch.object(runner, "download_batch", return_value=True) as bulk:
            self.assertTrue(runner.run_batch(0, 4))
        self.assertEqual([call.args[:2] for call in bulk.call_args_list], [(0, 1), (2, 1)])
        self.assertEqual(runner.cursor_offset(), 4)
        summary = self.run_report()
        self.assertEqual(summary["attempted"], 2)
        self.assertEqual(summary["playlists"]["mix"]["attempted"], 2)
        self.assertEqual([self.coverage_rows()[i]["status"] for i in range(1, 5)],
                         ["unmatched", "pending", "unmatched", "pending"])
        runner.prepare()
        self.assertTrue(runner.load_manifest()["records"][1]["manual_copy"])

    def test_bulk_pass_skips_waiting_and_previously_confirmed_catalog_songs(self):
        self.write_manifest([self.record(i, f"Song {i}") for i in range(1, 6)])
        requests = runner.requests_store()
        requests.create_or_find(2, "Artist", "Waiting", "", None, "", individual=True)
        row, _ = requests.create_or_find(4, "Artist", "Missing", "", None, "", individual=True)
        requests.confirm_format(row["id"], "alac", 0)
        requests.finish(row["id"], "not_found", "No verified ALAC", "")
        with mock.patch.object(runner, "download_batch", return_value=True) as bulk:
            self.assertTrue(runner.run_batch(0, 5, advance_cursor=False))
        self.assertEqual([call.args[:2] for call in bulk.call_args_list], [(0, 1), (2, 1), (4, 1)])

    def test_files_mode_still_lands_a_file(self):
        # Files mode is pinned explicitly (plan 062): it used to
        # be the implicit default this test silently relied on.
        self.set_mode("files")
        status, _message, audio_path = self.run_one_request(None)
        self.assertEqual(status, "downloaded")
        self.assertFalse(is_blob_ref(audio_path))
        self.assertTrue((self.library / "custom" / "7.flac").is_file())


class ExportTests(RunnerStoreTestCase):
    """Plan 060 step 6: the export writes referenced blob tracks
    back out as hash-equal files under portable names."""

    def test_export_writes_portable_hash_equal_files(self):
        record = self.record(1, "Song One")
        self.write_manifest([record], playlists={"mix": [1]})
        store = MediaStore(self.root / "media.sqlite3")
        source = self.root / "seed.mp3"
        source.write_bytes(AUDIO_BYTES)
        key = store.put_file(source, self.media())
        self.write_coverage([{
            "index": 1, "status": "verified", "artist": record["artist"],
            "title": record["title"], "uri": record["uri"],
            "path": blob_ref(key), "reason": ""}])
        custom_bytes = b"a custom request's audio" * 4
        custom_source = self.root / "custom.mp3"
        custom_source.write_bytes(custom_bytes)
        custom_key = store.put_file(
            custom_source, self.media("Custom Song", "Custom Artist"))
        requests = RequestsStore(self.state / "requests.sqlite3")
        request_id = requests.enqueue(None, "Custom Artist", "Custom Song",
                                      "", 0, "Custom Song")
        requests.finish(request_id, "downloaded", "Ready to play",
                        blob_ref(custom_key))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.run_export()
        exported = self.root / "exported" / "00001 - Artist 1 - Song One.mp3"
        self.assertEqual(exported.read_bytes(), AUDIO_BYTES)
        self.assertEqual(sha256(exported.read_bytes()), key)
        custom = (self.root / "exported" / "custom"
                  / f"{request_id} - Custom Artist - Custom Song.mp3")
        self.assertEqual(custom.read_bytes(), custom_bytes)
        status = json.loads((self.state / "export-status.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["failed"], 0)
        self.assertEqual(status["total"], 2)
        # Read-only against the store: both rows are still there.
        self.assertEqual(store.count(), 2)

    def test_export_ignores_a_stale_catalog_request(self):
        record = self.record(1, "Song One")
        self.write_manifest([record], playlists={"mix": [1]})
        store = MediaStore(self.root / "media.sqlite3")
        source = self.root / "old.mp3"
        source.write_bytes(b"the superseded download" * 4)
        stale_key = store.put_file(source, self.media())
        # The record's verified home is a plain file now; an old
        # downloaded request still names the blob.
        self.write_coverage([{
            "index": 1, "status": "verified", "artist": record["artist"],
            "title": record["title"], "uri": record["uri"],
            "path": "1 - Artist 1 - Song One.mp3", "reason": ""}])
        requests = RequestsStore(self.state / "requests.sqlite3")
        request_id = requests.enqueue(1, record["artist"],
                                      record["title"], "", 0,
                                      record["title"])
        requests.finish(request_id, "downloaded", "Ready to play",
                        blob_ref(stale_key))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.run_export()
        status = json.loads((self.state / "export-status.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(status["total"], 0)
        self.assertFalse((self.root / "exported").exists())

    def test_export_writes_a_shared_key_under_each_records_name(self):
        records = [self.record(1, "Song One"),
                   self.record(2, "Song One (reprise)", artist="Artist 1")]
        self.write_manifest(records, playlists={"mix": [1, 2]})
        store = MediaStore(self.root / "media.sqlite3")
        source = self.root / "seed.mp3"
        source.write_bytes(AUDIO_BYTES)
        key = store.put_file(source, self.media())
        self.write_coverage([{
            "index": record["index"], "status": "verified",
            "artist": record["artist"], "title": record["title"],
            "uri": record["uri"], "path": blob_ref(key), "reason": ""}
            for record in records])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.run_export()
        first = self.root / "exported" / "00001 - Artist 1 - Song One.mp3"
        second = (self.root / "exported"
                  / "00002 - Artist 1 - Song One (reprise).mp3")
        self.assertEqual(first.read_bytes(), AUDIO_BYTES)
        self.assertEqual(second.read_bytes(), AUDIO_BYTES)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg")
class LosslessIntakeTests(RunnerStoreTestCase):
    def test_alac_only_rejects_aac_in_m4a(self):
        self.set_mode("database")
        title = "Format fixture"
        path = self.root / "aac.m4a"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "anoisesrc=color=white:duration=2:sample_rate=44100:seed=7",
                        "-ac", "2", "-c:a", "aac", "-b:a", "320k", "-metadata",
                        "title=" + title, str(path)], check=True)
        facts = runner.probe(path)
        self.assertEqual(facts["codec"], "aac")
        self.assertGreaterEqual(facts["bitrate"], 192000)
        row = {"id": 1, "artist": "Fixture", "title": title, "length": 2,
               "album": "", "search_title": title, "format_confirmed": 1, "requested_format": "alac"}
        status, message, ref = self.receive_candidate(path, row, None)
        self.assertEqual((status, ref), ("rejected", ""))
        self.assertIn("audio verification", message)

    def test_new_downloads_and_export_recover_original_lossless_files(self):
        room = self.root / "portable"
        create_empty_collection(room)
        database = room / "collection.sqlite3"
        # A migrated host can retain an old file-landing setting on disk.
        self.set_mode("files")
        original_bytes = {}
        records = []
        coverage = []
        with mock.patch.object(runner, "ROOT", room), mock.patch.object(runner, "REQUESTS_DB", database):
            store = MediaStore(database)
            for i, (codec, suffix) in enumerate((("flac", ".flac"), ("alac", ".m4a"), ("aac", ".m4a")), 1):
                title = f"Lossless test {i}"
                song = self.root / (title + suffix)
                subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                                "anoisesrc=color=white:duration=2:sample_rate=44100:seed=7",
                                "-ac", "2", "-c:a", codec, "-b:a", "320k", "-metadata", "title=" + title,
                                str(song)], check=True, timeout=30)

                record = self.record(i, title, length=2)
                row = {"id": i, "title": title, "album": "Album", "search_title": ""}
                status, reason, ref = self.receive_candidate(song, row, record)
                if codec == "aac":
                    # Identical M4A container and a qualifying bitrate. Its stream is lossy.
                    self.assertGreaterEqual(runner.probe(song)["bitrate"], 192000)
                    self.assertEqual((status, ref), ("rejected", ""))
                    continue
                self.assertEqual(status, "downloaded", reason)
                self.assertEqual(ref, blob_ref(sha256(song.read_bytes())))
                original_bytes[f"{i:05d} - {record['artist']} - {title}{suffix}"] = song.read_bytes()
                records.append(record)
                coverage.append({"index": i, "status": "verified", "artist": record["artist"],
                                 "title": title, "uri": record["uri"], "path": ref, "reason": ""})
            self.assertEqual(store.count(), 2)
            # A migrated row with no active claim must also be recoverable through full export.
            orphan = self.root / "previous.mp3"
            orphan.write_bytes(AUDIO_BYTES)
            archived_key = store.put_file(orphan, self.media())
            original_bytes[f"originals/{archived_key}.mp3"] = AUDIO_BYTES
            runner.catalog_store().write_build({"records": records, "playlists": {}, "issues": [],
                                                "source_signature": {}})
            self.write_coverage(coverage)
            with contextlib.redirect_stdout(io.StringIO()):
                runner.run_export()
            exported = {p.relative_to(room / "exported").as_posix(): p.read_bytes()
                        for p in (room / "exported").rglob("*") if p.is_file()}
            self.assertEqual(exported, original_bytes)

    def test_bulk_staged_mp3_is_published_and_existing_flac_is_preserved(self):
        self.write_manifest([self.record(1, "Song One"), self.record(2, "Song Two")])
        runner.write_cursor(2)
        original = self.library / "2 - Song Two.flac"
        original.write_bytes(b"existing FLAC")
        staged = self.state / "bulk-incoming" / "1 - Song One.mp3"
        staged.parent.mkdir()
        staged.write_bytes(AUDIO_BYTES)

        def facts(path):
            if path.suffix == ".mp3":
                return dict(self.media(title="Song One"), codec="mp3", container="mp3", stream_bitrate=192_000)
            return dict(self.media(title="Song Two"), codec="flac", container="flac")

        self.set_mode("files")
        with mock.patch.object(runner, "probe", side_effect=facts):
            self.run_report()
        self.assertEqual(self.coverage_rows()[1]["status"], "verified")
        self.assertEqual((self.library / staged.name).read_bytes(), AUDIO_BYTES)
        self.assertEqual(original.read_bytes(), b"existing FLAC")
        self.assertEqual(self.coverage_rows()[2]["status"], "verified")

    def test_bulk_rejects_disguised_lossy_stream_in_both_landing_modes(self):
        record = self.record(1, "Song One")
        self.write_manifest([record])
        runner.write_cursor(1)
        incoming = self.state / "bulk-incoming"
        incoming.mkdir()
        (incoming / "1 - Song One.flac").write_bytes(AUDIO_BYTES)
        with mock.patch.object(runner, "probe", return_value=dict(self.media(), codec="mp3", container="mp3")):
            for mode in ("files", "database"):
                self.set_mode(mode)
                self.run_report()
                rejection = self.coverage_rows()[1]
                self.assertEqual(rejection["status"], "rejected")
                self.assertIn("requires FLAC", rejection["reason"])
                self.assertEqual(list(self.library.iterdir()), [])
        self.assertEqual(self.media_store().count(), 0)


class ScopedExportTests(RunnerStoreTestCase):
    """Plan 063 settlement 2: the export scoped to one playlist
    writes only that playlist's database songs, into the
    playlist's own folder under exported/, with an M3U8 manifest
    in playlist order. The scope and the folder's display name
    arrive in the runner's environment, the way the server
    starts the worker."""

    def seed_blob(self, record):
        store = MediaStore(self.root / "media.sqlite3")
        source = self.root / f"seed-{record['index']}.mp3"
        source.write_bytes(f"audio bytes for track {record['index']}"
                           .encode() * 8)
        key = store.put_file(source, self.media(record["title"],
                                                record["artist"]))
        return {"index": record["index"], "status": "verified",
                "artist": record["artist"], "title": record["title"],
                "uri": record["uri"], "path": blob_ref(key),
                "reason": ""}

    def run_scoped(self, scope, name=None):
        env = {"OFFLINE_MUSIC_SCOPE": scope}
        if name is not None:
            env["OFFLINE_MUSIC_EXPORT_NAME"] = name
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                contextlib.redirect_stdout(out):
            runner.run_export()
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def status(self):
        return json.loads((self.state / "export-status.json")
                          .read_text(encoding="utf-8"))

    def manifest_names(self, folder):
        manifest = next(folder.glob("*.m3u8")).read_text(encoding="utf-8")
        return [line for line in manifest.splitlines()
                if line and not line.startswith("#")]

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_compact_export_uses_real_audio_and_leaves_original_export_intact(self):
        records = [self.record(1, "Song One"), self.record(2, "Song Two")]
        store = MediaStore(self.root / "media.sqlite3")
        source = self.root / "tone.flac"
        source.unlink(missing_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=440:d=2", str(source)], check=True)
        key = store.put_file(source, self.media("Song One", "Artist 1"))
        self.write_manifest(records, playlists={"mix": [2, 1, 2]})
        self.write_coverage([
            {"index": row["index"], "status": "verified", "artist": row["artist"], "title": row["title"],
             "uri": row["uri"], "path": blob_ref(key), "reason": ""} for row in records
        ])

        self.run_scoped("mix", "Compact Opus")
        original = self.root / "exported" / "Compact Opus"
        with mock.patch.dict(os.environ, {"LISTENING_EXPORT_FORMAT": "opus"}):
            self.assertEqual(self.run_scoped("mix", "Mix")["exported"], 2)
            self.assertEqual(self.run_scoped("mix", "Mix")["exported"], 0)

        compact = next(self.root.glob("exported/Compact Opus - */Mix"))
        names = self.manifest_names(compact)
        self.assertEqual(names, ["00002 - Artist 2 - Song Two.opus", "00001 - Artist 1 - Song One.opus",
                                 "00002 - Artist 2 - Song Two.opus"])
        self.assertEqual(len(list((self.state / "compact-audio").glob("*.opus"))), 1)
        self.assertEqual((compact / names[0]).read_bytes(), (compact / names[1]).read_bytes())
        self.assertEqual((original / "00001 - Artist 1 - Song One.flac").read_bytes(), source.read_bytes())
        codec = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                                         "stream=codec_name", "-of", "csv=p=0", str(compact / names[0])])
        self.assertEqual(codec.strip(), b"opus")

    def test_a_playlist_named_custom_exports_under_its_key(self):
        # The whole-library export owns exported/custom/ for
        # custom requests; a playlist named Custom must not
        # mix its files into that folder.
        records = [self.record(1, "Song One")]
        self.write_manifest(records, playlists={"mix": [1]})
        self.write_coverage([self.seed_blob(record)
                             for record in records])
        result = self.run_scoped("mix", name="Custom")
        self.assertEqual(result["exported"], 1)
        self.assertTrue((self.root / "exported" / "mix").is_dir())
        self.assertFalse((self.root / "exported" / "custom").exists())
        self.assertEqual(self.status()["state"], "done")

    def test_only_the_playlists_tracks_land_in_its_folder(self):
        records = [self.record(1, "Song One"), self.record(2, "Song Two"),
                   self.record(3, "Song Three")]
        self.write_manifest(records,
                            playlists={"mix": [2, 1], "other": [3]})
        self.write_coverage([self.seed_blob(record) for record in records])
        # A custom request is in the library but in no playlist:
        # the whole-library export writes it, a scoped one cannot.
        store = MediaStore(self.root / "media.sqlite3")
        custom_source = self.root / "custom-seed.mp3"
        custom_source.write_bytes(b"custom audio" * 8)
        custom_key = store.put_file(
            custom_source, self.media("Custom Song", "Custom Artist"))
        requests = RequestsStore(self.state / "requests.sqlite3")
        request_id = requests.enqueue(None, "Custom Artist", "Custom Song",
                                      "", 0, "Custom Song")
        requests.finish(request_id, "downloaded", "Ready to play",
                        blob_ref(custom_key))
        result = self.run_scoped("mix", "Evening Mix")
        folder = self.root / "exported" / "Evening Mix"
        self.assertTrue(
            (folder / "00001 - Artist 1 - Song One.mp3").is_file())
        self.assertTrue(
            (folder / "00002 - Artist 2 - Song Two.mp3").is_file())
        self.assertFalse((self.root / "exported"
                          / "00003 - Artist 3 - Song Three.mp3").exists())
        self.assertFalse((folder / "custom").exists())
        self.assertEqual(result["total"], 2)
        status = self.status()
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["total"], 2)
        self.assertEqual(status["failed"], 0)
        # The manifest lists the tracks in playlist order (2, 1),
        # not catalog order.
        self.assertEqual(self.manifest_names(folder),
                         ["00002 - Artist 2 - Song Two.mp3",
                          "00001 - Artist 1 - Song One.mp3"])

    def test_mixed_exports_include_verified_files_and_preserve_playlist_order(self):
        (self.library / "2 - Artist 2 - Song Two.mp3").write_bytes(b"second fixture track")
        records = [self.record(1, "Song One"), self.record(2, "Song Two"),
                   self.record(3, "Song Three")]
        self.write_manifest(records, playlists={"mix": [1, 2, 3]})
        self.write_coverage([
            self.seed_blob(records[0]),
            {"index": 2, "status": "verified", "artist": "Artist 2",
             "title": "Song Two", "uri": records[1]["uri"],
             "path": "2 - Artist 2 - Song Two.mp3", "reason": ""},
            {"index": 3, "status": "pending", "artist": "Artist 3",
             "title": "Song Three", "uri": records[2]["uri"],
             "path": "", "reason": ""}])
        result = self.run_scoped("mix", "Mix")
        folder = self.root / "exported" / "Mix"
        self.assertEqual(result["total"], 2)
        self.assertEqual((folder / "00002 - Artist 2 - Song Two.mp3").read_bytes(), b"second fixture track")
        self.assertEqual(self.manifest_names(folder),
                         ["00001 - Artist 1 - Song One.mp3", "00002 - Artist 2 - Song Two.mp3"])
        self.run_scoped("mix", "Mix")
        self.assertEqual(self.status()["written"], 0)
        self.assertEqual(self.status()["skipped"], 2)

    def test_same_display_names_get_separate_manifests(self):
        records = [self.record(1, "Song One"), self.record(2, "Song Two")]
        self.write_manifest(records, playlists={"a": [1], "b": [2]})
        self.write_coverage([self.seed_blob(record) for record in records])
        self.run_scoped("a", "Mix")
        self.run_scoped("b", "Mix")
        manifests = sorted((self.root / "exported").rglob("*.m3u8"))
        self.assertEqual(len(manifests), 2)
        self.assertEqual({line for p in manifests for line in self.manifest_names(p.parent)},
                         {"00001 - Artist 1 - Song One.mp3", "00002 - Artist 2 - Song Two.mp3"})

    def test_a_repeated_track_is_listed_twice_and_written_once(self):
        record = self.record(1, "Song One")
        self.write_manifest([record], playlists={"mix": [1, 1]})
        self.write_coverage([self.seed_blob(record)])
        result = self.run_scoped("mix", "Mix")
        folder = self.root / "exported" / "Mix"
        self.assertEqual(result["total"], 1)
        self.assertEqual(self.manifest_names(folder),
                         ["00001 - Artist 1 - Song One.mp3"] * 2)

    def test_the_folder_falls_back_to_the_playlist_key(self):
        record = self.record(1, "Song One")
        self.write_manifest([record], playlists={"my_mix": [1]})
        self.write_coverage([self.seed_blob(record)])
        self.run_scoped("my_mix")
        folder = self.root / "exported" / "my mix"
        self.assertTrue(
            (folder / "00001 - Artist 1 - Song One.mp3").is_file())
        self.assertTrue((folder / "my mix.m3u8").is_file())

    def test_an_unknown_playlist_closes_the_status_out(self):
        record = self.record(1, "Song One")
        self.write_manifest([record], playlists={"mix": [1]})
        self.write_coverage([self.seed_blob(record)])
        result = self.run_scoped("nope", "Nope")
        self.assertEqual(result["total"], 0)
        status = self.status()
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["total"], 0)
        self.assertFalse((self.root / "exported").exists())


class ReclaimEnqueueTests(RunnerStoreTestCase):
    """Plan 060 steps 4/8: report deletes dead keys and queues
    the reclaim job once, only past the threshold."""

    def seed_dead_blob(self, size=64 * 1024):
        self.write_manifest([self.record(1, "Song One")],
                            playlists={"mix": [1]})
        runner.write_cursor(1)
        store = MediaStore(self.root / "media.sqlite3")
        source = self.root / "dead.mp3"
        source.write_bytes(os.urandom(size))
        return store.put_file(source, self.media())

    def test_reclaim_only_releases_explicitly_retired_audio(self):
        key = self.seed_dead_blob()
        with mock.patch.object(runner, "RECLAIM_THRESHOLD", 1024):
            self.run_report()
            self.assertTrue(self.media_store().has(key))
            self.media_store().delete(key)
            self.run_report()
            queue = JobQueue(self.state / "requests.sqlite3")
            self.assertIsNotNone(queue.queued_for("reclaim"))
            self.run_report()
            rows = [row for row in queue.queued()
                    if row["kind"] == "reclaim"]
            self.assertEqual(len(rows), 1)

    def test_no_enqueue_below_the_threshold(self):
        self.seed_dead_blob()
        with mock.patch.object(runner, "RECLAIM_THRESHOLD", 10**12):
            self.run_report()
        queue = JobQueue(self.state / "requests.sqlite3")
        self.assertIsNone(queue.queued_for("reclaim"))


class ReclaimJobTests(unittest.TestCase):
    """Plan 060 step 8: the reclaim card on the jobs layer."""

    def test_the_reclaim_card_reports_dead_bytes(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        database = Path(folder.name) / "requests.sqlite3"
        running = {"downloads": False, "requests": False,
                   "genres": False, "reclaim": False, "migrate": False}
        jobs = Jobs(
            queue=JobQueue(database),
            worker_running=lambda kind: running[kind],
            start_worker=lambda kind, scope=None: True,
            stop_worker=lambda kind: None,
            genre_tools_ready=lambda: True,
            download_ready=lambda: True,
            lists_ready=lambda: True,
            refresh_library=lambda force=False: None,
            save_settings=lambda: None,
            settings={},
            catalog=lambda: SimpleNamespace(records=[], summary={}),
            pending_requests=lambda: 0,
            playlist_options=lambda: [],
            media_dead_bytes=lambda: 4096)
        cards = {card["kind"]: card for card in jobs.snapshot()["jobs"]}
        self.assertIn("reclaim", cards)
        self.assertEqual(cards["reclaim"]["progress"],
                         {"dead_bytes": 4096})
        self.assertEqual(cards["reclaim"]["blocker"], "")


class BlobServingTests(unittest.TestCase):
    """Plan 060 step 5, end to end: a blob-backed track serves
    through the real server's audio route, ranges included."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.workspace = base / "room"
        cls.workspace.mkdir()
        config_dir = base / "config"
        config_dir.mkdir()
        cls.config_dir = config_dir
        config_path = config_dir / "config.json"
        config_path.write_text(json.dumps({"folder": str(cls.workspace)}),
                               encoding="utf-8")
        server.init(config_path)
        store = MediaStore(cls.workspace / "media.sqlite3")
        source = base / "seed.mp3"
        source.write_bytes(AUDIO_BYTES)
        cls.key = store.put_file(source, dict(PROBE))
        record = {"index": 1, "key": "key-1", "uri": "spotify:track:1",
                  "artist": "Artist 1", "title": "Song One",
                  "album": "Album", "length": 200, "isrc": "",
                  "first_playlist": "mix", "active": True}
        records = []
        coverage = []
        cls.samples = {1: AUDIO_BYTES}
        for index in range(1, 6):
            if index != 1:
                cls.samples[index] = bytes([index]) * 1000
                source.write_bytes(cls.samples[index])
            key = store.put_file(source, dict(PROBE))
            records.append({**record, "index": index, "key": f"key-{index}",
                            "uri": f"spotify:track:{index}"})
            coverage.append({"index": index, "status": "verified", "artist": "Artist 1",
                             "title": "Song One", "uri": records[-1]["uri"],
                             "path": blob_ref(key), "reason": ""})
        catalog_store = CatalogStore(server.DB)
        catalog_store.write_build({"records": records, "playlists": {"mix": list(cls.samples)},
                                   "issues": [], "source_signature": {}})
        catalog_store.write_coverage(coverage)
        server.catalog.refresh()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever,
                         daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def request(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port,
                                                timeout=10)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        connection.close()
        return result

    def test_five_simultaneous_range_reads_survive_three_slot_eviction(self):
        active = threading.Barrier(len(self.samples), timeout=10)
        send = server.Handler.send_audio_range

        def read_after_eviction(handler, handle, name, **options):
            active.wait()
            send(handler, handle, name, **options)

        with mock.patch.object(server.Handler, "send_audio_range", read_after_eviction):
            with ThreadPoolExecutor(max_workers=len(self.samples)) as pool:
                requests = {index: pool.submit(self.request, f"/api/audio/{index}",
                                               {"Range": "bytes=4-11"}) for index in self.samples}
                for index, response in requests.items():
                    status, _, data = response.result()
                    self.assertEqual((status, data), (206, self.samples[index][4:12]))

    def test_card_transfer_uses_verified_bytes_and_validates_resume_identity(self):
        path = "/api/audio/1?save=1"
        status, headers, audio = self.request(path)
        self.assertEqual((status, audio), (200, AUDIO_BYTES))
        tag = '"' + hashlib.sha256(audio).hexdigest() + '"'
        self.assertEqual(headers["ETag"], tag)
        self.assertEqual(headers["X-Audio-Extension"], ".mp3")
        status, _, suffix = self.request(path, {"Range": "bytes=5-", "If-Range": tag})
        self.assertEqual((status, suffix), (206, audio[5:]))
        status, _, body = self.request(path, {"Range": "bytes=5-", "If-Range": '"stale"'})
        self.assertEqual((status, body), (200, audio))

    def test_a_blob_track_serves_whole(self):
        status, _headers, data = self.request("/api/audio/1")
        self.assertEqual(status, 200)
        self.assertEqual(data, AUDIO_BYTES)

    def test_a_blob_track_serves_a_range(self):
        status, headers, data = self.request(
            "/api/audio/1", headers={"Range": "bytes=4-11"})
        self.assertEqual(status, 206)
        self.assertEqual(data, AUDIO_BYTES[4:12])
        self.assertEqual(headers.get("Content-Range"),
                         f"bytes 4-11/{len(AUDIO_BYTES)}")
        # The bytes came from the playback buffer's staged copy.
        staged = self.config_dir / "buffer" / f"{self.key}.mp3"
        self.assertTrue(staged.is_file())


if __name__ == "__main__":
    unittest.main()
