import os
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from adapters import youtube_search
from downloads.requests_store import RequestsStore
from metadata.external_metadata import ExternalMetadata


class YouTubeSearchTest(unittest.TestCase):
    def test_long_mixes_refill_from_provider_cursors_and_reuse_cached_pages(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = RequestsStore(Path(folder.name) / "fixture.sqlite3")
        cached = ExternalMetadata(lambda: store).cached
        key = "fixture-key"
        videos = ["ice9fixture", "fixtureMix2", "fixtureMix3"]
        pages = [{"items": [{"id": {"videoId": video}}], "nextPageToken": f"fixture-{page}"}
                 for page, video in enumerate(videos)]
        with patch.dict(os.environ, {"LISTENING_YOUTUBE_API_KEY": key}), \
                patch.object(youtube_search, "read_json", side_effect=pages) as request:
            self.assertEqual(youtube_search.find("House", "long", cached=cached), ((videos[0],), True))
            self.assertEqual(youtube_search.find("House", "long", 2, cached=cached), ((videos[2],), False))
            self.assertEqual(youtube_search.find("House", "long", 1, cached=cached), ((videos[1],), True))
            with self.assertRaises(ValueError):
                youtube_search.find("House", "long", 3, cached=cached)
        calls = request.call_args_list
        self.assertEqual(len(calls), 3)
        args = [parse_qs(urlsplit(call.args[0]).query) for call in calls]
        self.assertNotIn("pageToken", args[0])
        self.assertEqual(args[1]["pageToken"], ["fixture-0"])
        self.assertEqual(args[2]["pageToken"], ["fixture-1"])
        self.assertEqual(args[0]["videoDuration"], ["long"])
        self.assertEqual(args[0]["q"], ["House continuous mix"])
        for call in calls:
            self.assertNotIn(key, call.args[0])
            self.assertEqual(call.args[1], {"X-Goog-Api-Key": key})
