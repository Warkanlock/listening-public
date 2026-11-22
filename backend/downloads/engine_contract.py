"""The runner's versioned search and fetch boundary to the host manager."""

import contextlib
import fcntl
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from system.client_artifacts import release_version

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_VERSION = 1
MAX_RESPONSE_BYTES = 64 * 1024
MAX_CANDIDATES = 20
MAX_CANDIDATE_BYTES = 512 * 1024 * 1024
WORKER_TIMEOUT_SECONDS = {"search": 35, "fetch": 180, "test": 35}
# Allow the manager to stop a timed-out worker or finish the disk handoff.
MANAGER_RESPONSE_GRACE_SECONDS = 25
JOB_DIRECTORY = re.compile(r"[0-9a-f]{32}")


class EngineError(RuntimeError):
    def __init__(self, message, *, retryable=True):
        super().__init__(message)
        self.retryable = retryable


class EngineTimeout(EngineError):
    """An engine operation exceeded its deadline."""


class CandidateTimeout(EngineTimeout):
    """A candidate timed out and released its worker so another can be tried."""


class WorkerTimeout(CandidateTimeout):
    """The manager confirmed a worker timeout and released the operation."""


TIMEOUT_MESSAGES = {
    "search": "The download engine timed out while searching.",
    "fetch": "The download engine timed out while downloading a candidate.",
}


@contextlib.contextmanager
def timed_stage(engine, stage, timeout, *, attempt=None):
    started = time.monotonic()
    outcome = "completed"
    try:
        yield
    except (EngineTimeout, subprocess.TimeoutExpired):
        outcome = "timed out"
        raise
    except Exception:
        outcome = "failed"
        raise
    finally:
        label = f"{stage} {attempt}" if attempt is not None else stage
        elapsed = time.monotonic() - started
        print(f"Download engine {engine}: {label} {outcome} in {elapsed:.3f}s (limit {timeout:.1f}s).", flush=True)


class InsufficientDownloadSpace(Exception):
    """Pause a transfer at the room's free-space reserve."""


def engine_selection(settings):
    """Absent means an existing room can use its legacy downloader."""
    if "download_engines" not in settings:
        return None
    selected = settings["download_engines"]
    if not isinstance(selected, dict):
        return {}
    if not all(isinstance(selected.get(role), str) and selected[role] for role in ("search", "fetch")):
        return {}
    return {role: selected[role] for role in ("search", "fetch")}


