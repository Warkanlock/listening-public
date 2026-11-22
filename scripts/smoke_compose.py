#!/usr/bin/env python3
"""Exercise a local image with isolated Compose storage and generated audio."""

import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
USERNAME = "fixture-owner"
PASSWORD = "Disposable release smoke fixture 2026"
TRACK = "Compose fixture"
PLAYLIST = {"name": "fixture.csv", "content": f"Title,Artist,Album,Duration\n{TRACK},Fixture Artist,Fixture Album,2\n"}


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def wait_for(probe, message, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(0.3)
    raise RuntimeError(message)


class Room:
    def __init__(self, root, image, *, library=None, music=None, clients=None):
        for folder in (root / "config", root / "library"):
            folder.mkdir(parents=True)
            # The fixture may have been created by a different UID than Docker's.
            folder.chmod(0o777)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.origin = f"http://127.0.0.1:{port}"
        self.image = image
        self.command = ["docker", "compose", "--env-file", "/dev/null",
                        "--project-name", f"listening-smoke-{secrets.token_hex(7)}",
                        "--project-directory", str(root), "-f", str(ROOT / "docker-compose.yml")]
        if clients:
            self.command.extend(["-f", str(ROOT / "packaging/compose.clients.yaml")])
        self.env = dict(os.environ, LISTENING_CLIENT_DIR=str(clients or ""), LISTENING_IMAGE=image, LISTENING_IMAGE_TARGET="core", LISTENING_BIND="127.0.0.1",
                        LISTENING_PORT=str(port), LISTENING_ROOM_ORIGIN=self.origin,
                        LISTENING_LIBRARY_DIR=str(library or root / "library"), LISTENING_IMPORT_DIR=str(music or ""))

    def run(self, *args, importing=False, check=True, **kwargs):
        files = ["-f", str(ROOT / "packaging/compose.import.yaml")] if importing else []
        return subprocess.run([*self.command, *files, *args], env=self.env, check=check, timeout=90, **kwargs)

    def up(self, *, image=None, importing=False):
        self.env["LISTENING_IMAGE"] = image or self.image
        self.run("up", "-d", "--no-build", "--pull", "never", importing=importing)
        browser = self.browser()
        wait_for(lambda: browser.request("/api/status", statuses=(0, 200, 401))[0] != 0,
                 "Compose server did not answer HTTP")

    def browser(self):
        return Browser(self.origin)

    def set_owner(self):
        # The real owner command requires an interactive password prompt.
        code = """import json,sys
from system.owner_auth import OwnerAuth
from system.config import resolve_config_path
OwnerAuth(resolve_config_path().parent).set_password(*json.load(sys.stdin))
"""
        self.run("exec", "-T", "-e", "PYTHONPATH=/app/backend", "listening-room", "python3", "-c", code,
                 input=json.dumps([USERNAME, PASSWORD]), text=True)

    def legacy_api(self, path, body=None):
        code = """import json,sys,urllib.request
body = sys.stdin.buffer.read() or None
request = urllib.request.Request('http://127.0.0.1:8765' + sys.argv[1], body, {'Content-Type': 'application/json'})
sys.stdout.buffer.write(urllib.request.urlopen(request, timeout=15).read())
"""
        return self.run("exec", "-T", "listening-room", "python3", "-c", code, path,
                        input=json.dumps(body).encode() if body else b"", stdout=subprocess.PIPE).stdout

    def close(self):
        self.run("down", "--remove-orphans")


class Browser:
    def __init__(self, origin):
        self.origin = origin
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                  urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path, *, body=None, statuses=(200,)):
        request = urllib.request.Request(self.origin + path, None if body is None else json.dumps(body).encode(),
                                         {"Origin": self.origin, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=20) as reply:
                check(reply.status in statuses, f"{path.split('?')[0]} returned HTTP {reply.status}")
                return reply.status, reply.read()
        except urllib.error.HTTPError as error:
            check(error.code in statuses, f"{path.split('?')[0]} returned HTTP {error.code}")
            return error.code, error.read()
        except OSError:
            if 0 not in statuses:
                raise
            return 0, b""

    def json(self, path, body=None):
        return json.loads(self.request(path, body=body)[1])

    def login(self):
        self.json("/api/account/login", {"username": USERNAME, "password": PASSWORD})
        check(self.json("/api/account")["role"] == "owner", "Owner browser session unavailable")
        return self


def find_audio(get_json):
    tracks = get_json("/api/tracks?q=Compose+fixture&filter=ready")["tracks"]
    return tracks[0]["audio"] if tracks else None


def verify_audio(browser, original):
    audio = wait_for(lambda: find_audio(browser.json), "Fixture audio was never verified")
    check(browser.request(audio)[1] == original, "Playback stream differs from the original audio file")


def pair(room, owner, identity="compose-smoke-device"):
    link = urllib.parse.urlsplit(owner.json("/api/settings")["lan_url"])
    check(f"{link.scheme}://{link.netloc}" == room.origin, "Invitation did not preserve the configured origin")
    device = room.browser()
    device.request(f"/pair?{link.query}")
    device.json("/api/device", {"id": identity, "name": "Smoke test client"})
    return device


def prepare_fixture(folder):
    (folder / "Playlists").mkdir(parents=True)
    audio = folder / "fixture.flac"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                    "anoisesrc=sample_rate=44100:duration=2:seed=1", "-ac", "2", "-c:a", "flac",
                    "-metadata", f"title={TRACK}", "-metadata", "artist=Fixture Artist",
                    "-metadata", "album=Fixture Album", str(audio)], check=True, timeout=20)
    return audio.read_bytes()


