import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import unittest

from library.library_protocol import read_list

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class ExampleListTests(unittest.TestCase):
    """Plan 020: the shipped sample lists are living spec examples.
    Both files name the same tracks and must parse through the real
    list readers to the identical track set."""

    def test_sample_files_parse_to_the_same_tracks(self):
        csv_tracks = read_list(EXAMPLES / "sample.csv")
        xspf_tracks = read_list(EXAMPLES / "sample.xspf")
        for tracks in (csv_tracks, xspf_tracks):
            self.assertGreaterEqual(len(tracks), 5)
            self.assertTrue(all(track["title"] and track["artist"]
                                for track in tracks))
        self.assertEqual({(track["title"], track["artist"]) for track in csv_tracks},
                         {(track["title"], track["artist"]) for track in xspf_tracks})
        self.assertEqual({(track["title"], track["duration_ms"]) for track in csv_tracks},
                         {(track["title"], track["duration_ms"]) for track in xspf_tracks})


if __name__ == "__main__":
    unittest.main()
