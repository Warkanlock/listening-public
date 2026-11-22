import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import os
import tempfile
import unittest
from pathlib import Path

from library.list_intake import list_conflicts, place_lists, write_lists

CONTENT = "Title,Artist\nSong,Artist\n"
OTHER = "Title,Artist\nDifferent,Artist\n"


def folder_state(folder):
    """Every file under folder as {relative name: (bytes, mtime_ns)}."""
    return {str(path.relative_to(folder)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(folder.rglob("*")) if path.is_file()}


class ListIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def store(self, filename, content):
        path = self.folder / filename
        path.write_text(content, encoding="utf-8")
        return path

    def test_identical_reimport_is_a_noop_in_every_mode(self):
        path = self.store("mix.xspf", CONTENT)
        os.utime(path, (1_000_000, 1_000_000))
        before = folder_state(self.folder)
        for mode in (None, "replace", "rename"):
            self.assertEqual(write_lists(self.folder, [("mix.xspf", CONTENT)], mode),
                             ["mix.xspf"])
            self.assertEqual(folder_state(self.folder), before)

    def test_bom_equivalence_counts_as_identical_both_directions(self):
        self.store("bom.xspf", "\ufeff" + CONTENT)
        self.assertEqual(list_conflicts(self.folder, [("bom.xspf", CONTENT)]), [])
        self.store("plain.xspf", CONTENT)
        self.assertEqual(list_conflicts(self.folder, [("plain.xspf", "\ufeff" + CONTENT)]), [])
        self.assertEqual(write_lists(self.folder, [("bom.xspf", CONTENT)], "replace"),
                         ["bom.xspf"])
        self.assertFalse((self.folder / ".previous").exists())

    def test_only_a_differing_same_name_file_conflicts(self):
        self.store("same.xspf", CONTENT)
        self.store("different.xspf", CONTENT)
        incoming = [("missing.xspf", CONTENT), ("same.xspf", CONTENT),
                    ("different.xspf", OTHER)]
        self.assertEqual(list_conflicts(self.folder, incoming), ["different.xspf"])

    def test_replace_snapshots_one_generation(self):
        self.store("mix.xspf", CONTENT)
        write_lists(self.folder, [("mix.xspf", OTHER)], "replace")
        snapshot = self.folder / ".previous" / "mix.xspf"
        self.assertEqual(snapshot.read_text(encoding="utf-8"), CONTENT)
        self.assertEqual((self.folder / "mix.xspf").read_text(encoding="utf-8"), OTHER)
        third = "Title,Artist\nThird,Artist\n"
        write_lists(self.folder, [("mix.xspf", third)], "replace")
        self.assertEqual(snapshot.read_text(encoding="utf-8"), OTHER)
        self.assertEqual((self.folder / "mix.xspf").read_text(encoding="utf-8"), third)

    def test_non_utf8_stored_file_differs_and_is_snapshotted_by_copy(self):
        raw = b"\xff\xfe not a text list \x00\x01"
        (self.folder / "mix.xspf").write_bytes(raw)
        self.assertEqual(list_conflicts(self.folder, [("mix.xspf", CONTENT)]),
                         ["mix.xspf"])
        write_lists(self.folder, [("mix.xspf", CONTENT)], "replace")
        self.assertEqual((self.folder / ".previous" / "mix.xspf").read_bytes(), raw)
        self.assertEqual((self.folder / "mix.xspf").read_text(encoding="utf-8"), CONTENT)

    def test_rename_derives_the_next_free_name(self):
        self.store("Name.xspf", CONTENT)
        self.assertEqual(write_lists(self.folder, [("Name.xspf", OTHER)], "rename"),
                         ["Name (2).xspf"])
        self.assertEqual((self.folder / "Name (2).xspf").read_text(encoding="utf-8"), OTHER)
        self.assertEqual(write_lists(self.folder, [("Name.xspf", OTHER)], "rename"),
                         ["Name (3).xspf"])

    def test_place_lists_with_a_conflict_writes_nothing(self):
        self.store("mix.xspf", CONTENT)
        before = folder_state(self.folder)
        names, conflicts = place_lists(
            self.folder, [("new.xspf", CONTENT), ("mix.xspf", OTHER)], None)
        self.assertIsNone(names)
        self.assertEqual(conflicts, ["mix.xspf"])
        self.assertEqual(folder_state(self.folder), before)

    def test_intra_bundle_duplicates_last_wins_under_no_mode(self):
        names, conflicts = place_lists(
            self.folder, [("mix.xspf", CONTENT), ("mix.xspf", OTHER)], None)
        self.assertEqual(conflicts, [])
        self.assertEqual(names, ["mix.xspf", "mix.xspf"])
        self.assertEqual((self.folder / "mix.xspf").read_text(encoding="utf-8"), OTHER)
        self.assertFalse((self.folder / ".previous").exists())


if __name__ == "__main__":
    unittest.main()
