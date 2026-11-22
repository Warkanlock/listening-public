import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import hashlib
import tempfile
import unittest

from catalog.audio_store import MediaStore, blob_ref
from catalog.media_reconciliation import BlobIndex, ingest_verified_file

AUDIO_BYTES = b"fake mp3 audio bytes for plan 060 tests" * 8
OTHER_BYTES = b"a different song's bytes entirely" * 8
THIRD_BYTES = b"yet another song in the store" * 8


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def media(title="Song One", artist="Artist 1"):
    return {"duration": 200.0, "bitrate": 320000, "title": title,
            "artist": artist, "album": "Album", "genre": "Jazz"}


def coverage(index, path, status="verified"):
    return {"index": index, "status": status, "artist": "Artist 1",
            "title": "Song One", "uri": f"spotify:track:{index}",
            "path": path, "reason": ""}


def downloaded(track_index, audio_path, request_id=1):
    return {"id": request_id, "track_index": track_index,
            "audio_path": audio_path}


class MediaReconciliationTestCase(unittest.TestCase):
    """A real MediaStore in a temporary folder, seeded by putting
    fabricated files with fabricated probes."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.store = MediaStore(self.root / "media.sqlite3")

    def seed(self, data=AUDIO_BYTES, probe=None, name="seed.mp3"):
        source = self.root / name
        source.write_bytes(data)
        return self.store.put_file(source, probe or media())


class BlobIndexTests(MediaReconciliationTestCase):
    """Plan 061 step 2: the one candidacy construction, as a
    scenario matrix over coverage rows and downloaded requests."""

    def test_verified_coverage_is_a_candidate(self):
        key = self.seed()
        index = BlobIndex(self.store, [coverage(1, blob_ref(key))])
        self.assertEqual(index.get(1), (blob_ref(key), media()))
        self.assertEqual(index.references(),
                         {1: (blob_ref(key), media())})
        self.assertIsNone(index.get(2))

    def test_a_downloaded_request_bridges_a_fresh_landing(self):
        key = self.seed()
        index = BlobIndex(self.store, [],
                          [downloaded(2, blob_ref(key))])
        self.assertEqual(index.get(2), (blob_ref(key), media()))

    def test_a_stale_request_is_blocked(self):
        # The record's verified home is a plain file now; an old
        # downloaded request still names the blob, and its probe
        # exists, so only the staleness rule keeps it out.
        key = self.seed()
        rows = [coverage(1, "1 - Artist 1 - Song One.mp3")]
        index = BlobIndex(self.store, rows,
                          [downloaded(1, blob_ref(key))])
        self.assertIsNone(index.get(1))
        self.assertEqual(index.references(), {})

    def test_verified_coverage_wins_over_a_request(self):
        home = self.seed(AUDIO_BYTES, media("Song One"), "home.mp3")
        other = self.seed(OTHER_BYTES, media("Song One (old)"),
                          "other.mp3")
        rows = [coverage(1, blob_ref(home))]
        index = BlobIndex(self.store, rows,
                          [downloaded(1, blob_ref(other))])
        self.assertEqual(index.get(1),
                         (blob_ref(home), media("Song One")))

    def test_a_custom_request_is_not_a_candidate(self):
        key = self.seed()
        index = BlobIndex(
            self.store, [],
            [downloaded(None, blob_ref(key), request_id=7)])
        self.assertEqual(index.references(), {})

    def test_a_request_whose_probe_is_missing_is_not_a_candidate(self):
        ghost = blob_ref(sha256(b"bytes that never landed"))
        index = BlobIndex(self.store, [], [downloaded(3, ghost)])
        self.assertIsNone(index.get(3))
        self.assertEqual(index.references(), {})


class IngestTests(MediaReconciliationTestCase):
    """Intake commits the original bytes and recording identity together."""

    def test_ingest_retains_the_original_and_recovers_without_coverage(self):
        landed = self.root / "landed.mp3"
        landed.write_bytes(AUDIO_BYTES)
        reference = ingest_verified_file(self.store, landed, media(), owner=("catalog", "record-key"))
        key = sha256(AUDIO_BYTES)
        self.assertEqual(reference, blob_ref(key))
        self.assertEqual(landed.read_bytes(), AUDIO_BYTES)
        rebuilt = BlobIndex(self.store, [], records=[{"index": 27, "key": "record-key"}])
        self.assertEqual(rebuilt.get(27), (reference, media()))
        self.assertTrue(self.store.has(key))
        self.assertEqual(self.store.probe(key), media())
        self.assertEqual(self.store.audio_bytes(), len(AUDIO_BYTES))


if __name__ == "__main__":
    unittest.main()
