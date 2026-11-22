import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import csv
import json
import logging
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from fixture_environment import isolate_room_environment as setUpModule
import server


class _NoQueuedRequests:
    def has_queued(self):
        return False


class _QueuedRequests:
    def has_queued(self):
        return True


class _SpotifyOffline:
    def status(self):
        return {"connected": False}

    def start(self):
        raise AssertionError("the watchdog must not start an offline adapter")


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _reset_watchdog_state():
    server.WATCHDOG_HEALTH.update(
        {"last_error": "", "last_error_at": 0.0, "failures": 0})
    server._WATCHDOG_LOG_STATE.update({"last_at": 0.0, "suppressed": 0})


class WatchdogRunOnceTests(unittest.TestCase):
    """Plan 022: the loop's single-pass driver records failures
    instead of letting them kill the thread, and a healthy pass
    clears the record."""

    def setUp(self):
        _reset_watchdog_state()
        self.addCleanup(_reset_watchdog_state)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        media = mock.patch.object(server, "MEDIA", SimpleNamespace(release_playback_buffer=lambda **_: None, store=lambda: None))
        media.start()
        self.addCleanup(media.stop)

    def _drive(self, refresh_library):
        """Run one pass through the driver with the real pass body,
        its collaborators stubbed so refresh_library is reached."""
        catalog = SimpleNamespace(refresh=lambda: None, records=[], bulk_resume_free=0)
        with mock.patch.object(server, "catalog", catalog), \
                mock.patch.object(server, "REFRESH", SimpleNamespace(synced=lambda: True)), \
                mock.patch.object(server, "ROOT", self.root), \
                mock.patch.object(server, "DB",
                                  self.root / "state" / "requests.sqlite3"), \
                mock.patch.object(server, "worker_running", lambda name: False), \
                mock.patch.object(server, "refresh_library", refresh_library), \
                mock.patch.object(server, "requests_store",
                                  lambda: _NoQueuedRequests()), \
                mock.patch.object(server, "SETTINGS",
                                  {"auto_download": False,
                                   # Plan 062: this room's one-time
                                   # library migration is settled.
                                   "library_migrated": 1700000000.0}), \
                mock.patch.object(server, "spotify", _SpotifyOffline()):
            server._watchdog_run_once()

    def test_watchdog_pass_survives_a_csv_error(self):
        def broken(force=False):
            raise csv.Error("field larger than field limit (131072)")

        self._drive(broken)
        self._drive(broken)
        self.assertEqual(server.WATCHDOG_HEALTH["failures"], 2)
        self.assertEqual(server.WATCHDOG_HEALTH["last_error"],
                         "Error: field larger than field limit (131072)")
        self.assertGreater(server.WATCHDOG_HEALTH["last_error_at"], 0)
        # A following healthy pass clears the record.
        self._drive(lambda force=False: None)
        self.assertEqual(server.WATCHDOG_HEALTH,
                         {"last_error": "", "last_error_at": 0.0, "failures": 0})


class WatchdogLoggingTests(unittest.TestCase):
    """Plan 022: failure logging emits at most one line per minute;
    suppressed failures are counted into the next emitted line, and
    recovery logs exactly once."""

    def setUp(self):
        _reset_watchdog_state()
        self.addCleanup(_reset_watchdog_state)
        self.handler = _ListHandler()
        self.logger = server.WATCHDOG_LOGGER
        self._old_level = self.logger.level
        self._old_propagate = self.logger.propagate
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.addCleanup(self._restore_logger)

    def _restore_logger(self):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self._old_level)
        self.logger.propagate = self._old_propagate

    def _run(self, error=None):
        with mock.patch.object(server, "_watchdog_pass", side_effect=error):
            server._watchdog_run_once()

    def test_watchdog_failure_logging_is_rate_limited(self):
        for _ in range(3):
            self._run(RuntimeError("boom"))
        self.assertEqual(len(self.handler.records), 1)
        first = self.handler.records[0].getMessage()
        self.assertIn("RuntimeError: boom", first)
        self.assertIn("suppressed since last log line: 0", first)
        # A minute later the next failure emits again and reports the
        # two failures the rate limit suppressed.
        server._WATCHDOG_LOG_STATE["last_at"] -= 61
        self._run(RuntimeError("boom"))
        self.assertEqual(len(self.handler.records), 2)
        self.assertIn("suppressed since last log line: 2",
                      self.handler.records[1].getMessage())
        # Recovery logs once, with the total failure count.
        self._run()
        self.assertEqual(len(self.handler.records), 3)
        self.assertIn("watchdog recovered after 4 failures",
                      self.handler.records[2].getMessage())
        self.assertEqual(server.WATCHDOG_HEALTH,
                         {"last_error": "", "last_error_at": 0.0, "failures": 0})
        # The happy path logs nothing further.
        self._run()
        self.assertEqual(len(self.handler.records), 3)


