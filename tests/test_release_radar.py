"""Release Radar window, cache, access, and request isolation."""

import json
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from fixture_environment import isolate_room_environment as setUpModule
from test_http_server import ServerFixture, server
from downloads.requests_store import RequestsStore
from metadata import release_radar


TODAY = date(2026, 11, 25)
ARTIST = "00000000-0000-4000-8000-000000000001"
GUEST = "00000000-0000-4000-8000-000000000002"


def group(number, released, kind="Album", artist=ARTIST):
    return {"id": f"00000000-0000-4000-8000-{number:012d}", "title": f"Release {number}",
            "primary-type": kind, "first-release-date": released,
            "artist-credit": [{"artist": {"id": artist, "name": "Fixture artist"}}]}


class ReleaseRadarTests(unittest.TestCase):
    def setUp(self):
        self.metadata, self.connections = Mock(), Mock()
        self.now = 10_000_000
        self.radar = release_radar.ReleaseRadar(self.metadata, self.connections, today=lambda: TODAY, now=lambda: self.now)
        self.connections.artist_match.return_value = {"id": ARTIST, "title": "Fixture artist"}
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = RequestsStore(Path(folder.name) / "room.db")
        self.catalog, self.events = Mock(), Mock()
        self.catalog.records = [{"key": "played", "artist": "Fixture artist", "title": "Known"}]
        self.catalog.recording_keys.return_value = ("played",)
        self.events.history.return_value = {"played": (10, 1)}
        self.events.recent_started.return_value = []

    def status(self, radar=None):
        return (radar or self.radar).status(self.store, self.store, "room", self.catalog, self.events)

    def test_exact_dates_and_credits_bound_deduplicated_releases(self):
        self.connections.artist_match.side_effect = [
            None, {"id": ARTIST, "title": "Fixture artist"}, {"id": ARTIST, "title": "Fixture alias"},
        ]
        self.metadata.musicbrainz.return_value = {"release-groups": [
            group(6, "2026-11-24", "EP"), group(1, "2026-09-27", "Single"), group(5, "2026-11-25"),
            group(5, "2026-11-25"), group(2, "2026-09-26"), group(3, "2026-11-26"), group(4, "2026-11"),
            group(7, "2026-11-20", "Broadcast"), group(8, "2026-11-20", artist=GUEST),
        ]}
        result = self.radar.generate(["Ambiguous", "Fixture artist", "Fixture alias"], TODAY)
        self.assertEqual([(row["title"], row["kind"]) for row in result],
                         [("Release 5", "Album"), ("Release 6", "EP"), ("Release 1", "Single")])
        self.assertEqual(result[0]["seed_artist"], "Fixture artist")
        self.metadata.musicbrainz.assert_called_once_with(
            "release-group", {"query": f"arid:{ARTIST} AND firstreleasedate:[2026-09-27 TO 2026-11-25]"
                              " AND primarytype:(album OR ep OR single)", "fmt": "json", "limit": 100})

    def test_repeated_visits_share_one_refresh_and_success_survives_restart(self):
        started, finish = threading.Event(), threading.Event()

        def search(*args):
            started.set()
            finish.wait(2)
            return {"release-groups": [group(1, "2026-11-25")]}

        self.metadata.musicbrainz.side_effect = search
        self.assertEqual(self.status()["state"], "building")
        self.assertTrue(started.wait(2))
        try:
            self.assertTrue(self.status()["refreshing"])
            self.metadata.musicbrainz.assert_called_once()
        finally:
            finish.set()
        deadline = time.monotonic() + 2
        while self.status()["refreshing"] and time.monotonic() < deadline:
            time.sleep(.01)
        restarted = release_radar.ReleaseRadar(self.metadata, self.connections, today=lambda: TODAY, now=lambda: self.now)
        self.assertEqual(self.status(restarted)["releases"][0]["title"], "Release 1")
        self.metadata.musicbrainz.assert_called_once()
        self.assertEqual(self.store.recent(), [])

    def test_outage_keeps_saved_feed_and_retries_after_backoff(self):
        key = self.radar._key("room")
        last_release = {"id": group(1, "2026-11-25")["id"], "title": "Published release", "date": "2026-11-25"}
        self.store.cache_put(key, json.dumps({"state": "ready", "releases": [last_release]}),
                             self.now - release_radar.REFRESH_SECONDS)
        self.metadata.musicbrainz.side_effect = OSError("Provider unavailable")
        self.radar._build(self.store, self.store, key, self.catalog, self.events, TODAY)
        response = self.status()
        self.assertEqual(response["state"], "stale")
        self.assertEqual(response["releases"], [last_release])
        self.assertFalse(response["refreshing"])
        self.assertIsNotNone(response["updated_at"])
        self.metadata.musicbrainz.assert_called_once()

    def test_no_history_and_no_recent_releases_are_distinct(self):
        key = self.radar._key("room")
        self.events.history.return_value = {}
        self.radar._build(self.store, self.store, key, self.catalog, self.events, TODAY)
        waiting = self.status()
        self.assertEqual(waiting["state"], "waiting")
        self.assertIsNone(waiting["updated_at"])
        self.metadata.musicbrainz.assert_not_called()
        self.events.history.return_value = {"played": (10, 1)}
        self.metadata.musicbrainz.return_value = {"release-groups": []}
        self.radar._build(self.store, self.store, key, self.catalog, self.events, TODAY)
        self.assertEqual(self.status()["state"], "empty")


class ReleaseRadarHttpTests(ServerFixture, unittest.TestCase):
    def test_room_pairing_can_read_but_cannot_submit_to_the_feed(self):
        path = release_radar.PATH
        self.assertEqual(self.request(self.lan_port, "GET", path)[0], 401)
        headers = {"Cookie": self.lan_device_cookie("radar-device"), "Origin": self.lan_origin()}
        feed = {"state": "empty", "refreshing": False, "updated_at": None, "releases": []}
        with patch.object(server.radar, "status", return_value=feed) as radar:
            status, _, value = self.request(self.lan_port, "GET", path, headers=headers)
            self.assertEqual((status, json.loads(value)), (200, feed))
            status, _, _ = self.request(self.lan_port, "POST", path, headers=headers, body={"ids": [ARTIST]})
            self.assertGreaterEqual(status, 400)
            radar.assert_called_once()
        self.assertEqual(server.requests_store().recent(), [])
