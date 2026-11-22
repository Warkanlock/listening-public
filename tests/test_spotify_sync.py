import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import csv
import os
import tempfile
import sqlite3
from contextlib import closing
import unittest
import urllib.parse
from pathlib import Path

from catalog.collection_projection import ListProjection, documents
from catalog.collection_snapshot import create_empty_collection
from spotify_fixture import SpotifyFixture
from adapters.spotify_sync import SCOPES, SpotifyError, SpotifySync, csv_text, track_row
from library.library_protocol import canonicalize_directory, read_list, write_xspf


def item(number):
    return {"added_at": f"2026-11-{number:02d}T00:00:00Z", "track": {
        "type": "track", "uri": "spotify:track:" + str(number).zfill(22),
        "name": f"Song {number}", "artists": [{"name": "Artist"}],
        "album": {"name": "Album", "images": [{"url": "https://i.scdn.co/image/cover"}]},
        "duration_ms": 180000, "external_ids": {"isrc": "TEST123"}}}


class SpotifySyncTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.clock = [100000.0]
        self.changed = []
        self.manager = SpotifySync(self.root / "config", lambda: "a" * 32,
                                   lambda: self.root / "csv", lambda: self.changed.append(True),
                                   now=lambda: self.clock[0])
        self.manager.auth = {"access_token": "access", "refresh_token": "refresh", "expires_at": 999999}

    def test_pkce_callback_and_private_tokens(self):
        url = self.manager.begin("http://127.0.0.1:8765/api/spotify/callback")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertIn("user-library-read", query["scope"][0])
        self.assertNotIn("refresh", url)
        self.manager._token_request = lambda fields: {"access_token": "new", "refresh_token": "private", "expires_in": 3600}
        self.manager.start = lambda force=False: True
        with self.assertRaisesRegex(SpotifyError, "expired"):
            self.manager.complete({"state": ["wrong"], "code": ["code"]})
        url = self.manager.begin("http://127.0.0.1:8765/api/spotify/callback")
        state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
        self.manager.complete({"state": [state], "code": ["code"]})
        self.assertTrue(self.manager.status()["connected"])
        self.assertEqual(os.stat(self.manager.auth_path).st_mode & 0o777, 0o600)

    def test_first_sync_updates_csv_and_unchanged_sync_skips_playlist_items(self):
        calls = []
        playlist_id = "p" * 22
        version = ["v1"]
        saved = [[item(2), item(1)]]
        playlist_rows = [[item(3)]]
        def api(path, params=None):
            calls.append(path)
            if path == "/me":
                return {"id": "owner", "display_name": "Listener"}
            if path == "/me/tracks":
                return {"items": saved[0], "total": len(saved[0])}
            if path == "/me/playlists":
                return {"items": [{"id": playlist_id, "name": "New songs", "snapshot_id": version[0],
                                   "owner": {"id": "owner"}}], "total": 1}
            if path == f"/playlists/{playlist_id}/items":
                return {"items": playlist_rows[0], "total": len(playlist_rows[0])}
            raise AssertionError(path)
        self.manager._api = api
        self.manager._sync(0)
        self.assertEqual(len(self.changed), 1)
        with (self.root / "csv/spotify_liked.csv").open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["Track Name"] for row in rows], ["Song 2", "Song 1"])
        self.assertEqual(self.manager.playlist_names()[f"spotify_{playlist_id}"], "Spotify · New songs")
        calls.clear()
        self.clock[0] += 900
        self.manager._sync(0)
        self.assertNotIn(f"/playlists/{playlist_id}/items", calls)
        self.assertEqual(len(self.changed), 1)
        version[0] = "v2"
        playlist_rows[0] = [item(3), item(4)]
        calls.clear()
        self.manager._sync(0)
        self.assertIn(f"/playlists/{playlist_id}/items", calls)
        with (self.root / f"csv/spotify_{playlist_id}.csv").open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["Track Name"] for row in rows], ["Song 3", "Song 4"])
        self.assertEqual(len(self.changed), 2)
        saved[0] = [item(4), item(2), item(1)]
        self.manager._sync(0)
        with (self.root / "csv/spotify_liked.csv").open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["Track Name"], "Song 4")
        self.assertEqual(len(self.changed), 3)
        self.manager.disconnect()
        self.assertTrue((self.root / "csv/spotify_liked.csv").exists())
        self.assertFalse(self.manager.status()["connected"])
        self.assertEqual(self.manager.playlist_names()[f"spotify_{playlist_id}"], "Spotify · New songs")

    def fixture(self):
        provider = SpotifyFixture()
        self.manager.auth.update(access_token="fixture-token", scope=SCOPES)
        self.manager._request_json = provider.request
        self.manager._sync(0)
        return provider

    def edit(self, provider, op, key=None, **fields):
        return self.manager.manage({"op": op, "account_id": provider.owner,
                                    "playlist": provider.playlist if key is None else key, **fields})

    def test_add_unavailable_recording_by_metadata_reconciles_both_directions(self):
        remote = self.fixture()
        key = self.manager.management()["playlists"][1]["key"]
        self.assertEqual(self.manager.playlist_links()[key], {
            "account_id": remote.owner, "playlist_id": remote.playlist, "mode": "two_way"})
        uri = remote.uri(8)
        recording = {"name": "Vattenne", "artists": [{"name": "Peppino Gagliardi"}],
                     "album": {"name": "Collection"}, "type": "track", "uri": uri}
        def request(req):
            parsed = urllib.parse.urlsplit(req.full_url)
            if parsed.path.endswith("/search"):
                query = urllib.parse.parse_qs(parsed.query)
                self.assertEqual(query["type"], ["track"])
                self.assertEqual(query["limit"], ["10"])
                self.assertIn("artist:PEPPINO GAGLIARDI", query["q"][0])
                return {"tracks": {"items": [dict(recording, name="Vattenne (live)"), recording], "next": None}}
            return remote.request(req)
        self.manager._request_json = request
        # The mirror takes its display metadata from the same Spotify item.
        original_item = remote.item
        remote.item = lambda value: {"item": recording} if value == uri else original_item(value)
        fields = {"artist": "PEPPINO GAGLIARDI", "title": "Vattenne", "playlist_id": remote.playlist}
        self.manager.add_metadata(key, remote.owner, fields)
        remote.members[remote.playlist].append(remote.uri(9))
        remote.version += 1
        self.manager.add_metadata(key, remote.owner, fields)
        songs = self.manager.playlist_tracks(remote.owner, remote.playlist, "", 0)["tracks"]
        self.assertEqual([song["uri"] for song in songs], [remote.uri(1), uri, remote.uri(9)])
        self.assertEqual(songs[1]["title"], "Vattenne")
        self.assertEqual(len(remote.writes), 1)

    def test_ambiguous_metadata_asks_for_an_album_or_track_link_before_any_write(self):
        remote = self.fixture()
        key = "spotify_" + remote.playlist
        versions = [dict(remote.item(remote.uri(i))["item"], name="Old Song", album={"name": name})
                    for i, name in [(7, "Studio"), (8, "Live")]]
        self.manager._request_json = lambda req: ({"tracks": {"items": versions}}
                                                  if "/search?" in req.full_url else remote.request(req))
        fields = {"title": "Old Song", "artist": "Fixture Artist", "playlist_id": remote.playlist}
        with self.assertRaisesRegex(ValueError, "several recordings"):
            self.manager.add_metadata(key, remote.owner, fields)
        self.assertFalse(remote.writes)
        with self.assertRaisesRegex(ValueError, "no exact"):
            self.manager.add_metadata(key, remote.owner, {**fields, "artist": "Another artist"})
        self.assertFalse(remote.writes)
        self.manager.add_metadata(key, remote.owner, {**fields, "album": "Live"})
        self.assertEqual(remote.members[remote.playlist], [remote.uri(1), remote.uri(8)])

    def test_metadata_add_is_bound_to_current_account_playlist_and_enabled_grant(self):
        remote = self.fixture()
        key = "spotify_" + remote.playlist
        fields = {"track": remote.uri(5), "playlist_id": remote.playlist}
        for source, account, playlist in [(key, "stale-owner", remote.playlist), (key, remote.owner, "liked"),
                                         ("local", remote.owner, remote.playlist)]:
            with self.assertRaisesRegex(ValueError, "link has changed"):
                self.manager.add_metadata(source, account, {**fields, "playlist_id": playlist})
        self.manager._token_request = lambda fields: {"access_token": "fixture-token",
                                                    "scope": "user-library-read playlist-read-private"}
        self.manager._refresh()
        self.assertEqual(self.manager.playlist_links()[key]["mode"], "read_only")
        with self.assertRaisesRegex(ValueError, "read-only"):
            self.manager.add_metadata(key, remote.owner, fields)
        self.manager.auth["scope"] = SCOPES
        self.edit(remote, "unlink")
        self.assertEqual(self.manager.playlist_links()[key]["mode"], "paused")
        with self.assertRaisesRegex(ValueError, "Resume"):
            self.manager.add_metadata(key, remote.owner, fields)
        self.edit(remote, "link")
        self.manager.disconnect()
        self.assertEqual(self.manager.playlist_links()[key]["mode"], "disconnected")
        with self.assertRaisesRegex(ValueError, "Reconnect"):
            self.manager.add_metadata(key, remote.owner, fields)
        self.assertFalse(remote.writes)

    def test_collaborative_private_playlist_accepts_its_actual_write_scope(self):
        remote = self.fixture()
        details = remote.details
        remote.details = lambda key: dict(details(key), owner={"id": "other"}, collaborative=True, public=False)
        self.manager.auth["scope"] = "playlist-modify-private"
        self.manager._sync(0)
        key = "spotify_" + remote.playlist
        self.assertEqual(self.manager.playlist_links()[key]["mode"], "two_way")
        self.assertEqual(self.manager.playlist_links()["spotify_liked"]["mode"], "read_only")
        fields = {"track": remote.uri(5), "playlist_id": remote.playlist}
        self.manager.add_metadata(key, remote.owner, fields)
        self.assertEqual(remote.members[remote.playlist], [remote.uri(1), remote.uri(5)])
        # A provider-side change can arrive between viewing the link and submitting.
        remote.details = lambda key: dict(details(key), owner={"id": "other"}, collaborative=False, public=False)
        with self.assertRaisesRegex(ValueError, "owned playlist"):
            self.manager.add_metadata(key, remote.owner, dict(fields, track=remote.uri(6)))
        self.assertEqual(len(remote.writes), 1)

    def test_explicit_edits_round_trip_and_preserve_unrelated_entries(self):
        remote = self.fixture()
        self.assertFalse(remote.writes)
        remote.members[remote.playlist].append(remote.uri(3))
        remote.version += 1
        self.edit(remote, "add", track=remote.uri(2))
        self.edit(remote, "add", track=remote.uri(2))
        tracks = self.manager.playlist_tracks(remote.owner, remote.playlist, "", 0)
        self.assertEqual([t["uri"] for t in tracks["tracks"]], [remote.uri(n) for n in (1, 3, 2)])
        self.assertEqual(len(remote.writes), 1)
        self.edit(remote, "remove", track=remote.uri(2))
        self.assertEqual(remote.members[remote.playlist], [remote.uri(1), remote.uri(3)])
        self.edit(remote, "rename", name="Night shift")
        self.assertIn("Spotify · Night shift", self.manager.playlist_names().values())
        self.assertEqual(self.edit(remote, "create", "", name="From Listening")["playlist"], remote.created)
        with self.assertRaisesRegex(ValueError, "already own"):
            self.edit(remote, "create", "", name="From Listening")
        self.edit(remote, "add", "liked", track=remote.uri(1))
        self.assertEqual(self.manager.playlist_tracks(remote.owner, "liked", "", 0)["total"], 2)
        self.edit(remote, "remove", "liked", track=remote.uri(1))
        self.assertEqual(remote.saved, [remote.uri(2)])

    def test_unlinked_playlist_freezes_across_restart_others_keep_syncing(self):
        remote = self.fixture()
        self.edit(remote, "unlink")
        self.edit(remote, "unlink", "liked")
        remote.members[remote.playlist] = [remote.uri(4)]
        remote.saved = [remote.uri(6)]
        remote.names[remote.created] = "Added on Spotify"
        remote.members[remote.created] = [remote.uri(7)]
        remote.version += 1
        self.manager = SpotifySync(self.root / "config", lambda: "a" * 32, lambda: self.root / "csv", lambda: None,
                                   now=lambda: self.clock[0])
        self.manager._request_json = remote.request
        self.manager._sync(0)
        self.assertEqual(self.manager.playlist_tracks(remote.owner, remote.playlist, "", 0)["tracks"][0]["uri"], remote.uri(1))
        self.assertEqual(self.manager.playlist_tracks(remote.owner, "liked", "", 0)["tracks"][0]["uri"], remote.uri(2))
        self.assertEqual(self.manager.playlist_tracks(remote.owner, remote.created, "", 0)["total"], 1)
        with self.assertRaisesRegex(ValueError, "Link this playlist"):
            self.edit(remote, "add", track=remote.uri(5))
        self.edit(remote, "link")
        self.edit(remote, "link", "liked")
        self.assertEqual(self.manager.playlist_tracks(remote.owner, remote.playlist, "", 0)["tracks"][0]["uri"], remote.uri(4))
        self.assertEqual(self.manager.playlist_tracks(remote.owner, "liked", "", 0)["tracks"][0]["uri"], remote.uri(6))
        self.assertFalse(remote.writes)

    def test_switching_accounts_preserves_frozen_sources_for_both(self):
        remote = self.fixture()
        first = remote.owner
        for key in (remote.playlist, "liked"):
            self.edit(remote, "unlink", key)
        remote.owner = "second-owner"
        remote.saved = [remote.uri(7)]
        remote.members[remote.playlist] = [remote.uri(8)]
        self.manager._sync(0)
        second_files = {p.name for p in (self.root / "csv").glob("*.csv")}
        self.assertEqual(len(second_files), 4)
        for account, expected in [(first, (1, 2)), ("second-owner", (8, 7))]:
            remote.owner = account
            self.manager._sync(0)
            for key, song in zip((remote.playlist, "liked"), expected):
                tracks = self.manager.playlist_tracks(account, key, "", 0)["tracks"]
                self.assertEqual([t["uri"] for t in tracks], [remote.uri(song)])
        self.assertEqual({p.name for p in (self.root / "csv").glob("*.csv")}, second_files)

    def test_restored_room_relinks_without_recreating_mirrors(self):
        remote = self.fixture()
        self.edit(remote, "unlink")
        files = {p.name for p in (self.root / "csv").glob("*.csv")}
        restored = SpotifySync(self.root / "new-host", lambda: "a" * 32, lambda: self.root / "csv",
                               lambda: None, now=lambda: self.clock[0], database=self.manager.database)
        restored.auth = self.manager.auth.copy()
        self.manager = restored
        restored._request_json = remote.request
        remote.members[remote.playlist] = [remote.uri(4)]
        remote.version += 1
        restored._sync(0)
        self.assertEqual(restored.playlist_tracks(remote.owner, remote.playlist, "", 0)["tracks"][0]["uri"], remote.uri(1))
        self.edit(remote, "link")
        self.assertEqual(restored.playlist_tracks(remote.owner, remote.playlist, "", 0)["tracks"][0]["uri"], remote.uri(4))
        self.assertEqual({p.name for p in (self.root / "csv").glob("*.csv")}, files)

    def test_unavailable_playlist_cannot_relink_and_keeps_frozen_tracks(self):
        remote = self.fixture()
        self.edit(remote, "unlink")
        del remote.names[remote.playlist]
        remote.names[remote.created] = "Another playlist"
        remote.members[remote.created] = []
        with self.assertRaisesRegex(SpotifyError, "no longer available"):
            self.edit(remote, "link")
        self.manager._sync(0)
        frozen = next(p for p in self.manager.management()["playlists"] if p["id"] == remote.playlist)
        self.assertFalse(frozen["available"])
        self.assertFalse(frozen["linked"])
        self.assertEqual(self.manager.playlist_tracks(remote.owner, remote.playlist, "", 0)["total"], 1)
        self.assertFalse(remote.writes)

    def test_refresh_quota_after_mutation_respects_retry_after_and_never_replays(self):
        remote = self.fixture()
        def request(req):
            if remote.writes and "/me/tracks?" in req.full_url:
                raise SpotifyError("Quota", retry_after=3600)
            return remote.request(req)
        self.manager._request_json = request
        result = self.edit(remote, "add", track=remote.uri(3))
        self.assertIn("Change saved", result["message"])
        self.assertEqual(self.manager.state["next_sync_at"], self.clock[0] + 3600)
        self.assertFalse(self.manager.start())
        self.manager._request_json = remote.request
        self.manager._sync(0)
        self.assertEqual(len(remote.writes), 1)
        self.assertEqual(self.manager.playlist_tracks(remote.owner, remote.playlist, "", 0)["total"], 2)

    def test_stale_account_and_old_read_only_grant_never_write(self):
        remote = self.fixture()
        self.manager.auth.pop("scope")
        with self.assertRaisesRegex(ValueError, "Reconnect"):
            self.edit(remote, "add", track=remote.uri(2))
        with self.assertRaisesRegex(SpotifyError, "account"):
            self.edit(remote, "unlink", account_id="other")
        self.assertFalse(remote.writes)

    def test_track_rows_reject_non_tracks(self):
        self.assertEqual(track_row(item(1))["Track URI"], "spotify:track:" + "1".zfill(22))
        self.assertIsNone(track_row({"item": {"type": "episode", "name": "Talk"}}))

    def test_track_row_accepts_artwork_from_any_https_host(self):
        entry = item(2)
        entry["track"]["album"]["images"] = [{"url": "https://covers.example.org/a.jpg"}]
        self.assertEqual(track_row(entry)["Album Image URL"], "https://covers.example.org/a.jpg")

    def test_track_row_skips_non_https_artwork(self):
        entry = item(3)
        entry["track"]["album"]["images"] = [{"url": "http://covers.example.org/a.jpg"},
                                             {"url": "https://covers.example.org/b.jpg"}]
        self.assertEqual(track_row(entry)["Album Image URL"], "https://covers.example.org/b.jpg")
        entry["track"]["album"]["images"] = [{"url": "http://covers.example.org/a.jpg"}]
        self.assertEqual(track_row(entry)["Album Image URL"], "")

    def test_confirmed_legacy_adapter_output_retains_its_key_after_upgrade(self):
        playlist_id = "p" * 22
        key = "spotify_" + playlist_id
        folder = self.root / "csv"
        folder.mkdir()
        csv = folder / (key + ".csv")
        original = csv_text([track_row(item(1))]).encode()
        csv.write_bytes(original)
        mirror = csv.with_suffix(".xspf")
        old = write_xspf(key, read_list(csv)).encode()
        mirror.write_bytes(old)
        liked_csv = folder / "spotify_liked.csv"
        liked_csv.write_bytes(original)
        liked_xspf = liked_csv.with_suffix(".xspf")
        liked_xspf.write_text(write_xspf("spotify_liked", read_list(liked_csv)))
        self.manager.state = {"last_success": self.clock[0], "playlists": {
            playlist_id: {"name": "Owned", "snapshot": "v1"}}}
        def api(path, params=None):
            responses = {
                "/me": {"id": "owner"}, "/me/tracks": {"items": [item(1)], "total": 1},
                "/me/playlists": {"items": [{"id": playlist_id, "name": "Owned", "snapshot_id": "v1",
                                             "owner": {"id": "owner"}}], "total": 1},
                f"/playlists/{playlist_id}/items": {"items": [item(1)], "total": 1},
            }
            return responses[path]
        self.manager._api = api
        self.manager._sync(0)
        self.assertEqual(self.manager.state["playlists"][playlist_id]["key"], key)
        self.assertIn(key, self.manager.playlist_keys())
        self.assertEqual(set(folder.glob(key + "*.xspf")), {mirror})
        self.assertEqual(set(folder.glob("spotify_liked*.csv")), {liked_csv})
        self.assertEqual(set(folder.glob("spotify_liked*.xspf")), {liked_xspf})
        self.assertEqual(csv.read_bytes(), original)
        for path, data in [(csv, original), (mirror, old)]:
            backup, = (folder / ".previous").rglob(path.name)
            self.assertEqual(backup.read_bytes(), data)

    def test_sync_retires_only_unchanged_provider_sources(self):
        room = self.root / "portable"
        create_empty_collection(room)
        projection = ListProjection(room / "collection.sqlite3", self.root / "csv", self.root / "config")
        self.manager.csv_folder = lambda: projection.directory
        self.manager.on_remove = lambda path, expected: projection.remove_source(path.name, expected)
        folder = projection.directory
        folder.mkdir(parents=True)
        staying, leaving, edited, untracked = [letter * 22 for letter in "abcd"]
        ids = [staying, leaving, edited, untracked]
        # An owner can import a portable list under the very name Spotify
        # will later request. It must remain theirs throughout both syncs.
        owner = folder / f"spotify_{staying}.csv"
        owner.write_text("Title,Artist\nOwner,Artist\n")
        canonicalize_directory(folder)

        def api(path, params=None):
            if path == "/me":
                return {"id": "owner"}
            if path == "/me/tracks":
                return {"items": [], "total": 0}
            if path == "/me/playlists":
                return {"items": [{"id": key, "name": "Remote", "snapshot_id": "v1",
                                   "owner": {"id": "owner"}} for key in ids], "total": len(ids)}
            if path.startswith("/playlists/"):
                return {"items": [item(1)], "total": 1}
            raise AssertionError(path)
        self.manager._api = api
        self.manager.on_change = lambda: canonicalize_directory(folder)
        self.manager._sync(0)
        remote = folder / f"spotify_{staying}.spotify.csv"
        self.assertTrue(remote.is_file())
        self.assertIn(remote.stem, self.manager.playlist_keys())
        self.assertNotIn(owner.stem, self.manager.playlist_keys())
        self.assertEqual(read_list(owner.with_suffix(".xspf"))[0]["title"], "Owner")

        docs = documents(folder)
        with closing(sqlite3.connect(room / "collection.sqlite3")) as db, db:
            db.executemany("INSERT INTO collection_lists VALUES (?,?)", docs.items())
        projection.remember(docs)
        removed = folder / f"spotify_{leaving}.csv"
        modified = folder / f"spotify_{edited}.xspf"
        original = modified.read_bytes().replace(b"Song 1", b"Owner edit")
        modified.write_bytes(original)
        # Old states have no ownership proof for a CSV of the same name.
        self.manager.state["playlists"][untracked].pop("sha256")
        with closing(sqlite3.connect(self.manager.database)) as db, db:
            db.execute("DELETE FROM spotify_mirrors WHERE account=? AND playlist=?", ("owner", untracked))
        legacy = folder / f"spotify_{untracked}.csv"
        ids[:] = [staying]
        self.manager._sync(0)
        projection.reconcile()
        with closing(sqlite3.connect(room / "collection.sqlite3")) as db:
            names = {name for name, in db.execute("SELECT name FROM collection_lists")}
        self.assertNotIn(removed.name, names)
        self.assertNotIn(removed.with_suffix(".xspf").name, names)
        self.assertFalse(removed.exists())
        self.assertFalse(removed.with_suffix(".xspf").exists())
        self.assertEqual(modified.read_bytes(), original)
        self.assertTrue(legacy.is_file())
        self.assertTrue(owner.is_file())
        self.assertTrue(remote.is_file())
        self.assertTrue((folder / "spotify_liked.csv").is_file())

    def test_empty_playlist_response_keeps_linked_files(self):
        playlist_id = "p" * 22
        folder = self.root / "csv"
        folder.mkdir()
        playlist_file = folder / f"spotify_{playlist_id}.csv"
        playlist_file.write_text("previous playlist")
        self.manager.state = {"playlists": {playlist_id: {"name": "Kept", "snapshot": "v1"}}}
        def api(path, params=None):
            if path == "/me":
                return {"id": "owner"}
            if path == "/me/tracks":
                return {"items": [], "total": 0}
            if path == "/me/playlists":
                return {"items": [], "total": 0}
            raise AssertionError(path)
        self.manager._api = api
        with self.assertRaisesRegex(SpotifyError, "empty playlist"):
            self.manager._sync(0)
        self.assertEqual(playlist_file.read_text(), "previous playlist")
        self.assertIn(playlist_id, self.manager.state["playlists"])

    def test_incomplete_playlist_page_raises(self):
        self.manager._api = lambda path, params=None: {"items": [], "total": 10}
        with self.assertRaisesRegex(SpotifyError, "incomplete playlist"):
            self.manager._pages("/me/playlists")

    def test_quota_failure_schedules_a_later_retry(self):
        def fail(_):
            raise SpotifyError("Spotify quota reached", 3600)
        self.manager._sync = fail
        self.manager.running = True
        self.manager._run(0)
        self.assertEqual(self.manager.status()["next_sync_at"], self.clock[0] + 3600)
        self.assertFalse(self.manager.status()["running"])


class PlaylistKeysTests(unittest.TestCase):
    """Plan 046: the adapter owns exactly the playlists its sync
    state records; the liked list only once a sync has completed."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manager = SpotifySync(self.root / "config", lambda: "a" * 32,
                                   lambda: self.root / "csv", lambda: None)

    def test_no_successful_sync_owns_nothing(self):
        self.assertEqual(self.manager.playlist_keys(), set())

    def test_recorded_playlists_are_owned_without_a_success(self):
        self.manager.state = {"playlists": {"abc": {"name": "One"}}}
        self.assertEqual(self.manager.playlist_keys(), {"spotify_abc"})

    def test_liked_is_owned_once_a_sync_has_completed(self):
        self.manager.state = {"playlists": {"abc": {"name": "One"}}, "last_success": 1.0}
        self.assertEqual(self.manager.playlist_keys(), {"spotify_abc", "spotify_liked"})


if __name__ == "__main__":
    unittest.main()
