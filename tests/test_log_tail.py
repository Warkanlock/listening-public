"""Plan 050 Step 3 (F2): log tails are read as bounded tails.

`tail_lines` must return exactly what reading the whole file and
keeping the last lines returned, while touching only the bytes near
the end of the file; the activity history's 700-line window over
batch logs rides on the same reader.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import tempfile
import unittest
from unittest import mock

from downloads.download_activity import recent_events
from system.logtail import BLOCK, tail_lines


class TailLinesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "runner.log"

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def whole_file_tail(self, count):
        return self.path.read_text(
            encoding="utf-8", errors="replace").splitlines()[-count:]

    def test_small_file_returns_its_last_lines(self):
        self.write("one\ntwo\nthree\n")
        self.assertEqual(tail_lines(self.path, 2), ["two", "three"])
        self.assertEqual(tail_lines(self.path, 35), ["one", "two", "three"])

    def test_no_trailing_newline_keeps_the_final_line(self):
        self.write("one\ntwo\nthree")
        self.assertEqual(tail_lines(self.path, 2), ["two", "three"])

    def test_empty_file_and_zero_count(self):
        self.write("")
        self.assertEqual(tail_lines(self.path, 5), [])
        self.write("one\n")
        self.assertEqual(tail_lines(self.path, 0), [])

    def test_missing_file_raises_like_read_text(self):
        with self.assertRaises(OSError):
            tail_lines(self.path, 5)

    def test_large_file_matches_the_whole_file_read(self):
        lines = [f"line {index} " + "x" * 60 for index in range(5000)]
        self.write("\n".join(lines) + "\n")
        self.assertGreater(self.path.stat().st_size, BLOCK)
        for count in (1, 35, 700):
            self.assertEqual(tail_lines(self.path, count),
                             self.whole_file_tail(count))

    def test_a_line_longer_than_a_block_is_not_cut(self):
        long_line = "y" * (BLOCK * 3)
        self.write(f"first\n{long_line}\nlast\n")
        self.assertEqual(tail_lines(self.path, 2), [long_line, "last"])
        self.assertEqual(tail_lines(self.path, 3),
                         ["first", long_line, "last"])

    def test_the_read_is_bounded_by_the_tail_not_the_file(self):
        lines = [f"line {index} " + "x" * 60 for index in range(5000)]
        self.write("\n".join(lines) + "\n")
        size = self.path.stat().st_size
        counter = [0]
        real_open = Path.open

        class CountingReader:
            def __init__(self, raw):
                self._raw = raw

            def read(self, count=-1):
                data = self._raw.read(count)
                counter[0] += len(data)
                return data

            def seek(self, *args):
                return self._raw.seek(*args)

            def tell(self):
                return self._raw.tell()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._raw.close()
                return False

        def counting_open(self, mode="r", *args, **kwargs):
            return CountingReader(real_open(self, mode, *args, **kwargs))

        expected = self.whole_file_tail(10)
        with mock.patch.object(Path, "open", counting_open):
            self.assertEqual(tail_lines(self.path, 10), expected)
        self.assertLess(counter[0], size)


class RecentEventsWindowTests(unittest.TestCase):
    """The activity history sees only the last 700 lines of a batch
    log; the bounded reader must keep that window exact."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def test_events_older_than_the_window_are_invisible(self):
        lines = ["SongJob: succeeded: Old Song"]
        lines += [f"noise {index}" for index in range(750)]
        lines.append("SongJob: succeeded: New Song")
        (self.state / "batch-00001-00002.log").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
        events = recent_events(self.state)
        self.assertEqual([event["song"] for event in events], ["New Song"])


if __name__ == "__main__":
    unittest.main()
