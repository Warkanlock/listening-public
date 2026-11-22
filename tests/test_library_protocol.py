import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fixture_environment import isolate_room_environment as setUpModule
from catalog.catalog_store import CatalogStore
from library.library_protocol import (canonicalize_directory, csv_tracks, m3u_tracks,
                              migrate_legacy_lists, parse_list, playlist_identifier, playlist_title, portable_m3u8,
                              read_list, write_csv, write_xspf, xspf_tracks)


class LibraryProtocolTests(unittest.TestCase):
    def test_relocating_an_old_generated_sibling_keeps_one_playlist(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory) / "playlist"
            new = Path(directory) / "lists"
            old.mkdir()
            csv = old / "mix.csv"
            csv.write_text("title,artist\nSong,Artist\n")
            sibling = old / "mix.xspf"
            original = write_xspf("mix", read_list(csv))
            sibling.write_text(original)
            migrate_legacy_lists(old, new)
            self.assertEqual([p.name for p in new.glob("*.xspf")], ["mix.xspf"])
            self.assertEqual(sibling.read_text(), original)

    def test_empty_playlists_round_trip_without_blocking_sync(self):
        empty = csv_tracks("Track URI,Track Name,Artist Name(s),Album Name\n")
        self.assertEqual(empty, [])
        self.assertEqual(xspf_tracks(write_xspf("Empty Spotify playlist", empty)), [])

    def test_csv_adapters_round_trip_through_xspf(self):
        spotify = 'Track URI,Track Name,Artist Name(s),Album Name,Track Duration (ms),ISRC,Album Image URL\nspotify:track:abc,Song,Artist,Album,181000,USAAA1234567,https://example.org/art.jpg\n'
        generic = 'Title,Artist,Album,Duration\nAnother Song,Another Artist,Other Album,203.5\n'
        tracks = csv_tracks(spotify) + csv_tracks(generic)
        restored = xspf_tracks(write_xspf("Mixed library", tracks))
        self.assertEqual(restored, tracks)
        self.assertEqual(restored[0]["identifier"], "spotify:track:abc")
        self.assertEqual(restored[1]["duration_ms"], 203500)

    def test_incomplete_entry_keeps_its_playlist_position(self):
        rows = csv_tracks("Title,Artist\nFirst,Artist\nIncomplete,\nLast,Other\n")
        self.assertEqual(len(rows), 3)
        restored = xspf_tracks(write_xspf("List", rows))
        self.assertEqual([row["title"] for row in restored], ["First", "Incomplete", "Last"])
        self.assertEqual(restored[1]["artist"], "")

    def test_csv_no_artist_row_splits_at_first_dash(self):
        rows = csv_tracks("Title,Artist\nSome Artist - Some Title,\n")
        self.assertEqual((rows[0]["artist"], rows[0]["title"]),
                         ("Some Artist", "Some Title"))

    def test_csv_row_with_artist_keeps_dashed_title_intact(self):
        rows = csv_tracks("Title,Artist\nSinger - Dashed Title,Real Artist\n")
        self.assertEqual((rows[0]["artist"], rows[0]["title"]),
                         ("Real Artist", "Singer - Dashed Title"))

    def test_csv_legacy_prefixed_row_splits_as_the_old_special_case_did(self):
        rows = csv_tracks("Title,Artist\nToo Nano - Anything,\n")
        self.assertEqual((rows[0]["artist"], rows[0]["title"]),
                         ("Too Nano", "Anything"))

    def test_m3u_import_preserves_order_and_locations(self):
        tracks = m3u_tracks("#EXTM3U\n#EXTINF:180,Artist - First\n../audio/first.mp3\n"
                            "#EXTINF:210,Other - Second\n../audio/second.m4a\n")
        self.assertEqual([(track["artist"], track["title"]) for track in tracks],
                         [("Artist", "First"), ("Other", "Second")])
        self.assertEqual(xspf_tracks(write_xspf("Playlist", tracks)), tracks)
        self.assertIn("Second,Other,,210.0", write_csv(tracks))

    def test_portable_m3u_paths_do_not_expose_host_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio, playlists = root / "music", root / "exports"
            audio.mkdir()
            playlists.mkdir()
            (audio / "song.mp3").write_bytes(b"audio")
            playlist = playlists / "liked.m3u8"
            playlist.write_text(f"#EXTM3U\n#EXTINF:120,Artist - Song\n{audio / 'song.mp3'}\n"
                                "#EXTINF:100,Outside - Song\n/etc/passwd\n")
            portable = portable_m3u8(playlist, audio)
            self.assertEqual(portable, "#EXTM3U\n#EXTINF:120,Artist - Song\n../audio/song.mp3\n")
            self.assertNotIn(str(root), portable)

    def test_csv_alias_families(self):
        by_performer = csv_tracks("Song Title,Performer\nMy Song,The Performer\n")
        self.assertEqual((by_performer[0]["title"], by_performer[0]["artist"]),
                         ("My Song", "The Performer"))
        by_creator = csv_tracks("Title,Creator\nMy Song,The Creator\n")
        self.assertEqual(by_creator[0]["artist"], "The Creator")
        by_ms = csv_tracks("Title,Artist,Duration ms\nSong,Artist,181000\n")
        self.assertEqual(by_ms[0]["duration_ms"], 181000)
        by_length = csv_tracks("Title,Artist,Length\nSong,Artist,203.5\n")
        self.assertEqual(by_length[0]["duration_ms"], 203500)
        by_seconds = csv_tracks("Title,Artist,Seconds\nSong,Artist,10\n")
        self.assertEqual(by_seconds[0]["duration_ms"], 10000)
        by_mbid = csv_tracks("Title,Artist,MusicBrainz ID\nSong,Artist,mb-123\n")
        self.assertEqual(by_mbid[0]["identifier"], "mb-123")
        by_artwork = csv_tracks("Title,Artist,Artwork\nSong,Artist,https://example.org/art.jpg\n")
        self.assertEqual(by_artwork[0]["image"], "https://example.org/art.jpg")
        by_path = csv_tracks("Title,Artist,Path\nSong,Artist,/music/song.mp3\n")
        self.assertEqual(by_path[0]["location"], "/music/song.mp3")
        for header in ("track name", "TRACK-NAME", "Track  Name"):
            with self.subTest(header=header):
                rows = csv_tracks(f"{header},Artist\nVariant,Artist\n")
                self.assertEqual(rows[0]["title"], "Variant")

    def test_csv_rejections_and_soft_failures(self):
        with self.assertRaises(ValueError):
            csv_tracks("")
        with self.assertRaises(ValueError):
            csv_tracks("Title,Artist\nIncomplete,\n")
        rows = csv_tracks("Title,Artist,Duration\nSong,Artist,not-a-number\n")
        self.assertIsNone(rows[0]["duration_ms"])
        rows = csv_tracks("\ufeffTitle,Artist\nSong,Artist\n")
        self.assertEqual(rows[0]["title"], "Song")

    def test_xspf_rejects_doctype_entity_wrong_version_and_namespace(self):
        header = '<?xml version="1.0" encoding="UTF-8"?>\n'
        track = "<track><title>Song</title><creator>Artist</creator></track>"
        with self.assertRaises(ValueError):
            xspf_tracks(header + '<!DOCTYPE playlist [<!ENTITY x "y">]>\n'
                        '<playlist xmlns="http://xspf.org/ns/0/" version="1"><trackList /></playlist>')
        with self.assertRaises(ValueError):
            xspf_tracks(header + '<!ENTITY x "y">\n'
                        '<playlist xmlns="http://xspf.org/ns/0/" version="1"><trackList /></playlist>')
        with self.assertRaises(ValueError):
            xspf_tracks(header + '<playlist xmlns="http://xspf.org/ns/0/" version="2">'
                        f"<trackList>{track}</trackList></playlist>")
        with self.assertRaises(ValueError):
            xspf_tracks(header + '<playlist xmlns="http://example.com/ns" version="1">'
                        f"<trackList>{track}</trackList></playlist>")
        with self.assertRaises(ValueError):
            xspf_tracks("<playlist><trackList>")
        self.assertEqual(xspf_tracks(header + '<playlist xmlns="http://xspf.org/ns/0/" version="1">'
                                     "<trackList /></playlist>"), [])
        with self.assertRaises(ValueError):
            xspf_tracks(header + '<playlist xmlns="http://xspf.org/ns/0/" version="1"><trackList>'
                        "<track><title>Song</title></track></trackList></playlist>")

    def test_xspf_first_identifier_wins_and_isrc_comes_from_the_isrc_meta(self):
        content = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                   '<playlist xmlns="http://xspf.org/ns/0/" version="1"><trackList><track>'
                   "<title>Song</title><creator>Artist</creator>"
                   "<identifier>first-id</identifier><identifier>second-id</identifier>"
                   '<meta rel="https://example.com/other">NOT-AN-ISRC</meta>'
                   '<meta rel="https://isrc.ifpi.org/">USAAA1234567</meta>'
                   "</track></trackList></playlist>")
        tracks = xspf_tracks(content)
        self.assertEqual(tracks[0]["identifier"], "first-id")
        self.assertEqual(tracks[0]["isrc"], "USAAA1234567")

    def test_m3u_filename_fallback_and_incomplete_entries(self):
        tracks = m3u_tracks("#EXTM3U\n"
                            "audio/The_Band_-_The_Song.mp3\n"
                            "#EXTINF:-5,Negative - Duration\n"
                            "audio/negative.mp3\n"
                            "#EXTINF:abc,Bad - Duration\n"
                            "audio/bad.mp3\n"
                            "bare_name.mp3\n")
        self.assertEqual([(track["artist"], track["title"]) for track in tracks],
                         [("The Band", "The Song"), ("Negative", "Duration"),
                          ("Bad", "Duration"), ("", "bare name")])
        self.assertEqual([track["duration_ms"] for track in tracks], [None, None, None, None])
        with self.assertRaises(ValueError):
            m3u_tracks("#EXTM3U\nbare_name.mp3\n")

    def test_parse_list_rejects_an_unsupported_suffix(self):
        with self.assertRaises(ValueError):
            parse_list("x.txt", "Title,Artist\nSong,Artist\n")

    def test_legacy_migration_is_repeatable_and_keeps_originals(self):
        with tempfile.TemporaryDirectory() as temporary:
            old, new = Path(temporary, "playlist"), Path(temporary, "lists")
            old.mkdir()
            source = old / "liked.csv"
            source.write_text("Track Name,Artist Name(s)\nSong,Artist\n")
            self.assertEqual(len(migrate_legacy_lists(old, new)), 1)
            self.assertEqual(migrate_legacy_lists(old, new), [])
            self.assertTrue(source.is_file())
            self.assertEqual(read_list(new / "liked.xspf")[0]["title"], "Song")
            self.assertEqual(playlist_title(new / "liked.xspf"), "")
            updated = new / "liked.csv"
            updated.write_text("Track Name,Artist Name(s)\nNew Song,Artist\n")
            os.utime(updated, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
            canonicalize_directory(new)
            self.assertEqual(read_list(new / "liked.xspf")[0]["title"], "New Song")

    def test_colliding_sources_keep_their_contents_identity_and_stable_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            csv = folder / "mix.csv"
            m3u = folder / "mix.m3u"
            native = folder / "mix.xspf"
            contents = write_xspf("Owner", [], "spotify:playlist:" + "p" * 22).encode()
            native.write_bytes(contents)
            csv.write_text("Title,Artist\nCSV,Artist\n")
            m3u.write_text("#EXTM3U\n#EXTINF:120,Artist - M3U\nm3u.mp3\n")
            expected = {folder / "mix.csv.xspf", folder / "mix.m3u.xspf"}
            self.assertEqual(set(canonicalize_directory(folder)), expected)
            self.assertEqual(native.read_bytes(), contents)
            self.assertEqual(playlist_identifier(native), "spotify:playlist:" + "p" * 22)
            self.assertEqual({read_list(path)[0]["title"] for path in expected}, {"M3U", "CSV"})
            native.rename(folder / "renamed.xspf")
            csv.write_text("Title,Artist\nSecond,Artist\n")
            os.utime(csv, (5, 5))
            self.assertEqual(canonicalize_directory(folder), [folder / "mix.csv.xspf"])
            self.assertEqual(read_list(folder / "mix.csv.xspf")[0]["title"], "Second")
            self.assertEqual(canonicalize_directory(folder), [])

    def test_exact_legacy_mirror_upgrades_in_place_and_keeps_pre_upgrade_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "mix.csv"
            key = "spotify:playlist:" + "m" * 22
            source.write_text(f"Title,Artist,Playlist URI\nOriginal,Band,{key}\n")
            old = write_xspf("mix", read_list(source)).encode()
            mirror = folder / "mix.xspf"
            mirror.write_bytes(old)
            self.assertEqual(canonicalize_directory(folder), [mirror])
            self.assertEqual(list(folder.glob("*.xspf")), [mirror])
            self.assertEqual(playlist_identifier(mirror), key)
            backup, = (folder / ".previous").rglob("mix.xspf")
            self.assertEqual(backup.read_bytes(), old)
            source.write_text("Title,Artist\nNew,Band\n")
            self.assertEqual(canonicalize_directory(folder), [mirror])
            self.assertEqual(read_list(mirror)[0]["title"], "New")
            self.assertEqual(backup.read_bytes(), old)
            self.assertEqual(canonicalize_directory(folder), [])

    def test_editing_a_generated_list_preserves_the_edit_as_an_independent_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "mix.csv"
            source.write_text("Title,Artist\nOriginal,Artist\n")
            canonical, = canonicalize_directory(folder)
            edited = canonical.read_bytes().replace(b"Original", b"Edited")
            canonical.write_bytes(edited)
            alternate, = canonicalize_directory(folder)
            self.assertEqual(canonical.read_bytes(), edited)
            self.assertEqual(read_list(alternate)[0]["title"], "Original")
            self.assertEqual(canonicalize_directory(folder), [])

    def test_legacy_catalog_index_survives_xspf_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, new, state = root / "playlist", root / "lists", root / "state"
            old.mkdir()
            backup = state / "state-backup"
            backup.mkdir(parents=True)
            (old / "liked.csv").write_text("Track URI,Track Name,Artist Name(s),Album Name\n"
                                             "spotify:track:abc,Song,Artist,Album\n")
            old_record = {"index": 1, "key": "spotify:track:abc", "uri": "spotify:track:abc",
                          "artist": "Artist", "title": "Song", "album": "Album", "length": "",
                          "isrc": "", "first_playlist": "liked.csv", "active": True}
            (backup / "manifest.json").write_text(json.dumps({"records": [old_record],
                                                                "playlists": {"liked": [1]}}))
            migrate_legacy_lists(old, new)
            environment = dict(os.environ, OFFLINE_MUSIC_ROOT=str(root), OFFLINE_MUSIC_SOURCES=str(new),
                               OFFLINE_MUSIC_STATE=str(state), LISTENING_ROOM_CONFIG=str(state / "settings.json"))
            runner = Path(__file__).resolve().parents[1] / "backend" / "runner.py"
            subprocess.run([sys.executable, str(runner), "prepare"], env=environment,
                           check=True, capture_output=True, text=True)
            database = state / "requests.sqlite3"
            self.assertTrue(database.is_file())
            migrated = CatalogStore(database, state_dir=root / "verification-only").read_build()
            self.assertEqual(len(migrated["records"]), 1)
            self.assertEqual(migrated["records"][0]["index"], 1)
            self.assertTrue(migrated["records"][0]["active"])
            self.assertEqual(migrated["records"][0]["first_playlist"], "liked.csv")
            self.assertEqual(migrated["playlists"]["liked"], [1])


if __name__ == "__main__":
    unittest.main()
