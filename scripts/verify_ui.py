#!/usr/bin/env python3
"""The rendered gate (plan 050, step 1b): `make verify-ui`.

Builds a fixture room, boots the real server against it, seeds a
live playback session through the API, and drives headless Chromium
over every surface of the interface, capturing a screenshot per
surface and collecting console errors and page errors. The exit
code is the verdict: 0 only when every surface rendered and no page
errors or console errors were captured.

Why this exists: the React conversion's defects (invented class
names, an SDK contract the custom queue primitive did not meet,
dialogs rendering in a corner) were all invisible to builds and API
checks. This gate is the standing, scripted form of the rendered
pass that caught them by hand.

Fixture (grown once, per plan 050): the evening list of five songs
with four real verified MP3s, plus

- genre facets for the four verified tracks, written through the
  app's own GenreStore, so the genre tag filter has real facets;
- a second playlist in a watched folder, mirrored by the app's own
  source machinery, so its magnet (source-owned) classification is
  the app's, not staged;
- one pending song request in the requests store;
- synced lyrics for the session's track, seeded into the external
  cache under the app's own lyrics key, so the lyrics stage
  (plan 049) renders offline and the player's lyrics button shows;
- a short play history in the play_events table (plan 045's rows,
  under the app's own schema), so the Your listening views (plan
  050 step 5) render their data: Paradise Circus completed five
  times, Asleep twice, Blame started once and skipped.

Honesty rules:

- Browser discovery: Playwright must be importable by the
  interpreter running this script, and a Chromium executable must
  be launchable (the VERIFY_UI_CHROME environment variable names
  one explicitly). When either is missing the script prints exactly
  what is missing and exits 2. It never passes silently.
- Console messages whose text is a resource-load failure ("Failed
  to load resource") are recorded as warnings, not errors: in a
  sandboxed environment an external catalog search can fail to
  reach the network without the interface being broken. Page
  errors (uncaught exceptions) always count.

Output (screenshots, report.json, report.txt) lands in
verification/ui/ at the repository root, which is gitignored.
Exit codes: 0 pass, 1 a surface or the error verdict failed,
2 required tooling is missing.
"""

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "verification" / "ui"

PROFILE_ARTIST = "00000000-0000-0000-0000-000000000014"
PROFILE_ALBUM = "00000000-0000-0000-0000-000000000015"
PROFILE_EDITION = "00000000-0000-0000-0000-000000000016"
PROFILE_MISSING = "00000000-0000-0000-0000-000000000017"
PROFILE_ASLEEP = "00000000-0000-0000-0000-000000000018"
PROFILE_SINGLE = "00000000-0000-0000-0000-000000000019"

SONGS = [("atlas", "Atlas Air", "Massive Attack", "Heligoland", 65),
         ("asleep", "Asleep", "The Smiths", "Louder Than Bombs", 48),
         ("paradise", "Paradise Circus", "Massive Attack", "Heligoland", 72),
         ("blame", "Blame", "Tropics", "Rapture", 39)]
GENRES = {"Atlas Air": "Electronic", "Asleep": "Rock",
          "Paradise Circus": "Electronic", "Blame": "Indie"}
TEMPOS = {"atlas": 112, "asleep": 54, "paradise": 82, "blame": 118}
WATCHED_SONGS = [("Blue Monday", "New Order", "Power, Corruption & Lies"),
                 ("Once in a Lifetime", "Talking Heads", "Remain in Light")]
# Synced lyrics for Atlas Air, the session's track (plan 049): the
# lines land in the external cache under the app's lyrics key, so
# the stage renders with no network. The 30-second line is the
# seek target of the lyrics-stage surface.
ATLAS_LYRICS = [(0, "Fixture line one, the air is still"),
                (4, "Fixture line two, a signal climbs"),
                (8, "Fixture line three, the room turns slow"),
                (12, "Fixture line four, a window glows"),
                (16, "Fixture line five, the bassline walks"),
                (20, "Fixture line six, the city hums"),
                (24, "Fixture line seven, the night holds on"),
                (30, "Fixture line eight, the seek lands here"),
                (38, "Fixture line nine, the drums return"),
                (48, "Fixture line ten, the lights go low"),
                (58, "Fixture line eleven, the air goes home")]


def fail_tooling(what):
    print(f"verify-ui: missing tooling: {what}")
    print("verify-ui: the gate cannot run, so it does not pass.")
    return 2


# ---------------------------------------------------------------------
# Fixture

def build_fixture(root):
    """The fixture room under `root`: an application folder (config,
    state, batch logs) and a library workspace (lists + audio), plus
    a watched folder holding the second playlist's source file."""
    app = root / "app"
    music = root / "music"
    watched = root / "watched"
    (app).mkdir(parents=True, exist_ok=True)
    (music / "lists").mkdir(parents=True, exist_ok=True)
    (music / "audio").mkdir(parents=True, exist_ok=True)
    watched.mkdir(parents=True, exist_ok=True)

    (music / "lists" / "evening.csv").write_text(
        "title,artist,album\n"
        "Atlas Air,Massive Attack,Heligoland\n"
        "Asleep,The Smiths,Louder Than Bombs\n"
        "Paradise Circus,Massive Attack,Heligoland\n"
        "Blame,Tropics,Rapture\n"
        "Missing Song,Ghost Artist,Nowhere\n")
    (watched / "morning.csv").write_text(
        "title,artist,album\n"
        + "".join(f"{title},{artist},{album}\n"
                  for title, artist, album in WATCHED_SONGS))

    for slug, title, artist, album, secs in SONGS:
        out = music / "audio" / f"{slug}.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             f"sine=frequency=440:duration={secs}",
             "-codec:a", "libmp3lame", "-b:a", "320k",
             "-metadata", f"title={title}", "-metadata", f"artist={artist}",
             "-metadata", f"album={album}", "-metadata", f"BPM={TEMPOS[slug]}", str(out)],
            check=True, capture_output=True)

    lines = []
    for i, (slug, title, artist, album, secs) in enumerate(SONGS, start=1):
        lines.append(f"[{i:03d}] SongJob: searching: {artist} - {title}")
        lines.append(f"[{i:03d}] SongJob: downloading: {artist} - {title}: "
                     f"downloads\\{slug}.mp3")
        lines.append(f"[{i:03d}] SongJob: succeeded: {artist} - {title}: "
                     f"downloads\\{slug}.mp3")
    lines.append("[005] SongJob: searching: Ghost Artist - Missing Song")
    lines.append("[005] SongJob: failed [No search results]: "
                 "Ghost Artist - Missing Song")
    (app / "batch-00001-00005.log").write_text("\n".join(lines) + "\n")
    (app / "runner.log").write_text(
        "runner started\nbatch 00001-00005 finished\nwatchdog idle\n")
    return app, music, watched


def write_config(app, music, watched, port):
    (app / "listening-room.json").write_text(json.dumps({
        "folder": str(music),
        "port": port,
        "source_folders": [str(watched)],
        # The fixture models a settled room (plan 062): its songs
        # stay files so the surfaces exercise file playback, and
        # no migration starts during the gate.
        "library_migrated": 1700000000.0,
    }))


def seed_state(app):
    """Coverage, genres, the pending request, the session track's
    lyrics, and the play history, written while the server is
    stopped. Coverage goes in as rows the way the report writes
    them; genres go through the app's own GenreStore (the same
    write path the genre pass and corrections use); the request
    goes through the app's RequestsStore enqueue; the lyrics land
    in the external cache through the store's cache write, under
    the lyrics key the metadata layer reads; the play history goes
    in as play_events rows under the app's own schema, with fixed
    past stamps so the Your listening orders are deterministic
    however the live session behaves."""
    import sqlite3
    database = app / "requests.sqlite3"
    meta = {1: ("Massive Attack", "Atlas Air", "atlas.mp3"),
            2: ("The Smiths", "Asleep", "asleep.mp3"),
            3: ("Massive Attack", "Paradise Circus", "paradise.mp3"),
            4: ("Tropics", "Blame", "blame.mp3")}
    db = sqlite3.connect(str(database))
    for idx, (artist, title, filename) in meta.items():
        db.execute(
            "INSERT OR REPLACE INTO coverage "
            "(idx,status,artist,title,uri,path,reason) "
            "VALUES (?,?,?,?,?,?,?)",
            (idx, "verified", artist, title, "", filename, ""))
    keys = {}
    for record_id, blob in db.execute(
            "SELECT idx, record FROM catalog_records"):
        record = json.loads(blob)
        keys[record["title"]] = record["key"]
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from playback.play_events import SCHEMA as PLAY_EVENTS_SCHEMA
    db.executescript(PLAY_EVENTS_SCHEMA)
    # (offset, kind, track index, listened): Paradise Circus plays
    # to the end five times, Asleep twice, Blame is skipped once.
    history = [(100, "started", 2, None), (148, "completed", 2, 48.0),
               (200, "started", 3, None), (272, "completed", 3, 72.0),
               (300, "started", 3, None), (372, "completed", 3, 72.0),
               (400, "started", 4, None), (410, "skipped", 4, 10.0),
               (500, "started", 3, None), (572, "completed", 3, 72.0),
               (600, "started", 2, None), (648, "completed", 2, 48.0),
               (700, "started", 3, None), (772, "completed", 3, 72.0),
               (800, "started", 3, None), (872, "completed", 3, 72.0)]
    titles = {idx: title for idx, (_a, title, _f) in meta.items()}
    for offset, kind, idx, listened in history:
        db.execute(
            "INSERT INTO play_events (ts, kind, ref, track_index, "
            "record_key, request_id, device_id, listened) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (1_700_000_000.0 + offset, kind, str(idx), idx,
             keys[titles[idx]], None, None, listened))
    db.commit()
    db.close()

    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from catalog.genres import GenreStore
    from downloads.requests_store import RequestsStore
    from metadata.artist_connections import quote_search
    from metadata.external_metadata import GENRE_MATCH_PREFIX
    store = GenreStore(database)
    # Dialog controls render after release metadata resolves. Keep this fixture
    # independent of MusicBrainz latency while exercising the real route.
    metadata_store = RequestsStore(database)
    for key in keys.values():
        metadata_store.cache_put("metadata:" + key, "{}", time.time())
        metadata_store.cache_put(GENRE_MATCH_PREFIX + key, json.dumps(""), time.time())
    credit = [{"name": "The Smiths", "artist": {"id": PROFILE_ARTIST, "name": "The Smiths"}}]
    album = {"id": PROFILE_ALBUM, "title": "Louder Than Bombs", "primary-type": "Album", "artist-credit": credit}
    single = {"id": PROFILE_SINGLE, "title": "Another Single", "primary-type": "Single", "artist-credit": credit}
    edition = {"id": PROFILE_EDITION, "title": "Louder Than Bombs", "status": "Official", "release-group": album}
    asleep = {"id": PROFILE_ASLEEP, "title": "Asleep", "length": 48000, "artist-credit": credit, "releases": [edition]}
    missing = {"id": PROFILE_MISSING, "title": "New Song", "length": 60000, "artist-credit": credit}
    query = f'recording:{quote_search("Asleep")} AND artist:{quote_search("The Smiths")}'
    profile_cache = {
        "artist-graph:recording-search:" + query.casefold(): {"recordings": [asleep]},
        f"artist-graph:recording:{PROFILE_ASLEEP}": asleep,
        f"artist-graph:artist:{PROFILE_ARTIST}": {"id": PROFILE_ARTIST, "name": "The Smiths",
                                                "type": "Group", "release-groups": [album, single]},
        f"artist-popularity:recordings:{PROFILE_ARTIST}": [
            {"recording_mbid": PROFILE_MISSING, "recording_name": "New Song", "artist_name": "The Smiths",
             "release_name": "Louder Than Bombs", "length": 60000, "total_listen_count": 200},
            {"recording_mbid": PROFILE_ASLEEP, "recording_name": "Asleep", "artist_name": "The Smiths",
             "release_name": "Louder Than Bombs", "length": 48000, "total_listen_count": 80},
        ],
        f"artist-popularity:release-groups:{PROFILE_ARTIST}": [
            {"release_group_mbid": PROFILE_ALBUM, "total_listen_count": 120,
             "release_group": {"name": "Louder Than Bombs", "type": "Album"}},
            {"release_group_mbid": PROFILE_SINGLE, "total_listen_count": 40,
             "release_group": {"name": "Another Single", "type": "Single"}},
        ],
        f"artist-graph:release-group:{PROFILE_ALBUM}": {**album, "releases": [edition]},
        f"music-profile:edition:{PROFILE_EDITION}": {
            **edition, "artist-credit": credit, "media": [{
                "position": 1, "tracks": [{"number": "1", "recording": asleep}, {"number": "2", "recording": missing}]
            }]},
    }
    for key, payload in profile_cache.items():
        metadata_store.cache_put(key, json.dumps(payload), time.time())
    for title, genre in GENRES.items():
        store.set(keys[title], genre, "file")
    deferred = metadata_store.enqueue(
        None, "Nina Simone", "Feeling Good", "", None,
        "Nina Simone Feeling Good")
    metadata_store.claim(deferred)
    metadata_store.defer_attempt(metadata_store.get(deferred), "Transfer timed out")
    missing_copy = metadata_store.enqueue(
        None, "Fixture Artist", "Unavailable Copy", "", 60,
        "Unavailable Copy")
    metadata_store.finish(missing_copy, "not_found", "No copy found", "")
    synced = "\n".join(
        f"[{stamp // 60:02d}:{stamp % 60:02d}.00]{text}"
        for stamp, text in ATLAS_LYRICS)
    plain = "\n".join(text for _, text in ATLAS_LYRICS)
    RequestsStore(database).cache_put(
        "lyrics:" + keys["Atlas Air"],
        json.dumps({"found": True, "plain": plain, "synced": synced,
                    "source": "lrclib"}),
        time.time())


# ---------------------------------------------------------------------
# Server lifecycle

def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Server:
    def __init__(self, root, app, port):
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.log_path = root / "server.log"
        self.process = None
        self._log = None
        prefixes = ("LISTENING_ROOM_", "OFFLINE_MUSIC_", "SOCKSEEK_", "LISTENING_YOUTUBE_",
                    "LISTENING_JELLYFIN_", "LISTENING_AUDIOBOOKSHELF_")
        env = {key: value for key, value in os.environ.items() if not key.startswith(prefixes)}
        env["HOME"] = str(root / "home")
        env["LISTENING_ROOM_CONFIG"] = str(app / "listening-room.json")
        env["PYTHONUNBUFFERED"] = "1"
        self._env = env

    def start(self):
        self._log = self.log_path.open("ab")
        self.process = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "backend" / "server.py"),
             "--host", "127.0.0.1", "--port", str(self.port)],
            env=self._env, stdout=self._log, stderr=subprocess.STDOUT)

    def stop(self):
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None
        if self._log is not None:
            self._log.close()
            self._log = None

    def request(self, method, path, body=None, timeout=10):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=headers, method=method)

        def parse(text):
            if not text:
                return {}
            try:
                return json.loads(text)
            except ValueError:
                return {"raw": text}
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status, parse(response.read().decode())
        except urllib.error.HTTPError as error:
            return error.code, parse(error.read().decode())


def wait_for(description, predicate, timeout=120, interval=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
        except Exception:
            value = None
        if value:
            return value
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {description}")


# ---------------------------------------------------------------------
# Browser phase

class Surface:
    def __init__(self, name):
        self.name = name
        self.ok = False
        self.detail = ""


def verify_request_restart(page, root, screenshot):
    """Drive the picker and real restarted server/worker against local peer files."""
    app, music = root / "app", root / "music"
    app.mkdir()
    (music / "lists").mkdir(parents=True)
    (music / "lists" / "test.csv").write_text(
        "title,artist,album\nOlder song,Fixture,Album\nDirect song,Fixture,Album\nAAC fallback,Fixture,Album\n")
    sockseek = root / "sockseek"
    shutil.copyfile(REPO_ROOT / "tests/fixtures/format_sockseek.py", sockseek)
    sockseek.chmod(0o700)
    (app / "sockseek.conf").write_text("username = fixture\npassword = fixture\n")
    port = free_port()
    (app / "listening-room.json").write_text(json.dumps({
        "folder": str(music), "sockseek_binary": str(sockseek), "port": port,
        "library_migrated": 1, "audio_store": "database", "auto_download": False,
    }))
    for codec in ("aac", "alac"):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "anoisesrc=color=white:duration=2:sample_rate=44100:seed=7",
                        "-ac", "2", "-c:a", codec, "-b:a", "320k", "-metadata",
                        "title=Format fixture", "-metadata", "artist=Fixture", "-metadata", "album=Live", str(root / (codec + ".m4a"))], check=True)
    for seed, title in enumerate(("Older song", "Direct song"), start=7):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"anoisesrc=duration=2:seed={seed}",
                        "-ac", "2", "-c:a", "libmp3lame", "-b:a", "320k",
                        "-metadata", f"title={title}", "-metadata", "artist=Fixture",
                        "-metadata", "BPM=112", str(root / f"{title}.mp3")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=duration=2:seed=9",
                    "-ac", "2", "-c:a", "aac", "-b:a", "320k", "-metadata", "title=AAC fallback",
                    "-metadata", "artist=Fixture", str(root / "AAC fallback.m4a")], check=True)
    server = Server(root, app, port)
    def rows():
        return server.request("GET", "/api/requests")[1]["requests"]

    def open_requests():
        page.get_by_role("button", name="Requests", exact=True).click()
        page.get_by_role("heading", name="Recent requests").wait_for()

    calls = root / "calls.jsonl"
    sources_url = "**/api/extensions/sources"
    page.route(sources_url, lambda route: route.fulfill(json={
        "inventory_available": True, "state": "ready", "installed": [],
        "selection": {"search": "fixture-search", "fetch": "fixture-download"},
    }))
    try:
        server.start()
        wait_for("isolated server", lambda: server.request("GET", "/")[0] == 200)
        page.goto(server.base)
        find = page.get_by_role("button", name="Find a copy of Older song")
        find.click()
        picker = page.get_by_role("dialog", name="Choose format")
        picker.get_by_role("button", name="Later").click()
        page.get_by_role("button", name="Choose format for Older song").click()
        picker.get_by_role("button", name="Later").click()
        open_requests()
        sources = page.locator(".find-copy-sources")
        sources.get_by_text("Find copy uses the room’s configured download sources.", exact=True).wait_for()
        assert "fixture-search" not in sources.inner_text()
        assert "fixture-download" not in sources.inner_text()
        page.get_by_role("button", name="View extensions").wait_for()
        form = page.get_by_role("form", name="Request a song")
        form.get_by_label("Artist").fill("Fixture")
        form.get_by_label("Song title").fill("Format fixture")
        form.get_by_role("button", name="Find copy").click()
        picker.get_by_role("radio", name="Any lossless (prefer FLAC)").wait_for()
        assert picker.get_by_role("radio", name="Any lossless (prefer FLAC)").is_checked()
        picker.get_by_role("button", name="Later").click()
        original = rows()
        assert len(original) == 2 and all(r["status"] == "waiting_format" for r in original)
        page.goto("about:blank")
        server.stop()
        server.start()
        wait_for("restarted room", lambda: server.request("GET", "/")[0] == 200)
        page.goto(server.base)
        open_requests()
        assert [(r["id"], r["status"]) for r in rows()] == [(r["id"], "waiting_format") for r in original]
        assert not calls.exists(), "Sockseek ran before explicit confirmation"
        row = page.locator(".request-item", has_text="Format fixture")
        row.get_by_role("button", name="Choose format").click()
        picker.get_by_role("radio", name="Apple Lossless (ALAC) only").check()
        picker.get_by_role("button", name="View extensions").click()
        extensions = page.get_by_role("dialog", name="Extensions")
        extensions.get_by_role("heading", name="Download extensions").wait_for()
        back = extensions.get_by_role("button", name="Back to format")
        assert back.locator("svg").is_visible() and back.inner_text() == ""
        heading = extensions.get_by_role("heading", name="Extensions", exact=True)
        arrow, title = back.bounding_box(), heading.bounding_box()
        assert arrow["x"] < title["x"] and arrow["y"] <= title["y"] < arrow["y"] + arrow["height"]
        back.click()
        assert picker.get_by_role("radio", name="Apple Lossless (ALAC) only").is_checked()
        assert picker.get_by_role("button", name="View extensions").evaluate("el => el === document.activeElement")
        page.screenshot(path=str(screenshot))
        picker.get_by_role("button", name="Confirm and search").click()
        result = wait_for("worker to verify ALAC", lambda: next(
            (r for r in rows() if r["title"] == "Format fixture" and r["status"] == "downloaded"), None),
            timeout=60)
        assert result["requested_format"] == "alac" and result["audio_path"].startswith("blob:")
        status, _ = server.request("POST", "/api/requests/format", {
            "request_id": result["id"], "requested_format": "alac", "format_revision": 0})
        assert status == 200 and rows()[0]["status"] == "downloaded"
        assert rows()[1]["status"] == "waiting_format"
        commands = [json.loads(line) for line in calls.read_text().splitlines()]
        searches = [cmd for cmd in commands if "--print" in cmd]
        assert len(searches) == 1 and searches[0][searches[0].index("--format") + 1] == "m4a"
        assert [cmd[0].split("/")[2] for cmd in commands if cmd[0].startswith("slsk://")] == ["aac", "alac"]
        import hashlib
        assert result["audio_path"] == "blob:" + hashlib.sha256((root / "alac.m4a").read_bytes()).hexdigest()

        # A same-titled legacy download already exists. A new version must get
        # its own picker, verified audio reference, and room playback target.
        recording_id = "00000000-0000-4000-8000-000000000025"
        song = {"artist": "Fixture", "title": "Format fixture", "seed_artist": "Starting artist",
                "album": "Concert", "version": "Live", "duration": 2, "musicbrainz_id": recording_id}
        page.route("**/api/discover", lambda route: route.fulfill(json={"week": "test", "tracks": [song]}))
        page.route("**/api/discover/recording/*", lambda route: route.fulfill(json={"leads": []}))
        try:
            page.get_by_role("button", name="Deep Dive", exact=True).click()
            page.get_by_role("region", name="Artist trails").get_by_role("button", name=re.compile("Starting artist")).click()
            fork = page.get_by_role("region", name="Paths to follow")
            fork.get_by_role("button", name=re.compile("Suggested artist Fixture")).click()
            fork.get_by_role("button", name=re.compile("Recording Format fixture")).click()
            page.get_by_role("button", name="Find a copy", exact=True).click()
            picker.get_by_role("radio", name="Apple Lossless (ALAC) only").check()
            picker.get_by_role("button", name="Confirm and search").click()
            play = page.get_by_role("button", name="Play in room", exact=True)
            play.wait_for(timeout=60000)
            verified = next(row for row in rows() if row["musicbrainz_id"] == recording_id)
            assert verified["id"] != result["id"] and verified["version"] == "Live" and verified["length"] == 2
            play.click()
            wait_for("the selected version in the player", lambda:
                     server.request("GET", "/api/session")[1]["current_ref"] == "custom-" + str(verified["id"]))
        finally:
            page.unroute("**/api/discover")
            page.unroute("**/api/discover/recording/*")
        page.get_by_role("button", name="Library", exact=True).click()
        page.get_by_role("button", name="Choose format for Older song").click()
        assert picker.get_by_role("radio", name="Any lossless (prefer FLAC)").is_checked()
        assert picker.get_by_role("radio", name="MP3 only").is_visible()
        picker.get_by_role("button", name="Confirm and search").click()
        retry = page.get_by_role("button", name="Try MP3 for Older song", exact=True)
        retry.wait_for(timeout=60000)
        miss = next(row for row in rows() if row["title"] == "Older song")
        assert miss["requested_format"] == "any_lossless" and miss["status"] == "not_found"
        page.set_viewport_size({"width": 390, "height": 844})
        try:
            retry.click()
            wait_for("worker to verify the per-song MP3", lambda: any(
                r["id"] == miss["id"] and r["status"] == "downloaded" and r["requested_format"] == "mp3"
                for r in rows()), timeout=60)
        finally:
            page.set_viewport_size({"width": 1440, "height": 900})
        assert sum(r["title"] == "Older song" for r in rows()) == 1
        assert [r["requested_format"] for r in rows() if r["title"] == "Format fixture"] == ["alac", "alac"]
        formats = [cmd[cmd.index("--format") + 1] for cmd in map(json.loads, calls.read_text().splitlines())
                   if "--print" in cmd and cmd[0] == "Fixture - Older song"]
        assert formats == ["flac,m4a", "mp3"]

        page.get_by_role("button", name="Find a copy of Direct song").click()
        assert picker.get_by_role("radio", name="Any lossless (prefer FLAC)").is_checked()
        picker.get_by_role("radio", name="MP3 only").check()
        picker.get_by_role("button", name="Confirm and search").click()
        direct = wait_for("worker to verify the first-choice MP3", lambda: next((
            r for r in rows() if r["title"] == "Direct song"
            and r["status"] == "downloaded" and r["requested_format"] == "mp3"), None), timeout=60)
        direct_searches = [cmd[cmd.index("--format") + 1] for cmd in map(json.loads, calls.read_text().splitlines())
                           if "--print" in cmd and cmd[0] == "Fixture - Direct song"]
        assert direct_searches == ["mp3"]

        page.get_by_role("button", name="Find a copy of AAC fallback").click()
        assert picker.get_by_role("radio", name="Any lossless (prefer FLAC)").is_checked()
        picker.get_by_role("radio", name="MP3 or AAC (prefer MP3)", exact=True).check()
        picker.get_by_role("button", name="Confirm and search").click()
        fallback = wait_for("worker to verify AAC when MP3 is absent", lambda: next((
            row for row in rows() if row["title"] == "AAC fallback" and row["status"] == "downloaded"), None), timeout=60)
        assert fallback["requested_format"] == "any_lossy"
        search = next(cmd for cmd in map(json.loads, calls.read_text().splitlines())
                      if "--print" in cmd and cmd[0] == "Fixture - AAC fallback")
        assert search[search.index("--format") + 1] == "mp3,m4a"
        assert search[search.index("--pref-format") + 1] == "mp3"
        with urllib.request.urlopen(server.base + fallback["audio"], timeout=10) as response:
            assert response.status == 200 and response.headers["Content-Type"] == "audio/mp4"

        def downloaded_analysis():
            # Read the persisted result directly so no Activities request can start the scanner.
            with closing(sqlite3.connect(app / "requests.sqlite3")) as db:
                return db.execute("SELECT bpm, energy_db FROM activity_tempos JOIN activity_energy USING (identity) "
                                  "WHERE identity = ? AND energy_db IS NOT NULL",
                                  (direct["audio_path"],)).fetchone()

        assert wait_for("analysis of the newly downloaded MP3", downloaded_analysis, timeout=75)[0] == 112.0
    finally:
        page.unroute(sources_url)
        page.goto("about:blank")
        server.stop()


