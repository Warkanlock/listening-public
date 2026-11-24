import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import json
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from fixture_environment import isolate_room_environment as setUpModule
from runner_fixture import runner
from runner import focused_matches, request_failure, share_recording_matches


class RecordingMatchTests(unittest.TestCase):
    def test_resource_failures_are_retryable_even_with_success_exit_code(self):
        self.assertEqual(request_failure("Too many open files", 0)[0], "error")
        self.assertEqual(request_failure("Can't assign requested address", 0)[0], "error")
        self.assertEqual(request_failure("SongJob: failed [All downloads failed]", 0)[0], "error")
        self.assertEqual(request_failure("No search results", 0)[0], "not_found")

    def test_focused_results_reject_wrong_versions(self):
        groups = [{"User": {"Username": "peer", "HasFreeUploadSlot": True, "UploadSpeed": 5},
                   "Files": [{"Filename": "Music\\Linkin Park\\08 - In the End.flac", "Length": 216,
                              "Bitrate": 320},
                             {"Filename": "Music\\Linkin Park\\In the End demo.flac", "Length": 228,
                              "Bitrate": 320}]}]
        matches = focused_matches(groups, "In The End", 216.9)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][1], "peer")

    def test_song_filters_use_the_requested_suffixes_and_preference_without_peer_metadata(self):
        groups = [{"User": {"Username": "peer"}, "File": {"Filename": "Track" + ext}}
                  for ext in (".m4a", ".mp3", ".flac")]
        for choice, expected in [("flac", ["Track.flac"]), ("alac", ["Track.m4a"]),
                                 ("any_lossless", ["Track.flac", "Track.m4a"]),
                                 ("any_lossy", ["Track.mp3", "Track.m4a"])]:
            matches = focused_matches(groups, "Track", 200, requested_format=choice)
            self.assertEqual([name for _, _, name in matches], expected)

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
        share_recording_matches(records, rows, checked)
        self.assertEqual(rows[1]["status"], "verified")
        self.assertEqual(rows[1]["path"], "/audio/faint.mp3")
        self.assertEqual([row["status"] for row in rows[2:]], ["pending", "pending"])


class FocusedDownloadTests(unittest.TestCase):
    """A2 S-02: the focused path accepts only a file the chosen
    candidate's own download produced, judged by verification. A
    verified file merely sitting in staging is not an answer."""

    GROUPS = [{"User": {"Username": "peer", "HasFreeUploadSlot": True, "UploadSpeed": 5},
               "Files": [{"Filename": "Music\\Artist\\Right Song.flac", "Length": 200,
                          "Bitrate": 320}]}]
    GOOD = {"codec": "flac", "container": "flac", "bitrate": 320000, "duration": 200.0, "title": "Right Song"}
    WRONG = {"codec": "flac", "container": "flac", "bitrate": 320000, "duration": 200.0, "title": "Some Other Song"}

    def run_focused(self, staging, produced_name, media_by_name):
        def fake_run(command, **kwargs):
            if "--print" in command:
                return SimpleNamespace(returncode=0, stdout=json.dumps(self.GROUPS))
            if produced_name:
                (staging / produced_name).write_bytes(b"downloaded audio")
            return SimpleNamespace(returncode=0, stdout="")
        log_path = staging.parent / "focused.log"
        with mock.patch.object(subprocess, "run", fake_run), \
                mock.patch.object(runner, "probe",
                                  lambda path: media_by_name.get(Path(path).name)):
            return runner.focused_download("Album", "Right Song", 200, staging, log_path)

    def test_downloaded_matching_file_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            staging = Path(folder) / "staging"
            staging.mkdir()
            accepted = self.run_focused(staging, "Right Song.flac",
                                        {"Right Song.flac": self.GOOD})
        self.assertTrue(accepted)

    def test_staged_verified_file_is_not_accepted_when_download_delivers_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            staging = Path(folder) / "staging"
            staging.mkdir()
            (staging / "planted.flac").write_bytes(b"already here")
            accepted = self.run_focused(staging, None, {"planted.flac": self.GOOD})
            self.assertTrue((staging / "planted.flac").is_file())
        self.assertFalse(accepted)

    def test_staged_verified_file_does_not_cover_a_wrong_download(self):
        with tempfile.TemporaryDirectory() as folder:
            staging = Path(folder) / "staging"
            staging.mkdir()
            (staging / "planted.flac").write_bytes(b"already here")
            accepted = self.run_focused(
                staging, "downloaded.flac",
                {"planted.flac": self.GOOD, "downloaded.flac": self.WRONG})
            # The wrong file is left in staging and the search moves
            # on, exactly as a rejected verification does.
            self.assertTrue((staging / "downloaded.flac").is_file())
        self.assertFalse(accepted)


if __name__ == "__main__":
    unittest.main()
