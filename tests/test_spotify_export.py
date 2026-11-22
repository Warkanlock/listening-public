import io
import json
from pathlib import Path
import stat
import sys
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from adapters import spotify_export
from fixture_environment import isolate_room_environment as setUpModule
from library.library_protocol import NS, xspf_tracks
import xml.etree.ElementTree as ET


TRACK_ID = "spotify:track:" + "a" * 22
OTHER_ID = "spotify:track:" + "b" * 22
PLAYLIST_ID = "spotify:playlist:" + "1" * 22


def raw(document):
    return json.dumps(document).encode()


def playlist(name, *items, **fields):
    return {"name": name, "items": items, **fields}


def song(title, identifier=TRACK_ID, **fields):
    return {"track": {"trackName": title, "artistName": "Björk & friends", "albumName": "An album",
                      "trackUri": identifier, **fields}, "episode": None, "localTrack": None}


def saved(title, identifier=TRACK_ID):
    return {"track": title, "artist": "Artist", "album": "Album", "uri": identifier}


def archive(*members):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for name, content in members:
            zipped.writestr(name, content)
    return data.getvalue()


def title(content):
    return ET.fromstring(content).findtext(f"{{{NS}}}title")


class SpotifyExportTests(unittest.TestCase):
    def test_official_zip_playlist_order_occurrences_and_unique_saved_tracks(self):
        playlists = {"playlists": [playlist("Road trip", song("First", durationMs=123000),
                                             song("Second", OTHER_ID), song("First"), playlistUri=PLAYLIST_ID)]}
        other = {"playlists": [playlist("Evening", song("Last"))]}
        library = {"tracks": [saved("One"), saved("Repeated"), saved("Two", OTHER_ID)],
                   "albums": [{"uri": "spotify:album:fixture"}], "shows": [{"show": "Fixture"}]}
        result = spotify_export.parse_export("account.zip", archive(
            ("Spotify Account Data/Playlist10.json", raw(other)),
            ("Spotify Account Data/StreamingHistory0.json", b"ignored"),
            ("Spotify Account Data/YourLibrary.json", raw(library)),
            ("Spotify Account Data/Playlist2.json", raw(playlists)),
        ))
        self.assertEqual([name for name, _ in result.lists],
                         ["spotify-road-trip.xspf", "spotify-evening.xspf", "spotify-saved-tracks.xspf"])
        self.assertEqual([title(content) for _, content in result.lists],
                         ["Road trip", "Evening", "Spotify Saved Tracks"])
        first = result.lists[0][1]
        self.assertEqual(ET.fromstring(first).findtext(f"{{{NS}}}identifier"), PLAYLIST_ID)
        rows = xspf_tracks(first)
        self.assertEqual([row["title"] for row in rows], ["First", "Second", "First"])
        self.assertEqual(rows[0]["duration_ms"], 123000)
        self.assertEqual(rows[0]["identifier"], TRACK_ID)
        self.assertEqual([row["title"] for row in xspf_tracks(result.lists[-1][1])], ["One", "Two"])
        self.assertEqual((result.tracks, result.skipped), (6, 2))

    def test_unique_safe_filenames_preserve_titles_and_skip_non_music(self):
        document = {"playlists": [
            playlist("../Saved Tracks", song("<&>"), {"episode": {"episodeName": "Podcast"}},
                     {"localTrack": {"uri": "file:///private/track"}}, song("No coercion", artistName=["a"]),
                     song("Invalid XML\u0000"), song("An episode", "spotify:episode:bad"),
                     description="Never export private notes"),
            playlist("Saved Tracks", song("Second")),
            playlist("Sávéd Tracks"),
            playlist("音楽"),
            {"name": "Invalid list", "items": "broken"},
        ]}
        result = spotify_export.parse_export("Playlist1.json", b"\xef\xbb\xbf" + raw(document))
        self.assertEqual([name for name, _ in result.lists], ["spotify-saved-tracks-2.xspf", "spotify-saved-tracks-3.xspf",
                                                               "spotify-saved-tracks-4.xspf", "spotify-playlist.xspf"])
        self.assertEqual([title(content) for _, content in result.lists], ["../Saved Tracks", "Saved Tracks", "Sávéd Tracks", "音楽"])
        first = result.lists[0][1]
        self.assertEqual(xspf_tracks(first)[0]["title"], "<&>")
        self.assertNotIn("private", first)
        self.assertEqual((result.tracks, result.skipped), (2, 6))

    def test_native_saved_tracks_support_records_without_a_spotify_uri(self):
        result = spotify_export.parse_export("YourLibrary.json", raw({"tracks": [saved("A song", ""), saved("A SONG", "")]}))
        self.assertEqual((result.tracks, result.skipped), (1, 0))
        self.assertEqual(xspf_tracks(result.lists[0][1])[0]["identifier"], "")

    def test_rejects_traversal_duplicate_source_basename_and_symlink(self):
        is_link = zipfile.ZipInfo("Spotify/Playlist1.json")
        is_link.create_system = 3
        is_link.external_attr = (stat.S_IFLNK | 0o777) << 16
        valid = raw({"playlists": []})
        for members, error in (
            ((("../unrelated.txt", b"data"), ("Spotify/Playlist1.json", valid)), "unsafe path"),
            ((("Spotify\\Playlist1.json", valid),), "unsafe path"),
            ((("C:/Playlist1.json", valid),), "unsafe path"),
            ((("One/Playlist1.json", valid), ("Two/playlist1.json", valid)), "duplicate export files"),
            (((is_link, b"Somewhere"),), "regular, unencrypted files"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                spotify_export.parse_export("data.zip", archive(*members))

    def test_size_and_count_limits_apply_to_the_expanded_export(self):
        valid = raw({"playlists": []})
        large = archive(("Playlist1.json", valid + b" " * 5000))
        with patch.object(spotify_export, "MAX_EXPORT_BYTES", 1000), self.assertRaisesRegex(ValueError, "expands past"):
            spotify_export.parse_export("large.zip", large)
        two = archive(("Playlist1.json", valid), ("Playlist2.json", valid))
        with patch.object(spotify_export, "MAX_EXPANDED_BYTES", len(valid)), self.assertRaisesRegex(ValueError, "expands past"):
            spotify_export.parse_export("large.zip", two)
        with patch.object(spotify_export, "MAX_ARCHIVE_MEMBERS", 1), self.assertRaisesRegex(ValueError, "too many entries"):
            spotify_export.parse_export("large.zip", two)
        crowded = raw({"playlists": [playlist("Two tracks", song("First"), song("Second"))]})
        with patch.object(spotify_export, "MAX_ITEMS", 1), self.assertRaisesRegex(ValueError, "too many items"):
            spotify_export.parse_export("Playlist1.json", crowded)

    def test_refuses_unknown_or_malformed_data(self):
        for name, data in [("StreamingHistory.json", b"{}"), ("Playlist1.json", b"[]"),
                           ("YourLibrary.json", b"{\xff}"), ("Playlist1.json", b"{}"),
                           ("broken.zip", b"no archive"), ("unrelated.zip", archive(("notes.txt", b"ignore")))]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                spotify_export.parse_export(name, data)