def verify_idle_cutover(room, owner):
    def drain(action):
        return room.run("exec", "-T", "listening-room", "python3", "/app/scripts/update_drain.py",
                        action, check=False).returncode

    track_id = owner.json("/api/tracks?q=Compose+fixture&filter=ready")["tracks"][0]["id"]
    owner.json("/api/session", {"op": "play", "track_id": track_id})
    check(drain("claim") == os.EX_TEMPFAIL, "The host updater accepted an actively playing room")
    owner.json("/api/session", {"op": "pause"})
    radio = {"output": "fixture-radio-output", "source": "library"}
    owner.json("/api/radio/presence", radio)
    check(drain("claim") == os.EX_TEMPFAIL, "The host updater accepted an active Library Radio output")
    owner.json("/api/radio/presence", {**radio, "source": None})
    check(drain("claim") == 0, "An idle room did not grant the cutover lease")
    try:
        owner.request("/api/session", body={"op": "resume"}, statuses=(503,))
        owner.request("/")
    finally:
        check(drain("release") == 0, "The host updater failed to release the cutover lease")
    check(owner.json("/api/session", {"op": "resume"})["session_playing"],
          "The lease stopped playback after release")
    owner.json("/api/session", {"op": "pause"})
    print("Verified idle cutover defers playback, rejects new playback during the lease, and releases it.")


def verify_client_installers(browser):
    clients = browser.json("/api/setup/clients")
    check(len(clients["installers"]) == 2, "The room must offer Android and Mac installers")
    for entry in clients["installers"]:
        check("url" in entry, f"The staged {entry['platform']} installer was unavailable")
        payload = browser.request(entry["url"])[1]
        check(hashlib.sha256(payload).hexdigest() == entry["sha256"], "Installer checksum mismatch")
    print("Verified the paired client received both actual client installers with the advertised checksums.")


