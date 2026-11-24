import math
from array import array
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import shutil
import subprocess
import sys
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from catalog import activity_mixes as activities


def pulse_track(bpm):
    rate = activities.SAMPLE_RATE
    beat_length = 60 * rate / bpm
    samples = array("h", (int(15000 * math.sin(i * .22)) if i % beat_length < rate * .035 else 0
                          for i in range(rate * 24)))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


class ActivityMixTests(unittest.TestCase):
    def test_estimates_three_paces_and_declines_silence(self):
        for bpm in (54, 80, 112, 130):
            with self.subTest(bpm=bpm):
                self.assertAlmostEqual(activities.estimate_bpm(pulse_track(bpm)), bpm, delta=1)
        self.assertIsNone(activities.estimate_bpm(b"\0" * activities.SAMPLE_RATE * 32))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are required")
    def test_reads_explicit_tempo_and_decodes_untagged_audio(self):
        with TemporaryDirectory() as temp:
            source = Path(temp) / "pulse.wav"
            with wave.open(str(source), "wb") as pcm:
                pcm.setparams((1, 2, activities.SAMPLE_RATE, 0, "NONE", "not compressed"))
                pcm.writeframes(pulse_track(112))
            for tag in ([], ["-metadata", "BPM=68"]):
                path = Path(temp) / ("tagged.flac" if tag else "untagged.flac")
                subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), *tag, str(path)], check=True)
                with path.open("rb") as audio:
                    self.assertAlmostEqual(activities.analyze(audio, 24), 68 if tag else 112, delta=1)

    def test_results_persist_and_deduplicate_playable_audio(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "state.sqlite3"
            for name in ("walk", "study", "calm", "rhythmless"):
                (root / (name + ".mp3")).write_bytes(b"original")
            catalog = SimpleNamespace(records=[{} for _ in range(7)], coverage={
                1: {"status": "verified", "path": "walk.mp3"},
                2: {"status": "verified", "path": "study.mp3"},
                3: {"status": "verified", "path": "calm.mp3"},
                4: {"status": "verified", "path": "walk.mp3"},
                5: {"status": "verified", "path": "rhythmless.mp3"},
                6: {"status": "pending", "path": "study.mp3"},
                7: {"status": "verified", "path": "../missing.mp3"},
            })
            items = list(activities.recordings(catalog.records, catalog.coverage, root, has_media=False))
            for item, bpm in zip(items, [112, 84, 57, 112, None]):
                activities.save_tempo(database, item.identity, bpm)
            saved = activities.read_tempos(database)
            index = activities.MixIndex(activities.unique_recordings(items), saved)
            self.assertEqual(index.indexed, 4)
            for name, track in [("walking", 1), ("studying", 2), ("meditation", 3)]:
                self.assertEqual(index.queue(name), [track])
            (root / "walk.mp3").write_bytes(b"new version")
            changed = list(activities.recordings(catalog.records, catalog.coverage, root, has_media=False))
            index = activities.MixIndex(activities.unique_recordings(changed), saved)
            self.assertEqual(index.indexed, 3)
            with self.assertRaisesRegex(ValueError, "No songs"):
                index.queue("walking")
