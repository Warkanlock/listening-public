import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import tempfile
import unittest
from types import SimpleNamespace

from system.job_queue import JobQueue
from system.jobs import Jobs


class _Rig:
    """A Jobs layer over a real queue and fake worker machinery."""

    def __init__(self, database):
        self.running = {"downloads": False, "requests": False,
                        "genres": False, "reclaim": False,
                        "migrate": False}
        self.starts = []
        self.stops = []
        self.refreshes = 0
        self.saves = 0
        self.settings = {}
        self.flags = {"genre_tools": True, "download_ready": True,
                      "lists_ready": True, "acquisition_ready": True}
        self.playlists = [{"id": "evening", "name": "Evening"}]
        self.queue = JobQueue(database)
        self.jobs = Jobs(
            queue=self.queue,
            worker_running=lambda kind: self.running[kind],
            start_worker=self._start,
            stop_worker=self._stop,
            genre_tools_ready=lambda: self.flags["genre_tools"],
            download_ready=lambda: self.flags["download_ready"],
            acquisition_ready=lambda: self.flags["acquisition_ready"],
            lists_ready=lambda: self.flags["lists_ready"],
            refresh_library=self._refresh,
            save_settings=self._save,
            settings=self.settings,
            catalog=lambda: SimpleNamespace(records=[], summary={}),
            pending_requests=lambda: 0,
            playlist_options=lambda: list(self.playlists))

    def _start(self, kind, scope=None):
        self.starts.append((kind, scope))
        self.running[kind] = True
        return True

    def _stop(self, kind):
        self.stops.append(kind)
        self.running[kind] = False

    def _refresh(self, force=False):
        self.refreshes += 1

    def _save(self):
        self.saves += 1

    def card(self, kind):
        return {card["kind"]: card
                for card in self.jobs.snapshot()["jobs"]}[kind]


