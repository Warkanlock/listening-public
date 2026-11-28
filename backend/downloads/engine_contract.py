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
from system.formats import REQUEST_SUFFIXES
from system.normalize import credit_key

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


class FetchTimeout(EngineTimeout):
    """The room did not receive a transfer reply before its deadline."""


class CandidateHandoffError(EngineError):
    """The engine did not hand off a complete, regular candidate file."""


class EngineBusy(EngineError):
    """The worker is already in use by another request."""


class EngineUpdateConflict(EngineError):
    messages = {
        "extension_update_changed": "The extension package changed. Review the latest update.",
        "extension_update_current": "This extension is already up to date.",
        "extension_update_sources": "Choose compatible download sources before updating this extension.",
        "extension_update_settings": "Disable this extension before updating and setting up its new credentials.",
    }

    def __init__(self, code):
        super().__init__(self.messages[code], retryable=False)
        self.code = code


class EngineCooldown(EngineError):
    def __init__(self, message, seconds):
        super().__init__(message)
        self.seconds = seconds


class CandidateTimeout(EngineTimeout):
    """A candidate timed out and released its worker so another can be tried."""


class WorkerTimeout(CandidateTimeout):
    """The manager confirmed a worker timeout and released the operation."""


TIMEOUT_MESSAGES = {
    "search": "The download engine timed out while searching.",
    "fetch": "The download engine timed out while downloading a candidate.",
    "health": "The extension timed out during its health check.",
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
    selected = settings.get("download_engines")
    if not isinstance(selected, dict):
        return {}
    if not all(isinstance(selected.get(role), str) and selected[role] for role in ("search", "fetch")):
        return {}
    return {role: selected[role] for role in ("search", "fetch")}


def login_wait(seconds):
    minutes = max(1, (seconds + 59) // 60)
    unit = "minute" if minutes == 1 else "minutes"
    return EngineCooldown(f"The source could not sign in. Your requests stay queued for about {minutes} {unit}. "
                          "Check Extensions for details.", seconds)


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
        if action in ("update_preview", "update_reviewed"):
            code = response.get("code")
            if isinstance(code, str) and code in EngineUpdateConflict.messages:
                raise EngineUpdateConflict(code)
        if response.get("code") == "busy":
            raise EngineBusy("The extension is busy. Try again after the current operation.")
        if response.get("code") == "timeout":
            raise WorkerTimeout(timeout_message)
        if response.get("code") == "login_unavailable":
            seconds = response.get("retry_after_seconds")
            if type(seconds) is int and 1 <= seconds <= 86401:
                raise login_wait(seconds)
            raise login_wait(90)
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
               "state": "unconfigured"}
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


def extension_health(settings):
    """Check the active download path without requesting or transferring a song."""
    checks = []

    def report(label, state, message):
        checks.append({"label": label, "state": state, "message": message})

    tools = bool(shutil.which("ffprobe") and shutil.which("ffmpeg"))
    report("Audio verification", "ready" if tools else "failed",
           "Audio verification tools are available." if tools else "Install FFmpeg on the room server.")
    selection = engine_selection(settings)
    if not selection:
        report("Download sources", "failed", "Choose search and download sources in Extensions.")
        return {"checks": checks}
    staging = os.environ.get("LISTENING_ENGINE_STAGING")
    root = Path(staging) if staging else None
    staging_ready = root is not None and root.is_dir() and not root.is_symlink() and os.access(root, os.W_OK | os.X_OK)
    report("Download storage", "ready" if staging_ready else "failed",
           "Download staging is writable." if staging_ready else "The room cannot write to the download staging folder.")
    try:
        status = manager_call("status")
        engines = status.get("engines")
        _compatible_sources(engines, selection)
    except EngineError as error:
        report("Download sources", "failed", str(error))
        return {"checks": checks}
    if status.get("health_check") is not True:
        report("Download sources", "unverified", "Update the room’s engine manager to run provider checks.")
        return {"checks": checks}
    for engine in dict.fromkeys(selection.values()):
        roles = [label for role, label in (("search", "Search"), ("fetch", "Download")) if selection[role] == engine]
        label = f"{' and '.join(roles)} · {engine}"
        if deadline := selected_retry_at(engines, {"selected": engine}):
            report(label, "failed", str(login_wait(max(1, deadline - int(time.time())))))
            continue
        try:
            manager_call("health", engine=engine,
                         timeout=WORKER_TIMEOUT_SECONDS["test"] + MANAGER_RESPONSE_GRACE_SECONDS)
        except EngineBusy as error:
            report(label, "unverified", str(error))
        except EngineError as error:
            report(label, "failed", str(error))
        else:
            report(label, "ready", "The extension passed its setup check.")
    return {"checks": checks}


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
            entry = {"source": source, "id": candidate_id}
            for field, cap in (("label", 180), ("format", 12)):
                value = candidate.get(field)
                if isinstance(value, str) and value:
                    entry[field] = "".join(char for char in value[:cap] if char.isprintable())
            for field, cap in (("duration", 86400), ("bitrate", 100000)):
                value = candidate.get(field)
                if type(value) is int and 0 < value <= cap:
                    entry[field] = value
            for field in ("recommended", "identity_match"):
                if type(candidate.get(field)) is bool:
                    entry[field] = candidate[field]
            yield entry


def simplify_title(title):
    return re.sub(r"\s*[-(]\s*(?:(?:digital|original)\s+)?(?:\d{4}\s+)?"
                  r"remaster(?:ed)?(?:\s+\d{4})?\)?\s*$", "", title, flags=re.IGNORECASE).strip() or title


def engine_recording(record, search_title, suffixes, preferred):
    return {
        "id": str(record["key"]), "title": record["title"], "search_title": search_title,
        "artist": record["artist"], "album": record["album"],
        "duration": float(record["length"] or 0), "formats": list(suffixes),
        "preferred_format": preferred,
    }


def request_recording(row, record, choice):
    item = record or {**row, "key": f"request:{row['id']}"}
    suffixes = REQUEST_SUFFIXES[choice]
    recording = engine_recording(item, row["search_title"] or simplify_title(item["title"]), suffixes, suffixes[0][1:])
    if row.get("version"):
        recording["version"] = row["version"]
    return recording


def matching_copies(recording, candidates):
    """Exclude known unrelated results while preserving unclassified plugin candidates."""
    artist = credit_key(re.split(r",|;|\s+(?:featuring|feat\.?|ft\.?)\s+", recording["artist"], maxsplit=1,
                                 flags=re.IGNORECASE)[0])
    title = credit_key(simplify_title(recording["title"]))
    if not artist or not title:
        return candidates
    matching = []
    for copy in candidates:
        label = " " + credit_key(copy.get("label", "").rsplit(".", 1)[0]) + " "
        if (copy.get("recommended") is not False or copy.get("identity_match") is True
                or (f" {title} " in label and f" {artist} " in label)):
            matching.append(copy)
    return matching


def search_recording(selection, recording, *, timeout=None):
    sources = selected_engine_sources(selection, for_acquisition=True)
    budget = WORKER_TIMEOUT_SECONDS["search"] + MANAGER_RESPONSE_GRACE_SECONDS
    if timeout is not None:
        budget = min(budget, timeout)
    with timed_stage(selection["search"], "search", budget):
        query = " ".join(f"{recording['search_title'][:400]} {recording['artist'][:400]}".lower().split())
        reply = manager_call("search", timeout=budget, engine=selection["search"],
                             recording={**recording, "search_query": query})
    candidates = list(candidates_from(reply))
    print(f"Download engine {selection['search']}: returned {len(candidates)} candidate(s).", flush=True)
    if any(candidate["source"] not in sources for candidate in candidates):
        raise EngineError("The search engine returned a candidate outside its declared sources.")
    return matching_copies(recording, candidates)


PROGRESS_FILE = ".download-progress"


def read_transfer_percent(directory):
    """Read one bounded, optional progress sample from an isolated transfer."""
    try:
        folder = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(PROGRESS_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        finally:
            os.close(folder)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 1 <= info.st_size <= 3:
                return None
            value = os.read(fd, 4)
            if value.isdigit() and len(value) <= 3 and int(value) <= 100:
                return int(value)
        finally:
            os.close(fd)
    except OSError:
        pass
    return None


def _copy_result(directory, name, destination, suffixes, check_space):
    if (not isinstance(name, str) or name in {"", ".", ".."}
            or Path(name).name != name or "\x00" in name):
        raise CandidateHandoffError("The download engine returned an invalid file name.")
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
        raise CandidateHandoffError("The download engine did not return a regular staged file.") from error
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or not 0 < before.st_size <= MAX_CANDIDATE_BYTES):
            raise CandidateHandoffError("The download engine did not return a regular staged file.")
        check_space(before.st_size)
        path = destination / ("candidate" + suffix)
        with path.open("xb") as output:
            remaining = before.st_size
            while remaining:
                check_space()
                chunk = source.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise CandidateHandoffError("The staged download changed before verification.")
                output.write(chunk)
                remaining -= len(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise CandidateHandoffError("The staged download changed before verification.")
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
def fetch_recording(selection, recording, *, state, free_bytes, reserve, estimate, accept, timeout,
                    chosen=None, recommended_only=False, on_review=None, on_transfer=None, on_progress=None):
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
        uncertain = []
        if chosen is None:
            candidates = search_recording(selection, recording, timeout=remaining_time())
            if recommended_only:
                uncertain = [candidate for candidate in candidates if candidate.get("recommended") is False]
                candidates = [candidate for candidate in candidates if candidate.get("recommended") is not False]
        else:
            sources = selected_engine_sources(selection, for_acquisition=True)
            if chosen["source"] not in sources:
                raise EngineError("This copy is no longer available from the room’s sources.", retryable=False)
            candidates = [chosen]
        fetch_budget = WORKER_TIMEOUT_SECONDS["fetch"] + MANAGER_RESPONSE_GRACE_SECONDS
        timed_out = None
        for attempt, candidate in enumerate(candidates, 1):
            if on_transfer is not None:
                on_transfer()
            check_space(estimate)
            budget = min(fetch_budget, remaining_time())
            job = uuid.uuid4().hex
            directory = root / job
            directory.mkdir(mode=0o700)
            last_percent = None

            def monitor_transfer():
                nonlocal last_percent
                check_space()
                if on_progress is not None:
                    percent = read_transfer_percent(directory)
                    if percent is not None and percent != last_percent:
                        on_progress(percent)
                        last_percent = percent

            with tempfile.TemporaryDirectory(prefix="engine-candidate-", dir=state) as copy:
                try:
                    try:
                        with timed_stage(selection["fetch"], "transfer", budget, attempt=attempt):
                            reference = {key: candidate[key] for key in ("source", "id")}
                            options = {"report_progress": True} if on_progress is not None else {}
                            fetched = manager_call("fetch", engine=selection["fetch"], candidate=reference,
                                                   job=job, timeout=budget, check_space=monitor_transfer, **options)
                    except WorkerTimeout:
                        raise
                    except EngineTimeout as error:
                        raise FetchTimeout(str(error)) from error
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
        if uncertain and on_review is not None:
            on_review(uncertain)
        yield None
