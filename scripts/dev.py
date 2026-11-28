#!/usr/bin/env python3
"""Development runner for Listening.

Runs `python3 backend/server.py` in the foreground, passing any
arguments through (`make dev ARGS="--port 8899"`), and restarts the server
when a backend Python file changes. Polls the backend tree's .py
mtimes once a second, with a one-second settle before restarting; no
file-watching library. Ctrl-C stops the watcher and the server. If the
server exits on its own, the watcher exits with the same code.
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
POLL_SECONDS = 1.0
SETTLE_SECONDS = 1.0


def tree_signature(root):
    """Map every .py file under root to its mtime in nanoseconds."""
    signature = {}
    for path in sorted(root.rglob("*.py")):
        try:
            signature[path] = path.stat().st_mtime_ns
        except OSError:
            continue
    return signature


def changed_paths(before, after):
    """Paths added, removed, or mtime-bumped between two signatures."""
    return sorted(path for path in before.keys() | after.keys()
                  if before.get(path) != after.get(path))


def _stop(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    command = [sys.executable, str(BACKEND / "server.py"), *args]
    signature = tree_signature(BACKEND)
    child = subprocess.Popen(command, cwd=ROOT)
    try:
        while True:
            time.sleep(POLL_SECONDS)
            if child.poll() is not None:
                return child.returncode
            current = tree_signature(BACKEND)
            if changed_paths(signature, current):
                time.sleep(SETTLE_SECONDS)
                signature = tree_signature(BACKEND)
                print("dev: a backend file changed, restarting the server",
                      flush=True)
                _stop(child)
                child = subprocess.Popen(command, cwd=ROOT)
    except KeyboardInterrupt:
        _stop(child)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
