"""Plan 027: the Spotify share link, derived from the track's own URI.

Three levels, all offline: the derivation itself (catalog layer, one
place), the catalog track payload that exposes it, and the song-dialog
payload (track_metadata) the Share action renders from. A track whose
identifier is not a Spotify track URI gets no link at any level, and
the frontend shows no Share action for it.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import json
import tempfile
import unittest
import urllib.error

from catalog.catalog import Catalog, CatalogPaths, spotify_share_url
from catalog.catalog_store import CatalogStore
from downloads.requests_store import RequestsStore
from metadata.external_metadata import ExternalMetadata


TRACK_ID = "4uLU6hMCjMI75M1A2tKUQC"  # 22 base-62 characters
TRACK_URI = f"spotify:track:{TRACK_ID}"
TRACK_URL = f"https://open.spotify.com/track/{TRACK_ID}"
MBID = "b1a7c2e4-3f5a-4c8d-9e6f-0123456789ab"


class DerivationTests(unittest.TestCase):
    def test_spotify_track_uri_yields_the_canonical_url(self):
        self.assertEqual(spotify_share_url(TRACK_URI), TRACK_URL)

    def test_musicbrainz_identifiers_yield_no_link(self):
        self.assertEqual(spotify_share_url(MBID), "")
        self.assertEqual(spotify_share_url(f"https://musicbrainz.org/recording/{MBID}"), "")

    def test_isrc_only_and_identifierless_rows_yield_no_link(self):
        self.assertEqual(spotify_share_url(""), "")
        self.assertEqual(spotify_share_url(None), "")

    def test_other_spotify_entities_yield_no_link(self):
        self.assertEqual(spotify_share_url(f"spotify:album:{TRACK_ID}"), "")
        self.assertEqual(spotify_share_url(f"spotify:artist:{TRACK_ID}"), "")
        self.assertEqual(spotify_share_url(f"spotify:playlist:{TRACK_ID}"), "")

    def test_malformed_uris_yield_no_link(self):
        for uri in ("spotify:track:", "spotify:track:short",
                    f"spotify:track:{TRACK_ID}x", f"spotify:track:{TRACK_ID[:-1]}",
                    f"spotify:track:{TRACK_ID[:-1]}-", f"{TRACK_URI}:extra",
                    f"SPOTIFY:TRACK:{TRACK_ID}", f" {TRACK_URI}", f"{TRACK_URI} ",
                    TRACK_URL):
            with self.subTest(uri=uri):
                self.assertEqual(spotify_share_url(uri), "")


def make_record(index, uri):
    return {"index": index, "key": f"key-{index}", "uri": uri, "artist": "Artist",
            "title": "Song", "album": "Album", "length": 200, "isrc": "",
            "first_playlist": "mix", "active": True}


class CatalogPayloadTests(unittest.TestCase):
    """The track payload exposes the share link under spotify_url."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.state = root / "state"
        self.state.mkdir()
        self.lists = root / "lists"
        self.lists.mkdir()
        self.paths = CatalogPaths(database=self.state / "requests.sqlite3",
                                  lists=self.lists)

    def catalog(self, records):
        payload = {"records": records,
                   "playlists": {"mix": [r["index"] for r in records]},
                   "issues": [], "source_signature": {}, "covers": {}, "list_names": {}}
        CatalogStore(self.paths.database).write_build(payload)
        return Catalog(self.paths)

    def test_spotify_record_exposes_the_share_link(self):
        catalog = self.catalog([make_record(1, TRACK_URI)])
        self.assertEqual(catalog.track(1)["spotify_url"], TRACK_URL)

    def test_non_spotify_record_exposes_no_share_link(self):
        catalog = self.catalog([make_record(1, MBID), make_record(2, "")])
        self.assertEqual(catalog.track(1)["spotify_url"], "")
        self.assertEqual(catalog.track(2)["spotify_url"], "")


class FakeCatalog:
    def __init__(self, records, memberships=None, list_names=None):
        self.records = records
        self.memberships = memberships or {}
        self.list_names = list_names or {}

    def record_at(self, index):
        # The Catalog seam (plan 052), mirrored: a non-bool int in
        # range reads records[index - 1]; anything else is None.
        if isinstance(index, int) and not isinstance(index, bool) \
                and 1 <= index <= len(self.records):
            return self.records[index - 1]
        return None


def dialog_record(index, uri, isrc=""):
    return {"index": index, "artist": "Any Artist", "title": "Any Song",
            "album": "Any Album", "length": 210, "isrc": isrc,
            "key": f"k{index}", "active": True, "uri": uri}


class DialogPayloadTests(unittest.TestCase):
    """track_metadata carries spotify_url so the dialog's Share action
    renders exactly when a link exists. The MusicBrainz lookup is made
    to fail, so the payload under test is the local half, which is
    where the share link lives."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RequestsStore(Path(self.tmp.name) / "requests.sqlite3")

    def make_metadata(self):
        def transport(url, headers):
            raise urllib.error.HTTPError(url, 503, "error", None, None)
        return ExternalMetadata(lambda: self.store, transport=transport,
                                sleep=lambda seconds: None)

    def test_spotify_track_dialog_payload_carries_the_share_link(self):
        metadata = self.make_metadata()
        catalog = FakeCatalog([dialog_record(1, TRACK_URI)])
        result = metadata.track_metadata(1, catalog)
        self.assertEqual(result["spotify_url"], TRACK_URL)
        self.assertEqual(result["identifier"], TRACK_URI)

    def test_non_spotify_track_dialog_payload_has_no_share_link(self):
        metadata = self.make_metadata()
        catalog = FakeCatalog([dialog_record(1, MBID),
                               dialog_record(2, "", isrc="ISRC001")])
        self.assertEqual(metadata.track_metadata(1, catalog)["spotify_url"], "")
        self.assertEqual(metadata.track_metadata(2, catalog)["spotify_url"], "")


if __name__ == "__main__":
    unittest.main()
