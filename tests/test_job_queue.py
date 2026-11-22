import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import tempfile
import unittest

from system.job_queue import JobQueue


class JobQueueTests(unittest.TestCase):
    """Plan 050 Step 2 (ADR 0002): the durable job queue keeps its
    order across instances (the server-restart shape), reports
    positions, and records the state transitions the cards read.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / "requests.sqlite3"
        self.queue = JobQueue(self.database)

    def test_enqueue_keeps_fifo_order_and_positions(self):
        first = self.queue.enqueue("genres", {"type": "library"})
        second = self.queue.enqueue("downloads", {"type": "all"})
        third = self.queue.enqueue("requests", {"type": "queue"})
        self.assertEqual([row["id"] for row in self.queue.queued()],
                         [first["id"], second["id"], third["id"]])
        self.assertEqual(self.queue.next_queued()["id"], first["id"])
        self.assertEqual(self.queue.position(first["id"]), 1)
        self.assertEqual(self.queue.position(second["id"]), 2)
        self.assertEqual(self.queue.position(third["id"]), 3)
        self.assertEqual(self.queue.queued_for("downloads")["id"],
                         second["id"])

    def test_scope_round_trips_as_data(self):
        row = self.queue.enqueue(
            "downloads", {"type": "list", "id": "evening", "name": "Evening"})
        self.assertEqual(self.queue.get(row["id"])["scope"],
                         {"type": "list", "id": "evening", "name": "Evening"})

    def test_the_queue_survives_a_new_instance_with_order_intact(self):
        self.queue.enqueue("downloads", {"type": "all"})
        self.queue.enqueue("genres", {"type": "library"})
        restarted = JobQueue(self.database)
        self.assertEqual([row["kind"] for row in restarted.queued()],
                         ["downloads", "genres"])
        self.assertEqual(restarted.position(
            restarted.queued()[1]["id"]), 2)

    def test_transitions_stamp_their_times(self):
        row = self.queue.enqueue("requests", {"type": "queue"})
        self.assertIsNone(row["started_at"])
        running = self.queue.mark_running(row["id"])
        self.assertEqual(running["state"], "running")
        self.assertIsNotNone(running["started_at"])
        self.assertEqual(self.queue.running_for("requests")["id"], row["id"])
        done = self.queue.mark_done(row["id"])
        self.assertEqual(done["state"], "done")
        self.assertIsNotNone(done["finished_at"])
        self.assertIsNone(self.queue.running_for("requests"))

    def test_failed_keeps_its_error_and_cancel_leaves_the_queue(self):
        failed = self.queue.enqueue("genres", {"type": "library"})
        self.queue.mark_failed(failed["id"], "The genre pass needs ffprobe")
        self.assertEqual(self.queue.latest_for("genres")["state"], "failed")
        self.assertEqual(self.queue.latest_for("genres")["error"],
                         "The genre pass needs ffprobe")
        cancelled = self.queue.enqueue("genres", {"type": "library"})
        self.queue.mark_cancelled(cancelled["id"])
        self.assertEqual(self.queue.queued(), [])
        self.assertEqual(self.queue.latest_for("genres")["state"],
                         "cancelled")

    def test_only_the_newest_terminal_row_per_kind_is_kept(self):
        for _ in range(3):
            row = self.queue.enqueue("requests", {"type": "queue"})
            self.queue.mark_done(row["id"])
        rows = self.queue._fetch("WHERE kind = 'requests'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "done")


if __name__ == "__main__":
    unittest.main()
