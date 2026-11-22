"""The supervisor: the server owns its workers' processes (ADR 0001).

Until plan 050 Step 2 the server started, stopped, and checked its
four workers (bulk downloads, song requests, genre categorization,
sharing) by shelling out to tmux. The supervisor replaces that: each
worker is a detached subprocess in its own process group, spawned by
the server, with a pidfile in the application folder. Workers survive
a server restart because a new supervisor re-adopts them from their
pidfiles; stop is SIGINT to the process group, escalating to SIGKILL
after 10 seconds, the graceful-then-final shape the runner's own
child handling already uses.

Worker identity is the kind ("downloads", "requests", "genres",
"share"); there are no session names and no per-config suffixes.
The supervisor owns processes only: what a worker does, when one
may start, and what its output means all live with its callers.

One accepted caveat: a pidfile names a pid, and pids are reused.
Where /proc is readable (Linux), an adopted pid is only trusted
when its command line still matches the argv recorded at spawn;
elsewhere the pidfile is trusted as written, the same bargain every
pidfile system makes.
"""

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

STOP_TIMEOUT = 10.0
KILL_TIMEOUT = 5.0
POLL_INTERVAL = 0.1


def _pid_alive(pid):
    # A dead worker whose parent has not reaped it lingers as a
    # zombie, and kill(pid, 0) still succeeds for one: it runs
    # nothing and holds nothing, so it must not read as alive.
    # Where the pid is this process's own child (the in-process
    # re-adoption shape), waitpid answers directly and reaps it;
    # a sibling Popen handle for the same child reads the death
    # through its own poll, so the reap costs it nothing.
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Belt and braces on Linux for a zombie parented elsewhere (not
    # ours to reap): /proc names its state. The state is the first
    # field after the parenthesized comm, which may itself contain
    # spaces or parens; split from the LAST closing paren. Where
    # /proc does not exist, the kill result stands, as before.
    try:
        stat = Path(f"/proc/{pid}/stat").read_bytes()
        state = stat.rpartition(b")")[2].split()[0]
    except (OSError, IndexError):
        return True
    return state != b"Z"


def _cmdline_matches(pid, argv):
    """Whether /proc still shows this pid running the recorded argv.

    Only checked where /proc exists; a mismatch means the pid was
    reused by an unrelated process after the worker died.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True
    parts = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    if not parts or not argv:
        return True
    return parts[:len(argv)] == [str(arg) for arg in argv]


class Supervisor:
    """Starts, stops, and re-adopts the room's worker processes."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self._procs = {}
        self._lock = threading.Lock()
        self.stop_timeout = STOP_TIMEOUT
        self.adopt()

    # -- pidfiles ----------------------------------------------------------

    def _pidfile(self, kind):
        return self.directory / f"worker-{kind}.pid"

    def _read_pidfile(self, kind):
        try:
            record = json.loads(self._pidfile(kind).read_text(encoding="utf-8"))
            return {"pid": int(record["pid"]),
                    "argv": [str(arg) for arg in record.get("argv", [])]}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_pidfile(self, kind, pid, argv):
        self.directory.mkdir(parents=True, exist_ok=True)
        record = {"pid": pid, "kind": kind,
                  "argv": [str(arg) for arg in argv],
                  "started_at": time.time()}
        self._pidfile(kind).write_text(json.dumps(record), encoding="utf-8")

    def _drop_pidfile(self, kind):
        try:
            self._pidfile(kind).unlink()
        except OSError:
            pass

    # -- lifecycle -----------------------------------------------------------

    def adopt(self):
        """The boot-time pass: forget pidfiles whose worker is gone.

        A pidfile whose pid is still alive (and still the recorded
        command, where that can be checked) is left in place; status
        and stop find the worker through it, which is what re-adopting
        means here. Stale files are removed so a later start is not
        blocked by a ghost.
        """
        if not self.directory.is_dir():
            return
        for path in self.directory.glob("worker-*.pid"):
            kind = path.name[len("worker-"):-len(".pid")]
            record = self._read_pidfile(kind)
            if (record is None or not _pid_alive(record["pid"])
                    or not _cmdline_matches(record["pid"], record["argv"])):
                self._drop_pidfile(kind)

    def start(self, kind, argv, env=None, log=None):
        """Spawn the worker detached in its own process group.

        Output appends to the worker's existing log file. Returns
        False when the worker is already running or the spawn fails.
        """
        argv = [str(arg) for arg in argv]
        with self._lock:
            if self._status_locked(kind):
                return False
            stream = None
            try:
                if log is not None:
                    log = Path(log)
                    log.parent.mkdir(parents=True, exist_ok=True)
                    stream = open(log, "ab")
                proc = subprocess.Popen(
                    argv, env=env, stdin=subprocess.DEVNULL,
                    stdout=stream if stream is not None else subprocess.DEVNULL,
                    stderr=subprocess.STDOUT if stream is not None else subprocess.DEVNULL,
                    start_new_session=True)
            except OSError:
                return False
            finally:
                if stream is not None:
                    stream.close()
            self._procs[kind] = proc
            self._write_pidfile(kind, proc.pid, argv)
            return True

    def status(self, kind):
        """Whether the worker is alive, ours or re-adopted."""
        with self._lock:
            return self._status_locked(kind)

    def _status_locked(self, kind):
        proc = self._procs.get(kind)
        if proc is not None:
            if proc.poll() is None:
                return True
            del self._procs[kind]
            self._drop_pidfile(kind)
            return False
        record = self._read_pidfile(kind)
        if record is None:
            return False
        if (not _pid_alive(record["pid"])
                or not _cmdline_matches(record["pid"], record["argv"])):
            self._drop_pidfile(kind)
            return False
        return True

    def _pid(self, kind):
        proc = self._procs.get(kind)
        if proc is not None:
            return proc.pid
        record = self._read_pidfile(kind)
        return record["pid"] if record else None

    def stop(self, kind):
        """SIGINT the worker's process group, SIGKILL after the timeout.

        Returns True when the worker is not running at the end,
        whether it stopped, had already exited, or never ran; False
        only when the process survives even SIGKILL (an unkillable
        kernel state, reported by the caller).
        """
        with self._lock:
            if not self._status_locked(kind):
                return True
            pid = self._pid(kind)
        if pid is None:
            return True
        self._signal_group(pid, signal.SIGINT)
        if self._wait_dead(kind, self.stop_timeout):
            return True
        self._signal_group(pid, signal.SIGKILL)
        return self._wait_dead(kind, KILL_TIMEOUT)

    @staticmethod
    def _signal_group(pid, sig):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _wait_dead(self, kind, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.status(kind):
                return True
            time.sleep(POLL_INTERVAL)
        return not self.status(kind)
