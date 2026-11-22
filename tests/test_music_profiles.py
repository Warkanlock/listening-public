"""Public edition metadata is matched to verified local recordings."""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import server
from metadata.music_profiles import MusicProfiles
from playback.playback import PlaybackSession

ARTIST = "00000000-0000-0000-0000-000000000001"
ALBUM = "00000000-0000-0000-0000-000000000002"
EDITION = "00000000-0000-0000-0000-000000000003"
FIRST = "00000000-0000-0000-0000-000000000004"
SECOND = "00000000-0000-0000-0000-000000000005"
OTHER = "00000000-0000-0000-0000-000000000006"
CREDIT = [{"artist": {"id": ARTIST, "name": "Fixture Band"}}]


def local(index, title, identity=""):
    return {"index": index, "title": title, "artist": "Fixture Band", "album": "Fixture Album",
            "length": 120, "uri": f"https://musicbrainz.org/recording/{identity}" if identity else ""}


class MusicProfilesTests(unittest.TestCase):
    def setUp(self):
        self.connections = Mock()
        self.profiles = MusicProfiles(self.connections)

    def test_public_album_preserves_disc_order_and_keeps_distinct_recordings(self):
        self.connections.recording_match.side_effect = OSError("recording lookup temporarily unavailable")
        self.connections.album_match.return_value = ALBUM
        self.connections.payload.return_value = {"id": ALBUM, "title": "Fixture Album",
            "artist-credit": CREDIT, "releases": [{"id": EDITION, "status": "Official"}]}
        self.connections.metadata.cached.side_effect = lambda _, __, load: load()
        self.connections.metadata.musicbrainz.return_value = {"id": EDITION, "title": "Fixture Album",
            "artist-credit": CREDIT, "media": [
                {"position": 1, "tracks": [{"number": "1", "recording": {"id": FIRST, "title": "One", "length": 120000}}]},
                {"position": 2, "tracks": [{"number": "1", "recording": {"id": SECOND, "title": "Two", "length": 120000}}]},
            ]}
        rows = [local(8, "One"), local(2, "Two", OTHER)]
        result = self.profiles.lookup("album", None, rows[0], rows, lambda _: "verified")
        self.assertEqual(result["artist_id"], ARTIST)
        first, second, extra = result["tracks"]
        self.assertEqual((first["disc"], first["track_id"], first["ready"]), (1, 8, True))
        self.assertEqual((second["disc"], second["musicbrainz_id"]), (2, SECOND))
        self.assertNotIn("track_id", second)
        self.assertEqual((extra["track_id"], extra["musicbrainz_id"], extra["library_only"]), (2, OTHER, True))

    def test_an_unavailable_catalog_keeps_existing_local_tracks(self):
        self.connections.recording_match.side_effect = OSError("offline")
        self.connections.album_match.return_value = None
        song = local(8, "One")
        result = self.profiles.lookup("album", None, song, [song], lambda _: "verified")
        self.assertTrue(result["unavailable"])
        self.assertEqual([row["track_id"] for row in result["tracks"]], [8])

    def test_artist_search_can_recover_from_an_unavailable_recording_lookup(self):
        song = local(8, "One")
        self.connections.recording_match.side_effect = OSError("recording lookup temporarily unavailable")
        self.connections.artist_match.return_value = {"id": ARTIST}
        self.connections.payload.return_value = {"id": ARTIST}
        self.connections.graph.return_value = {"root": {"title": "Fixture Band", "detail": "Group"}, "edges": []}
        result = self.profiles.lookup("artist", None, song, [song], lambda _: "verified")
        self.assertEqual(result["identity"], ARTIST)
        self.assertNotIn("unavailable", result)
        self.assertTrue(result["tracks"][0]["ready"])

    def test_artist_alias_keeps_library_tracks_and_uses_known_recording(self):
        song = {**local(8, "One", FIRST), "artist": "Fixture Band (original credit)"}
        self.connections.optional_payload.return_value = {"id": FIRST, "artist-credit": CREDIT}
        self.connections.payload.return_value = {"id": ARTIST}
        self.connections.graph.return_value = {"root": {"title": "Fixture Band", "detail": "Group"}, "edges": []}
        result = self.profiles.lookup("artist", None, song, [song], lambda _: "verified")
        self.connections.optional_payload.assert_called_once_with("recording", FIRST)
        self.connections.recording_match.assert_not_called()
        self.assertEqual(result["title"], "Fixture Band")
        self.assertEqual(result["tracks"][0]["track_id"], 8)
        self.assertEqual(result["library"], {"tracks": 1, "ready": 1, "albums": 1})
        self.assertEqual(result["library_albums"], [{"title": "Fixture Album", "track_id": 8}])

    def test_popular_recordings_keep_public_rank_and_only_link_matching_versions(self):
        payload = [
            {"recording_mbid": FIRST, "recording_name": "One", "artist_name": "Fixture Band",
             "release_name": "Fixture Album", "total_listen_count": 50, "length": 120000},
            {"recording_mbid": SECOND, "recording_name": "Two", "artist_name": "Fixture Band",
             "release_name": "Different Album", "total_listen_count": 140, "length": 120000},
            {"recording_mbid": FIRST, "recording_name": "One", "artist_name": "Fixture Band",
             "release_name": "Fixture Album", "total_listen_count": 20, "length": 120000},
            {"recording_mbid": OTHER, "recording_name": "Alternate", "artist_name": "Fixture Band",
             "release_name": "Fixture Album", "total_listen_count": 30, "length": 120000},
        ]
        records = [local(8, "One", FIRST), local(2, "Two"), local(3, "Alternate")]
        tracks = self.profiles.popular_tracks(payload, records, lambda _: "verified", lambda _: "")
        self.assertEqual([track["listens"] for track in tracks], [140, 50, 30])
        self.assertNotIn("track_id", tracks[0])
        self.assertEqual([(track["track_id"], track["ready"]) for track in tracks[1:]], [(8, True), (3, True)])

    def test_artist_highlights_keep_releases_when_recording_service_is_unavailable(self):
        release = {"release_group_mbid": ALBUM, "total_listen_count": 23,
                   "release_group": {"name": "Fixture Album", "type": "Album", "date": "1992-02"}}
        self.connections.metadata.artist_popularity.side_effect = [OSError("offline"), [release]]
        result = self.profiles.highlights(ARTIST, [], lambda _: "", lambda _: "")
        self.assertEqual(result["tracks"], [])
        self.assertEqual([(item["id"], item["year"], item["listens"]) for item in result["albums"]],
                         [(ALBUM, "1992", 23)])

    def test_local_plays_rank_songs_and_releases_for_the_supplied_history(self):
        first = {**local(1, "A Song"), "key": "one", "linked_recording": "a"}
        second = {**local(2, "B Song"), "key": "two", "album": "Second Album"}
        alias = {**first, "index": 3, "key": "alias"}
        self.connections.artist_match.return_value = None
        rows = [first, second, alias]
        result = self.profiles.lookup("artist", "", first, rows, lambda _: "verified",
                                      history={"one": (100, 2), "alias": (100, 2), "two": (200, 6)})
        self.assertEqual([(track["track_id"], track["plays"]) for track in result["tracks"]], [(2, 6), (1, 2), (3, 2)])
        self.assertEqual([(album["title"], album["plays"]) for album in result["library_albums"]],
                         [("Second Album", 6), ("Fixture Album", 2)])

    def test_album_queue_uses_only_active_tracks_and_resolved_custom_copies(self):
        catalog = Mock()
        catalog.active_record_at.side_effect = {3: {"index": 3}, 2: None}.get
        catalog.status_of.return_value = "verified"
        body = {"op": "play_album", "track_ids": [3, "custom-8", 2, 3]}
        with patch.object(server, "catalog", catalog, create=True), patch.object(server, "resolve_ref",
                lambda ref: {"id": ref} if ref == "custom-8" else None):
            self.assertEqual(server.selected_playback_tracks(body), [3, "custom-8"])
            with self.assertRaises(ValueError):
                server.selected_playback_tracks(body, custom=False)

    def test_album_playback_follows_supplied_order(self):
        session = PlaybackSession(lambda _: [1, 2, 3], lambda ref: {"id": ref} if ref else None)
        session.apply({"op": "shuffle"})
        snapshot = session.apply({"op": "play_album", "track_id": 1}, mood_refs=[3, 1, 2])
        self.assertEqual(snapshot["current_ref"], 1)
        self.assertEqual([track["id"] for track in snapshot["upcoming"]], [2])
        self.assertEqual(session.apply({"op": "previous"})["current_ref"], 3)
        with self.assertRaises(ValueError):
            session.apply({"op": "play_album", "track_id": True}, mood_refs=[1, 2])
        with self.assertRaises(ValueError):
            session.apply({"op": "play_album", "track_id": 1.0}, mood_refs=[1, 2])
        snapshot = session.apply({"op": "play_album", "track_id": "custom-8"}, mood_refs=[3, "custom-8", 2])
        self.assertEqual(snapshot["current_ref"], "custom-8")
        self.assertEqual([track["id"] for track in snapshot["upcoming"]], [2])
