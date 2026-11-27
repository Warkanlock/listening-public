import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from contextlib import closing, contextmanager
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fixture_environment import isolate_room_environment as setUpModule
from catalog import legacy_spoken
from catalog.audio_store import MediaStore
from catalog.catalog_store import CatalogStore
from catalog.collection_snapshot import collection_access, create_empty_collection, create_snapshot, restore_snapshot, verify_collection
from catalog.favorites import FavoritesStore
from catalog.genres import GenreStore
from downloads.requests_store import RequestsStore
from downloads.verification import probe
from playback.play_events import PlayEventsStore


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs FFmpeg fixture tools")
class CollectionRestoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source"
        self.original = self.base / "original"
        self.original.mkdir()
        self.source_app = self.base / "source-app"
        self.config = self.settings(self.source_app, self.source)
        (self.source / "lists").mkdir(parents=True)
        (self.source / "audio").mkdir()
        (self.source / "lists" / "mix.csv").write_text(
            "title,artist,album\nFixture A,Fixture,Both\nFixture A,Fixture,Both\n", encoding="utf-8")
        self.catalog_file = self.audio("Fixture A")
        self.custom_file = self.audio("Fixture B")
        (self.source / "audio" / "1 - Fixture A.mp3").symlink_to(self.catalog_file)
        for action in ("prepare", "report"):
            self.run_worker(action, self.source, self.config)
        state = self.source_app / "requests.sqlite3"
        record = CatalogStore(state).read_build()["records"][0]
        self.key = record["key"]
        GenreStore(state).set(self.key, "Jazz", "manual")
        FavoritesStore(state).set(self.key, True)
        PlayEventsStore(state).record("started", "1", track_index=1, record_key=self.key)
        requests = RequestsStore(state)
        self.request = requests.enqueue(None, "Fixture", "Fixture B", "Both", 1, "Fixture B")
        requests.finish(self.request, "downloaded", "", str(self.custom_file))
        self.backup = self.base / "Room snapshot.sqlite3"

    def audio(self, title):
        path = self.original / (title + ".mp3")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=530:duration=1", "-b:a", "320k",
                        "-metadata", "title=" + title, "-metadata", "artist=Fixture",
                        "-metadata", "album=Both", "-metadata", "genre=Classical", str(path)],
                       check=True, timeout=20)
        return path

    def settings(self, app, room):
        app.mkdir()
        config = app / "listening-room.json"
        config.write_text(json.dumps({"folder": str(room), "auto_download": False,
                                      "playlist_names": {"mix": "My fixture"}}))
        return config

    def environment(self, room, config):
        return {**os.environ, "HOME": str(self.base), "XDG_CONFIG_HOME": str(self.base / ".config"),
                "OFFLINE_MUSIC_ROOT": str(room), "OFFLINE_MUSIC_STATE": str(config.parent),
                "OFFLINE_MUSIC_LIBRARY": str(room / "audio"), "OFFLINE_MUSIC_SOURCES": str(room / "lists"),
                "OFFLINE_MUSIC_PLAYLISTS": str(room / "playlists"), "OFFLINE_MUSIC_OUTPUTS": str(room / "reports"),
                "LISTENING_ROOM_CONFIG": str(config)}

    def run_worker(self, action, room, config):
        subprocess.run([sys.executable, str(ROOT / "backend/runner.py"), action],
                       env=self.environment(room, config), check=True, capture_output=True, timeout=35)

    @contextmanager
    def served(self, room, config):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([sys.executable, str(ROOT / "backend/server.py"), "--host",
                                    "127.0.0.1", "--port", str(port)], env=self.environment(room, config),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            base = f"http://127.0.0.1:{port}"
            for attempt in range(100):
                try:
                    with urllib.request.urlopen(base + "/api/overview", timeout=1):
                        break
                except OSError:
                    if process.poll() is not None or attempt == 99:
                        raise
                    time.sleep(.05)
            yield base
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_snapshot_keeps_retired_spoken_catalog_data(self):
        state = self.source_app / "requests.sqlite3"
        with closing(sqlite3.connect(state)) as db, db:
            db.executescript(legacy_spoken.SCHEMA)
            db.execute("INSERT INTO podcast_feeds VALUES ('feed','https://example.org','https://example.org',"
                       "'Fixture feed','','','',0,1)")
            db.execute("INSERT INTO podcast_episodes VALUES ('episode','feed','Fixture episode','',"
                       "'https://example.org/sample.mp3',30,0,0)")
        create_snapshot(self.config, self.backup)
        with closing(sqlite3.connect(self.backup)) as db:
            self.assertEqual(db.execute("SELECT title FROM podcast_episodes").fetchone(), ("Fixture episode",))

    def test_early_recording_profiles_survive_and_an_unliked_track_stays_unliked(self):
        with closing(sqlite3.connect(self.source_app / "requests.sqlite3")) as db, db:
            db.execute("DROP TABLE favorites")
            db.execute("CREATE TABLE track_profile(track_key PRIMARY KEY,like_level,play_count,last_played_at,styles,like_changed_at)")
            db.execute("INSERT INTO track_profile VALUES (?,1,8,12.0,'[]',4500000000)", (self.key,))
        create_snapshot(self.config, self.backup)
        with closing(sqlite3.connect(self.backup)) as db:
            self.assertEqual(db.execute("SELECT key,stamp FROM favorites").fetchall(), [(self.key, 4.5)])
        restored = self.base / "restored"
        restore_snapshot(self.backup, restored)
        settings = self.settings(self.base / "second-host", restored)
        FavoritesStore(restored / "collection.sqlite3").set(self.key, False)
        again = create_snapshot(settings, self.base / "copy.sqlite3")
        with closing(sqlite3.connect(again)) as db:
            self.assertEqual(db.execute("SELECT key FROM favorites").fetchall(), [])
            self.assertEqual(db.execute("SELECT track_key,play_count FROM track_profile").fetchall(), [(self.key, 8)])

    def make_unowned_legacy_blob(self, status):
        payload = self.catalog_file.read_bytes()
        key = hashlib.sha256(payload).hexdigest()
        db_path = self.source / "media.sqlite3"
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("DROP TABLE IF EXISTS audio_owners")
            db.execute("DROP TABLE IF EXISTS audio_deflate")
            db.execute("INSERT INTO audio VALUES (?,?,?,?,?,?)",
                       (key, payload, len(payload), ".mp3", json.dumps(probe(self.catalog_file)), 1))
        with closing(sqlite3.connect(self.source_app / "requests.sqlite3")) as db, db:
            db.execute("UPDATE coverage SET path=?, status=? WHERE idx=1", ("blob:" + key, status))
        (self.source / "audio" / "1 - Fixture A.mp3").unlink()
        return payload, key

    def test_legacy_blob_plays_after_retirement_two_refreshes_and_reimport(self):
        original, key = self.make_unowned_legacy_blob("verified")
        lists = self.source / "lists"
        csv = (lists / "mix.csv").read_bytes()
        (lists / "mix.csv").unlink()
        (lists / "mix.xspf").unlink()
        for action in ("prepare", "report", "prepare", "report"):
            self.run_worker(action, self.source, self.config)
        store = MediaStore(self.source / "media.sqlite3")
        self.assertEqual(store.owned_audio("catalog", self.key), "blob:" + key)
        (lists / "mix.csv").write_bytes(csv)
        for action in ("prepare", "report"):
            self.run_worker(action, self.source, self.config)
        with self.served(self.source, self.config) as base:
            with urllib.request.urlopen(base + "/api/audio/1", timeout=3) as stream:
                self.assertEqual(stream.read(), original)

    def test_snapshot_and_first_report_adopt_an_already_retired_legacy_reference(self):
        _, key = self.make_unowned_legacy_blob("retired")
        create_snapshot(self.config, self.backup)
        self.assertEqual(MediaStore(self.backup).owned_audio("catalog", self.key), "blob:" + key)
        self.run_worker("report", self.source, self.config)
        self.assertEqual(MediaStore(self.source / "media.sqlite3").owned_audio("catalog", self.key), "blob:" + key)

    def test_adoption_compresses_a_legacy_raw_blob_without_modifying_it(self):
        payload = b"legacy raw sample " * 10000
        key = hashlib.sha256(payload).hexdigest()
        old = self.source / "media.sqlite3"
        MediaStore(old).count()
        with closing(sqlite3.connect(old)) as db, db:
            db.execute("INSERT INTO audio VALUES (?,?,?,?,?,?)", (key, payload, len(payload), ".mp3", "{}", 1))
        create_snapshot(self.config, self.backup)
        with closing(sqlite3.connect(self.backup)) as db:
            self.assertLess(db.execute("SELECT length(data) FROM audio WHERE hash=?", (key,)).fetchone()[0],
                            len(payload))
        with closing(sqlite3.connect(old)) as db:
            self.assertEqual(db.execute("SELECT data FROM audio WHERE hash=?", (key,)).fetchone()[0], payload)

    def test_copy_restore_play_export_and_source_retention(self):
        original = [self.catalog_file.read_bytes(), self.custom_file.read_bytes()]
        # A committed genre correction still in WAL must travel with the backup.
        with closing(sqlite3.connect(self.source_app / "requests.sqlite3")) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE genres SET genre='Soul' WHERE key=?", (self.key,))
            writer.commit()
            create_snapshot(self.config, self.backup)
        self.assertTrue((self.source / "audio/1 - Fixture A.mp3").is_symlink())
        self.assertEqual([self.catalog_file.read_bytes(), self.custom_file.read_bytes()], original)
        room = restore_snapshot(self.backup, self.base / "fresh-room")
        other = self.settings(self.base / "fresh-app", room)
        # Sources are unreachable from the restore, but remain safe on disk.
        self.source.rename(self.base / "retained-source")
        self.original.rename(self.base / "retained-originals")
        self.source_app.rename(self.base / "retained-app")
        for action in ("prepare", "report", "export"):
            self.run_worker(action, room, other)
        collection = room / "collection.sqlite3"
        self.assertEqual(CatalogStore(collection).read_build()["playlists"]["mix"], [1, 1])
        self.assertEqual(GenreStore(collection).get(self.key)["genre"], "Soul")
        self.assertIn(self.key, FavoritesStore(collection).keys())
        self.assertEqual(len(PlayEventsStore(collection).recent_started(10)), 1)
        self.assertEqual(sorted(p.read_bytes() for p in (room / "exported").rglob("*.mp3")), sorted(original))
        self.assertFalse((other.parent / "requests.sqlite3").exists())
        with self.served(room, other) as base:
            alternate = self.settings(self.base / "alternate-app", room)
            with self.assertRaisesRegex(RuntimeError, "busy"):
                create_snapshot(alternate, self.base / "while-serving.sqlite3")
            for route, data in zip(("1", f"custom/{self.request}"), original):
                with urllib.request.urlopen(f"{base}/api/audio/{route}", timeout=3) as stream:
                    self.assertEqual(stream.read(), data)
        # Losing a derived row never retires owned audio.
        CatalogStore(collection).write_coverage([])
        (room / "lists" / "mix.csv").unlink()
        (room / "lists" / "mix.xspf").unlink()
        for action in ("prepare", "report"):
            self.run_worker(action, room, other)
        self.assertEqual(set(MediaStore(collection).keys()), {hashlib.sha256(x).hexdigest() for x in original})

    def test_interrupted_copy_resumes_after_the_last_committed_file(self):
        from catalog import collection_snapshot
        probe = collection_snapshot.probe
        calls = []

        def interrupt(source):
            calls.append(source)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return probe(source)

        with mock.patch.object(collection_snapshot, "probe", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                create_snapshot(self.config, self.backup)
        self.assertFalse(self.backup.exists())
        with mock.patch.object(collection_snapshot, "probe", wraps=probe) as resumed:
            create_snapshot(self.config, self.backup)
        self.assertEqual(resumed.call_count, 1)
        self.assertTrue(self.catalog_file.is_file())
        self.assertTrue(self.custom_file.is_file())

    def test_open_allows_file_landing_while_snapshot_requires_all_originals(self):
        room = self.base / "files-room"
        create_empty_collection(room)
        app = self.settings(self.base / "files-app", room)
        (room / "audio").mkdir()
        external = room / "audio" / "file.mp3"
        shutil.copyfile(self.catalog_file, external)
        requests = RequestsStore(room / "collection.sqlite3")
        rid = requests.enqueue(None, "Fixture", "Fixture A", "Both", 1, "Fixture A")
        requests.finish(rid, "downloaded", "", "file.mp3")
        verify_collection(room)
        create_snapshot(app, self.backup)
        with closing(sqlite3.connect(self.backup)) as db:
            self.assertTrue(db.execute("SELECT audio_path FROM requests").fetchone()[0].startswith("blob:"))
        external.unlink()
        with self.assertRaisesRegex(ValueError, "Missing request"):
            create_snapshot(app, self.base / "missing.sqlite3")

    def test_resnapshot_of_database_only_room_preserves_list_bytes_and_names(self):
        create_snapshot(self.config, self.backup)
        room = self.base / "portable"
        room.mkdir()
        shutil.copyfile(self.backup, room / "collection.sqlite3")
        app = self.settings(self.base / "other-app", room)
        app.write_text(json.dumps({"folder": str(room)}))
        with collection_access(room):
            with self.assertRaisesRegex(RuntimeError, "busy"):
                create_snapshot(app, self.base / "blocked.sqlite3")
        second = create_snapshot(app, self.base / "second.sqlite3")
        restored = restore_snapshot(second, self.base / "restored")
        self.assertEqual((restored / "lists/mix.csv").read_bytes(), (self.source / "lists/mix.csv").read_bytes())
        with closing(sqlite3.connect(restored / "collection.sqlite3")) as db:
            self.assertEqual(json.loads(db.execute("SELECT playlist_names FROM collection_info").fetchone()[0]),
                             {"mix": "My fixture"})

    def test_restore_rejects_a_mistyped_playable_reference_even_when_the_audio_exists(self):
        create_snapshot(self.config, self.backup)
        with closing(sqlite3.connect(self.backup)) as db, db:
            db.execute("UPDATE requests SET audio_path='file:' || substr(audio_path, 6) WHERE id=?",
                       (self.request,))
        restored = self.base / "broken-reference"
        with self.assertRaisesRegex(ValueError, "unresolved playable track"):
            restore_snapshot(self.backup, restored)
        self.assertFalse(restored.exists())

    def test_refuses_low_space_and_busy_room_without_creating_destination(self):
        with collection_access(self.config.parent):
            with self.assertRaisesRegex(RuntimeError, "busy"):
                create_snapshot(self.config, self.backup)
        with mock.patch("catalog.collection_snapshot.shutil.disk_usage",
                        return_value=shutil._ntuple_diskusage(100, 99, 1)):
            with self.assertRaisesRegex(ValueError, "space"):
                create_snapshot(self.config, self.backup)
        self.assertFalse(self.backup.exists())
