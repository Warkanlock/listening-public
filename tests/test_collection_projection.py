from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fixture_environment import isolate_room_environment as setUpModule
from catalog.catalog_store import CatalogStore
from catalog.collection_projection import ListProjection
from catalog.collection_snapshot import create_empty_collection, create_snapshot
from library.library_protocol import write_xspf


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.room = self.base / "room"
        self.config = self.base / "app" / "config.json"
        self.db = self.room / "collection.sqlite3"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("OFFLINE_MUSIC_", "LISTENING_ROOM_"))}
        self.env.update(HOME=str(self.base), OFFLINE_MUSIC_ROOT=str(self.room), LISTENING_ROOM_CONFIG=str(self.config))

    @contextmanager
    def server(self):
        with socket.socket() as port:
            port.bind(("127.0.0.1", 0))
            number = port.getsockname()[1]
        command = [str(ROOT / "listening"), "server", "start", "--foreground",
                   "--host", "127.0.0.1", "--port", str(number)]
        with open(self.base / "server.log", "ab") as log:
            proc = subprocess.Popen(command, env=self.env, stdout=log, stderr=log)
        try:
            for _ in range(100):
                try:
                    with urlopen(f"http://127.0.0.1:{number}/api/overview", timeout=.3):
                        break
                except OSError:
                    if proc.poll() is not None:
                        self.fail((self.base / "server.log").read_text())
                    time.sleep(.05)
            else:
                self.fail("The fixture server did not become ready")
            yield f"http://127.0.0.1:{number}"
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    @staticmethod
    def song(name):
        return write_xspf(name, [dict(title=name, artist="Fixture", album="Music")]).encode()

    def canonical(self):
        with closing(sqlite3.connect(self.db)) as db:
            return dict(db.execute("SELECT name,content FROM collection_lists"))

    def prepare(self):
        subprocess.run([sys.executable, str(ROOT / "backend/runner.py"), "prepare"], env=self.env,
                       capture_output=True, check=True, timeout=20)

    def test_bare_start_remote_lists_offline_changes_and_stale_snapshot(self):
        with self.server():
            settings = json.loads(self.config.read_text())
            cache = Path(settings["lists_folder"])
            self.assertTrue(cache.is_relative_to(self.config.parent))
        # SQLite may still have journals while an already-started refresh exits.
        entries = {p.name for p in self.room.iterdir()}
        self.assertIn("collection.sqlite3", entries)
        self.assertLessEqual(entries, {"collection.sqlite3", "collection.sqlite3-wal", "collection.sqlite3-shm"})
        (cache / "one.xspf").write_bytes(self.song("First"))
        self.prepare()
        self.assertEqual(set(self.canonical()), {"one.xspf"})

        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO collection_lists VALUES (?,?)", ("two.xspf", self.song("Second")))
            db.execute("UPDATE collection_info SET playlist_names=?", (json.dumps({"two": "New room title"}),))
        (cache / "one.xspf").write_bytes(self.song("Offline"))
        snapshot = create_snapshot(self.config, self.base / "before-reopen.sqlite3")
        with closing(sqlite3.connect(snapshot)) as db:
            self.assertEqual(dict(db.execute("SELECT name,content FROM collection_lists")),
                             {"one.xspf": self.song("First"), "two.xspf": self.song("Second")})
            self.assertEqual(json.loads(db.execute("SELECT playlist_names FROM collection_info").fetchone()[0]),
                             {"two": "New room title"})
        with self.server():
            self.assertEqual((cache / "two.xspf").read_bytes(), self.song("Second"))
            self.assertEqual(json.loads(self.config.read_text())["playlist_names"], {"two": "New room title"})
        self.prepare()
        self.assertEqual(self.canonical(), {"one.xspf": self.song("Offline"), "two.xspf": self.song("Second")})
        result = CatalogStore(self.db).read_build()
        self.assertEqual(set(result["playlists"]), {"one", "two"})
        self.assertEqual([result["records"][i-1]["title"] for i in result["playlists"]["one"]], ["Offline"])

        (cache / "one.xspf").write_bytes(self.song("Concurrent local edit"))
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE collection_lists SET content=? WHERE name='one.xspf'", (self.song("Authoritative"),))
        self.prepare()
        self.assertEqual(self.canonical()["one.xspf"], self.song("Authoritative"))
        preserved = list(self.config.parent.rglob("retained-lists/*/one.xspf"))
        self.assertEqual([p.read_bytes() for p in preserved], [self.song("Concurrent local edit")])

    def test_explicit_input_folder_keeps_originals_on_conflict_and_remote_removal(self):
        create_empty_collection(self.room)
        source = self.base / "owner-lists"
        source.mkdir()
        self.config.parent.mkdir()
        self.config.write_text(json.dumps({"folder": str(self.room), "lists_folder": str(source)}))
        owner_file = source / "mix.xspf"
        owner_file.write_bytes(self.song("Original"))
        self.prepare()
        self.assertEqual(self.canonical()["mix.xspf"], owner_file.read_bytes())
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE collection_lists SET content=? WHERE name='mix.xspf'", (self.song("Remote"),))
        owner_file.write_bytes(self.song("Different offline edit"))
        with self.server():
            self.assertEqual(json.loads(self.config.read_text())["lists_folder"], str(source))
        self.prepare()
        self.assertEqual(owner_file.read_bytes(), self.song("Different offline edit"))
        self.assertEqual(self.canonical()["mix.xspf"], self.song("Remote"))
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DELETE FROM collection_lists")
        self.prepare()
        self.assertEqual(self.canonical(), {})
        self.assertEqual(owner_file.read_bytes(), self.song("Different offline edit"))
        owner_file.write_bytes(self.song("Later explicit edit"))
        self.prepare()
        self.assertEqual(self.canonical()["mix.xspf"], owner_file.read_bytes())
        owner_file.unlink()
        self.prepare()
        self.assertEqual(self.canonical(), {})

    def test_invalid_cache_marker_cannot_name_files_outside_workspace(self):
        create_empty_collection(self.room)
        projection = ListProjection(self.db, self.base / "source", self.config.parent)
        projection.reconcile()
        victim = projection.directory.parent / "original.xspf"
        victim.write_bytes(b"owner content")
        projection.marker.write_text(json.dumps({"../original.xspf": "invalid"}))
        with self.assertRaisesRegex(ValueError, "playlist name"):
            projection.reconcile()
        self.assertEqual(victim.read_bytes(), b"owner content")

    def test_offline_config_rename_preserves_concurrent_remote_owner_name(self):
        with self.server():
            pass
        saved = json.loads(self.config.read_text())
        saved["playlist_names"] = {"mine": "My offline name", "both": "Old host name"}
        self.config.write_text(json.dumps(saved))
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE collection_info SET playlist_names=?", (json.dumps({"both": "New remote name"}),))
        with self.server():
            expected = {"mine": "My offline name", "both": "New remote name"}
            self.assertEqual(json.loads(self.config.read_text())["playlist_names"], expected)
            with closing(sqlite3.connect(self.db)) as db:
                self.assertEqual(json.loads(db.execute("SELECT playlist_names FROM collection_info").fetchone()[0]), expected)

    def test_detached_source_cannot_overwrite_remote_update_with_offline_edit(self):
        create_empty_collection(self.room)
        source = self.base / "removable"
        source.mkdir()
        self.config.parent.mkdir()
        self.config.write_text(json.dumps({"folder": str(self.room), "lists_folder": str(source)}))
        (source / "one.xspf").write_bytes(self.song("A"))
        self.prepare()
        detached = self.base / "disconnected"
        source.rename(detached)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE collection_lists SET content=?", (self.song("B"),))
        self.prepare()
        (detached / "one.xspf").write_bytes(self.song("C"))
        detached.rename(source)
        self.prepare()
        self.assertEqual(self.canonical()["one.xspf"], self.song("B"))
        self.assertEqual((source / "one.xspf").read_bytes(), self.song("C"))

    def test_source_removal_retires_only_its_untouched_generated_sibling(self):
        create_empty_collection(self.room)
        source = self.base / "inputs"
        source.mkdir()
        self.config.parent.mkdir()
        self.config.write_text(json.dumps({"folder": str(self.room), "lists_folder": str(source)}))
        raw = b"title,artist,album\nSong,Artist,Album\n"
        for name in ("one.csv", "two.csv"):
            (source / name).write_bytes(raw)
        self.prepare()
        lists = ListProjection(self.db, source, self.config.parent).directory
        (lists / "two.xspf").write_bytes(self.song("Independent edit"))
        for name in ("one.csv", "two.csv"):
            (source / name).unlink()
        self.prepare()
        self.assertEqual(self.canonical(), {"two.xspf": self.song("Independent edit")})
        self.assertEqual(list(source.iterdir()), [])

    def test_two_source_edits_before_commit_publish_the_latest_bytes(self):
        create_empty_collection(self.room)
        source = self.base / "explicit"
        source.mkdir()
        self.config.parent.mkdir()
        self.config.write_text(json.dumps({"folder": str(self.room), "lists_folder": str(source)}))
        file = source / "one.xspf"
        file.write_bytes(self.song("A"))
        self.prepare()
        file.write_bytes(self.song("B"))
        projection = ListProjection(self.db, source, self.config.parent)
        projection.reconcile()
        self.assertEqual(self.canonical()["one.xspf"], self.song("A"))
        self.assertEqual((projection.directory / file.name).read_bytes(), self.song("B"))
        file.write_bytes(self.song("C"))
        self.prepare()
        self.assertEqual(self.canonical()["one.xspf"], self.song("C"))
        self.assertEqual(file.read_bytes(), self.song("C"))

    def test_saving_host_settings_leaves_disconnected_source_directory_absent(self):
        create_empty_collection(self.room)
        source = self.base / "offline-input"
        self.config.parent.mkdir()
        self.config.write_text(json.dumps({"folder": str(self.room), "lists_folder": str(source)}))
        with self.server() as url:
            save = Request(url + "/api/settings", data=b'{"auto_download":false}',
                           headers={"Content-Type": "application/json", "Origin": url})
            with urlopen(save, timeout=10) as response:
                self.assertEqual(response.status, 200)
            self.assertFalse(source.exists())

    def test_single_cache_eviction_rehydrates_documents_and_an_explicit_deletion_sticks(self):
        create_empty_collection(self.room)
        source = self.base / "inputs"
        source.mkdir()
        self.config.parent.mkdir()
        self.config.write_text(json.dumps({"folder": str(self.room), "lists_folder": str(source)}))
        (source / "own.xspf").write_bytes(self.song("Own"))
        (source / "raw.csv").write_bytes(b"title,artist,album\nSong,Artist,Album\n")
        (source / "radio.m3u").write_bytes(b"#EXTM3U\n#EXTINF:20,Artist - Radio Song\nradio.mp3\n")
        self.prepare()
        projection = ListProjection(self.db, source, self.config.parent)
        (projection.directory / "imported.xspf").write_bytes(self.song("Imported"))
        self.prepare()
        all_docs = self.canonical()
        for name in ("own.xspf", "raw.csv", "raw.xspf", "radio.m3u", "radio.xspf", "imported.xspf"):
            (projection.directory / name).unlink()
            self.prepare()
            self.assertEqual(self.canonical(), all_docs)
            self.assertEqual((projection.directory / name).read_bytes(), all_docs[name])
        (source / "radio.m3u").unlink()
        projection.reconcile()
        projection.reconcile()
        self.prepare()
        remaining = set(self.canonical())
        self.assertEqual(remaining, set(all_docs) - {"radio.m3u", "radio.xspf"})

    def test_removing_stale_input_keeps_newer_canonical_version(self):
        create_empty_collection(self.room)
        source = self.base / "lists"
        source.mkdir()
        self.config.parent.mkdir()
        self.config.write_text(json.dumps({"folder": str(self.room), "lists_folder": str(source)}))
        raw = source / "one.xspf"
        raw.write_bytes(self.song("A"))
        self.prepare()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE collection_lists SET content=?", (self.song("B"),))
        self.prepare()
        raw.unlink()
        self.prepare()
        self.assertEqual(self.canonical(), {"one.xspf": self.song("B")})