def record_browser_errors(page, errors, warnings):
    page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))

    def record_console(message):
        if message.type == "error":
            target = warnings if "Failed to load resource" in message.text else errors
            target.append(f"console: {message.text}")

    page.on("console", record_console)


def verify_landing(browser, base, output, errors, warnings):
    with urllib.request.urlopen(base + "/service-worker.js", timeout=5) as response:
        worker = response.read()
        assert b"/assets/first-light-" not in worker, "The player precaches the tour recording or artwork"
        assert b"/assets/about-showcase/" not in worker, "The player precaches the About screenshots"
        assert b"/assets/feature-pattern.worker-" not in worker, "The player precaches the About search worker"
        assert b"about/index.html" not in worker, "The player precaches the gated feature tour"
    desktop = browser.new_page(viewport={"width": 1440, "height": 900}, service_workers="block")
    mobile = browser.new_page(viewport={"width": 390, "height": 844}, has_touch=True,
                              is_mobile=True, reduced_motion="reduce", service_workers="block")
    for page, url in ((desktop, "/about"), (mobile, "/about/")):
        try:
            record_browser_errors(page, errors, warnings)
            requests = []
            page.on("request", lambda request: requests.append(request.url))
            response = page.goto(base + url)
            assert response.status == 200
            page.get_by_role("heading", level=1, name="Stream the records you own.").wait_for()
            assert page.get_by_role("navigation").get_by_role("link", name="Open the room").get_attribute("href") == "/"
            sleeve = page.locator(".about-sleeve")
            assert sleeve.locator("img").evaluate("img => img.complete && img.naturalWidth > 0")
            assert page.get_by_role("link", name="Explore the player").get_attribute("href") == "#showcase"
            assert page.get_by_role("navigation").get_by_role("link", name="Features").is_visible()
            assert not re.search(r"\b(?:YouTube|SomaFM|Spotify)\b", page.locator("body").inner_text(), re.I)
            screens = page.get_by_role("region", name="Inside Listening.")
            first_screen = screens.get_by_role("img", name="Library in Listening")
            assert first_screen.bounding_box()["width"] <= (390 if page == mobile else 560)
            screens.get_by_text("See more screens", exact=True).click()
            room_images = screens.get_by_role("img").all()
            assert len(room_images) == 16
            for picture in room_images:
                picture.scroll_into_view_if_needed()
                picture.evaluate("image => image.decode()")
                assert picture.evaluate("image => image.naturalWidth > 0")
                assert picture.get_attribute("src") == picture.locator("..").get_attribute("href")
            screens.get_by_text("See more screens", exact=True).click()
            showcase = page.get_by_role("region", name="Listening on e-ink.")
            showcase.scroll_into_view_if_needed()
            images = showcase.get_by_role("img").all()
            assert len(images) == 2
            for image in images:
                image.evaluate("img => img.decode()")
                assert image.evaluate("img => img.naturalWidth === 480 && img.naturalHeight === 800")
                assert image.get_attribute("src") == image.locator("..").get_attribute("href")
            assert page.evaluate("document.documentElement.scrollWidth === innerWidth"), "Landing overflows"
            assert not any(".mp3" in url for url in requests), "Preview audio loaded before play"
            page.screenshot(path=str(output / ("landing-mobile.png" if page == mobile else "landing-desktop.png")),
                            full_page=True, animations="disabled")
            if page == mobile:
                assert page.locator(".about-lead").evaluate("node => node.getAnimations().length") == 0
                sleeve.locator("summary").tap()
            else:
                sleeve.locator("summary").focus()
                page.keyboard.press("Enter")
            assert sleeve.evaluate("node => node.open")
            page.get_by_role("button", name="Resume on phone").click()
            assert "Phone · 0:04" in page.locator(".about-mini").inner_text()
            page.get_by_role("button", name="Artist credits").click()
            page.get_by_role("button", name="Joe Morello").click()
            assert "Drums" in page.locator(".about-result").inner_text()
            page.get_by_role("button", name="Follow credit to Time Out").click()
            page.get_by_role("heading", name="Time Out").wait_for()
            page.get_by_role("button", name="Back to Take Five").click()
            page.get_by_role("button", name="Camping mode", exact=True).click()
            page.get_by_role("button", name="90 songs").click()
            assert "90 songs" in page.locator(".about-result").inner_text()
            page.get_by_role("button", name="Pack preview mix").click()
            assert "Saved trip preview" in page.locator(".about-result").inner_text()
            features = page.get_by_role("region", name="Find a feature.")
            query = features.get_by_role("searchbox", name="Search supported features")
            query.fill("campign")
            features.get_by_text("1 feature", exact=True).wait_for()
            features.get_by_text("Offline listening and Camping mode", exact=True).click()
            assert features.get_by_text("Buffer upcoming tracks", exact=False).is_visible()
            features.get_by_role("checkbox", name="Regex").check()
            query.fill(r"crossfade|sync(?:ed)? lyrics")
            features.get_by_text("2 features", exact=True).wait_for()
            assert features.get_by_text("Playback and queue", exact=True).is_visible()
            assert features.get_by_text("Synced lyrics", exact=True).is_visible()
            query.fill("(")
            features.get_by_text("Check your regular expression.", exact=True).wait_for()
            query.fill("qwertyuiop")
            features.get_by_text("No features match. Try another search.", exact=True).wait_for()
            query.fill("")
            features.get_by_text("13 features", exact=True).wait_for()

            install = page.get_by_role("region", name="Set up a room.")
            mac = install.get_by_role("tab", name="Mac", exact=True)
            mac.press("ArrowRight")
            assert install.get_by_role("tab", name="Linux", exact=True).get_attribute("aria-selected") == "true"
            linux = install.get_by_role("tabpanel", name="Linux", exact=True)
            guide = linux.get_by_role("link", name="Illustrated setup guide")
            assert guide.is_visible()
            assert guide.get_attribute("href").endswith("/docs/onboarding-walkthrough.md")
            assert "docker compose" not in linux.inner_text().lower()
            install.get_by_text("See the setup screens", exact=True).click()
            setup_images = install.get_by_role("img").all()
            assert len(setup_images) == 2
            for picture in setup_images:
                picture.scroll_into_view_if_needed()
                picture.evaluate("image => image.decode()")
                assert picture.evaluate("image => image.naturalWidth === 1200")
            assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
            page.evaluate("location.hash = 'safari-guide'")
            apple = install.get_by_role("tabpanel", name="iPhone & iPad")
            apple.get_by_text("Share → Add to Home Screen").wait_for()
            install.get_by_role("tab", name="Android").click()
            assert install.get_by_role("link", name="Download for Android").is_visible()
            assert install.get_by_role("link", name="Get apps from this room").get_attribute("href") == "/?setup=clients"
            assert page.locator("a[href]").evaluate_all(
                """links => links.every(link => link.origin === location.origin ||
                    ['https://github.com', 'https://musicbrainz.org'].includes(link.origin))"""
            ), "The landing contains an unexpected external link"
            unexpected = [url for url in requests
                          if not url.startswith((base + "/", "blob:" + base + "/"))
                          or url.startswith(base + "/api/")]
            assert not unexpected, f"The landing fetched unexpected resources: {unexpected}"
        finally:
            page.close()


