import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from catalog.hiking import COUNTS, plan


class Library:
    def __init__(self, genres, *, extra=()):
        self.records = [
            {"key": f"recording-{index}", "title": f"Song {index}", "artist": "Artist",
             "length": 123, "genre": genre, "ready": True, "active": True}
            for index, genre in enumerate(genres, start=1)
        ]
        self.records.extend(extra)

    def active_record_at(self, index):
        record = self.records[index - 1]
        return record if record["active"] else None

    def status_of(self, index):
        return "verified" if self.records[index - 1]["ready"] else "pending"

    @staticmethod
    def genre_canonical(record):
        return record["genre"]


class HikingTests(unittest.TestCase):
    def test_fills_each_trip_with_distinct_recordings_in_named_mixes(self):
        catalog = Library(["Ambient"] * 40 + ["Jazz"] * 40 + [""] * 40)
        for count in COUNTS:
            with self.subTest(count=count):
                trip = plan(catalog, count, 'test-room')
                picks = [track for mix in trip["mixes"] for track in mix["tracks"]]
                self.assertEqual(trip["total"], count)
                self.assertEqual(len(picks), count)
                self.assertEqual(len({track["id"] for track in picks}), count)
                self.assertEqual({mix["name"] for mix in trip["mixes"]}, {"Ambient", "Jazz", "Library shuffle"})
                for mix in trip["mixes"][:-1]:
                    self.assertEqual(Counter(catalog.records[track["id"] - 1]["genre"]
                                             for track in mix["tracks"]), {mix["name"]: count // 3})

    def test_caps_a_trip_to_active_verified_unique_recordings(self):
        duplicates = [
            {"key": "recording-1", "title": "Same song", "artist": "Artist", "length": 124,
             "genre": "", "ready": True, "active": True},
            {"key": "pending", "ready": False, "active": True},
            {"key": "inactive", "ready": True, "active": False},
        ]
        trip = plan(Library(["", "", ""], extra=duplicates), 90, 'test-room')
        self.assertEqual(trip["requested"], 90)
        self.assertEqual(trip["total"], 3)
        self.assertEqual(len(trip["mixes"]), 2)
        self.assertEqual({track["id"] for mix in trip["mixes"] for track in mix["tracks"]}, {1, 2, 3})

    def test_uses_shuffle_to_fill_a_large_trip_with_one_eligible_genre(self):
        trip = plan(Library(["Jazz"] * 32 + [""] * 85), 90, "test-room")
        self.assertEqual([len(mix["tracks"]) for mix in trip["mixes"]], [32, 58])

    def test_deduplicates_linked_recordings_and_binds_audio_to_the_collection(self):
        catalog = Library(["Jazz", "Jazz"])
        for record in catalog.records:
            record["linked_recording"] = 1
        first = plan(catalog, 30, "room-one")
        second = plan(catalog, 30, "room-two")
        self.assertEqual(first["total"], 1)
        self.assertNotEqual(first["mixes"][0]["tracks"][0]["audio"], second["mixes"][0]["tracks"][0]["audio"])

    def test_empty_and_single_recording_rooms(self):
        self.assertEqual(plan(Library([]), 30, "test-room")["mixes"], [])
        self.assertEqual(len(plan(Library(["Jazz"]), 60, "test-room")["mixes"]), 1)