class JobsQueueTests(unittest.TestCase):
    """Plan 050 Step 2 (ADR 0002): Start enqueues, the dispatcher
    starts the oldest waiting job when the library frees up, Stop
    on a waiting job cancels it, and a job whose blocker appears by
    dispatch time fails visibly on its card."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / "requests.sqlite3"
        self.rig = _Rig(self.database)

    def test_start_with_a_free_library_starts_in_the_same_request(self):
        self.rig.jobs.act("genres", "start")
        self.assertEqual(self.rig.starts, [("genres", {"type": "library", "job_id": self.rig.queue.latest_for("genres")["id"]})])
        card = self.rig.card("genres")
        self.assertEqual(card["state"], "running")
        self.assertTrue(card["running"])

    def test_start_while_busy_takes_a_place_in_line(self):
        self.rig.running["genres"] = True  # a watchdog-started run
        self.rig.jobs.act("downloads", "start", {"type": "all"})
        self.assertEqual(self.rig.starts, [])
        card = self.rig.card("downloads")
        self.assertEqual(card["state"], "queued")
        self.assertEqual(card["queue_position"], 1)
        self.assertEqual(card["blocker"], "")
        # Background downloading was armed at enqueue, as the old
        # immediate start armed it.
        self.assertTrue(self.rig.settings["auto_download"])

    def test_source_pause_leaves_downloads_queued_while_genres_can_run(self):
        self.rig.settings["download_engines"] = {"search": "fixture", "fetch": "fixture"}
        self.rig.flags["acquisition_ready"] = False
        self.rig.jobs.act("downloads", "start", {"type": "all"})
        self.assertEqual(self.rig.card("downloads")["state"], "queued")
        self.assertEqual(self.rig.refreshes, 0)
        self.rig.jobs.act("genres", "start")
        self.assertEqual(self.rig.card("genres")["state"], "running")
        self.rig.running["genres"] = False
        self.rig.flags["acquisition_ready"] = True
        self.assertTrue(self.rig.jobs.dispatch())
        self.assertEqual(self.rig.card("downloads")["state"], "running")

    def test_queued_format_override_survives_room_default_changes(self):
        self.rig.running["genres"] = True
        self.rig.jobs.act("downloads", "start", {"type": "list", "id": "evening", "format": "mp3_only"})
        self.rig.settings["bulk_download_format"] = "lossless_only"
        card = self.rig.card("downloads")
        self.assertEqual(card["default_format"], "lossless_only")
        self.assertEqual(card["scope"]["format"], "mp3_only")
        self.rig.running["genres"] = False
        self.assertTrue(self.rig.jobs.dispatch())
        self.assertEqual(self.rig.starts, [
            ("downloads", {"type": "list", "id": "evening", "name": "Evening", "format": "mp3_only"})
        ])
        self.assertNotIn("auto_download", self.rig.settings)

    def test_invalid_download_format_does_not_enqueue_or_arm_a_worker(self):
        with self.assertRaisesRegex(ValueError, "Choose MP3 first"):
            self.rig.jobs.act("downloads", "start", {"type": "all", "format": ["mp3"]})
        self.assertEqual(self.rig.starts, [])
        self.assertNotIn("auto_download", self.rig.settings)

    def test_a_queued_job_starts_on_its_own_when_the_library_frees_up(self):
        self.rig.running["genres"] = True
        self.rig.jobs.act("downloads", "start", {"type": "all"})
        self.rig.jobs.act("requests", "start")
        self.assertEqual(self.rig.card("requests")["queue_position"], 2)
        self.rig.running["genres"] = False
        self.assertTrue(self.rig.jobs.dispatch())
        # The oldest waiting job goes first.
        self.assertEqual(self.rig.starts, [("downloads", {"type": "all"})])
        self.assertEqual(self.rig.card("downloads")["state"], "running")
        self.assertEqual(self.rig.card("requests")["queue_position"], 1)

    def test_the_queue_survives_a_restart_with_its_order_intact(self):
        self.rig.running["genres"] = True
        self.rig.jobs.act("downloads", "start", {"type": "all"})
        self.rig.jobs.act("requests", "start")
        restarted = _Rig(self.database)
        self.assertEqual(restarted.card("downloads")["state"], "queued")
        self.assertEqual(restarted.card("downloads")["queue_position"], 1)
        self.assertEqual(restarted.card("requests")["queue_position"], 2)
        self.assertTrue(restarted.jobs.dispatch())
        self.assertEqual(restarted.starts,
                         [("downloads", {"type": "all"})])

    def test_stop_on_a_queued_job_cancels_it_and_it_never_runs(self):
        self.rig.running["genres"] = True
        self.rig.jobs.act("requests", "start")
        self.rig.jobs.act("requests", "stop")
        self.assertEqual(self.rig.queue.latest_for("requests")["state"],
                         "cancelled")
        self.rig.running["genres"] = False
        self.assertFalse(self.rig.jobs.dispatch())
        self.assertEqual(self.rig.starts, [])
        self.assertEqual(self.rig.card("requests")["state"], "idle")

    def test_stop_on_the_running_downloads_job_disarms_auto_download(self):
        self.rig.jobs.act("downloads", "start", {"type": "all"})
        self.assertTrue(self.rig.settings["auto_download"])
        self.rig.jobs.act("downloads", "stop")
        self.assertEqual(self.rig.stops, ["downloads"])
        self.assertFalse(self.rig.settings["auto_download"])
        self.assertEqual(self.rig.queue.latest_for("downloads")["state"],
                         "cancelled")

    def test_a_blocker_appearing_by_dispatch_time_fails_visibly(self):
        self.rig.running["downloads"] = True
        self.rig.jobs.act("genres", "start")
        self.rig.flags["genre_tools"] = False
        self.rig.running["downloads"] = False
        self.assertTrue(self.rig.jobs.dispatch())
        self.assertEqual(self.rig.starts, [])
        card = self.rig.card("genres")
        self.assertEqual(card["state"], "failed")
        self.assertIn("ffprobe", card["error"])

    def test_unavailable_engine_preserves_job_and_dispatches_independent_work(self):
        self.rig.settings["download_engines"] = {"search": "catalog", "fetch": "catalog"}
        self.rig.running["requests"] = True
        self.rig.jobs.act("downloads", "start")
        self.rig.jobs.act("genres", "start")
        waiting = self.rig.queue.queued_for("downloads")["id"]
        self.rig.running["requests"] = False
        self.rig.flags["download_ready"] = False
        self.assertTrue(self.rig.jobs.dispatch())
        self.assertTrue(self.rig.running["genres"])
        self.assertEqual(self.rig.queue.queued_for("downloads")["id"], waiting)
        self.assertIn("Extensions", self.rig.card("downloads")["blocker"])

    def test_managed_job_retries_with_original_scope_and_checkpoint(self):
        self.rig.settings["download_engines"] = {"search": "catalog", "fetch": "catalog"}
        scope = {"type": "list", "id": "evening", "format": "mp3_only"}
        self.rig.jobs.act("downloads", "start", scope)
        job = self.rig.queue.running_for("downloads")
        identity = job["id"]
        self.rig.queue.record_progress(identity, {"engine_retry": "Engine unavailable", "scoped_attempted": ["first"]})
        self.rig.running["downloads"] = False
        self.assertFalse(self.rig.jobs.dispatch())
        retry = self.rig.queue.queued_for("downloads")
        self.assertEqual(retry["id"], identity)
        self.assertEqual(retry["scope"], job["scope"])
        self.assertEqual(self.rig.card("downloads")["error"], "Engine unavailable")
        self.rig.queue.record_progress(identity, {**retry["progress"], "retry_after": 0})
        self.assertTrue(self.rig.jobs.dispatch())
        self.assertEqual(self.rig.queue.running_for("downloads")["id"], identity)
        self.assertEqual(self.rig.queue.get(identity)["progress"], {"scoped_attempted": ["first"]})

    def test_a_list_deleted_while_waiting_fails_the_job(self):
        self.rig.running["genres"] = True
        self.rig.jobs.act("downloads", "start",
                          {"type": "list", "id": "evening"})
        self.rig.playlists = []
        self.rig.running["genres"] = False
        self.assertTrue(self.rig.jobs.dispatch())
        card = self.rig.card("downloads")
        self.assertEqual(card["state"], "failed")
        self.assertIn("no longer there", card["error"])

    def test_setup_blockers_still_refuse_at_enqueue_time(self):
        self.rig.flags["lists_ready"] = False
        with self.assertRaises(ValueError) as caught:
            self.rig.jobs.act("downloads", "start", {"type": "all"})
        self.assertIn("Import or link a music list first",
                      str(caught.exception))
        self.assertEqual(self.rig.queue.queued(), [])

    def test_the_migrate_job_starts_with_a_library_scope(self):
        # Plan 062: the one-time migration is a queued job like
        # reclaim; its card shows while the completion marker is
        # absent from settings and retires once it lands.
        self.rig.jobs.act("migrate", "start")
        self.assertEqual(self.rig.starts,
                         [("migrate", {"type": "library"})])
        card = self.rig.card("migrate")
        self.assertEqual(card["state"], "running")
        self.assertEqual(card["progress"],
                         {"done": 0, "total": 0, "migrated": 0,
                          "skipped": 0, "already": 0})
        self.rig.running["migrate"] = False
        self.rig.settings["library_migrated"] = 1700000000.0
        kinds = [entry["kind"]
                 for entry in self.rig.jobs.snapshot()["jobs"]]
        self.assertNotIn("migrate", kinds)

    def test_a_genre_scan_can_finish_without_adding_any_genres(self):
        self.rig.jobs.act("genres", "start")
        job_id = self.rig.queue.running_for("genres")["id"]
        counts = {"checked": 3, "scan_total": 3, "found": 0, "unavailable": 1, "complete": 1}
        self.rig.queue.record_progress(job_id, counts)
        self.rig.running["genres"] = False
        card = self.rig.card("genres")
        self.assertEqual(card["state"], "done")
        self.assertEqual(card["progress"]["checked"], 3)

    def test_legacy_finished_genre_run_has_no_confirmation_or_counts(self):
        old = self.rig.queue.enqueue("genres", {"type": "library"})
        self.rig.queue.mark_running(old["id"])
        card = self.rig.card("genres")
        self.assertEqual(card["state"], "done")
        self.assertNotIn("complete", card["progress"])

    def test_an_interrupted_scan_keeps_its_counts_and_a_new_run_starts_fresh(self):
        self.rig.jobs.act("genres", "start")
        job_id = self.rig.queue.running_for("genres")["id"]
        self.rig.queue.record_progress(job_id, {"checked": 7, "scan_total": 10})
        self.rig.running["genres"] = False
        self.assertEqual(self.rig.card("genres")["state"], "failed")
        self.rig.jobs.act("genres", "start")
        self.rig.queue.record_progress(job_id, {"checked": 10, "complete": 1})
        self.assertEqual(self.rig.card("genres")["progress"]["checked"], 0)
        self.rig.jobs.act("genres", "stop")
        self.assertEqual(self.rig.card("genres")["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
