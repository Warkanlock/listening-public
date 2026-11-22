"""Exercise the same upload and source endpoints used by the browser."""
import base64
from contextlib import closing
import io
import json
import shutil
import sqlite3
import unittest
from unittest import mock
import zipfile

from fixture_environment import isolate_room_environment as setUpModule
from test_http_server import ServerFixture
import server
from catalog.collection_snapshot import create_empty_collection, create_snapshot
from library.library_protocol import write_xspf


class PortableIntakeTests(ServerFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Select a separate portable room inside the disposable host fixture.
        cls.room = cls.workspace.parent / "portable"
        create_empty_collection(cls.room)
        cls.portable_config = cls.config_path.with_name("portable.json")
        cls.portable_config.write_text(json.dumps({"folder": str(cls.room)}))
        server.init(cls.portable_config)
        cls.database = cls.room / "collection.sqlite3"

    def docs(self, table="collection_lists"):
        with closing(sqlite3.connect(self.database)) as db:
            return dict(db.execute(f"SELECT name,content FROM {table}"))

    def send(self, route, payload):
        status, _, body = self.request(self.loop_port, "POST", route, body=payload)
        return status, json.loads(body)

    @staticmethod
    def song(name):
        return write_xspf(name, [{"title": name, "artist": "Fixture", "album": ""}])

    def upload(self, name, content, **options):
        return self.send("/api/import-list", {"name": name, "content": content, **options})

    def bundle(self, docs, **options):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name, content in docs:
                archive.writestr("lists/" + name, content)
        return self.send("/api/import-bundle", {"content": base64.b64encode(stream.getvalue()).decode(), **options})

    def test_portable_room_uses_sqlite_with_stale_file_landing_settings(self):
        saved = json.loads(self.portable_config.read_text())
        self.portable_config.write_text(json.dumps({**saved, "audio_store": "files"}))
        server.init(self.portable_config)
        self.assertEqual(server.SETTINGS["audio_store"], "database")
        status, settings = self.send("/api/settings", {"audio_store": "files"})
        self.assertEqual(status, 200)
        self.assertEqual(settings["audio_store"], "database")
        self.assertEqual(json.loads(self.portable_config.read_text())["audio_store"], "database")
        self.assertEqual(settings["collection"]["path"], str(self.database.resolve()))
        self.assertGreater(settings["collection"]["bytes"], 0)
        self.assertEqual(settings["collection"]["list_workspace"], str(server.source_dir()))
        self.assertIsNone(server.redact_settings(settings)["collection"])

    def test_deferred_uploads_survive_cache_loss_and_restart_with_source_bytes_and_order(self):
        csv = "\ufeffTitle,Artist\r\nTwo,Room\r\nOne,Room\r\nTwo,Room\r\n"
        m3u = "#EXTM3U\r\n#EXTINF:60,Room - Radio\r\nradio.flac\r\n"
        xspf = "\ufeff" + self.song("Packed")
        with mock.patch.object(server, "worker_running", side_effect=lambda kind: kind == "downloads"):
            self.assertEqual(self.upload("ordered.csv", csv)[0], 200)
            self.assertEqual(self.upload("radio.m3u8", m3u)[0], 200)
            self.assertEqual(self.bundle([("packed.xspf", xspf)])[0], 200)
        docs = self.docs()
        for name, content in (("ordered.csv", csv), ("radio.m3u8", m3u), ("packed.xspf", xspf)):
            self.assertEqual(docs[name], content.encode())
        self.assertNotIn("ordered", server.catalog.playlists)
        shutil.rmtree(server.source_dir())
        server.init(self.portable_config)
        self.assertEqual(self.send("/api/sync", {})[0], 200)
        titles = [server.catalog.record_at(i)["title"] for i in server.catalog.playlists["ordered"]]
        self.assertEqual(titles, ["Two", "One", "Two"])
        self.assertIn("radio", server.catalog.playlists)
        self.assertIn("packed", server.catalog.playlists)

    def test_different_bundle_members_with_equivalent_names_conflict_atomically(self):
        first, second = self.song("First upload"), self.song("Second upload")
        pair = [("Collision.xspf", first), ("collision.xspf", second)]
        with mock.patch.object(server, "worker_running", return_value=True):
            before = self.docs()
            self.assertEqual(self.bundle(pair)[0], 409)
            self.assertEqual(self.docs(), before)
            status, result = self.bundle(pair, on_conflict="rename")
            self.assertEqual(status, 200)
            self.assertEqual(result["names"], ["Collision.xspf", "Collision (2).xspf"])
            self.assertEqual(self.docs()["Collision.xspf"], first.encode())
            self.assertEqual(self.docs()["Collision (2).xspf"], second.encode())

    def test_bundle_conflicts_rollback_and_replacement_history_in_snapshot(self):
        first = "\ufeff" + self.song("First").replace("\n", "\r\n")
        later = self.song("Later")
        pair = [("keep.xspf", later), ("extra.xspf", later)]
        with mock.patch.object(server, "worker_running", return_value=True):
            self.assertEqual(self.upload("keep.xspf", first)[0], 200)
            self.assertEqual(self.bundle(pair)[0], 409)
            self.assertNotIn("extra.xspf", self.docs())
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute("CREATE TRIGGER fail_import BEFORE INSERT ON collection_lists WHEN NEW.name='extra.xspf' "
                           "BEGIN SELECT RAISE(ABORT,'fixture insert failure'); END")
            try:
                self.assertEqual(self.bundle(pair, on_conflict="replace")[0], 400)
                self.assertEqual(self.docs()["keep.xspf"], first.encode())
                self.assertFalse((server.source_dir() / "extra.xspf").exists())
            finally:
                with closing(sqlite3.connect(self.database)) as db, db:
                    db.execute("DROP TRIGGER fail_import")
            self.assertEqual(self.bundle(pair, on_conflict="replace")[0], 200)
            self.assertEqual(self.upload("keep.xspf", first, on_conflict="rename")[1]["name"], "keep (2).xspf")
        snapshot = create_snapshot(self.portable_config, self.room.parent / "import-copy.sqlite3")
        with closing(sqlite3.connect(snapshot)) as db:
            self.assertEqual(db.execute("SELECT content FROM collection_list_previous WHERE name='keep.xspf'").fetchone(),
                             (first.encode(),))
            self.assertEqual(db.execute("SELECT content FROM collection_lists WHERE name='keep.xspf'").fetchone(),
                             (later.encode(),))

    def test_link_and_spotify_callback_publish_while_downloads_run(self):
        source = self.room.parent / "linked.xspf"
        source.write_text(self.song("Linked"))
        with mock.patch.object(server, "worker_running", side_effect=lambda kind: kind == "downloads"):
            status, _ = self.send("/api/sources", {"op": "link", "path": str(source)})
            self.assertEqual(status, 200)
            mirror = next(name for name in self.docs() if name.startswith("source_linked_"))
            self.assertIn(b"Linked", self.docs()[mirror])
            (server.source_dir() / "spotify_liked.csv").write_bytes(b"title,artist\nSaved,Room\n")
            server.spotify_library_changed()
            self.assertEqual(self.docs()["spotify_liked.csv"], b"title,artist\nSaved,Room\n")
