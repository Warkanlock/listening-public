import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import json
import os
import tempfile
import unittest
from unittest import mock

from fixture_environment import isolate_room_environment as setUpModule
from runner_fixture import runner
from catalog.catalog_store import CatalogStore
from downloads.verification import judge, judge_bulk_download, judge_download, probe, title_matches
from runner import verified_existing


class BulkDownloadVerificationTests(unittest.TestCase):
    def test_mp3_still_requires_a_verified_stream_bit_rate_duration_and_title(self):
        media = {"bitrate": 192_000, "stream_bitrate": 192_000, "duration": 240.0,
                 "title": "Sample song", "codec": "mp3", "container": "mp3"}
        self.assertTrue(judge_bulk_download("Sample song", 240, media, ".MP3").ok)
        self.assertTrue(judge_download("Sample song", 240, media, ".MP3", "mp3").ok)
        self.assertFalse(judge_download("Sample song", 240, media, ".mp3").ok)
        self.assertFalse(judge_download("Sample song", 240, media, ".m4a", "mp3").ok)
        for change in ({"stream_bitrate": 128_000}, {"stream_bitrate": 0}, {"duration": 246.0}, {"title": "Other music"},
                       {"codec": "aac"}, {"container": "mov"}):
            with self.subTest(change=change):
                self.assertFalse(judge_bulk_download("Sample song", 240, {**media, **change}, ".mp3").ok)
                self.assertFalse(judge_download("Sample song", 240, {**media, **change}, ".mp3", "mp3").ok)

    def test_lossy_requests_accept_mp3_and_aac_with_a_verified_audio_bitrate(self):
        media = {"bitrate": 300_000, "stream_bitrate": 200_000, "duration": 240.0,
                 "title": "Sample song", "codec": "aac", "container": "mov,mp4,m4a"}
        self.assertTrue(judge_download("Sample song", 240, media, ".M4A", "any_lossy").ok)
        mp3 = {**media, "codec": "mp3", "container": "mp3"}
        self.assertTrue(judge_download("Sample song", 240, mp3, ".mp3", "any_lossy").ok)
        self.assertFalse(judge_download("Sample song", 240, media, ".m4a", "mp3").ok)
        self.assertFalse(judge_download("Sample song", 240, media, ".m4a", "any_lossless").ok)
        for change in ({"codec": "alac"}, {"container": "aac"}, {"stream_bitrate": 191_999},
                       {"duration": 245.6}, {"title": "Different recording"}, {"title": "Track 7"}, {"title": ""}):
            with self.subTest(change=change):
                self.assertFalse(judge_download("Sample song", 240, {**media, **change}, ".m4a", "any_lossy").ok)


class DiscoveryCopyTests(unittest.TestCase):
    def test_title_words_alone_are_not_version_evidence(self):
        media = {"bitrate": 800000, "duration": 242, "title": "Live Forever", "artist": "Singer",
                 "album": "Live Forever", "codec": "flac", "container": "flac"}
        def accepted(version, **tags):
            return judge_download("Live Forever", 242, {**media, **tags}, ".flac", recording_id="id",
                                  expected_artist="Singer", version=version).ok
        self.assertFalse(accepted("Live"))
        self.assertFalse(accepted("", subtitle="Remix"))
        self.assertFalse(accepted("Live", subtitle="Studio", album="Live"))
        self.assertTrue(accepted("Live", title="Live Forever (Live)"))

    def test_same_artist_live_take_requires_its_performance_evidence(self):
        facts = {"title": "Flood", "artist": "Singer", "version": "Live", "duration": 185,
                 "bitrate": 600000, "codec": "flac", "container": "flac"}
        def check(album):
            return judge_download("Flood", 183, {**facts, "album": album}, ".flac", expected_artist="Singer",
                                  recording_id="id", version="Live", performance="Paris · 1994-06-08").ok
        self.assertFalse(check("Paris 1994-06-09"))
        self.assertTrue(check("Paris 1994-06-08"))

    def test_version_evidence_is_additional_to_audio_verification(self):
        media = {"bitrate": 800000, "duration": 242, "title": "Song", "artist": "The Trio",
                 "codec": "flac", "container": "flac", "album": "Studio"}
        def accepted(**changes):
            return judge_download("Song", 242, {**media, **changes}, ".flac",
                                  recording_id="live-id", version="Live in Paris", expected_artist="The Trio").ok
        self.assertFalse(accepted())
        self.assertTrue(accepted(album="Live in Paris"))
        self.assertTrue(accepted(title="Song (Live in Paris)"))
        self.assertFalse(accepted(album="Live in Paris", artist="Cover Band"))
        self.assertFalse(accepted(album="Live in Paris", title="Song of Winter"))
        self.assertFalse(accepted(musicbrainz_trackid="studio-id", title="Song (Live in Paris)"))
        self.assertTrue(accepted(musicbrainz_trackid="live-id"))
        self.assertFalse(accepted(musicbrainz_trackid="live-id", duration=200))
        self.assertFalse(accepted(musicbrainz_trackid="live-id", title=""))
        self.assertFalse(accepted(musicbrainz_trackid="live-id", bitrate=128000))


