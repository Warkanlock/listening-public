import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import zlib
from unittest import mock
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from catalog import collection_preview as audit
from catalog.audio_store import SCHEMA as AUDIO_SCHEMA
from catalog.audio_resolution import resolve_stored
from catalog.collection_snapshot import _copy_legacy_audio


class PreviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.config = self.root / "room.json"
        self.config.write_text(json.dumps({"folder": str(self.root), "music_folder": str(self.library)}))
        self.state = self.root / "requests.sqlite3"
        with closing(sqlite3.connect(self.state)) as db:
            db.execute("CREATE TABLE catalog_records(idx INTEGER PRIMARY KEY, record TEXT)")
            db.execute("CREATE TABLE coverage(idx INTEGER,status TEXT,artist TEXT,title TEXT,path TEXT,reason TEXT)")
            db.execute("CREATE TABLE requests(id INTEGER,artist TEXT,title TEXT,length REAL,audio_path TEXT,status TEXT,message TEXT)")
            db.execute("INSERT INTO requests VALUES (1,'Artist','Song',10,'library/song.mp3','downloaded','')")
            db.executemany("INSERT INTO coverage VALUES (?,?,?,?,?,?)", [
                (1, "verified", "Artist", "Song", "song.mp3", ""), (2, "verified", "Artist", "Gone", "absent.mp3", ""),
                (3, "verified", "Artist", "Unknown blob", "blob:missing", ""), (4, "pending", "Artist", "Future", "", "")])
            db.executemany("INSERT INTO catalog_records VALUES (?,?)", [
                (idx, json.dumps({"key": str(idx), "artist": "Artist", "title": title, "length": 10}))
                for idx, title in enumerate(("Song", "Gone", "Unknown blob", "Future"), start=1)])
            db.commit()

    def add_blob(self, original, probe_json, compressed=False):
        key = hashlib.sha256(original).hexdigest()
        media = self.root / "media.sqlite3"
        with closing(sqlite3.connect(media)) as db:
            db.executescript(AUDIO_SCHEMA)
            db.execute("INSERT INTO audio VALUES (?,?,?,?,?,?)",
                       (key, zlib.compress(original) if compressed else original,
                        len(original), ".mp3", probe_json, 0))
            if compressed:
                db.execute("INSERT INTO audio_deflate VALUES (?)", (key,))
            db.commit()
        return key

    def test_estimate_matches_snapshot_for_raw_and_precompressed_database_audio(self):
        facts = json.dumps({"bitrate": 256000, "duration": 10, "title": "Song"})
        raw = b"music" * 740000
        self.add_blob(raw, facts)
        self.add_blob(b"stored" * 60000, facts, compressed=True)
        self.add_blob(b"short", facts)
        (self.library / "song.mp3").write_bytes(raw)
        with mock.patch.object(audit, "probe", return_value=json.loads(facts)):
            result = audit.preview(self.config, self.root / "snapshot.sqlite3", check_audio=True)
        with closing(sqlite3.connect(":memory:")) as copy:
            copy.executescript(AUDIO_SCHEMA)
            copy.execute("ATTACH DATABASE ? AS original", (str(self.root / "media.sqlite3"),))
            _copy_legacy_audio(copy, {"audio", "audio_deflate"}, self.root)
            expected = copy.execute("SELECT SUM(length(data)) FROM audio").fetchone()[0]
        self.assertEqual(result["estimated_stored_audio_bytes"], expected)
        self.assertEqual(result["distinct_audio_payloads"], 3)

    def test_portable_raw_row_keeps_its_stored_size_on_another_snapshot(self):
        raw = b"portable raw" * 20000
        self.add_blob(raw, json.dumps({"bitrate": 256000, "duration": 10, "title": "Song"}))
        (self.root / "media.sqlite3").rename(self.root / "collection.sqlite3")
        result = audit.preview(self.config, self.root / "snapshot.sqlite3", check_audio=True)
        self.assertEqual(result["estimated_stored_audio_bytes"], len(raw))

    def test_legacy_reader_without_blobopen_reads_bounded_sql_results(self):
        raw = b"a" * ((3 << 20) + 1)
        self.add_blob(raw, "{}")
        with closing(sqlite3.connect(self.root / "media.sqlite3")) as db:
            older_connection = mock.Mock(spec=["execute"])
            older_connection.execute = db.execute
            chunks = audit._legacy_chunks(older_connection, 1, len(raw))
            digest = hashlib.sha256()
            biggest = 0
            for chunk in chunks:
                digest.update(chunk)
                biggest = max(biggest, len(chunk))
        self.assertLessEqual(biggest, 1 << 20)
        self.assertEqual(digest.digest(), hashlib.sha256(raw).digest())

    def test_preview_uses_catalog_titles_and_lengths_and_reports_missing_records(self):
        valid = {"bitrate": 256000, "duration": 10, "title": "Song"}
        key = self.add_blob(b"blob", json.dumps(valid))
        (self.library / "song.mp3").write_bytes(b"file")
        with closing(sqlite3.connect(self.state)) as db:
            db.execute("UPDATE catalog_records SET record=? WHERE idx=1",
                       (json.dumps({"key": "1", "artist": "Author", "title": "Different work", "length": 10}),))
            db.execute("UPDATE catalog_records SET record=? WHERE idx=3",
                       (json.dumps({"key": "3", "artist": "Author", "title": "Song", "length": 50}),))
            db.execute("UPDATE coverage SET path=? WHERE idx=3", ("blob:" + key,))
            db.execute("DELETE FROM catalog_records WHERE idx=2")
            db.commit()
        with mock.patch.object(audit, "probe", return_value=valid):
            result = audit.preview(self.config, self.root / "snapshot.sqlite3", check_audio=True)
        issues = {e["id"]: e for e in result["tracks_requiring_attention"] if e["kind"] == "catalog"}
        self.assertEqual(issues[1]["title"], "Different work")
        self.assertIn("title tag differs", issues[1]["reason"])
        self.assertIn("duration differs", issues[3]["reason"])
        self.assertEqual(issues[2]["status"], "missing_catalog_record")
        self.assertIn(issues[2], result["snapshot_blockers"])
        basic = audit.preview(self.config, self.root / "snapshot.sqlite3")
        self.assertIn(issues[2], basic["snapshot_blockers"])

    def test_bad_stored_probes_are_reported_per_claim_and_other_tracks_complete(self):
        with closing(sqlite3.connect(self.state)) as db:
            malformed = ("not json", "[]", "{}", '{"bitrate": "256000"}',
                         '{"bitrate": 256000, "duration": 10, "title": []}',
                         json.dumps({"bitrate": 256000, "duration": 10**400, "title": "Song"}))
            for idx, payload in enumerate(malformed, start=10):
                key = self.add_blob(str(idx).encode(), payload)
                db.execute("INSERT INTO requests VALUES (?,'Artist','Song',10,?,'downloaded','')",
                           (idx, "blob:" + key))
            valid = {"bitrate": 256000, "duration": 10, "title": "Song"}
            key = self.add_blob(b"intact", json.dumps(valid))
            db.execute("INSERT INTO requests VALUES (20,'Artist','Song',10,?,'downloaded','')", ("blob:" + key,))
            db.commit()
        result = audit.preview(self.config, self.root / "snapshot.sqlite3", check_audio=True)
        invalid = {e["id"] for e in result["snapshot_blockers"] if e["status"] == "invalid_probe"}
        self.assertEqual(invalid, set(range(10, 10 + len(malformed))))
        self.assertFalse(any(e.get("id") == 20 for e in result["tracks_requiring_attention"]))
        self.assertEqual(result["distinct_audio_payloads"], len(malformed) + 1)

    def test_deep_preview_resolves_both_path_forms_and_preserves_every_input(self):
        sample = b"sample" * 100
        target = self.library / "song.mp3"
        target.write_bytes(sample)
        (self.library / "duplicate.mp3").write_bytes(sample)
        (self.library / "broken.mp3").touch()
        files = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        facts = {"title": "Song", "duration": 10, "bitrate": 256000}
        with mock.patch.object(audit, "probe", side_effect=lambda p: facts if p.stat().st_size else None):
            result = audit.preview(self.config, self.root / "out/copy.sqlite3", check_audio=True)
        self.assertEqual(result["distinct_audio_payloads"], 1)
        self.assertLess(result["estimated_stored_audio_bytes"], len(sample))
        self.assertEqual(result["missing_files"], ["absent.mp3", "blob:missing"])
        self.assertEqual(result["unreadable_files"], [str(self.library / "broken.mp3")])
        self.assertEqual({entry["status"] for entry in result["tracks_requiring_attention"]},
                         {"missing_file", "missing_blob", "pending", "unreadable"})
        self.assertEqual(files, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertFalse((self.root / "out").exists())
        self.assertEqual(resolve_stored("library/song.mp3", self.library), target)

    def test_preview_includes_committed_wal_with_no_owner_sidecar_changes(self):
        with closing(sqlite3.connect(self.state)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO coverage VALUES (5,'unmatched','Artist','No match','','')")
            writer.commit()
            watched = {p: p.read_bytes() for p in self.root.glob("requests.sqlite3*")}
            result = audit.preview(self.config, self.root / "test.sqlite3")
            self.assertEqual(result["coverage"]["unmatched"], 1)
            self.assertEqual(watched, {p: p.read_bytes() for p in self.root.glob("requests.sqlite3*")})
