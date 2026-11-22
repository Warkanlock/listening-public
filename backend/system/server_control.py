"""Local lifecycle control. The HTTP listener never accepts control commands."""

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import stat


def control_path(config):
    # AF_UNIX paths have a small platform limit. Keep even long room paths usable.
    directory = Path("/tmp") / f"listening-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError(f"Private control directory required at {directory}")
    identity = hashlib.sha256(os.fsencode(config.resolve())).hexdigest()[:32]
    return directory / (identity + ".sock")


@contextlib.contextmanager
def lock_file(path, *, wait=True):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def request(path, command):
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(2)
        try:
            conn.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError):
            return None
        conn.sendall(command.encode("ascii") + b"\n")
        with conn.makefile("rb") as reply:
            return json.loads(reply.readline(4096))


@contextlib.contextmanager
def own_listener(config):
    path = control_path(config)
    try:
        with lock_file(path.with_suffix(".owner"), wait=False):
            path.unlink(missing_ok=True)
            try:
                yield path
            finally:
                path.unlink(missing_ok=True)
    except BlockingIOError as error:
        raise RuntimeError("This room already has a server process") from error


def serve(httpd, path, *, drain=None):
    host, port = httpd.server_address[:2]
    info = json.dumps({"host": host, "port": port, "pid": os.getpid()}).encode() + b"\n"
    running = True

    def stop(*_):
        nonlocal running
        running = False

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        with socket.socket(socket.AF_UNIX) as listener, selectors.DefaultSelector() as events:
            listener.bind(str(path))
            listener.listen()
            events.register(listener, selectors.EVENT_READ)
            events.register(httpd, selectors.EVENT_READ)
            while running:
                for key, _mask in events.select(timeout=0.3):
                    if key.fileobj is httpd:
                        httpd.handle_request()
                        continue
                    with listener.accept()[0] as conn:
                        conn.settimeout(1)
                        try:
                            with conn.makefile("rb") as incoming:
                                verb = incoming.readline(32).strip()
                            if drain is not None and verb in (b"claim", b"release"):
                                conn.sendall(json.dumps(drain(verb.decode("ascii"))).encode() + b"\n")
                            elif verb in (b"status", b"stop"):
                                conn.sendall(info)
                                if verb == b"stop":
                                    stop()
                        except OSError:
                            continue
    finally:
        signal.signal(signal.SIGTERM, previous)
