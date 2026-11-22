import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import unittest

from devices.announcements import AnnouncementBoard


class AnnouncementBoardTests(unittest.TestCase):
    def test_broadcast_and_private_delivery(self):
        clock = [100.0]
        board = AnnouncementBoard(now=lambda: clock[0])
        public = board.post("  Music starts soon  ")
        private = board.post("Your request is ready", "device_a")
        self.assertEqual(public["message"], "Music starts soon")
        self.assertEqual([item["id"] for item in board.for_device("device_a")],
                         [public["id"], private["id"]])
        self.assertEqual([item["id"] for item in board.for_device("device_b")], [public["id"]])
        clock[0] += 24 * 60 * 60 + 1
        self.assertEqual(board.for_device("device_a"), [])

    def test_board_caps_at_fifty(self):
        board = AnnouncementBoard(now=lambda: 100.0)
        for number in range(1, 52):
            item = board.post(f"Message {number}")
        self.assertEqual(item["id"], 51)
        served = board.for_device("device_a")
        self.assertEqual(len(served), 50)
        self.assertEqual(served[0]["id"], 2)
        self.assertEqual(served[-1]["id"], 51)

    def test_whitespace_collapses_and_control_characters_are_rejected(self):
        board = AnnouncementBoard()
        item = board.post("  too   many \n\n spaces\t here  ")
        self.assertEqual(item["message"], "too many spaces here")
        with self.assertRaises(ValueError):
            board.post("hello\x01world")

    def test_returned_items_are_copies(self):
        board = AnnouncementBoard()
        posted = board.post("Original message")
        posted["message"] = "Mutated"
        self.assertEqual(board.for_device("device_a")[0]["message"], "Original message")
        served = board.for_device("device_a")
        served[0]["message"] = "Mutated again"
        self.assertEqual(board.for_device("device_a")[0]["message"], "Original message")

    def test_rejects_empty_and_oversized_messages(self):
        board = AnnouncementBoard()
        for message in ("", "   ", "x" * 281, None):
            with self.subTest(message=message), self.assertRaises(ValueError):
                board.post(message)


if __name__ == "__main__":
    unittest.main()
