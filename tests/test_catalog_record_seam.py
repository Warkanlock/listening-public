"""Plan 052: the Catalog record seam, at the module layer.

record_at / active_record_at / valid_index are the one place the
1-based-index to 0-based-list mapping lives. Written against the
seam as specified in plan 052 Step 2, in the same step as the
HTTP dialect pins (tests/test_track_index_dialects.py), so the
test wave's first run covers both the pins and the seam.

The fixture mirrors tests/test_catalog_read_path.py's
make_catalog (this module imports no other test module).
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import tempfile
import unittest

from catalog.catalog import Catalog, CatalogPaths
from catalog.catalog_store import CatalogStore


def make_catalog(folder, records):
    state = Path(folder) / "state"
    state.mkdir(parents=True, exist_ok=True)
    database = state / "requests.sqlite3"
    manifest = {"records": records, "playlists": {}, "issues": [],
                "covers": {}, "list_names": {}}
    CatalogStore(database).write_build(manifest)
    paths = CatalogPaths(database=database, lists=Path(folder) / "lists")
    return Catalog(paths)


RECORD = {"index": 1, "key": "key-1", "artist": "Alpha Band",
          "title": "First Song", "album": "Debut", "length": 201,
          "uri": "", "active": True}

RECORDS = [RECORD,
           dict(RECORD, index=2, key="key-2", title="Second Song",
                active=False),
           dict(RECORD, index=3, key="key-3", title="Third Song")]
N = len(RECORDS)


class RecordAtTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.catalog = make_catalog(self.tmp.name,
                                    [dict(record) for record in RECORDS])

    def test_in_range_indices_return_their_records(self):
        for index in range(1, N + 1):
            self.assertIs(self.catalog.record_at(index),
                          self.catalog.records[index - 1])

    def test_out_of_range_indices_return_none(self):
        for index in (0, -1, -100, N + 1, N + 100):
            self.assertIsNone(self.catalog.record_at(index), index)

    def test_non_int_inputs_return_none(self):
        # A bool is never a song: the seam is strict even where a
        # caller is lenient (plan 052 Step 2's one tightening).
        for index in (True, False, "1", 1.0, None, [1]):
            self.assertIsNone(self.catalog.record_at(index), index)


class ActiveRecordAtTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.catalog = make_catalog(self.tmp.name,
                                    [dict(record) for record in RECORDS])

    def test_active_records_return_their_records(self):
        self.assertIs(self.catalog.active_record_at(1),
                      self.catalog.records[0])
        self.assertIs(self.catalog.active_record_at(3),
                      self.catalog.records[2])

    def test_inactive_record_returns_none(self):
        self.assertIsNone(self.catalog.active_record_at(2))

    def test_out_of_range_and_non_int_return_none(self):
        for index in (0, N + 1, True, "1", None):
            self.assertIsNone(self.catalog.active_record_at(index), index)

    def test_a_record_without_an_active_flag_counts_as_active(self):
        # Every active check in the codebase reads
        # record.get("active", True); the seam keeps that default.
        with tempfile.TemporaryDirectory() as folder:
            bare = dict(RECORD)
            del bare["active"]
            catalog = make_catalog(folder, [bare])
            self.assertIs(catalog.active_record_at(1), catalog.records[0])


class ValidIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.catalog = make_catalog(self.tmp.name,
                                    [dict(record) for record in RECORDS])

    def test_in_range_ints_are_valid(self):
        for index in range(1, N + 1):
            self.assertTrue(self.catalog.valid_index(index), index)

    def test_out_of_range_and_non_int_are_invalid(self):
        for index in (0, -1, N + 1, True, False, "1", 1.0, None):
            self.assertFalse(self.catalog.valid_index(index), index)

    def test_validity_does_not_depend_on_active(self):
        # valid_index is the range dialect only; the inactive
        # record 2 is a valid index whose record active_record_at
        # refuses. The two questions stay separate, as the pinned
        # HTTP dialects keep them.
        self.assertTrue(self.catalog.valid_index(2))
        self.assertIsNone(self.catalog.active_record_at(2))


class TrackTrapTests(unittest.TestCase):
    """The latent trap the seam removes: track(0) used to return
    the last record's payload with "id": 0 (records[-1]). Now the
    read goes through record_at and an unresolvable index raises
    IndexError naming the index. Callers guarantee valid indices
    (plan 052 Step 3's caller audit); this pins the failure mode
    for the ones that would break that guarantee."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.catalog = make_catalog(self.tmp.name,
                                    [dict(record) for record in RECORDS])

    def test_track_raises_for_zero_and_beyond(self):
        for index in (0, -1, N + 1):
            with self.assertRaises(IndexError, msg=str(index)):
                self.catalog.track(index)

    def test_search_text_raises_for_zero_and_beyond(self):
        for index in (0, -1, N + 1):
            with self.assertRaises(IndexError, msg=str(index)):
                self.catalog.search_text(index)

    def test_track_still_answers_in_range(self):
        self.assertEqual(self.catalog.track(1)["id"], 1)
        self.assertEqual(self.catalog.track(1)["title"], "First Song")
        # search_text is the normalized match haystack (lowercased,
        # punctuation folded), not display text.
        self.assertIn("first song", self.catalog.search_text(1))


if __name__ == "__main__":
    unittest.main()
