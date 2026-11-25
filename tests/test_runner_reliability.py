import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from fixture_environment import isolate_room_environment as setUpModule
from runner_fixture import runner
from catalog.catalog_store import CatalogStore
from downloads import verification


def reset_runner_caches():
    """Drop the per-invocation module caches so a test is a fresh run."""
    if hasattr(runner, "_probe_cache"):
        runner._probe_cache = None
    if hasattr(runner, "_probe_cache_dirty"):
        runner._probe_cache_dirty = False
    if hasattr(runner, "_REPORT_DIRTY"):
        runner._REPORT_DIRTY = False


class WorkspaceTestCase(unittest.TestCase):
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
            mock.patch.object(runner, "STATE", self.state),
            mock.patch.object(runner, "LIBRARY", self.library),
            mock.patch.object(runner, "PLAYLISTS", self.playlists_dir),
            mock.patch.object(runner, "OUTPUTS", self.outputs),
            mock.patch.object(runner, "SOURCES", self.sources),
            mock.patch.object(runner, "MASTER", self.state / "master.csv"),
            mock.patch.object(runner, "REQUESTS_DB", self.state / "requests.sqlite3"),
            mock.patch.object(runner, "free_bytes", lambda: 100 * 1024**3),
            # Plan 062: these fixtures characterize the report's
            # file-home behavior and never modeled a settings
            # file; the landing mode is pinned to "files" now
            # that "database" is the default (landing itself is
            # covered, with explicit modes, in test_audio_store).
            mock.patch.object(runner, "audio_store_mode", lambda: "files"),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def record(self, index, title, length=200, active=True):
        return {"index": index, "key": f"key-{index}", "uri": f"spotify:track:{index}",
                "artist": f"Artist {index}", "title": title, "album": "Album",
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


class ReportCharacterizationTests(WorkspaceTestCase):
    """A6 test 11 / A3 TEST-02: pin report() behavior before changing it.

    One track per status: verified; rejected for low bitrate, duration,
    and title (with the detailed coverage reason strings); unmatched;
    pending; retired.
    """

    MEDIA = {
        "1 - Song One.mp3": {"duration": 200.0, "bitrate": 320000, "title": "Song One"},
        "2 - Song Two.mp3": {"duration": 200.0, "bitrate": 128000, "title": "Song Two"},
        "3 - Song Three.mp3": {"duration": 220.0, "bitrate": 320000, "title": "Song Three"},
        "4 - Song Four.mp3": {"duration": 200.0, "bitrate": 320000,
                              "title": "Another Song Entirely"},
    }

    def test_report_assigns_each_status_and_reason(self):
        records = [self.record(1, "Song One"), self.record(2, "Song Two"),
                   self.record(3, "Song Three"), self.record(4, "Song Four"),
                   self.record(5, "Song Five"), self.record(6, "Song Six", active=False),
                   self.record(7, "Song Seven")]
        self.write_manifest(records, playlists={"mix": [1, 2, 3, 4, 5, 6, 7]})
        self.write_cursor(6)
        for name in self.MEDIA:
            (self.library / name).write_bytes(b"audio")
        with mock.patch.object(runner, "probe", lambda path: self.MEDIA[path.name]):
            summary = self.run_report()
        rows = self.coverage_rows()
        self.assertEqual(rows[1]["status"], "verified")
        self.assertEqual(rows[1]["reason"], "")
        self.assertTrue(rows[1]["path"].endswith("1 - Song One.mp3"))
        self.assertEqual(rows[2]["status"], "rejected")
        self.assertEqual(rows[2]["reason"], "low bitrate 128000")
        self.assertEqual(rows[3]["status"], "rejected")
        self.assertEqual(rows[3]["reason"], "duration differs by 20.0s")
        self.assertEqual(rows[4]["status"], "rejected")
        self.assertEqual(rows[4]["reason"], "title tag differs: Another Song Entirely")
        self.assertEqual(rows[5]["status"], "unmatched")
        self.assertEqual(rows[5]["reason"], "")
        self.assertEqual(rows[6]["status"], "retired")
        self.assertEqual(rows[7]["status"], "pending")
        self.assertEqual(summary["verified"], 1)
        self.assertEqual(summary["rejected"], 3)
        self.assertEqual(summary["unmatched"], 1)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["attempted"], 5)
        self.assertEqual(summary["playlists"]["mix"],
                         {"entries": 7, "attempted": 6, "playable": 1})
        m3u8 = (self.playlists_dir / "mix.m3u8").read_text(encoding="utf-8")
        self.assertEqual(m3u8.count("#EXTINF"), 1)
        self.assertIn("Song One", m3u8)
        self.assertNotIn("Song Two", m3u8)


