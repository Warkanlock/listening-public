"""Radio provider opt-in, station normalization and existing-room continuity."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from fixture_environment import isolate_room_environment as setUpModule
import server
from adapters import youtube_search
from playback.radio import clean_stations, migrate_providers, stations_from, video_id_from
from system.config import AppConfig


# ---------------------------------------------------------------------------
# The module
# ---------------------------------------------------------------------------

class VideoIdFromTests(unittest.TestCase):
    def test_bare_ids_and_every_url_shape_normalize(self):
        video = "vugodYOKeis"
        for text in (
            video,
            f"  {video}  ",
            f"https://www.youtube.com/watch?v={video}",
            f"https://www.youtube.com/watch?v={video}&list=RD{video}&start_radio=1",
            f"https://youtube.com/watch?v={video}",
            f"https://m.youtube.com/watch?v={video}",
            f"https://music.youtube.com/watch?v={video}",
            f"https://youtu.be/{video}",
            f"https://youtu.be/{video}?si=abc123",
            f"https://www.youtube.com/shorts/{video}",
            f"https://www.youtube.com/live/{video}",
            f"https://www.youtube.com/embed/{video}",
            f"https://www.youtube-nocookie.com/embed/{video}",
        ):
            self.assertEqual(video_id_from(text), video, text)

    def test_anything_else_names_no_video(self):
        for text in (
            "",
            "not a link",
            "vugodYOKeis!",                    # not an ID, not a URL
            "vugodYO",                         # too short for an ID
            "https://example.com/watch?v=vugodYOKeis",
            "https://www.youtube.com/playlist?list=RDvugodYOKeis",
            "https://www.youtube.com/@SomeChannel",
            "https://www.youtube.com/watch",   # no v parameter
            "https://www.youtube.com/watch?v=short",
            "https://youtu.be/",               # no ID segment
            "https://youtu.be/vugodYOKeis/extra",
            None,
            42,
        ):
            self.assertIsNone(video_id_from(text), repr(text))


class CleanStationsTests(unittest.TestCase):
    def test_url_seeds_normalize_and_duplicates_collapse(self):
        stations = clean_stations([
            {"name": "  Lo-fi  ", "seeds": [
                "https://www.youtube.com/watch?v=vugodYOKeis",
                "vugodYOKeis",
                "https://youtu.be/jfKfPfyJRdk",
            ]},
            {"name": "Quiet", "seeds": []},
        ])
        self.assertEqual(stations, [
            {"name": "Lo-fi", "seeds": ["vugodYOKeis", "jfKfPfyJRdk"]},
            {"name": "Quiet", "seeds": []},
        ])

    def test_refusals(self):
        for value, message in (
            ({"name": "Lo-fi"}, "Stations must be a list"),
            ([{"seeds": []}], "Each station needs a name"),
            ([{"name": "A", "seeds": []}, {"name": "a", "seeds": []}],
             "Two stations are named a"),
            ([{"name": "A", "seeds": "vugodYOKeis"}],
             "A's seeds must be a list"),
            ([{"name": "A", "seeds": ["https://example.com/x"]}],
             "That link does not name a YouTube video"),
        ):
            with self.assertRaises(ValueError) as caught:
                clean_stations(value)
            self.assertEqual(str(caught.exception), message, value)


class StationsFromTests(unittest.TestCase):
    def test_reading_absent_stations_does_not_write_defaults(self):
        settings = {}
        self.assertEqual(stations_from(settings), [])
        self.assertEqual(settings, {})

    def test_migration_is_repeatable_and_keeps_station_order(self):
        mixes = [{"name": "Mine", "seeds": ["vugodYOKeis", "dqnnTbt99Q4"]}]
        previous = {"radio_stations": mixes}
        migrate_providers(previous, previous_room=True)
        self.assertEqual(previous["radio_providers"], ["somafm", "youtube"])
        previous["radio_providers"] = []
        migrate_providers(previous, previous_room=True)
        self.assertEqual(previous, {"radio_providers": [], "radio_stations": mixes})
        with tempfile.TemporaryDirectory() as tmp:
            fresh = AppConfig(Path(tmp) / "settings.json", Path(tmp)).load()
        self.assertEqual(fresh["radio_providers"], [])
        self.assertNotIn("radio_stations", fresh)

    def test_an_empty_stored_list_stays_empty(self):
        settings = {"radio_stations": []}
        self.assertEqual(stations_from(settings), [])

    def test_a_corrupt_stored_value_reads_as_no_stations(self):
        settings = {"radio_stations": [{"name": "A", "seeds": ["junk"]}]}
        self.assertEqual(stations_from(settings), [])


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------

LAN_IP = "192.0.2.44"  # TEST-NET-1 (RFC 5737), as in test_http_server.


class PresentedClientServer(ThreadingHTTPServer):
    presented_client = None

    def get_request(self):
        request, address = super().get_request()
        if self.presented_client:
            return request, (self.presented_client, address[1])
        return request, address


class RadioHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.workspace = base / "room"
        config_dir = base / "config"
        config_dir.mkdir(parents=True)
        cls.config_path = config_dir / "config.json"
        cls.config_path.write_text(json.dumps({"folder": str(cls.workspace)}),
                                   encoding="utf-8")
        server.init(cls.config_path)
        cls.token = server.SETTINGS["share_token"]
        cls.loop_server = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.lan_server = PresentedClientServer(("127.0.0.1", 0), server.Handler)
        cls.lan_server.presented_client = LAN_IP
        cls.loop_port = cls.loop_server.server_address[1]
        cls.lan_port = cls.lan_server.server_address[1]
        for instance in (cls.loop_server, cls.lan_server):
            threading.Thread(target=instance.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for instance in (cls.loop_server, cls.lan_server):
            instance.shutdown()
            instance.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        # Every test starts from a room that has never seen stations.
        server.SETTINGS.pop("radio_stations", None)
        server.SETTINGS.pop("somafm_stations", None)
        server.SETTINGS["radio_providers"] = ["somafm", "youtube"]
        server.save_settings()

    def request(self, port, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        all_headers = dict(headers or {})
        if body is not None:
            all_headers.setdefault("Content-Type", "application/json")
        connection.request(method, path,
                           body=json.dumps(body) if body is not None else None,
                           headers=all_headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        connection.close()
        return result

    def lan_origin(self):
        return f"http://127.0.0.1:{self.lan_port}"

    def lan_device_cookie(self, device_id="radio-device-01", name="Radio device"):
        status, headers, _ = self.request(self.lan_port, "GET",
                                          f"/?token={self.token}")
        self.assertEqual(status, 303)
        bootstrap = headers["Set-Cookie"].split(";", 1)[0]
        status, headers, _ = self.request(
            self.lan_port, "POST", "/api/device",
            body={"id": device_id, "name": name},
            headers={"Cookie": bootstrap, "Origin": self.lan_origin()})
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_modules_are_independent_and_disable_preserves_saved_mixes(self):
        server.SETTINGS["radio_providers"] = []
        body = {"action": "radio", "provider": "youtube", "enabled": True}
        status, _, data = self.request(self.loop_port, "GET", "/api/stations")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["stations"], [])
        self.assertEqual(json.loads(data)["live"], {})
        self.assertNotIn("radio_stations", json.loads(self.config_path.read_text()))
        with mock.patch.object(youtube_search, "find") as search:
            status, _, _ = self.request(self.loop_port, "GET", "/api/radio/search?station=Lo-fi")
            self.assertEqual(status, 403)
            search.assert_not_called()
        cookie = self.lan_device_cookie()
        status, _, _ = self.request(self.lan_port, "POST", "/api/extensions", body,
                                    {"Cookie": cookie, "Origin": self.lan_origin()})
        self.assertEqual(status, 403)
        self.assertEqual(self.request(self.loop_port, "POST", "/api/extensions", body)[0], 200)
        mixes = [{"name": "Mine", "seeds": ["vugodYOKeis", "dqnnTbt99Q4"]}]
        self.assertEqual(self.request(self.loop_port, "POST", "/api/stations", {"stations": mixes})[0], 200)
        self.request(self.loop_port, "POST", "/api/extensions", {**body, "enabled": False})
        _, _, data = self.request(self.loop_port, "GET", "/api/stations")
        self.assertEqual(json.loads(data)["stations"], [])
        self.request(self.loop_port, "POST", "/api/extensions", body)
        _, _, data = self.request(self.loop_port, "GET", "/api/stations")
        self.assertEqual(json.loads(data)["stations"], mixes)
        self.assertEqual(json.loads(data)["live"], {})

    def test_owner_can_edit_somafm_channels_without_changing_video_mixes(self):
        path = "/api/radio/somafm"
        _, _, data = self.request(self.loop_port, "GET", path)
        config = json.loads(data)
        self.assertIn({"name": "Study", "channel": "groovesalad"}, config["stations"])
        self.assertEqual(config["channels"]["bossa"], "Bossa Beyond")
        self.assertNotIn("somafm_stations", json.loads(self.config_path.read_text()))

        cookie = self.lan_device_cookie()
        headers = {"Cookie": cookie, "Origin": self.lan_origin()}
        edited = {"enabled": True, "stations": [{"name": "Study", "channel": "lush"},
                                               {"name": "Bossa Beyond", "channel": "bossa"}]}
        self.assertEqual(self.request(self.lan_port, "GET", path, headers=headers)[0], 403)
        self.assertEqual(self.request(self.lan_port, "POST", path, edited, headers)[0], 403)
        invalid = {**edited, "stations": [{"name": "Custom URL", "channel": "https://example.test/"}]}
        self.assertEqual(self.request(self.loop_port, "POST", path, invalid)[0], 400)
        previous = self.config_path.read_bytes()
        with mock.patch.object(server, "save_settings", side_effect=OSError("disk full")):
            self.assertEqual(self.request(self.loop_port, "POST", path, edited)[0], 503)
        self.assertEqual(self.config_path.read_bytes(), previous)
        _, _, data = self.request(self.loop_port, "GET", path)
        self.assertEqual(json.loads(data), config)
        self.assertEqual(self.request(self.loop_port, "POST", path, edited)[0], 200)
        self.assertEqual(json.loads(self.config_path.read_text())["somafm_stations"], edited["stations"])
        _, _, data = self.request(self.loop_port, "GET", "/api/stations")
        self.assertEqual(json.loads(data)["live"], {
            "Study": {"title": "Lush", "url": "https://ice2.somafm.com/lush-128-mp3"},
            "Bossa Beyond": {"title": "Bossa Beyond", "url": "https://ice2.somafm.com/bossa-128-mp3"},
        })
        self.assertTrue(json.loads(data)["youtube_enabled"])

        self.assertEqual(self.request(self.loop_port, "POST", path, {**edited, "enabled": False})[0], 200)
        _, _, data = self.request(self.loop_port, "GET", "/api/stations")
        self.assertEqual(json.loads(data)["live"], {})
        _, _, data = self.request(self.loop_port, "GET", path)
        self.assertEqual(json.loads(data)["stations"], edited["stations"])
        self.assertEqual(self.request(self.loop_port, "POST", path, {"stations": config["stations"]})[0], 200)
        _, _, data = self.request(self.loop_port, "GET", path)
        self.assertFalse(json.loads(data)["enabled"])
        self.assertEqual(json.loads(data)["stations"], config["stations"])

    def test_live_presence_rejects_a_station_disabled_after_it_was_listed(self):
        cookie = self.lan_device_cookie()
        headers = {"Cookie": cookie, "Origin": self.lan_origin()}
        output = {"output": "fixture-output-id", "source": "live"}
        status, _, data = self.request(self.lan_port, "GET", "/api/stations", headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("Study", json.loads(data)["live"])
        self.assertEqual(self.request(self.lan_port, "POST", "/api/radio/presence", output, headers)[0], 200)
        self.assertEqual(self.request(self.loop_port, "POST", "/api/extensions",
                                     {"action": "radio", "provider": "somafm", "enabled": False})[0], 200)
        self.assertEqual(self.request(self.lan_port, "POST", "/api/radio/presence", output, headers)[0], 403)

    def test_genre_search_uses_allowed_station_names_and_keeps_the_key_on_the_server(self):
        response = {"items": [
            {"id": {"videoId": "vugodYOKeis"}}, {"id": {"videoId": "not a video"}},
            {"id": {"videoId": "vugodYOKeis"}},
        ]}
        with (
            mock.patch.dict(os.environ, {"LISTENING_YOUTUBE_API_KEY": "disposable-video-key"}),
            mock.patch.object(youtube_search, "read_json", return_value=response) as fetch,
        ):
            route = "/api/radio/search?station="
            self.assertEqual(self.request(self.loop_port, "GET", route + "Anything")[0], 400)
            fetch.assert_not_called()
            for _ in range(2):
                status, _, body = self.request(self.loop_port, "GET", route + "Lo-fi")
                self.assertEqual((status, json.loads(body)), (200, {"videos": ["vugodYOKeis"], "more": False}))
            fetch.assert_called_once()
            query = parse_qs(urlsplit(fetch.call_args.args[0]).query)
            self.assertEqual(query["q"], ["Lo-fi music mix"])
            self.assertEqual(query["videoEmbeddable"], ["true"])
            self.assertNotIn("disposable-video-key", fetch.call_args.args[0])

    def test_only_verified_genres_offer_room_audio(self):
        records = [
            {"title": "Ready", "artist": "Artist", "album": "Night Work"},
            {"title": "Missing", "artist": "Artist"},
            {"title": "Removed", "artist": "Artist", "active": False},
            {"title": "Other genre", "artist": "Artist"},
        ]
        with (mock.patch.object(server.catalog, "records", records),
              mock.patch.object(server.catalog, "coverage", {i: {"status": "verified"} for i in (1, 3, 4)}),
              mock.patch.object(server.catalog, "genre_canonical", side_effect=lambda row:
                                "jazz" if row is records[3] else "indie"),
              mock.patch.object(server.catalog, "canonical_genres", return_value=["indie", "jazz"])):
            status, _, body = self.request(self.loop_port, "GET", "/api/stations")
            self.assertEqual((status, json.loads(body)["ready_genres"]), (200, {"indie": 1, "jazz": 1}))
            path = "/api/radio/library?genre=Indie"
            self.assertEqual(self.request(self.lan_port, "GET", path)[0], 401)
            cookie = self.lan_device_cookie()
            status, _, body = self.request(self.lan_port, "GET", path, headers={"Cookie": cookie})
            self.assertEqual((status, json.loads(body)), (200, {"tracks": [
                {"id": 1, "title": "Ready", "artist": "Artist", "album": "Night Work", "audio": "/api/audio/1"},
            ]}))
            self.assertEqual(self.request(self.loop_port, "GET", "/api/radio/library")[0], 400)
            _, _, body = self.request(self.loop_port, "GET", "/api/radio/library?genre=house")
            self.assertEqual(json.loads(body), {"tracks": []})

    def test_a_write_replaces_the_list_and_normalizes_seeds(self):
        body = {"stations": [
            {"name": "Focus", "seeds": [
                "https://www.youtube.com/watch?v=dqnnTbt99Q4&t=12s",
                "https://youtu.be/xgZc-C_LZqM",
            ]},
        ]}
        status, _, data = self.request(self.loop_port, "POST",
                                       "/api/stations", body=body)
        self.assertEqual(status, 200)
        expected = [{"name": "Focus",
                     "seeds": ["dqnnTbt99Q4", "xgZc-C_LZqM"]}]
        self.assertEqual(json.loads(data)["stations"], expected)
        status, _, data = self.request(self.loop_port, "GET", "/api/stations")
        self.assertEqual(json.loads(data)["stations"], expected)

    def test_a_bad_seed_is_refused_and_nothing_is_written(self):
        body = {"stations": [{"name": "Focus", "seeds": ["not a link"]}]}
        status, _, data = self.request(self.loop_port, "POST",
                                       "/api/stations", body=body)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(data)["error"],
                         "That link does not name a YouTube video")
        status, _, data = self.request(self.loop_port, "GET", "/api/stations")
        self.assertEqual(json.loads(data)["stations"], [])

    def test_deleting_every_station_stays_deleted(self):
        status, _, _ = self.request(self.loop_port, "POST", "/api/stations",
                                    body={"stations": []})
        self.assertEqual(status, 200)
        status, _, data = self.request(self.loop_port, "GET", "/api/stations")
        self.assertEqual(json.loads(data)["stations"], [])

    def test_stations_are_a_paired_device_surface(self):
        cookie = self.lan_device_cookie()
        headers = {"Cookie": cookie, "Origin": self.lan_origin()}
        status, _, data = self.request(self.lan_port, "GET", "/api/stations",
                                       headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["stations"], [])
        body = {"stations": [{"name": "Seeded from the couch",
                              "seeds": ["vugodYOKeis"]}]}
        status, _, data = self.request(self.lan_port, "POST", "/api/stations",
                                       body=body, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["stations"], body["stations"])
        # ...while a host-only action on the same tier is refused,
        # which is what keeps the two tiers distinguishable.
        status, _, _ = self.request(self.lan_port, "POST", "/api/settings",
                                    body={}, headers=headers)
        self.assertEqual(status, 403)

    def test_an_unpaired_lan_caller_gets_nothing(self):
        status, _, _ = self.request(self.lan_port, "GET", "/api/stations")
        self.assertEqual(status, 401)
        status, _, _ = self.request(
            self.lan_port, "POST", "/api/stations", body={"stations": []},
            headers={"Origin": self.lan_origin()})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
