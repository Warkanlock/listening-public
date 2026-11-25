import math
from array import array
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import shutil
import sqlite3
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
                self.assertAlmostEqual(activities._estimate_bpm(activities._frame_power(pulse_track(bpm))),
                                       bpm, delta=1)
        silence = activities._frame_power(b"\0" * activities.SAMPLE_RATE * 32)
        self.assertIsNone(activities._estimate_bpm(silence))
        self.assertIsNone(activities._estimate_energy(silence))

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
                    result = activities.analyze(audio, 24)
                self.assertAlmostEqual(result.bpm, 68 if tag else 112, delta=1)
                self.assertGreater(result.energy_db, -19)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are required")
    def test_meditation_excludes_loud_audio_at_the_same_tempo(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "state.sqlite3"
            items = []
            for index, volume in enumerate(("0.3", "6"), 1):
                path = root / f"song{index}.flac"
                subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=24",
                                "-af", f"volume={volume}", "-metadata", "BPM=60", str(path)], check=True)
                item = activities.Recording(index, path.name, path, None, 24)
                items.append(item)
                with path.open("rb") as audio:
                    activities.save_analysis(database, item.identity, activities.analyze(audio, 24))
            index = activities.MixIndex(items, *activities.read_analysis(database))
            self.assertEqual(index.queue("meditation"), [1])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_backfill_keeps_saved_bpm_and_only_queues_analyzed_songs(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "state.sqlite3"
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("CREATE TABLE activity_tempos (identity TEXT NOT NULL, version INTEGER NOT NULL, "
                           "bpm REAL, PRIMARY KEY (identity, version))")
                db.execute("INSERT INTO activity_tempos VALUES ('quiet', 1, 67)")
            path = root / "quiet.flac"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=24",
                            "-metadata", "BPM=112", str(path)], check=True)
            items = [activities.Recording(1, "quiet", path, None, 24)]
            tempos, energy = activities.read_analysis(database)
            self.assertEqual(activities.MixIndex(items, tempos, energy).indexed, 0)
            with path.open("rb") as audio:
                result = activities.analyze(audio, 24, tempos["quiet"])
            activities.save_analysis(database, "quiet", result)
            self.assertEqual(result.bpm, 67)
            index = activities.MixIndex(items, *activities.read_analysis(database))
            self.assertEqual(index.indexed, 1)
            self.assertEqual(index.queue("meditation"), [1])

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
                activities.save_analysis(database, item.identity, activities.Analysis(bpm, -24))
            saved = activities.read_analysis(database)
            index = activities.MixIndex(activities.unique_recordings(items), *saved)
            self.assertEqual(index.indexed, 4)
            for name, track in [("walking", 1), ("studying", 2), ("meditation", 3)]:
                self.assertEqual(index.queue(name), [track])
            (root / "walk.mp3").write_bytes(b"new version")
            changed = list(activities.recordings(catalog.records, catalog.coverage, root, has_media=False))
            index = activities.MixIndex(activities.unique_recordings(changed), *saved)
            self.assertEqual(index.indexed, 3)
            with self.assertRaisesRegex(ValueError, "No songs"):
                index.queue("walking")
