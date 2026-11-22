#!/usr/bin/env python3
"""Provision the pinned Sockseek binary into the application folder
(plan 044).

Sockseek is the download adapter, and installing it used to be a
manual errand: download the executable from the releases page, make
it executable, note the absolute path, type the path into Settings.
The package routes run this script instead. It downloads the one
pinned release from `packaging/sockseek-pin.json`, verifies the
archive's SHA-256 against the pin before extracting anything, and
installs the executable into the managed bin, `<application
folder>/bin`, where the app's default resolution
(`backend/system/config.py`) finds it on a fresh install.

Idempotent: a marker file beside the binary records the pinned
version it installed, and a run that finds the marker at the pinned
version with an executable binary downloads nothing. `--force`
re-provisions (the repair path for a damaged binary). The target
directory is resolved through the app's own resolver, `AppConfig`,
so the `LISTENING_ROOM_CONFIG` override is honored rather than
reimplemented; `--app-dir` overrides it outright (the Docker build
and fixtures). `--pin` and `--platform-key` exist for the same
callers: the Docker fetch stage maps the build's TARGETARCH to a
pin key explicitly, and fixtures pin against a local mirror.

Standard library only, like everything else here. Scripts carry no
unit tests (owner's tooling rule); this one verifies itself with
the checksum refusal and the marker, and is exercised by the
fixture smokes named in plan 044.
"""

import argparse
import hashlib
import json
import os
import platform
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

TREE = Path(__file__).resolve().parent.parent
DEFAULT_PIN = TREE / "packaging" / "sockseek-pin.json"
EXECUTABLE = "sockseek"
MARKER = ".sockseek-version"
SUPPORTED_KEYS = ("osx-arm64", "osx-x64", "linux-x64", "linux-arm")
USER_AGENT = "ListeningRoom/1.2 (sockseek provisioner)"


class ProvisionError(RuntimeError):
    """A refusal whose message is shown to the person installing."""


def platform_key(system=None, machine=None):
    """The pin key for this machine, or None when unmapped.

    Windows is unmapped on purpose: native Windows is unsupported
    (WSL2 is the Windows route), and the Docker route serves it
    with the Linux asset.
    """
    system = (system or platform.system()).casefold()
    machine = (machine or platform.machine()).casefold()
    if system == "darwin":
        return "osx-arm64" if machine in {"arm64", "aarch64"} else "osx-x64"
    if system == "linux":
        if machine in {"x86_64", "amd64"}:
            return "linux-x64"
        # The one ARM Linux asset is a 32-bit armhf build (verified
        # against the pinned release at plan 044's execution: ELF
        # 32-bit ARM, /lib/ld-linux-armhf.so.3). It is the right
        # binary for a 32-bit ARM system and cannot run on a 64-bit
        # one, so aarch64 maps to no key and refuses loudly below
        # rather than installing a binary that would not execute.
        if machine.startswith("arm") and machine not in {"aarch64", "arm64"}:
            return "linux-arm"
    return None


def resolve_app_dir(override):
    """The application folder: the override, or AppConfig's answer.

    The resolver import is lazy and only happens without an
    override, so the Docker fetch stage can run this script with
    nothing but the pin table beside it.
    """
    if override:
        return Path(override).expanduser()
    sys.path.insert(0, str(TREE / "backend"))
    from system.config import AppConfig
    return AppConfig.from_environment(TREE).app_dir


def load_pin(path):
    try:
        pin = json.loads(Path(path).read_text(encoding="utf-8"))
        version = str(pin["version"])
        assets = pin["assets"]
        if not isinstance(assets, dict) or not assets:
            raise KeyError("assets")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ProvisionError(f"Could not read the Sockseek pin at {path}: {error}") from error
    return version, assets


def download(url, target):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response, \
            open(target, "wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)


def extract_executable(archive_path, target):
    """Write the archive's `sockseek` member to `target`, atomically.

    The member is matched by base name (an archive may nest it in a
    folder); anything else in the archive is ignored. The binary
    lands beside its destination and is renamed over it, so a
    failed extraction never leaves a partial binary behind.
    """
    with tarfile.open(archive_path, "r:gz") as archive:
        member = next((item for item in archive.getmembers()
                       if item.isfile()
                       and Path(item.name).name == EXECUTABLE), None)
        if member is None:
            names = ", ".join(item.name for item in archive.getmembers()[:10])
            raise ProvisionError(
                f"The downloaded archive has no executable named "
                f"{EXECUTABLE} (it contains: {names}). The pinned "
                f"release's shape has changed; refusing to guess.")
        source = archive.extractfile(member)
        if source is None:
            raise ProvisionError(f"Could not read {member.name} from the archive.")
        fd, temporary = tempfile.mkstemp(dir=str(target.parent),
                                         prefix=".sockseek-")
        try:
            with os.fdopen(fd, "wb") as handle:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            os.chmod(temporary, 0o755)
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def provision(app_dir, pin_path, key, force):
    version, assets = load_pin(pin_path)
    if key is None:
        key = platform_key()
    if key not in assets:
        raise ProvisionError(
            f"No pinned Sockseek for this platform "
            f"({platform.system()} / {platform.machine()}). Supported: "
            f"{', '.join(SUPPORTED_KEYS)}. Two shapes have no upstream "
            f"build to install: 64-bit ARM Linux (the linux-arm asset "
            f"is a 32-bit armhf build) and Windows (use the Docker "
            f"route, on amd64, instead).")
    asset = assets[key]
    bin_dir = Path(app_dir) / "bin"
    binary = bin_dir / EXECUTABLE
    marker = bin_dir / MARKER
    if (not force and binary.is_file() and os.access(binary, os.X_OK)
            and marker.is_file()
            and marker.read_text(encoding="utf-8").strip() == version):
        print(f"Sockseek {version} is already provisioned at {binary}; "
              f"nothing downloaded.")
        return binary
    bin_dir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {asset['file']} (Sockseek {version}, {key})...")
    with tempfile.TemporaryDirectory(dir=str(bin_dir)) as staging:
        archive_path = Path(staging) / asset["file"]
        try:
            download(asset["url"], archive_path)
        except OSError as error:
            raise ProvisionError(
                f"Could not download {asset['url']}: {error}") from error
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        if digest != asset["sha256"]:
            raise ProvisionError(
                f"Checksum mismatch for {asset['file']}; refusing to "
                f"install it.\n  pinned:   {asset['sha256']}\n"
                f"  received: {digest}")
        print(f"SHA-256 verified: {digest}")
        extract_executable(archive_path, binary)
    marker.write_text(version + "\n", encoding="utf-8")
    print(f"Installed Sockseek {version} at {binary}")
    return binary


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Download and install the pinned Sockseek binary "
                    "into the application folder's bin/.")
    parser.add_argument("--app-dir", default=None,
                        help="application folder to provision into "
                             "(default: the app's own resolution)")
    parser.add_argument("--pin", default=str(DEFAULT_PIN),
                        help="pin table to read (default: the tree's "
                             "packaging/sockseek-pin.json)")
    parser.add_argument("--platform-key", default=None,
                        help="pin key to install for, instead of this "
                             "machine's (the Docker build's TARGETARCH "
                             "mapping; fixtures)")
    parser.add_argument("--force", action="store_true",
                        help="re-download and reinstall even when the "
                             "pinned version is already provisioned")
    args = parser.parse_args(argv)
    try:
        app_dir = resolve_app_dir(args.app_dir)
        provision(app_dir, Path(args.pin), args.platform_key, args.force)
    except ProvisionError as error:
        print(f"provision_sockseek: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