class PrepareRetirementAndKeyingTests(WorkspaceTestCase):
    """Plan 019 / A6 proposals 1 and prepare table: prepare() keying."""

    def write_source(self, name, content):
        path = self.sources / name
        path.write_text(content, encoding="utf-8")
        return path

    def run_prepare(self):
        with contextlib.redirect_stdout(io.StringIO()):
            runner.prepare()
        return runner.catalog_store().read_build()

    def test_prepare_retires_a_removed_track_without_renumbering(self):
        self.write_source("mix.csv", "Track URI,Track Name,Artist Name(s)\n"
                          "spotify:track:a,Song A,Artist\n"
                          "spotify:track:b,Song B,Artist\n")
        self.write_source("other.csv", "Track URI,Track Name,Artist Name(s)\n"
                          "spotify:track:c,Song C,Artist\n")
        payload = self.run_prepare()
        self.assertEqual([record["index"] for record in payload["records"]], [1, 2, 3])
        self.assertEqual(payload["playlists"]["mix"], [1, 2])
        self.assertEqual(payload["playlists"]["other"], [3])
        mix = self.sources / "mix.csv"
        mix.write_text("Track URI,Track Name,Artist Name(s)\n"
                       "spotify:track:a,Song A,Artist\n", encoding="utf-8")
        os.utime(mix, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
        payload = self.run_prepare()
        by_index = {record["index"]: record for record in payload["records"]}
        self.assertEqual(sorted(by_index), [1, 2, 3])
        self.assertTrue(by_index[1]["active"])
        self.assertFalse(by_index[2]["active"])
        self.assertTrue(by_index[3]["active"])
        self.assertEqual(payload["playlists"]["mix"], [1])
        self.assertEqual(payload["playlists"]["other"], [3])

    def test_prepare_duplicate_key_appears_in_both_playlists(self):
        self.write_source("mix.csv", "Track URI,Track Name,Artist Name(s),Album Name\n"
                          "spotify:track:shared,Shared Song,Artist,First Album\n"
                          "spotify:track:only-mix,Only Mix,Artist,Album\n")
        self.write_source("other.csv", "Track URI,Track Name,Artist Name(s),Album Name\n"
                          "spotify:track:shared,Shared Song,Artist,Second Album\n")
        payload = self.run_prepare()
        self.assertEqual(len(payload["records"]), 2)
        shared = next(record for record in payload["records"]
                      if record["key"] == "spotify:track:shared")
        self.assertIn(shared["index"], payload["playlists"]["mix"])
        self.assertIn(shared["index"], payload["playlists"]["other"])
        self.assertEqual(shared["album"], "Second Album")

    def test_prepare_keys_by_casefolded_tuple_without_an_identifier(self):
        self.write_source("mix.csv", "Title,Artist,Album\n"
                          "The Title,The Artist,The Album\n"
                          "the title,the artist,the album\n"
                          "The Title,The Artist,Another Album\n")
        payload = self.run_prepare()
        self.assertEqual(len(payload["records"]), 2)
        self.assertEqual(payload["playlists"]["mix"], [1, 1, 2])

    def test_prepare_incomplete_row_records_an_issue_and_a_gap(self):
        self.write_source("mix.csv", "Title,Artist\nFirst,Artist\nIncomplete,\nLast,Other\n")
        payload = self.run_prepare()
        self.assertEqual(payload["playlists"]["mix"], [1, None, 2])
        self.assertEqual(len(payload["issues"]), 1)
        issue = payload["issues"][0]
        self.assertEqual(issue["playlist"], "mix.xspf")
        self.assertEqual(issue["line"], 2)


class FakeBatchProcess:
    """A Sockseek stand-in that writes a canned batch log and exits 0."""

    log_text = ""
    exit_code = 0

    def __init__(self, command, stdout=None, stderr=None):
        self.returncode = type(self).exit_code
        if stdout is not None:
            stdout.write(type(self).log_text)
            stdout.flush()

    def poll(self):
        return 0


class CursorDisciplineTests(WorkspaceTestCase):
    """A6 test 6: the cursor advances only on a complete batch log."""

    def setUp(self):
        super().setUp()
        self.sockseek = self.root / "sockseek"
        self.sockseek.write_bytes(b"bin")
        for patcher in (mock.patch.object(runner, "SOCKSEEK", self.sockseek),
                        mock.patch.object(runner, "setup_credentials", lambda: None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.write_manifest([self.record(i, f"Song {i}") for i in (1, 2, 3)])
        self.write_cursor(0)

    def run_batch_with_log(self, log_text, exit_code=0):
        with (mock.patch.object(FakeBatchProcess, "log_text", log_text),
              mock.patch.object(FakeBatchProcess, "exit_code", exit_code),
              mock.patch.object(subprocess, "Popen", FakeBatchProcess)):
            with contextlib.redirect_stdout(io.StringIO()):
                return runner.run_batch(0, 3)

    def cursor_offset_on_disk(self):
        return {"next_offset": runner.cursor_offset()}

    def test_run_batch_does_not_advance_cursor_on_incomplete_log(self):
        self.assertFalse(self.run_batch_with_log("Completed: 1 succeeded, 1 failed\n"))
        self.assertEqual(self.cursor_offset_on_disk(), {"next_offset": 0})

    def test_run_batch_does_not_advance_cursor_without_a_completed_line(self):
        self.assertFalse(self.run_batch_with_log("searching...\n"))
        self.assertEqual(self.cursor_offset_on_disk(), {"next_offset": 0})

    def test_run_batch_advances_cursor_on_a_complete_log(self):
        self.assertTrue(self.run_batch_with_log("Completed: 2 succeeded, 1 failed\n"))
        self.assertEqual(self.cursor_offset_on_disk(), {"next_offset": 3})

    def test_run_batch_advances_from_sockseek_song_results(self):
        self.assertTrue(self.run_batch_with_log(
            "[002] Job List: running: master\n"
            "[003] SongJob: succeeded: Song 1\n"
            "[004] SongJob: failed [No search results]: Song 2\n"
            "[005] SongJob: succeeded: Song 3\n"
            "[002] Job List: partial: master\n",
            exit_code=1,
        ))
        self.assertEqual(self.cursor_offset_on_disk(), {"next_offset": 3})

    def test_retry_message_and_repeated_job_do_not_complete_batch(self):
        self.assertFalse(self.run_batch_with_log(
            "[003] SongJob: succeeded: Song 1\n"
            "[003] SongJob: succeeded: Song 1\n"
            "[004] SongJob: failed [All downloads failed]: Song 2\n"
            "[005] SongJob: download attempt failed: Song 3\n"
        ))
        self.assertEqual(self.cursor_offset_on_disk(), {"next_offset": 0})

    def test_uses_a_fresh_sockseek_index_after_an_interrupted_batch(self):
        stale = self.state / "index-00001-00003.csv"
        stale.write_text("stale")
        log = "Completed: 3 succeeded, 0 failed\n"

        def process(command, **kwargs):
            index = Path(command[command.index("--index-path") + 1])
            self.assertFalse(index.exists())
            index.write_text("new result")
            return FakeBatchProcess(command, **kwargs)

        with (mock.patch.object(FakeBatchProcess, "log_text", log),
              mock.patch.object(subprocess, "Popen", side_effect=process),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertTrue(runner.run_batch(0, 3))
        self.assertEqual(stale.read_text(), "new result")
        self.assertEqual(self.cursor_offset_on_disk(), {"next_offset": 3})

    def test_song_title_cannot_supply_a_completed_summary(self):
        self.assertFalse(self.run_batch_with_log(
            "[003] SongJob: succeeded: Completed: 0 succeeded, 3 failed\n"
            "[error] [cli] Soulseek connection unavailable\n",
            exit_code=1,
        ))
        self.assertEqual(self.cursor_offset_on_disk(), {"next_offset": 0})

    def test_completed_no_match_batch_advances_with_the_work_failed_exit(self):
        self.assertTrue(self.run_batch_with_log("[cli] Completed: 0 succeeded, 3 failed.\n", exit_code=1))
        self.assertEqual(self.cursor_offset_on_disk(), {"next_offset": 3})

    def test_usage_cancellation_and_unexplained_failures_do_not_advance_cursor(self):
        for code in (1, 2, 130, -15):
            with self.subTest(code=code):
                self.assertFalse(self.run_batch_with_log("Completed: 3 succeeded, 0 failed\n", exit_code=code))
                self.assertEqual(self.cursor_offset_on_disk(), {"next_offset": 0})


class ReportBatchingTests(WorkspaceTestCase):
    """A3 PERF-02: one report write at the end of process_requests."""

    def seed_requests(self, count):
        connection = runner.request_connection()
        with connection:
            for i in range(count):
                connection.execute("INSERT INTO requests (artist, title) VALUES (?, ?)",
                                   (f"Artist {i}", f"Title {i}"))
        connection.close()

    def test_report_is_written_once_at_the_end_of_process_requests(self):
        self.seed_requests(50)
        calls = []
        stub = lambda row, record: ("downloaded", "Ready to play", "/fake/song.mp3")
        with mock.patch.object(runner, "run_request", stub), \
                mock.patch.object(runner, "report", lambda: calls.append(1)):
            with contextlib.redirect_stdout(io.StringIO()):
                runner.process_requests([], limit=100000)
        self.assertEqual(len(calls), 1)
        connection = runner.request_connection()
        done = connection.execute(
            "SELECT COUNT(*) FROM requests WHERE status = 'downloaded'").fetchone()[0]
        connection.close()
        self.assertEqual(done, 50)

    def test_no_report_when_no_requests_were_processed(self):
        calls = []
        with mock.patch.object(runner, "report", lambda: calls.append(1)):
            runner.process_requests([], limit=100000)
        self.assertEqual(calls, [])

    def test_transient_failure_releases_the_next_request_and_eventually_stops_retrying(self):
        self.seed_requests(2)
        store = runner.requests_store()
        attempted = []

        def run_request(row, _):
            attempted.append(row["id"])
            if row["id"] == 1:
                raise runner.CandidateTimeout("Transfer timed out")
            return "not_found", "No copy found", ""

        with mock.patch.object(runner, "run_request", run_request), \
                mock.patch("downloads.requests_store.time.time", return_value=1000) as clock:
            runner.process_requests([])
            self.assertEqual(attempted, [1, 2])
            self.assertEqual(store.get(2)["status"], "not_found")
            self.assertEqual(store.get(1)["retry_at"], 1030)
            self.assertFalse(store.has_queued())
            self.assertFalse(store.claim(1))
            for timestamp in (1030, 1150, 1450):
                clock.return_value = timestamp
                runner.process_requests([])
            self.assertEqual(attempted, [1, 2, 1, 1, 1])
            self.assertEqual(store.get(1)["status"], "error")
            self.assertTrue(store.retry(1))
            self.assertEqual(store.queued(1)[0]["id"], 1)


class ProbeCacheTests(WorkspaceTestCase):
    """A3 PERF-03: verified_existing shares report()'s stamp-keyed cache."""

    GOOD = {"duration": 200.0, "bitrate": 320000, "title": "My Song"}

    def test_report_shares_newly_published_file_during_first_report(self):
        records = [dict(self.record(index, "My Song"), isrc="US1234567890") for index in (1, 2)]
        self.write_manifest(records, playlists={"mix": [1, 2]})
        incoming = self.state / "bulk-incoming"
        incoming.mkdir()
        staged = incoming / "1 - My Song.flac"
        staged.write_bytes(b"fixture download")
        with mock.patch.object(runner, "probe", return_value={**self.GOOD, "codec": "flac", "container": "flac"}):
            self.run_report()
        rows = self.coverage_rows()
        self.assertEqual([rows[i]["status"] for i in (1, 2)], ["verified", "verified"])
        self.assertEqual(rows[1]["path"], rows[2]["path"])
        self.assertEqual((self.library / rows[2]["path"]).read_bytes(), b"fixture download")
        self.assertFalse(staged.exists())

    def test_report_refreshes_v5_probe_with_previously_unread_title(self):
        self.write_manifest([self.record(1, "My Song")])
        target = self.library / "1 - My Song.flac"
        target.write_bytes(b"fixture audio")
        stat = target.stat()
        runner.state_store().put_probes({str(target): {
            "stamp": [stat.st_size, stat.st_mtime_ns], "version": 5,
            "media": {**self.GOOD, "title": ""},
        }})
        fresh = {**self.GOOD, "title": "Different Recording"}
        with mock.patch.object(runner, "probe", return_value=fresh) as probe:
            self.run_report()
        probe.assert_called_once_with(target)
        self.assertEqual(self.coverage_rows()[1]["status"], "rejected")
        self.assertEqual(runner.state_store().probes()[str(target)]["media"], fresh)

    def test_verified_existing_reuses_the_probe_cache(self):
        candidate = self.library / "7 - My Song.mp3"
        candidate.write_bytes(b"audio")
        calls = []

        def counting_probe(path):
            calls.append(path)
            return dict(self.GOOD)

        record = {"title": "My Song", "length": 200}
        with mock.patch.object(runner, "probe", counting_probe):
            self.assertEqual(runner.verified_existing(7, record), candidate)
            self.assertEqual(runner.verified_existing(7, record), candidate)
            self.assertEqual(len(calls), 1)
            candidate.write_bytes(b"audio" * 100)
            self.assertEqual(runner.verified_existing(7, record), candidate)
            self.assertEqual(len(calls), 2)

    def test_report_probe_cache_invalidates_when_file_changes(self):
        self.write_manifest([self.record(1, "Song One")], playlists={"mix": [1]})
        self.write_cursor(1)
        target = self.library / "1 - Song One.mp3"
        target.write_bytes(b"audio")
        current = {"media": {"duration": 200.0, "bitrate": 320000, "title": "Song One"}}
        calls = []

        def stub_probe(path):
            calls.append(path)
            return dict(current["media"])

        with mock.patch.object(runner, "probe", stub_probe):
            self.run_report()
            self.assertEqual(self.coverage_rows()[1]["status"], "verified")
            target.write_bytes(b"audio" * 500)
            current["media"] = {"duration": 200.0, "bitrate": 128000, "title": "Song One"}
            self.run_report()
            rows = self.coverage_rows()
            self.assertEqual(rows[1]["status"], "rejected")
            self.assertEqual(rows[1]["reason"], "low bitrate 128000")
        self.assertEqual(len(calls), 2)


class DefensiveStateTests(WorkspaceTestCase):
    """A2 C-02: torn state files fall back to safe defaults, never crash."""

    def test_report_tolerates_a_corrupt_cursor_and_cache(self):
        records = [self.record(1, "Song One")] + \
                  [self.record(i, f"Song {i}") for i in range(2, 6)]
        self.write_manifest(records, playlists={"mix": [1, 2, 3, 4, 5]})
        backup = self.state / "state-backup"
        backup.mkdir(parents=True, exist_ok=True)
        (backup / "cursor.json").write_text("{torn", encoding="utf-8")
        (backup / "probe-cache.json").write_text("not json", encoding="utf-8")
        (self.library / "1 - Song One.mp3").write_bytes(b"audio")
        media = {"duration": 200.0, "bitrate": 320000, "title": "Song One"}
        with mock.patch.object(runner, "probe", lambda path: media):
            summary = self.run_report()
        rows = self.coverage_rows()
        self.assertEqual(rows[1]["status"], "verified")
        # Corrupt cursor falls back to 0 attempted, so track 5 is pending.
        self.assertEqual(rows[5]["status"], "pending")
        self.assertEqual(summary["attempted"], 0)

    def test_prepare_tolerates_a_corrupt_manifest(self):
        # A torn legacy manifest.json imports nothing; prepare builds
        # from the (empty) sources instead of crashing.
        backup = self.state / "state-backup"
        backup.mkdir(parents=True, exist_ok=True)
        (backup / "manifest.json").write_text("{torn", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            runner.prepare()
        payload = runner.catalog_store().read_build()
        self.assertEqual(payload["records"], [])

    def test_cursor_round_trips_through_the_store(self):
        # Plan 036: the cursor lives in the state database now; a
        # completed batch leaves the advanced offset readable back.
        self.write_manifest([self.record(i, f"Song {i}") for i in (1, 2, 3)])
        self.write_cursor(0)
        sockseek = self.root / "sockseek"
        sockseek.write_bytes(b"bin")
        FakeBatchProcess.log_text = "Completed: 3 succeeded, 0 failed\n"
        with mock.patch.object(runner, "SOCKSEEK", sockseek), \
                mock.patch.object(runner, "setup_credentials", lambda: None), \
                mock.patch.object(subprocess, "Popen", FakeBatchProcess):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(runner.run_batch(0, 3))
        self.assertEqual(runner.cursor_offset(), 3)


class HangingProcess:
    """A Sockseek process that never exits on its own."""

    instances = []

    def __init__(self, command, stdout=None, stderr=None):
        self.returncode = None
        self.signals = []
        self.polls = 0
        type(self).instances.append(self)

    def poll(self):
        self.polls += 1
        # Give up after many polls so a missing timeout fails, not hangs.
        return None if self.polls <= 10000 else 0

    def send_signal(self, sig):
        self.signals.append(sig)

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


class SockseekTimeoutTests(WorkspaceTestCase):
    """A2 C-03: a hung request process fails as retryable, queue moves on."""

    def test_a_hung_sockseek_request_fails_as_retryable(self):
        HangingProcess.instances = []
        sockseek = self.root / "sockseek"
        sockseek.write_bytes(b"bin")
        row = {"id": 1, "artist": "Artist", "title": "Title", "album": "Album",
               "length": 200, "search_title": ""}
        with mock.patch.object(runner, "SOCKSEEK", sockseek), \
                mock.patch.object(runner, "setup_credentials", lambda: None), \
                mock.patch.object(runner, "focused_download", lambda *args: False), \
                mock.patch.object(runner, "REQUEST_TIMEOUT", 0, create=True), \
                mock.patch.object(subprocess, "Popen", HangingProcess), \
                mock.patch.object(time, "sleep", lambda seconds: None):
            with self.assertRaisesRegex(runner.EngineTimeout, "timed out"):
                runner.run_request(row, None)
        self.assertTrue(HangingProcess.instances[0].signals)


class PrepareIndexStabilityTests(WorkspaceTestCase):
    """Plan 045: prepare asserts index stability instead of trusting it.

    Coverage, requests, the cursor, and on-disk filenames all key on
    record indexes; a build that would move, duplicate, or compact
    one must abort before any write, leaving the stored build
    authoritative.
    """

    def doctored_record(self, index, stored_index=None):
        record = self.record(index, f"Song {index}")
        record["key"] = record["uri"]
        if stored_index is not None:
            record["index"] = stored_index
        return record

    def write_source(self, name, content):
        path = self.sources / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_prepare_aborts_on_a_gap_in_the_previous_build(self):
        # Two stored records at indexes 1 and 3: the walk reuses
        # record 1, record 3 stays, and the contiguous 1..N invariant
        # the positional reads assume is broken.
        records = [self.doctored_record(1), self.doctored_record(3)]
        self.write_manifest(records, playlists={"mix": [1, 3]})
        self.write_source("mix.csv", "Track URI,Track Name,Artist Name(s)\n"
                          "spotify:track:1,Song 1,Artist 1\n")
        with self.assertRaisesRegex(RuntimeError, "prepare:"):
            with contextlib.redirect_stdout(io.StringIO()):
                runner.prepare()
        payload = runner.catalog_store().read_build()
        self.assertEqual([record["index"] for record in payload["records"]], [1, 3])
        self.assertFalse((self.state / "master.csv").exists())

    def test_prepare_aborts_on_a_duplicated_previous_index(self):
        records = [self.doctored_record(1),
                   self.doctored_record(2, stored_index=1)]
        self.write_manifest(records, playlists={"mix": [1, 2]})
        self.write_source("mix.csv", "Track URI,Track Name,Artist Name(s)\n"
                          "spotify:track:1,Song 1,Artist 1\n")
        with self.assertRaisesRegex(RuntimeError, "prepare:"):
            with contextlib.redirect_stdout(io.StringIO()):
                runner.prepare()
        payload = runner.catalog_store().read_build()
        self.assertEqual(len(payload["records"]), 2)


class ProbeCachePruneTests(WorkspaceTestCase):
    """Plan 045: report() prunes probe rows for files that left."""

    MEDIA = {"1 - Song One.mp3": {"duration": 200.0, "bitrate": 320000,
                                  "title": "Song One"},
             "2 - Song Two.mp3": {"duration": 200.0, "bitrate": 320000,
                                  "title": "Song Two"}}

    def test_report_prunes_the_orphan_and_keeps_present_entries(self):
        self.write_manifest([self.record(1, "Song One"), self.record(2, "Song Two")],
                            playlists={"mix": [1, 2]})
        self.write_cursor(2)
        first = self.library / "1 - Song One.mp3"
        second = self.library / "2 - Song Two.mp3"
        first.write_bytes(b"audio")
        second.write_bytes(b"audio")
        probe = lambda path: self.MEDIA[path.name]
        with mock.patch.object(runner, "probe", probe):
            self.run_report()
        cache = runner.state_store().probes()
        self.assertEqual(set(cache), {str(first), str(second)})
        second.unlink()
        with mock.patch.object(runner, "probe", probe):
            self.run_report()
        cache = runner.state_store().probes()
        self.assertEqual(set(cache), {str(first)})
        self.assertEqual(cache[str(first)]["media"], self.MEDIA["1 - Song One.mp3"])
        # A further report with nothing changed leaves the cache as is.
        with mock.patch.object(runner, "probe", probe):
            self.run_report()
        self.assertEqual(runner.state_store().probes(), cache)


class ProbeEntryTests(unittest.TestCase):
    """Plan 042 stage 6: the state store's single-entry probe accessors.

    The HTTP server reads and writes back one track's probe entry
    through these, beside the runner's whole-cache cycle.
    """

    def test_put_and_read_one_entry(self):
        from downloads.state_store import StateStore
        with tempfile.TemporaryDirectory() as folder:
            store = StateStore(Path(folder) / "requests.sqlite3")
            self.assertIsNone(store.probe_entry("/music/a.flac"))
            entry = {"stamp": [123, 456], "version": 5,
                     "media": {"codec": "flac", "track_gain": -7.89}}
            store.put_probe("/music/a.flac", entry)
            self.assertEqual(store.probe_entry("/music/a.flac"), entry)
            store.put_probe("/music/a.flac", {**entry, "version": 6})
            self.assertEqual(store.probe_entry("/music/a.flac")["version"], 6)
            self.assertEqual(store.probes()["/music/a.flac"]["media"],
                             entry["media"])


class ProbeTimeoutTests(unittest.TestCase):
    """A2 C-04: probe() bounds ffprobe, which runs on untrusted files."""

    def test_probe_times_out_on_a_hung_ffprobe(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bindir = root / "bin"
            bindir.mkdir()
            script = bindir / "ffprobe"
            script.write_text("#!/bin/sh\nsleep 5\n")
            script.chmod(0o755)
            media = root / "song.mp3"
            media.write_bytes(b"\0" * 1000)
            env = {"PATH": str(bindir) + os.pathsep + os.environ["PATH"]}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(verification, "PROBE_TIMEOUT", 1, create=True):
                started = time.monotonic()
                result = verification.probe(media)
                elapsed = time.monotonic() - started
        self.assertIsNone(result)
        self.assertLess(elapsed, 4)


if __name__ == "__main__":
    unittest.main()
