import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import tempfile
import unittest
from pathlib import Path

from library.library_protocol import read_list, write_xspf
from library.local_sources import (discover_folders, is_music_list, list_files, mirror_key,
                                   mirror_path, source_mirror_kinds, sync_files, sync_folders)


class LocalSourcesTests(unittest.TestCase):
    def test_discover_and_watch_multiple_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            first = home / "Downloads" / "first"
            second = home / "Music" / "second"
            destination = home / "Music" / "ListeningRoom" / "lists"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "favorites.csv").write_text("Title,Artist\nFirst,Artist\n")
            (first / "bank.csv").write_text("Date,Amount\n2026-01-01,20\n")
            (second / "favorites.csv").write_text("Title,Artist\nSecond,Artist\n")
            suggestions = discover_folders(home, destination)
            self.assertEqual({item["path"] for item in suggestions}, {str(first.resolve()), str(second.resolve())})
            self.assertEqual(next(item["count"] for item in suggestions if item["path"] == str(first.resolve())), 1)
            imported, errors = sync_folders([first, second], destination)
            self.assertFalse(errors)
            self.assertEqual(len(imported), 2)
            self.assertEqual({read_list(path)[0]["title"] for path in imported}, {"First", "Second"})
            self.assertEqual(sync_folders([first, second], destination), ([], []))
            self.assertEqual(len(list_files(destination)), 2)
            (first / "favorites.csv").write_text("Title,Artist\nUpdated,Artist\n")
            imported, errors = sync_folders([first, second], destination)
            self.assertEqual(len(imported), 1)
            self.assertFalse(errors)
            self.assertEqual(read_list(imported[0])[0]["title"], "Updated")

    def test_invalid_file_does_not_stop_valid_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "bad.csv").write_text("Title,Artist\n,\n")
            (source / "good.csv").write_text("Title,Artist\nSong,Artist\n")
            imported, errors = sync_folders([source], root / "lists")
            self.assertEqual(len(imported), 1)
            self.assertEqual(len(errors), 1)

    def test_sync_files_reports_an_oversized_field_csv_as_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            huge = root / "huge.csv"
            huge.write_text("Title,Artist\n" + ("x" * 200_000) + ",Some Artist\n")
            good = root / "good.csv"
            good.write_text("Title,Artist\nSong,Artist\n")
            imported, errors = sync_files([huge, good], root / "lists")
            self.assertEqual(len(imported), 1)
            self.assertEqual(len(errors), 1)
            self.assertTrue(errors[0]["source"].endswith("huge.csv"))
            self.assertIn("field larger than field limit", errors[0]["error"])

    def test_is_music_list_sniffing_by_format(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            xspf = root / "mix.xspf"
            xspf.write_text('<?xml version="1.0"?>\n'
                            '<playlist xmlns="http://xspf.org/ns/0/" version="1">'
                            "<trackList /></playlist>")
            self.assertTrue(is_music_list(xspf))
            fake = root / "fake.xspf"
            fake.write_text("<html>not a playlist</html>")
            self.assertFalse(is_music_list(fake))
            m3u = root / "mix.m3u"
            m3u.write_text("#EXTM3U\n#EXTINF:180,Artist - Song\naudio/song.mp3\n")
            self.assertTrue(is_music_list(m3u))
            no_artist = root / "songs.csv"
            no_artist.write_text("Title,Album\nSong,Album\n")
            self.assertFalse(is_music_list(no_artist))
            self.assertFalse(is_music_list(root / "missing.csv"))

    def test_list_files_respects_depth_and_count_caps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = "Title,Artist\nSong,Artist\n"
            (root / "top.csv").write_text(content)
            deep = root / "one" / "two"
            deep.mkdir(parents=True)
            (deep / "mid.csv").write_text(content)
            deeper = deep / "three"
            deeper.mkdir()
            (deeper / "bottom.csv").write_text(content)
            hidden = root / ".hidden"
            hidden.mkdir()
            (hidden / "secret.csv").write_text(content)
            real_dir = root / "real"
            real_dir.mkdir()
            (real_dir / "inside.csv").write_text(content)
            (root / "linked.csv").symlink_to(root / "top.csv")
            (root / "linked_dir").symlink_to(real_dir, target_is_directory=True)
            found = {path.name for path in list_files(root)}
            self.assertEqual(found, {"top.csv", "mid.csv", "inside.csv"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for number in range(505):
                (root / f"list_{number:03d}.csv").write_text("Title,Artist\nSong,Artist\n")
            self.assertEqual(len(list_files(root)), 500)

    def test_discover_folders_excludes_the_workspace_and_caps_results(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            content = "Title,Artist\nSong,Artist\n"
            workspace = home / "Music" / "ListeningRoom" / "lists"
            workspace.mkdir(parents=True)
            for number in range(5):
                (workspace / f"mine_{number}.csv").write_text(content)
            busy = home / "Downloads" / "busy"
            busy.mkdir(parents=True)
            for number in range(3):
                (busy / f"busy_{number}.csv").write_text(content)
            for number in range(13):
                folder = home / "Music" / f"folder_{number:02d}"
                folder.mkdir(parents=True)
                (folder / "one.csv").write_text(content)
            suggestions = discover_folders(home, workspace)
            paths = [item["path"] for item in suggestions]
            self.assertNotIn(str(workspace.resolve()), paths)
            self.assertEqual(len(suggestions), 12)
            self.assertEqual(suggestions[0]["path"], str(busy.resolve()))
            self.assertEqual(suggestions[0]["count"], 3)

    def test_sync_files_skips_sources_inside_the_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "lists"
            destination.mkdir()
            inside = destination / "already.csv"
            inside.write_text("Title,Artist\nInside,Artist\n")
            outside = root / "outside.csv"
            outside.write_text("Title,Artist\nOutside,Artist\n")
            notes = root / "notes.txt"
            notes.write_text("Title,Artist\nNot,A,List\n")
            imported, errors = sync_files([inside, outside, notes], destination)
            self.assertEqual(errors, [])
            self.assertEqual(len(imported), 1)
            self.assertEqual(read_list(imported[0])[0]["title"], "Outside")
            self.assertEqual(inside.read_text(), "Title,Artist\nInside,Artist\n")

    def test_linked_file_updates_without_removing_last_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "outside" / "mix.csv"
            source.parent.mkdir()
            source.write_text("Title,Artist\nOld,Artist\n")
            imported, errors = sync_files([source], root / "lists")
            self.assertEqual(len(imported), 1)
            self.assertFalse(errors)
            source.write_text("Title,Artist\nNew,Artist\n")
            updated, errors = sync_files([source], root / "lists")
            self.assertEqual(updated, imported)
            self.assertEqual(read_list(updated[0])[0]["title"], "New")
            source.unlink()
            self.assertEqual(sync_files([source], root / "lists"), ([], []))
            self.assertTrue(imported[0].exists())


class SourceMirrorKindsTests(unittest.TestCase):
    """Plan 046: classification reads the same mirror names the sync
    writes, so the two can never drift apart."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.lists = self.root / "lists"
        self.lists.mkdir()

    def write_list(self, path, title="List"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Title,Artist\nSong,Artist\n", encoding="utf-8")

    def test_mirror_path_is_where_sync_files_writes(self):
        source = self.root / "elsewhere" / "my list.csv"
        self.write_list(source)
        imported, errors = sync_files([source], self.lists)
        self.assertFalse(errors)
        self.assertEqual(imported, [mirror_path(source, self.lists)])
        self.assertEqual(mirror_key(source), imported[0].stem)

    def test_legacy_watched_copy_upgrades_at_the_existing_key(self):
        watched = self.root / "watched"
        source = watched / "mix.csv"
        self.write_list(source)
        mirror = mirror_path(source, self.lists)
        original = write_xspf("mix", read_list(source)).encode()
        mirror.write_bytes(original)
        imported, errors = sync_folders([watched], self.lists)
        self.assertFalse(errors)
        self.assertEqual(imported, [mirror])
        source.write_text("Title,Artist\nReplacement,Band\n")
        self.assertEqual(sync_folders([watched], self.lists), ([mirror], []))
        self.assertEqual(read_list(mirror)[0]["title"], "Replacement")
        self.assertEqual([p.resolve() for p in self.lists.glob("*.xspf")], [mirror.resolve()])
        backup, = (self.lists / ".previous").rglob(mirror.name)
        self.assertEqual(backup.read_bytes(), original)

    def test_edited_watched_copy_becomes_independent_and_stays_unchanged(self):
        watched = self.root / "watched"
        source = watched / "mix.csv"
        self.write_list(source)
        [original], errors = sync_folders([watched], self.lists)
        self.assertFalse(errors)
        edited = original.read_bytes().replace(b"Song", b"Owner edit")
        original.write_bytes(edited)
        [fresh], errors = sync_folders([watched], self.lists)
        self.assertFalse(errors)
        self.assertNotEqual(original, fresh)
        self.assertEqual(original.read_bytes(), edited)
        kinds = source_mirror_kinds({"source_folders": [str(watched)]}, self.lists)
        self.assertEqual(kinds, {fresh.stem: "watched"})
        self.assertEqual(sync_folders([watched], self.lists), ([], []))

    def test_linked_and_watched_classification(self):
        linked = self.root / "linked.csv"
        self.write_list(linked)
        watched = self.root / "watched"
        inside = watched / "inside.csv"
        self.write_list(inside)
        settings = {"source_files": [str(linked)], "source_folders": [str(watched)]}
        self.assertEqual(source_mirror_kinds(settings),
                         {mirror_key(linked): "linked", mirror_key(inside): "watched"})

    def test_linked_wins_when_a_file_is_both(self):
        watched = self.root / "watched"
        inside = watched / "inside.csv"
        self.write_list(inside)
        settings = {"source_files": [str(inside)], "source_folders": [str(watched)]}
        self.assertEqual(source_mirror_kinds(settings), {mirror_key(inside): "linked"})

    def test_unwatched_source_claims_nothing(self):
        linked = self.root / "linked.csv"
        self.write_list(linked)
        self.assertEqual(source_mirror_kinds({"source_files": [], "source_folders": []}), {})


if __name__ == "__main__":
    unittest.main()