class WatchdogDecisionTests(unittest.TestCase):
    """A6 proposal 49, against the extracted pass: queued requests
    win over bulk, and bulk starts only when every gate is open."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        media = mock.patch.object(server, "MEDIA", SimpleNamespace(release_playback_buffer=lambda **_: None, store=lambda: None))
        media.start()
        self.addCleanup(media.stop)

    def _run_pass(self, *, queued=False, auto_download=True, ready=True,
                  records=3, cursor_offset=None, free_gib=40):
        if cursor_offset is not None:
            backup = self.root / "state" / "state-backup"
            backup.mkdir(parents=True, exist_ok=True)
            (backup / "cursor.json").write_text(
                json.dumps({"next_offset": cursor_offset}), encoding="utf-8")
        starts = []
        catalog = SimpleNamespace(refresh=lambda: None,
                                  records=[{}] * records, bulk_resume_free=0)
        store = _QueuedRequests() if queued else _NoQueuedRequests()
        statvfs = SimpleNamespace(f_bavail=int(free_gib * 1024**3) // 4096,
                                  f_frsize=4096)
        with mock.patch.object(server, "catalog", catalog), \
                mock.patch.object(server, "REFRESH", SimpleNamespace(synced=lambda: True)), \
                mock.patch.object(server, "ROOT", self.root), \
                mock.patch.object(server, "DB",
                                  self.root / "state" / "requests.sqlite3"), \
                mock.patch.object(server, "worker_running", lambda name: False), \
                mock.patch.object(server, "refresh_library",
                                  lambda force=False: None), \
                mock.patch.object(server, "requests_store", lambda: store), \
                mock.patch.object(server, "start_worker",
                                  lambda action, root=None, force=False:
                                  starts.append(action) or True), \
                mock.patch.object(server, "SETTINGS",
                                  {"auto_download": auto_download,
                                   # Plan 062: this room's one-time
                                   # library migration is settled.
                                   "library_migrated": 1700000000.0}), \
                mock.patch.object(server, "download_ready", lambda: ready), \
                mock.patch.object(server, "spotify", _SpotifyOffline()), \
                mock.patch.object(server.os, "statvfs", lambda path: statvfs):
            server._watchdog_pass()
        return starts

    def test_startup_requeues_a_request_interrupted_before_publication(self):
        from downloads.requests_store import RequestsStore

        store = RequestsStore(self.root / "requests.sqlite3")
        request = store.enqueue(None, "Artist", "Title", "", 120, "")
        self.assertTrue(store.claim(request))
        catalog = SimpleNamespace(refresh=lambda: None, records=[], bulk_resume_free=0)
        with mock.patch.object(server, "catalog", catalog), \
                mock.patch.object(server, "REFRESH", SimpleNamespace(synced=lambda: False)), \
                mock.patch.object(server, "ROOT", self.root), \
                mock.patch.object(server, "STATE", self.root), \
                mock.patch.object(server, "requests_store", return_value=store), \
                mock.patch.object(server, "SETTINGS", {"library_migrated": 1700000000.0}), \
                mock.patch.object(server, "worker_running", return_value=False), \
                mock.patch.object(server, "jobs", None), \
                mock.patch.object(server, "refresh_library"), \
                mock.patch.object(server, "spotify", _SpotifyOffline()), \
                mock.patch.object(server, "start_worker") as start:
            server._watchdog_pass()
        self.assertEqual(store.get(request)["status"], "queued")
        start.assert_called_once_with("requests", self.root)

    def test_idle_lease_blocks_source_refresh(self):
        with mock.patch.object(server, "update_draining", return_value=True), \
                mock.patch.object(server, "publish_phone_imports") as imports, \
                mock.patch.object(server, "refresh_library") as refresh:
            server._watchdog_pass()
        imports.assert_not_called()
        refresh.assert_not_called()

    def test_watchdog_prefers_requests_and_respects_space_and_cursor(self):
        # Queued requests start the requests worker, never bulk.
        self.assertEqual(self._run_pass(queued=True), ["requests"])
        # Every gate open and the cursor unfinished: bulk starts.
        self.assertEqual(self._run_pass(), ["run"])
        # Each gate on its own suppresses the bulk start.
        self.assertEqual(self._run_pass(auto_download=False), [])
        self.assertEqual(self._run_pass(ready=False), [])
        self.assertEqual(self._run_pass(cursor_offset=3), [])
        self.assertEqual(self._run_pass(free_gib=10), [])


if __name__ == "__main__":
    unittest.main()
