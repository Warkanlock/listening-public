#!/usr/bin/env python3
"""Run the private Listening update socket on the Docker host."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import socketserver
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib


REVISION = re.compile(r"[0-9a-f]{40}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
REVISION_LABEL = "org.opencontainers.image.revision"
DATA_MOUNTS = {"/config", "/library"}
ENGINE_MOUNTS = {"/run/listening-engines", "/engine-staging"}
PERSISTENT_MOUNTS = DATA_MOUNTS | ENGINE_MOUNTS


class UpdateError(Exception):
    pass


def run(command, *, timeout=20, output=None):
    result = subprocess.run(command, stdout=output or subprocess.PIPE,
                            text=True, timeout=timeout, check=True)
    return result.stdout.strip() if output is None else None


def source_member(member, destination):
    member = tarfile.data_filter(member, destination)
    # The Docker copies are root-owned. Tracked directories in the build
    # context must remain traversable by the image's unprivileged user.
    return member.replace(mode=0o755) if member.isdir() else member


class ComposeUpdater:
    def __init__(self, args, git_dir):
        self.repository = args.repository
        self.compose = ["docker", "compose", "--project-directory", args.project_directory]
        for path in args.compose_file:
            self.compose.extend(["--file", path])
        self.service = args.service
        self.image = args.image
        self.rollback_image = args.image + "-previous"
        self.pending = git_dir / "listening-updater.pending"
        self.target = args.target
        self.lock = threading.Lock()
        self.source_lock = threading.Lock()
        self.current = self.remote = self.applying = self.error = ""
        self.checked_at = 0

    def git(self, *args, **options):
        return run(["git", "-C", self.repository, *args], **options)

    def container(self):
        container = run([*self.compose, "ps", "-a", "-q", self.service])
        if len(container.splitlines()) != 1:
            raise UpdateError("Expected one existing Listening container.")
        return container

    def installed(self):
        container = self.container()
        image = run(["docker", "inspect", "--format", "{{.Image}}", container])
        revision = run(["docker", "image", "inspect", "--format",
                        '{{index .Config.Labels "' + REVISION_LABEL + '"}}', image])
        return image, revision if REVISION.fullmatch(revision) else ""

    def source_state(self):
        try:
            image, current = self.installed()
            self.git("fetch", "--quiet", "--no-tags", "origin", "refs/heads/main")
            remote = self.git("rev-parse", "FETCH_HEAD")
            if not REVISION.fullmatch(remote):
                raise UpdateError("The source repository returned an invalid revision.")
            if current and current != remote:
                try:
                    self.git("merge-base", "--is-ancestor", current, remote)
                except subprocess.CalledProcessError as error:
                    raise UpdateError("The installed revision cannot fast-forward to main.") from error
        except (OSError, subprocess.SubprocessError) as error:
            raise UpdateError("Could not check main from the Compose host.") from error
        return image, current, remote

    def check(self):
        with self.source_lock:
            with self.lock:
                if self.applying:
                    return self.status()
            _, current, remote = self.source_state()
            with self.lock:
                if not self.applying:
                    self.current, self.remote = current, remote
                    self.checked_at = time.time()
                return self.status()

    def status(self):
        return {"current": self.current, "remote": self.remote,
                "behind": int(self.current != self.remote),
                "checked_at": self.checked_at, "applying": self.applying,
                "update_error": self.error}

    def apply(self, revision):
        if not isinstance(revision, str) or not REVISION.fullmatch(revision):
            raise UpdateError("Check for updates before choosing a revision.")
        if self.applying:
            if revision != self.applying:
                raise UpdateError("An update is already running.")
        else:
            if revision != self.remote:
                raise UpdateError("Main has changed. Check for updates again.")
            if revision == self.current:
                return {"status": "You are on the latest revision.", "updating_to": ""}
            self.applying, self.error = revision, ""
            threading.Thread(target=self.deploy, args=(revision,), daemon=True).start()
        return {"status": f"Building {revision[:7]} on the server. Listening will restart after the build.",
                "updating_to": revision}

    def running_storage(self):
        mounts = json.loads(run(["docker", "inspect", "--format",
                                 "{{json .Mounts}}", self.container()]))
        return {mount["Destination"]: [
            mount["Type"],
            mount["Name"] if mount["Type"] == "volume" else os.path.realpath(mount["Source"]),
            not mount["RW"],
        ] for mount in mounts if mount["Destination"] in PERSISTENT_MOUNTS}

    def verify_storage(self, expected):
        config = json.loads(run([*self.compose, "config", "--format", "json"]))
        service = config["services"][self.service]
        mounts = {}
        for mount in service.get("volumes", []):
            destination, kind = mount["target"], mount["type"]
            if destination in PERSISTENT_MOUNTS and kind in {"bind", "volume"} and mount.get("source"):
                source = (os.path.realpath(mount["source"]) if kind == "bind"
                          else config["volumes"][mount["source"]]["name"])
                mounts[destination] = [kind, source, mount.get("read_only", False)]
        if ENGINE_MOUNTS.intersection(expected):
            env = service.get("environment", {})
            if (env.get("LISTENING_ENGINE_SOCKET") != "/run/listening-engines/engine.sock"
                    or env.get("LISTENING_ENGINE_STAGING") != "/engine-staging"):
                raise UpdateError("The Compose engine socket and staging settings differ from the installed room.")
        if service.get("image") != self.image or not DATA_MOUNTS.issubset(mounts) or mounts != expected:
            raise UpdateError("The Compose image tag or data mounts differ from the deployment configuration.")

    def recreate(self, expected_image, storage):
        self.verify_storage(storage)
        run([*self.compose, "up", "-d", "--no-deps", "--no-build", "--pull", "never", "--wait",
             "--wait-timeout", "120", self.service], timeout=150)
        if self.installed()[0] != expected_image or self.running_storage() != storage:
            raise UpdateError("Compose did not start the expected image with its existing data mounts.")
        deadline = time.monotonic() + 20
        while True:
            try:
                run([*self.compose, "exec", "-T", self.service, "/app/listening",
                     "server", "status"], timeout=5)
                return
            except (OSError, subprocess.SubprocessError):
                if time.monotonic() >= deadline:
                    raise UpdateError("Listening did not answer after restart.") from None
                time.sleep(1)

    def recover_pending(self):
        if not self.pending.is_file():
            return
        pending = json.loads(self.pending.read_text())
        image = pending["image"]
        if not IMAGE_ID.fullmatch(image):
            raise UpdateError("The saved recovery image ID is invalid.")
        run(["docker", "image", "tag", image, self.image])
        self.recreate(image, pending["storage"])
        self.pending.unlink()

    def drain(self, action, *arguments):
        return subprocess.run([*self.compose, "exec", "-T", self.service, "python3",
                               "/app/scripts/update_drain.py", action, *arguments], check=False,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=12).returncode

    def deploy(self, revision, *, stopped_room=False):
        error = "The previous update could not be recovered. Check the Compose host updater."
        claimed = False
        try:
            with self.source_lock:
                self.recover_pending()
                error = "The server could not prepare the update. The current room is still running."
                previous_image, current, remote = self.source_state()
            if revision != remote:
                with self.lock:
                    self.current, self.remote = current, remote
                error = "Main changed before the build started. Check for updates again."
                raise UpdateError(error)
            storage = self.running_storage()
            try:
                self.verify_storage(storage)
            except UpdateError as failure:
                error = str(failure)
                raise
            with tempfile.TemporaryDirectory(prefix="listening-build-") as work:
                source = Path(work) / "source"
                source.mkdir()
                with tempfile.TemporaryFile() as archive:
                    self.git("archive", revision, output=archive)
                    archive.seek(0)
                    with tarfile.open(fileobj=archive) as tracked:
                        tracked.extractall(source, filter=source_member)
                with (source / "pyproject.toml").open("rb") as metadata:
                    candidate_version = tomllib.load(metadata)["tool"]["poetry"]["version"]
                iid = Path(work) / "image-id"
                run(["docker", "build", "--target", self.target,
                     "--build-arg", f"LISTENING_ROOM_REVISION={revision}",
                     "--iidfile", str(iid), str(source)], timeout=1200)
                candidate_image = iid.read_text().strip()
                if not IMAGE_ID.fullmatch(candidate_image) or not IMAGE_ID.fullmatch(previous_image):
                    raise UpdateError("Docker returned an invalid image ID.")
                managed = bool(ENGINE_MOUNTS.intersection(storage))
                if managed and (stopped_room or self.drain("engines", candidate_version)):
                    error = "The selected engines are unavailable for the candidate release. Update deferred."
                    raise UpdateError(error)
                if stopped_room:
                    running = run(["docker", "inspect", "--format", "{{.State.Running}}", self.container()])
                    if running != "false":
                        error = "Stop the verified idle room before using the one-time upgrade command."
                        raise UpdateError(error)
                else:
                    drain = self.drain("claim")
                    if drain:
                        error = ("Update waiting for idle." if drain == os.EX_TEMPFAIL else
                                 "The running server cannot confirm idle cutover. See the one-time upgrade guide.")
                        raise UpdateError(error)
                    claimed = True
                run(["docker", "image", "tag", previous_image, self.rollback_image])
                staged = self.pending.with_suffix(".tmp")
                staged.write_text(json.dumps({"image": previous_image, "storage": storage}) + "\n")
                staged.replace(self.pending)
                error = "The update could not start and the previous image could not be restored."
            try:
                run(["docker", "image", "tag", candidate_image, self.image])
                self.recreate(candidate_image, storage)
                if self.installed()[1] != revision:
                    raise UpdateError("The running image does not identify the requested revision.")
                if managed and self.drain("engines", candidate_version):
                    raise UpdateError("The selected engines are unavailable after restart.")
            except (OSError, subprocess.SubprocessError, UpdateError):
                self.recover_pending()
                error = "The update could not start. The previous image was restored."
                raise
            self.pending.unlink()
        except Exception as failure:
            print(f"Listening update {revision[:7]} failed: {failure}", flush=True)
            with self.lock:
                self.error, self.applying = error, ""
        else:
            print(f"Listening update {revision[:7]} installed", flush=True)
            with self.lock:
                self.current, self.applying, self.error = revision, "", ""

        finally:
            if claimed:
                try:
                    self.drain("release")
                except (OSError, subprocess.SubprocessError):
                    # The old session's lease expires if it is still running.
                    pass

    def handle(self, request):
        if request == {"action": "check"}:
            return self.check()
        with self.lock:
            if request == {"action": "status"}:
                return self.status()
            if isinstance(request, dict) and request.keys() == {"action", "revision"} and request["action"] == "apply":
                return self.apply(request["revision"])
            raise UpdateError("Unknown update request.")


class UpdateSocket(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class Handler(socketserver.StreamRequestHandler):
    timeout = 3

    def handle(self):
        try:
            line = self.rfile.readline(1024)
            if not line.endswith(b"\n"):
                return
            response = self.server.updater.handle(json.loads(line))
        except UpdateError as error:
            response = {"error": str(error)}
        except (ValueError, OSError):
            return
        try:
            self.wfile.write(json.dumps(response).encode() + b"\n")
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True, help="host Git clone with a read-only origin")
    parser.add_argument("--project-directory", required=True)
    parser.add_argument("--compose-file", action="append", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--image", required=True, help="local image tag used by the Compose service")
    parser.add_argument("--target", choices=("core", "full"), default="core")
    parser.add_argument("--from-stopped-room", action="store_true",
                        help="one-time upgrade of a manually stopped legacy room")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("The host updater requires Linux Docker Engine.")
    try:
        directory = args.socket.parent.lstat()
    except OSError:
        parser.error("Create the private socket directory before starting the host updater.")
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or directory.st_mode & 0o077:
        parser.error("The socket directory must belong to this user and have mode 0700.")
    git_dir = Path(run(["git", "-C", args.repository, "rev-parse", "--absolute-git-dir"]))
    fd = os.open(git_dir / "listening-updater.lock", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if args.from_stopped_room:
                parser.error("Stop the updater service before the one-time upgrade.")
            return
        if args.from_stopped_room:
            updater = ComposeUpdater(args, git_dir)
            if updater.pending.exists():
                parser.error("Resolve the previous update before the one-time upgrade.")
            _, current, remote = updater.source_state()
            if current == remote:
                parser.error("The configured image already matches main. Start it with Compose.")
            updater.deploy(remote, stopped_room=True)
            if updater.error:
                parser.exit(1, updater.error + "\n")
            return
        try:
            socket_file = args.socket.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(socket_file.st_mode) or socket_file.st_uid != os.getuid():
                parser.error("The socket path already exists and is not this user's Unix socket.")
            args.socket.unlink()
        os.umask(0o077)
        updater = ComposeUpdater(args, git_dir)
        updater.recover_pending()
        try:
            with UpdateSocket(str(args.socket), Handler) as server:
                args.socket.chmod(0o600)
                server.updater = updater
                server.serve_forever()
        finally:
            args.socket.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
