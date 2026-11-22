import sys
from pathlib import Path
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from metadata.artist_connections import ArtistConnections
from metadata.discovery_trail import LibraryRecordings, branch, listening_source

ARTIST, STUDIO, LIVE, WORK = [f"00000000-0000-4000-8000-{i:012}" for i in range(1, 5)]


class DiscoveryTrailTests(unittest.TestCase):
    def test_cached_performances_are_filtered_against_todays_catalog(self):
        credit = [{"name": "Test Artist", "artist": {"id": ARTIST, "name": "Test Artist"}}]
        studio = {"id": STUDIO, "title": "High tide", "length": 180000, "artist-credit": credit}
        live = {**studio, "id": LIVE, "length": 226000, "disambiguation": "Warehouse session"}
        links = [{"type": "performance", "work": {"id": WORK}}, {"type": "streaming",
                  "url": {"resource": "https://tidal.com/track/123"}}]
        work = {"relations": [{"type": "performance", "recording": item, "attributes": ["live"]}
                              for item in (studio, live, live)]}
        payloads = {"recording/" + STUDIO: {**studio, "relations": links}, "work/" + WORK: work}
        metadata = Mock()
        metadata.musicbrainz.side_effect = lambda path, args: payloads[path]
        cache = {}
        def cached(key, ttl, fetch):
            if key not in cache:
                cache[key] = fetch()
            return cache[key]
        metadata.cached.side_effect = cached
        connections = ArtistConnections(metadata)
        local = {"title": "High tide", "artist": "Test Artist", "length": 180}
        result = branch(metadata, connections, "recording", STUDIO, LibraryRecordings([local]))
        self.assertEqual(result["source"]["name"], "TIDAL")
        recordings = [item for item in result["leads"] if item["kind"] == "recording"]
        self.assertEqual([(item["label"], item["id"]) for item in recordings], [("Live recording", LIVE)])
        self.assertEqual(recordings[0]["track"]["duration"], 226)
        self.assertIn(ARTIST, [edge["id"] for edge in result["leads"]])
        updated = LibraryRecordings([local, {**local, "uri": "musicbrainz:recording:" + LIVE}])
        again = branch(metadata, connections, "recording", STUDIO, updated)
        self.assertNotIn(LIVE, [edge["id"] for edge in again["leads"]])
        self.assertEqual(metadata.musicbrainz.call_count, 2)

    def test_ambiguous_legacy_version_and_reissue_stay_out(self):
        live = {"artist": "A", "title": "Night", "musicbrainz_id": LIVE, "duration": 300, "version": "Live"}
        self.assertTrue(LibraryRecordings([{"artist": "A", "title": "Night"}]).contains(live))
        with_version = {"artist": "A", "title": "Night (Live at the Warehouse)", "length": 300}
        self.assertTrue(LibraryRecordings([with_version]).contains({**live, "version": "Live at the Warehouse"}))
        identified = {"artist": "A", "title": "Night", "uri": "https://musicbrainz.org/recording/" + STUDIO}
        self.assertFalse(LibraryRecordings([identified]).contains(live))
        self.assertTrue(LibraryRecordings([identified]).contains({**live, "musicbrainz_id": STUDIO, "album": "Reissue"}))

    def test_search_homepage_and_ended_relationships_do_not_become_listening_actions(self):
        for address in ("https://www.youtube.com/results?search_query=Flood", "https://open.spotify.com/",
                        "https://music.apple.com/us/artist/example/123", "https://example.com/music"):
            self.assertIsNone(listening_source([{"type": "streaming", "url": {"resource": address}}]))
        url = {"resource": "https://bandcamp.com/track/fixture"}
        self.assertIsNone(listening_source([{"type": "official homepage", "url": url},
                                            {"type": "free streaming", "url": url, "ended": True},
                                            {"type": "streaming", "url": {"resource": "javascript:bad()"}}]))
