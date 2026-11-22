import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import os
import subprocess
import tempfile
import time
import unittest

from system.supervisor import Supervisor, _pid_alive as supervisor_pid_alive

SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]
# The SIGINT-proof worker announces readiness through a file named
# in its last argument: without that wait, a stop issued during the
# interpreter's startup measures the default disposition's death,
# not the escalation.
SIGINT_PROOF = [sys.executable, "-c",
                "import pathlib, signal, sys, time; "
                "signal.signal(signal.SIGINT, signal.SIG_IGN); "
                "pathlib.Path(sys.argv[1]).write_text('ready'); "
                "time.sleep(120)"]


class SupervisorTests(unittest.TestCase):
    """Plan 050 Step 2 (ADR 0001): the supervisor starts workers as
    detached processes in their own process groups, reports them
    from pidfiles, re-adopts them across supervisor instances (the
    server-restart shape), and stops them SIGINT first, SIGKILL
    after the timeout. Stand-in sleeper processes play the workers.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.supervisor = Supervisor(self.directory)
        self.addCleanup(self._stop_everything)

    def _stop_everything(self):
        for kind in ("downloads", "requests", "genres", "share"):
            self.supervisor.stop(kind)

    def test_start_reports_and_writes_a_pidfile(self):
        self.assertTrue(self.supervisor.start("downloads", SLEEPER))
        self.assertTrue(self.supervisor.status("downloads"))
        pidfile = self.directory / "worker-downloads.pid"
        self.assertTrue(pidfile.is_file())
        # A second start of a running worker is refused.
        self.assertFalse(self.supervisor.start("downloads", SLEEPER))

    def test_output_appends_to_the_workers_log(self):
        log = self.directory / "runner.log"
        argv = [sys.executable, "-c", "print('worker hello')"]
        self.assertTrue(self.supervisor.start("genres", argv, log=log))
        deadline = time.monotonic() + 10
        while self.supervisor.status("genres") and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.supervisor.status("genres"))
        self.assertIn("worker hello", log.read_text(encoding="utf-8"))
        # The pidfile of a finished worker is cleaned up.
        self.assertFalse((self.directory / "worker-genres.pid").exists())

    def test_a_new_supervisor_readopts_a_running_worker(self):
        self.assertTrue(self.supervisor.start("requests", SLEEPER))
        restarted = Supervisor(self.directory)
        self.assertTrue(restarted.status("requests"))
        # And the re-adopted worker can be stopped by its new owner.
        self.assertTrue(restarted.stop("requests"))
        self.assertFalse(restarted.status("requests"))
        self.assertFalse(self.supervisor.status("requests"))

    def test_stop_uses_sigint_before_sigkill(self):
        self.assertTrue(self.supervisor.start("downloads", SLEEPER))
        started = time.monotonic()
        self.assertTrue(self.supervisor.stop("downloads"))
        # A cooperative worker dies on SIGINT, long before the
        # 10-second escalation could fire.
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(self.supervisor.status("downloads"))

    def test_stop_escalates_to_sigkill_after_the_timeout(self):
        self.supervisor.stop_timeout = 0.5
        ready = self.directory / "ready"
        self.assertTrue(self.supervisor.start(
            "downloads", [*SIGINT_PROOF, str(ready)]))
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready.exists())
        started = time.monotonic()
        self.assertTrue(self.supervisor.stop("downloads"))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertFalse(self.supervisor.status("downloads"))

    def test_stop_of_a_worker_that_never_ran_is_a_quiet_true(self):
        self.assertTrue(self.supervisor.stop("share"))
        self.assertFalse(self.supervisor.status("share"))

    def test_an_unreaped_dead_worker_does_not_read_as_alive(self):
        # A worker that exited but has not been reaped is a zombie:
        # kill(pid, 0) succeeds for it, yet it is dead. The pid
        # liveness check must say so, or an in-process re-adoption
        # waits out the whole stop timeout on a corpse (the failure
        # the re-adoption test above first surfaced).
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            deadline = time.monotonic() + 10
            while supervisor_pid_alive(proc.pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(supervisor_pid_alive(proc.pid))
        finally:
            proc.wait()

    def test_a_stale_pidfile_is_forgotten(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        dead_pid = proc.pid
        self.assertFalse(_pid_alive(dead_pid))
        (self.directory / "worker-share.pid").write_text(
            '{"pid": %d, "kind": "share", "argv": ["gone"]}' % dead_pid,
            encoding="utf-8")
        restarted = Supervisor(self.directory)
        self.assertFalse(restarted.status("share"))
        self.assertFalse((self.directory / "worker-share.pid").exists())


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
