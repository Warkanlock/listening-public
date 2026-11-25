import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib

from downloads.requests_store import COPY_REVIEW_MESSAGE, RequestsStore, mp3_retry


# The runner's former private DDL: a requests table and nothing else.
# A database created by that copy must open unchanged through the store.
LEGACY_RUNNER_DDL = """CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY,
        track_index INTEGER,
        artist TEXT NOT NULL,
        title TEXT NOT NULL,
        album TEXT NOT NULL DEFAULT '',
        length REAL,
        search_title TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'queued',
        message TEXT NOT NULL DEFAULT '',
        audio_path TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )"""


class StoreTestCase(unittest.TestCase):
    """A store over a fresh database in a temp state folder."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "state" / "requests.sqlite3"
        self.store = RequestsStore(self.path)

    def enqueue_track(self, index=3, title="Song Three"):
        return self.store.enqueue(index, f"Artist {index}", title, "Album", 200.0, title)

    def enqueue_custom(self, artist="Custom Artist", title="Custom Title"):
        return self.store.enqueue(None, artist, title, "", None, title)


class SchemaTests(StoreTestCase):
    def test_schema_is_created_from_empty(self):
        self.store.initialize()
        self.assertTrue(self.path.is_file())
        conn = sqlite3.connect(self.path)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertEqual(tables, {"requests", "external_cache", "request_copies"})

    def test_reopening_an_existing_database_keeps_its_rows(self):
        request_id = self.enqueue_track()
        reopened = RequestsStore(self.path)
        row = reopened.get(request_id)
        self.assertEqual(row["title"], "Song Three")
        self.assertEqual(row["status"], "queued")

    def test_a_database_created_by_the_old_runner_copy_opens_unchanged(self):
        self.path.parent.mkdir(parents=True)
        conn = sqlite3.connect(self.path)
        conn.execute(LEGACY_RUNNER_DDL)
        conn.execute("INSERT INTO requests (track_index,artist,title) VALUES (1,'A','T')")
        conn.commit()
        conn.close()
        # The store reads the old rows and gains the cache table.
        self.assertEqual(self.store.get(1)["title"], "T")
        self.assertEqual(self.store.queued(1)[0]["requested_format"], "any_lossless")
        self.store.cache_put("k", "body", 1.0)
        self.assertEqual(self.store.cache_get("k")["body"], "body")


class LifecycleTests(StoreTestCase):
    """The runner's claim/complete pattern, and the server's retry."""

    def test_enqueue_claim_finish_downloaded(self):
        request_id = self.enqueue_track()
        self.assertEqual([row["id"] for row in self.store.queued(10)], [request_id])
        self.store.claim(request_id)
        self.assertEqual(self.store.get(request_id)["status"], "searching")
        self.assertEqual(self.store.queued(10), [])
        self.store.finish(request_id, "downloaded", "Ready to play", "/audio/3 - Song.mp3")
        row = self.store.get(request_id)
        self.assertEqual(row["status"], "downloaded")
        self.assertEqual(row["message"], "Ready to play")
        self.assertEqual(row["audio_path"], "/audio/3 - Song.mp3")

    def test_enqueue_claim_finish_error_then_retry(self):
        request_id = self.enqueue_custom()
        self.store.claim(request_id)
        self.store.finish(request_id, "error", "Sockseek stopped", "")
        self.assertEqual(self.store.get(request_id)["status"], "error")
        self.store.retry(request_id, "a broader search")
        row = self.store.get(request_id)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["search_title"], "a broader search")
        self.assertEqual(row["message"], "")

    def test_worker_keeps_uncertain_matches_for_explicit_review(self):
        request_id = self.enqueue_custom()
        self.assertTrue(self.store.claim(request_id))
        recording, sources = {"title": "Custom Title"}, {"search": "one", "fetch": "one"}
        candidate = {"source": "music", "id": "private", "label": "peer folder/Song.mp3", "recommended": False}
        matches = self.store.save_copy_matches(request_id, "mp3", 0, recording, sources, [candidate], for_review=True)
        self.store.finish(request_id, "rejected", COPY_REVIEW_MESSAGE, "")
        self.assertTrue(self.store.get(request_id)["copy_review"])
        self.assertEqual(matches[0]["label"], "Song.mp3")
        self.assertNotIn("id", matches[0])
        self.assertEqual(self.store.review_matches(request_id, "mp3", 0, recording, sources), matches)
        self.assertIsNone(self.store.review_matches(request_id, "flac", 0, recording, sources))
        row, released = self.store.confirm_format(request_id, "mp3", 0, matches[0]["key"])
        self.assertTrue(released)
        self.assertNotIn("copy_review", row)
        self.assertEqual(self.store.selected_copy(request_id, 1)["candidate"], {"source": "music", "id": "private"})

    def test_queued_is_oldest_first_and_limited(self):
        ids = [self.enqueue_custom(title=f"T{i}") for i in range(3)]
        self.store.claim(ids[0])
        self.assertEqual([row["id"] for row in self.store.queued(10)], ids[1:])
        self.assertEqual([row["id"] for row in self.store.queued(1)], ids[1:2])

    def test_reset_searching_requeues_only_searching(self):
        searching = self.enqueue_track(1)
        waiting = self.enqueue_track(2)
        done = self.enqueue_track(3)
        self.store.claim(searching)
        self.store.finish(waiting, "waiting_space", "Waiting for more free space", "")
        self.store.finish(done, "downloaded", "Ready to play", "/x.mp3")
        self.store.reset_searching()
        self.assertEqual(self.store.get(searching)["status"], "queued")
        self.assertEqual(self.store.get(waiting)["status"], "waiting_space")
        self.assertEqual(self.store.get(done)["status"], "downloaded")

    def test_reset_interrupted_requeues_searching_and_waiting_space(self):
        searching = self.enqueue_track(1)
        waiting = self.enqueue_track(2)
        self.store.claim(searching)
        self.store.finish(waiting, "waiting_space", "Waiting for more free space", "")
        self.store.reset_interrupted()
        self.assertEqual(self.store.get(searching)["status"], "queued")
        self.assertEqual(self.store.get(waiting)["status"], "queued")


class FormatSelectionTests(StoreTestCase):
    def create(self, *, individual=True):
        return self.store.create_or_find(1, "Artist", "Song", "Album", 200, "Song", individual=individual)

    def test_copy_handles_are_scoped_to_the_request_revision_and_format(self):
        row, _ = self.create()
        request_id = row["id"]
        candidates = [
            {"source": "fixture", "id": "private:first", "label": "/private/first.mp3"},
            {"source": "fixture", "id": "private:second", "label": "Second", "recommended": False},
        ]
        first, second = self.store.save_copy_matches(request_id, "mp3", 0, {}, {}, candidates)
        self.assertEqual(first, {"key": first["key"], "label": "first.mp3"})
        self.assertNotIn("private", str(second))
        with self.assertRaisesRegex(ValueError, "changed or expired"):
            self.store.confirm_format(request_id, "any_format", 0, second["key"])
        queued, released = self.store.confirm_format(request_id, "mp3", 0, second["key"])
        self.assertTrue(released)
        reopened = RequestsStore(self.path)
        self.assertEqual(reopened.selected_copy(request_id, queued["format_revision"])["candidate"],
                         {"source": "fixture", "id": "private:second"})
        self.assertFalse(reopened.confirm_format(request_id, "mp3", 0, second["key"])[1])
        with self.assertRaisesRegex(ValueError, "already started"):
            reopened.confirm_format(request_id, "mp3", 0, first["key"])
        reopened.finish(request_id, "downloaded", "Ready to play", "first.mp3")
        self.assertIsNone(reopened.selected_copy(request_id, queued["format_revision"]))
        self.assertFalse(reopened.confirm_format(request_id, "mp3", 0, second["key"])[1])
        with self.assertRaisesRegex(ValueError, "already started"):
            reopened.confirm_format(request_id, "mp3", 0)

    def test_mp3_shortcut_appears_after_a_lossless_miss_and_stays_with_one_request(self):
        request, _ = self.create()
        request_id = request["id"]
        self.assertIsNone(mp3_retry(request))
        self.store.confirm_format(request_id, "alac", 0)
        self.store.finish(request_id, "error", "Search timed out", "")
        self.assertIsNone(mp3_retry(self.store.get(request_id)))
        self.store.finish(request_id, "not_found", "No copy found", "")
        self.assertEqual(mp3_retry(self.store.get(request_id)), {"id": request_id, "format_revision": 1})
        retry, released = self.store.confirm_format(request_id, "mp3", 1)
        self.assertTrue(released)
        self.assertEqual((retry["id"], retry["status"], retry["requested_format"]), (request_id, "queued", "mp3"))
        self.store.finish(request_id, "not_found", "No copy found", "")
        self.assertIsNone(mp3_retry(self.store.get(request_id)))
        self.assertFalse(self.store.confirm_format(request_id, "mp3", 1)[1])
        with self.assertRaisesRegex(ValueError, "This request changed"):
            self.store.confirm_format(request_id, "mp3", 0)

        other, _ = self.store.create_or_find(2, "Artist", "Other song", "Album", 200, "Other song", individual=True)
        self.assertEqual(other["status"], "waiting_format")
        self.assertIsNone(other["requested_format"])

    def test_concurrent_clicks_reopen_one_durable_waiting_request(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            attempts = list(pool.map(lambda _: self.create(), range(12)))
        self.assertEqual(sum(created for _, created in attempts), 1)
        ids = {row["id"] for row, _ in attempts}
        self.assertEqual(len(ids), 1)
        reopened = RequestsStore(self.path)
        reopened.reset_interrupted()
        self.assertFalse(reopened.has_queued())
        self.assertFalse(reopened.claim(ids.pop()))
        self.assertEqual(self.create(individual=False)[1], False)
        self.assertEqual(reopened.recent()[0]["status"], "waiting_format")

    def test_confirmation_is_a_single_release_and_old_replays_cannot_retry(self):
        pending, _ = self.create()
        args = pending["id"], "alac", pending["format_revision"]
        with ThreadPoolExecutor(max_workers=6) as pool:
            confirms = list(pool.map(lambda _: self.store.confirm_format(*args), range(12)))
        self.assertEqual(sum(released for _, released in confirms), 1)
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(self.store.claim, [pending["id"]] * 12)), 1)
        self.store.finish(pending["id"], "not_found", "No ALAC", "")
        self.assertFalse(self.store.confirm_format(*args)[1])
        self.assertTrue(self.store.retry(pending["id"], "Other title"))
        retry = self.store.queued(1)[0]
        self.assertEqual(retry["requested_format"], "alac")
        self.store.finish(pending["id"], "not_found", "No ALAC", "")
        with self.assertRaises(ValueError):
            self.store.confirm_format(*args)
        changed, released = self.store.confirm_format(pending["id"], "flac", retry["format_revision"])
        self.assertTrue(released)
        self.assertEqual(changed["search_title"], "Other title")
        self.assertEqual(changed["requested_format"], "flac")

    def test_failed_history_cannot_release_a_second_search_for_an_active_song(self):
        old, _ = self.create()
        self.store.confirm_format(old["id"], "alac", 0)
        self.store.finish(old["id"], "not_found", "No ALAC", "")
        current, _ = self.create()
        with self.assertRaisesRegex(ValueError, "Another request"):
            self.store.retry(old["id"], "Other title")
        with self.assertRaisesRegex(ValueError, "Another request"):
            self.store.confirm_format(old["id"], "flac", 1)
        self.assertFalse(self.store.has_queued())
        self.assertEqual(self.store.reserved_tracks(1, 5), {1})
        self.store.confirm_format(current["id"], "flac", 0)
        self.store.finish(current["id"], "not_found", "No FLAC", "")
        retried, released = self.create(individual=False)
        self.assertTrue(released)
        self.assertEqual((retried["id"], retried["requested_format"]), (current["id"], "flac"))

    def test_legacy_retry_increments_revision_without_marking_a_format_confirmed(self):
        request_id = self.enqueue_track()
        self.store.finish(request_id, "not_found", "Missing", "")
        self.store.retry(request_id, "Other title")
        row = self.store.get(request_id)
        self.assertEqual((row["requested_format"], row["format_confirmed"]), ("any_lossless", 0))
        self.assertFalse(self.store.reserved_tracks(1, 5))

    def test_waiting_request_remains_visible_and_ready_rows_are_processed_first(self):
        pending, _ = self.create()
        ready = self.enqueue_custom()
        self.assertEqual(self.store.queued(1)[0]["id"], ready)
        self.assertIn(pending["id"], [r["id"] for r in self.store.recent(limit=1)])
        self.assertFalse(self.store.retry(pending["id"], "skip format"))
        self.assertIsNone(self.store.get(pending["id"])["requested_format"])

    def test_mp3_can_be_confirmed_on_first_search_or_selected_again_after_a_miss(self):
        pending, _ = self.create()
        request_id = pending["id"]
        first, released = self.store.confirm_format(request_id, "mp3", 0)
        self.assertTrue(released)
        self.assertEqual((first["requested_format"], first["status"]), ("mp3", "queued"))
        self.assertFalse(self.store.confirm_format(request_id, "mp3", 0)[1])

        self.store.finish(request_id, "not_found", "No copy found", "")
        second, released = self.store.confirm_format(request_id, "mp3", first["format_revision"])
        self.assertTrue(released)
        self.assertEqual((second["requested_format"], second["status"]), ("mp3", "queued"))

    def test_aac_preference_survives_restarts_and_new_retries_with_a_compatible_stored_format(self):
        pending, _ = self.create()
        request_id = pending["id"]
        row, released = self.store.confirm_format(request_id, "any_lossy", 0)
        self.assertTrue(released)
        self.assertEqual(row["requested_format"], "any_lossy")
        self.assertEqual(self.store.confirm_format(request_id, "any_lossy", 0), (row, False))
        self.store.finish(request_id, "not_found", "No copy found", "")
        self.assertTrue(self.store.retry(request_id, "Broader search"))
        reopened = RequestsStore(self.path)
        self.assertEqual(reopened.queued(1)[0]["requested_format"], "any_lossy")
        self.store.finish(request_id, "downloaded", "Ready to play", "song.m4a")
        self.assertEqual(reopened.downloaded_audio()[0]["requested_format"], "any_lossy")
        with closing(sqlite3.connect(self.path)) as old_room, old_room:
            self.assertEqual(old_room.execute(
                "SELECT requested_format FROM requests WHERE id=?", (request_id,)).fetchone(), ("mp3",))
            old_room.execute("UPDATE requests SET requested_format='mp3', format_revision=format_revision+1 WHERE id=?",
                             (request_id,))
        self.assertEqual(reopened.get(request_id)["requested_format"], "mp3")


class LookupTests(StoreTestCase):
    """The server's former call patterns, through the store's operations."""

    def test_has_queued_and_active_count(self):
        self.assertFalse(self.store.has_queued())
        self.assertEqual(self.store.active_count(), 0)
        first = self.enqueue_track(1)
        self.enqueue_track(2)
        self.assertTrue(self.store.has_queued())
        self.assertEqual(self.store.active_count(), 2)
        self.store.claim(first)
        self.assertEqual(self.store.active_count(), 2)
        self.store.finish(first, "downloaded", "Ready to play", "/x.mp3")
        self.assertEqual(self.store.active_count(), 1)

    def test_two_versions_keep_independent_requests_and_retries(self):
        def request(identity, version, length):
            return self.store.create_or_find(None, "Artist", "Song", "Release", length, "Song " + version,
                                             individual=True, musicbrainz_id=identity, version=version)[0]
        studio = request("studio-id", "", 185)
        self.store.finish(studio["id"], "downloaded", "Ready", "/fixture/studio.flac")
        live = request("live-id", "Live", 242)
        self.assertNotEqual(studio["id"], live["id"])
        self.assertEqual(live["id"], request("live-id", "Live", 242)["id"])
        self.store.finish(live["id"], "not_found", "No verified copy", "")
        self.assertTrue(self.store.retry(live["id"], "Song live session"))
        queued = self.store.queued(1)[0]
        self.assertEqual((queued["musicbrainz_id"], queued["version"], queued["length"]), ("live-id", "Live", 242))
        keys = [("Artist", "Song", row["musicbrainz_id"]) for row in (studio, live)]
        matches = self.store.for_picks(keys)
        self.assertIn("audio", matches[keys[0]])
        self.assertNotIn("audio", matches[keys[1]])
        self.assertEqual(matches[keys[1]]["length"], 242)
        self.assertIsNone(self.store.active_custom("Artist", "Song"))

    def test_old_deep_dive_pick_still_has_its_play_action(self):
        track = self.enqueue_custom(artist="Shelf artist", title="Shelf pick")
        self.store.finish(track, "downloaded", "Ready", "/fixture/song.flac")
        for i in range(81):
            self.enqueue_custom(title=f"Later {i}")
        self.assertNotIn(track, [row["id"] for row in self.store.recent(80)])
        pick = ("Shelf artist", "Shelf pick")
        requests = self.store.for_picks([pick, ("Unknown", "Title")])
        self.assertEqual(list(requests), [pick])
        self.assertEqual(requests[pick]["audio"], f"/api/audio/custom/{track}")
        self.assertNotIn("audio_path", requests[pick])

    def test_recent_is_newest_first_and_limited(self):
        ids = [self.enqueue_custom(title=f"T{i}") for i in range(3)]
        self.assertEqual([row["id"] for row in self.store.recent()], list(reversed(ids)))
        self.assertEqual([row["id"] for row in self.store.recent(2)], list(reversed(ids))[:2])

    def test_by_track_maps_tracks_to_their_newest_request(self):
        self.enqueue_track(5, "First")
        newest = self.enqueue_track(5, "Second")
        self.enqueue_custom()
        by_track = self.store.by_track()
        self.assertEqual(set(by_track), {5})
        self.assertEqual(by_track[5]["id"], newest)

    def test_active_for_track_prefers_the_newest_active_row(self):
        old = self.enqueue_track(7)
        self.store.finish(old, "downloaded", "Ready to play", "/x.mp3")
        current = self.enqueue_track(7)
        self.assertEqual(self.store.active_for_track(7)["id"], current)
        self.assertTrue(self.store.has_active_for_track(7))
        self.store.finish(current, "error", "boom", "")
        self.assertIsNone(self.store.active_for_track(7))
        self.assertFalse(self.store.has_active_for_track(7))

    def test_active_custom_matches_artist_and_title(self):
        request_id = self.enqueue_custom("Some Artist", "Some Title")
        self.assertEqual(self.store.active_custom("Some Artist", "Some Title")["id"],
                         request_id)
        self.assertIsNone(self.store.active_custom("Some Artist", "Other Title"))
        self.store.finish(request_id, "error", "boom", "")
        self.assertIsNone(self.store.active_custom("Some Artist", "Some Title"))

    def test_downloaded_custom_and_custom_audio(self):
        request_id = self.enqueue_custom()
        self.assertIsNone(self.store.downloaded_custom(request_id))
        self.assertEqual(self.store.custom_audio(request_id)["status"], "queued")
        self.store.finish(request_id, "downloaded", "Ready to play", "audio/custom/1.mp3")
        row = self.store.downloaded_custom(request_id)
        self.assertEqual(row["title"], "Custom Title")
        self.assertEqual(self.store.custom_audio(request_id),
                         {"status": "downloaded", "audio_path": "audio/custom/1.mp3"})
        self.assertIsNone(self.store.custom_audio(9999))


class CacheTests(StoreTestCase):
    def test_cache_round_trip_and_replace(self):
        self.assertIsNone(self.store.cache_get("missing"))
        self.store.cache_put("search:x", '{"a": 1}', 100.0)
        self.assertEqual(self.store.cache_get("search:x"),
                         {"body": '{"a": 1}', "saved_at": 100.0})
        self.store.cache_put("search:x", '{"a": 2}', 200.0)
        self.assertEqual(self.store.cache_get("search:x")["body"], '{"a": 2}')

    def test_legacy_metadata_is_packed_on_read_without_extending_its_lifetime(self):
        body = '{"lyrics":"' + "Música 夜 " * 200 + '"}'
        with closing(self.store.connect()) as db, db:
            db.execute("INSERT INTO external_cache VALUES (?,?,?)", ("lyrics:old", body, 100))
        self.assertEqual(self.store.cache_get("lyrics:old"), {"body": body, "saved_at": 100})
        with closing(sqlite3.connect(self.path)) as db, db:
            key, packed, stamp = db.execute("SELECT * FROM external_cache").fetchone()
            self.assertNotEqual(key, "lyrics:old")
            self.assertEqual(stamp, 100)
            self.assertEqual(zlib.decompress(packed).decode(), body)
            self.assertLess(len(packed), len(body.encode()))
            db.execute("INSERT INTO external_cache VALUES (?,?,?)", ("lyrics:old", "updated during rollback", 101))
        self.assertEqual(RequestsStore(self.path).cache_get("lyrics:old")["body"], "updated during rollback")

    def test_cache_reads_again_after_damaged_payload_and_evicts_oldest(self):
        self.store.cache_put("lyrics:1", "some lyrics", 1)
        with patch("downloads.requests_store.SEARCH_MAX_ENTRIES", 2):
            for stamp in (1, 2, 3):
                self.store.cache_put(f"catalog-search:{stamp}", "a search result", stamp)
        self.assertIsNone(self.store.cache_get("catalog-search:1"))
        self.assertIsNotNone(self.store.cache_get("lyrics:1"))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE external_cache SET body=? WHERE saved_at=2", (b"truncated",))
        self.assertIsNone(self.store.cache_get("catalog-search:2"))
        self.store.cache_put("catalog-search:2", "recovered", 4)
        self.assertEqual(self.store.cache_get("catalog-search:2")["body"], "recovered")
        self.assertEqual(self.store.cache_get("catalog-search:3")["body"], "a search result")

    def test_cache_saved_at_supports_caller_side_ttl(self):
        # Callers expire entries by comparing saved_at to their own max
        # age; an old entry reads back as expired, a fresh one does not.
        now = time.time()
        self.store.cache_put("stale", "old", now - 90000)
        self.store.cache_put("fresh", "new", now)
        max_age = 86400
        self.assertGreaterEqual(now - self.store.cache_get("stale")["saved_at"], max_age)
        self.assertLess(now - self.store.cache_get("fresh")["saved_at"], max_age)


if __name__ == "__main__":
    unittest.main()