def verify_phone(browser, server, output, errors, warnings):
    with browser.new_context(has_touch=True, is_mobile=True,
                             viewport={"width": 320, "height": 568}) as context:
        phone = context.new_page()
        record_browser_errors(phone, errors, warnings)
        phone.goto(server.base)
        menu = phone.get_by_role("button", name="Menu", exact=True)
        nav = phone.get_by_role("navigation", name="Main")
        player = phone.get_by_role("contentinfo", name="Audio player")
        menu.tap()
        if nav.bounding_box()["height"] > 0.53 * phone.viewport_size["height"]:
            raise AssertionError("Phone navigation takes up more than half the viewport")
        phone.get_by_role("button", name="Settings", exact=True).tap()
        phone.get_by_role("heading", name="Settings", exact=True).wait_for()
        nav.wait_for(state="hidden")
        phone.get_by_role("tab", name="Storage", exact=True).tap()
        storage = phone.get_by_role("tab", name="Storage", exact=True)
        if storage.get_attribute("aria-selected") != "true":
            raise AssertionError("Storage is not reachable from the phone settings tabs")
        if phone.evaluate("document.documentElement.scrollWidth") > phone.viewport_size["width"]:
            raise AssertionError("Settings overflows the 320px viewport")

        phone.set_viewport_size({"width": 390, "height": 650})
        phone.get_by_role("tab", name="Room setup", exact=True).tap()
        phone.get_by_role("button", name="Manage sources", exact=True).wait_for()
        phone.screenshot(path=str(output / "01c-phone-settings.png"))
        menu.tap()
        phone.screenshot(path=str(output / "01d-phone-menu.png"))
        phone.keyboard.press("Escape")
        nav.wait_for(state="hidden")
        menu.tap()
        phone.touchscreen.tap(phone.viewport_size["width"] - 2, phone.viewport_size["height"] // 2)
        nav.wait_for(state="hidden")
        menu.tap()
        phone.get_by_role("button", name="Favorites", exact=True).tap()
        phone.get_by_role("heading", name="Favorite songs").wait_for()
        nav.wait_for(state="hidden")
        menu.tap()
        phone.get_by_role("button", name="Library", exact=True).tap()
        phone.locator(".playlist-card", has_text="Evening").tap()
        row = phone.locator(".track-row").first
        row.wait_for()
        row.scroll_into_view_if_needed()
        song = row.locator(".song-cell").bounding_box()
        actions = row.locator(".row-action").bounding_box()
        if song["width"] < 250 or song["y"] + song["height"] > actions["y"]:
            raise AssertionError("Song title is squeezed by its phone action buttons")
        row.locator(".status-pill").wait_for(state="visible")
        row.locator(".time-cell").wait_for(state="visible")
        assert row.locator(".favorite-button svg, .track-more svg").count() == 2
        if phone.evaluate("document.documentElement.scrollWidth") > phone.viewport_size["width"]:
            raise AssertionError("The phone library overflows the viewport")
        phone.screenshot(path=str(output / "01e-phone-library.png"))
        player.get_by_role("button", name="Open queue").tap()
        phone.get_by_role("button", name="Close queue").tap()
        menu.tap()
        phone.get_by_role("button", name="Mood map", exact=True).tap()
        mood = phone.get_by_role("button", name="Calm", exact=True)
        mood.tap()
        if mood.get_attribute("aria-pressed") != "true":
            raise AssertionError("Tapping a mood did not select it")
        region = phone.get_by_role("region", name="Browse by mood")
        settings = region.get_by_role("button", name="Map settings")
        if settings.get_attribute("aria-expanded") != "false" or region.get_by_role("combobox").count():
            raise AssertionError("Mood settings should be collapsed on entry")
        settings.tap()
        region.get_by_role("combobox", name="Genre").wait_for()
        settings.tap()
        field = phone.get_by_role("button", name="Blend moods")
        box = field.bounding_box()
        with phone.expect_request(lambda request: request.method == "POST" and
                                  (request.post_data_json or {}).get("blend")) as tuned:
            field.tap(position={"x": int(box["width"] * .52), "y": int(box["height"] * .22)})
        if tuned.value.post_data_json.get("op") != "play_mood" or phone.locator(".track-row").count():
            raise AssertionError("Touching between moods must start a blend without a song list")
        if phone.evaluate("document.documentElement.scrollWidth") > phone.viewport_size["width"]:
            raise AssertionError("The mood map overflows the phone viewport")
        phone.screenshot(path=str(output / "01g-phone-mood-map.png"))


def verify_playback_outputs(browser, server, output, errors, warnings):
    audible = """() => Array.from(document.querySelectorAll('.player audio'))
        .some(audio => !audio.paused && !audio.muted && audio.currentTime > 0)"""
    mac_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)"
    with browser.new_context(service_workers="block", user_agent=mac_agent) as desktop_context, browser.new_context(
        service_workers="block", has_touch=True, is_mobile=True, viewport={"width": 390, "height": 650}
    ) as phone_context:
        desktop, phone = desktop_context.new_page(), phone_context.new_page()
        phone.add_init_script("""(() => {
            const mediaSession = navigator.mediaSession;
            const install = mediaSession.setActionHandler.bind(mediaSession);
            mediaSession.setActionHandler = (action, handler) => {
                if (action === 'play') window.pressListeningPlay = handler;
                install(action, handler);
            };
        })();""")
        desktop.add_init_script("Object.defineProperty(navigator, 'platform', {value: 'MacIntel'})")
        for client, name in ((desktop, "Desktop fixture"), (phone, "Phone fixture")):
            client.add_init_script(f"localStorage.setItem('listening-device-name', {json.dumps(name)})")
            record_browser_errors(client, errors, warnings)
            client.goto(server.base)
            client.get_by_role("button", name="Play Atlas Air", exact=True).wait_for()
        phone.get_by_role("button", name="Play Atlas Air", exact=True).tap()
        phone.wait_for_function(audible)
        phone.get_by_role("button", name="More actions for Atlas Air").tap()
        phone.get_by_role("menuitem", name="Open in room player").tap()
        phone.get_by_role("dialog", name="Now playing").get_by_role("heading", name="Atlas Air").wait_for()
        phone.get_by_role("button", name="Close now playing").tap()
        desktop.get_by_text("Playing on another device", exact=True).wait_for()
        mac_player = desktop.get_by_role("contentinfo", name="Audio player")
        mac_player.get_by_role("button", name="Listen here", exact=True).wait_for()
        assert desktop.locator(".player audio").count() == 1
        mac_player.get_by_role("button", name="Listen here", exact=True).click()
        desktop.wait_for_function(audible)
        mac_player.get_by_role("button", name="Pause", exact=True).click()
        mac_player.get_by_role("button", name="Play", exact=True).wait_for()
        mac_player.get_by_role("button", name="Play", exact=True).click()
        desktop.wait_for_function(audible)
        phone.get_by_role("button", name="Play Atlas Air", exact=True).tap()
        phone.wait_for_function(audible)
        desktop.wait_for_function("() => !(" + audible + ")()", timeout=8000)

        desktop.get_by_role("button", name="Playback devices", exact=True).click()
        devices = desktop.get_by_role("dialog", name="Playback devices")
        here = devices.locator("label", has_text="This device").get_by_role("checkbox")
        here.check()
        devices.get_by_role("button", name="Use selected devices").click()
        desktop.wait_for_function(audible)
        if not phone.evaluate(audible):
            raise AssertionError("Adding desktop output silenced the phone")

        for section in ("Mood map", "Browse catalogs"):
            phone.get_by_role("button", name="Menu", exact=True).tap()
            phone.get_by_role("button", name=section, exact=True).tap()
            phone.get_by_role("heading", name="How do you feel?" if section == "Mood map" else section, exact=True).wait_for()
            if not phone.evaluate(audible):
                raise AssertionError(f"Opening {section} paused the music")

        dock = phone.get_by_role("contentinfo", name="Audio player")
        bounds = dock.bounding_box()
        if bounds["x"] < 4 or bounds["y"] + bounds["height"] > phone.viewport_size["height"] - 4:
            raise AssertionError(f"The phone dock touches the viewport edge: {bounds}")
        lyrics = dock.get_by_role("button", name="Show lyrics")
        lyrics.tap()
        phone.locator(".lyrics-stage-line").first.wait_for()
        phone.get_by_role("button", name="Close lyrics").tap()
        if not phone.evaluate(audible):
            raise AssertionError("Opening lyrics interrupted the phone's audio")
        trigger = dock.get_by_role("button", name="Open now playing")
        trigger.tap()
        now_playing = phone.get_by_role("dialog", name="Now playing")
        now_playing.get_by_role("heading", name="Atlas Air").wait_for()
        now_playing.get_by_role("button", name="Show lyrics").tap()
        phone.locator(".lyrics-stage-line").first.wait_for()
        phone.get_by_role("button", name="Close lyrics").tap()
        now_playing.get_by_role("button", name="Next track").wait_for()
        now_playing.locator("summary", has_text="Queue").tap()
        now_playing.get_by_role("button", name="Asleep The Smiths").wait_for()
        phone.screenshot(path=str(output / "23-now-playing-phone.png"))
        now_playing.get_by_role("button", name="Close now playing").tap()
        phone.wait_for_function("document.activeElement?.getAttribute('aria-label') === 'Open now playing'")

        mac_player.get_by_role("button", name="Open now playing").click()
        window = desktop.get_by_role("dialog", name="Now playing")
        handle = window.get_by_role("group", name="Player window position")
        body = window.locator(".now-playing-body")
        assert body.bounding_box()["height"] > handle.bounding_box()["height"], "Player body collapsed below the title bar"
        window.get_by_role("button", name="Next track").click(trial=True)

        def drag_window_to(x, y):
            bar = handle.bounding_box()
            desktop.mouse.move(bar["x"] + bar["width"] / 2, bar["y"] + bar["height"] / 2)
            desktop.mouse.down()
            desktop.mouse.move(x, y, steps=12)

        viewport = desktop.viewport_size
        center = handle.bounding_box()
        initial_x = window.bounding_box()["x"]
        drag_window_to(center["x"] + center["width"] / 2 + 90, center["y"] + 22)
        desktop.mouse.up()
        assert window.bounding_box()["x"] > initial_x + 50
        for edge, x, y in (("left", 4, 180), ("right", viewport["width"] - 4, 200),
                           ("top", 480, 4), ("bottom", 480, viewport["height"] - 4)):
            drag_window_to(x, y)
            desktop.locator(f".now-playing-snap-preview[data-dock={edge}]").wait_for()
            desktop.mouse.up()
            assert window.get_attribute("data-dock") == edge
            rect = window.bounding_box()
            dimension = "width" if edge in ("left", "right") else "height"
            assert abs(rect[dimension] - (viewport[dimension] / 2 - 18)) < 2, rect

        window.get_by_role("button", name="Close now playing").click()
        mac_player.get_by_role("button", name="Open now playing").click()
        assert window.get_attribute("data-dock") == "bottom"
        handle.press("Shift+ArrowRight")
        assert window.get_attribute("data-dock") == "right"
        handle.press("Home")
        assert window.get_attribute("data-dock") is None
        handle.dblclick()
        assert abs(window.bounding_box()["x"] - initial_x) < 2
        window.get_by_role("button", name="Close now playing").click()

        # Background playback must still observe remote route changes.
        phone.evaluate("Object.defineProperty(document, 'hidden', { configurable: true, value: true })")
        desktop.get_by_role("button", name="Playback devices", exact=True).click()
        devices.get_by_role("button", name="Listen here only").click()
        phone.wait_for_function("() => !(" + audible + ")()", timeout=8000)
        if not desktop.evaluate(audible):
            raise AssertionError("Switching to desktop-only output stopped the desktop")

        desktop.get_by_role("button", name="Playback devices", exact=True).click()
        devices.get_by_role("checkbox", name="Phone fixture", exact=True).check()
        devices.get_by_role("button", name="Use selected devices").click()
        phone.wait_for_function(audible, timeout=14000)

        phone.wait_for_function("""() => [...document.querySelectorAll('.player audio')]
            .some(audio => !audio.paused && audio.src.startsWith('blob:'))""", timeout=10000)
        output_id = phone.evaluate("sessionStorage.getItem('listening-playback-output')")
        phone.reload()
        phone.get_by_role("button", name="Playback devices", exact=True).tap()
        phone.get_by_role("dialog", name="Playback devices").locator("label", has_text="This device").get_by_role(
            "checkbox", checked=True).wait_for()
        if phone.evaluate("sessionStorage.getItem('listening-playback-output')") != output_id:
            raise AssertionError("Reloading the phone replaced its selected output")

        def phone_presence(expected, timeout=12):
            check = """async ([output, device, expected]) => {
                const headers = {
                    'X-Listening-Output': sessionStorage.getItem('listening-playback-output'),
                    'X-Listening-Output-Name': 'Desktop fixture',
                    'X-Listening-Output-Epoch': sessionStorage.getItem('listening-playback-epoch'),
                };
                const session = await fetch('/api/session', {cache: 'no-store', headers}).then(reply => reply.json());
                const devices = await fetch('/api/devices', {cache: 'no-store'}).then(reply => reply.json());
                return session.outputs.some(item => item.id === output) === expected &&
                    devices.devices.some(item => item.id === device) === expected;
            }"""
            wait_for(f"phone presence to be {expected}",
                     lambda: desktop.evaluate(check, [output_id, phone_id, expected]), timeout=timeout)

        phone_id = phone.evaluate("localStorage.getItem('listening-device-id')")
        phone_presence(True)

        # Minimized audio keeps playing. A minimized idle tab goes offline until it returns.
        phone.evaluate("""() => {
            Object.defineProperty(document, 'hidden', { configurable: true, value: true });
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        phone.wait_for_function(audible)
        phone_presence(True)

        mac_player.get_by_role("button", name="Pause", exact=True).click()
        phone.wait_for_function("() => !(" + audible + ")()", timeout=10000)
        phone_presence(False, timeout=42)
        phone.wait_for_function("""() =>
            [...document.querySelectorAll('.player audio')].every(audio => !audio.hasAttribute('src'))""")
        phone.evaluate("window.pressListeningPlay()")
        phone_presence(True)
        phone.wait_for_function(audible)
        desktop.wait_for_function(audible)

        desktop.get_by_role("button", name="Playback devices", exact=True).click()
        devices.get_by_role("button", name="Listen here only").click()
        phone.wait_for_function("() => !(" + audible + ")()", timeout=10000)
        phone.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        phone_presence(False)
        phone.evaluate("""() => {
            Object.defineProperty(document, 'hidden', { configurable: true, value: false });
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        phone_presence(True)
        phone.close(run_before_unload=True)
        phone_presence(False)
        assert desktop.evaluate(audible), "Closing the phone paused playback on the desktop"


def verify_mobile_queue_and_shuffle(browser, server, errors, warnings):
    with browser.new_context(service_workers="block", has_touch=True, is_mobile=True,
                             viewport={"width": 390, "height": 844}) as context:
        phone = context.new_page()
        record_browser_errors(phone, errors, warnings)
        phone.goto(server.base)
        phone.get_by_role("button", name="Play Atlas Air", exact=True).tap()
        phone.get_by_role("button", name="More actions for Blame", exact=True).tap()
        phone.get_by_role("menuitem", name="Add to queue", exact=True).tap()
        phone.get_by_text("Added to queue", exact=True).wait_for()
        status, session = server.request("GET", "/api/session")
        assert status == 200 and session["upcoming"][0]["title"] == "Blame", session["upcoming"][:1]

        player = phone.get_by_role("contentinfo", name="Audio player")
        shuffle = player.get_by_role("button", name="Shuffle play", exact=True)
        pressed = shuffle.get_attribute("aria-pressed")
        shuffle.tap()
        phone.wait_for_function("previous => document.querySelector('.player-shuffle').getAttribute('aria-pressed') !== previous",
                                arg=pressed)
        player.get_by_role("button", name="Open now playing", exact=True).tap()
        modal = phone.get_by_role("dialog", name="Now playing")
        modal_shuffle = modal.get_by_role("button", name="Shuffle play", exact=True)
        assert modal_shuffle.is_visible()
        modal.get_by_text("Queue", exact=True).tap()
        assert modal.locator(".now-playing-queue li").first.get_by_text("Blame", exact=True).is_visible()
        modal_shuffle.tap()
        phone.wait_for_function("previous => document.querySelector('.player-shuffle').getAttribute('aria-pressed') === previous",
                                arg=pressed)


def verify_apple_background_audio(browser, server, errors, warnings):
    user_agent = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1")
    with browser.new_context(user_agent=user_agent, service_workers="block", has_touch=True,
                             is_mobile=True, viewport={"width": 390, "height": 844}) as context:
        phone = context.new_page()
        record_browser_errors(phone, errors, warnings)
        phone.add_init_script("""localStorage.setItem('listening-crossfade', '5');
            localStorage.setItem('listening-normalize', '1');
            const AudioContext = window.AudioContext;
            if (AudioContext) window.AudioContext = new Proxy(AudioContext, {
                construct() { throw Error('The Apple mobile player must use direct media output'); }
            });""")
        # Keep both the original next song and the later manual addition
        # out of the prefetch cache so this exercises the HTTP retry path.
        phone.route(re.compile(r"/api/audio/(?:2|4)(?:\?|$)"), lambda route: route.continue_()
                    if "range" in route.request.headers else route.abort("timedout"))
        phone.goto(server.base)
        phone.get_by_role("button", name="Play Atlas Air", exact=True).tap()
        phone.wait_for_function("""() => {
            const sources = document.querySelectorAll('.player audio');
            return sources.length === 1 && sources[0].currentTime > 0 && !sources[0].paused;
        }""")

        advance_attempts = []

        def interrupt_first_advance(route):
            if route.request.method == "POST" and route.request.post_data_json.get("op") == "next":
                advance_attempts.append(route.request.post_data_json.get("expected_ref"))
                if len(advance_attempts) == 1:
                    status, _ = server.request("POST", "/api/session", {"op": "enqueue", "track_id": 4})
                    if status != 200:
                        raise AssertionError("Could not change the queue while its advance was interrupted")
                    route.fulfill(status=503, json={"error": "Fixture temporarily disconnected"})
                    return
            route.continue_()

        phone.route("**/api/session", interrupt_first_advance)
        phone.evaluate("""() => {
            window.playerBeforeBackground = document.querySelector('.player audio');
            Object.defineProperty(document, 'hidden', { configurable: true, value: true });
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        status, _ = server.request("POST", "/api/session", {"op": "seek", "position": 61})
        if status != 200:
            raise AssertionError("Could not seek the fixture toward its track boundary")
        phone.wait_for_function("""() => {
            const audio = document.querySelector('.player audio');
            return audio === window.playerBeforeBackground && !audio.paused && audio.currentTime > 0
                && document.querySelector('.player-text strong').textContent === 'Blame';
        }""", timeout=15000)
        if advance_attempts != [1, 1]:
            raise AssertionError(f"Expected one retry of the ended track, got {advance_attempts}")
        phone.evaluate("""() => {
            delete document.hidden;
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        phone.wait_for_function("""() => {
            const audio = document.querySelector('.player audio');
            return audio === window.playerBeforeBackground && !audio.paused && audio.currentTime > 1;
        }""")


def verify_standby_buffer(browser, server, errors, warnings):
    select_output = """async () => {
        const output = sessionStorage.getItem('listening-playback-output');
        return (await fetch('/api/session', {
            method: 'POST',
            headers: {'X-Listening-Output': output, 'Content-Type': 'application/json',
                          'X-Listening-Output-Epoch': sessionStorage.getItem('listening-playback-epoch')},
            body: JSON.stringify({op: 'outputs', devices: [output]})
        })).ok;
    }"""
    with browser.new_context(service_workers="block") as context:
        desktop = context.new_page()
        desktop.clock.install()
        record_browser_errors(desktop, errors, warnings)
        next_requests = []
        desktop.on("request", lambda request: next_requests.append(request.resource_type)
                   if urlsplit(request.url).path == "/api/audio/2" else None)
        desktop.goto(server.base)
        desktop.get_by_role("button", name="Play Atlas Air", exact=True).click()
        assert desktop.evaluate(select_output)
        desktop.wait_for_function("""() => {
            const standby = document.querySelectorAll('.player audio')[1];
            return standby?.src.startsWith('blob:') && standby.readyState >= 2;
        }""", timeout=12000)
        assert next_requests == ["fetch"], next_requests
        standby = desktop.locator(".player audio").nth(1)
        buffered_url = standby.evaluate("element => element.src")

        status, _ = server.request("POST", "/api/session", {"op": "seek", "position": 61})
        assert status == 200
        desktop.wait_for_function("""() => {
            const standby = document.querySelectorAll('.player audio')[1];
            return document.querySelector('.player-text strong')?.textContent === 'Asleep'
                && !standby.paused && standby.currentTime > 0;
        }""", timeout=12000)
        assert standby.evaluate("element => element.src") == buffered_url

        # Removing this output detaches both media elements immediately. The
        # separately held buffer remains reusable during the handoff grace.
        with browser.new_context(service_workers="block") as other_context:
            other = other_context.new_page()
            other.goto(server.base)
            other.get_by_role("button", name="Playback devices", exact=True).wait_for()
            assert other.evaluate(select_output)
            desktop.wait_for_function("""() =>
                [...document.querySelectorAll('.player audio')].every(audio => !audio.hasAttribute('src'))""",
                timeout=12000)
            can_read = """async url => {
                try {
                    const response = await fetch(url);
                    await response.body.cancel();
                    return response.ok;
                } catch {
                    return false;
                }
            }"""
            assert desktop.evaluate(can_read, buffered_url)
            desktop.clock.fast_forward(301_000)
            assert not desktop.evaluate(can_read, buffered_url)

    with browser.new_context(service_workers="block") as context:
        desktop = context.new_page()
        desktop.add_init_script("""(() => {
            localStorage.setItem('listening-buffer-ahead', '2');
            window.oversizedAttempts = 0;
            const request = window.fetch.bind(window);
            window.fetch = (url, options) => {
                if (new URL(String(url), location.origin).pathname !== '/api/audio/2') return request(url, options);
                window.oversizedAttempts++;
                return Promise.resolve(new Response(new Uint8Array([1]), {
                    headers: {'Content-Type': 'audio/mpeg', 'Content-Length': String(129 * 1024 * 1024)}
                }));
            };
        })();""")
        desktop.goto(server.base)
        desktop.get_by_role("button", name="Play Atlas Air", exact=True).click()
        assert desktop.evaluate(select_output)
        desktop.wait_for_function("""() => {
            const standby = document.querySelectorAll('.player audio')[1];
            return standby?.src && new URL(standby.src).pathname === '/api/audio/2' && standby.readyState >= 2;
        }""", timeout=12000)
        status, _ = server.request("POST", "/api/session", {"op": "queue_remove", "index": 1})
        assert status == 200
        desktop.wait_for_function("""() => performance.getEntriesByType('resource')
            .some(entry => new URL(entry.name).pathname === '/api/audio/4' && entry.initiatorType === 'fetch')""", timeout=12000)
        assert desktop.evaluate("window.oversizedAttempts") == 1


def verify_insights(browser, server, output, errors, warnings):
    idle_with_history = """() => {
        const down = document.querySelector('.insights-rate');
        return /^0 kb/.test(down?.querySelector('.insights-value')?.textContent.trim() ?? '') &&
            [...(down?.querySelectorAll('.insights-graph i') ?? [])]
                .some(bar => parseFloat(bar.style.height) > 4);
    }"""
    with browser.new_context(service_workers="block", has_touch=True, is_mobile=True,
                             viewport={"width": 390, "height": 844}) as context:
        phone = context.new_page()
        record_browser_errors(phone, errors, warnings)
        polls = []
        phone.on("request", lambda request: polls.append(request) if urlsplit(request.url).path == "/api/insights" else None)
        phone.goto(server.base)
        phone.get_by_role("button", name="Play Atlas Air", exact=True).wait_for()
        assert phone.evaluate("""async () => {
            const output = sessionStorage.getItem('listening-playback-output');
            return (await fetch('/api/session', {
                method: 'POST',
                headers: {'X-Listening-Output': output, 'Content-Type': 'application/json',
                          'X-Listening-Output-Epoch': sessionStorage.getItem('listening-playback-epoch')},
                body: JSON.stringify({op: 'outputs', devices: [output]})
            })).ok;
        }""")
        phone.get_by_role("button", name="Play Atlas Air", exact=True).tap()
        assert not polls
        phone.get_by_role("button", name="Menu", exact=True).tap()
        phone.get_by_role("button", name="Insights", exact=True).tap()
        phone.get_by_role("heading", name="Playback traffic", exact=True).wait_for()
        phone.get_by_text("3 songs queued", exact=True).wait_for()
        phone.get_by_text("Asleep", exact=True).wait_for()
        phone.wait_for_function("""() => [...document.querySelectorAll('.insights-value')]
            .filter(el => /^[0-9]/.test(el.textContent) && !/^0 kb/.test(el.textContent)).length === 2""")
        assert phone.evaluate("document.documentElement.scrollWidth === innerWidth")
        phone.screenshot(path=str(output / "30-insights.png"))
        phone.get_by_role("button", name="Open now playing").tap()
        panel = phone.get_by_role("dialog", name="Now playing")
        panel.get_by_text("Queue", exact=True).wait_for()
        panel.get_by_role("button", name="Next track", exact=True).tap()
        panel.get_by_role("button", name="Close now playing").tap()
        phone.get_by_text("2 songs queued", exact=True).wait_for()
        phone.wait_for_function(idle_with_history, timeout=12000)
        phone.get_by_role("button", name="Menu", exact=True).tap()
        phone.get_by_role("button", name="Library", exact=True).first.tap()
        phone.get_by_role("heading", name="Playback traffic").wait_for(state="hidden")
        number_of_polls = len(polls)
        phone.wait_for_timeout(1200)
        assert len(polls) == number_of_polls
        phone.get_by_role("button", name="Menu", exact=True).tap()
        phone.get_by_role("button", name="Insights", exact=True).tap()
        phone.wait_for_function(idle_with_history, timeout=12000)


def verify_hiking(browser, server, output, errors, warnings):
    with browser.new_context(viewport={"width": 1440, "height": 900}) as context:
        desktop = context.new_page()
        record_browser_errors(desktop, errors, warnings)
        desktop.goto(server.base + "/#hiking-plan")
        landscape = desktop.locator(".hike-landscape")
        landscape.wait_for()
        assert landscape.get_attribute("aria-hidden") == "true"
        assert desktop.get_by_role("button", name="Make mixes").is_visible()
        desktop.screenshot(path=str(output / "29-camping-mode-desktop.png"))
    with browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True) as context:
        phone = context.new_page()
        record_browser_errors(phone, errors, warnings)
        phone.goto(server.base + "/#hiking-plan")
        phone.get_by_role("heading", name="Camping mode").wait_for()
        phone.locator(".hike-landscape").wait_for()
        phone.wait_for_function("navigator.serviceWorker.controller !== null")
        phone.get_by_role("button", name="90 songs").tap()
        phone.get_by_role("button", name="Make mixes").tap()
        phone.get_by_text("4 songs across", exact=False).wait_for()
        assert phone.evaluate("document.documentElement.scrollWidth === innerWidth")
        phone.screenshot(path=str(output / "29-camping-mode.png"))

        # Interrupt a later track after at least one complete song was saved.
        downloads = []
        disconnected = []

        def interrupt(route):
            downloads.append(route.request.url)
            if len(downloads) == 2:
                disconnected.append(route.request.url)
                route.abort("connectionfailed")
            else:
                route.continue_()

        hike_audio = re.compile(r"/api/audio/[1-9][0-9]*\?recording=[a-f0-9]{64}&format=aac$")
        phone.route(hike_audio, interrupt)
        phone.get_by_role("button", name="Save on this device").tap()
        phone.get_by_role("button", name="Resume download").wait_for(timeout=45000)
        assert len(disconnected) == 1 and len(downloads) == 2, phone.get_by_role("alert").all_text_contents()
        first = downloads[0]
        phone.unroute(hike_audio)
        resumed = []
        phone.on("request", lambda request: resumed.append(request.url) if "format=aac" in request.url else None)
        phone.reload()
        phone.get_by_role("button", name="Resume download").tap()
        phone.get_by_text("Your trip is ready offline.").wait_for(timeout=60000)
        assert first not in resumed, resumed
        assert phone.get_by_role("link", name="Open saved trip").get_attribute("href") == "#camping"

        phone.close()
        context.set_offline(True)
        phone = context.new_page()
        record_browser_errors(phone, errors, warnings)
        phone.goto(server.base + "/")
        phone.get_by_text("saved on this device", exact=False).wait_for()
        phone.get_by_role("group", name="Choose a mix").get_by_role("button").first.tap()
        phone.wait_for_function("""() => {
            const audio = document.querySelector('audio[aria-label="Offline audio player"]');
            return audio?.src.startsWith('blob:') && audio.readyState >= 1;
        }""")
        phone.locator('audio[aria-label="Offline audio player"]').evaluate("audio => audio.play()")
        phone.wait_for_function("document.querySelector('audio')?.currentTime > 0.1")

        # Signing out in another tab emits these two storage changes.
        other = context.new_page()
        other.goto(server.base + "/#hiking")
        other.evaluate("""() => {
            localStorage.removeItem('listening-hiking-account');
            localStorage.setItem('listening-account-change', String(Date.now()));
        }""")
        phone.get_by_text("No trip saved on this device.").wait_for()
        assert phone.locator('audio[aria-label="Offline audio player"]').count() == 0


def verify_queue_buffer(browser, server, errors, warnings):
    with browser.new_context(user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15",
                             service_workers="block", has_touch=True, is_mobile=True,
                             viewport={"width": 390, "height": 844}) as context:
        phone = context.new_page()
        record_browser_errors(phone, errors, warnings)
        phone.goto(server.base)
        phone.get_by_role("button", name="Play Atlas Air", exact=True).tap()
        selected = phone.evaluate("""async () => {
            const output = sessionStorage.getItem('listening-playback-output');
            return (await fetch('/api/session', {
                method: 'POST',
                headers: {'X-Listening-Output': output, 'Content-Type': 'application/json',
                          'X-Listening-Output-Epoch': sessionStorage.getItem('listening-playback-epoch')},
                body: JSON.stringify({op: 'outputs', devices: [output]})
            })).ok;
        }""")
        if not selected:
            raise AssertionError("Could not select the phone as the fixture output")
        phone.get_by_role("button", name="Menu", exact=True).tap()
        phone.get_by_role("button", name="Settings", exact=True).tap()
        phone.get_by_role("tab", name="Playback", exact=True).tap()
        phone.get_by_role("combobox", name="Buffer ahead").tap()
        phone.get_by_role("option", name="3 upcoming songs").tap()
        phone.get_by_role("button", name="Menu", exact=True).tap()
        phone.get_by_role("button", name="Insights", exact=True).tap()
        phone.get_by_text("4 songs buffered on this device.", exact=True).wait_for()

        def playing(title, minimum=0, cached=False):
            phone.wait_for_function("""({title, minimum, cached}) =>
                document.querySelector('.player-text strong')?.textContent === title &&
                [...document.querySelectorAll('.player audio')].some(a => !a.paused &&
                    a.currentTime > minimum && (!cached || a.src.startsWith('blob:')))""",
                arg={"title": title, "minimum": minimum, "cached": cached})

        phone.get_by_role("button", name="Show lyrics").tap()
        line = phone.locator('.lyrics-stage-line[data-t="48"]')
        line.wait_for()
        # A lost route can leave the network interface online. Lyrics already
        # on screen still use the buffered recording's clock and seek control.
        phone.route("**/api/session", lambda route: route.abort("connectionfailed"))
        line.tap()
        playing("Atlas Air", 48, cached=True)
        phone.wait_for_function("""() => document.querySelector('.lyrics-stage-line.is-current')?.dataset.t === '48'""")
        phone.locator('.lyrics-stage-line[data-t="58"]').tap()
        playing("Atlas Air", 58, cached=True)
        phone.get_by_role("button", name="Close lyrics").tap()
        playing("Asleep", 0.1, cached=True)
        context.set_offline(True)
        phone.get_by_role("button", name="Open now playing").tap()
        player = phone.get_by_role("dialog", name="Now playing")
        player.get_by_role("button", name="Pause", exact=True).tap()
        player.get_by_role("button", name="Play", exact=True).tap()
        playing("Asleep", 0.1, cached=True)
        player.get_by_role("button", name="Next track", exact=True).tap()
        playing("Paradise Circus", 0.1, cached=True)
        status, before = server.request("GET", "/api/session")
        if status != 200 or before["current_ref"] != 1:
            raise AssertionError("An offline advance changed the shared queue")
        phone.unroute("**/api/session")
        context.set_offline(False)
        wait_for("buffered position to rejoin the room",
                 lambda: server.request("GET", "/api/session")[1]["current_ref"] == 3, timeout=12)
        playing("Paradise Circus", 0.1, cached=True)


def verify_artist_connections(browser, server, output, errors, warnings):
    ids = {name: f"{index:08x}-0000-0000-0000-000000000000"
           for index, name in enumerate(("song", "band", "member", "album", "solo", "project"), 1)}

    def node(name, kind, title, detail):
        return {"id": ids[name], "kind": kind, "title": title, "detail": detail}

    def graph(root, *branches):
        return {"state": "ready", "root": root, "source": "MusicBrainz",
                "edges": [{"label": label, "node": item, "note": note} for label, item, note in branches]}

    song = node("song", "recording", "Atlas Air", "Recording")
    band = node("band", "artist", "Massive Attack", "Group")
    member = node("member", "artist", "Robert Del Naja", "Person")
    album = node("album", "release-group", "Heligoland", "Album")
    solo = node("solo", "release-group", "Fixture solo album", "Album")
    project = node("project", "artist", "Fixture side project", "Group")
    paths = {
        "/api/connections/song/1": graph(song, ("Credited artist", band, ""), ("Appears on", album, "")),
        "/api/connections/album/1": graph(album, ("Credited artist", band, "")),
        f"/api/connections/artist/{band['id']}": graph(band, ("Band member", member, "Vocals · From 1988")),
        f"/api/connections/artist/{member['id']}": graph(member, ("Member of", band, ""),
                                                        ("Solo release", solo, ""),
                                                        ("Collaboration project", project, "")),
    }

    song_path = "/api/connections/song/1"
    band_path = f"/api/connections/artist/{band['id']}"

    for phone in (False, True):
        waiting = {}
        delayed = {song_path, band_path}
        reads = {}

        def connections(route):
            path = urlparse(route.request.url).path
            reads[path] = reads.get(path, 0) + 1
            if path in delayed:
                waiting[path] = route
            else:
                route.fulfill(json=paths[path])

        def release(page, path):
            for _ in range(100):
                if path in waiting:
                    break
                page.wait_for_timeout(10)
            delayed.remove(path)
            waiting.pop(path).fulfill(json=paths[path])

        def centered(dialog):
            assert dialog.evaluate("""el => {
                const {left, top, width, height} = el.getBoundingClientRect();
                return Math.abs(left + width / 2 - document.documentElement.clientWidth / 2) < 2 &&
                    Math.abs(top + height / 2 - window.innerHeight / 2) < 2;
            }"""), "Song dialog is off center"
        with browser.new_context(viewport={"width": 390 if phone else 1440, "height": 844 if phone else 900},
                                 has_touch=phone, is_mobile=phone, reduced_motion="reduce" if phone else "no-preference",
                                 service_workers="block") as context:
            page = context.new_page()
            record_browser_errors(page, errors, warnings)
            page.route("**/api/connections/**", connections)
            page.route("**/api/metadata/1", lambda route: route.fulfill(json={
                "title": "Atlas Air", "artist": "Massive Attack", "album": "Heligoland",
                "first_release": "2010", "isrc": "GBABC1000001", "genre": "electronic",
                "genre_source_label": "local audio tags", "provenance": "Local library",
                "lists": ["Favorites", "Late night"], "identifier": "fixture-atlas-air",
                "source": "MusicBrainz", "musicbrainz_url": "https://musicbrainz.org/",
                "connections": [{"musicbrainz_id": ids["project"], "relationship": "Remix",
                                 "title": "Fixture side project", "artist": "Fixture artist"}],
            }))
            page.goto(server.base)
            if phone:
                page.get_by_role("button", name="Play Atlas Air", exact=True).tap()
                page.get_by_role("button", name="Open now playing").tap()
                page.get_by_role("button", name="Artist credits", exact=True).tap()
            else:
                page.get_by_role("button", name="Song details for Atlas Air", exact=True).click()
                page.get_by_role("tab", name="Details", exact=True).press("ArrowRight")
                credits_tab = page.get_by_role("tab", name="Credits", exact=True)
                credits_tab.press("Enter")
                assert credits_tab.evaluate("(el) => document.activeElement === el")
            dialog = page.get_by_role("dialog", name="Atlas Air", exact=True)
            loading = dialog.get_by_role("status").filter(has_text="Looking up credits")
            loading.wait_for()
            centered(dialog)
            assert dialog.locator(".connections-map-loading .connections-node").count() == 5
            assert not dialog.get_by_role("region", name="Catalog connections").count()
            if phone:
                assert loading.locator(".room-spinner").evaluate("(el) => getComputedStyle(el).animationName") == "none"
            release(page, song_path)
            dialog.get_by_role("group", name="Connections around Atlas Air").wait_for()
            centered(dialog)
            band_button = dialog.get_by_role("button", name="Explore Massive Attack · Credited artist")
            if phone:
                assert band_button.evaluate("(el) => getComputedStyle(el).animationName") == "none"
                assert dialog.evaluate("(el) => el.scrollWidth <= el.clientWidth + 1"), "Phone graph overflows"
                band_button.tap()
            else:
                band_button.focus()
                assert dialog.get_by_role("complementary", name="Credit details").get_by_role(
                    "heading", name="Massive Attack").is_visible()
                band_button.press("Enter")
            loading.wait_for()
            assert dialog.get_by_role("heading", name="Massive Attack", level=3).is_visible()
            assert dialog.locator(".connections-map-loading").is_visible()
            centered(dialog)
            page.screenshot(path=str(output / f"31-credits-loading-{'phone' if phone else 'desktop'}.png"),
                            animations="disabled")
            release(page, band_path)
            member_button = dialog.get_by_role("button", name="Explore Robert Del Naja · Band member")
            member_button.wait_for()
            if phone:
                member_button.tap()
            else:
                member_button.focus()
                assert dialog.get_by_text("Vocals · From 1988", exact=True).is_visible()
                member_button.press("Enter")
            dialog.get_by_role("button", name="Explore Fixture solo album · Solo release").wait_for()
            assert dialog.get_by_role("button", name="Explore Fixture side project · Collaboration project").is_visible()
            filename = "31-artist-connections-phone.png" if phone else "31-artist-connections-desktop.png"
            page.screenshot(path=str(output / filename), animations="disabled")
            graph_palette = dialog.evaluate("(el) => getComputedStyle(el).backgroundColor")
            room_palette = page.locator(".main-area").evaluate("(el) => getComputedStyle(el).backgroundColor")
            assert graph_palette == room_palette, "Credits use a different background from the room"
            action_labels = ["Details", "Credits", "Artist", "Track list", "Album"]
            graph_nav = dialog.get_by_role("tablist", name="Song views")
            assert graph_nav.get_by_role("tab").all_text_contents() == action_labels
            graph_nav_position = graph_nav.bounding_box()
            if not phone:
                back = dialog.get_by_role("button", name=re.compile(r"^Back to "))
                assert not back.inner_text().strip() and back.locator("svg[aria-hidden=true] path").count() == 1
                back.click()
                member_button.wait_for()
                assert reads[band_path] == 1, "Back refetched the artist graph"
            dialog.get_by_role("tab", name="Details", exact=True).click()
            info = page.get_by_role("dialog", name="Atlas Air")
            info.get_by_role("heading", name="Recording details", exact=True).wait_for()
            centered(info)
            assert info.evaluate("(el) => getComputedStyle(el).backgroundColor") == graph_palette
            assert info.evaluate("(el) => el.scrollWidth <= el.clientWidth + 1"), "Song details overflow"
            facts = info.locator(".song-facts dt").all_text_contents()
            assert facts == ["Album", "First release", "ISRC", "Genre", "Source", "In lists", "Identifier", "Catalog"]
            info_nav = info.get_by_role("tablist", name="Song views")
            actions = info_nav.get_by_role("tab")
            assert actions.all_text_contents() == action_labels
            info_nav_position = info_nav.bounding_box()
            assert abs(info_nav_position["y"] - graph_nav_position["y"]) < 2, "Song navigation jumps on mode switch"
            assert actions.first.get_attribute("aria-selected") == "true"
            assert info.locator(".song-facts > div").evaluate_all(
                "(rows) => rows.every(row => getComputedStyle(row).borderBlockStartWidth === '1px')"
            ), "Recording facts are missing their row dividers"
            assert actions.locator("svg[aria-hidden=true]").count() == len(action_labels)
            page.screenshot(path=str(output / ("32-song-details-phone.png" if phone else "32-song-details-desktop.png")))
            grip = info.get_by_role("group", name="Window position")
            initial = info.bounding_box()
            grip.press("ArrowRight")
            page.wait_for_function("x => document.querySelector('.connections-dialog').getBoundingClientRect().x > x + 6", arg=initial["x"])
            grip.press("Home")
            page.wait_for_function("x => Math.abs(document.querySelector('.connections-dialog').getBoundingClientRect().x - x) < 2", arg=initial["x"])
            if phone:
                page.keyboard.press("Escape")
                info.wait_for(state="detached")
                page.wait_for_function("document.activeElement?.getAttribute('aria-label') === 'Open now playing'")
            else:
                info.get_by_role("tab", name="Album", exact=True).click()
                dialog.get_by_role("group", name="Connections around Heligoland").wait_for()

                info_trigger = dialog.get_by_role("tab", name="Credits", exact=True)
                info_trigger.click()
                dialog.get_by_role("group", name="Connections around Atlas Air").wait_for()
                assert reads[song_path] == 1, "Reopening Credits refetched the recording graph"
                assert not loading.is_visible()

                def reopen():
                    dialog.get_by_role("button", name="Close song information").click()
                    page.get_by_role("button", name="Song details for Atlas Air", exact=True).click()
                    info_trigger.click()

                # The server resolves an unmatched recording to the artist at the same URL.
                local = {"title": "Atlas Air", "album": "Heligoland", "artist": "Massive Attack"}
                paths[song_path] = {**graph(band, ("Band member", member, "")), "library": local}
                reopen()
                member_button.wait_for()
                assert not dialog.get_by_role("button", name=re.compile(r"^Back to ")).count()
                member_button.click()
                dialog.get_by_role("button", name=re.compile(r"^Back to ")).click()
                member_button.wait_for()
                paths[song_path] = {"state": "unmatched", "title": local["title"],
                    "artist": local["artist"], "credit": None, "library": local, "retry": True}
                reopen()
                catalog = dialog.get_by_role("region", name="Catalog connections")
                catalog.get_by_text("Heligoland").wait_for()
                assert not dialog.get_by_text("no confirmed match", exact=False).count()
                paths["/api/connections/song/1"] = {**graph(band, ("Band member", member, "")), "library": local}
                dialog.get_by_role("button", name="Retry artist lookup").click()
                member_button.wait_for()
                # The phone pass starts with the documented recording again.
                paths["/api/connections/song/1"] = graph(song, ("Credited artist", band, ""), ("Appears on", album, ""))
                bar = dialog.get_by_role("group", name="Window position").bounding_box()
                x, y = bar["x"] + bar["width"] / 2, bar["y"] + 8
                before = dialog.bounding_box()
                page.mouse.move(x, y)
                page.mouse.down()
                page.mouse.move(x + 70, y + 36, steps=8)
                page.mouse.up()
                assert dialog.bounding_box()["x"] > before["x"] + 50, "Pointer drag did not move the window"
                dialog.get_by_role("tab", name="Details", exact=True).click()
                assert info.get_attribute("data-floating") == "true"
                info.get_by_role("group", name="Window position").dblclick()
                assert info.get_attribute("data-floating") is None
                centered(info)
                page.set_viewport_size({"width": 980, "height": 540})
                centered(info)
                page.set_viewport_size({"width": 980, "height": 300})
                info_trigger.click()
                member_button.focus()
                member_button.press("Enter")
                focused_heading = dialog.get_by_role("heading", name="Robert Del Naja", level=3)
                focused_heading.wait_for()
                assert focused_heading.evaluate("""el => el === document.activeElement &&
                    el.getBoundingClientRect().top >= el.closest('[role=dialog]').getBoundingClientRect().top"""), (
                    "Opening a branch by keyboard left its heading above the scroll area")


def verify_room_layout(browser, server, output):
    for phone in (False, True):
        with browser.new_context(viewport={"width": 390 if phone else 1600, "height": 900},
                                 has_touch=phone, is_mobile=phone) as context:
            page = context.new_page()
            page.goto(server.base)
            library = page.get_by_role("heading", name="Library", exact=True)
            library.wait_for()
            origin = library.bounding_box()
            library_style = library.evaluate("(el) => getComputedStyle(el).fontSize")
            views = [
                ("Radio", "Radio"), ("Mood map", "How do you feel?"), ("Activities", "Activities"), ("Insights", "Insights"),
                ("Camping mode", "Camping mode"), ("Deep Dive", "Deep Dive"),
                ("Browse catalogs", "Browse catalogs"), ("Requests", "Requests"),
                ("Jobs", "Jobs"), ("Settings", "Settings"),
            ]
            for menu_label, title in views:
                if phone:
                    page.get_by_role("button", name="Menu", exact=True).tap()
                page.get_by_role("navigation", name="Main", exact=True).get_by_role("button", name=menu_label, exact=True).click()
                heading = page.get_by_role("heading", name=title, exact=True, level=1)
                heading.wait_for()
                box = heading.bounding_box()
                assert abs(box["x"] - origin["x"]) < 2 and abs(box["y"] - origin["y"]) < 2, (
                    f"{title} at {box['x']}, {box['y']} drifts from Library at {origin['x']}, {origin['y']}")
                assert heading.evaluate("(el) => getComputedStyle(el).fontSize") == library_style
                assert page.locator(".main-area").evaluate("(el) => el.scrollWidth <= el.clientWidth + 1")
                if menu_label in {"Mood map", "Radio", "Insights"}:
                    layout = "phone" if phone else "desktop"
                    page.screenshot(path=str(output / f"33-{menu_label.lower().replace(' ', '-')}-{layout}.png"),
                                    animations="disabled")


def verify_tablet(browser, server, output):
    # Use touch input as well as a tablet viewport. A Mac window of the same
    # width deliberately keeps the desktop sidebar and full transport.
    with browser.new_context(has_touch=True, is_mobile=True,
                             viewport={"width": 1194, "height": 834}) as context:
        tablet = context.new_page()
        tablet.goto(server.base)
        tablet.locator(".playlist-card").filter(has_text="Evening").tap()
        tablet.get_by_role("heading", name="evening", exact=True).wait_for()
        tablet.locator(".track-row").first.wait_for()
        for name, width, height in (("landscape", 1194, 834), ("portrait", 834, 1194)):
            tablet.set_viewport_size({"width": width, "height": height})
            nav = tablet.locator(".sidebar").bounding_box()
            player = tablet.locator(".player").bounding_box()
            if not (nav["y"] > height * 0.8 and abs(nav["y"] - player["y"]) < 2
                    and nav["x"] + nav["width"] <= player["x"] + 1
                    and player["x"] + player["width"] <= width + 1):
                raise AssertionError(f"{name} dock is clipped or overlaps: {nav}, {player}")
            tablet.locator(".playlist-banner-art").wait_for(state="visible")
            if tablet.evaluate("document.querySelector('main').scrollWidth > document.querySelector('main').clientWidth"):
                raise AssertionError(f"{name} playlist overflows horizontally")
            tablet.screenshot(path=str(output / f"18-tablet-{name}.png"))
            tablet.get_by_role("button", name="Expand player controls").tap()
            tablet.get_by_role("button", name="Open queue").tap()
            tablet.get_by_role("button", name="Close queue").tap()
            tablet.get_by_role("button", name="Collapse player controls").tap()
        tablet.get_by_role("tab", name="Need a copy").tap()
        tablet.get_by_role("button", name="Missing Song", exact=True).tap()
        tablet.get_by_role("dialog").get_by_role("heading", name="Missing Song").wait_for()
        youtube = tablet.get_by_role("link", name="Find on YouTube")
        youtube.wait_for()
        link = urlsplit(youtube.get_attribute("href"))
        if (link.scheme, link.netloc, link.path) != ("https", "www.youtube.com", "/results") or \
                not parse_qs(link.query).get("search_query", [""])[0].endswith("Missing Song"):
            raise AssertionError("Missing song should link to a YouTube search by artist and title")
        tablet.get_by_role("button", name="Close song information").tap()
        tablet.get_by_role("button", name="Menu").tap()
        tablet.get_by_role("button", name="Settings", exact=True).tap()
        tablet.get_by_role("heading", name="Settings", exact=True).wait_for()


def verify_lan_pair_again(browser):
    """Exercise real cross-site navigation, Strict cookies and a revoked credential."""
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from test_http_server import ServerFixture

    class RoomFixture(ServerFixture):
        pass

    RoomFixture.setUpClass()
    fixture = RoomFixture()
    page = browser.new_page(service_workers="block")
    try:
        first = f"http://127.0.0.1:{fixture.lan_port}"
        second = f"http://localhost:{fixture.lan_port}"

        def submit(server_url):
            page.get_by_role("heading", name="Pair this device").wait_for(timeout=12000)
            page.get_by_label("Pairing link").fill(f"{server_url}/?token={fixture.token}")
            page.get_by_role("button", name="Pair again", exact=True).click()
            page.wait_for_url(server_url + "/", timeout=12000)
            page.get_by_role("heading", name="Catalog is still loading").wait_for(timeout=12000)

        page.goto(first + "/")
        submit(second)
        device = page.evaluate("localStorage.getItem('listening-device-id')")
        status, _, _ = fixture.request(fixture.loop_port, "POST", "/api/devices/revoke", {"id": device})
        if status != 200:
            raise AssertionError("Could not revoke the disposable browser credential")
        page.get_by_role("heading", name="Pairing expired").wait_for(timeout=12000)
        page.get_by_role("button", name="Pair again", exact=True).click()
        submit(first)
    finally:
        page.close()
        RoomFixture.tearDownClass()


def install_youtube_fixture(page):
    # Model the player interface so transport is deterministic on a private
    # LAN, including when the machine has no outbound internet access.
    page.add_init_script("""window.YT = {
      PlayerState: {ENDED: 0, PLAYING: 1, PAUSED: 2, CUED: 5},
      Player: class {
        constructor(el, options) {this.events = options.events; this.n = 0;
          if (window.holdRadioReady) window.releaseRadioReady = () => this.events.onReady();
          else setTimeout(() => this.events.onReady(), 0);}
        state(value) {this.value = value; this.events.onStateChange({data: value});}
        getCurrentTime() {return this.position;}
        getPlayerState() {return this.value;}
        getPlaylistIndex() {return this.index ?? -1;}
        cuePlaylist(videos, index) {this.videos = videos; this.index = index; this.position = 0; this.state(5);}
        getVideoData() {return {title: 'Fixture radio ' + this.n, author:'Fixture', video_id:window.omitVideoId ? '' : this.videos[this.index]};}
        loadPlaylist(videos, index, position) {this.position = position; this.n++; this.index = index; this.videos = videos; window.fixtureRadio = this;
          if (window.failRadio) setTimeout(() => this.events.onError(), 0);
          else if (window.blockRadio) setTimeout(() => {
            this.events.onAutoplayBlocked(); window.autoplayBlocked = true;
          }, 0);
          else setTimeout(() => this.state(1), 0);}
        playVideo() {this.state(1);} pauseVideo() {this.state(2);}
        nextVideo() {this.position = 0; this.index++; this.n++; this.state(1);}
        previousVideo() {this.position = 0; this.index = Math.max(0, this.index - 1); this.n--; this.state(1);}
        setVolume(value) {window.radioVolume = value;} stopVideo() {this.value = 0;} unMute() {} destroy() {}
      }};""")
    page.route("https://www.youtube.com/embed/**", lambda route: route.fulfill(body="Fixture embed"))


def verify_nearby_jams(browser, root, output):
    from system.owner_auth import Household, OwnerAuth

    app, music, watched = build_fixture(root)
    server = Server(root, app, free_port())
    write_config(app, music, watched, server.port)
    contexts = []
    try:
        server.start()
        wait_for("Jam fixture catalog", lambda: server.request("GET", "/api/overview")[1].get("total", 0) >= 7)
        server.stop()
        seed_state(app)
        password = "Disposable Jam fixture login"
        OwnerAuth(app).set_password("room-owner", password)
        for username in ("Gabby", "Neighbor"):
            Household(app).add_member(username, password)
        server._env.update(LISTENING_ROOM_ORIGIN=server.base)
        server.start()
        wait_for("Jam sign in", lambda: server.request("GET", "/api/account")[0] == 200)

        def visit(username, width):
            context = browser.new_context(viewport={"width": width, "height": 900}, service_workers="block")
            contexts.append(context)
            page = context.new_page()
            page.goto(server.base + "/")
            page.get_by_label("Username").fill(username)
            page.get_by_label("Password").fill(password)
            page.get_by_role("button", name="Sign in", exact=True).click()
            page.get_by_role("button", name="Play Atlas Air", exact=True).wait_for()
            return page

        host = visit("Gabby", 1440)
        guest = visit("Neighbor", 390)
        host.get_by_role("button", name=re.compile(r"^Jams, ")).click()
        host_dialog = host.get_by_role("dialog", name="Jams")
        host_dialog.get_by_role("button", name="Share listening", exact=True).click()
        host_dialog.get_by_role("button", name="Stop sharing", exact=True).wait_for()
        host_dialog.get_by_role("button", name="Close Jams").click()
        host.get_by_role("button", name="Play Atlas Air", exact=True).click()
        playing = """[...document.querySelectorAll('.player audio')].some(
            audio => audio.currentTime > 0.2 && audio.duration > 5 && !audio.paused && !audio.muted)"""
        host.wait_for_function(playing)
        host.get_by_role("button", name=re.compile(r"^Jams, ")).click()
        host_dialog.get_by_role("button", name="Start Jam", exact=True).click()
        host_dialog.get_by_role("button", name="End Jam", exact=True).wait_for()

        guest.get_by_role("button", name=re.compile(r"^Jams, 1 listening nearby, a Jam is available")).wait_for()
        assert not guest.evaluate(playing), "An invitation started audio before Join"
        assert guest.request.get(server.base + "/api/session").json()["current_ref"] is None
        guest.get_by_role("button", name=re.compile(r"^Jams, ")).click()
        guest_dialog = guest.get_by_role("dialog", name="Jams")
        guest_dialog.get_by_text("Gabby’s Jam", exact=True).wait_for()
        guest.screenshot(path=str(output / "34-nearby-jams-phone.png"))
        guest_dialog.get_by_role("button", name="Join Jam", exact=True).click()
        guest_dialog.get_by_text("Listening with Gabby", exact=True).wait_for()
        guest.wait_for_function(playing)
        assert guest.request.get(server.base + "/api/session").json()["current_ref"] is None
        host_dialog.get_by_role("button", name="End Jam", exact=True).click()
        guest_dialog.get_by_text("Listening with Gabby", exact=True).wait_for(state="detached")
        guest.wait_for_function("Array.from(document.querySelectorAll('.player audio')).every(audio => audio.paused)")
        assert host.evaluate(playing), "Ending the Jam stopped the host"
    finally:
        for context in contexts:
            context.close()
        server.stop()


def verify_household_browser(browser, root, output):
    from catalog.collection_snapshot import create_empty_collection
    from library.music_intake import create_demo
    from system.owner_auth import Household, OwnerAuth

    app = root / "config"
    room = root / "room"
    inbox = root / "inbox"
    app.mkdir()
    create_empty_collection(room)
    create_demo(inbox)
    server = Server(root, app, free_port())
    write_config(app, room, inbox / "Playlists", server.port)
    config = app / "listening-room.json"
    settings = json.loads(config.read_text())
    settings["radio_stations"] = [{"name": "House set", "seeds": ["vugodYOKeis"]}]
    config.write_text(json.dumps(settings))
    server._env.update(LISTENING_ROOM_ORIGIN=server.base, LISTENING_ROOM_MUSIC_INBOX=str(inbox))
    server._env.update(LISTENING_JELLYFIN_URL="http://127.0.0.1:9",
                       LISTENING_JELLYFIN_WEB_URL="http://media.home:8096",
                       LISTENING_JELLYFIN_TOKEN="fixture-catalog-token")
    password = "Disposable UI fixture login"
    OwnerAuth(app).set_password("room-owner", password)
    Household(app).add_member("first-listener", password)
    Household(app).add_member("second-listener", password)
    server.start()
    pages = []
    contexts = []
    try:
        wait_for("the private room sign-in", lambda: server.request("GET", "/api/account")[0] == 200)
        unauthenticated = browser.new_page(service_workers="block")
        pages.append(unauthenticated)
        unauthenticated.goto(server.base + "/")
        unauthenticated.get_by_role("link", name="About Listening Room").click()
        unauthenticated.get_by_role("heading", level=1, name="Stream the records you own.").wait_for()
        assert unauthenticated.request.get(server.base + "/api/tracks").status == 401

        def visit(username):
            context = browser.new_context(service_workers="block")
            contexts.append(context)
            page = context.new_page()
            pages.append(page)
            install_youtube_fixture(page)
            page.goto(server.base + "/")
            page.get_by_role("heading", name="Sign in to Listening Room").wait_for()
            page.get_by_label("Username").fill(username)
            page.get_by_label("Password").fill(password)
            page.get_by_role("button", name="Sign in", exact=True).click()
            page.get_by_role("button", name="Sign out", exact=True).wait_for(timeout=20000)
            page.get_by_role("button", name="Play First light", exact=True).wait_for(timeout=25000)
            return page

        def make_playlist(page, name):
            page.get_by_role("button", name="My playlists").click()
            page.get_by_label("New playlist name").fill(name)
            page.get_by_role("button", name="Create playlist").click()
            page.get_by_label("Search songs to add").fill("First light")
            page.get_by_role("button", name="Search", exact=True).click()
            page.get_by_role("button", name="Add First light").click()
            page.get_by_role("button", name="Remove track 1").wait_for()
            page.get_by_role("button", name="Play privately").click()

        first = visit("first-listener")
        second = visit("second-listener")
        first.get_by_role("button", name="Radio", exact=True).click()
        first.get_by_role("searchbox", name="Find a station").fill("brown")
        rejected = []

        def interrupt_resume_writes(route):
            if route.request.method == "POST" and len(rejected) < 2:
                rejected.append(route.request.post_data_json["op"])
                route.abort()
            else:
                route.continue_()

        first.route("**/api/my/radio", interrupt_resume_writes)
        with first.expect_response(lambda response: "/api/my/radio" in response.url and response.request.method == "POST"
                                   and response.status == 200):
            first.get_by_role("button", name="Play Brown noise · Generated on this device").click()
        assert rejected == ["tune", "tune"]
        first.locator(".player").get_by_role("button", name="Pause radio").wait_for()
        first.close()
        resumed = visit("first-listener")
        player = resumed.locator(".player")
        player.get_by_text("Brown noise", exact=True).wait_for()
        player.get_by_role("button", name="Play radio", exact=True).wait_for()
        assert not second.locator(".player").get_by_role("button", name="Play radio", exact=True).count()
        resumed.get_by_role("button", name="Play First light", exact=True).click()
        player.get_by_text("First light", exact=True).wait_for()
        resumed.wait_for_function("[...document.querySelectorAll('.player audio')].some(a => a.currentTime > 0.2 && a.duration > 1 && !a.paused)")
        assert resumed.request.get(server.base + "/api/my/radio").json()["history"]["title"] == "Brown noise"
        resumed.close()

        resumed = visit("first-listener")
        player = resumed.locator(".player")
        player.get_by_text("First light", exact=True).wait_for()
        assert resumed.request.get(server.base + "/api/my/radio").json()["history"]["title"] == "Brown noise"
        resumed.get_by_role("button", name="Radio", exact=True).click()
        player.get_by_text("Brown noise", exact=True).wait_for()
        with resumed.expect_response(lambda response: "/api/my/radio" in response.url and response.request.method == "POST"):
            player.get_by_role("button", name="Play radio", exact=True).click()
        player.get_by_role("button", name="Pause radio", exact=True).wait_for()
        with resumed.expect_response(lambda response: "/api/my/radio" in response.url and response.request.method == "POST"
                                     and response.request.post_data_json["op"] == "clear"):
            player.get_by_role("button", name="Stop radio", exact=True).click()
        first = visit("first-listener")
        assert not first.locator(".player").get_by_role("button", name="Play radio", exact=True).count()

        # Two signed-in clients share the clock while their transports remain independent.
        first.get_by_role("button", name="Radio", exact=True).click()
        first.get_by_role("button", name="Play House set · YouTube · 1 video").click()
        first.locator(".player").get_by_role("button", name="Pause radio").wait_for()
        with first.expect_response(lambda r: "/api/my/radio" in r.url and r.request.method == "POST"
                                   and r.request.post_data_json.get("history", {}).get("position") == 1200.5, timeout=15000):
            first.evaluate("window.fixtureRadio.position = 1200.5")
        resumed.get_by_role("button", name="Radio", exact=True).click()
        player.get_by_text("Resume at 20:00", exact=False).wait_for()
        assert resumed.evaluate("!window.fixtureRadio")
        # Pause between periodic samples; Play must read the newer bookmark.
        first.evaluate("window.fixtureRadio.position = 1207")
        with first.expect_response(lambda r: "/api/my/radio" in r.url and r.request.method == "POST"
                                   and r.request.post_data_json.get("history", {}).get("position") == 1207):
            first.locator(".player").get_by_role("button", name="Pause radio").click()
        resumed.get_by_role("button", name="Play House set · YouTube · 1 video").click()
        resumed.wait_for_function("window.fixtureRadio?.position === 1207 && window.fixtureRadio.getPlayerState() === 1")
        assert resumed.evaluate("window.fixtureRadio.getVideoData().video_id") == "vugodYOKeis"
        with resumed.expect_response(lambda r: "/api/my/radio" in r.url and r.request.method == "POST"
                                    and r.request.post_data_json.get("history", {}).get("position") == 1210):
            resumed.evaluate("""() => {
              window.fixtureRadio.position = 1210;
              Object.defineProperty(document, "hidden", { configurable: true, value: true });
              document.dispatchEvent(new Event("visibilitychange"));
              delete document.hidden;
              document.dispatchEvent(new Event("visibilitychange"));
            }""")
        resumed.evaluate("window.fixtureRadio.position = 1212")
        link = player.get_by_role("link", name="Open in YouTube")
        resumed.context.route("https://www.youtube.com/watch?**", lambda route: route.fulfill(body="YouTube handoff"))
        with resumed.expect_popup() as opened:
            link.click()
        opened.value.wait_for_url("**/watch?v=vugodYOKeis&t=1212s")
        opened.value.close()
        player.get_by_role("button", name="Play radio").wait_for()
        assert resumed.evaluate("window.fixtureRadio.getPlayerState() !== 1")

        # A slow iframe boot must never start sound after a handoff.
        resumed.reload()
        resumed.get_by_role("button", name="Radio", exact=True).click()
        resumed.evaluate("window.holdRadioReady = true")
        player.get_by_role("button", name="Play radio").click()
        resumed.wait_for_function("typeof window.releaseRadioReady === 'function'")
        with resumed.expect_popup() as opened:
            link.click()
        opened.value.close()
        resumed.evaluate("window.releaseRadioReady()")
        assert resumed.evaluate("!window.fixtureRadio")
        player.get_by_role("button", name="Play radio").click()
        resumed.wait_for_function("window.fixtureRadio?.position === 1212 && window.fixtureRadio.getPlayerState() === 1")

        # Even an outstanding checkpoint write cannot indefinitely block local Play.
        waiting = []
        def hold_write(route):
            if route.request.method == "POST":
                waiting.append(route)
            else:
                route.continue_()
        resumed.route("**/api/my/radio", hold_write)
        with resumed.expect_request(lambda r: "/api/my/radio" in r.url and r.method == "POST"):
            player.get_by_role("button", name="Pause radio").click()
        player.get_by_role("button", name="Play radio").click()
        player.get_by_role("button", name="Pause radio").wait_for(timeout=4500)
        resumed.unroute("**/api/my/radio", hold_write)
        for request in waiting:
            request.continue_()
        assert second.request.get(server.base + "/api/my/radio").json()["history"] is None
        player.get_by_role("button", name="Stop radio").click()
        first.get_by_role("button", name="Library", exact=True).click()

        library = first.get_by_role("tabpanel", name="All", exact=True)
        library.get_by_role("button", name="Favorite First light").click()
        library.get_by_role("button", name="Remove First light from favorites").wait_for()
        make_playlist(first, "Private fixture list")
        first.get_by_role("button", name="Add by title and artist").click()
        composer = first.get_by_role("dialog", name="Add a song")
        composer.get_by_label("Song title", exact=True).fill("Vattenne")
        composer.get_by_label("Artist", exact=True).fill("Peppino Gagliardi")
        composer.get_by_role("button", name="Add song", exact=True).click()
        first.locator(".private-track-list li").filter(has_text="Vattenne").get_by_text("Peppino Gagliardi (awaiting audio)").wait_for()
        first.get_by_text("Playlist settings", exact=True).click()
        first.get_by_role("button", name="Delete playlist", exact=True).click()
        first.get_by_role("alertdialog", name="Delete playlist?").get_by_role("button", name="Cancel").click()
        first.get_by_role("heading", name="Private fixture list").wait_for()
        first.screenshot(path=str(output.with_name("22-private-playlist.png")))
        second.reload()
        second.get_by_role("button", name="Favorite First light").wait_for()
        if second.get_by_role("button", name="Settings", exact=True).count():
            raise AssertionError("A household member was offered owner settings")
        second.get_by_role("button", name="My playlists").click()
        second.get_by_role("heading", name="My playlists").wait_for()
        if second.get_by_role("button", name="Private fixture list").count():
            raise AssertionError("A playlist appeared in another member's account")
        first.get_by_role("button", name="My devices", exact=True).click()
        first.get_by_role("heading", name="My devices").wait_for()
        first.get_by_role("heading", name="Playback devices").wait_for()
        for retired in ("Audiobooks", "Podcasts"):
            assert not first.get_by_role("button", name=retired, exact=True).count()
        first.screenshot(path=str(output))

        owner = visit("room-owner")
        owner.get_by_role("button", name="My playlists", exact=True).click()
        shared_shelf = owner.get_by_role("region", name="Room playlists")
        shared_card = shared_shelf.locator(".playlist-card").first
        shared_name = shared_card.locator("strong").inner_text()
        shared_card.click()
        owner.get_by_role("heading", name=shared_name, exact=True).wait_for()
        owner.get_by_role("button", name="Back to my playlists", exact=True).click()
        shared_card.wait_for()
        owner.get_by_role("button", name="Library", exact=True).click()
        remote_output = {"Origin": server.base, "X-Listening-Output": "fixture-other-device"}
        tuning = "a" * 32
        radio_bookmark = {"source": {"kind": "noise", "name": "Brown noise", "profile": "brown"},
                          "title": "Brown noise", "channel": ""}
        assert owner.request.post(server.base + "/api/my/radio", headers=remote_output,
                                  data={"op": "tune", "tuning": tuning, "history": radio_bookmark}).ok
        started = owner.request.post(server.base + "/api/session", headers=remote_output,
                                     data={"op": "play_available", "playlist": "all"})
        assert started.ok and started.json()["session_playing"]
        with owner.expect_response(lambda reply: "/api/my/radio" in reply.url and reply.request.method == "GET"):
            owner.reload()
        owner_player = owner.get_by_role("contentinfo", name="Audio player")
        owner_player.get_by_role("button", name="Listen here", exact=True).wait_for()
        assert owner_player.get_by_text("First light", exact=True).is_visible()
        assert owner.evaluate("[...document.querySelectorAll('.player audio')].every(audio => audio.paused)")
        assert owner.request.get(server.base + "/api/my/radio").json()["history"]["title"] == "Brown noise"
        owner.get_by_role("button", name="Radio", exact=True).click()
        owner.get_by_role("button", name="Play radio", exact=True).wait_for()
        assert owner_player.get_by_text("First light", exact=True).is_visible()
        owner.get_by_role("button", name="Library", exact=True).click()
        assert owner.request.post(server.base + "/api/my/radio", headers=remote_output,
                                  data={"op": "clear", "tuning": tuning}).ok
        assert owner.request.post(server.base + "/api/session", headers=remote_output, data={"op": "pause"}).ok
        make_playlist(owner, "Owner private fixture")
        owner.wait_for_function("Array.from(document.querySelectorAll('.player audio')).some(audio => !audio.paused)")
        owner.route("**/api/session", lambda route: route.fulfill(status=503, json={"error": "Fixture queue offline"}))
        owner.get_by_role("button", name="Use shared queue").click()
        owner.wait_for_function("Array.from(document.querySelectorAll('.player audio')).every(audio => audio.paused)")

        owner.get_by_role("button", name="Browse catalogs", exact=True).click()
        owner.get_by_text("Manage discovery sources", exact=True).click()
        owner.get_by_role("combobox", name="Source type", exact=True).click()
        owner.get_by_role("option", name="Funkwhale server", exact=True).click()
        owner.get_by_label("Source name", exact=True).fill("Fixture community radio")
        owner.get_by_label("Server address", exact=True).fill("https://community.example")
        owner.get_by_role("button", name="Add source", exact=True).click()
        owner.get_by_text("Source saved.", exact=True).wait_for()

        catalog_calls = []
        recording = {"title": "Settembre", "creator": "Peppino Gagliardi", "album": "Settembre",
                     "musicbrainz_id": "82212620-cbfc-40df-b700-cfa49405beeb", "song": True,
                     "url": "https://musicbrainz.org/recording/82212620-cbfc-40df-b700-cfa49405beeb"}
        def catalog_call(route):
            if route.request.method != "POST":
                return route.continue_()
            path = route.request.url.split("/api/")[-1]
            catalog_calls.append((path, route.request.post_data_json))
            replies = {"discovery/search": {"groups": [{"source": "musicbrainz", "name": "MusicBrainz", "items": [recording]}]},
                       "discovery/import": {"imported": True, "track_id": 731, "ready": False},
                       "requests": {"id": 913, "status": "queued", "message": "Queued"}}
            route.fulfill(json=replies[path])
        for path in ("discovery/search", "discovery/import", "requests"):
            owner.route("**/api/" + path, catalog_call)
        owner.get_by_role("searchbox", name="Search public catalogs").fill("PEPPINO GAGLIARDI")
        owner.locator(".discovery-search").get_by_role("button", name="Search", exact=True).click()
        owner.get_by_role("button", name="Import song", exact=True).click()
        owner.get_by_text("In library", exact=True).wait_for()
        assert [path for path, _ in catalog_calls] == ["discovery/search", "discovery/import"]
        assert catalog_calls[0][1]["source"] == "recordings"
        assert catalog_calls[1][1]["musicbrainz_id"] == recording["musicbrainz_id"]
        with owner.expect_response(lambda response: response.url.endswith("/api/requests") and response.request.method == "POST"):
            owner.get_by_role("button", name="Find a copy", exact=True).click()
        assert catalog_calls[-1] == ("requests", {"track_id": 731})

        second.set_viewport_size({"width": 390, "height": 844})
        second.get_by_role("button", name="Menu", exact=False).click()
        second.get_by_role("button", name="Extensions", exact=True).click()
        second.get_by_role("heading", name="Song downloads").wait_for()
        assert not second.get_by_role("button", name=re.compile(r"^Install(?: |$)")).count()
        second.goto(server.base + "/#extensions")
        second.reload()
        second.get_by_role("heading", name="Installed extensions").wait_for()
        second.get_by_role("button", name="Menu", exact=False).click()
        second.get_by_role("button", name="Browse catalogs", exact=True).click()
        browse = second.locator(".discovery-view")
        browse.get_by_role("heading", name="Browse catalogs").wait_for()
        if browse.get_by_text("Manage discovery sources", exact=True).count():
            raise AssertionError("A household member can manage discovery sources")
        browse.get_by_role("button", name="View extensions").click()
        sources_dialog = second.get_by_role("dialog", name="Extensions")
        sources_dialog.get_by_role("heading", name="Installed extensions").wait_for()
        sources_dialog.get_by_role("button", name="Close extensions").click()
        picker = browse.get_by_role("combobox", name="Search source", exact=True)
        picker.click()
        option = second.get_by_role("option", name="Fixture community radio", exact=True)
        expected_source = option.get_attribute("data-value")
        assert expected_source
        option.click()
        searches = []

        def discovery_results(route):
            query = route.request.post_data_json
            searches.append(query)
            if query["scope"] == "room":
                route.fulfill(json={"groups": [{"source": "jellyfin", "name": "Jellyfin",
                    "items": [
                        {"url": "http://media.home:8096/web/index.html#/details?id=fixture",
                         "title": "Quiet album", "creator": "Fixture artist", "detail": "", "license": ""},
                        {"url": "http://media.home:8096/web/index.html#/details?id=song",
                         "title": "Quiet song", "creator": "Fixture artist", "detail": "Quiet album", "license": "",
                         "song": True, "album": "Quiet album"}],
                    "more": False, "error": ""}]})
                return
            route.fulfill(json={"groups": [{"source": expected_source, "name": "Fixture community radio",
                                           "items": [{"url": "https://community.example/episode.mp3",
                                                      "title": "Night report", "creator": "Community radio",
                                                      "detail": "Audio enclosure", "license": ""}],
                                           "more": query["page"] == 0, "error": ""}]})

        second.route("**/api/discovery/search", discovery_results)
        browse.get_by_label("Search public catalogs", exact=True).fill("night")
        if searches:
            raise AssertionError("Typing a query searched a public catalog before submit")
        browse.get_by_role("button", name="Search", exact=True).click()
        link = browse.get_by_role("link", name="Night report", exact=True)
        assert link.get_attribute("href") == "https://community.example/episode.mp3"
        assert "noreferrer" in link.get_attribute("rel")
        browse.get_by_role("button", name="Next page", exact=True).click()
        browse.get_by_text("Page 2", exact=True).wait_for()
        assert [(entry["source"], entry["query"], entry["page"]) for entry in searches] == [
            (expected_source, "night", 0), (expected_source, "night", 1)]
        second.screenshot(path=str(output.with_name("21-discovery-mobile.png")))
        long_query = "n" * 160
        browse.get_by_label("Search public catalogs", exact=True).fill(long_query)
        browse.get_by_role("button", name="Search", exact=True).click()
        browse.get_by_text(f"1 result for “{long_query}”", exact=True).wait_for()
        assert second.evaluate("document.documentElement.scrollWidth") <= second.viewport_size["width"]
        browse.get_by_role("button", name="Private catalog", exact=True).click()
        browse.get_by_role("combobox", name="Search source", exact=True).click()
        second.get_by_role("option", name="Jellyfin", exact=True).click()
        browse.get_by_label("Search private catalog", exact=True).fill("quiet")
        browse.get_by_role("button", name="Search", exact=True).click()
        browse.get_by_role("link", name="Quiet album", exact=True).wait_for()
        assert searches[-1]["scope"] == "room" and searches[-1]["source"] == "jellyfin"
        assert browse.get_by_role("button", name="Add to playlist", exact=True).count() == 1
        browse.get_by_role("button", name="Add to playlist", exact=True).click()
        from_catalog = second.get_by_role("dialog", name="Add to playlist")
        assert from_catalog.get_by_label("Song title", exact=True).input_value() == "Quiet song"
        assert from_catalog.get_by_label("Album (optional)").input_value() == "Quiet album"
        from_catalog.get_by_role("button", name="Close add song").click()

        imported = owner.request.post(server.base + "/api/import-list", headers={"Origin": server.base}, data={
            "name": "fresh-picks.csv", "content": "title,artist,album\nNight tide,Fixture quartet,Harbor\n"})
        assert imported.ok, f"Could not import fixture playlist ({imported.status}): {imported.text()}"
        owner.reload()
        shelf = owner.locator(".playlist-shelf .playlist-card").filter(has_text=re.compile("fresh.picks", re.I))
        shelf.click()
        owner.get_by_role("button", name="Add song", exact=True).click()
        composer = owner.get_by_role("dialog", name="Add a song")
        composer.get_by_label("Song title", exact=True).fill("Vattenne")
        composer.get_by_label("Artist", exact=True).fill("Peppino Gagliardi")
        with owner.expect_response(lambda res: "/api/playlists/add-song" in res.url and res.request.method == "POST" and res.ok):
            composer.get_by_role("button", name="Add song", exact=True).click()
        owner.get_by_role("button", name="Song details for Vattenne", exact=True).first.wait_for()
        assert not owner.get_by_role("button", name="Play Vattenne", exact=True).count()

        # Browser contract for a projected group linked to two Spotify lists.
        # Intercept the provider write so this gate never edits a real playlist.
        spotify_targets = [
            {"mode": "two_way", "account_id": "fixture-owner", "playlist_id": "fixture-A", "name": "Late radio"},
            {"mode": "two_way", "account_id": "fixture-owner", "playlist_id": "fixture-B", "name": "Day radio"}]
        spotify_link = {**spotify_targets[0], "mode": "read_only", "needs_write_permission": True}
        playlist_revision = "first-order"

        def spotify_overview(route):
            upstream = route.fetch()
            data = upstream.json()
            group = next(row for row in data["playlists"] if re.search("fresh.picks", row["name"], re.I))
            group.update(can_add=spotify_link["mode"] != "read_only", spotify_link=spotify_link, revision=playlist_revision)
            route.fulfill(response=upstream, json=data)

        writes = []
        def save_spotify(route):
            writes.append(route.request.post_data_json)
            route.fulfill(json={"message": "Song added to the linked playlist."})

        owner.route("**/api/overview", spotify_overview)
        owner.route("**/api/playlists/add-song", save_spotify)
        owner.reload()
        shelf.get_by_text("Spotify · read-only", exact=True).wait_for()
        shelf.click()
        owner.get_by_role("button", name="Enable Spotify edits", exact=True).wait_for()
        assert not owner.get_by_role("button", name="Add song", exact=True).count()
        spotify_link = {"mode": "multiple", "targets": spotify_targets}
        owner.reload()
        shelf.get_by_text("Spotify · multiple links", exact=True).wait_for()
        shelf.click()
        owner.get_by_role("button", name="Add song", exact=True).click()
        composer.get_by_label("Song title", exact=True).fill("Evening ride")
        composer.get_by_label("Artist", exact=True).fill("Fixture quartet")
        assert composer.get_by_role("button", name="Add song", exact=True).is_disabled()
        composer.get_by_role("combobox", name="Spotify destination").click()
        owner.get_by_role("option", name=re.compile("Day radio")).click()
        composer.get_by_role("button", name="Add song", exact=True).click()
        composer.wait_for(state="detached")
        assert len(writes) == 1
        assert (writes[0]["playlist_id"], writes[0]["account_id"], writes[0]["title"]) == (
            "fixture-B", "fixture-owner", "Evening ride")

        # A reconnected account can have the same local key and provider
        # playlist id (e.g. Liked Songs). An open form must keep its original binding.
        spotify_link = {"mode": "two_way", "account_id": "fixture-owner", "playlist_id": "liked"}
        owner.reload()
        shelf.get_by_text("Spotify · two-way sync", exact=True).wait_for()
        shelf.click()
        owner.get_by_role("button", name="Add song", exact=True).click()
        composer.get_by_label("Song title", exact=True).fill("Evening ride")
        composer.get_by_label("Artist", exact=True).fill("Fixture quartet")
        assert composer.get_by_role("button", name="Add song", exact=True).is_enabled()
        spotify_link = {**spotify_link, "account_id": "replacement-account"}
        owner.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        composer.get_by_role("alert").filter(has_text="access changed").wait_for(timeout=25000)
        assert composer.get_by_role("button", name="Add song", exact=True).is_disabled()
        assert len(writes) == 1
        composer.get_by_role("button", name="Close add song").click()

        # A provider reorder changes neither the song count nor room coverage.
        titles = owner.locator(".track-row .track-title")
        original_order = titles.all_inner_texts()
        assert len(original_order) == 2
        def reordered_tracks(route):
            upstream = route.fetch()
            result = upstream.json()
            result["tracks"].reverse()
            route.fulfill(response=upstream, json=result)
        owner.route("**/api/tracks?**", reordered_tracks)
        playlist_revision = "second-order"
        owner.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        owner.wait_for_function("title => document.querySelector('.track-row .track-title')?.textContent === title",
                                arg=original_order[-1])
        assert titles.all_inner_texts() == list(reversed(original_order))

        first.get_by_role("button", name="Sign out", exact=True).click()
        first.get_by_role("heading", name="Sign in to Listening Room").wait_for()
    finally:
        for page in pages:
            page.close()
        for context in contexts:
            context.close()
        server.stop()


def check_pick_action_contrast(action):
    # Resolve transparent controls against their painted ancestor (the original hover bug).
    fg, bg = action.evaluate("""async el => {
        await new Promise(requestAnimationFrame);
        await Promise.all(el.getAnimations().map(animation => animation.finished));
        let surface = el;
        while (surface.parentElement && getComputedStyle(surface).backgroundColor === 'rgba(0, 0, 0, 0)')
            surface = surface.parentElement;
        return [getComputedStyle(el).color, getComputedStyle(surface).backgroundColor];
    }""")

    def luminance(rgb):
        channels = [int(value) / 255 for value in re.fullmatch(r"rgb\((\d+), (\d+), (\d+)\)", rgb).groups()]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
        return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    low, high = sorted((luminance(fg), luminance(bg)))
    assert (high + 0.05) / (low + 0.05) >= 4.5, f"{action.inner_text()} is unreadable: {fg} on {bg}"
    return fg, bg


def verify_discovery_trail(page, screenshot):
    """Exercise the web and WebKit entry point with deterministic provider replies."""
    seed, artist, studio, live = [f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 5)]
    song = {"musicbrainz_id": studio, "title": "Frequencies", "artist": "The Fixture Trio",
            "artist_id": artist, "album": "Signal Path", "duration": 182, "seed_artist": "Massive Attack",
            "seed_id": seed, "seed_rank": 0, "reason": "From artists near Massive Attack", "art_seed": "781299"}
    alternate = {**song, "musicbrainz_id": live, "duration": 249, "version": "Live at the Winter Garden"}
    lead = {"kind": "recording", "id": live, "name": song["title"], "label": "Live recording", "track": alternate}
    shelf = [song, *[{**song, "artist_id": f"artist-{i}", "artist": name, "musicbrainz_id": f"song-{i}"}
                     for i, name in enumerate(["Night Tapes", "Broadcast", "A Winged Victory for the Sullen"])]]
    shelf.append({**song, "seed_id": "another-seed", "seed_artist": "The Smiths", "seed_rank": 1,
                  "musicbrainz_id": "another-recording", "title": "After the rain"})
    producer = {"kind": "artist", "id": "producer", "label": "Producer", "name": "Ada Fields"}
    attempts = []
    def branch(route):
        key = route.request.url.rsplit("/", 1)[-1]
        if key == studio and not attempts:
            attempts.append(key)
            return route.fulfill(status=503, json={"error": "Catalog offline"})
        source = {"name": "Bandcamp", "url": "https://fixture.bandcamp.com/track/frequencies"}
        route.fulfill(json={"leads": [lead, producer] if key == studio else [], "source": source if key == live else None})

    page.route("**/api/discover", lambda route: route.fulfill(json={"week": "2026-W47", "weekly_edition": "2026-11-23", "tracks": shelf}))
    page.route("**/api/discover/weekly", lambda route: route.fulfill(json={"week": "2026-11-23", "state": "ready",
        "can_request": True, "download_progress": {"total": 2, "downloaded": 1, "pending": 0,
                                                     "needs_attention": 1, "percent": 50},
        "tracks": [{"id": seed, "title": "Weekly edition track", "artist": "Fixture Ensemble", "depth": 2,
                    "request": {"id": 901, "status": "downloaded", "audio": "/api/audio/custom/901"}},
                   {"id": live, "title": "Recording to review", "artist": "Fixture Ensemble", "depth": 2,
                    "request": {"id": 902, "status": "rejected", "copy_review": True,
                                "format_revision": 2, "requested_format": "any_lossy"}}]}))
    page.route("**/api/requests/copies", lambda route: route.fulfill(json={"matches": [
        {"key": "a" * 32, "label": "Possible copy.mp3", "format": "mp3", "recommended": False}]}))
    page.route("**/api/discover/*/*", branch)
    activity = {"liked": [{"id": 1, "artist": "Massive Attack", "title": "Atlas Air"}],
                "played": [{"id": 2, "artist": "The Smiths", "title": "Asleep"}]}
    page.route("**/api/connections/song/1", lambda r: r.fulfill(json={"state": "ready",
        "root": {"kind": "recording", "id": studio, "detail": "Recording", "title": "Atlas Air"},
        "edges": [{"node": {"kind": "artist", "id": artist, "title": "Guest Singer", "detail": "Person"},
                   "label": "Vocal credit", "note": ""}]}))
    page.route("**/api/discover/activity", lambda r: r.fulfill(json=activity))
    page.route("**/api/discover/press", lambda r: r.fulfill(json={"stories": [
        {"title": "The Fixture Trio announce a live session", "url": "https://www.theguardian.com/music/fixture",
         "published": "2026-11-19T12:00:00+00:00"}]}))
    page.route("**/api/discover/matches?*", lambda r: r.fulfill(json={"leads": [lead]}))
    page.route("**/api/search?q=Fixture*", lambda r: r.fulfill(json={"results": []}))
    try:
        search = page.get_by_role("searchbox", name="Search songs, artists and albums")
        search.fill("Fixture Trio")
        search.press("Enter")
        page.get_by_role("button", name="Deep Dive", exact=True).click()
        board = page.get_by_role("region", name="Discovery board")
        trails = board.get_by_role("region", name="Artist trails")
        first = trails.get_by_role("button", name=re.compile("Massive Attack"))
        first.wait_for()
        board.get_by_role("region", name="Recently liked").get_by_role("button", name=re.compile("Atlas Air")).wait_for()
        board.get_by_role("link", name=re.compile("announce a live session")).wait_for()
        cards = board.get_by_role("region", name="Recordings to hear").get_by_role("button")
        assert cards.count() == len(shelf)
        page.get_by_text("Week of Nov 23", exact=True).wait_for()
        page.get_by_role("button", name="Open Weekly Discovery").click()
        page.get_by_role("heading", name="Weekly Discovery").wait_for()
        page.get_by_text("Weekly edition track", exact=True).wait_for()
        assert page.get_by_role("progressbar", name="Playlist downloads").get_attribute("value") == "50"
        assert page.get_by_text("1 of 2 downloaded · 50% · 1 needs attention").is_visible()
        assert page.get_by_role("button", name="Review copies for Recording to review").is_visible()
        page.get_by_role("button", name="Review copies", exact=True).click()
        page.get_by_role("dialog", name="Choose a copy").get_by_role("button", name=re.compile("Possible copy")).wait_for()
        page.get_by_role("button", name="Back to format").click()
        page.get_by_role("button", name="Later", exact=True).click()
        assert not board.is_visible()
        page.get_by_role("button", name="Back to Deep Dive").click()
        first.wait_for()
        screenshot()
        liked = board.get_by_role("region", name="Recently liked").get_by_role("button", name=re.compile("Atlas Air"))
        liked.click()
        page.get_by_role("heading", name="Atlas Air").wait_for()
        page.get_by_role("button", name="Vocal credit Guest Singer").wait_for()
        page.get_by_role("button", name="Back to discovery board").click()
        assert liked.evaluate("el => el === document.activeElement")
        for region, label in (("Recent searches", "Fixture Trio"), ("Recently played", "The Smiths")):
            origin = board.get_by_role("region", name=region, exact=True).get_by_role("button", name=re.compile(label))
            origin.click()
            page.get_by_role("region", name="Paths to follow").get_by_role("button", name=re.compile("Frequencies")).wait_for()
            page.get_by_role("button", name="Back to discovery board").click()
            assert origin.evaluate("el => el === document.activeElement")
        page.set_viewport_size({"width": 390, "height": 844})
        assert board.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        page.set_viewport_size({"width": 1440, "height": 900})
        first.click()
        fork = page.get_by_role("region", name="Paths to follow")
        fork.get_by_role("button", name="Show more").click()
        fork.get_by_role("button", name=re.compile("A Winged Victory")).wait_for()
        fork.get_by_role("button", name=re.compile("Suggested artist The Fixture Trio")).click()
        fork.get_by_role("button", name=re.compile("Recording Frequencies")).click()
        page.get_by_role("button", name="Retry branch").click()
        live_choice = fork.get_by_role("button", name=re.compile("Live recording Frequencies"))
        live_choice.focus()
        page.keyboard.press("Enter")
        article = page.get_by_role("article", name="Selected recording")
        article.get_by_text(re.compile("Live at the Winter Garden")).wait_for()
        link = article.get_by_role("link", name="Listen on Bandcamp")
        assert link.get_attribute("target") == "_blank"
        page.context.route("https://fixture.bandcamp.com/**", lambda route: route.fulfill(body="Fixture listening page"))
        with page.expect_popup() as opened:
            link.click()
        opened.value.close()
        article.get_by_role("heading", name="Frequencies").wait_for()
        # This alternate is outside the weekly shelf. Keep its known artist across the dialog.
        page.route(f"**/api/metadata/recording/{live}", lambda route: route.fulfill(json=alternate))
        page.route(f"**/api/connections/recording/{live}", lambda route: route.fulfill(json={
            "state": "ready", "root": {"kind": "recording", "id": live, "title": "Frequencies", "detail": "Recording"}, "edges": []}))
        page.route(f"**/api/connections/artist/{artist}", lambda route: route.fulfill(json={
            "state": "ready", "root": {"kind": "artist", "id": artist, "title": song["artist"], "detail": "Group"},
            "edges": [{"label": "Album", "note": "", "node": {"kind": "release-group", "id": seed, "title": "Signal Path", "detail": "Album"}}]}))
        article.get_by_role("button", name="Song details").click()
        page.get_by_role("tab", name="Credits", exact=True).click()
        credits = page.get_by_role("dialog", name="Frequencies", exact=True)
        credits.get_by_role("button", name="Explore Signal Path · Album").wait_for()
        credits.get_by_role("button", name="Close song information").click()
        back_to_board = page.get_by_role("button", name="Back to discovery board")
        for control in [back_to_board, *article.locator(".trail-actions").locator("button, a").all()]:
            assert not control.inner_text().strip() and control.locator("svg[aria-hidden=true] path").count() == 1
            assert control.get_attribute("aria-label") == control.get_attribute("title")
            assert control.bounding_box()["width"] >= 44
            assert control.bounding_box()["height"] >= 44
            check_pick_action_contrast(control)
        page.set_viewport_size({"width": 390, "height": 844})
        page.emulate_media(reduced_motion="reduce")
        assert page.locator(".trail-stage").evaluate("el => getComputedStyle(el).gridTemplateColumns.split(' ').length === 1")
        assert page.locator(".trail-stop").evaluate("el => getComputedStyle(el).animationName === 'none'")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.set_viewport_size({"width": 1440, "height": 900})
        nav = page.get_by_role("navigation", name="Discovery trail")
        nav.get_by_role("button", name="Frequencies").click()
        live_choice.wait_for()
        article.get_by_role("link", name="Search on YouTube").wait_for()
        nav.get_by_role("button", name="Massive Attack").click()
        assert fork.get_by_role("button", name=re.compile("The Fixture Trio")).evaluate("el => document.activeElement === el")
        fork.get_by_role("button", name=re.compile("Night Tapes")).click()
        page.get_by_role("heading", name="Night Tapes").wait_for()
        page.get_by_role("button", name="Back to discovery board").click()
        trails.get_by_role("button", name=re.compile("The Smiths")).click()
        page.get_by_role("heading", name="The Smiths").wait_for()
        assert nav.locator("li").count() == 1
    finally:
        page.set_viewport_size({"width": 1440, "height": 900})
        page.emulate_media(reduced_motion="no-preference")
        page.unroute("**/api/discover")
        page.unroute("**/api/discover/weekly")
        page.unroute("**/api/requests/copies")
        page.unroute("**/api/discover/*/*")
        for pattern in ("**/api/connections/song/1", "**/api/discover/activity", "**/api/discover/press", "**/api/discover/matches?*", "**/api/search?q=Fixture*"):
            page.unroute(pattern)
        for path in (f"metadata/recording/{live}", f"connections/recording/{live}", f"connections/artist/{artist}"):
            page.unroute("**/api/" + path)
        page.context.unroute("https://fixture.bandcamp.com/**")


def run_browser(server, output):
    """Drive the surfaces; returns (surfaces, errors, warnings)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, None, None  # caller reports the missing tooling

    surfaces = []
    errors = []
    warnings = []

    def surface(name):
        item = Surface(name)
        surfaces.append(item)
        return item

    with sync_playwright() as pw:
        executable = os.environ.get("VERIFY_UI_CHROME") or None
        try:
            browser = pw.chromium.launch(
                executable_path=executable, args=["--no-sandbox"])
        except Exception:
            if executable:
                raise
            candidates = sorted(
                Path.home().glob(
                    ".cache/ms-playwright/chromium_headless_shell-*/"
                    "chrome-headless-shell-linux64/chrome-headless-shell"))
            candidates += sorted(
                Path.home().glob(
                    ".cache/ms-playwright/chromium-*/chrome-linux/chrome"))
            if not candidates:
                raise
            browser = pw.chromium.launch(
                executable_path=str(candidates[-1]),
                args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        # Loopback grants secure-context features that paired LAN browsers lack.
        page.add_init_script("Object.defineProperty(crypto, 'randomUUID', {value: undefined})")
        install_youtube_fixture(page)
        record_browser_errors(page, errors, warnings)

        def shot(item, filename):
            page.screenshot(path=str(output / filename))

        def close_any_dialog():
            # A failed dialog surface must not leave a modal open to
            # cascade into every later surface: close whatever is up,
            # by its close button when that works and by Escape when
            # it does not (a hidden dialog's button cannot be
            # clicked, but its keyboard handling still answers).
            for _ in range(3):
                try:
                    content = page.locator("[data-slot='dialog-content']")
                    if content.count() == 0:
                        return
                    closer = content.last.locator(".dialog-close")
                    if closer.count():
                        try:
                            closer.first.click(timeout=3000)
                        except Exception:
                            page.keyboard.press("Escape")
                    else:
                        page.keyboard.press("Escape")
                    page.wait_for_timeout(400)
                except Exception:
                    return

        def expect(item, condition, detail):
            if not condition:
                raise AssertionError(detail)

        try:
            item = surface("landing-page")
            try:
                verify_landing(browser, server.base, output, errors, warnings)
                item.ok = True
                item.detail = "Both routes, local artwork, keyboard and touch mood selection, reduced motion"
            except Exception as error:
                item.detail = str(error)

            item = surface("library-search-link")
            linked = browser.new_page()
            try:
                linked.goto(server.base + "/?" + urlencode({"q": "Atlas Air"}))
                search_field = linked.get_by_role("searchbox", name="Search songs, artists and albums")
                search_field.wait_for(timeout=15000)
                expect(item, search_field.input_value() == "Atlas Air", "Search link did not fill the library field")
                song = linked.locator(".track-row")
                song.first.wait_for(timeout=15000)
                expect(item, song.count() == 1 and "Atlas Air" in song.first.inner_text(),
                       "Search link did not filter the library")
                item.ok = True
                item.detail = "A linked title filters the authenticated room library"
            except Exception as error:
                item.detail = str(error)
            finally:
                linked.close()

            # -- Collection: library tab, shelf, table ----------------
            item = surface("collection-library")
            try:
                page.goto(server.base + "/", wait_until="load")
                page.wait_for_selector("section.playlist-shelf .playlist-card",
                                       timeout=30000)
                cards = page.locator("section.playlist-shelf .playlist-card")
                expect(item, cards.count() == 2,
                       f"shelf holds {cards.count()} playlists, want 2")
                magnet = page.locator(
                    "section.playlist-shelf .playlist-card",
                    has_text="Morning")
                expect(item, magnet.first.locator('.source-badge[title="Watched folder"] svg').count() == 1,
                       "watched playlist wears no magnet badge")
                expect(item, page.get_by_role(
                    "heading", name="Library", exact=True).count() == 1,
                    "Library heading missing")
                expect(item, not page.get_by_role("tab", name="Explore").count(),
                       "Search tabs appeared before a search")
                page.locator(".track-row").first.wait_for()
                first_row = page.locator(".track-row").first.bounding_box()
                player = page.locator(".player").bounding_box()
                expect(item, first_row["y"] + first_row["height"] <= player["y"],
                       "First song is obscured by the player")
                shot(item, "00-library.png")
                cards.filter(has_text="Evening").click()
                page.wait_for_function(
                    "document.querySelectorAll('.track-row')"
                    ".length === 5", timeout=30000)
                item.ok = True
                item.detail = ("2 shelf playlists (watched one badged), "
                               "evening table holds its 5 rows")
                shot(item, "01-collection.png")
            except Exception as error:
                item.detail = str(error)

            item = surface("playlist-metadata-share")
            try:
                page.get_by_role("button", name="Share playlist", exact=True).click()
                dialog = page.get_by_role("dialog", name="Share playlist")
                dialog.get_by_text("Atlas Air", exact=True).wait_for()
                dialog.get_by_role("button", name="Text for a post").click()
                expect(item, "Massive Attack — Atlas Air" in dialog.get_by_role(
                    "textbox", name="Playlist post text").input_value(), "Post text lost track metadata")
                with page.expect_download() as download:
                    dialog.get_by_role("link", name="Download XSPF").click()
                data = Path(download.value.path()).read_text(encoding="utf-8")
                expect(item, "Atlas Air" in data and "<location>" not in data and
                       "Atlas Air" in data.split("Asleep", 1)[0], "Playlist shares a location or changes order")
                item.ok = True
                item.detail = "Selected playlist preview, share text, and ordered metadata-only XSPF"
                shot(item, "01b-playlist-share.png")
            except Exception as error:
                item.detail = str(error)
            finally:
                page.keyboard.press("Escape")

            item = surface("phone-navigation")
            try:
                verify_phone(browser, server, output, errors, warnings)
                item.ok = True
                item.detail = "320px and 390px touch layouts, Settings and song reflow, menu dismissal, and queue access"
            except Exception as error:
                item.detail = str(error)

            item = surface("activity-mixes")
            try:
                page.get_by_role("button", name="Mood map", exact=True).click()
                page.get_by_role("region", name="Browse by mood").get_by_role(
                    "button", name="Activities", exact=True).click()
                activities = page.get_by_role("region", name="Activity mixes")
                for label, matches in [("Walking", [1, 4]), ("Studying", [3]), ("Meditation", [2])]:
                    with page.expect_response(lambda response: response.request.method == "POST" and
                                              response.request.post_data_json.get("op") == "play_activity") as start:
                        activities.get_by_role("button", name=f"Play {label} mix").click(timeout=60000)
                    expect(item, start.value.ok and start.value.json()["current_ref"] in matches,
                           f"{label} did not play a song within its pace and energy range")
                item.ok = True
                item.detail = "All three activities played matching local tracks from the saved audio analysis"
                shot(item, "01e-activities.png")
                page.get_by_role("button", name="Library", exact=True).click()
            except Exception as error:
                item.detail = str(error)

            item = surface("mood-browsing")
            try:
                page.get_by_role("button", name="Mood map", exact=True).click()
                cloud = page.get_by_role("region", name="Browse by mood")
                energetic = cloud.get_by_role("button", name="Energetic", exact=True)
                energetic.hover()
                page.locator(".mood-map[data-mood='energetic']").wait_for()
                expect(item, energetic.get_attribute("aria-pressed") == "false",
                       "Hover selected a mood before activation")
                energetic.focus()
                with page.expect_response(lambda response: response.request.method == "POST" and
                                          response.request.post_data_json.get("op") == "play_mood") as start:
                    page.keyboard.press("Enter")
                expect(item, start.value.ok and start.value.json()["current_ref"] in (1, 2, 3),
                       "Mood did not start a ready matching track")
                field = cloud.get_by_role("button", name="Blend moods")
                field.focus()
                for _ in range(4):
                    field.press("ArrowRight")
                with page.expect_response(lambda response: response.request.method == "POST" and
                                          response.request.post_data_json.get("blend")) as blend:
                    field.press("Enter")
                request = blend.value.request.post_data_json
                spoken = cloud.locator(".visually-hidden[role=status]").inner_text()
                expect(item, f"{request['blend_percent']}%" in spoken, "Keyboard adjustment lost audible blend feedback")
                expect(item, blend.value.ok and request["mood"] != request["blend"] and 0 < request["blend_percent"] <= 50,
                       "Moving between landmarks did not start a weighted blend")
                expect(item, cloud.locator(".track-row").count() == 0, "Tuning opened a song list")
                settings = cloud.get_by_role("button", name="Map settings")
                expect(item, settings.get_attribute("aria-expanded") == "false" and
                       not cloud.get_by_role("heading", level=2).count() and
                       not cloud.locator("p, h2").filter(has_text=re.compile(r"\d+%")).count() and
                       not cloud.get_by_role("button", name="Play selection").count(),
                       "The map displayed a separate mix readout or settings on entry")
                shot(item, "01f-mood-map.png")
                settings.focus()
                settings.press("Enter")
                energetic.click()
                genre = cloud.get_by_role("combobox", name="Genre", exact=True)
                genre.click()
                page.get_by_role("option", name="rock", exact=True).click()
                page.wait_for_function("document.querySelector('.player-text strong')?.textContent === 'Asleep'")

                def mood_catalog(route):
                    search = route.request.post_data_json
                    if (search.get("source"), search.get("query")) != ("live-music", "rock"):
                        route.fulfill(status=400, json={"error": "Unexpected catalog query"})
                        return
                    route.fulfill(json={"groups": [{"items": [{
                        "title": "Fixture live recording", "url": "https://archive.org/details/fixture"
                    }]}]})

                source = cloud.get_by_role("combobox", name="Tune from")
                source.click()
                page.get_by_role("option", name="Open catalogs", exact=True).click()
                page.route("**/api/discovery/search", mood_catalog)
                cloud.get_by_role("button", name="Explore recordings").click()
                cloud.get_by_role("link", name="Fixture live recording").wait_for()
                page.unroute("**/api/discovery/search")

                source.click()
                page.get_by_role("option", name="YouTube", exact=True).click()
                page.route("**/api/radio/search?*", lambda route: route.fulfill(json={"videos": ["vugodYOKeis"]}))
                with page.expect_response(lambda response: "/api/radio/search?" in response.url) as search:
                    cloud.get_by_role("button", name="Play selection", exact=True).click()
                expect(item, parse_qs(urlsplit(search.value.url).query) == {"mood": ["energetic"], "genre": ["rock"]},
                       "Mood did not search YouTube for the selected genre")
                page.locator(".player").get_by_role("button", name="Pause radio").wait_for()
                # Availability can change after the facets load. The server decides whether
                # Auto may fall back to YouTube when the library has no playable match.
                source.click()
                page.get_by_role("option", name="Library, then YouTube", exact=True).click()
                def empty_mix(route):
                    if route.request.method == "POST" and route.request.post_data_json.get("op") == "play_mood":
                        route.fulfill(status=400, json={"error": "No ready songs match this mood", "code": "empty_mood"})
                    else:
                        route.continue_()
                page.route("**/api/session", empty_mix)
                try:
                    with page.expect_request(lambda req: "/api/radio/search?" in req.url):
                        cloud.get_by_role("button", name="Play selection", exact=True).click()
                finally:
                    page.unroute("**/api/session", empty_mix)
                energy = cloud.get_by_role("slider", name="Energy")
                energy.focus()
                energy.press("End")
                expect(item, energy.input_value() == "100", "Assistive tuning could not adjust energy")
                page.unroute("**/api/radio/search?*")

                page.get_by_role("button", name="Library", exact=True).click()
                page.locator(".playlist-card", has_text="Evening").click()
                page.wait_for_function("document.querySelectorAll('.track-row').length === 5")
                page.get_by_role("button", name="Play Atlas Air", exact=True).click()
                item.ok = True
                item.detail = "Touch and keyboard blends, genre playback without a track list, YouTube and catalog discovery"
            except Exception as error:
                item.detail = str(error)

            # -- Genre tag filter --------------------------------------
            item = surface("genre-tag-filter")
            try:
                # The input's accessible name is its label ("Genre");
                # the placeholder is the stable handle.
                box = page.get_by_placeholder("Add a genre")
                box.click()
                box.fill("Roc")
                page.get_by_role("option", name="Rock").click(
                    timeout=10000)
                page.get_by_role("button", name="Remove Rock").wait_for(
                    timeout=10000)
                page.wait_for_function(
                    "document.querySelectorAll('.track-row')"
                    ".length === 1", timeout=15000)
                expect(item, "Asleep" in " ".join(
                    page.locator(".track-row").all_inner_texts()),
                    "Rock filter shows no Asleep")
                item.ok = True
                item.detail = "Rock tag filters the table to the one Rock track"
                shot(item, "02-genre-filter.png")
                page.get_by_role("button", name="Remove Rock").click()
                page.wait_for_function(
                    "document.querySelectorAll('.track-row')"
                    ".length === 5", timeout=15000)
            except Exception as error:
                item.detail = str(error)

            # -- Explore tab --------------------------------------------
            item = surface("explore-tab")
            try:
                search = page.locator(".topbar input").first
                search.click()
                search.fill("Massive Attack")
                search.press("Enter")
                explore_tab = page.get_by_role("tab", name="Explore")

                def explore_selected():
                    return (explore_tab.get_attribute("aria-selected")
                            == "true"
                            or explore_tab.get_attribute("data-state")
                            == "active")
                wait_for("the Explore tab to select", explore_selected,
                         timeout=30)

                def explore_settled():
                    return (page.locator("h2", has_text="Explore results")
                            .count() > 0
                            or page.locator(".external-fallback").count() > 0
                            or page.locator(".ui-state").count() > 0)
                wait_for("the explore search to settle", explore_settled,
                         timeout=60)
                item.ok = True
                item.detail = ("Explore tab auto-selected; the search "
                               "settles into results or a stated fallback")
                shot(item, "03-explore.png")
                page.get_by_role("button", name="Library", exact=True).click()
                page.wait_for_selector("section.playlist-shelf", timeout=15000)
            except Exception as error:
                item.detail = str(error)

            # -- Merge from a playlist ----------------------------------
            item = surface("playlist-merge")
            try:
                merge = page.get_by_role("button", name="Merge playlist", exact=True)
                assert not merge.is_visible()
                cards = page.locator("section.playlist-shelf .playlist-card")
                source_name = cards.nth(0).locator("strong").inner_text()
                other_name = cards.nth(1).locator("strong").inner_text()
                cards.nth(0).click()
                page.get_by_role("heading", name=source_name, exact=True).wait_for()
                merge.click()
                dialog = page.get_by_role("dialog", name="Merge playlist")
                assert dialog.get_by_role("checkbox", name=source_name, exact=True).is_checked()
                assert dialog.get_by_role("button", name="Merge", exact=True).is_disabled()
                dialog.get_by_role("checkbox", name=other_name, exact=True).check()
                assert dialog.get_by_role("button", name="Merge", exact=True).is_enabled()
                shot(item, "04-playlist-merge.png")
                dialog.get_by_role("button", name="Close merge playlist").click()
                page.get_by_role("button", name="Back to library").click()
                cards.nth(0).wait_for()
                item.ok = True
                item.detail = "A room playlist opens Merge with its source already selected"
            except Exception as error:
                item.detail = str(error)
            finally:
                close_any_dialog()

            # -- Queue: render + reorder --------------------------------
            item = surface("queue-reorder")
            try:
                page.wait_for_selector(".queue-panel .queue-item",
                                       timeout=20000)
                items = page.locator(".queue-panel .queue-item")
                expect(item, items.count() >= 2,
                       f"queue holds {items.count()} items, want >= 2")

                def titles():
                    current = page.locator(".queue-panel .queue-item")
                    return [current.nth(i).locator("strong").inner_text()
                            for i in range(current.count())]
                before = titles()
                page.get_by_role(
                    "button", name=f"Move {before[0]} down").click()
                wait_for("queue order to change",
                         lambda: titles() != before, timeout=15)
                item.ok = True
                item.detail = f"queue reordered: {before[0]} moved down"
                shot(item, "05-queue.png")
            except Exception as error:
                item.detail = str(error)

            # -- Player while playing ------------------------------------
            item = surface("player-playing")
            try:
                page.wait_for_selector(".player .player-track",
                                       timeout=20000)
                expect(item, "Atlas Air" in page.locator(
                    ".player").inner_text(), "player shows no current track")

                def playing():
                    return page.evaluate(
                        "[...document.querySelectorAll('audio')].some("
                        "a => !a.paused && a.currentTime > 0.2)")
                try:
                    wait_for("audio playback", playing, timeout=6)
                except TimeoutError:
                    page.locator(".player-toggle").click()
                    wait_for("audio playback after toggle", playing,
                             timeout=10)
                assert page.get_by_text("Playing together", exact=True).count() == 0
                assert page.get_by_role("slider", name="Volume", exact=True).is_visible()
                item.ok = True
                item.detail = "Atlas Air loaded, audio advancing and volume still available"
                shot(item, "06-player.png")
            except Exception as error:
                item.detail = str(error)

            # -- Song dialog ----------------------------------------------
            item = surface("song-dialog")
            try:
                page.get_by_role(
                    "button", name="Song details for Asleep").click()
                page.wait_for_selector(".connections-dialog", timeout=15000)
                expect(item, "Asleep" in page.locator(
                    ".connections-dialog").inner_text(),
                    "song dialog shows the wrong song")
                item.ok = True
                item.detail = "song dialog opens on Asleep"
                shot(item, "07-song-dialog.png")
            except Exception as error:
                item.detail = str(error)

            # -- Artwork dialog (from the song dialog's artwork) ---------
            item = surface("artwork-dialog")
            try:
                # Scoped to the dialog: the player has an "Expand
                # artwork" button of its own behind the modal.
                page.locator(".connections-dialog").get_by_role(
                    "button", name="Expand artwork").click()
                page.wait_for_selector(".artwork-dialog", timeout=15000)
                expect(item, page.locator(".artwork-dialog").is_visible(),
                       "artwork dialog not visible")
                item.ok = True
                item.detail = "artwork dialog opens from the song dialog"
                shot(item, "08-artwork-dialog.png")
                page.get_by_role("button", name="Close artwork").click()
                page.wait_for_selector(".artwork-dialog", state="detached",
                                       timeout=10000)
                page.get_by_role(
                    "button", name="Close song information").click()
                page.wait_for_selector(".connections-dialog", state="detached",
                                       timeout=10000)
            except Exception as error:
                item.detail = str(error)
            finally:
                close_any_dialog()

            # -- Lyrics stage (plan 049), from the player ---------------
            item = surface("lyrics-stage")
            try:
                # The player's lyrics button appears once its probe of
                # the seeded lyrics lands; no dialog is open here, so
                # the role name is the player's alone.
                page.get_by_role("button", name="Show lyrics").click(
                    timeout=15000)
                page.wait_for_selector(".lyrics-stage", timeout=15000)
                lines = page.locator(".lyrics-stage-line")
                wait_for("the stage's lines to render",
                         lambda: lines.count() >= 5, timeout=15)
                expect(item, lines.count() >= 5,
                       f"stage renders {lines.count()} lines, want >= 5")

                def current_line():
                    current = page.locator(
                        ".lyrics-stage-line.is-current")
                    return current if current.count() == 1 else None
                wait_for("a current line to track playback",
                         current_line, timeout=15)

                def centered():
                    return page.evaluate(
                        "() => {"
                        " const box = document.querySelector("
                        "'.lyrics-stage-lines');"
                        " const line = document.querySelector("
                        "'.lyrics-stage-line.is-current');"
                        " if (!box || !line) return false;"
                        " const b = box.getBoundingClientRect();"
                        " const l = line.getBoundingClientRect();"
                        " const drift = Math.abs("
                        "(l.top + l.height / 2)"
                        " - (b.top + b.height / 2));"
                        " return drift < b.height * 0.2; }")
                wait_for("the current line to sit near center",
                         centered, timeout=15)
                shot(item, "14-lyrics-stage.png")

                # Clicking the 30-second line seeks: the session moves
                # and that line becomes the current one.
                page.locator(
                    '.lyrics-stage-line[data-t="30"]').click()

                def sought():
                    current = page.locator(
                        ".lyrics-stage-line.is-current")
                    return (current.count() == 1
                            and current.first.get_attribute("data-t")
                            == "30")
                wait_for("the seek to land on the 30s line", sought,
                         timeout=15)
                page.keyboard.press("Escape")
                page.wait_for_selector(".lyrics-stage", state="detached",
                                       timeout=10000)

                # The same stage opens from the song dialog's row.
                page.get_by_role(
                    "button", name="Song details for Atlas Air").click()
                page.wait_for_selector(".connections-dialog", timeout=15000)
                page.locator(".connections-dialog").get_by_role(
                    "button", name="Show lyrics").click()
                page.wait_for_selector(".lyrics-stage", timeout=15000)
                shot(item, "15-lyrics-stage-dialog.png")
                page.locator(".lyrics-stage .dialog-close").click()
                page.wait_for_selector(".lyrics-stage", state="detached",
                                       timeout=10000)
                page.get_by_role(
                    "button", name="Close song information").click()
                page.wait_for_selector(".connections-dialog", state="detached",
                                       timeout=10000)
                item.ok = True
                item.detail = ("stage opens from player and dialog, "
                               "current line tracks and centers, "
                               "line click seeks to 30s")
            except Exception as error:
                item.detail = str(error)
            finally:
                close_any_dialog()

            # -- Genre combobox in the song dialog (plan 049) ------------
            item = surface("genre-combobox")
            try:
                page.get_by_role(
                    "button", name="Song details for Asleep").click()
                page.wait_for_selector(".connections-dialog", timeout=15000)
                box = page.get_by_placeholder("Electronic, jazz, ambient…")
                box.click()
                box.fill("Roc")
                page.get_by_role("option", name="Rock").wait_for(
                    timeout=10000)
                expect(item, page.get_by_role(
                    "option", name="Electronic").count() == 0,
                    "non-matching facet Electronic still offered")
                shot(item, "16-genre-combobox.png")
                page.get_by_role("option", name="Rock").click()
                # The facets are canonical forms (the genre layer
                # case-folds: "Rock" the raw tag is "rock" the
                # canonical bucket), so the pick fills the field
                # with the canonical string, exactly as the tag
                # filter's suggestions do.
                expect(item, box.input_value() == "rock",
                       f"picking the Rock facet left "
                       f"{box.input_value()!r} in the field")
                item.ok = True
                item.detail = ("combobox suggests from the canonical "
                               "facet list, filters as typed, pick "
                               "fills the field")
                page.get_by_role(
                    "button", name="Close song information").click()
                page.wait_for_selector(".connections-dialog", state="detached",
                                       timeout=10000)
            except Exception as error:
                item.detail = str(error)
            finally:
                close_any_dialog()

            # -- Activity dialog -------------------------------------------
            item = surface("activity-dialog")
            try:
                page.locator(".activity-bar").click()
                page.wait_for_selector(".activity-dialog", timeout=15000)
                page.wait_for_selector(".activity-event", timeout=15000)
                events = page.locator(".activity-event")
                expect(item, events.count() == 5,
                       f"activity feed holds {events.count()} events, "
                       "want 5 (one per song, latest state)")
                item.ok = True
                item.detail = "activity feed shows the 5 fixture songs"
                shot(item, "09-activity-dialog.png")
                page.get_by_role(
                    "button", name="Close download activity").click()
                page.wait_for_selector(".activity-dialog", state="detached",
                                       timeout=10000)
            except Exception as error:
                item.detail = str(error)
            finally:
                close_any_dialog()

            # -- Jobs ------------------------------------------------------
            item = surface("jobs")
            try:
                page.get_by_role("button", name="Jobs").click()
                page.wait_for_selector(".job-card", timeout=20000)
                # The upkeep card rides the activity poll, which can
                # land a beat after the job cards: wait for all five.
                page.wait_for_function(
                    "document.querySelectorAll('.job-card').length === 5",
                    timeout=20000)
                cards = page.locator(".job-card")
                expect(item, cards.count() == 5,
                       f"jobs view holds {cards.count()} cards, want 5")
                refresh = page.get_by_role("button", name="Refresh jobs", exact=True)
                background = refresh.evaluate("button => getComputedStyle(button).backgroundColor")
                refresh.hover()
                assert refresh.evaluate("button => getComputedStyle(button).backgroundColor") != background
                with page.expect_response(lambda res: res.url.endswith("/api/jobs") and res.request.method == "GET" and res.ok):
                    refresh.click()
                page.get_by_title("Refreshed jobs", exact=True).wait_for()
                downloads = page.get_by_role("region", name="Download missing songs")
                format_picker = downloads.get_by_role("combobox", name="Download format", exact=True)
                format_picker.get_by_text("Room default (MP3 first)").wait_for()
                format_picker.click()
                page.get_by_role("option", name="MP3 only", exact=True).click()
                format_picker.get_by_text("MP3 only", exact=True).wait_for()
                genre = page.get_by_role("region", name="Categorize genres")
                genre.get_by_text("Audio file tags, then MusicBrainz and ListenBrainz", exact=True).wait_for()
                genre.get_by_role("button", name="Find missing genres").click()
                genre.get_by_text("No genres found", exact=True).wait_for(timeout=45000)
                genre.get_by_text("Songs checked this run", exact=True).wait_for()
                genre.get_by_text(re.compile(r"of [\d,]+ checked · 0 newly tagged")).wait_for()
                header = page.get_by_role("heading", name="Jobs", exact=True).bounding_box()
                assert abs(header["x"] - cards.first.bounding_box()["x"]) < 2
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator(".job-card .status-pill").first.wait_for(state="visible")
                page.set_viewport_size({"width": 1440, "height": 900})
                item.ok = True
                item.detail = "four job cards render (the report cycle creates the media database, so the reclaim card shows; the settled migration marker keeps the migrate card retired), plus the room upkeep card fed by the activity snapshot's watchdog block"
                shot(item, "10-jobs.png")
            except Exception as error:
                item.detail = str(error)

            # -- Settings, every tab ---------------------------------------
            tabs = ["Room setup", "Sources", "Downloads", "Spotify", "Playback",
                    "Radio", "Devices", "Sharing", "Storage", "Extensions"]
            plugin = {"id": "fixture", "version": "1.0", "capabilities": ["search", "fetch"], "enabled": False,
                      "sources": {"search": ["fixture"], "fetch": ["fixture"]}, "image": "fixture@sha256:same",
                      "settings": [{"key": "api_token", "label": "Catalog token",
                                    "type": "secret", "required": True}]}
            plugin.update(enabled=True, ready=True, retry_at=int(time.time()) + 1800)
            signed_update = {**plugin, "server_versions": ["1.5.0"]}
            disabled = {"id": "paused-engine", "version": "1.0", "capabilities": ["search", "fetch"],
                        "sources": {"search": ["fixture"], "fetch": ["fixture"]},
                        "enabled": False, "ready": False}
            healthy = {**disabled, "id": "ready-engine", "enabled": True, "ready": True}
            available = {**disabled, "id": "example-catalog"}
            extension_settings = {"radio": [], "installed": [plugin, disabled, healthy], "available": [signed_update, available],
                                  "configured": True, "selection": {"search": "paused-engine", "fetch": "paused-engine"}, "error": ""}

            def engine_settings(route):
                if route.request.method == "POST":
                    body = route.request.post_data_json
                    if body["action"] == "enable":
                        assert body == {"action": "enable", "engine": "paused-engine"}
                        disabled["retry_at"] = int(time.time()) + 1800
                        route.fulfill(status=503, json={"error": "Unable to start the source."})
                        return
                    if body["action"] == "select":
                        assert body["selection"] == {"search": "ready-engine", "fetch": "ready-engine"}
                        extension_settings["selection"] = body["selection"]
                    elif body["action"] == "radio":
                        assert body == {"action": "radio", "provider": "youtube", "enabled": True}
                        extension_settings["radio"] = ["youtube"]
                    else:
                        assert body == {
                            "action": "configure", "engine": "fixture",
                            "settings": {"api_token": "disposable-playwright-credential"},
                        }
                        plugin["configured_settings"] = ["api_token"]
                route.fulfill(json=extension_settings)

            page.route("**/api/extensions", engine_settings)
            try:
                page.get_by_role("button", name="Settings").click(
                    timeout=15000)
                page.get_by_role("tab", name="Room setup").wait_for(timeout=20000)
            except Exception as error:
                for label in tabs:
                    item = surface(
                        f"settings-{label.lower().replace(' ', '-')}")
                    item.detail = f"could not open Settings: {error}"
                tabs = []
            for label in tabs:
                item = surface(f"settings-{label.lower().replace(' ', '-')}")
                try:
                    tab = page.get_by_role("tab", name=label, exact=True)
                    tab.click()

                    def selected():
                        return (tab.get_attribute("aria-selected") == "true"
                                or tab.get_attribute("data-state") == "active")
                    wait_for(f"the {label} tab to select", selected,
                             timeout=15)
                    visible_panel = page.locator(
                        ".settings-panel:visible").first
                    visible_panel.wait_for(state="visible", timeout=15000)
                    expect(item,
                           len(visible_panel.inner_text().strip()) > 0,
                           f"{label} panel renders empty")
                    if label == "Playback":
                        buffer = visible_panel.get_by_role("combobox", name="Buffer ahead")
                        before = buffer.inner_text()
                        buffer.focus()
                        buffer.press("Enter")
                        page.get_by_role("listbox").wait_for()
                        page.keyboard.press("End")
                        page.keyboard.press("Escape")
                        assert buffer.inner_text() == before
                        assert buffer.evaluate("el => document.activeElement === el")
                    if label == "Downloads":
                        default_format = visible_panel.get_by_role("combobox", name="Default format for bulk downloads")
                        assert default_format.inner_text().strip() == "MP3 first"
                        default_format.click()
                        page.get_by_role("option", name="Lossless only", exact=True).click()
                        assert default_format.inner_text().strip() == "Lossless only"
                        default_format.click()
                        page.get_by_role("option", name="MP3 first", exact=True).click()
                    if label == "Extensions":
                        for name, url in (
                            ("Plugin setup guide", "https://github.com/Warkanlock/listening-public/blob/main/docs/download-engines.md"),
                            ("Listening Plugins on GitHub", "https://github.com/Warkanlock/listening-plugins-public"),
                        ):
                            link = visible_panel.get_by_role("link", name=name, exact=True)
                            assert link.is_visible() and link.get_attribute("href") == url
                        table = visible_panel.get_by_role("table", name="Download extensions")
                        assert table.get_by_role("columnheader").all_text_contents() == ["Extension", "Version", "Status", "Actions"]
                        install = table.get_by_role("button", name="Install example-catalog", exact=True)
                        assert install.is_enabled() and install.inner_text() == ""
                        update = table.get_by_role("button", name="Update fixture", exact=True)
                        update.wait_for()
                        assert update.is_enabled()
                        visible_panel.get_by_text("Sign-in failed. Listening can try again", exact=False).wait_for()
                        paused = table.get_by_role("row").filter(has=page.get_by_role("rowheader", name="paused-engine", exact=True))
                        assert paused.get_by_text("Disabled", exact=True).is_visible()
                        enable = paused.get_by_role("button", name="Enable paused-engine", exact=True)
                        assert enable.is_enabled()
                        enable.click()
                        visible_panel.get_by_text("Sign-in failed. You can try enabling this extension after", exact=False).wait_for()
                        assert enable.is_disabled()
                        visible_panel.get_by_text("Unable to start the source.").wait_for()
                        visible_panel.get_by_text("Change download sources", exact=True).click()
                        for role in ("Search with", "Download with"):
                            visible_panel.get_by_role("combobox", name=role).click()
                            page.get_by_role("option", name="ready-engine", exact=True).click()
                        visible_panel.get_by_role("button", name="Save sources").click()
                        wait_for("Source selection saved", lambda: visible_panel.locator(".extension-source-summary").inner_text().count("ready-engine") == 2)
                        refresh = visible_panel.get_by_role("button", name="Refresh extensions", exact=True)
                        assert refresh.evaluate("element => parseFloat(getComputedStyle(element).minHeight) >= 40")
                        radio_switch = visible_panel.get_by_role("switch", name="YouTube mixes")
                        assert radio_switch.get_attribute("aria-checked") == "false"
                        radio_switch.click()
                        radio_switch.wait_for()
                        wait_for("Radio provider enabled", lambda: radio_switch.get_attribute("aria-checked") == "true")
                        remove = table.get_by_role("button", name="Remove fixture", exact=True)
                        update.focus()
                        page.keyboard.press("Tab")
                        assert remove.evaluate("(element) => element === document.activeElement")
                        page.get_by_role("tooltip", name="Remove fixture", exact=True).wait_for()
                        page.keyboard.press("Escape")
                        page.get_by_role("tooltip", name="Remove fixture", exact=True).wait_for(state="hidden")
                        remove.click()
                        page.get_by_role("alertdialog", name="Remove fixture?").get_by_role("button", name="Cancel").click()
                        assert table.get_by_role("rowheader", name="fixture", exact=True).is_visible()
                        table.get_by_role("button", name="Configure fixture", exact=True).click()
                        token = visible_panel.get_by_label("Catalog token (required to start)", exact=True)
                        assert token.get_attribute("type") == "password"
                        token.fill("disposable-playwright-credential")
                        visible_panel.get_by_role("button", name="Save engine settings").click()
                        visible_panel.get_by_placeholder("Saved · leave blank to keep").wait_for()
                        assert token.input_value() == ""
                    item.ok = True
                    item.detail = ("Styled actions, disabled source recovery, Radio switch, and write-only credential form"
                                   if label == "Extensions" else f"{label} panel renders")
                    shot(item, f"11-settings-{label.lower().replace(' ', '-')}.png")
                except Exception as error:
                    item.detail = str(error)

            item = surface("extensions-page")
            viewport = page.viewport_size
            try:
                page.goto(server.base + "/#extensions")
                view = page.locator('[data-view="extensions"]')
                view.get_by_role("heading", name="Extensions", exact=True).wait_for()
                view.get_by_role("heading", name="Download extensions").wait_for()
                shot(item, "34-extensions-desktop.png")
                page.set_viewport_size({"width": 390, "height": 844})
                refresh = view.get_by_role("button", name="Refresh extensions")
                assert refresh.is_visible()
                assert view.evaluate("(element) => element.scrollWidth <= element.clientWidth + 1")
                row = view.get_by_role("row").filter(has=page.get_by_role("rowheader", name="fixture", exact=True))
                assert row.evaluate("(el) => el.scrollWidth <= el.clientWidth + 1")
                action = row.get_by_role("button", name="Remove fixture")
                assert action.evaluate("(el) => el.offsetWidth >= 44 && el.offsetHeight >= 44")
                shot(item, "34-extensions-phone.png")
                item.ok = True
                item.detail = "Standalone Extensions uses responsive cards and shared controls"
            except Exception as error:
                item.detail = str(error)
            finally:
                page.set_viewport_size(viewport)
                page.unroute("**/api/extensions")

            # -- Deep Dive ---------------------------------------------------
            item = surface("deep-dive")
            try:
                verify_discovery_trail(page, lambda: shot(item, "12-deep-dive.png"))
                item.ok = True
                item.detail = "Weekly trail, another performance, retry, listening actions, focus and phone layout"
            except Exception as error:
                item.detail = str(error)

            # -- Requests ---------------------------------------
            item = surface("requests")
            try:
                page.get_by_role("button", name="Requests", exact=True).click()
                page.get_by_role("heading", name="Recent requests").wait_for(timeout=20000)
                extensions = page.get_by_role("button", name="View extensions", exact=True)
                refresh_requests = page.get_by_role("button", name="Refresh requests", exact=True)
                for width, height in ((1440, 900), (320, 740), (390, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    left, right = extensions.bounding_box(), refresh_requests.bounding_box()
                    assert left and right and left["x"] + left["width"] <= right["x"] and abs(left["y"] - right["y"]) < 1
                extensions.click()
                page.get_by_role("dialog", name="Extensions", exact=True).get_by_role("button", name="Close extensions").click()
                page.set_viewport_size({"width": 1440, "height": 900})
                queued = page.locator(".request-item", has_text="Feeling Good")
                scheduled = queued.locator(".request-status").filter(has_text=re.compile("^Retry scheduled$"))
                scheduled.wait_for(timeout=20000)
                missing_copy = page.locator(".request-item", has_text="Unavailable Copy")
                result = missing_copy.get_by_text("No copy found", exact=True)
                retry = missing_copy.get_by_role("button", name="Try MP3 for Unavailable Copy", exact=True)
                result.wait_for(state="visible")
                retry.wait_for(state="visible")
                page.set_viewport_size({"width": 390, "height": 844})
                result.wait_for(state="visible")
                retry.wait_for(state="visible")
                manual_retry = queued.get_by_role("button", name="Retry now: Feeling Good", exact=True)
                with page.expect_response(lambda res: res.url.endswith("/api/requests/retry") and res.ok):
                    manual_retry.click()
                queued.locator(".request-status").filter(has_text=re.compile("^Queued$")).wait_for()
                live_id = "00000000-0000-4000-8000-000000000091"
                studio_id = "00000000-0000-4000-8000-000000000092"
                versions = [{
                    "kind": "recording", "track": {"musicbrainz_id": mbid, "title": "Feeling Good", "artist": "Nina Simone",
                                                  "duration": duration, "version": kind, "album": album}
                } for mbid, kind, album, duration in (
                    (live_id, "Live", "Concert sessions", 185), (studio_id, "Studio", "Studio album", 190))]
                page.route("**/api/discover/matches?*", lambda route: route.fulfill(json={"leads": versions}))
                try:
                    queued.get_by_role("button", name="Choose a version: Feeling Good").click()
                    picker = page.get_by_role("dialog", name="Choose a recording")
                    picker.get_by_role("button", name=re.compile("Studio album")).wait_for()
                    with page.expect_response(lambda res: res.url.endswith("/api/requests") and res.request.method == "POST" and res.ok) as submitted:
                        picker.get_by_role("button", name=re.compile("Concert sessions")).click()
                    assert submitted.value.json()["musicbrainz_id"] == live_id
                    assert submitted.value.json()["length"] == 185
                    page.get_by_role("dialog", name="Choose format").get_by_role("button", name="Later").click()
                finally:
                    page.unroute("**/api/discover/matches?*")
                page.set_viewport_size({"width": 1440, "height": 900})
                item.ok = True
                item.detail = "Retry scheduled chip, manual retry, recording choices, and no-copy action at mobile width"
                shot(item, "13-requests.png")
            except Exception as error:
                item.detail = str(error)
            finally:
                page.set_viewport_size({"width": 1440, "height": 900})
                close_any_dialog()

            item = surface("recent-downloads")
            try:
                download = {"id": 902, "track_index": 1, "title": "Verified fixture copy", "artist": "Fixture",
                            "audio": "/api/audio/1"}
                request_data = {"requests": [], "downloads": [download], "outcomes": []}
                page.route("**/api/requests", lambda route: route.fulfill(json=request_data))
                with page.expect_response(lambda response: response.url.endswith("/api/requests")):
                    page.get_by_role("button", name="Refresh requests").click()
                page.get_by_title("Refreshed requests", exact=True).wait_for()
                request_data["outcomes"] = [{"key": "fixture/902", "title": download["title"], "status": "downloaded"}]
                page.get_by_role("button", name="Refresh requests").click()
                page.get_by_text("Verified fixture copy · Ready to play", exact=True).wait_for()
                page.get_by_role("button", name="Library", exact=True).click()
                recent = page.get_by_role("region", name="Recent downloads")
                play = recent.get_by_role("button", name="Play Verified fixture copy")
                play.wait_for()
                with page.expect_request(lambda request: request.url.endswith("/api/session") and request.method == "POST") as sent:
                    play.click()
                assert sent.value.post_data_json["track_id"] == 1 and sent.value.post_data_json["op"] == "play"
                page.get_by_role("button", name="Pause", exact=True).first.click()
                item.ok = True
                item.detail = "Find Copy outcome notices and direct playback in the Library shelf"
            except Exception as error:
                item.detail = str(error)
            finally:
                page.unroute("**/api/requests")

            # -- Your listening (plan 050 step 5) -------------------------
            # The seeded history plus whatever the live session added
            # during this run: the DOM order must equal the API's
            # order at the same moment, and the seeded facts hold
            # (a skipped-only song is recent but never most played;
            # five completions cannot be caught by a live play).
            def dom_titles():
                return [row.locator(".track-title").inner_text()
                        for row in page.locator(".track-row").all()]

            def api_titles(filter_name):
                status, payload = server.request(
                    "GET", f"/api/tracks?filter={filter_name}&limit=100")
                if status != 200:
                    return None
                return [track["title"] for track in payload["tracks"]]

            item = surface("recently-played")
            try:
                page.get_by_role("button", name="Recently played").click()
                page.wait_for_function(
                    "document.querySelector('.tracks-section h1')"
                    ".textContent === 'Recently played'", timeout=20000)
                titles = wait_for(
                    "the recently played rows to match the API",
                    lambda: (dom_titles()
                             if dom_titles() == api_titles("recent")
                             else None),
                    timeout=20)
                expect(item, "Blame" in titles,
                       "the skipped-only song is missing from recency")
                expect(item, "Missing Song" not in titles,
                       "a never-played song is listed as recent")
                item.ok = True
                item.detail = ("recently played renders the API order: "
                               + ", ".join(titles))
                shot(item, "14-recently-played.png")
            except Exception as error:
                item.detail = str(error)

            item = surface("most-played")
            try:
                page.get_by_role("button", name="Most played").click()
                page.wait_for_function(
                    "document.querySelector('.tracks-section h1')"
                    ".textContent === 'Most played'", timeout=20000)
                titles = wait_for(
                    "the most played rows to match the API",
                    lambda: (dom_titles()
                             if dom_titles() == api_titles("most")
                             else None),
                    timeout=20)
                expect(item, titles[0] == "Paradise Circus",
                       f"most played leads with {titles[0]!r}, "
                       "want Paradise Circus (5 completions)")
                expect(item, "Blame" not in titles,
                       "a skipped-only song counts as most played")
                item.ok = True
                item.detail = ("most played orders by completed plays: "
                               + ", ".join(titles))
                shot(item, "15-most-played.png")
            except Exception as error:
                item.detail = str(error)

            # -- Radio (plan 059) --------------------------------------------
            item = surface("radio")
            try:
                page.get_by_role("button", name="Radio", exact=True).click()
                radio = page.locator(".radio-view")
                search = radio.get_by_role("searchbox", name="Find a station")
                search.wait_for(timeout=20000)
                expect(item, radio.get_by_role("list", name="Live stations").get_by_role("button").count() <= 4,
                       "Radio starts with a long station list")
                expect(item, radio.locator(".radio-result").filter(has_text="Your library").count() <= 2,
                       "Radio starts with too many Library genres")
                expect(item, radio.locator(".radio-player-mount").count() == 1, "Radio player mount missing")

                stage = radio.locator(".radio-stage")
                dock = page.locator(".player")

                def check_links(href):
                    for surface in (stage, dock):
                        link = surface.locator(f'.radio-source a[href="{href}"]')
                        link.click(trial=True)
                        expect(item, link.get_attribute("target") == "_blank", "Source link would replace the player")

                def check_video():
                    video = page.evaluate("window.fixtureRadio.videos[window.fixtureRadio.index]")
                    check_links(f"https://www.youtube.com/watch?v={video}")
                    return video

                with urllib.request.urlopen(server.base + "/api/audio/1", timeout=10) as response:
                    audio = response.read()
                page.route("https://ice2.somafm.com/**", lambda route: route.fulfill(content_type="audio/mpeg", body=audio))
                search.fill("ambient")
                radio.get_by_role("button", name="Play Ambient · SomaFM · Drone Zone", exact=True).click()
                radio.locator(".radio-stage").get_by_text("Drone Zone").wait_for()
                check_links("https://ice2.somafm.com/dronezone-128-mp3")
                radio.get_by_role("button", name="Pause radio", exact=True).wait_for()
                radio.get_by_role("button", name="Stop radio", exact=True).click()
                search.fill("brown")
                radio.get_by_role("button", name="Play Brown noise · Generated on this device").click()
                radio.locator(".radio-stage").get_by_text("Generated on this device").wait_for()
                dock.locator(".radio-source").get_by_text("This device", exact=False).wait_for()
                radio.get_by_role("button", name="Stop radio", exact=True).click()

                search.fill("unavailable genre")
                radio.get_by_text("No matching stations or playable genres.").wait_for()
                search.fill("elec")
                library_station = radio.get_by_role("button", name="Play electronic · Your library · 2 songs")
                library_station.wait_for()
                search.fill("ambient")
                radio.get_by_role("button", name="Play Ambient · SomaFM · Drone Zone", exact=True).click()
                radio.get_by_role("button", name="Pause radio", exact=True).wait_for()
                held = []
                page.route("**/api/radio/library?*", lambda route: held.append(route))
                search.fill("elec")
                with page.expect_request(lambda request: "/api/radio/library?" in request.url):
                    library_station.click()
                radio.get_by_text("Loading songs for electronic…", exact=True).wait_for()
                with page.expect_event("requestfailed", predicate=lambda request: "/api/radio/library?" in request.url):
                    radio.get_by_role("button", name="Stop radio", exact=True).click()
                radio.locator(".radio-controls").wait_for(state="hidden")
                page.unroute("**/api/radio/library?*")
                expect(item, not radio.get_by_text("Loading songs for electronic…", exact=True).count(),
                       "Stopping Radio left a library tune pending")
                before = server.request("GET", "/api/session")[1]
                with page.expect_request(lambda request: "/api/radio/library?" in request.url):
                    search.press("Enter")
                pause = radio.get_by_role("button", name="Pause radio", exact=True)
                pause.wait_for(timeout=10000)
                title = radio.locator(".radio-stage-copy strong")
                first = title.inner_text()
                stage.get_by_role("button", name="Open song in your library").click()
                page.get_by_role("dialog").get_by_text(first, exact=True).first.wait_for()
                close_any_dialog()
                expect(item, first in {"Paradise Circus", "Atlas Air"}, f"{first!r} is not a verified electronic song")
                radio.get_by_role("button", name="Next track", exact=True).click()
                second = "Atlas Air" if first == "Paradise Circus" else "Paradise Circus"
                radio.locator(".radio-stage-copy").get_by_text(second, exact=True).wait_for()
                radio.get_by_role("button", name="Next track", exact=True).click()
                radio.locator(".radio-stage-copy").get_by_text(first, exact=True).wait_for()
                expect(item, server.request("GET", "/api/session")[1]["current_ref"] == before["current_ref"],
                       "Tuning a library station changed the room's shared current song")
                for width in (320, 390):
                    page.set_viewport_size({"width": width, "height": 800})
                    pause.click()
                    radio.get_by_role("button", name="Play radio", exact=True).click()
                    pause.wait_for()
                    expect(item, radio.evaluate("node => node.scrollWidth <= node.clientWidth"),
                           f"Radio overflows at {width}px")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.get_by_role("button", name="Library", exact=True).click()
                dock.get_by_role("button", name="Pause radio").wait_for()
                page.reload()
                dock.get_by_role("button", name="Play radio").wait_for()
                dock.get_by_text(first, exact=True).wait_for()
                dock.get_by_role("button", name="Play radio").click()
                dock.get_by_role("button", name="Pause radio").wait_for()
                dock.get_by_text(first, exact=True).wait_for()
                dock.get_by_role("button", name="Stop radio").click()
                page.reload()
                dock.get_by_role("button", name="Play radio").wait_for(state="hidden")
                page.get_by_role("button", name="Radio", exact=True).click()
                search.fill("elec")
                shot(item, "16-radio.png")
                rainy_videos = [f"rainy{index:06d}" for index in range(10)]
                status, _ = server.request("POST", "/api/stations", {
                    "stations": [
                        {"name": "Electronic", "seeds": ["vugodYOKeis"]},
                        {"name": "Rainy day", "seeds": rainy_videos},
                        {"name": "Empty mix", "seeds": []},
                    ],
                })
                expect(item, status == 200, "Could not save fixture mixes")
                page.get_by_role("button", name="Radio", exact=True).click()
                search.fill("")
                mixes = radio.get_by_role("region", name="YouTube mixes")
                mixes.get_by_text("2 topics · 11 videos", exact=True).wait_for()
                rainy = mixes.get_by_role("button", name="Play Rainy day · YouTube · 10 videos", exact=True)
                rainy.wait_for()
                expect(item, mixes.get_by_role("button").count() == 2, "Empty topics appear in YouTube mixes")
                radio.get_by_role("list", name="Live stations").get_by_role("button").first.wait_for()
                shot(item, "16b-youtube-mixes.png")
                search.fill("rain")
                expect(item, mixes.get_by_role("button").count() == 1, "Search did not filter YouTube topics")
                search.press("Enter")
                radio.get_by_role("button", name="Pause radio", exact=True).wait_for()
                radio.locator(".radio-stage").get_by_text(re.compile(r"^Fixture radio \d+$")).wait_for()
                expect(item, set(page.evaluate("window.fixtureRadio.videos")) == set(rainy_videos),
                       "Radio did not load the saved topic's video queue")
                first_video = check_video()
                shot(item, "16c-radio-source.png")
                page.evaluate("window.fixtureRadio.state(0); window.fixtureRadio.nextVideo()")
                auto_video = check_video()
                expect(item, auto_video != first_video, "Automatic advance kept the previous source")
                page.evaluate("window.omitVideoId = true")
                radio.get_by_role("button", name="Next track").click()
                next_video = check_video()
                expect(item, next_video != auto_video, "Next kept the previous source")
                radio.get_by_role("button", name="Pause radio", exact=True).click()
                radio.get_by_role("button", name="Next track").click()
                paused_video = check_video()
                expect(item, paused_video != next_video, "Paused skip kept the previous source")
                radio.get_by_role("button", name="Play radio", exact=True).wait_for()
                page.set_viewport_size({"width": 320, "height": 800})
                check_video()
                page.set_viewport_size({"width": 1440, "height": 900})
                page.get_by_role("button", name="Library", exact=True).click()
                page.reload()
                dock.get_by_role("button", name="Play radio").wait_for()
                saved_href = f"https://www.youtube.com/watch?v={paused_video}"
                dock.locator(f'.radio-source a[href="{saved_href}"]').click(trial=True)
                page.get_by_role("button", name="Radio", exact=True).click()
                check_links(saved_href)
                radio.get_by_role("button", name="Stop radio", exact=True).click()
                page.evaluate("window.blockRadio = true")
                search.fill("electronic")
                mixes.get_by_role("button", name="Play Electronic · YouTube · 1 video", exact=True).wait_for()
                radio.get_by_role("button", name="Play electronic · Your library · 2 songs").wait_for()
                search.press("Enter")
                radio.get_by_role("button", name="Play radio", exact=True).wait_for()
                page.wait_for_function("window.autoplayBlocked === true")
                check_links("https://www.youtube.com/watch?v=vugodYOKeis")
                expect(item, page.evaluate("window.fixtureRadio.videos.includes('vugodYOKeis')"),
                       "Enter did not play the first displayed station")
                radio.get_by_role("button", name="Play radio", exact=True).click()
                radio.get_by_role("button", name="Pause radio", exact=True).wait_for()
                page.route("**/api/radio/library?*", lambda route: held.append(route))
                with page.expect_request(lambda request: "/api/radio/library?" in request.url):
                    library_station.click()
                radio.get_by_text("Loading songs for electronic…", exact=True).wait_for()
                page.context.route("https://www.youtube.com/watch?**", lambda route: route.fulfill(body="YouTube"))
                with page.expect_event("requestfailed", predicate=lambda request: "/api/radio/library?" in request.url):
                    with page.expect_popup() as handoff:
                        stage.get_by_role("link", name="Open in YouTube").click()
                handoff.value.close()
                page.context.unroute("https://www.youtube.com/watch?**")
                radio.get_by_role("button", name="Play radio", exact=True).wait_for()
                expect(item, not radio.get_by_text("Loading songs for electronic…", exact=True).count(),
                       "Opening YouTube left a pending library tune")
                radio.get_by_role("button", name="Stop radio", exact=True).click()
                item.ok = True
                item.detail = "Library radio, live and noise playback, visible YouTube counts, search and keyboard tuning"
            except Exception as error:
                item.detail = str(error)
            finally:
                page.unroute("https://ice2.somafm.com/**")
                page.unroute("**/api/radio/library?*")

            item = surface("tablet-playlist")
            try:
                verify_tablet(browser, server, output)
                item.ok = True
                item.detail = "touch playlist, dock, queue, missing-song YouTube fallback and menu in both orientations"
            except Exception as error:
                item.detail = repr(error)

            item = surface("room-recovery")
            recovery = browser.new_page(service_workers="block")
            try:
                endpoint = "**/api/room"
                loading = lambda route: route.fulfill(json={"state": "catalog_loading"})
                offline = lambda route: route.abort("internetdisconnected")
                expired = lambda route: route.fulfill(status=401, json={"code": "pairing_required"})
                registration = "**/api/device"
                unavailable = lambda route: route.fulfill(status=503, json={"error": "Fixture is starting"})
                recovery.route(endpoint, loading)
                recovery.route(registration, unavailable)
                recovery.goto(server.base + "/")
                recovery.get_by_role("heading", name="Room unreachable").wait_for(timeout=12000)
                recovery.unroute(registration, unavailable)
                recovery.get_by_role("button", name="Retry", exact=True).click()
                recovery.get_by_role("heading", name="Catalog is still loading").wait_for(timeout=12000)
                recovery.unroute(endpoint, loading)
                recovery.route(endpoint, offline)
                recovery.get_by_role("button", name="Retry", exact=True).click()
                recovery.get_by_role("heading", name="Room unreachable").wait_for(timeout=12000)
                recovery.unroute(endpoint, offline)
                recovery.get_by_role("button", name="Retry", exact=True).click()
                recovery.locator(".room-content").wait_for(state="attached")
                recovery.get_by_role("heading", name="Room unreachable").wait_for(state="hidden", timeout=12000)
                recovery.route(endpoint, expired)
                recovery.get_by_role("heading", name="Pairing expired").wait_for(timeout=12000)
                recovery.get_by_role("button", name="Pair again", exact=True).wait_for(timeout=2000)
                verify_lan_pair_again(browser)
                item.ok = True
                item.detail = "retry after failed registration, loading, offline, revoked pairing and alternate invite address"
            except Exception as error:
                item.detail = repr(error)
            finally:
                recovery.close()

            item = surface("request-format-restart")
            try:
                with tempfile.TemporaryDirectory(prefix="listening-format-room-") as root:
                    verify_request_restart(page, Path(root), output / "17-request-format.png")
                item.ok = True
                item.detail = "Format choice survives restart, AAC rejected, verified ALAC and Deep Dive playback, mobile per-song MP3 retry"
            except Exception as error:
                item.detail = repr(error)
            item = surface("private-household-and-catalogs")
            try:
                with tempfile.TemporaryDirectory(prefix="listening-private-browser-") as root:
                    verify_household_browser(browser, Path(root), output / "20-household-devices.png")
                item.ok = True
                item.detail = "Private profiles, device controls, and mobile public and private catalog browse"
            except Exception as error:
                item.detail = repr(error)
            item = surface("mobile-manual-queue-and-shuffle")
            try:
                verify_mobile_queue_and_shuffle(browser, server, errors, warnings)
                item.ok = True
                item.detail = "Touch adds a song ahead of the playlist, and Shuffle is visible in both players"
            except Exception as error:
                item.detail = repr(error)
            item = surface("apple-mobile-audio")
            try:
                verify_apple_background_audio(browser, server, errors, warnings)
                item.ok = True
                item.detail = "Direct media playback, retry after an interrupted queue advance in a hidden page, and playback after return"
            except Exception as error:
                item.detail = repr(error)
            item = surface("playback-outputs")
            try:
                verify_playback_outputs(browser, server, output, errors, warnings)
                item.ok = True
                item.detail = "Mac and phone handoff, retained buffer, window layout, background playback, and idle and closed device cleanup"
            except Exception as error:
                item.detail = repr(error)
            item = surface("standby-buffer")
            try:
                verify_standby_buffer(browser, server, errors, warnings)
                item.ok = True
                item.detail = "One next-song fetch, gapless reuse, bounded fallback, and five-minute handoff expiry"
            except Exception as error:
                item.detail = repr(error)
            item = surface("insights")
            try:
                verify_insights(browser, server, output, errors, warnings)
                item.ok = True
                item.detail = "Measured traffic, full queue count, mobile layout and view-scoped polling"
            except Exception as error:
                item.detail = repr(error)
            item = surface("camping-mode")
            try:
                verify_hiking(browser, server, output, errors, warnings)
                item.ok = True
                item.detail = "Mobile trip preview, interrupted download resume, cold offline launch and playback from local storage"
            except Exception as error:
                item.detail = repr(error)
            item = surface("queue-buffer")
            try:
                verify_queue_buffer(browser, server, errors, warnings)
                item.ok = True
                item.detail = "Four tracks buffered, play and pause, two advances offline and a successful rejoin"
            except Exception as error:
                item.detail = repr(error)
            item = surface("room-view-layout")
            try:
                verify_room_layout(browser, server, output)
                item.ok = True
                item.detail = "Ten room views align with Library on desktop and phone"
            except Exception as error:
                item.detail = repr(error)
            item = surface("nearby-jams")
            try:
                with tempfile.TemporaryDirectory(prefix="listening-ui-jams-") as root:
                    verify_nearby_jams(browser, Path(root), output)
                item.ok = True
                item.detail = "Two household accounts, nearby suggestion, consent, shared song, and independent queues"
            except Exception as error:
                item.detail = repr(error)

            item = surface("artist-connections")
            try:
                verify_artist_connections(browser, server, output, errors, warnings)
                item.ok = True
                item.detail = "Viewport centering, slow credits skeletons, cached back and tab navigation, drag, touch and reduced motion"
            except Exception as error:
                item.detail = repr(error)
            item = surface("music-profiles")
            profiles = browser.new_page(viewport={"width": 1440, "height": 900}, service_workers="block")
            try:
                profiles.goto(server.base + "/")
                row = profiles.locator(".track-row").filter(has=profiles.get_by_role("button", name="Asleep", exact=True))
                row.wait_for(timeout=15000)
                index = profiles.evaluate("""async () => {
                    const result = await fetch('/api/tracks').then(response => response.json());
                    return result.tracks.find(track => track.title === 'Asleep').id;
                }""")
                profiles_panel = profiles.get_by_role("region", name="Music profile")
                artist_path = f"**/api/music-profile/artist/{index}"
                album_path = f"**/api/music-profile/album/{index}"
                pending_profile = []
                pending_album = []
                profiles.route(artist_path, lambda route: pending_profile.append(route))
                profiles.route(album_path, lambda route: pending_album.append(route))

                def check_album_skeleton(screenshot):
                    profiles_panel.get_by_role("status").filter(has_text="Loading album").wait_for()
                    profiles_panel.get_by_role("heading", name="Track list").wait_for()
                    rows = profiles_panel.locator(".music-profile-track[data-loading]")
                    assert rows.count() == 4
                    assert rows.first.get_attribute("data-ranked") is None
                    assert profiles_panel.evaluate("(el) => el.scrollWidth <= el.clientWidth + 1")
                    positions = rows.first.evaluate("""el => {
                      const heading = el.closest('section').querySelector('h3');
                      const row = el.getBoundingClientRect();
                      return [
                        el.querySelector('.music-profile-track-copy').getBoundingClientRect().left - row.left,
                        row.top - heading.getBoundingClientRect().top,
                        row.height,
                      ];
                    }""")
                    profiles.screenshot(path=str(output / screenshot))
                    return positions

                def check_album_alignment(positions):
                    profiles_panel.get_by_role("button", name="Play Asleep", exact=True).wait_for()
                    rows = profiles_panel.locator(".music-profile-track:not([data-loading])")
                    copy = rows.first.locator(".music-profile-track-copy").bounding_box()
                    row = rows.first.bounding_box()
                    heading = profiles_panel.get_by_role("heading", name="Track list").bounding_box()
                    assert abs(copy["x"] - row["x"] - positions[0]) < 2
                    assert abs(row["y"] - heading["y"] - positions[1]) < 2
                    assert abs(row["height"] - positions[2]) < 2
                row.get_by_role("button", name="Asleep", exact=True).click()
                album_byline = profiles.get_by_role("region", name="Recording details").get_by_role(
                    "button", name="Open album Louder Than Bombs", exact=True)
                album_byline.hover()
                assert album_byline.evaluate("(el) => getComputedStyle(el).cursor === 'pointer'")
                album_byline.click()
                loading_rows = check_album_skeleton("music-profile-album-loading-desktop.png")
                pending_album.pop().continue_()
                profiles.unroute(album_path)
                profiles_panel.get_by_role("heading", name="Louder Than Bombs").wait_for()
                check_album_alignment(loading_rows)
                tracklist_tab = profiles.get_by_role("tab", name="Track list", exact=True)
                assert tracklist_tab.get_attribute("aria-selected") == "true"
                assert tracklist_tab.evaluate("(el) => document.activeElement === el")
                profiles.get_by_role("tab", name="Details", exact=True).click()
                byline = profiles.locator(".song-dialog-head").get_by_role("button", name="Open artist The Smiths")
                assert byline.evaluate("(el) => getComputedStyle(el).textDecorationLine.includes('underline')")
                byline.press("Enter")
                profiles_panel.get_by_role("status").filter(has_text="Loading artist").wait_for()
                artist_tab = profiles.get_by_role("tab", name="Artist", exact=True)
                assert artist_tab.evaluate("(el) => document.activeElement === el")
                assert profiles_panel.locator(".artist-overview-hero").is_visible()
                assert profiles_panel.locator(".music-profile-track[data-loading][data-ranked]").count() == 4
                profiles.screenshot(path=str(output / "music-profile-artist-loading.png"))
                pending_profile.pop().continue_()
                profiles.unroute(artist_path)
                profiles_panel.get_by_role("heading", name="The Smiths").wait_for()
                profiles.get_by_role("tab", name="Track list", exact=True).click()
                byline.click()
                profiles_panel.get_by_role("heading", name="The Smiths").wait_for()
                popular = profiles_panel.get_by_role("region", name="Popular songs")
                popular.get_by_role("button", name="Find a copy New Song").wait_for()
                assert "New Song" in popular.get_by_role("listitem").first.inner_text()
                assert "200 listens" in popular.get_by_role("listitem").first.inner_text()
                popular.get_by_role("button", name="Play Asleep").wait_for()
                popular.get_by_role("button", name="Show library songs").click()
                library_songs = profiles_panel.get_by_role("region", name="Most played here")
                library_songs.get_by_text("2 plays", exact=False).wait_for()
                library_songs.get_by_role("button", name="Play Asleep").wait_for()
                library_songs.get_by_role("button", name="Show popular songs").click()
                profiles_panel.get_by_role("region", name="Popular albums").wait_for()
                profiles_panel.get_by_role("region", name="Other popular releases").wait_for()
                discography = profiles_panel.get_by_role("region", name="Discography")
                discography.get_by_role("button", name="Singles & EPs").click()
                assert discography.get_by_role("button", name=re.compile(r"^Another Single")).is_visible()
                assert not discography.get_by_role("button", name=re.compile(r"^Louder Than Bombs")).count()
                discography.get_by_role("button", name="Albums", exact=True).click()
                profiles.screenshot(path=str(output / "music-profile-artist.png"))
                profiles.set_viewport_size({"width": 390, "height": 844})
                assert profiles_panel.evaluate("(el) => el.scrollWidth <= el.clientWidth + 1"), "Artist overview overflows"
                profiles.screenshot(path=str(output / "music-profile-artist-mobile.png"))
                album_release = discography.get_by_role("button", name=re.compile(r"^Louder Than Bombs"))
                release_path = f"**/api/music-profile/release-group/{PROFILE_ALBUM}"
                profiles.route(release_path, lambda route: pending_album.append(route))
                profiles.emulate_media(reduced_motion="reduce")
                album_release.click()
                loading_rows = check_album_skeleton("music-profile-album-loading-phone.png")
                line = profiles_panel.locator(".music-profile-track[data-loading] .ui-skeleton-line").first
                assert line.evaluate("(el) => getComputedStyle(el).animationName") == "none"
                pending_album.pop().continue_()
                profiles.unroute(release_path)
                check_album_alignment(loading_rows)
                profiles.emulate_media(reduced_motion="no-preference")
                profiles_panel.get_by_role("button", name="Back", exact=True).click()
                album_release.click()
                profiles.get_by_role("heading", name="Track list").wait_for()
                assert profiles.get_by_role("dialog").evaluate("(el) => el.scrollTop < 2"), "Cached album retained artist scroll"
                profiles.set_viewport_size({"width": 1440, "height": 900})
                with profiles.expect_request(lambda request: request.url.endswith("/api/session")
                                             and request.method == "POST") as playing:
                    profiles_panel.get_by_role("button", name="Play Asleep", exact=True).click()
                assert playing.value.post_data_json["op"] == "play_album"
                assert playing.value.post_data_json["track_ids"] == [index]
                profiles.get_by_role("button", name="Close song information").click()
                player_album = profiles.locator(".player").get_by_role(
                    "button", name="Open album Louder Than Bombs", exact=True)
                idle_color = player_album.evaluate("(el) => getComputedStyle(el).color")
                player_album.hover()
                assert player_album.evaluate("(el) => getComputedStyle(el).cursor === 'pointer'")
                assert player_album.evaluate("(el) => getComputedStyle(el).color") != idle_color
                profiles.get_by_role("button", name="Open now playing").click()
                player_window = profiles.get_by_role("dialog", name="Now playing")
                window_album = player_window.get_by_role("button", name="Open album Louder Than Bombs", exact=True)
                window_album.hover()
                assert window_album.evaluate("(el) => getComputedStyle(el).color") == "rgb(255, 255, 255)"
                window_album.click()
                player_window.wait_for(state="hidden")
                profiles_panel.get_by_role("heading", name="Track list").wait_for()
                profiles.set_viewport_size({"width": 390, "height": 844})
                assert profiles_panel.evaluate("(el) => el.scrollWidth <= el.clientWidth + 1"), "Album track list overflows"
                profiles.screenshot(path=str(output / "music-profile-album.png"))
                pending_request = []

                def pause_copy(route):
                    if route.request.method == "POST":
                        pending_request.append(route)
                    else:
                        route.continue_()

                profiles.route("**/api/requests", pause_copy)
                find_copy = profiles_panel.get_by_role("button", name="Find a copy New Song", exact=True)
                idle_width = find_copy.bounding_box()["width"]
                with profiles.expect_request(lambda request: request.url.endswith("/api/requests")
                                             and request.method == "POST") as queued:
                    find_copy.focus()
                    find_copy.press("Enter")
                find_copy.locator(".room-spinner").wait_for()
                assert find_copy.get_attribute("aria-busy") == "true"
                assert find_copy.evaluate("(el) => el === document.activeElement")
                assert abs(find_copy.bounding_box()["width"] - idle_width) < 1
                assert queued.value.post_data_json["musicbrainz_id"] == PROFILE_MISSING
                pending_request.pop().continue_()
                profiles.get_by_role("button", name="Confirm and search", exact=True).wait_for()
                item.ok = True
                item.detail = "Aligned album loading rows on desktop and phone, artist loading state, reduced motion, direct album navigation, playback, and copy requests"
            except Exception as error:
                item.detail = repr(error)
            finally:
                profiles.close()
        except Exception as error:
            item = surface("browser-run")
            item.detail = f"the browser phase stopped early: {error}"
        finally:
            browser.close()
    return surfaces, errors, warnings


# ---------------------------------------------------------------------

def main():
    if not shutil.which("ffmpeg"):
        return fail_tooling("ffmpeg (the fixture's audio is generated "
                            "with it and the app verifies with ffprobe)")
    dist_index = REPO_ROOT / "frontend" / "dist" / "index.html"
    if not dist_index.is_file():
        print("verify-ui: frontend/dist is missing; run `make build` "
              "first (the class gate runs as part of it).")
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for stale in OUTPUT_DIR.glob("*.png"):
        stale.unlink()

    workdir = Path(tempfile.mkdtemp(prefix="listening-verify-ui-"))
    keep = bool(os.environ.get("VERIFY_UI_KEEP"))
    server = None
    report = {"surfaces": [], "errors": [], "warnings": [], "notes": []}
    try:
        app, music, watched = build_fixture(workdir)
        port = free_port()
        write_config(app, music, watched, port)
        server = Server(workdir, app, port)

        server.start()
        wait_for("the fixture server to answer",
                 lambda: server.request("GET", "/")[0] in (200, 401))

        def catalog_ready():
            status, overview = server.request("GET", "/api/overview")
            if status == 200 and overview.get("total", 0) >= 7:
                if any(p.get("source") == "watched" for p in overview.get("playlists", [])):
                    return overview
            return None
        overview = wait_for("the catalog import (7 records and a watched playlist)",
                            catalog_ready, timeout=180)
        report["fixture"] = {
            "records": overview.get("total"),
            "playlists": [(p.get("name"), p.get("source"))
                          for p in overview.get("playlists", [])],
        }
        server.stop()

        seed_state(app)
        server.start()
        wait_for("the fixture server to answer after seeding",
                 lambda: server.request("GET", "/")[0] in (200, 401))

        def seeded():
            status, overview = server.request("GET", "/api/overview")
            summary = overview.get("summary", {}) if status == 200 else {}
            return overview if summary.get("verified") == 4 else None
        overview = wait_for("the seeded coverage (4 verified)",
                            seeded, timeout=60)
        report["fixture"]["verified"] = overview["summary"]["verified"]

        status, session = server.request(
            "POST", "/api/session",
            {"op": "play", "track_id": 1, "playlist": "all"})
        if status != 200 or session.get("current") is None \
                or len(session.get("upcoming", [])) < 2:
            print(f"verify-ui: could not seed a live session "
                  f"(status {status}): {session}")
            return 1
        report["fixture"]["session_upcoming"] = len(session["upcoming"])

        outcome = run_browser(server, OUTPUT_DIR)
        if outcome[0] is None:
            return fail_tooling(
                "Playwright is not importable by "
                f"{sys.executable}; run this gate with an interpreter "
                "that has it, or install it for this one")
        surfaces, errors, warnings = outcome
        report["surfaces"] = [
            {"name": item.name, "ok": item.ok, "detail": item.detail}
            for item in surfaces]
        report["errors"] = errors
        report["warnings"] = warnings
    finally:
        if server is not None:
            server.stop()
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"verify-ui: fixture kept at {workdir}")

    (OUTPUT_DIR / "report.json").write_text(json.dumps(report, indent=2))
    lines = ["verify-ui report", ""]
    for item in report["surfaces"]:
        mark = "PASS" if item["ok"] else "FAIL"
        lines.append(f"[{mark}] {item['name']}: {item['detail']}")
    lines.append("")
    lines.append(f"errors: {len(report['errors'])}")
    lines.extend(f"  {error}" for error in report["errors"])
    lines.append(f"warnings: {len(report['warnings'])}")
    lines.extend(f"  {warning}" for warning in report["warnings"])
    lines.extend(f"note: {note}" for note in report["notes"])
    (OUTPUT_DIR / "report.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))

    failed = [item for item in report["surfaces"] if not item["ok"]]
    if failed or report["errors"]:
        print(f"verify-ui: FAILED ({len(failed)} surfaces, "
              f"{len(report['errors'])} errors)")
        return 1
    print(f"verify-ui: OK ({len(report['surfaces'])} surfaces, "
          "0 errors)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
