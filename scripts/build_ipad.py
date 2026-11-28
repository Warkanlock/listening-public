#!/usr/bin/env python3
"""Build a universal iOS Simulator app or export a development IPA for registered devices."""

import argparse
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1] / "clients/ipad/Listening.xcodeproj"


def build(output):
    if sys.platform != "darwin" or not shutil.which("xcodebuild"):
        raise ValueError("Build the iPhone and iPad client on a Mac with Xcode and its iOS platform installed.")
    output.mkdir(parents=True, exist_ok=True)
    team = os.environ.get("LISTENING_APPLE_TEAM", "").strip()
    with tempfile.TemporaryDirectory(dir=output, prefix=".listening-ipad-") as temporary:
        work = Path(temporary)
        xcode = ["xcodebuild", "-quiet", "-project", str(PROJECT), "-scheme", "Listening",
                 "-configuration", "Release", "-derivedDataPath", str(work / "derived")]
        if team:
            archive = work / "Listening.xcarchive"
            subprocess.run([*xcode, "-destination", "generic/platform=iOS", "-archivePath", archive,
                            f"DEVELOPMENT_TEAM={team}", "-allowProvisioningUpdates", "archive"], check=True)
            options = work / "Export.plist"
            options.write_bytes(plistlib.dumps({"method": "debugging", "teamID": team,
                                                "signingStyle": "automatic"}))
            export = work / "export"
            subprocess.run(["xcodebuild", "-quiet", "-exportArchive", "-archivePath", archive,
                            "-exportPath", export, "-exportOptionsPlist", options,
                            "-allowProvisioningUpdates"], check=True)
            artifact, = export.glob("*.ipa")
            app = archive / "Products/Applications/Listening.app"
            suffix = "ios.ipa"
        else:
            subprocess.run([*xcode, "-destination", "generic/platform=iOS Simulator", "build"], check=True)
            app = work / "derived/Build/Products/Release-iphonesimulator/Listening.app"
            artifact = work / "simulator.zip"
            subprocess.run(["ditto", "-c", "-k", "--keepParent", app, artifact], check=True)
            suffix = "ios-simulator.zip"
        info = plistlib.loads((app / "Info.plist").read_bytes())
        if sorted(info["UIDeviceFamily"]) != [1, 2] or not (app / info["CFBundleExecutable"]).is_file():
            raise ValueError("The built app is missing its executable or iPhone and iPad support.")
        if "audio" not in info.get("UIBackgroundModes", []):
            raise ValueError("The built app is missing background audio support.")
        version = info["CFBundleShortVersionString"]
        name = f"listening-room-{version}-{suffix}"
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        checksum = work / "checksum"
        checksum.write_text(f"{digest}  {name}\n")
        artifact.replace(output / name)
        checksum.replace(output / (name + ".sha256"))
    print(f"Built {output / name}")
    if not team:
        print("For a physical iPhone or iPad, open clients/ipad/Listening.xcodeproj in Xcode and select your signing team.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    try:
        build(parser.parse_args().output.resolve())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"iOS build failed: {error}")
