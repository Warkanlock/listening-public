import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import tempfile
import unittest
import os
from pathlib import Path

from downloads.download_activity import parse_event, recent_events


class DownloadActivityTests(unittest.TestCase):
    def test_parses_search_success_and_failure(self):
        self.assertEqual(parse_event("[011] SongJob: searching: José González - Save Your Day"),
                         {"state": "searching", "song": "José González - Save Your Day", "reason": ""})
        self.assertEqual(parse_event("[011] SongJob: succeeded: José González - Save Your Day: undated\\music.mp3"),
                         {"state": "succeeded", "song": "José González - Save Your Day", "reason": ""})
        self.assertEqual(parse_event("[012] SongJob: failed [No search results]: Artist - Song"),
                         {"state": "failed", "song": "Artist - Song", "reason": "No search results"})
        self.assertIsNone(parse_event("Unrelated log output"))

    def test_downloading_lines_strip_the_source_path(self):
        self.assertEqual(parse_event("[011] SongJob: downloading: Artist - Song: downloads\\Artist - Song.mp3"),
                         {"state": "downloading", "song": "Artist - Song", "reason": ""})

    def test_song_and_reason_truncate_at_their_limits(self):
        event = parse_event("SongJob: searching: " + "S" * 200)
        self.assertEqual(event["song"], "S" * 180)
        event = parse_event("SongJob: failed [" + "R" * 200 + "]: Artist - Song")
        self.assertEqual(event["reason"], "R" * 120)
        self.assertEqual(event["song"], "Artist - Song")

    def test_only_the_newest_four_batches_and_700_lines_are_read(self):
        with tempfile.TemporaryDirectory() as directory:
            for number in range(1, 7):
                path = Path(directory, f"batch-{number:05d}-{number:05d}.log")
                path.write_text(f"SongJob: searching: Song {number}\n")
                os.utime(path, ns=(number * 1_000_000_000, number * 1_000_000_000))
            events = recent_events(directory)
            self.assertEqual([event["song"] for event in events],
                             ["Song 6", "Song 5", "Song 4", "Song 3"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "batch-00001-00705.log")
            path.write_text("".join(f"SongJob: searching: Track {number}\n"
                                    for number in range(705)))
            events = recent_events(directory, limit=1000)
            self.assertEqual(len(events), 700)
            self.assertEqual(events[0]["song"], "Track 704")
            self.assertEqual(events[-1]["song"], "Track 5")
            self.assertEqual(len(recent_events(directory)), 80)

    def test_missing_or_unreadable_logs_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(recent_events(directory), [])
            self.assertEqual(recent_events(Path(directory) / "missing"), [])
            unreadable = Path(directory, "batch-00001-00002.log")
            unreadable.mkdir()
            self.assertEqual(recent_events(directory), [])

    def test_reads_recent_batches_in_reverse_order_without_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory, "batch-00001-00050.log")
            first.write_text("SongJob: succeeded: First - Song: first.mp3\n")
            second = Path(directory, "batch-00051-00100.log")
            second.write_text("SongJob: searching: Second - Song\nSongJob: searching: Second - Song\n"
                              "SongJob: failed [No search results]: Second - Song\n")
            os.utime(first, ns=(1_000_000_000, 1_000_000_000))
            os.utime(second, ns=(2_000_000_000, 2_000_000_000))
            events = recent_events(directory)
            self.assertEqual([(event["state"], event["song"]) for event in events],
                             [("failed", "Second - Song"),
                              ("succeeded", "First - Song")])
            self.assertEqual(events[0]["batch"], "00051–00100")


if __name__ == "__main__":
    unittest.main()
