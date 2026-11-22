import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from fixture_environment import isolate_room_environment as setUpModule
from catalog.audio_store import MediaStore
from catalog.catalog import Catalog, CatalogPaths
from catalog.catalog_store import CatalogStore
from catalog.collection_snapshot import create_snapshot, restore_snapshot
from library.library_protocol import write_xspf
from library.playlist_links import review
from playback.play_events import PlayEventsStore
from test_http_server import ServerFixture

ID = "123456789abcdefghijklm"


class PlaylistLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.room = self.base / "room"
        self.lists = self.room / "lists"
        self.lists.mkdir(parents=True)
        self.app = self.base / "application"
        self.app.mkdir()
        self.mirror = "spotify_" + ID
        self.config = self.app / "test.json"
        self.config.write_text(json.dumps({"folder": str(self.room)}))
        (self.app / "spotify-sync.json").write_text(json.dumps({"playlists": {ID: {"name": "Mirror"}}}))
        self.db = self.app / "requests.sqlite3"

    def write(self, key, names, *, mirror=False, playlist_id="", title=""):
        rows = [dict(title=n, artist="Fixture", album="Album", duration_ms=120000,
                     identifier="spotify:track:" + n if mirror else "") for n in names]
        (self.lists / (key + ".xspf")).write_text(write_xspf(title or key, rows, playlist_id))

    def run_worker(self, operation, **overrides):
        env = {**os.environ, "HOME": str(self.base), "LISTENING_ROOM_CONFIG": str(self.config),
               "OFFLINE_MUSIC_ROOT": str(self.room), "OFFLINE_MUSIC_STATE": str(self.app),
               "OFFLINE_MUSIC_SOURCES": str(self.lists)}
        subprocess.run([sys.executable, str(ROOT / "backend/runner.py"), operation],
                       env={**env, **overrides}, check=True, capture_output=True)

    def prepare(self):
        self.run_worker("prepare")
        self.build = CatalogStore(self.db).read_build()
        return review(self.db, self.build["records"])

    def action(self, op, **fields):
        review(self.db, self.build["records"], dict(op=op, **fields))
        return self.prepare()

    def test_confirmed_union_tracks_sync_and_survives_single_database_restore(self):
        self.write("import", ["One", "One", "Two", "Three", "Four"])
        self.write(self.mirror, ["Four", "Two", "Three", "One", "Five"], mirror=True)
        source = (self.lists / "import.xspf").read_bytes()
        view = self.prepare()
        (candidate,) = view["candidates"]
        self.assertEqual(candidate["matched"], 4)
        self.assertEqual(candidate["source_only"], ["Fixture — Five"])
        view = self.action("confirm", base="import", source=self.mirror, name="Evening music")
        group = view["links"][0]["id"]
        catalog = self.build
        titles = lambda: [self.build["records"][idx-1]["title"] for idx in self.build["playlists"][group]]
        self.assertEqual(titles(), ["One", "One", "Two", "Three", "Four", "Five"])
        self.assertEqual(list(catalog["playlists"]), [group])
        self.assertEqual(catalog["list_names"][group], "Evening music")
        self.assertEqual((self.lists / "import.xspf").read_bytes(), source)
        self.write(self.mirror, ["Six", "Three", "Four"], mirror=True, title="Renamed upstream")
        self.prepare()
        self.assertEqual(titles(), ["One", "One", "Two", "Three", "Four", "Six"])
        backup = create_snapshot(self.config, self.base / "portable.sqlite3")
        self.room = restore_snapshot(backup, self.base / "restored")
        self.lists = self.room / "lists"
        self.db = self.room / "collection.sqlite3"
        self.app = self.base / "fresh-application"
        self.app.mkdir()
        self.config = self.app / "test.json"
        self.config.write_text(json.dumps({"folder": str(self.room)}))
        self.prepare()
        self.assertEqual(titles(), ["One", "One", "Two", "Three", "Four", "Six"])
        self.action("unlink", id=group)
        self.assertEqual(set(self.build["playlists"]), {"import", self.mirror})

    def test_overlap_is_reviewable_regardless_of_adapter_or_distinct_playlist_ids(self):
        titles = ["One", "Two", "Three"]
        self.write("first-import", titles)
        self.write("watched-import", titles)
        candidate, = self.prepare()["candidates"]
        self.assertEqual({candidate["base"], candidate["source"]}, {"first-import", "watched-import"})
        self.write("first-import", titles, playlist_id=f"spotify:playlist:{ID}")
        self.write("watched-import", titles, playlist_id="spotify:playlist:0123456789abcdefghijkl")
        view = self.prepare()
        self.assertFalse(view["links"])
        self.assertEqual(len(view["candidates"]), 1)

    def test_equivalent_source_keys_keep_verified_audio_preferences_and_play_order(self):
        self.write("import", ["One", "One", "Two", "Three"])
        self.write(self.mirror, ["Three", "Two", "One"], mirror=True)
        self.prepare()
        catalog = Catalog(CatalogPaths(self.db, self.lists))
        imported = catalog.playlists["import"]
        mirrored = catalog.playlists[self.mirror]
        self.assertFalse(set(imported) & set(mirrored))
        donor = catalog.record_at(mirrored[-1])
        base = catalog.record_at(imported[0])
        catalog.favorite_store().set(donor["key"], True)
        catalog.genre_store().set(base["key"], "Pop", "tag")
        catalog.genre_store().set(donor["key"], "Jazz", "manual")
        events = PlayEventsStore(self.db)
        for key in (base["key"], donor["key"]):
            events.record("started", 1, record_key=key)
            events.record("completed", 1, record_key=key)
        media = MediaStore(self.room / "media.sqlite3")
        for idx in mirrored:
            record = catalog.record_at(idx)
            path = self.base / (record["title"] + ".mp3")
            path.write_bytes(record["title"].encode())
            media.put_file(path, {"bitrate": 320000, "duration": 120, "title": record["title"]},
                           owners=(("catalog", record["key"]),))

        self.run_worker("report")
        catalog.reload()
        self.assertNotEqual(catalog.coverage[base["index"]]["status"], "verified")
        self.assertEqual(catalog.coverage[donor["index"]]["status"], "verified")
        view = self.action("confirm", base="import", source=self.mirror, name="Evening")
        group = view["links"][0]["id"]
        self.run_worker("report")
        catalog.reload()
        self.assertEqual(catalog.playlists[group], imported)
        self.assertTrue(all(catalog.coverage[i]["status"] == "verified" for i in imported))
        self.assertEqual(catalog.coverage[base["index"]]["path"], catalog.coverage[donor["index"]]["path"])
        base = catalog.record_at(imported[0])
        self.assertTrue(catalog.is_favorite(base))
        self.assertEqual(catalog.genre_entry(base)["genre"], "Jazz")
        self.assertEqual(catalog.linked_history(events.history())[base["key"]][1], 2)
        self.assertEqual(events.stats(*catalog.recording_keys(base))[0], 2)
        self.run_worker("export", OFFLINE_MUSIC_SCOPE=group)
        manifest, = (self.room / "exported" / "Evening").glob("*.m3u8")
        contents = [(manifest.parent / line).read_bytes() for line in manifest.read_text().splitlines()
                    if line and not line.startswith("#")]
        self.assertEqual(contents, [b"One", b"One", b"Two", b"Three"])

        self.action("unlink", id=group)
        catalog.reload()
        self.assertEqual(catalog.playlists["import"], imported)
        self.assertEqual(catalog.playlists[self.mirror], mirrored)
        self.assertTrue(catalog.is_favorite(catalog.record_at(mirrored[-1])))
        self.assertFalse(catalog.is_favorite(catalog.record_at(imported[0])))
        self.assertEqual(catalog.genre_entry(catalog.record_at(imported[0]))["genre"], "Pop")
        self.assertEqual(events.stats(base["key"])[0], 1)

    def test_manual_group_and_undo_preserve_two_small_sources(self):
        self.write("one", ["One"])
        self.write("two", ["Two"])
        self.prepare()
        view = self.action("group", base="one", source="two")
        group = view["links"][0]["id"]
        self.assertEqual(list(self.build["playlists"]), [group])
        self.assertEqual(len(self.build["playlists"][group]), 2)
        self.action("unlink", id=group)
        self.assertEqual(set(self.build["playlists"]), {"one", "two"})

    def test_short_exact_lists_with_same_name_suggest_one_reviewable_match(self):
        self.write("one", ["Song"], title="Evening")
        self.write("two", ["Song"], title="Evening")
        self.write("other", ["Song"], title="Morning")
        view = self.prepare()
        self.assertFalse(view["links"])
        candidate, = view["candidates"]
        self.assertEqual({candidate["base"], candidate["source"]}, {"one", "two"})

    def test_provider_id_links_even_without_overlap_and_undo_persists(self):
        self.write("original", ["One"], playlist_id=f"https://open.spotify.com/playlist/{ID}")
        self.write(self.mirror, ["Two"], mirror=True)
        view = self.prepare()
        group, = view["links"]
        self.assertEqual(group["sources"], ["original", self.mirror])
        view = self.action("unlink", id=group["id"])
        self.assertEqual(view["links"], [])
        self.assertEqual(self.prepare()["links"], [])
        self.action("reconsider", base="original", source=self.mirror)
        self.assertEqual(len(self.build["playlists"]), 1)

    def test_ambiguous_recordings_remain_separate_and_dismissal_survives(self):
        names = ["One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]
        self.write("import", names)
        self.write(self.mirror, names, mirror=True)
        # Conflicting IDs for the exact same tags prevent the metadata-only
        # entry for One from being collapsed into a provider track.
        extra = dict(title="One", artist="Fixture", album="Album", identifier="spotify:track:alternate")
        (self.lists / "third.xspf").write_text(write_xspf("Third", [extra]))
        candidate, = self.prepare()["candidates"]
        self.assertTrue(candidate["ambiguous"])
        self.assertLess(candidate["matched"], len(names))
        view = self.action("dismiss", base="import", source=self.mirror)
        self.assertFalse(view["candidates"])
        self.assertFalse(self.prepare()["candidates"])


class PlaylistLinkHttpTests(ServerFixture, unittest.TestCase):
    def test_paired_device_cannot_review_or_change_links(self):
        cookie = self.lan_device_cookie()
        for method in ("GET", "POST"):
            status, _, _ = self.request(self.lan_port, method, "/api/playlist-links",
                                        headers={"Cookie": cookie, "Origin": self.lan_origin()},
                                        body={"op": "dismiss"} if method == "POST" else None)
            self.assertEqual(status, 403)
        status, _, data = self.request(self.loop_port, "GET", "/api/playlist-links")
        self.assertEqual(status, 200)
        self.assertIn("candidates", json.loads(data))
