import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from catalog.catalog import Catalog, CatalogPaths
from catalog.catalog_store import CatalogStore
from fixture_environment import isolate_room_environment as setUpModule
from library.library_protocol import xspf_tracks
from library.playlist_sharing import playlist_preview, playlist_xspf


class PlaylistSharingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.database = root / "collection.sqlite3"
        self.lists = root / "lists"

    def catalog(self, records, playlists, names):
        CatalogStore(self.database).write_build({"records": records, "playlists": playlists,
                                                 "list_names": names, "covers": {}})
        return Catalog(CatalogPaths(self.database, self.lists))

    def record(self, index, title, **extra):
        return {"index": index, "key": f"private-record-{index}", "uri": "", "artist": "Artist",
                "title": title, "album": "Album", "length": 182.5, "first_playlist": "source.csv",
                **extra}

    def test_preview_and_import_round_trip_keep_only_selected_metadata_in_order(self):
        mbid = "8ad4d753-db52-4879-9f0c-f2c1dd893d0a"
        url = f"https://musicbrainz.org/recording/{mbid}"
        catalog = self.catalog([
            self.record(1, "Unselected song"),
            self.record(2, "Björk & Company", uri=f"http://musicbrainz.org/recording/{mbid.upper()}"),
            self.record(3, "Finale", uri="spotify:track:abc", length="", isrc="SECRETISRC",
                        image="http://room.example:8765/art.png?token=secret", location="/mnt/private/music.mp3",
                        audio="/api/audio/3", favorite=True),
            self.record(4, "Retired song", active=False),
        ], {"private-source-key": [2, None, 3, 4, 2], "other-key": [1]},
            {"private-source-key": "Songs to share", "other-key": "Unselected playlist"})

        preview = playlist_preview(catalog, "private-source-key")
        self.assertEqual(preview, {"name": "Songs to share", "tracks": [
            {"title": "Björk & Company", "artist": "Artist", "album": "Album",
             "duration_ms": 182500, "musicbrainz_url": url},
            {"title": "Finale", "artist": "Artist", "album": "Album",
             "duration_ms": None, "musicbrainz_url": None},
            {"title": "Björk & Company", "artist": "Artist", "album": "Album",
             "duration_ms": 182500, "musicbrainz_url": url},
        ]})
        xspf = playlist_xspf(preview).decode("utf-8")
        for document in (json.dumps(preview), xspf):
            for private in ("Unselected", "Retired song", "private-source-key", "private-record-",
                            "source.csv", "spotify:", "SECRETISRC", "server:8765", "token=",
                            "/mnt/private/", "/api/audio/", "favorite"):
                self.assertNotIn(private, document)
        self.assertEqual(xspf_tracks(xspf), [
            {"title": row["title"], "artist": row["artist"], "album": row["album"],
             "duration_ms": row["duration_ms"], "identifier": row["musicbrainz_url"] or "",
             "image": "", "location": "", "isrc": ""}
            for row in preview["tracks"]
        ])

    def test_only_explicit_musicbrainz_recording_ids_are_shared(self):
        mbid = "8ad4d753-db52-4879-9f0c-f2c1dd893d0a"
        catalog = self.catalog([
            self.record(1, "Explicit ID", musicbrainz_id=mbid),
            self.record(2, "Unknown UUID", uri=mbid),
            self.record(3, "Private link", uri=f"https://musicbrainz.org/recording/{mbid}?token=secret"),
            self.record(4, "Non-recording ID", uri=f"https://musicbrainz.org/artist/{mbid}"),
        ], {"private-filename": [1, 2, 3, 4]}, {})
        preview = playlist_preview(catalog, "private-filename", name="Selected name")
        self.assertEqual(preview["name"], "Selected name")
        self.assertEqual([row["musicbrainz_url"] for row in preview["tracks"]],
                         [f"https://musicbrainz.org/recording/{mbid}", None, None, None])

    def test_empty_playlist_does_not_reveal_its_source_key(self):
        catalog = self.catalog([], {"private-filename": []}, {})
        preview = playlist_preview(catalog, "private-filename")
        self.assertEqual(preview, {"name": "Playlist", "tracks": []})
        self.assertEqual(xspf_tracks(playlist_xspf(preview).decode("utf-8")), [])
        with self.assertRaises(KeyError):
            playlist_preview(catalog, "unknown")
