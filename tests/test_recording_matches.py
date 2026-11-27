import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import unittest

from fixture_environment import isolate_room_environment as setUpModule
from runner_fixture import runner


class RecordingMatchTests(unittest.TestCase):
    def test_same_isrc_shares_verified_audio_across_provider_ids(self):
        records = [
            {"index": 1, "isrc": "USWB10300468", "title": "Faint", "length": 162.1},
            {"index": 2, "isrc": "uswb10300468", "title": "Faint", "length": 162.2},
            {"index": 3, "isrc": "USWB10300468", "title": "Faint", "length": 190.0},
            {"index": 4, "isrc": "OTHER", "title": "Faint", "length": 162.1},
        ]
        rows = [
            {"index": 1, "status": "verified", "path": "/audio/faint.mp3", "reason": ""},
            {"index": 2, "status": "pending", "path": "", "reason": ""},
            {"index": 3, "status": "pending", "path": "", "reason": ""},
            {"index": 4, "status": "pending", "path": "", "reason": ""},
        ]
        checked = {"/audio/faint.mp3": {"media": {"duration": 162.1, "title": "Faint", "bitrate": 320000}}}
        runner.share_recording_matches(records, rows, checked)
        self.assertEqual(rows[1]["status"], "verified")
        self.assertEqual(rows[1]["path"], "/audio/faint.mp3")
        self.assertEqual([row["status"] for row in rows[2:]], ["pending", "pending"])


if __name__ == "__main__":
    unittest.main()
