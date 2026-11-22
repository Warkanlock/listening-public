import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import os
import stat
import tempfile
import threading
import unittest

from downloads.verification import title_matches
from system.formats import AUDIO_FORMATS
from system.normalize import normalize_title
from system.private_files import private_write


class NormalizeTitleTest(unittest.TestCase):
    """Pinned edge cases for the one title normalization.

    The expected values are the verification side's behavior (the old
    runner.normalized_title), recorded before the unification: search
    adopts it, verification does not move.
    """

    def test_accents_are_stripped_and_case_is_folded(self):
        self.assertEqual(normalize_title("Beyoncé"), "beyonce")
        self.assertEqual(normalize_title("Sigur Rós"), "sigur ros")
        self.assertEqual(normalize_title("Motörhead"), "motorhead")
        self.assertEqual(normalize_title("Canción Núñez"), "cancion nunez")
        self.assertEqual(normalize_title("HeLLo WoRLD"), "hello world")

    def test_punctuation_splits_and_digits_survive(self):
        self.assertEqual(normalize_title("AC/DC"), "ac dc")
        self.assertEqual(normalize_title("P!nk"), "p nk")
        self.assertEqual(normalize_title("Blink-182"), "blink 182")
        self.assertEqual(normalize_title("Jay-Z"), "jay z")
        self.assertEqual(normalize_title("Rock & Roll"), "rock roll")

    def test_underscores_split_tokens(self):
        # The old server search kept underscores inside a token ("ac_dc");
        # verification split on them. Verification wins.
        self.assertEqual(normalize_title("AC_DC"), "ac dc")
        self.assertEqual(normalize_title("a_b_c"), "a b c")

    def test_non_ascii_letters_are_dropped(self):
        # The old server search kept Cyrillic/Greek letters and Æ/Ø;
        # verification dropped them. Verification wins.
        self.assertEqual(normalize_title("Москва"), "")
        self.assertEqual(normalize_title("Αθήνα"), "")
        self.assertEqual(normalize_title("Ænima"), "nima")
        self.assertEqual(normalize_title("Øya"), "ya")

    def test_whitespace_collapses_and_empty_is_empty(self):
        self.assertEqual(normalize_title("  Spaced   Out  "), "spaced out")
        self.assertEqual(normalize_title(""), "")
        self.assertEqual(normalize_title(None), "")


class TitleMatchesStabilityTest(unittest.TestCase):
    """Verification outcomes that must not move under the unification."""

    def test_exact_and_contained_titles_match(self):
        self.assertTrue(title_matches("Back in Black", "Back in Black"))
        self.assertTrue(title_matches("Back in Black", "AC-DC - Back in Black (1980)"))

    def test_generic_and_empty_tags_are_accepted(self):
        self.assertTrue(title_matches("Back in Black", "Track 01"))
        self.assertTrue(title_matches("Back in Black", ""))

    def test_unrelated_title_does_not_match(self):
        self.assertFalse(title_matches("Back in Black", "Highway to Hell"))


class PrivateWriteTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_writes_contents_at_0600_and_creates_parents(self):
        target = self.root / "nested" / "secret.txt"
        private_write(target, "s3cret")
        self.assertEqual(target.read_text(encoding="utf-8"), "s3cret")
        self.assertEqual(self.mode(target), 0o600)

    def test_overwrite_tightens_an_existing_loose_file(self):
        target = self.root / "config.json"
        target.write_text("old", encoding="utf-8")
        os.chmod(target, 0o644)
        private_write(target, "new")
        self.assertEqual(target.read_text(encoding="utf-8"), "new")
        self.assertEqual(self.mode(target), 0o600)

    def test_concurrent_writers_never_interleave(self):
        target = self.root / "state.json"
        payloads = [f"payload-{index}-" + "x" * 4096 for index in range(8)]
        barrier = threading.Barrier(len(payloads))

        def write(payload):
            barrier.wait()
            for _ in range(25):
                private_write(target, payload)

        threads = [threading.Thread(target=write, args=(payload,)) for payload in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertIn(target.read_text(encoding="utf-8"), payloads)
        self.assertEqual(self.mode(target), 0o600)
        self.assertEqual([p.name for p in self.root.iterdir()], ["state.json"])

    def test_exclusive_write_refuses_to_overwrite(self):
        target = self.root / "sockseek.conf"
        private_write(target, "username = a\n", exclusive=True)
        with self.assertRaises(FileExistsError):
            private_write(target, "username = b\n", exclusive=True)
        self.assertEqual(target.read_text(encoding="utf-8"), "username = a\n")
        self.assertEqual(self.mode(target), 0o600)

    def test_import_style_write_lands_at_0600(self):
        # The import handlers write user-supplied list content through
        # private_write; the file they produce must carry the guarantee.
        target = self.root / "lists" / "mix.xspf"
        content = "<?xml version=\"1.0\"?><playlist version=\"1\"/>"
        private_write(target, content)
        self.assertEqual(target.read_text(encoding="utf-8"), content)
        self.assertEqual(self.mode(target), 0o600)


class FormatsTest(unittest.TestCase):
    def test_the_one_formats_set(self):
        # Plan 050 step 4: FLAC joins; Opus, WAV, and AIFF stay out.
        self.assertEqual(AUDIO_FORMATS, {".mp3", ".m4a", ".aac", ".flac"})


if __name__ == "__main__":
    unittest.main()