def run_checks(args, scratch):
    music = scratch / "music"
    audio = prepare_fixture(music)
    room = Room(scratch / "original", args.image, music=music, clients=args.client_artifacts)
    try:
        previous_has_owner = False
        previous_device = previous_playback = previous_radio = None
        mixes = [{"name": "Fixture mixes", "seeds": ["vugodYOKeis", "dqnnTbt99Q4"]}]
        if args.previous_image:
            room.up(image=args.previous_image, importing=True)
            status, body = room.browser().request("/api/account", statuses=(200, 401, 404))
            previous_has_owner = status == 200 and json.loads(body)["enabled"]
            if previous_has_owner:
                room.set_owner()
                owner = room.browser().login()
                owner.json("/api/import-list", PLAYLIST)
                verify_audio(owner, audio)
                previous_device = pair(room, owner)
                owner.json("/api/stations", {"stations": mixes})
                track_id = owner.json("/api/tracks?q=Compose+fixture&filter=ready")["tracks"][0]["id"]
                owner.json("/api/session", {"op": "play", "track_id": track_id})
                owner.json("/api/session", {"op": "pause"})
                previous_playback = owner.json("/api/session", {"op": "seek", "position": 1.25})
                owner.json("/api/my/radio", {"op": "tune", "tuning": secrets.token_hex(16), "history": {
                    "source": {"kind": "library", "name": "jazz"}, "current": track_id,
                    "position": 1.25, "sample": 1, "title": TRACK, "channel": "Fixture Artist",
                }})
                previous_radio = owner.json("/api/my/radio")
                check(previous_radio["history"] is not None, "Failed to seed previous Radio checkpoint")
            else:
                room.legacy_api("/api/import-list", PLAYLIST)
                audio_url = wait_for(lambda: find_audio(lambda path: json.loads(room.legacy_api(path))),
                                     "Previous release did not import the fixture")
                check(room.legacy_api(audio_url) == audio, "Previous release has different playback bytes")
            room.up()
        else:
            room.up(importing=True)
        room.browser().request("/api/overview", statuses=(401,))
        if not previous_has_owner:
            room.set_owner()
        owner = room.browser().login()
        if not args.previous_image:
            stations = owner.json("/api/stations")
            check(stations["stations"] == [] and stations["live"] == {} and not stations["youtube_enabled"],
                  "The fresh room came with external Radio providers")
            owner.json("/api/import-list", PLAYLIST)
        if previous_device:
            verify_audio(previous_device, audio)
            check(owner.json("/api/stations")["stations"] == mixes, "Upgrade changed the saved mixes")
            check(owner.json("/api/my/radio") == previous_radio, "Upgrade changed the Radio checkpoint")
            current = owner.json("/api/session")
            check(current["current_ref"] == previous_playback["current_ref"]
                  and abs(current["position"] - previous_playback["position"]) < 0.1,
                  "Upgrade changed the saved song position")
            print("Verified ordered mixes, existing device credentials, and song and Library Radio checkpoints.")
        verify_audio(owner, audio)
        if args.verify_idle:
            verify_idle_cutover(room, owner)
        owner.request("/")
        second_device = pair(room, owner, "compose-smoke-other-device")
        verify_audio(second_device, audio)
        if args.client_artifacts:
            verify_client_installers(second_device)
        room.up()
        verify_audio(room.browser().login(), audio)
        print("Verified Compose setup, private access, device pairing and original playback after removing the import mount.")
        if args.previous_image:
            print("Verified playback after upgrading from the previous image.")
            room.up(image=args.previous_image)
            rollback_owner = room.browser().login()
            verify_audio(rollback_owner, audio)
            if previous_device:
                verify_audio(previous_device, audio)
                check(rollback_owner.json("/api/stations")["stations"] == mixes, "Rollback changed the saved mixes")
            print("Verified the previous image can reopen the upgraded library.")
            room.up(image=args.image)
        if args.skip_restore:
            return
        room.run("stop")
        room.run("run", "-T", "--no-deps", "--rm", "listening-room", "/app/listening", "collection", "snapshot",
                 "/library/fixture-snapshot.sqlite3")
        room.run("run", "-T", "--no-deps", "--rm", "listening-room", "/app/listening", "collection", "restore",
                 "/library/fixture-snapshot.sqlite3", "/library/restored")
        restored = Room(scratch / "second", args.image, library=scratch / "original/library/restored")
        try:
            restored.up()
            restored.set_owner()
            verify_audio(restored.browser().login(), audio)
            print("Verified byte-identical playback after restoring into a second deployment.")
        finally:
            restored.close()
    finally:
        room.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="already-built image to verify")
    parser.add_argument("--previous-image", help="already-built previous image to upgrade from")
    parser.add_argument("--skip-restore", action="store_true", help="for CI hosts below the snapshot's 20 GiB disk reserve")
    parser.add_argument("--verify-idle", action="store_true", help="check the candidate host cutover lease")
    parser.add_argument("--client-artifacts", type=Path, help="staged client installers and manifest")
    args = parser.parse_args()
    scratch = Path(tempfile.mkdtemp(prefix="listening-compose-smoke-")).resolve()
    try:
        run_checks(args, scratch)
    finally:
        try:
            shutil.rmtree(scratch)
        except PermissionError:
            # The container's UID can own private directories in the fresh fixture.
            subprocess.run(["docker", "run", "--rm", "--pull", "never", "--user", "0", "--volume", f"{scratch}:/fixture",
                            args.image, "python3", "-c",
                            "from pathlib import Path; import shutil; [shutil.rmtree(p) for p in Path('/fixture').iterdir()]"],
                           check=True, timeout=30)
            scratch.rmdir()


if __name__ == "__main__":
    main()
