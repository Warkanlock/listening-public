#!/usr/bin/env python3
"""Start the operator-owned engine manager on a local Unix socket."""

import argparse
import fcntl
import json
import os
import platform
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from system.engine_fleet import Fleet, IDENTITY, LIMIT, WorkerDeadlineExceeded
from system.engine_repository import stage_repository


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5)
        try:
            raw = self.rfile.readline(LIMIT + 1)
            if len(raw) > LIMIT:
                raise ValueError("The engine request is too large.")
            request = json.loads(raw)
            if not isinstance(request, dict) or request.get("version") != 1:
                raise ValueError("Unsupported engine protocol.")
            if request.get("action") in ("search", "fetch"):
                if not isinstance(request.get("engine"), str) or not IDENTITY.fullmatch(request["engine"]):
                    raise ValueError("Choose an engine.")
                response = self.server.fleet.acquire(request, self.connection)
            else:
                response = self.server.fleet.manage(request, self.connection)
        except BlockingIOError:
            response = {"ok": False, "code": "busy", "retryable": True}
        except ConnectionError:
            response = {"ok": False, "error": "The worker is busy or cancelled.", "retryable": True}
        except WorkerDeadlineExceeded:
            response = {"ok": False, "code": "timeout", "retryable": True}
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            response = {"ok": False, "error": "The engine is unavailable. Check the host manager.", "retryable": True}
        except (KeyError, TypeError, ValueError):
            response = {"ok": False, "error": "The engine request or catalog is invalid.", "retryable": False}
        try:
            self.wfile.write(json.dumps(response).encode() + b"\n")
        except OSError:
            pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


@contextmanager
def private_lock(path):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("The engine manager lock must be an owner-only file.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("An engine manager is already running for this room or socket.") from error
        yield
    finally:
        os.close(descriptor)


def remove_stale_socket(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError("The engine socket path is occupied by another file.")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            path.unlink()
            return
    raise RuntimeError("An engine manager is already accepting requests at this socket.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("socket", "state", "staging", "public-key"):
        parser.add_argument(f"--{option}", required=True, type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--catalog", type=Path, help="path to a locally signed catalog")
    source.add_argument("--repository", help="HTTPS GitHub repository with signed catalog release assets")
    parser.add_argument("--plugin-release", help="catalog release tag (defaults to latest)")
    parser.add_argument("--server-version", required=True)
    args = parser.parse_args()
    if args.plugin_release and not args.repository:
        parser.error("--plugin-release requires --repository")
    if not os.getuid():
        parser.error("Run the engine manager as the Listening host operator with Docker access.")
    os.umask(0o077)
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    socket_lock = args.socket.with_name(args.socket.name + ".lock")
    try:
        with (private_lock(args.state / ".manager.lock"), private_lock(socket_lock),
              tempfile.TemporaryDirectory(prefix="listening-engine-repository-") as directory):
            remove_stale_socket(args.socket)
            key = args.public_key.resolve()
            catalog = (stage_repository(args.repository, args.plugin_release, Path(directory), args.state,
                                        key, arch, args.server_version)
                       if args.repository else args.catalog.resolve())
            fleet = Fleet(args.state, args.staging, catalog, key, arch, args.server_version)
            with Server(str(args.socket), Handler) as server:
                server.fleet = fleet
                args.socket.chmod(0o660)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    args.socket.unlink(missing_ok=True)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"Engine manager could not start: {error}\n")


if __name__ == "__main__":
    main()