class TitleMatchesGenericTests(unittest.TestCase):
    """A6 test 5/8: generic and empty tags are accepted (A2 C-06)."""

    def test_generic_placeholder_tags_match_a_real_title(self):
        self.assertTrue(title_matches("Bohemian Rhapsody", "Track 01"))
        self.assertTrue(title_matches("Bohemian Rhapsody", "Traccia 2"))
        self.assertTrue(title_matches("Bohemian Rhapsody", "Pista 12"))
        self.assertTrue(title_matches("Bohemian Rhapsody", "Audio 3"))

    def test_empty_tag_matches_a_real_title(self):
        self.assertTrue(title_matches("Bohemian Rhapsody", ""))
        self.assertTrue(title_matches("Bohemian Rhapsody", None))

    def test_unrelated_tag_does_not_match(self):
        self.assertFalse(title_matches("Bohemian Rhapsody", "Stairway to Heaven"))


class TitleMatchesThresholdTests(unittest.TestCase):
    """A6 test 9: normalization, and the 0.55 similarity boundary.

    The boundary pairs are frozen literals over normalized tokens
    (lowercase letters only, so normalization is the identity and no
    side contains the other). Their SequenceMatcher ratios are pinned
    in the comments: a threshold change must fail these tests loudly.
    """

    def test_accents_case_and_punctuation_match(self):
        self.assertTrue(title_matches("Café del Mar", "cafe del mar"))
        self.assertTrue(title_matches("Beyoncé", "BEYONCE"))
        self.assertTrue(title_matches("Song_Name (Live!)", "song name live"))

    def test_pair_just_above_the_threshold_matches(self):
        # ratio("abcdefghijklmnop", "abcdefghiXXXXXXX") == 0.5625
        self.assertTrue(title_matches("abcdefghijklmnop", "abcdefghiXXXXXXX"))
        # ratio("abcdefghij", "abcdefwxyz") == 0.6
        self.assertTrue(title_matches("abcdefghij", "abcdefwxyz"))

    def test_pair_just_below_the_threshold_does_not_match(self):
        # ratio("abcdefghij", "abcde12345") == 0.5
        self.assertFalse(title_matches("abcdefghij", "abcde12345"))


