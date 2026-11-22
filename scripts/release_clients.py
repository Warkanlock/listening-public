"""Build signed client distributables and publish the checked files on GitHub."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import tomllib


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from system.client_artifacts import MANIFEST, build_manifest, status


def read(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def run(*args):
    subprocess.run(args, cwd=ROOT, check=True)


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def release_version():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["poetry"]["version"]
    expected = json.loads((ROOT / "frontend/react/package.json").read_text())["version"]
    android = (ROOT / "clients/android/app/build.gradle").read_text()
    apple = (ROOT / "clients/ipad/ListeningRoom.xcodeproj/project.pbxproj").read_text()
    if (not re.fullmatch(r"\d+\.\d+\.\d+", version) or expected != version
            or re.findall(r"versionName '([^']+)'", android) != [version]
            or set(re.findall(r"MARKETING_VERSION = ([^;]+);", apple)) != {version}):
        raise ValueError("The client and server versions must match.")
    return version


def artifacts(output, version):
    return [output / f"listening-room-{version}{suffix}"
            for suffix in (".apk", ".dmg")]


def verify(files):
    for file in files:
        checksum = file.with_name(file.name + ".sha256").read_text().split()
        if checksum != [digest(file), file.name]:
            raise ValueError(f"Checksum mismatch for {file.name}.")


def build(output, version, commit):
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="listening-release-source-") as directory:
        temp = Path(directory)
        archive = temp / "source.tar"
        source = temp / "source"
        source.mkdir()
        run("git", "archive", "--format=tar", "--output", archive, commit)
        with tarfile.open(archive, "r:") as committed:
            committed.extractall(source, filter="data")
        run(source / "listening", "build", "apk", "--source", source, "--output", output)
        run(source / "listening", "build", "dmg", "--source", source, "--output", output)
    files = artifacts(output, version)
    verify(files)
    (output / "SHA256SUMS").write_text("".join(f"{digest(file)}  {file.name}\n" for file in files))
    (output / "release-source.txt").write_text(commit + "\n")
    build_manifest(output, version)
    print(f"Verified release files in {output}", flush=True)


def origin_repo():
    url = read("git", "remote", "get-url", "origin")
    match = re.fullmatch(r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
                         r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", url)
    if match is None:
        raise ValueError("The origin must point to a GitHub repository.")
    return f"github.com/{match[1]}"


def tag_commit(tag):
    ref = f"refs/tags/{tag}"
    peeled = ref + "^{}"
    entries = {name: sha for line in read("git", "ls-remote", "origin", ref, peeled).splitlines()
               for sha, name in [line.split()]}
    return entries.get(peeled, entries.get(ref))


def publish(output, version, commit, notes):
    if (output / "release-source.txt").read_text().strip() != commit:
        raise ValueError("Build the artifacts from this commit before publishing.")
    files = artifacts(output, version)
    verify(files)
    sums = "".join(f"{digest(file)}  {file.name}\n" for file in files)
    if (output / "SHA256SUMS").read_text() != sums:
        raise ValueError("The release checksums do not match the artifacts.")
    if not all("url" in asset for asset in status(output, version, "")["installers"]):
        raise ValueError("The client installer manifest does not match the artifacts.")
    remote = read("git", "ls-remote", "origin", "refs/heads/main").split()
    if not remote or remote[0] != commit:
        raise ValueError("Push the verified source to main before publishing.")
    tag = "v" + version
    repo = origin_repo()
    target = tag_commit(tag)
    if target is None:
        run("git", "push", "origin", f"{commit}:refs/tags/{tag}")
    if tag_commit(tag) != commit:
        raise ValueError(f"The GitHub tag {tag} points at another commit.")
    uploads = [*files, output / MANIFEST, output / "SHA256SUMS", output / "release-source.txt"]
    run("gh", "release", "create", tag, "--repo", repo, "--verify-tag", "--target", commit,
        "--title", f"Listening Room {version}",
        "--notes-file", notes, "--draft", *uploads)
    result = json.loads(read("gh", "release", "view", tag, "--repo", repo, "--json", "assets,isDraft"))
    received = {asset["name"]: asset.get("digest") for asset in result["assets"]}
    expected = {file.name: f"sha256:{digest(file)}" for file in uploads}
    if not result["isDraft"] or tag_commit(tag) != commit or received != expected:
        raise ValueError("The draft release assets or commit do not match the verified build.")
    run("gh", "release", "edit", tag, "--repo", repo, "--draft=false")
    if tag_commit(tag) != commit:
        raise ValueError(f"The tag {tag} changed during publication.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/clients/release")
    parser.add_argument("--publish", type=Path, metavar="RELEASE_NOTES", help="Publish previously built artifacts")
    args = parser.parse_args()
    try:
        if read("git", "status", "--porcelain", "--untracked-files=no"):
            raise ValueError("Commit tracked changes before building or publishing a release.")
        version = release_version()
        commit = read("git", "rev-parse", "HEAD")
        if args.publish:
            publish(args.output.resolve(), version, commit, args.publish.resolve())
        else:
            build(args.output.resolve(), version, commit)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Client release failed: {error}\n")
