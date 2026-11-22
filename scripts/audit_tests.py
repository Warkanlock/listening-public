#!/usr/bin/env python3
"""Run Listening's fixture checks and audit protected filesystem access on macOS."""

import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time

from test_audit.audit_paths import comparison_path

ROOT = Path(__file__).resolve().parents[1]
AUDITED = "LISTENING_TEST_AUDIT_ACTIVE"
DATABASES = ("collection.sqlite3", "media.sqlite3", "requests.sqlite3", "host.sqlite3", "owner-auth.sqlite3")
DENIAL = re.compile(r"^(?:\d+ duplicate reports? for )?Sandbox: (.+?) deny\(\d+\) (file-\S+) (.+)$")
LOG_FILTER = 'senderImagePath ENDSWITH "/Sandbox" AND eventMessage CONTAINS "Sandbox:"'


def installed_paths():
    """Protect default installations and databases in any configured room."""
    home = Path.home()
    support = home / "Library/Application Support"
    directories = {support / name for name in ("Listening", "ListeningMusic", "OfflineMusic")}
    directories.update((home / "Music/ListeningRoom", home / ".config/listening-room"))
    configs = {support / "Listening/host/listening-room.json",
               support / "ListeningMusic/listening-room.json",
               support / "OfflineMusic/listening-room.json",
               home / ".config/listening-room/config.json"}
    configured = os.environ.get("LISTENING_ROOM_CONFIG")
    if configured:
        configs.add(Path(configured).expanduser().absolute())
    for base in tuple(configs):
        configs.update((base.parent / "rooms").glob("*/config.json"))
        configs.update((base.resolve().parent / "rooms").glob("*/config.json"))
        selector = base.with_suffix(base.suffix + ".room")
        if selector.is_file():
            configs.add(Path(selector.read_text(encoding="utf-8").strip()).expanduser().resolve())

    database_dirs = {directory for config in configs
                     for directory in (config.parent.resolve(), config.resolve().parent)}
    rooms = set()
    for config in configs:
        try:
            settings = json.loads(config.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        if isinstance(settings, dict) and settings.get("folder"):
            rooms.add(Path(settings["folder"]).expanduser().resolve())
    if root := os.environ.get("OFFLINE_MUSIC_ROOT"):
        rooms.add(Path(root).expanduser().resolve())
    if state := os.environ.get("OFFLINE_MUSIC_STATE"):
        database_dirs.add(Path(state).expanduser().resolve())
    database_dirs.update(rooms)
    database_dirs.update(room / "state" for room in rooms)
    databases = {folder.resolve() / name for folder in database_dirs for name in DATABASES}
    # SQLite may create sidecars beside the resolved database under another basename.
    database_names = databases | {p.resolve() for p in databases}
    files = {p.with_name(p.name + suffix) for p in database_names
             for suffix in ("", "-wal", "-shm", "-journal")}
    # Protect the names of symlinks from removal as well as their targets from I/O.
    return directories | {p.resolve() for p in directories}, files | {p.resolve() for p in files}


def write_policy(path, directories, files):
    rules = [f"(subpath {json.dumps(str(p), ensure_ascii=False)})" for p in sorted(directories)]
    rules.extend(f"(literal {json.dumps(str(p), ensure_ascii=False)})" for p in sorted(files)
                 if not any(p.is_relative_to(directory) for directory in directories))
    # Let SQLite resolve a symlink before denying metadata on its target.
    # Writes are still denied at both names.
    metadata = [f"(literal {json.dumps(str(p), ensure_ascii=False)})"
                for p in sorted(files) if not p.is_symlink()]
    path.write_text("(version 1)\n(allow default)\n(deny file-read-data file-write* file-map-executable\n"
                    + "\n".join(rules) + ")\n(deny file-read-metadata\n"
                    + "\n".join(metadata) + ")\n", encoding="utf-8")


def collect_denials(stream, events):
    try:
        for line in stream:
            if not line.startswith("{"):
                continue
            entry = json.loads(line)
            match = DENIAL.fullmatch(entry.get("eventMessage") or "")
            if match:
                process, operation, path = match.groups()
                events.put((process, operation, Path(path)))
    finally:
        events.put(None)


def wait_for_probe(events, sandbox, probes, record):
    """Fail closed if a refused native file read never appears in the audit."""
    pending = set()
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        probe = probes / f"probe-{len(pending)}"
        # The fixture is created outside the sandbox. A successful read would
        # demonstrate that the policy failed, rather than validating the audit.
        probe.write_bytes(b"disposable audit fixture")
        pending.add(probe)
        read = subprocess.run([*sandbox, "/bin/cat", str(probe)], capture_output=True, timeout=3)
        if read.returncode == 0:
            raise RuntimeError("The filesystem guard allowed its disposable probe")
        wait_until = min(deadline, time.monotonic() + .5)
        while time.monotonic() < wait_until:
            try:
                event = events.get(timeout=max(0, wait_until - time.monotonic()))
            except queue.Empty:
                break
            if event is None:
                raise RuntimeError("The macOS audit log stream stopped")
            if event[2] in pending:
                return
            record(event)
    raise RuntimeError("The macOS audit log did not report a blocked probe")


def guarded(command):
    directories, files = installed_paths()
    events = queue.Queue()
    violations = []
    directory_keys = tuple(comparison_path(path) for path in directories)
    file_keys = {comparison_path(path) for path in files}

    def record(event):
        process, operation, path = event
        key = comparison_path(path)
        if key in file_keys or any(key.is_relative_to(directory) for directory in directory_keys):
            violations.append(f"{process} attempted {operation} on {path}")

    with tempfile.TemporaryDirectory(prefix="listening-test-audit-") as temporary:
        root = Path(temporary).resolve()
        start_probes = root / "start"
        end_probes = root / "end"
        start_probes.mkdir()
        end_probes.mkdir()
        python_events = root / "python-denials.log"
        details = root / "paths.json"
        details.write_text(json.dumps({"directories": sorted(map(str, directories)),
                                       "files": sorted(map(str, files)),
                                       "events": str(python_events)}), encoding="utf-8")
        policy = root / "test.sb"
        write_policy(policy, directories | {start_probes, end_probes}, files)
        sandbox = ["/usr/bin/sandbox-exec", "-f", str(policy)]
        with subprocess.Popen(["/usr/bin/log", "stream", "--level", "debug", "--style", "ndjson",
                               "--predicate", LOG_FILTER],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) as monitor:
            reader = threading.Thread(target=collect_denials, args=(monitor.stdout, events))
            reader.start()
            try:
                wait_for_probe(events, sandbox, start_probes, record)
                hooks = str(ROOT / "scripts" / "test_audit")
                pythonpath = os.pathsep.join(filter(None, (hooks, os.environ.get("PYTHONPATH"))))
                env = {**os.environ, AUDITED: "1", "PYTHONPATH": pythonpath,
                       "LISTENING_TEST_AUDIT_DETAILS": str(details)}
                result = subprocess.run([*sandbox, *command], cwd=ROOT, env=env)
                wait_for_probe(events, sandbox, end_probes, record)
                # Log delivery can lag the last process exit or reorder events
                # across processes. Let pending kernel reports reach the reader.
                deadline = time.monotonic() + .4
                while time.monotonic() < deadline:
                    try:
                        event = events.get(timeout=max(0, deadline - time.monotonic()))
                    except queue.Empty:
                        break
                    if event is None:
                        raise RuntimeError("The macOS audit log stream stopped")
                    record(event)
            finally:
                monitor.terminate()
                reader.join()
        while not events.empty():
            event = events.get_nowait()
            if event is not None:
                record(event)
        if python_events.is_file():
            violations.extend(python_events.read_text(encoding="utf-8").splitlines())
    for violation in dict.fromkeys(violations):
        print(f"Listening test audit: {violation}", file=sys.stderr)
    if violations:
        return 1
    print("Listening test audit: no attempts to access the installed room.")
    return result.returncode


def main(arguments):
    if arguments == ["--ui"]:
        command = [sys.executable, str(ROOT / "scripts/verify_ui.py")]
    else:
        unittest_args = arguments or ["discover", "-s", "tests"]
        command = [sys.executable, "-m", "unittest", *unittest_args]
    if sys.platform != "darwin":
        return subprocess.call(command, cwd=ROOT)
    try:
        return guarded(command)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Listening test audit failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
