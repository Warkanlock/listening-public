#!/usr/bin/env python3
"""Build and verify a personal Apple silicon disk image through listening build dmg."""

import argparse
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAC = ROOT / "clients/macos"
sys.path.insert(0, str(MAC))
from runtime import add_runtime, digest


def check_tools():
    if (platform.system(), platform.machine()) != ("Darwin", "arm64"):
        raise ValueError("Build the DMG on an Apple silicon Mac.")
    for tool in ("xcrun", "codesign", "hdiutil", "yarn", "node", "make", "pkg-config"):
        if not shutil.which(tool):
            raise ValueError(f"Missing Mac build tool: {tool}")
    subprocess.run(["xcrun", "--find", "swiftc"], stdout=subprocess.DEVNULL, check=True)
    node = tuple(map(int, subprocess.check_output(["node", "-p", "process.versions.node"],
                                                  text=True).strip().split(".")))
    if node < (20, 19, 0) or (node[0] == 21) or ((22, 0, 0) <= node < (22, 12, 0)):
        raise ValueError("Select Node 20.19+ or Node 22.12+ for the frontend build.")


def copy_room(resources):
    room = resources / "room"
    # Explicitly copy application code/data only. Rooms and host settings
    # may live beside a checkout and must never make it into an installer.
    for path in sorted((ROOT / "backend").rglob("*")):
        is_runtime = path.suffix == ".py" or path == ROOT / "backend/catalog/genre_aliases.json"
        if is_runtime and path.is_file() and not path.is_symlink():
            dest = room / path.relative_to(ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
    shutil.copy2(ROOT / "pyproject.toml", room)
    shutil.copytree(ROOT / "frontend/dist", room / "frontend/dist")
    shutil.copy2(MAC / "listening", resources / "bin/listening")
    notices = resources / "Notices"
    shutil.copy2(ROOT / "LICENSE", notices / "Listening-Room-LICENSE")
    shutil.copy2(ROOT / "THIRD_PARTY.md", notices)
    modules = ROOT / "frontend/react/node_modules"
    for name in ("react", "react-dom", "scheduler", "@ui-sdk/react", "@ui-sdk/core"):
        shutil.copy2(modules / name / "LICENSE", notices / (name.replace("/", "-") + "-LICENSE"))


def make_app(app, version):
    contents = app / "Contents"
    binary = contents / "MacOS/ListeningRoom"
    binary.parent.mkdir(parents=True)
    resources = contents / "Resources"
    resources.mkdir()
    info = {
        "CFBundleName": "Listening Room", "CFBundleDisplayName": "Listening Room",
        "CFBundleIdentifier": "com.warkanlock.listeningroom.client",
        "CFBundleExecutable": "ListeningRoom", "CFBundlePackageType": "APPL",
        "CFBundleIconFile": "Room.icns",
        "CFBundleVersion": version, "CFBundleShortVersionString": version,
        "LSMinimumSystemVersion": "13.0", "NSPrincipalClass": "NSApplication",
        "NSHighResolutionCapable": True, "LSApplicationCategoryType": "public.app-category.music",
        "NSLocalNetworkUsageDescription": "Listening Room connects to the music server you pair with.",
        "NSAppTransportSecurity": {"NSAllowsArbitraryLoadsInWebContent": True,
                                   "NSAllowsLocalNetworking": True},
    }
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    subprocess.run(["xcrun", "swiftc", "-O", "-target", "arm64-apple-macos13",
                    MAC / "main.swift", MAC.parent / "apple/RoomAddress.swift", "-o", binary], check=True)
    subprocess.run(["/usr/bin/sips", "-s", "format", "icns",
                    ROOT / "frontend/react/public/assets/icons/icon-512.png",
                    "--out", resources / "Room.icns"], check=True, stdout=subprocess.DEVNULL)
    add_runtime(resources)
    copy_room(resources)
    subprocess.run(["codesign", "--force", "--sign", "-", app], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", app], check=True)


def verify_volume(image, mount, version):
    subprocess.run(["hdiutil", "verify", image], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mount, image],
                   check=True, stdout=subprocess.DEVNULL)
    try:
        app = mount / "Listening Room.app"
        resources = app / "Contents/Resources"
        subprocess.run(["codesign", "--verify", "--deep", "--strict", app], check=True)
        # Isolate even help/version checks from the builder's settings.
        with tempfile.TemporaryDirectory() as temporary:
            env = dict(PATH="/usr/bin:/bin", HOME=temporary, TMPDIR=temporary,
                       LISTENING_ROOM_CONFIG=str(Path(temporary) / "unused/config.json"))
            found = subprocess.check_output([resources / "bin/listening", "--version"],
                                            env=env, text=True).strip()
            if found != version:
                raise ValueError(f"Bundled command reports {found}, expected {version}")
            for tool in ("ffmpeg", "ffprobe"):
                subprocess.run([resources / "bin" / tool, "-version"], env=env,
                               stdout=subprocess.DEVNULL, check=True)
            for extension, encoder in (("opus", "libopus"), ("m4a", "aac")):
                output = Path(temporary) / f"tone.{extension}"
                subprocess.run([resources / "bin/ffmpeg", "-v", "error", "-f", "lavfi",
                                "-i", "sine=440:d=0.25", "-c:a", encoder, output],
                               env=env, check=True)
                subprocess.run([resources / "bin/ffprobe", "-v", "error", output],
                               env=env, check=True)
    finally:
        # macOS may still be releasing handles to tools run from this
        # temporary read-only volume after their processes have exited.
        detached = subprocess.run(["hdiutil", "detach", mount], stdout=subprocess.DEVNULL)
        if detached.returncode:
            subprocess.run(["hdiutil", "detach", "-force", mount], check=True, stdout=subprocess.DEVNULL)


def build(output):
    check_tools()
    frontend = ROOT / "frontend/react"
    subprocess.run(["yarn", "install", "--frozen-lockfile", "--non-interactive"], cwd=frontend, check=True)
    subprocess.run(["yarn", "build"], cwd=frontend, check=True)
    version = json.loads((frontend / "package.json").read_text())["version"]
    name = f"listening-room-{version}.dmg"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".listening-dmg-", dir=output) as temporary:
        stage = Path(temporary)
        volume = stage / "volume"
        make_app(volume / "Listening Room.app", version)
        (volume / "Applications").symlink_to("/Applications")
        image = stage / name
        subprocess.run(["hdiutil", "create", "-volname", "Listening Room", "-srcfolder", volume,
                        "-format", "UDZO", "-fs", "HFS+", image], check=True)
        mount = stage / "mounted"
        mount.mkdir()
        verify_volume(image, mount, version)
        checksum = stage / (name + ".sha256")
        checksum.write_text(f"{digest(image)}  {name}\n")
        os.replace(image, output / name)
        os.replace(checksum, output / checksum.name)
    print(f"Verified {output / name}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    try:
        build(parser.parse_args().output.resolve())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"DMG build failed: {error}")