class ProbeTests(unittest.TestCase):
    """A6 test 10: probe() against a fake ffprobe on PATH."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.media_file = self.root / "song.mp3"
        self.media_file.write_bytes(b"\0" * 5_000_000)

    def install_fake_ffprobe(self, payload=None, fail=False):
        bindir = self.root / "bin"
        bindir.mkdir(exist_ok=True)
        script = bindir / "ffprobe"
        if fail:
            script.write_text("#!/bin/sh\nexit 1\n")
        else:
            payload_path = self.root / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            script.write_text(f"#!/bin/sh\ncat {payload_path}\n")
        script.chmod(0o755)
        patcher = mock.patch.dict(os.environ,
                                  {"PATH": str(bindir) + os.pathsep + os.environ["PATH"]})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_non_audio_stream_is_rejected(self):
        self.install_fake_ffprobe({"streams": [{"codec_type": "video"}],
                                   "format": {"duration": "100.0", "bit_rate": "320000",
                                              "tags": {"title": "A Video"}}})
        self.assertIsNone(probe(self.media_file))

    def test_ffprobe_failure_is_rejected(self):
        self.install_fake_ffprobe(fail=True)
        self.assertIsNone(probe(self.media_file))

    def test_bitrate_and_tags_fall_back_to_size_and_stream_tags(self):
        self.install_fake_ffprobe(
            {"streams": [{"codec_type": "audio", "tags": {"title": "Stream Title"}}],
             "format": {"duration": "200.0"}})
        media = probe(self.media_file)
        self.assertEqual(media["duration"], 200.0)
        # No format bit_rate: 5,000,000 bytes * 8 / 200 s = 200,000 bps.
        self.assertEqual(media["bitrate"], 200_000)
        self.assertEqual(media["title"], "Stream Title")

    def test_lossless_title_is_found_in_uppercase_stream_tags(self):
        self.install_fake_ffprobe({
            "streams": [{"codec_type": "audio", "codec_name": "flac", "tags": {"TITLE": "Other Track"}}],
            "format": {"format_name": "flac", "duration": "10", "bit_rate": "900000",
                       "tags": {"ENCODER": "fixture"}}})
        media = probe(self.media_file)
        self.assertEqual(media["title"], "Other Track")
        self.assertFalse(judge("Some Song", 10, media).ok)

    def test_format_values_are_used_when_present(self):
        self.install_fake_ffprobe(
            {"streams": [{"codec_type": "audio", "bit_rate": "256000"}],
             "format": {"duration": "180.5", "bit_rate": "320000",
                        "tags": {"title": "Real Title", "artist": "Real Artist",
                                 "album": "Real Album", "genre": "Detroit Techno"}}})
        self.assertEqual(probe(self.media_file),
                         {"duration": 180.5, "bitrate": 320000, "stream_bitrate": 256000, "title": "Real Title",
                          "artist": "Real Artist", "album": "Real Album",
                          "genre": "Detroit Techno",
                          "codec": "", "container": "", "sample_rate": 0, "bit_depth": None,
                          "track_gain": None, "album_gain": None,
                          "track_peak": None, "album_peak": None,
                          "r128_track_gain": None})

    def test_bulk_mp3_does_not_count_cover_art_toward_the_audio_bitrate(self):
        self.install_fake_ffprobe({
            "streams": [{"codec_type": "audio", "codec_name": "mp3", "bit_rate": "128000"},
                        {"codec_type": "video", "codec_name": "mjpeg"}],
            "format": {"format_name": "mp3", "duration": "5", "bit_rate": "552000",
                       "tags": {"title": "Cover fixture"}},
        })
        media = probe(self.media_file)
        self.assertEqual(media["bitrate"], 552_000)
        self.assertEqual(media["stream_bitrate"], 128_000)
        verdict = judge_bulk_download("Cover fixture", 5, media, ".mp3")
        self.assertFalse(verdict.ok)
        self.assertIn("128000", verdict.reason)

    def test_quality_and_gain_fields_are_captured(self):
        # Plan 042 stage 6: the descriptive fields ride the same
        # probe. Tag keys arrive in the file's own case (Vorbis
        # comments shout the ReplayGain names) and gain values
        # carry their " dB" suffix; the R128 tag is the Q7.8
        # fixed-point integer the R128 tools write.
        self.install_fake_ffprobe(
            {"streams": [{"codec_type": "audio", "codec_name": "flac",
                          "sample_rate": "96000", "bits_per_raw_sample": 24}],
             "format": {"duration": "180.5", "bit_rate": "900000",
                        "tags": {"title": "Real Title",
                                 "REPLAYGAIN_TRACK_GAIN": "-7.89 dB",
                                 "REPLAYGAIN_ALBUM_GAIN": "-8.50 dB",
                                 "REPLAYGAIN_TRACK_PEAK": "0.987654",
                                 "REPLAYGAIN_ALBUM_PEAK": "0.999000",
                                 "R128_TRACK_GAIN": "-821"}}})
        media = probe(self.media_file)
        self.assertEqual(media["codec"], "flac")
        self.assertEqual(media["sample_rate"], 96000)
        self.assertEqual(media["bit_depth"], 24)
        self.assertEqual(media["track_gain"], -7.89)
        self.assertEqual(media["album_gain"], -8.50)
        self.assertEqual(media["track_peak"], 0.987654)
        self.assertEqual(media["album_peak"], 0.999)
        self.assertEqual(media["r128_track_gain"], -821 / 256)

    def test_gain_tags_are_read_from_stream_tags(self):
        self.install_fake_ffprobe(
            {"streams": [{"codec_type": "audio", "codec_name": "mp3",
                          "sample_rate": "44100",
                          "tags": {"replaygain_track_gain": "-3.25 dB"}}],
             "format": {"duration": "180.5", "bit_rate": "320000",
                        "tags": {"title": "Stream Tagged"}}})
        media = probe(self.media_file)
        self.assertEqual(media["codec"], "mp3")
        self.assertEqual(media["sample_rate"], 44100)
        self.assertEqual(media["track_gain"], -3.25)
        self.assertIsNone(media["bit_depth"])

    def test_descriptive_fields_default_when_absent(self):
        self.install_fake_ffprobe(
            {"streams": [{"codec_type": "audio"}],
             "format": {"duration": "200.0", "bit_rate": "320000",
                        "tags": {"title": "Untagged"}}})
        media = probe(self.media_file)
        self.assertEqual(media["codec"], "")
        self.assertEqual(media["sample_rate"], 0)
        self.assertIsNone(media["bit_depth"])
        self.assertIsNone(media["track_gain"])
        self.assertIsNone(media["album_gain"])
        self.assertIsNone(media["track_peak"])
        self.assertIsNone(media["album_peak"])
        self.assertIsNone(media["r128_track_gain"])

    def test_artist_and_album_default_to_empty(self):
        self.install_fake_ffprobe(
            {"streams": [{"codec_type": "audio"}],
             "format": {"duration": "200.0", "bit_rate": "320000",
                        "tags": {"title": "Untagged"}}})
        media = probe(self.media_file)
        self.assertEqual(media["artist"], "")
        self.assertEqual(media["album"], "")

    def test_genre_tag_defaults_to_empty(self):
        self.install_fake_ffprobe(
            {"streams": [{"codec_type": "audio"}],
             "format": {"duration": "200.0", "bit_rate": "320000",
                        "tags": {"title": "Untagged"}}})
        self.assertEqual(probe(self.media_file)["genre"], "")


class VerifiedExistingTests(unittest.TestCase):
    """A6 test 12: both lookup paths apply bitrate, duration, and title."""

    GOOD = {"duration": 200.0, "bitrate": 320_000, "title": "My Song"}

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.library = self.root / "audio"
        self.library.mkdir()
        root = mock.patch.object(runner, "ROOT", self.root)
        root.start()
        self.addCleanup(root.stop)
        self.record = {"title": "My Song", "length": 200.0}
        runner._coverage_paths.cache_clear()
        self.addCleanup(runner._coverage_paths.cache_clear)

    def run_with_media(self, media):
        with mock.patch.object(runner, "LIBRARY", self.library), \
                mock.patch.object(runner, "REQUESTS_DB", self.root / "requests.sqlite3"), \
                mock.patch.object(runner, "probe", lambda path: media):
            return verified_existing(7, self.record)

    def library_candidate(self):
        path = self.library / "7 - My Song.mp3"
        path.write_bytes(b"audio")
        return path

    def test_library_path_accepts_a_good_copy(self):
        candidate = self.library_candidate()
        self.assertEqual(self.run_with_media(dict(self.GOOD)), candidate)

    def test_library_path_rejects_low_bitrate(self):
        self.library_candidate()
        self.assertIsNone(self.run_with_media({**self.GOOD, "bitrate": 128_000}))

    def test_library_path_rejects_wrong_duration(self):
        self.library_candidate()
        self.assertIsNone(self.run_with_media({**self.GOOD, "duration": 220.0}))

    def test_library_path_rejects_wrong_title(self):
        self.library_candidate()
        self.assertIsNone(self.run_with_media({**self.GOOD, "title": "Another Song Entirely"}))

    def test_library_path_rejects_unreadable_audio(self):
        self.library_candidate()
        self.assertIsNone(self.run_with_media(None))

    def coverage_candidate(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        candidate = elsewhere / "shared.mp3"
        candidate.write_bytes(b"audio")
        database = self.root / "requests.sqlite3"
        CatalogStore(database).write_coverage(
            [{"index": 7, "status": "verified", "artist": "", "title": "",
              "uri": "", "path": str(candidate), "reason": ""}])
        return candidate, database

    def run_with_coverage(self, media):
        candidate, database = self.coverage_candidate()
        with mock.patch.object(runner, "LIBRARY", self.library), \
                mock.patch.object(runner, "REQUESTS_DB", database), \
                mock.patch.object(runner, "probe", lambda path: media):
            return verified_existing(7, self.record), candidate

    def test_coverage_path_accepts_a_good_copy(self):
        result, candidate = self.run_with_coverage(dict(self.GOOD))
        self.assertEqual(result, candidate.resolve())

    def test_coverage_path_rejects_low_bitrate(self):
        result, _ = self.run_with_coverage({**self.GOOD, "bitrate": 128_000})
        self.assertIsNone(result)

    def test_coverage_path_rejects_wrong_duration(self):
        result, _ = self.run_with_coverage({**self.GOOD, "duration": 220.0})
        self.assertIsNone(result)

    def test_coverage_path_rejects_wrong_title(self):
        result, _ = self.run_with_coverage({**self.GOOD, "title": "Another Song Entirely"})
        self.assertIsNone(result)


class JudgeTests(unittest.TestCase):
    """judge() owns the verdict and the coverage reason vocabulary."""

    GOOD = {"duration": 200.0, "bitrate": 320_000, "title": "My Song"}

    def test_good_copy_is_accepted_without_a_reason(self):
        verdict = judge("My Song", 200.0, dict(self.GOOD))
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.reason, "")

    def test_generic_tag_is_accepted_and_recorded(self):
        verdict = judge("My Song", 200.0, {**self.GOOD, "title": "Track 01"})
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.reason, "title tag generic")

    def test_empty_tag_is_accepted_and_recorded(self):
        verdict = judge("My Song", 200.0, {**self.GOOD, "title": ""})
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.reason, "title tag generic")

    def test_unreadable_audio_is_rejected(self):
        verdict = judge("My Song", 200.0, None)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, "unreadable audio")

    def test_low_bitrate_is_rejected_with_its_value(self):
        verdict = judge("My Song", 200.0, {**self.GOOD, "bitrate": 128_000})
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, "low bitrate 128000")

    def test_wrong_duration_is_rejected_with_the_difference(self):
        verdict = judge("My Song", 200.0, {**self.GOOD, "duration": 220.0})
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, "duration differs by 20.0s")

    def test_wrong_title_is_rejected_with_the_tag(self):
        verdict = judge("My Song", 200.0, {**self.GOOD, "title": "Another Song Entirely"})
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, "title tag differs: Another Song Entirely")

    def test_unknown_length_skips_the_duration_check(self):
        verdict = judge("My Song", "", {**self.GOOD, "duration": 999.0})
        self.assertTrue(verdict.ok)


if __name__ == "__main__":
    unittest.main()
