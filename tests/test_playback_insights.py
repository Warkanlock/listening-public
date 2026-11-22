import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from playback.insights import PlaybackTraffic


class PlaybackTrafficTests(unittest.TestCase):
    def test_samples_accumulate_and_expire_with_no_cross_output_totals(self):
        now = 100.0
        playback = PlaybackTraffic(lambda: now)
        first = ("owner", "output-one")
        second = ("owner", "output-two")
        playback.record(first, sent=12)
        playback.record(first, sent=8, received=30)
        playback.record(second, sent=9)
        now += 2
        playback.record(first, received=5)
        self.assertEqual(playback.snapshot(first)["upload_bytes"], 30)
        now += .25
        current = playback.snapshot(first)
        history = current.pop("history")
        self.assertEqual(current,
                         {"window_seconds": 5, "download_bytes": 20, "upload_bytes": 35})
        self.assertEqual(history[-1], {"download_bytes": 20, "upload_bytes": 35})
        self.assertEqual(playback.snapshot(second)["download_bytes"], 9)
        now += 3
        self.assertEqual(playback.snapshot(first)["download_bytes"], 0)
        self.assertEqual(playback.snapshot(first)["upload_bytes"], 5)
        now += 5
        self.assertEqual(playback.snapshot(first)["upload_bytes"], 0)

    def test_bounds_the_number_of_retained_outputs(self):
        now = 100.0
        playback = PlaybackTraffic(lambda: now)
        for index in range(playback.MAX_OUTPUTS + 1):
            playback.record(("owner", str(index)), received=1)
        now += .25
        self.assertEqual(playback.snapshot(("owner", "0"))["upload_bytes"], 0)
        self.assertEqual(playback.snapshot(("owner", str(playback.MAX_OUTPUTS)))["upload_bytes"], 1)

    def test_previous_transfer_is_visible_after_live_rate_goes_idle(self):
        now = 100.0
        playback = PlaybackTraffic(lambda: now)
        output = ("owner", "output-one")
        playback.record(output, sent=1024, received=64)
        now += 5.25
        snapshot = playback.snapshot(output)
        self.assertEqual(snapshot["download_bytes"], 0)
        self.assertEqual(snapshot["upload_bytes"], 0)
        self.assertEqual(len(snapshot["history"]), 12)
        self.assertEqual(snapshot["history"][-2], {"download_bytes": 1024, "upload_bytes": 64})
        self.assertTrue(all(window == {"download_bytes": 0, "upload_bytes": 0}
                            for window in playback.snapshot(("owner", "output-two"))["history"]))
        now += 55
        self.assertTrue(all(window == {"download_bytes": 0, "upload_bytes": 0}
                            for window in playback.snapshot(output)["history"]))