def login_wait(seconds):
    minutes = max(1, (seconds + 59) // 60)
    unit = "minute" if minutes == 1 else "minutes"
    return EngineError(f"The source could not sign in. Searches and downloads are paused for about {minutes} {unit}. "
                       "Your requests stay queued. Check Extensions for the next attempt.")


def manager_call(action, *, timeout=2, check_space=None, server_release=None, **fields):
    timeout_message = TIMEOUT_MESSAGES.get(action, "The download engine timed out.")
    if timeout <= 0:
        raise EngineTimeout(timeout_message)
    address = os.environ.get("LISTENING_ENGINE_SOCKET")
    if not address:
        raise EngineError("The download engine manager is not configured.")
    if server_release is None:
        server_release = release_version(ROOT)
    payload = json.dumps({"version": PROTOCOL_VERSION, "action": action,
                          "server_release": server_release, **fields}, allow_nan=False)
    deadline = time.monotonic() + timeout
    data = bytearray()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(min(1, timeout))
            connection.connect(address)
            connection.sendall(payload.encode("utf-8") + b"\n")
            while len(data) <= MAX_RESPONSE_BYTES:
                if check_space:
                    check_space()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EngineTimeout(timeout_message)
                connection.settimeout(min(1, remaining))
                try:
                    chunk = connection.recv(8192)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                data.extend(chunk)
                if b"\n" in data:
                    break
    except OSError as error:
        raise EngineError("The download engine manager is unavailable.") from error
    line, newline, _ = data.partition(b"\n")
    if not newline or len(line) > MAX_RESPONSE_BYTES:
        raise EngineError("The download engine returned an incomplete response.")
    try:
        response = json.loads(line)
    except (UnicodeError, ValueError) as error:
        raise EngineError("The download engine returned an invalid response.") from error
    if not isinstance(response, dict):
        raise EngineError("The download engine returned an invalid response.")
    if response.get("ok") is not True:
        if response.get("code") == "timeout":
            raise WorkerTimeout(timeout_message)
        if response.get("code") == "login_unavailable":
            seconds = response.get("retry_after_seconds")
            if type(seconds) is int and 1 <= seconds <= 86401:
                raise login_wait(seconds)
            raise EngineError("The source could not sign in. Check its connection and account in Extensions.")
        # Engine text may contain peer data or credentials. Use a room-owned diagnostic.
        raise EngineError("The download engine could not complete the operation.",
                          retryable=response.get("retryable") is not False)
    return response


def selected_engine_sources(selection, *, server_release=None, for_acquisition=False):
    """Return the selected searcher's namespaces after validating the pair."""
    engines = manager_call("status", server_release=server_release).get("engines")
    sources = _compatible_sources(engines, selection)
    if for_acquisition and (deadline := selected_retry_at(engines, selection)):
        raise login_wait(max(1, deadline - int(time.time())))
    return sources


def selected_retry_at(engines, selection):
    now = time.time()
    return max((engine["retry_at"] for engine in engines if isinstance(engine, dict)
                and engine.get("id") in selection.values() and type(engine.get("retry_at")) is int
                and now < engine["retry_at"] <= now + 86401), default=0)


def _compatible_sources(engines, selection):
    if not isinstance(engines, list):
        raise EngineError("The download engine manager returned an invalid status.")
    ready = {engine["id"]: engine for engine in engines
             if isinstance(engine, dict) and isinstance(engine.get("id"), str)
             and engine.get("enabled") is True and engine.get("ready") is True}
    sources = {}
    for role in ("search", "fetch"):
        engine = ready.get(selection.get(role), {})
        capabilities = engine.get("capabilities")
        declared = engine.get("sources")
        names = declared.get(role) if isinstance(declared, dict) else None
        if (not isinstance(capabilities, list) or role not in capabilities
                or not isinstance(names, list) or not names
                or not all(isinstance(name, str) and name for name in names)):
            raise EngineError("Choose available engines with a signed source declaration in Settings.")
        sources[role] = set(names)
    if not sources["search"] <= sources["fetch"]:
        raise EngineError("The download engine cannot fetch all sources returned by the selected search engine.")
    return sources["search"]


def source_summary(settings):
    """Expose source choices to listeners without catalog internals or credentials."""
    selection = engine_selection(settings)
    summary = {"selection": selection, "installed": [], "inventory_available": False,
               "state": "legacy" if selection is None else "unconfigured"}
    if not os.environ.get("LISTENING_ENGINE_SOCKET"):
        if selection:
            summary["state"] = "unavailable"
        return summary
    try:
        engines = manager_call("status").get("engines")
        if not isinstance(engines, list):
            raise EngineError("Invalid installed engine list.")
        summary["inventory_available"] = True
        public_fields = ("id", "version", "enabled", "ready", "capabilities", "sources", "retry_at")
        summary["installed"] = [{key: engine[key] for key in public_fields if key in engine}
                                for engine in engines if isinstance(engine, dict) and engine.get("id")]
        if selection:
            _compatible_sources(engines, selection)
            summary["state"] = "ready" if os.environ.get("LISTENING_ENGINE_STAGING") else "unavailable"
            if summary["state"] == "ready" and (deadline := selected_retry_at(engines, selection)):
                summary.update(state="cooldown", retry_at=deadline)
    except EngineError:
        if selection:
            summary["state"] = "unavailable"
    return summary


def engines_ready(settings, *, server_release=None, for_acquisition=False):
    selection = engine_selection(settings)
    if not selection or not os.environ.get("LISTENING_ENGINE_STAGING"):
        return False
    try:
        selected_engine_sources(selection, server_release=server_release, for_acquisition=for_acquisition)
    except EngineError:
        return False
    return True


def candidates_from(response):
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
        raise EngineError("The download engine returned an invalid candidate list.")
    unique = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise EngineError("The download engine returned an invalid candidate.")
        source, candidate_id = candidate.get("source"), candidate.get("id")
        if not all(isinstance(value, str) and 0 < len(value) <= 512 for value in (source, candidate_id)):
            raise EngineError("The download engine returned an invalid candidate.")
        key = (source, candidate_id)
        if key not in unique:
            unique.add(key)
            yield {"source": source, "id": candidate_id}


def _copy_result(directory, name, destination, suffixes, check_space):
    if (not isinstance(name, str) or name in {"", ".", ".."}
            or Path(name).name != name or "\x00" in name):
        raise EngineError("The download engine returned an invalid file name.")
    suffix = Path(name).suffix.lower()
    if suffix not in suffixes:
        return None
    # Neither a symlink nor a FIFO from a provider may become an audio candidate.
    try:
        folder = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        finally:
            os.close(folder)
    except OSError as error:
        raise EngineError("The download engine did not return a regular staged file.") from error
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or not 0 < before.st_size <= MAX_CANDIDATE_BYTES):
            raise EngineError("The download engine did not return a regular staged file.")
        check_space(before.st_size)
        path = destination / ("candidate" + suffix)
        with path.open("xb") as output:
            remaining = before.st_size
            while remaining:
                check_space()
                chunk = source.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise EngineError("The staged download changed before verification.")
                output.write(chunk)
                remaining -= len(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise EngineError("The staged download changed before verification.")
    return path


def _recover_interrupted_transfers(root, state):
    # A killed runner releases the transfer lock, even if its finally
    # blocks did not run. Only this room's disposable copies live here.
    for directory in root.iterdir():
        if JOB_DIRECTORY.fullmatch(directory.name) and directory.is_dir() and not directory.is_symlink():
            shutil.rmtree(directory)
    for directory in state.glob("engine-candidate-*"):
        if directory.is_dir() and not directory.is_symlink():
            shutil.rmtree(directory)


@contextlib.contextmanager
def fetch_recording(selection, recording, *, state, free_bytes, reserve, estimate, accept, timeout):
    """Yield one core-owned candidate or None. A provider never gets its path."""
    staging = os.environ.get("LISTENING_ENGINE_STAGING")
    if not selection or not staging:
        raise EngineError("Choose compatible search and download engines in Settings.")
    root = Path(staging)
    if not root.is_dir() or root.is_symlink():
        raise EngineError("The shared download staging folder is unavailable.")

    def check_space(required=0):
        if min(free_bytes(), shutil.disk_usage(root).free) - required < reserve:
            raise InsufficientDownloadSpace

    # Manual invocations and the supervisor share the same staging root and lock.
    with (state / "engine-transfer.lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _recover_interrupted_transfers(root, state)
        deadline = time.monotonic() + timeout

        def remaining_time():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EngineTimeout("The download engine reached the time limit for this recording.")
            return remaining

        def check_handoff_space(required=0):
            remaining_time()
            check_space(required)

        check_space(estimate)
        sources = selected_engine_sources(selection, for_acquisition=True)
        search_budget = min(WORKER_TIMEOUT_SECONDS["search"] + MANAGER_RESPONSE_GRACE_SECONDS,
                            remaining_time())
        with timed_stage(selection["search"], "search", search_budget):
            result = manager_call("search", timeout=search_budget, engine=selection["search"], recording=recording)
        candidates = list(candidates_from(result))
        if any(candidate["source"] not in sources for candidate in candidates):
            raise EngineError("The search engine returned a candidate outside its declared sources.")
        fetch_budget = WORKER_TIMEOUT_SECONDS["fetch"] + MANAGER_RESPONSE_GRACE_SECONDS
        timed_out = None
        for attempt, candidate in enumerate(candidates, 1):
            check_space(estimate)
            budget = min(fetch_budget, remaining_time())
            job = uuid.uuid4().hex
            directory = root / job
            directory.mkdir(mode=0o700)
            with tempfile.TemporaryDirectory(prefix="engine-candidate-", dir=state) as copy:
                try:
                    try:
                        with timed_stage(selection["fetch"], "transfer", budget, attempt=attempt):
                            fetched = manager_call("fetch", engine=selection["fetch"], candidate=candidate,
                                                   job=job, timeout=budget, check_space=check_space)
                    except EngineError as error:
                        if error.retryable:
                            raise
                        continue
                    path = _copy_result(directory, fetched.get("file"), Path(copy),
                                        recording["formats"], check_handoff_space)
                    accepted = path is not None and accept(path, remaining_time)
                    remaining_time()
                except CandidateTimeout as error:
                    timed_out = error
                else:
                    if accepted:
                        yield path
                        return
                finally:
                    # The manager owns its own worker scratch. This directory only
                    # holds the bounded handoff to the core.
                    if directory.exists():
                        shutil.rmtree(directory)
        if timed_out is not None:
            raise timed_out
        yield None
