"""Host-side lifecycle for isolated, digest-pinned acquisition workers."""

import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from downloads.engine_contract import WORKER_TIMEOUT_SECONDS
from system import engine_settings
from system.private_files import private_write

IDENTITY = re.compile(r"[a-z][a-z0-9-]{0,47}")
ARCHITECTURE = re.compile(r"[a-z0-9][a-z0-9_]{0,31}")
SOURCE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
RELEASE = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}")
IMAGE = re.compile(r"[a-z0-9][a-z0-9.:/_-]+@sha256:[0-9a-f]{64}")
TOKEN = re.compile(r"[0-9a-f]{16,64}")
FILENAME = re.compile(r"[^/\\\x00-\x1f]{1,255}")
CAPABILITIES = {"search", "fetch"}
LIMIT = 256 * 1024
RESERVE = 20 * 1024 ** 3
MAX_AUDIO = 512 * 1024 ** 2


class WorkerDeadlineExceeded(RuntimeError):
    """A timed-out worker has been stopped and can accept another operation."""


def declares_sources(engine):
    sources = engine.get("sources")
    capabilities = engine["capabilities"]
    return (isinstance(sources, dict) and set(sources) == set(capabilities)
            and all(isinstance(names, list) and names
                    and all(isinstance(name, str) and SOURCE.fullmatch(name) for name in names)
                    for names in sources.values()))


def supports_release(engine, release):
    versions = engine.get("server_versions")
    return isinstance(versions, list) and release in versions


def verify_catalog(path, public_key, architecture, version):
    """Verify the exact manifest bytes before accepting any image references."""
    with tempfile.TemporaryDirectory(prefix="listening-catalog-") as directory:
        verified = Path(directory) / "catalog.json"
        with path.open("rb") as source:
            payload = source.read(LIMIT + 1)
        if len(payload) > LIMIT:
            raise ValueError("The engine catalog is too large.")
        verified.write_bytes(payload)
        check = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(public_key),
             "-signature", str(path.with_suffix(".sig")), str(verified)],
            capture_output=True, timeout=10,
        )
        if check.returncode:
            raise ValueError("The engine catalog signature failed verification.")
    catalog = json.loads(payload)
    if (not isinstance(catalog, dict) or type(catalog.get("version")) is not int
            or catalog["version"] != 1 or not isinstance(catalog.get("engines"), list)):
        raise ValueError("Unsupported engine catalog format.")
    result = {}
    for engine in catalog["engines"]:
        if not isinstance(engine, dict):
            raise ValueError("Invalid engine catalog entry.")
        identity, caps, image, platform = (engine.get(key) for key in ("id", "capabilities", "image", "architecture"))
        if (not isinstance(identity, str) or not IDENTITY.fullmatch(identity)
                or not isinstance(image, str) or not IMAGE.fullmatch(image)
                or not isinstance(caps, list) or not caps
                or any(not isinstance(role, str) or role not in CAPABILITIES for role in caps)
                or not isinstance(engine.get("version"), str)
                or not isinstance(platform, str) or not ARCHITECTURE.fullmatch(platform)):
            raise ValueError("Invalid engine catalog entry.")
        if "sources" not in engine:
            # A valid older signature cannot establish which IDs this package exchanges.
            continue
        if not declares_sources(engine):
            raise ValueError("Invalid source declaration in engine catalog.")
        versions = engine.get("server_versions")
        if (not isinstance(versions, list) or not versions
                or not all(isinstance(release, str) and RELEASE.fullmatch(release) for release in versions)):
            raise ValueError("Invalid server release declaration in engine catalog.")
        settings = engine_settings.declarations(engine.get("settings", []))
        if engine["architecture"] == architecture and supports_release(engine, version):
            if identity in result:
                raise ValueError("Duplicate compatible engine.")
            result[identity] = {key: engine[key] for key in
                                ("id", "version", "image", "capabilities", "server_versions", "sources")}
            if settings:
                result[identity]["settings"] = settings
    return result


