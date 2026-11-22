"""Serve release-matched client installers staged by the room operator."""

from contextlib import ExitStack, contextmanager
import hashlib
from http import HTTPStatus
import json
import os
from pathlib import Path
import re
import shutil
import stat

from system.private_files import private_write


ROUTE = "/api/setup/clients"
MANIFEST = "client-installers.json"
PLATFORMS = {
    ".apk": ("android", "Android", "Signed APK"),
    ".dmg": ("macos", "Apple silicon", "Ad-hoc signed DMG"),
}


def names(version):
    return {f"listening-room-{version}{extension}": facts
            for extension, facts in PLATFORMS.items()}


def release_version(root):
    match = re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(), re.M)
    return match.group(1)


@contextmanager
def open_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("An installer must be a regular file.")
        yield handle, info.st_size


def entries(directory, version):
    with open_regular(directory / MANIFEST) as (handle, size):
        if size > 16_384:
            raise ValueError("Installer manifest is too large.")
        data = json.load(handle)
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("version") != version:
        raise ValueError("Incorrect installer release.")
    entries = data.get("installers")
    if not isinstance(entries, dict):
        raise ValueError("Invalid installer manifest.")
    return entries


@contextmanager
def verified_file(directory, name, record):
    if not isinstance(record, dict):
        raise ValueError("No installer record was staged.")
    with open_regular(directory / name) as (handle, size):
        if size == 0 or size != record.get("size") or digest(handle) != record.get("sha256"):
            raise ValueError("Installer checksum or size mismatch.")
        handle.seek(0)
        yield handle, size


def digest(handle):
    hashed = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        hashed.update(chunk)
    return hashed.hexdigest()


def build_manifest(directory, version):
    installers = {}
    for name in names(version):
        with open_regular(directory / name) as (handle, size):
            if not size:
                raise ValueError(f"{name} is empty.")
            installers[name] = {"size": size, "sha256": digest(handle)}
    private_write(directory / MANIFEST, json.dumps({"schema": 1, "version": version, "installers": installers}, indent=2))


def status(directory, version, origin):
    try:
        manifest = entries(directory, version)
    except (OSError, ValueError):
        manifest = {}
    installers = []
    for name, (platform, architecture, signing) in names(version).items():
        record = manifest.get(name)
        details = {"name": name, "platform": platform, "architecture": architecture, "signing": signing}
        try:
            with verified_file(directory, name, record):
                details.update(size=record["size"], sha256=record["sha256"], url=f"{ROUTE}/{name}")
        except (OSError, ValueError):
            details["repair"] = f"Stage and verify the {version} installers on the room server."
        installers.append(details)
    return {"version": version, "origin": origin, "installers": installers}


def is_route(path):
    return path == ROUTE or path.startswith(ROUTE + "/")


def serve(handler, path, origin, root, config_dir):
    directory = Path(os.environ.get("LISTENING_ROOM_CLIENT_ARTIFACTS")
                     or config_dir / "clients")
    version = release_version(root)
    if path == ROUTE:
        return handler.json_response(status(directory, version, origin))
    name = path.removeprefix(ROUTE + "/")
    if name not in names(version):
        return handler.error_json("Installer not found.", HTTPStatus.NOT_FOUND)
    with ExitStack() as stack:
        try:
            record = entries(directory, version).get(name)
            handle, size = stack.enter_context(verified_file(directory, name, record))
        except (OSError, ValueError):
            return handler.error_json("Installer unavailable. Verify the release files on the room server.", HTTPStatus.NOT_FOUND)
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "application/octet-stream")
        handler.send_header("Content-Length", str(size))
        handler.send_header("Content-Disposition", f'attachment; filename="{name}"')
        handler.send_header("Cache-Control", "private, no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        try:
            shutil.copyfileobj(handle, handler.wfile)
        except OSError:
            handler.close_connection = True