class Fleet:
    def __init__(self, state, staging, catalog, public_key, architecture, version):
        self.state = state.resolve()
        self.staging = staging.resolve()
        self.catalog = catalog
        self.public_key = public_key
        self.architecture = architecture
        self.version = version
        self.namespace = hashlib.sha256(os.fsencode(self.state)).hexdigest()[:16]
        self.status_lock = threading.Lock()
        self.status_until = 0.0
        self.running = frozenset()
        self.lock = threading.RLock()
        self.transfer = threading.BoundedSemaphore(1)
        self.searches = threading.BoundedSemaphore(2)
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.staging.mkdir(exist_ok=True)
        self.state_file = self.state / "engines.json"
        self.engines = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}

    def available(self, release):
        return verify_catalog(self.catalog, self.public_key, self.architecture, release)

    def save(self):
        private_write(self.state_file, json.dumps(self.engines))

    def container(self, identity):
        return f"listening-engine-{self.namespace}-{identity}"

    def docker(self, *arguments, timeout=20, check=True):
        result = subprocess.run(["docker", *arguments], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError("The worker container command failed. Check the host service.")
        return result

    def invalidate_status(self):
        with self.status_lock:
            self.status_until = 0.0

    def running_containers(self):
        with self.status_lock:
            if time.monotonic() >= self.status_until:
                try:
                    result = self.docker("ps", "--filter", "status=running",
                                         "--filter", f"label=music.listening.fleet={self.namespace}",
                                         "--format", "{{.Names}}", timeout=1)
                    self.running = frozenset(result.stdout.decode("utf-8").splitlines())
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    self.running = frozenset()
                self.status_until = time.monotonic() + 3
            return self.running

    def mount(self, identity):
        scratch = self.state / identity / "scratch"
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        return scratch

    def validate_image(self, image):
        # Inspect the immutable digest reference Docker just pulled rather
        # than trusting the architecture label in the signed catalog.
        platform = self.docker("image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image)
        if platform.stdout.decode("utf-8").strip() != f"linux/{self.architecture}":
            raise ValueError("The verified image does not match this host architecture.")

    def start(self, engine):
        self.validate_image(engine["image"])
        identity = engine["id"]
        credentials = self.state / identity / "credentials"
        credentials.mkdir(mode=0o700, parents=True, exist_ok=True)
        mounts = ["--mount", f"type=bind,src={self.mount(identity)},dst=/scratch",
                  "--mount", f"type=bind,src={credentials},dst=/credentials,readonly"]
        self.docker("run", "--detach", "--init", "--name", self.container(identity),
                    "--platform", f"linux/{self.architecture}",
                    "--label", f"music.listening.fleet={self.namespace}",
                    "--restart", "unless-stopped", "--user", f"{os.getuid()}:{os.getgid()}",
                    "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--memory", "512m", "--cpus", "1", "--pids-limit", "128",
                    *mounts, "--entrypoint", "/bin/sleep", engine["image"], "infinity")
        self.invalidate_status()

    def stop(self, identity):
        self.docker("rm", "-f", self.container(identity), check=False)
        self.invalidate_status()

    def worker(self, identity, request, connection, *, scratch=None):
        # Keep protocol output bounded independently of the worker implementation.
        with subprocess.Popen(["docker", "exec", "-i", self.container(identity),
                               "/usr/local/bin/listening-engine"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as process:
            try:
                payload = memoryview(json.dumps(request).encode() + b"\n")
                os.set_blocking(process.stdin.fileno(), False)
                deadline = time.monotonic() + WORKER_TIMEOUT_SECONDS[request["action"]]
                output = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WorkerDeadlineExceeded("The engine timed out.")
                    if scratch is not None and shutil.disk_usage(scratch).free < RESERVE:
                        raise OSError("Listening is keeping 20 GiB free on engine staging.")
                    readable, writable, _ = select.select([process.stdout, connection],
                                                          [process.stdin] if payload else [],
                                                          [], min(remaining, 0.5))
                    if connection in readable:
                        raise ConnectionError("The server cancelled the engine request.")
                    if process.stdin in writable:
                        try:
                            payload = payload[os.write(process.stdin.fileno(), payload):]
                        except BlockingIOError:
                            pass
                        if not payload:
                            process.stdin.close()
                    if process.stdout not in readable:
                        continue
                    data = os.read(process.stdout.fileno(), 8192)
                    if not data:
                        break
                    output.extend(data)
                    if len(output) > LIMIT:
                        raise ValueError("The engine reply was too large.")
                if process.wait(timeout=2):
                    raise RuntimeError("The engine command failed.")
                result = json.loads(output)
                if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                    raise ValueError("Invalid engine response.")
                version = result.pop("version", 1)
                if type(version) is not int or version != 1:
                    raise ValueError("Unsupported engine response version.")
                return result
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                process.kill()
                # Killing docker exec closes its transport, but may leave an
                # exec process alive. Restart this worker to terminate it.
                try:
                    self.docker("restart", self.container(identity))
                finally:
                    self.invalidate_status()
                if isinstance(error, subprocess.TimeoutExpired):
                    raise WorkerDeadlineExceeded("The engine timed out.") from error
                raise

    def snapshot(self, scratch, job, name):
        if not isinstance(name, str) or not FILENAME.fullmatch(name) or name in (".", ".."):
            raise ValueError("The engine did not return a safe filename.")
        if not isinstance(job, str) or not TOKEN.fullmatch(job):
            raise ValueError("Invalid staging job.")
        target = self.staging / job
        if not target.is_dir() or target.is_symlink():
            raise ValueError("The server has cancelled this staging job.")
        directory = os.open(scratch, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
        try:
            candidate = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(candidate, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAX_AUDIO:
                raise ValueError("The engine did not return a regular audio candidate.")
            destination = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
            try:
                space = os.fstatvfs(destination)
                if space.f_bavail * space.f_frsize < RESERVE + info.st_size:
                    raise OSError("Listening is keeping 20 GiB free on staging.")
                output_file = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                      mode=0o600, dir_fd=destination)
                with os.fdopen(output_file, "wb") as output:
                    remaining = info.st_size
                    while remaining:
                        data = source.read(min(remaining, 1024 * 1024))
                        if not data:
                            raise OSError("The engine candidate changed during transfer.")
                        output.write(data)
                        remaining -= len(data)
                    after = os.fstat(source.fileno())
                    if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                            after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise OSError("The engine candidate changed during transfer.")
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                os.close(destination)

    def requested_release(self, request):
        release = request.get("server_release", self.version)
        if not isinstance(release, str) or not RELEASE.fullmatch(release):
            raise ValueError("Invalid Listening server release.")
        return release

    def compatible(self, engine, release):
        return supports_release(engine, release) and declares_sources(engine)

    def retry_at(self, identity):
        try:
            deadline = int((self.state / identity / "login-retry-at").read_text())
        except FileNotFoundError:
            return 0
        return deadline if deadline > time.time() else 0

    def login_pause(self, identity):
        if deadline := self.retry_at(identity):
            return {"ok": False, "retryable": True, "code": "login_unavailable",
                    "retry_after_seconds": max(1, deadline - int(time.time()))}

    def acquire_worker(self, identity, request, connection, **options):
        # The caller holds this provider's operation lease across check, call and save.
        if paused := self.login_pause(identity):
            return paused
        result = self.worker(identity, request, connection, **options)
        if result.get("ok") is False and result.get("code") == "login_unavailable":
            seconds = result.get("retry_after_seconds")
            if type(seconds) is not int or not 1 <= seconds <= 86400:
                # v1 workers can report this condition without a suggested delay.
                seconds = 1800
            private_write(self.state / identity / "login-retry-at", str(int(time.time()) + seconds + 1))
            return {"ok": False, "retryable": True, "code": "login_unavailable", "retry_after_seconds": seconds}
        return result

    def acquire(self, request, connection):
        identity, action = request["engine"], request["action"]
        release = self.requested_release(request)
        if identity not in self.engines:
            return {"ok": False, "error": "Install a compatible engine.", "retryable": True}
        concurrency = self.transfer if action == "fetch" else self.searches
        if not concurrency.acquire(blocking=False):
            return {"ok": False, "error": "Engine capacity is busy.", "retryable": True}
        try:
            # A provider-wide lease also serializes management and cancellation
            # against its executable. Different providers can work independently.
            with (self.state / identity / ".operation").open("a") as lease:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                engine = self.engines.get(identity)
                if not engine or not engine["enabled"] or not self.compatible(engine, release) or action not in engine["capabilities"]:
                    return {"ok": False, "error": "Enable a compatible engine.", "retryable": True}
                if action == "search":
                    return self.acquire_worker(identity, {"version": 1, "action": "search",
                                                          "recording": request["recording"]}, connection)
                candidate = request.get("candidate")
                if not isinstance(candidate, dict) or candidate.get("source") not in engine["sources"]["fetch"]:
                    return {"ok": False, "error": "The download engine cannot fetch this source.", "retryable": True}
                scratch = self.mount(identity)
                if shutil.disk_usage(scratch).free <= RESERVE:
                    raise OSError("Listening is keeping 20 GiB free on engine staging.")
                with tempfile.TemporaryDirectory(dir=scratch, prefix="job-") as directory:
                    worker_request = {"version": 1, "action": "fetch", "candidate": candidate,
                                      "staging": "/scratch/" + Path(directory).name}
                    result = self.acquire_worker(identity, worker_request, connection, scratch=scratch)
                    if result["ok"]:
                        self.snapshot(Path(directory), request.get("job"), result.get("file"))
                        return {"ok": True, "file": result["file"]}
                    return result
        finally:
            concurrency.release()

    def manage(self, request, connection):
        action = request["action"]
        release = self.requested_release(request)
        if action == "status":
            installed = list(self.engines.values())
            running = self.running_containers() if any(engine["enabled"] for engine in installed) else frozenset()
            return {"ok": True, "engines": [
                {**engine, "retry_at": self.retry_at(engine["id"]), "configured_settings": engine_settings.saved_keys(
                    self.state / engine["id"] / "credentials", engine.get("settings", [])),
                 "ready": engine["enabled"] and self.compatible(engine, release)
                 and self.container(engine["id"]) in running}
                for engine in installed
            ]}
        if action == "catalog":
            return {"ok": True, "engines": list(self.available(release).values())}
        identity = request.get("engine")
        if not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
            raise ValueError("Choose an engine.")
        with self.lock:
            directory = self.state / identity
            directory.mkdir(mode=0o700, exist_ok=True)
            with (directory / ".operation").open("a") as lease:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if action == "configure":
                    return self.configure(identity, request.get("settings"), release)
                return self.change(identity, action, connection, release)

    def configure(self, identity, updates, release):
        engine = self.engines.get(identity)
        if not engine or not self.compatible(engine, release):
            raise ValueError("Install a compatible engine first.")
        directory = self.state / identity / "credentials"
        engine_settings.write(directory, engine.get("settings", []), updates)
        if engine["enabled"]:
            # Older containers can lack the credentials mount. Inspect the
            # container so a failed restart can be retried after saving.
            status = self.docker(
                "inspect", "--format",
                '{{.State.Running}}{{range .Mounts}}{{if eq .Destination "/credentials"}} {{.RW}}{{end}}{{end}}',
                self.container(identity), check=False,
            )
            if status.returncode or status.stdout.strip() != b"true false":
                self.stop(identity)
                self.start(engine)
        return {"ok": True}

    def change(self, identity, action, connection, release):
        current = self.engines.get(identity)
        if action in ("install", "update"):
            candidate = self.available(release).get(identity)
            if candidate is None:
                raise ValueError("No compatible package is listed for this host.")
            if current and current["image"] == candidate["image"]:
                refreshed = {**current, **candidate}
                if "settings" not in candidate:
                    refreshed.pop("settings", None)
                self.engines[identity] = refreshed
                self.save()
                return {"ok": True, "engine": refreshed}
            if (action == "install") != (current is None):
                raise ValueError("Choose install for a new engine or update for an installed engine.")
            self.docker("pull", candidate["image"], timeout=180)
            self.validate_image(candidate["image"])
            previous = ({key: current[key] for key in (*candidate, "settings") if key in current}
                        if current else None)
            replacement = {**candidate, "enabled": bool(current and current["enabled"]), "previous": previous}
        elif not current:
            raise ValueError("Install this engine first.")
        elif action == "rollback":
            if not current.get("previous") or not self.compatible(current["previous"], release):
                raise ValueError("No compatible previous package is available.")
            previous = {key: value for key, value in current.items() if key not in {"enabled", "previous"}}
            replacement = {**current["previous"], "enabled": current["enabled"], "previous": previous}
        elif action == "remove":
            self.stop(identity)
            (self.state / identity / "login-retry-at").unlink(missing_ok=True)
            del self.engines[identity]
            self.save()
            return {"ok": True}
        elif action == "test":
            if not self.compatible(current, release):
                raise ValueError("Update this engine from the signed catalog to restore compatibility.")
            if paused := self.login_pause(identity):
                return paused
            if not current["enabled"]:
                self.start(current)
            try:
                return self.acquire_worker(identity, {"version": 1, "action": "test"}, connection)
            finally:
                if not current["enabled"]:
                    self.stop(identity)
        elif action in ("enable", "disable"):
            if action == "enable" and not self.compatible(current, release):
                raise ValueError("Update this engine from the signed catalog to restore compatibility.")
            replacement = {**current, "enabled": action == "enable"}
            if replacement == current and (not current["enabled"]
                                           or self.container(identity) in self.running_containers()):
                return {"ok": True, "engine": current}
        else:
            raise ValueError("Unknown engine action.")
        if replacement["enabled"] and (paused := self.login_pause(identity)):
            return paused
        self.stop(identity)
        result = {}
        try:
            if replacement["enabled"]:
                self.start(replacement)
                result = self.acquire_worker(identity, {"version": 1, "action": "test"}, connection)
                if not result["ok"]:
                    raise RuntimeError("The new package failed its readiness test.")
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            self.stop(identity)
            if current and current["enabled"]:
                self.start(current)
            if result.get("code") == "login_unavailable":
                return result
            raise
        self.engines[identity] = replacement
        self.save()
        return {"ok": True, "engine": replacement}
