import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import base64
import csv
import gzip
import http.client
import io
import hashlib
import hmac
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode, urlparse

from fixture_environment import isolate_room_environment as setUpModule
import server
from catalog.catalog_store import CatalogStore
from catalog.genres import GenreStore
from library.library_protocol import write_xspf
from system.updater import DIRTY_TREE, NOT_A_CHECKOUT, UpdateError, Updater

# TEST-NET-1 (RFC 5737): never a real interface. LAN tests present this
# address to the handler while the socket itself stays on loopback;
# the peer address is exactly the input the pairing exchange and the
# host-only gates branch on.
LAN_IP = "192.0.2.44"


class PresentedClientServer(ThreadingHTTPServer):
    daemon_threads = True
    presented_client = None

    def get_request(self):
        request, address = super().get_request()
        if self.presented_client:
            return request, (self.presented_client, address[1])
        return request, address


class ServerFixture:
    """init() against a temp workspace, real servers on ephemeral ports."""

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
            # Simulate the production wildcard listener while keeping both
            # fixture sockets reachable only through loopback.
            instance.server_address = ("0.0.0.0", instance.server_port)
            threading.Thread(target=instance.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for instance in (cls.loop_server, cls.lan_server):
            instance.shutdown()
            instance.server_close()
        cls.tmp.cleanup()

    def request(self, port, method, path, body=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        payload = raw if raw is not None else (
            json.dumps(body) if body is not None else None)
        all_headers = dict(headers or {})
        if body is not None:
            all_headers.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=payload, headers=all_headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        connection.close()
        return result

    def lan_cookie(self):
        status, headers, _ = self.request(self.lan_port, "GET", f"/?token={self.token}")
        self.assertEqual(status, 303)
        return headers["Set-Cookie"].split(";", 1)[0]

    def lan_device_cookie(self, device_id="lan-device-0001", name="LAN device"):
        """The cookie a paired browser holds after plan 021a: exchange
        the token (the bootstrap cookie), register the device, and keep
        the device credential the registration response issues."""
        bootstrap = self.lan_cookie()
        status, headers, _ = self.request(
            self.lan_port, "POST", "/api/device",
            body={"id": device_id, "name": name},
            headers={"Cookie": bootstrap, "Origin": self.lan_origin()})
        self.assertEqual(status, 200)
        set_cookie = headers["Set-Cookie"]
        self.assertNotIn(self.token, set_cookie)
        return set_cookie.split(";", 1)[0]

    def lan_origin(self):
        # The Origin a browser on the paired device sends: this
        # connection's own authority, which http.client also puts in
        # the Host header the server checks it against.
        return f"http://127.0.0.1:{self.lan_port}"


class PairingTests(ServerFixture, unittest.TestCase):
    def test_spotify_management_requires_host_even_when_paired(self):
        cookie = self.lan_device_cookie("spotify-host-only")
        for method in ("GET", "POST"):
            for headers, expected in [({}, 401), ({"Cookie": cookie}, 403)]:
                status, _, _ = self.request(self.lan_port, method, "/api/spotify/playlists",
                                            body={"op": "create"} if method == "POST" else None,
                                            headers={**headers, "Origin": self.lan_origin()})
                self.assertEqual(status, expected)

    def test_lan_request_without_cookie_gets_401(self):
        status, _, data = self.request(self.lan_port, "GET", "/api/overview")
        self.assertEqual(status, 401)
        self.assertIn("paired link", json.loads(data)["error"])

    def test_lan_root_without_token_gets_401(self):
        status, _, _ = self.request(self.lan_port, "GET", "/")
        self.assertEqual(status, 401)

    def test_wrong_token_is_rejected(self):
        status, headers, _ = self.request(self.lan_port, "GET", "/?token=wrong-token")
        self.assertEqual(status, 401)
        self.assertNotIn("Set-Cookie", headers)

    def test_wifi_discovery_proves_room_identity_without_a_cookie(self):
        device = "9b37bbee-f40a-4c62-b9d0-35de4d71bcc9"
        cookie = self.lan_device_cookie(device)
        secret = hashlib.sha256(cookie.split("=", 1)[1].encode()).digest()
        path = "/api/room/discover"
        status, headers, data = self.request(self.lan_port, "GET", path)
        self.assertEqual((status, headers["X-Listening-Room"], data), (204, "1", b""))
        self.assertNotIn("X-Listening-Proof", headers)
        bootstrap = {"X-Listening-Challenge": "b" * 64}
        status, headers, data = self.request(self.lan_port, "GET", path, headers=bootstrap)
        expected = hmac.new(hashlib.sha256(self.token.encode()).digest(),
                            ("listening-room-wifi-v1:" + "b" * 64).encode(), hashlib.sha256).hexdigest()
        self.assertEqual((status, headers["X-Listening-Proof"], data), (204, expected, b""))
        self.assertNotIn("Set-Cookie", headers)
        challenge = {"X-Listening-Device": device, "X-Listening-Challenge": "a" * 64}
        status, headers, data = self.request(self.lan_port, "GET", path, headers=challenge)
        proof = hmac.new(secret, ("listening-room-wifi-v1:" + "a" * 64).encode(), hashlib.sha256).hexdigest()
        self.assertEqual((status, headers["X-Listening-Proof"], data), (204, proof, b""))
        self.assertNotIn("Set-Cookie", headers)
        status, _, _ = self.request(self.lan_port, "GET", "/api/overview", headers=challenge)
        self.assertEqual(status, 401)
        server.device_store.delete(device)
        status, _, _ = self.request(self.lan_port, "GET", path, headers=challenge)
        self.assertEqual(status, 401)

    def test_room_readiness_preserves_pairing_gate(self):
        status, _, data = self.request(self.lan_port, "GET", "/api/room")
        self.assertEqual((status, json.loads(data)["code"]), (401, "pairing_required"))
        cookie = self.lan_device_cookie("room-readiness-01")
        headers = {"Cookie": cookie}
        with mock.patch.object(server.catalog, "build_stamp", 0), \
                mock.patch.object(server.REFRESH, "synced", return_value=False) as synced:
            status, _, data = self.request(self.lan_port, "GET", "/api/room", headers=headers)
            self.assertEqual((status, json.loads(data)), (200, {"state": "catalog_loading"}))
            synced.return_value = True
            status, _, data = self.request(self.lan_port, "GET", "/api/room", headers=headers)
            self.assertEqual((status, json.loads(data)), (200, {"state": "ready"}))
        server.device_store.delete("room-readiness-01")
        status, _, data = self.request(self.lan_port, "GET", "/api/room", headers=headers)
        self.assertEqual((status, json.loads(data)["code"]), (401, "pairing_required"))

    def test_unpaired_browser_gets_recovery_form_at_root(self):
        public = server.FRONTEND / "react" / "public"
        with mock.patch.object(server, "DIST", public):
            status, headers, data = self.request(
                self.lan_port, "GET", "/", headers={"Accept": "text/html"})
        self.assertEqual(status, 401)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b"Pair again", data)
        self.assertNotIn(self.token.encode(), data)

    def test_browser_recovery_exchange_sets_cookie_on_uncached_path(self):
        status, headers, data = self.request(
            self.lan_port, "GET", f"/pair?token={self.token}", headers={"Accept": "text/html"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn(b'location.replace("/")', data)
        self.assertNotIn(self.token.encode(), data)
        bad_status, bad_headers, _ = self.request(
            self.lan_port, "GET", "/pair?token=%C3%A9", headers={"Accept": "text/html"})
        self.assertEqual(bad_status, 401)
        self.assertNotIn("Set-Cookie", bad_headers)

    def test_token_exchange_sets_cookie_and_redirects(self):
        status, headers, _ = self.request(self.lan_port, "GET", f"/?token={self.token}")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        cookie = headers["Set-Cookie"]
        self.assertTrue(cookie.startswith(f"lr_token={self.token};"))
        self.assertNotIn("Max-Age", cookie)
        self.assertNotIn("Expires", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)

    def test_cookie_authorizes_lan_requests(self):
        # After plan 021a the authorizing cookie is the device
        # credential issued at registration, not the share token.
        cookie = self.lan_device_cookie()
        status, _, data = self.request(self.lan_port, "GET", "/api/overview",
                                       headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("playlists", json.loads(data))


class DeviceAccessTests(ServerFixture, unittest.TestCase):
    """Plan 021a: the pairing token is a one-time bootstrap. A browser
    that arrives with it registers its device and is issued its own
    credential; that credential is revocable on its own, and rotating
    the token kills the old link without unpairing anyone."""

    def register(self, device_id, name="Device"):
        bootstrap = self.lan_cookie()
        status, headers, _ = self.request(
            self.lan_port, "POST", "/api/device",
            body={"id": device_id, "name": name},
            headers={"Cookie": bootstrap, "Origin": self.lan_origin()})
        self.assertEqual(status, 200)
        return bootstrap, headers["Set-Cookie"].split(";", 1)[0]

    @mock.patch.dict(os.environ, {"LISTENING_ROOM_PROFILE": "home-server"})
    def test_home_browser_pairing_and_delegated_invitations(self):
        host = f"server:{self.lan_port}"
        device = "home-admin-browser"
        headers = {"Host": host, "Origin": f"http://{host}"}
        self.assertEqual(self.request(self.lan_port, "GET", "/", headers=headers)[0], 401)
        status, reply, _ = self.request(self.lan_port, "GET", f"/?token={self.token}", headers=headers)
        self.assertEqual(status, 303)
        headers["Cookie"] = reply["Set-Cookie"].split(";", 1)[0]
        status, reply, _ = self.request(self.lan_port, "POST", "/api/device",
                                        {"id": device, "name": "Mac"}, headers=headers)
        self.assertEqual(status, 200)
        headers["Cookie"] = reply["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request(self.lan_port, "GET", "/", headers=headers)[0], 200)
        self.assertEqual(self.request(self.lan_port, "GET", "/api/tracks", headers=headers)[0], 200)
        grant = {"id": device, "allowed": True}
        self.assertEqual(self.request(self.lan_port, "POST", "/api/devices/invites", grant, headers=headers)[0], 403)
        for allowed in (False, True, False):
            grant["allowed"] = allowed
            self.assertEqual(self.request(self.loop_port, "POST", "/api/devices/invites", grant)[0], 200)
            status, _, data = self.request(self.lan_port, "GET", "/api/settings", headers=headers)
            self.assertEqual(status, 200)
            settings = json.loads(data)
            self.assertEqual(settings["lan_url"], f"http://{host}/?token={self.token}" if allowed else "")
            self.assertEqual(settings["local_url"], "")
            self.assertEqual(settings["folder"], "")
            self.assertEqual(self.request(self.lan_port, "POST", "/api/pairing/rotate", {}, headers=headers)[0], 403)

    def test_device_credential_is_issued_at_registration(self):
        bootstrap = self.lan_cookie()
        status, _, _ = self.request(self.lan_port, "GET", "/api/overview",
                                    headers={"Cookie": bootstrap})
        self.assertEqual(status, 401)
        status, headers, data = self.request(
            self.lan_port, "POST", "/api/device",
            body={"id": "issue-device-01", "name": "Issued"},
            headers={"Cookie": bootstrap, "Origin": self.lan_origin()})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["ok"])
        set_cookie = headers["Set-Cookie"]
        self.assertTrue(set_cookie.startswith("lr_token="))
        self.assertNotIn(self.token, set_cookie)
        self.assertIn("Max-Age=31536000", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertIn("Path=/", set_cookie)
        cookie = set_cookie.split(";", 1)[0]
        status, _, data = self.request(self.lan_port, "GET", "/api/overview",
                                       headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("playlists", json.loads(data))

    def test_device_credential_registers_only_its_own_presence(self):
        _, cookie = self.register("registered-device")
        headers = {"Cookie": cookie, "Origin": self.lan_origin()}
        for device, expected in (("registered-device", 200), ("unregistered-device", 403)):
            status, _, _ = self.request(self.lan_port, "POST", "/api/device",
                                        body={"id": device, "name": "Phone"}, headers=headers)
            self.assertEqual(status, expected)
        self.assertNotIn("unregistered-device", {item["id"] for item in server.device_snapshot()})

    def test_device_presence_follows_tabs_and_keeps_the_pairing(self):
        device_id = "shared-browser"
        body = {"id": device_id, "name": "Browser"}
        origin = {"Origin": self.lan_origin(), "X-Listening-Output": "first-tab", "X-Listening-Output-Epoch": "1"}
        bootstrap = self.lan_cookie()
        status, reply, _ = self.request(self.lan_port, "POST", "/api/device", body=body,
                                        headers={**origin, "Cookie": bootstrap})
        self.assertEqual(status, 200)
        first = {**origin, "Cookie": reply["Set-Cookie"].split(";", 1)[0]}
        second = {**first, "X-Listening-Output": "second-tab"}

        def post(headers, **fields):
            status, _, _ = self.request(self.lan_port, "POST", "/api/device",
                                        body={**body, **fields}, headers=headers)
            self.assertEqual(status, 200)

        def present():
            status, _, data = self.request(self.lan_port, "GET", "/api/devices", headers=first)
            self.assertEqual(status, 200)
            snapshot = json.loads(data)
            connected = [item["id"] for item in snapshot["devices"]]
            paired = next(item for item in snapshot["paired_devices"] if item["id"] == device_id)
            self.assertEqual(connected.count(device_id), int(paired["present"]))
            return paired["present"]

        post(second)
        post(first, op="leave")
        self.assertTrue(present())
        post(second, op="leave")
        self.assertFalse(present())
        post(first)
        self.assertFalse(present())
        post({**first, "X-Listening-Output-Epoch": "2"})
        post(first, op="leave")
        self.assertTrue(present())

    def test_bootstrap_cookie_cannot_read_or_control(self):
        bootstrap = self.lan_cookie()
        for method, path, body in (("GET", "/api/tracks", None),
                                   ("GET", "/api/session", None),
                                   ("POST", "/api/session", {"op": "toggle"}),
                                   ("GET", "/api/settings", None)):
            status, _, _ = self.request(self.lan_port, method, path, body=body,
                                        headers={"Cookie": bootstrap,
                                                 "Origin": self.lan_origin()})
            self.assertEqual(status, 401, f"{method} {path}")
        status, _, _ = self.request(self.lan_port, "GET", "/",
                                    headers={"Cookie": bootstrap})
        self.assertEqual(status, 200)
        status, _, _ = self.request(self.lan_port, "GET", "/service-worker.js",
                                    headers={"Cookie": bootstrap})
        self.assertEqual(status, 200)

    def test_device_credential_cannot_pair(self):
        _, cookie = self.register("pair-device-01")
        credential = cookie.split("=", 1)[1]
        status, headers, _ = self.request(self.lan_port, "GET", f"/?token={credential}")
        self.assertEqual(status, 401)
        self.assertNotIn("Set-Cookie", headers)

    def test_revoked_device_loses_access(self):
        _, cookie_a = self.register("revoke-device-a", "Device A")
        _, cookie_b = self.register("revoke-device-b", "Device B")
        for cookie in (cookie_a, cookie_b):
            status, _, _ = self.request(self.lan_port, "GET", "/api/overview",
                                        headers={"Cookie": cookie})
            self.assertEqual(status, 200)
        status, _, _ = self.request(self.loop_port, "POST", "/api/devices/revoke",
                                    body={"id": "revoke-device-a"})
        self.assertEqual(status, 200)
        status, _, _ = self.request(self.lan_port, "GET", "/api/overview",
                                    headers={"Cookie": cookie_a})
        self.assertEqual(status, 401)
        status, _, _ = self.request(self.lan_port, "GET", "/api/overview",
                                    headers={"Cookie": cookie_b})
        self.assertEqual(status, 200)
        status, _, data = self.request(self.loop_port, "GET", "/api/devices")
        payload = json.loads(data)
        self.assertNotIn("revoke-device-a", {item["id"] for item in payload["paired_devices"]})
        self.assertNotIn("revoke-device-a", {item["id"] for item in payload["devices"]})
        self.assertIn("revoke-device-b", {item["id"] for item in payload["paired_devices"]})

    def test_revoke_and_rotate_are_host_only(self):
        _, cookie = self.register("host-only-device")
        token_before = server.SETTINGS["share_token"]
        for path, body in (("/api/devices/revoke", {"id": "host-only-device"}),
                           ("/api/pairing/rotate", {})):
            status, _, _ = self.request(self.lan_port, "POST", path, body=body,
                                        headers={"Cookie": cookie,
                                                 "Origin": self.lan_origin()})
            self.assertEqual(status, 403, path)
        # Neither attempt changed anything: the device still
        # authorizes and the pairing token is untouched.
        status, _, _ = self.request(self.lan_port, "GET", "/api/overview",
                                    headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(server.SETTINGS["share_token"], token_before)

    def test_rotation_keeps_devices_and_kills_the_old_link(self):
        _, cookie_a = self.register("rotate-device-a", "Device A")
        stale = self.lan_cookie()  # exchanged, but never registers
        old_token = server.SETTINGS["share_token"]
        status, _, data = self.request(self.loop_port, "POST", "/api/pairing/rotate",
                                       body={}, headers={"Host": "100.64.3.4:8765"})
        self.assertEqual(status, 200)
        lan_url = json.loads(data)["lan_url"]
        self.assertIn("?token=", lan_url)
        new_token = lan_url.split("?token=", 1)[1]
        self.assertNotEqual(new_token, old_token)
        self.assertEqual(server.SETTINGS["share_token"], new_token)
        # The old link is dead.
        status, headers, _ = self.request(self.lan_port, "GET", f"/?token={old_token}")
        self.assertEqual(status, 401)
        self.assertNotIn("Set-Cookie", headers)
        # The new link pairs.
        status, headers, _ = self.request(self.lan_port, "GET", f"/?token={new_token}")
        self.assertEqual(status, 303)
        self.assertTrue(headers["Set-Cookie"].startswith(f"lr_token={new_token};"))
        # The registered device never noticed.
        status, _, _ = self.request(self.lan_port, "GET", "/api/overview",
                                    headers={"Cookie": cookie_a})
        self.assertEqual(status, 200)
        # A cookie holding the old token is no longer even
        # bootstrap-valid: it is refused everywhere.
        for path in ("/", "/api/overview"):
            status, _, _ = self.request(self.lan_port, "GET", path,
                                        headers={"Cookie": stale})
            self.assertEqual(status, 401, path)

    def test_legacy_cookie_upgrades_without_repairing(self):
        # The pre-021 state: the browser's only cookie is the share
        # token itself, set by an old pairing exchange.
        legacy = f"lr_token={server.SETTINGS['share_token']}"
        status, _, _ = self.request(self.lan_port, "GET", "/",
                                    headers={"Cookie": legacy})
        self.assertEqual(status, 200)
        status, _, _ = self.request(self.lan_port, "GET", "/api/overview",
                                    headers={"Cookie": legacy})
        self.assertEqual(status, 401)
        status, headers, _ = self.request(
            self.lan_port, "POST", "/api/device",
            body={"id": "legacy-device-01", "name": "Legacy"},
            headers={"Cookie": legacy, "Origin": self.lan_origin()})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertNotIn(server.SETTINGS["share_token"], cookie)
        status, _, data = self.request(self.lan_port, "GET", "/api/overview",
                                       headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("playlists", json.loads(data))

    def test_devices_listing_merges_presence_and_pairing(self):
        _, cookie = self.register("listing-device-01", "Listing")
        # Simulate the device going away: paired, but not present.
        with server.DEVICES_LOCK:
            server.DEVICES.forget(("listing-device-01", ""))
        status, _, data = self.request(self.loop_port, "GET", "/api/devices")
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertTrue(payload["manage"])
        paired = {item["id"]: item for item in payload["paired_devices"]}
        self.assertIn("listing-device-01", paired)
        self.assertEqual(paired["listing-device-01"]["name"], "Listing")
        self.assertFalse(paired["listing-device-01"]["present"])
        self.assertGreater(paired["listing-device-01"]["paired_at"], 0)
        # A paired caller sees the same listing, without manage.
        status, _, data = self.request(self.lan_port, "GET", "/api/devices",
                                       headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertNotIn("manage", payload)
        self.assertIn("listing-device-01",
                      {item["id"] for item in payload["paired_devices"]})


class HostGateTests(ServerFixture, unittest.TestCase):
    # Plan 010 (A2 S-03): a cookie-authorized POST without an Origin is
    # refused before dispatch, so these host-gate pins send the Origin
    # a real browser would, isolating the gate they characterize.
    # The caller is a registered device (plan 021a): the bootstrap
    # cookie no longer reaches these routes at all (401 in
    # DeviceAccessTests), while a paired device is authorized and then
    # refused by the host gate itself, which is what these pins check.
    def test_headless_player_guides_owner_without_resolving_or_launching_audio(self):
        with mock.patch.object(server, "install_kind", return_value="docker"), mock.patch.object(server, "source_audio") as resolve:
            status, _, data = self.request(self.loop_port, "POST", "/api/open-source", {"ref": 1})
        self.assertEqual(status, 409)
        self.assertIn("room player", json.loads(data)["error"])
        resolve.assert_not_called()

    def test_desktop_player_requires_owner_and_uses_verified_source(self):
        with mock.patch.object(server, "desktop_player_opener", return_value="/usr/bin/open"), \
             mock.patch.object(server, "source_audio", return_value=Path("/fixture/track.mp3")) as resolve, \
             mock.patch.object(server.subprocess, "Popen") as launch:
            status, _, _ = self.request(self.lan_port, "POST", "/api/open-source", {"ref": 1},
                                        headers={"Cookie": self.lan_device_cookie(), "Origin": self.lan_origin()})
            self.assertEqual(status, 403)
            launch.assert_not_called()
            launch.return_value.wait.return_value = 0
            status, _, _ = self.request(self.loop_port, "POST", "/api/open-source", {"ref": 1})
            self.assertEqual(status, 200)
            resolve.assert_called_once_with(1)
            self.assertEqual(launch.call_args.args[0], ["/usr/bin/open", "/fixture/track.mp3"])
            launch.return_value.wait.return_value = 1
            status, _, data = self.request(self.loop_port, "POST", "/api/open-source", {"ref": 1})
            self.assertEqual(status, 409)
            self.assertIn("room player", json.loads(data)["error"])

    def test_post_settings_refused_from_lan(self):
        status, _, data = self.request(self.lan_port, "POST", "/api/settings", body={},
                                       headers={"Cookie": self.lan_device_cookie(),
                                                "Origin": self.lan_origin()})
        self.assertEqual(status, 403)
        self.assertIn("host settings", json.loads(data)["error"])

    def test_post_sources_refused_from_lan(self):
        status, _, _ = self.request(self.lan_port, "POST", "/api/sources",
                                    body={"op": "watch", "path": "/tmp"},
                                    headers={"Cookie": self.lan_device_cookie(),
                                             "Origin": self.lan_origin()})
        self.assertEqual(status, 403)

    def test_post_import_list_refused_from_lan(self):
        status, _, _ = self.request(self.lan_port, "POST", "/api/import-list",
                                    body={"name": "x.csv", "content": "Title,Artist\nA,B\n"},
                                    headers={"Cookie": self.lan_device_cookie(),
                                             "Origin": self.lan_origin()})
        self.assertEqual(status, 403)

    def test_post_process_refused_from_lan(self):
        status, _, _ = self.request(self.lan_port, "POST", "/api/process",
                                    body={"action": "bulk_stop"},
                                    headers={"Cookie": self.lan_device_cookie(),
                                             "Origin": self.lan_origin()})
        self.assertEqual(status, 403)

    def test_get_settings_redacted_from_lan(self):
        # Plan 010 (A2 S-01), coordinator revision: the paired UI reads
        # both routes, so non-host callers get the payload with every
        # host-local value blanked rather than a refusal.
        status, _, data = self.request(self.lan_port, "GET", "/api/settings",
                                       headers={"Cookie": self.lan_device_cookie()})
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertEqual(payload["lan_url"], "")
        self.assertEqual(payload["folder"], "")
        self.assertEqual(payload["music_folder"], "")
        self.assertEqual(payload["sockseek_binary"], "")
        self.assertTrue(all(entry["path"] == "" for entry in payload["source_folders"]))
        self.assertIn("list_files", payload)
        self.assertIn("download_ready", payload)

    def test_get_process_logs_redacted_from_lan(self):
        status, _, data = self.request(self.lan_port, "GET", "/api/process",
                                       headers={"Cookie": self.lan_device_cookie()})
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertEqual(payload["logs"], [])
        self.assertIn("activity", payload)

    def test_get_settings_ok_from_host(self):
        status, _, data = self.request(self.loop_port, "GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["lan_url"])

    def test_get_process_ok_from_host(self):
        status, _, data = self.request(self.loop_port, "GET", "/api/process")
        self.assertEqual(status, 200)
        self.assertIn("logs", json.loads(data))

    def test_get_export_refused_from_lan(self):
        status, _, data = self.request(self.lan_port, "GET", "/api/export",
                                       headers={"Cookie": self.lan_device_cookie()})
        self.assertEqual(status, 403)
        self.assertIn("host computer", json.loads(data)["error"])

    def test_get_export_settings_refused_from_lan(self):
        status, _, _ = self.request(self.lan_port, "GET", "/api/export/settings",
                                    headers={"Cookie": self.lan_device_cookie()})
        self.assertEqual(status, 403)


class PlaybackInsightsTests(ServerFixture, unittest.TestCase):
    def test_reports_only_the_authenticated_output_playback_payloads(self):
        cookie = self.lan_device_cookie("insights-device-a")
        other = self.lan_device_cookie("insights-device-b")
        output = "test-output-a"
        headers = {"Cookie": cookie, "X-Listening-Output": output, "Origin": self.lan_origin()}
        sample = self.workspace / "insights.mp3"
        sample.write_bytes(b"audio sample data")
        command = b'{"op":"outputs","devices":["test-output-a"]}'

        now = 100.0
        with mock.patch.object(server, "PLAYBACK_TRAFFIC", server.PlaybackTraffic(lambda: now)), \
             mock.patch.object(server.audio_resolution, "track_file", return_value=sample):
            status, _, _ = self.request(self.lan_port, "POST", "/api/session", raw=command,
                                        headers={**headers, "Content-Type": "application/json"})
            self.assertEqual(status, 200)
            status, _, data = self.request(self.lan_port, "GET", f"/api/audio/1?output={output}",
                                            headers={"Cookie": cookie, "Range": "bytes=0-4"})
            self.assertEqual((status, data), (206, b"audio"))
            status, _, _ = self.request(self.lan_port, "GET", f"/api/audio/1?save=1&output={output}",
                                        headers={"Cookie": cookie, "Range": "bytes=0-4"})
            self.assertEqual(status, 206)
            now += .25
            status, _, data = self.request(self.lan_port, "GET", "/api/insights", headers=headers)
            self.assertEqual(status, 200)
            snapshot = json.loads(data)
            self.assertEqual(snapshot.pop("history")[-1],
                             {"download_bytes": 5, "upload_bytes": len(command)})
            self.assertEqual(snapshot, {"window_seconds": 5, "download_bytes": 5,
                                        "upload_bytes": len(command)})
            now += 5
            status, _, data = self.request(self.lan_port, "GET", "/api/insights", headers=headers)
            self.assertEqual(status, 200)
            snapshot = json.loads(data)
            self.assertEqual(snapshot["download_bytes"], 0)
            self.assertEqual(snapshot["history"][-2],
                             {"download_bytes": 5, "upload_bytes": len(command)})
            for unseen in ({**headers, "Cookie": other}, {**headers, "X-Listening-Output": "test-output-b"}):
                status, _, data = self.request(self.lan_port, "GET", "/api/insights", headers=unseen)
                self.assertEqual(status, 200)
                snapshot = json.loads(data)
                self.assertEqual(snapshot["download_bytes"], 0)
                self.assertEqual(snapshot["upload_bytes"], 0)
                self.assertFalse(any(window["download_bytes"] or window["upload_bytes"]
                                     for window in snapshot["history"]))
            status, _, _ = self.request(self.lan_port, "GET", "/api/insights",
                                        headers={"X-Listening-Output": output})
            self.assertEqual(status, 401)


class HikingAccessTests(ServerFixture, unittest.TestCase):
    def test_https_authority_uses_secure_cookies_without_inheriting_loopback_access(self):
        host = "room.example.test:8443"
        origin = "https://" + host
        with mock.patch.dict(os.environ, {"LISTENING_ROOM_HIKING_ORIGIN": origin}):
            status, _, _ = self.request(self.loop_port, "GET", "/api/hiking", headers={"Host": host})
            self.assertEqual(status, 401)
            status, _, _ = self.request(self.loop_port, "GET", "/api/hiking",
                                        headers={"Host": "room.example.test:8444"})
            self.assertEqual(status, 400)
            status, headers, _ = self.request(self.loop_port, "GET", f"/?token={self.token}",
                                              headers={"Host": host})
            self.assertEqual(status, 303)
            self.assertIn("; Secure", headers["Set-Cookie"])
            bootstrap = headers["Set-Cookie"].split(";", 1)[0]
            body = {"id": "hiking-test-device", "name": "HTTPS device"}
            status, _, _ = self.request(self.loop_port, "POST", "/api/device", body=body,
                                        headers={"Host": host, "Origin": "http://" + host, "Cookie": bootstrap})
            self.assertEqual(status, 403)
            status, headers, _ = self.request(self.loop_port, "POST", "/api/device", body=body,
                                              headers={"Host": host, "Origin": origin, "Cookie": bootstrap})
            self.assertEqual(status, 200)
            self.assertIn("; Secure", headers["Set-Cookie"])
            credential = headers["Set-Cookie"].split(";", 1)[0]
            status, _, payload = self.request(self.loop_port, "GET", "/api/hiking",
                                               headers={"Host": host, "Cookie": credential})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(payload)["offline_origin"], origin)


class LandingAccessTests(ServerFixture, unittest.TestCase):
    def test_tailnet_landing_requires_a_tailnet_or_loopback_peer(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, {"LISTENING_ROOM_ABOUT_TAILNET_ONLY": "1"}), mock.patch.object(
                server, "REQUIRE_OWNER", True), mock.patch.object(server, "DIST", Path(directory)):
            dist = Path(directory)
            (dist / "assets/about-showcase").mkdir(parents=True)
            (dist / "about").mkdir()
            (dist / "about/index.html").write_text("Mock landing")
            (dist / "index.html").write_text("Mock player")
            screenshot = "/assets/about-showcase/kompakt.png"
            (dist / screenshot.lstrip("/")).write_bytes(b"Mock screenshot")
            for path in ("/about", "/about/", "/about/index.html", "/assets/../about/index.html",
                         screenshot, "/assets/icons/../about-showcase/kompakt.png"):
                with self.subTest(path=path):
                    self.assertEqual(self.request(self.lan_port, "GET", path,
                                                  headers={"X-Forwarded-For": "100.69.1.2"})[0], 404)
                    status, headers, _ = self.request(self.loop_port, "GET", path)
                    self.assertEqual(status, 200)
                    self.assertEqual(headers["Cache-Control"], "private, no-store")
            self.assertEqual(self.request(self.lan_port, "GET", "/")[0], 200)
            with mock.patch.object(self.lan_server, "presented_client", "100.69.1.2"):
                self.assertEqual(self.request(self.lan_port, "GET", "/about/")[0], 200)
                self.assertEqual(self.request(self.lan_port, "GET", screenshot)[0], 200)
                self.assertEqual(self.request(self.lan_port, "GET", "/api/tracks")[0], 401)
            with mock.patch.dict(os.environ, {"LISTENING_ROOM_ABOUT_TAILNET_ONLY": "0"}):
                status, headers, _ = self.request(self.lan_port, "GET", screenshot)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")


class PostDefenseTests(ServerFixture, unittest.TestCase):
    def test_cross_origin_origin_is_refused(self):
        status, _, data = self.request(
            self.loop_port, "POST", "/api/session", body={},
            headers={"Origin": "http://evil.example.com"})
        self.assertEqual(status, 403)
        self.assertIn("Invalid origin", json.loads(data)["error"])

    def test_missing_origin_refused_for_cookie_post(self):
        status, _, data = self.request(
            self.lan_port, "POST", "/api/device",
            body={"id": "device-1234", "name": "Paired"},
            headers={"Cookie": self.lan_cookie()})
        self.assertEqual(status, 403)
        self.assertIn("Invalid origin", json.loads(data)["error"])

    def test_matching_origin_accepted_for_cookie_post(self):
        status, _, data = self.request(
            self.lan_port, "POST", "/api/device",
            body={"id": "device-1234", "name": "Paired"},
            headers={"Cookie": self.lan_cookie(), "Origin": self.lan_origin()})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["ok"])

    def test_missing_origin_accepted_from_host(self):
        # Host callers are authorized by loopback address, not the
        # cookie, and may be non-browser tools: no Origin required.
        status, _, data = self.request(
            self.loop_port, "POST", "/api/device",
            body={"id": "device-5678", "name": "Host tool"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["ok"])

    def test_non_json_content_type_is_refused(self):
        status, _, data = self.request(
            self.loop_port, "POST", "/api/session", raw="hello",
            headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 400)
        self.assertIn("Send JSON", json.loads(data)["error"])

    def test_malformed_json_is_refused(self):
        status, _, data = self.request(
            self.loop_port, "POST", "/api/session", raw="{not json",
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIn("Invalid JSON", json.loads(data)["error"])

    def test_unknown_action_is_404(self):
        status, _, data = self.request(self.loop_port, "POST", "/api/bogus", body={})
        self.assertEqual(status, 404)
        self.assertIn("Unknown action", json.loads(data)["error"])


class PairingLinkHostTests(ServerFixture, unittest.TestCase):
    """Plan 024, pinned at the HTTP seam: the Settings pairing link
    follows the request's Host when it is a Tailscale address."""

    def test_link_follows_host_with_port(self):
        status, _, data = self.request(self.loop_port, "GET", "/api/settings",
                                       headers={"Host": "100.64.3.4:8765"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["lan_url"],
                         f"http://100.64.3.4:8765/?token={self.token}")

    def test_link_follows_host_without_port(self):
        status, _, data = self.request(self.loop_port, "GET", "/api/settings",
                                       headers={"Host": "100.64.3.4"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["lan_url"],
                         f"http://100.64.3.4:{self.loop_port}/?token={self.token}")


class HostValidationTests(ServerFixture, unittest.TestCase):
    """Plan 021b: the Host header is validated before authorization,
    for loopback callers too. IP literals, localhost, .local names,
    and the allowed_hosts setting are served; anything else gets a
    400 with no cookie and no redirect, and forwarding headers are
    never consulted."""

    def request_without_host(self, port, path):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.putrequest("GET", path, skip_host=True)
        connection.endheaders()
        response = connection.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        connection.close()
        return result

    def test_hostile_host_is_refused_with_a_valid_cookie(self):
        cookie = self.lan_device_cookie()
        token_before = server.SETTINGS["share_token"]
        for method, path, body in (("GET", "/api/overview", None),
                                   ("POST", "/api/session", {"op": "toggle"})):
            status, headers, data = self.request(
                self.lan_port, method, path, body=body,
                headers={"Cookie": cookie, "Host": "evil.example",
                         "Origin": "http://evil.example"})
            self.assertEqual(status, 400, f"{method} {path}")
            self.assertEqual(json.loads(data)["error"], "Unknown host")
            self.assertNotIn("Set-Cookie", headers)
        # No state changed: the token is untouched and the device
        # credential still authorizes under a served host.
        self.assertEqual(server.SETTINGS["share_token"], token_before)
        status, _, _ = self.request(self.lan_port, "GET", "/api/overview",
                                    headers={"Cookie": cookie})
        self.assertEqual(status, 200)

    def test_hostile_host_cannot_exchange_the_token(self):
        status, headers, data = self.request(
            self.lan_port, "GET", f"/?token={self.token}",
            headers={"Host": "evil.example"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(data)["error"], "Unknown host")
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn("Location", headers)

    def test_hostile_host_is_refused_from_loopback_too(self):
        # A loopback caller is otherwise host-authorized for
        # everything; the header alone refuses it.
        status, _, data = self.request(self.loop_port, "GET", "/api/overview",
                                       headers={"Host": "evil.example"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(data)["error"], "Unknown host")

    def test_forwarded_host_is_never_trusted(self):
        status, _, data = self.request(
            self.lan_port, "GET", "/api/overview",
            headers={"Host": "evil.example",
                     "X-Forwarded-Host": "127.0.0.1"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(data)["error"], "Unknown host")

    def test_allowed_host_forms_pass(self):
        # No cookie: a 401 means the Host gate let the request
        # through to authorization, which is the pass condition.
        for host in (f"192.0.2.10:{self.lan_port}", "100.64.3.4",
                     "localhost", f"localhost:{self.lan_port}",
                     "[::1]:8765", "::1", "room.local",
                     f"room.local:{self.lan_port}", "ROOM.LOCAL"):
            status, _, _ = self.request(self.lan_port, "GET", "/api/overview",
                                        headers={"Host": host})
            self.assertEqual(status, 401, host)
        server.SETTINGS["allowed_hosts"] = ["room.example"]
        try:
            for host in ("room.example", f"Room.Example:{self.lan_port}"):
                status, _, _ = self.request(self.lan_port, "GET", "/api/overview",
                                            headers={"Host": host})
                self.assertEqual(status, 401, host)
        finally:
            server.SETTINGS["allowed_hosts"] = []

    def test_missing_host_from_a_lan_address_is_refused(self):
        status, _, data = self.request_without_host(self.lan_port, "/api/overview")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(data)["error"], "Unknown host")
        # From loopback the same request is local tooling: allowed.
        status, _, _ = self.request_without_host(self.loop_port, "/api/overview")
        self.assertEqual(status, 200)

    def test_pairing_link_host_passes_validation(self):
        # The plan 024 integration assertion: browse Settings as the
        # host under an allowed name, take the generated link's host,
        # and a fresh LAN client using that exact Host pairs.
        status, _, data = self.request(
            self.loop_port, "GET", "/api/settings",
            headers={"Host": f"room.local:{self.loop_port}"})
        self.assertEqual(status, 200)
        authority = urlparse(json.loads(data)["lan_url"]).netloc
        self.assertTrue(authority)
        status, headers, _ = self.request(
            self.lan_port, "GET", f"/?token={self.token}",
            headers={"Host": authority})
        self.assertEqual(status, 303)
        self.assertTrue(headers["Set-Cookie"].startswith(f"lr_token={self.token};"))


class ImportBundleTests(ServerFixture, unittest.TestCase):
    def test_import_bundle_writes_nothing_when_one_member_is_invalid(self):
        good = write_xspf("Good List", [{"title": "Song", "artist": "Artist",
                                         "album": "Album"}])
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("lists/good.xspf", good)
            archive.writestr("lists/bad.xspf", "this is not an xspf document")
        before = sorted(path.name for path in server.source_dir().iterdir())
        status, _, _ = self.request(
            self.loop_port, "POST", "/api/import-bundle",
            body={"content": base64.b64encode(buffer.getvalue()).decode("ascii")})
        self.assertEqual(status, 400)
        after = sorted(path.name for path in server.source_dir().iterdir())
        self.assertEqual(before, after)
        self.assertNotIn("good.xspf", after)


class ImportConflictTests(ServerFixture, unittest.TestCase):
    """Plan 011: a same-name import with different content is a
    choice, never a silent overwrite. Default is fail-closed."""

    def import_list(self, name, content, **extra):
        body = {"name": name, "content": content}
        body.update(extra)
        return self.request(self.loop_port, "POST", "/api/import-list", body=body)

    def import_bundle(self, members, **extra):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in members:
                archive.writestr(f"lists/{name}", content)
        body = {"content": base64.b64encode(buffer.getvalue()).decode("ascii")}
        body.update(extra)
        return self.request(self.loop_port, "POST", "/api/import-bundle", body=body)

    @staticmethod
    def xspf(title, song):
        return write_xspf(title, [{"title": song, "artist": "Artist", "album": "Album"}])

    def test_single_conflict_writes_nothing_and_names_the_conflict(self):
        original = self.xspf("Clash", "First Song")
        changed = self.xspf("Clash", "Other Song")
        status, _, _ = self.import_list("clash.xspf", original)
        self.assertEqual(status, 200)
        target = server.source_dir() / "clash.xspf"
        status, _, data = self.import_list("clash.xspf", changed)
        self.assertEqual(status, 409)
        payload = json.loads(data)
        self.assertEqual(payload["conflicts"], ["clash.xspf"])
        self.assertIn("clash.xspf", payload["error"])
        self.assertEqual(target.read_text(encoding="utf-8"), original)
        self.assertFalse((server.source_dir() / ".previous" / "clash.xspf").exists())
        # An unrecognized choice is the fail-closed default too.
        status, _, _ = self.import_list("clash.xspf", changed, on_conflict="merge")
        self.assertEqual(status, 409)
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_identical_reimport_is_a_noop(self):
        content = self.xspf("Same", "Only Song")
        status, _, _ = self.import_list("same.xspf", content)
        self.assertEqual(status, 200)
        target = server.source_dir() / "same.xspf"
        mtime = target.stat().st_mtime_ns
        status, _, data = self.import_list("same.xspf", content)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["name"], "same.xspf")
        self.assertEqual(target.stat().st_mtime_ns, mtime)
        self.assertEqual(target.read_text(encoding="utf-8"), content)

    def test_replace_snapshots_the_previous_version(self):
        first = self.xspf("Swap", "Version One")
        second = self.xspf("Swap", "Version Two")
        third = self.xspf("Swap", "Version Three")
        self.assertEqual(self.import_list("swap.xspf", first)[0], 200)
        status, _, _ = self.import_list("swap.xspf", second, on_conflict="replace")
        self.assertEqual(status, 200)
        target = server.source_dir() / "swap.xspf"
        snapshot = server.source_dir() / ".previous" / "swap.xspf"
        self.assertEqual(target.read_text(encoding="utf-8"), second)
        self.assertEqual(snapshot.read_text(encoding="utf-8"), first)
        # A second replace refreshes the snapshot to the middle version:
        # one generation is kept, not a history.
        status, _, _ = self.import_list("swap.xspf", third, on_conflict="replace")
        self.assertEqual(status, 200)
        self.assertEqual(target.read_text(encoding="utf-8"), third)
        self.assertEqual(snapshot.read_text(encoding="utf-8"), second)

    def test_rename_keeps_both_under_derived_names(self):
        first = self.xspf("Twin", "Original Song")
        second = self.xspf("Twin", "Second Song")
        third = self.xspf("Twin", "Third Song")
        self.assertEqual(self.import_list("twin.xspf", first)[0], 200)
        target = server.source_dir() / "twin.xspf"
        before = target.read_bytes()
        status, _, data = self.import_list("twin.xspf", second, on_conflict="rename")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["name"], "twin (2).xspf")
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual((server.source_dir() / "twin (2).xspf").read_text(encoding="utf-8"),
                         second)
        status, _, data = self.import_list("twin.xspf", third, on_conflict="rename")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["name"], "twin (3).xspf")
        self.assertEqual(target.read_bytes(), before)

    def test_bundle_conflict_blocks_everything_until_replace(self):
        existing = self.xspf("Bundle Clash", "Old Song")
        changed = self.xspf("Bundle Clash", "New Song")
        fresh = self.xspf("Bundle Fresh", "Fresh Song")
        self.assertEqual(self.import_list("bundleclash.xspf", existing)[0], 200)
        status, _, data = self.import_bundle(
            [("bundleclash.xspf", changed), ("bundlefresh.xspf", fresh)])
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(data)["conflicts"], ["bundleclash.xspf"])
        directory = server.source_dir()
        self.assertEqual((directory / "bundleclash.xspf").read_text(encoding="utf-8"),
                         existing)
        self.assertFalse((directory / "bundlefresh.xspf").exists())
        status, _, data = self.import_bundle(
            [("bundleclash.xspf", changed), ("bundlefresh.xspf", fresh)],
            on_conflict="replace")
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertEqual(payload["lists"], 2)
        self.assertEqual(payload["names"], ["bundleclash.xspf", "bundlefresh.xspf"])
        self.assertEqual((directory / "bundleclash.xspf").read_text(encoding="utf-8"),
                         changed)
        self.assertEqual((directory / "bundlefresh.xspf").read_text(encoding="utf-8"),
                         fresh)
        self.assertEqual((directory / ".previous" / "bundleclash.xspf")
                         .read_text(encoding="utf-8"), existing)

    def test_catalog_never_surfaces_previous_snapshots(self):
        self.assertEqual(self.import_list(
            "visible.xspf", self.xspf("Visible", "Seen Song"))[0], 200)
        previous = server.source_dir() / ".previous"
        previous.mkdir(parents=True, exist_ok=True)
        (previous / "ghost.xspf").write_text(
            self.xspf("Ghost", "Hidden Song"), encoding="utf-8")
        # A later import runs the refresh pipeline end to end.
        self.assertEqual(self.import_list(
            "visibletwo.xspf", self.xspf("Visible Two", "Seen Too"))[0], 200)
        status, _, data = self.request(self.loop_port, "GET", "/api/overview")
        self.assertEqual(status, 200)
        ids = {playlist["id"] for playlist in json.loads(data)["playlists"]}
        self.assertIn("visible", ids)
        self.assertIn("visibletwo", ids)
        self.assertNotIn("ghost", ids)
        self.assertNotIn("ghost", server.catalog.list_names)


class ImportOversizedFieldTests(ServerFixture, unittest.TestCase):
    """Plan 022: a CSV field past the csv module's limit is the
    standard JSON error, not a dropped connection (csv.Error escaped
    the import handlers' except tuples)."""

    def test_import_list_with_an_oversized_field_csv_returns_a_json_error(self):
        content = "Title,Artist\n" + ("x" * 200_000) + ",Some Artist\n"
        before = sorted(path.name for path in server.source_dir().iterdir())
        status, _, data = self.request(
            self.loop_port, "POST", "/api/import-list",
            body={"name": "huge.csv", "content": content})
        self.assertEqual(status, 400)
        self.assertIn("field larger than field limit",
                      json.loads(data)["error"])
        after = sorted(path.name for path in server.source_dir().iterdir())
        self.assertEqual(before, after)


class WatchdogHealthHttpTests(ServerFixture, unittest.TestCase):
    """Plan 022: the activity snapshot carries the watchdog failure
    record; the healthy form is the empty one."""

    def setUp(self):
        server.WATCHDOG_HEALTH.update(
            {"last_error": "", "last_error_at": 0.0, "failures": 0})
        # Keep the failure below out of the log: this class tests the
        # snapshot, not the rate-limited logging.
        server._WATCHDOG_LOG_STATE.update(
            {"last_at": time.monotonic(), "suppressed": 0})
        self.addCleanup(server.WATCHDOG_HEALTH.update,
                        {"last_error": "", "last_error_at": 0.0, "failures": 0})
        self.addCleanup(server._WATCHDOG_LOG_STATE.update,
                        {"last_at": 0.0, "suppressed": 0})

    def activity(self):
        status, _, data = self.request(self.loop_port, "GET", "/api/activity")
        self.assertEqual(status, 200)
        return json.loads(data)

    def test_activity_snapshot_carries_watchdog_health(self):
        self.assertEqual(self.activity()["watchdog"],
                         {"last_error": "", "last_error_at": 0.0, "failures": 0})
        with mock.patch.object(server, "_watchdog_pass",
                               side_effect=RuntimeError("boom")):
            server._watchdog_run_once()
        block = self.activity()["watchdog"]
        self.assertEqual(block["failures"], 1)
        self.assertEqual(block["last_error"], "RuntimeError: boom")
        self.assertGreater(block["last_error_at"], 0)


class OverviewNamingTests(ServerFixture, unittest.TestCase):
    """Plan 012: overview naming precedence is the list's own declared
    title, then the adapter's name, then the owner's playlist_names
    setting, then the humanized key."""

    def setUp(self):
        self._spotify = server.spotify
        self._owner_names = server.SETTINGS.get("playlist_names")
        self.addCleanup(self._restore_naming_state)

    def _restore_naming_state(self):
        server.spotify = self._spotify
        if self._owner_names is None:
            server.SETTINGS.pop("playlist_names", None)
        else:
            server.SETTINGS["playlist_names"] = self._owner_names

    def stub_spotify_names(self, names):
        class Stub:
            def playlist_links(self):
                return {}

            def playlist_names(self):
                return names

            def paused_keys(self):
                return set()

            def playlist_keys(self):
                return set()
        server.spotify = Stub()

    def import_list(self, name, content):
        status, _, _ = self.request(self.loop_port, "POST", "/api/import-list",
                                    body={"name": name, "content": content})
        self.assertEqual(status, 200)

    def overview_names(self):
        status, _, data = self.request(self.loop_port, "GET", "/api/overview")
        self.assertEqual(status, 200)
        return {entry["id"]: entry["name"]
                for entry in json.loads(data)["playlists"]}

    def test_declared_title_beats_adapter_name(self):
        content = write_xspf("Declared Title", [{"title": "Song", "artist": "Artist",
                                                 "album": "Album"}])
        self.import_list("declared.xspf", content)
        self.stub_spotify_names({"declared": "Adapter Name"})
        self.assertEqual(self.overview_names()["declared"], "Declared Title")

    def test_adapter_name_when_list_declares_no_title(self):
        # A CSV declares no title of its own; its canonical XSPF is
        # titled with the file stem, which playlist_title treats as
        # no declared title, so the adapter's name applies.
        self.import_list("adapteronly.csv", "Title,Artist\nSome Song,Some Artist\n")
        self.stub_spotify_names({"adapteronly": "Adapter Name"})
        self.assertEqual(self.overview_names()["adapteronly"], "Adapter Name")

    def test_settings_name_beats_humanized_key(self):
        self.import_list("owner_named.csv", "Title,Artist\nSome Song,Some Artist\n")
        self.stub_spotify_names({})
        server.SETTINGS["playlist_names"] = {"owner_named": "Owner Name"}
        self.assertEqual(self.overview_names()["owner_named"], "Owner Name")

    def test_spotify_generated_title_uses_cached_name_for_projected_and_legacy_playlists(self):
        provider_id = "a" * 22
        key = "spotify_" + provider_id
        tracks = [{"title": "Song", "artist": "Fixture Artist"}]
        self.import_list("projected-import.xspf", write_xspf("spotify " + provider_id, tracks))
        self.import_list(key + ".xspf", write_xspf(key, tracks))
        self.stub_spotify_names({key: "Spotify · Evening records"})
        names = self.overview_names()
        self.assertEqual(names["projected-import"], "Spotify · Evening records")
        self.assertEqual(names[key], "Spotify · Evening records")
        self.stub_spotify_names({})
        self.assertEqual(self.overview_names()[key], "Spotify playlist")

    def test_humanized_key_is_the_last_resort(self):
        self.import_list("plain_mix.csv", "Title,Artist\nSome Song,Some Artist\n")
        self.stub_spotify_names({})
        self.assertEqual(self.overview_names()["plain_mix"], "plain mix")


class PersonalDataAbsenceTests(unittest.TestCase):
    """Plan 012: the NAMES map and the owner's personal playlist names
    live in his settings now, never in the shipped backend code. The
    adapter's own "Spotify · Liked songs" display name is its own
    business and stays; the needle for that one value is the exact
    quoted string from the old map."""

    NEEDLES = [
        "la_mia_storia_tra_le_dita_radio", "new_ambient",
        "l'casa_di_garibaldi", "valencianas_and_piamontesas_and_new_yorkinas",
        "menos_ruido",
        "La mia storia tra le dita · Radio", "New ambient",
        "L’casa di Garibaldi", "Valencianas, Piamontesas & New Yorkinas",
        "Menos ruido", '"Liked songs"', "Too Nano",
    ]

    def test_backend_carries_no_personal_names(self):
        backend = Path(server.__file__).resolve().parent
        offenders = []
        for path in sorted(backend.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for needle in self.NEEDLES:
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
            if re.search(r"^NAMES\s*=", text, re.MULTILINE):
                offenders.append(f"{path.name}: NAMES map")
        self.assertEqual(offenders, [])
        self.assertFalse(hasattr(server, "NAMES"))


class SettingsRollbackTests(ServerFixture, unittest.TestCase):
    def test_room_download_format_validates_and_persists_without_starting_a_job(self):
        status, _, data = self.request(self.loop_port, "GET", "/api/settings")
        self.assertEqual((status, json.loads(data)["bulk_download_format"]), (200, "mp3_preferred"))
        try:
            with mock.patch.object(server, "start_worker") as start:
                status, _, _ = self.request(self.loop_port, "POST", "/api/settings",
                                            body={"bulk_download_format": "mp3_only"})
                self.assertEqual(status, 200)
                status, _, _ = self.request(self.loop_port, "POST", "/api/settings",
                                            body={"bulk_download_format": []})
                self.assertEqual(status, 400)
                start.assert_not_called()
            saved = json.loads(self.config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["bulk_download_format"], "mp3_only")
        finally:
            self.request(self.loop_port, "POST", "/api/settings", body={"bulk_download_format": "mp3_preferred"})

    def test_settings_failure_restores_prior_settings_and_root(self):
        old_root = server.ROOT
        old_folder = server.SETTINGS["folder"]
        new_root = Path(self.tmp.name) / "elsewhere"
        status, _, data = self.request(
            self.loop_port, "POST", "/api/settings",
            body={"folder": str(new_root), "share_password": "secret"})
        self.assertEqual(status, 400)
        self.assertIn("share folder", json.loads(data)["error"])
        self.assertEqual(server.ROOT, old_root)
        self.assertEqual(server.SETTINGS["folder"], old_folder)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["folder"], old_folder)


GIT_ENV = dict(os.environ,
               GIT_AUTHOR_NAME="Listening Test", GIT_AUTHOR_EMAIL="test@example.com",
               GIT_COMMITTER_NAME="Listening Test", GIT_COMMITTER_EMAIL="test@example.com")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, env=GIT_ENV, check=True)


class UpdateHttpTests(ServerFixture, unittest.TestCase):
    """Plan 023: the update check and the update process action are
    host-only; a good update fast-forwards the checkout and schedules
    the restart; refusals change nothing. The checkout under test is a
    temp clone of a local bare remote, swapped in for the app's own."""

    def setUp(self):
        self._updater = server.updater
        self._schedule = server.schedule_update_restart
        self.restarts = []
        server.schedule_update_restart = lambda: self.restarts.append(True)
        self.addCleanup(self._restore_update_state)
        base = Path(tempfile.mkdtemp(dir=self.tmp.name))
        self.remote = base / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote)],
                       capture_output=True, env=GIT_ENV, check=True)
        self.seed = base / "seed"
        subprocess.run(["git", "init", "-b", "main", str(self.seed)],
                       capture_output=True, env=GIT_ENV, check=True)
        git(self.seed, "remote", "add", "origin", str(self.remote))
        self.commit(self.seed, "app.py", "VERSION = 1\n", "first")
        git(self.seed, "push", "origin", "main")
        self.clone = base / "app"
        subprocess.run(["git", "clone", str(self.remote), str(self.clone)],
                       capture_output=True, env=GIT_ENV, check=True)
        server.updater = Updater(self.clone)

    def _restore_update_state(self):
        server.updater = self._updater
        server.schedule_update_restart = self._schedule

    def commit(self, repo, name, content, message):
        (repo / name).write_text(content, encoding="utf-8")
        git(repo, "add", name)
        git(repo, "commit", "-m", message)

    def publish(self):
        self.commit(self.seed, "feature.py", "NEW = True\n", "advance")
        git(self.seed, "push", "origin", "main")
        return git(self.seed, "rev-parse", "--short", "HEAD").stdout.strip()

    def head(self):
        return git(self.clone, "rev-parse", "--short", "HEAD").stdout.strip()

    def test_check_refused_from_lan(self):
        status, _, data = self.request(self.lan_port, "GET", "/api/update",
                                       headers={"Cookie": self.lan_device_cookie()})
        self.assertEqual(status, 403)
        self.assertIn("host computer", json.loads(data)["error"])

    def test_update_action_refused_from_lan(self):
        status, _, _ = self.request(self.lan_port, "POST", "/api/process",
                                    body={"action": "update"},
                                    headers={"Cookie": self.lan_device_cookie(),
                                             "Origin": self.lan_origin()})
        self.assertEqual(status, 403)
        self.assertEqual(self.restarts, [])

    def test_check_reports_parity_then_behind(self):
        status, _, data = self.request(self.loop_port, "GET", "/api/update")
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertEqual(payload["behind"], 0)
        self.assertEqual(payload["current"], payload["remote"])
        target = self.publish()
        # A fresh updater stands in for a fresh process: no fetch cache.
        server.updater = Updater(self.clone)
        status, _, data = self.request(self.loop_port, "GET", "/api/update")
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertEqual(payload["behind"], 1)
        self.assertEqual(payload["remote"], target)

    def test_unchanged_update_keeps_the_running_download_job_intact(self):
        with mock.patch.object(server, "worker_running", return_value=True), \
                mock.patch.object(server, "stop_worker") as stop, \
                mock.patch.object(server, "start_worker") as start:
            result = server.perform_update()
        self.assertEqual(result["updating_to"], "")
        stop.assert_not_called()
        start.assert_not_called()

    def test_update_defers_without_stopping_an_active_job(self):
        prior = self.head()
        self.publish()
        with mock.patch.object(server, "worker_running", side_effect=lambda kind: kind == "downloads"), \
                mock.patch.object(server, "stop_worker") as stop:
            with self.assertRaisesRegex(UpdateError, "Update waiting for idle"):
                server.perform_update()
        stop.assert_not_called()
        self.assertEqual(self.head(), prior)

    def test_merge_failure_releases_the_idle_lease(self):
        def fail_merge(*, before_merge):
            before_merge()
            self.assertTrue(server.update_draining())
            raise UpdateError("The merge failed.")

        with mock.patch.object(server.updater, "update", side_effect=fail_merge):
            with self.assertRaisesRegex(UpdateError, "The merge failed"):
                server.perform_update()
        self.assertFalse(server.update_draining())

    def test_update_action_fast_forwards_and_schedules_restart(self):
        target = self.publish()
        status, _, data = self.request(self.loop_port, "POST", "/api/process",
                                       body={"action": "update"})
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertEqual(payload["updating_to"], target)
        self.assertEqual(self.restarts, [True])
        self.assertTrue((self.clone / "feature.py").is_file())
        self.assertEqual(self.head(), target)

    def test_update_action_refuses_dirty_tree_and_changes_nothing(self):
        self.publish()
        (self.clone / "app.py").write_text("VERSION = 'local edit'\n",
                                          encoding="utf-8")
        before = self.head()
        status, _, data = self.request(self.loop_port, "POST", "/api/process",
                                       body={"action": "update"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(data)["error"], DIRTY_TREE)
        self.assertEqual(self.head(), before)
        self.assertFalse((self.clone / "feature.py").exists())
        self.assertEqual(self.restarts, [])

    def test_update_action_refuses_non_checkout(self):
        plain = Path(self.tmp.name) / "plain-app"
        plain.mkdir(exist_ok=True)
        server.updater = Updater(plain)
        status, _, data = self.request(self.loop_port, "POST", "/api/process",
                                       body={"action": "update"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(data)["error"], NOT_A_CHECKOUT)
        self.assertEqual(self.restarts, [])

    def test_restart_uses_the_servers_own_arguments(self):
        argv = [sys.executable, str(Path(server.__file__).resolve()), *sys.argv[1:]]
        with mock.patch.dict(os.environ, {"LISTENING_ROOM_SERVICE": "", "SYSTEMD_EXEC_PID": ""}), \
             mock.patch.object(server, "spawn_restarter") as spawn, \
             mock.patch.object(server.threading, "Timer") as timer:
            self._schedule()
        spawn.assert_called_once_with(os.getpid(), argv, server.REPO_ROOT, self.config_path.parent / "server.log")
        timer.assert_called_once_with(1.0, os._exit, args=(0,))
        timer.return_value.start.assert_called_once_with()

    def test_systemd_update_relaunches_in_the_managed_process(self):
        argv = [sys.executable, str(Path(server.__file__).resolve()), *sys.argv[1:]]
        for marker, main_pid in (("systemd", ""), ("", str(os.getpid()))):
            with self.subTest(marker=marker), mock.patch.dict(os.environ, {
                "LISTENING_ROOM_SERVICE": marker, "SYSTEMD_EXEC_PID": main_pid
            }), mock.patch.object(server, "spawn_restarter") as spawn, mock.patch.object(server.threading, "Timer") as timer:
                self._schedule()
                spawn.assert_not_called()
                timer.assert_called_once_with(1.0, os.execv, args=(sys.executable, argv))
                timer.return_value.start.assert_called_once_with()


class TracksPagingPinTests(ServerFixture, unittest.TestCase):
    """Plan 018 (PERF-01): the track listing's payloads, pinned whole.

    Written against the pre-refactor handler, which builds a full
    track dict for every match before slicing; it must pass unchanged
    after the two-phase refactor (cheap predicates first, track()
    only for the sliced page). The seed is larger than one page at
    the test's page size, with mixed statuses, genres, a queued
    request that overrides one status and fails to override a
    verified one, and an inactive record that must never appear.
    """

    # index, artist, title, album, length, uri,
    # (genre, canonical, source) or None, coverage status, reason, active
    SEED = [
        (1, "Alpha Band", "First Song", "Debut", 201, "spotify:track:" + "A" * 22,
         ("Rock", "rock", "file"), "verified", "", True),
        (2, "Alpha Band", "Second Song", "Debut", 202, "",
         ("Rock", "rock", "file"), "pending", "", True),
        (3, "Beta Singer", "Third Song", "Solo", 203, "",
         ("Pop", "pop", "musicbrainz"), "verified", "", True),
        (4, "Beta Singer", "Fourth Song", "Solo", 204, "",
         None, "pending", "", True),
        (5, "Gamma Trio", "Fifth Song", "Live", 205, "",
         None, "unmatched", "no match", True),
        (6, "Gamma Trio", "Sixth Song", "Live", 206, "",
         ("Jazz Fusion", "jazz", "file"), "verified", "", True),
        (7, "Delta Poet", "Seventh Song", "Words", 207, "",
         ("Pop", "pop", "listenbrainz"), "rejected", "low bitrate", True),
        (8, "Delta Poet", "Eighth Song", "Words", 208, "",
         None, "pending", "", True),
        (9, "Echo Park", "Ninth Song", "Echoes", 209, "",
         ("Rock", "rock", "manual"), "verified", "", True),
        (10, "Echo Park", "Tenth Song", "Echoes", 210, "",
         None, "verified", "", True),
        (11, "Foxtrot Club", "Eleventh Song", "Dance", 211, "",
         ("Electronic", "electronic", "musicbrainz"), "pending", "", True),
        (12, "Foxtrot Club", "Twelfth Song", "Dance", 212, "",
         None, "unmatched", "no match", True),
        (13, "Golf Hotel", "Thirteenth Song", "Rooms", 213, "",
         ("Jazz Fusion", "jazz", "file"), "pending", "", True),
        (14, "Golf Hotel", "Fourteenth Song", "Rooms", 214, "",
         ("Rock", "rock", "file"), "verified", "", False),
    ]
    # Enqueued requests: record 4 (pending) shows "queued"; record 10
    # is verified, and a queued request never overrides verified.
    REQUESTED = (4, 10)
    EXPECTED_STATUS = {1: "verified", 2: "pending", 3: "verified", 4: "queued",
                       5: "unmatched", 6: "verified", 7: "rejected", 8: "pending",
                       9: "verified", 10: "verified", 11: "pending",
                       12: "unmatched", 13: "pending"}
    GENRES_FACET = ["electronic", "jazz", "pop", "rock"]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        state = Path(server.STATE)
        state.mkdir(parents=True, exist_ok=True)
        records = [{"index": index, "key": f"key-{index}", "uri": uri,
                    "artist": artist, "title": title, "album": album,
                    "length": length, "isrc": "", "first_playlist": "mix",
                    "active": active}
                   for index, artist, title, album, length, uri, _genre,
                   _status, _reason, active in cls.SEED]
        manifest = {"records": records,
                    "playlists": {"mix": [row[0] for row in cls.SEED],
                                  "few": [3, 4, 9]},
                    "issues": [],
                    "covers": {cls.SEED[0][5]: "https://img.example/cover1.jpg"},
                    "list_names": {"mix": "Mix", "few": "Few"}}
        store = CatalogStore(server.DB)
        store.write_build(manifest)
        store.write_coverage(
            [{"index": index, "status": status, "artist": artist,
              "title": title, "uri": uri, "path": "", "reason": reason}
             for index, artist, title, _album, _length, uri, _genre,
             status, reason, _active in cls.SEED])
        genre_store = GenreStore(server.DB)
        for row in cls.SEED:
            if row[6]:
                genre_store.set(f"key-{row[0]}", row[6][0], row[6][2])
        server.catalog.reload()
        server.catalog.refresh()
        store = server.requests_store()
        store.initialize()
        for index in cls.REQUESTED:
            row = cls.SEED[index - 1]
            store.enqueue(index, row[1], row[2], row[3], row[4], row[2])

    @classmethod
    def expected_track(cls, index):
        row = cls.SEED[index - 1]
        _i, artist, title, album, length, uri, genre, _status, reason, _active = row
        genre_raw, canonical, source = genre or ("", "", "")
        status = cls.EXPECTED_STATUS[index]
        playlists = ["mix"] + (["few"] if index in (3, 4, 9) else [])
        return {"id": index, "artist": artist, "title": title, "album": album,
                "genre": genre_raw, "genre_canonical": canonical,
                "genre_source": source, "favorite": False, "duration": length,
                "cover": "https://img.example/cover1.jpg" if index == 1 else "",
                "status": status, "playlists": playlists,
                "audio": f"/api/audio/{index}" if status == "verified" else None,
                "spotify_url": ("https://open.spotify.com/track/" + "A" * 22
                                if index == 1 else ""),
                "reason": reason}

    def fetch(self, params):
        status, _, data = self.request(self.loop_port, "GET",
                                       "/api/tracks?" + urlencode(params))
        self.assertEqual(status, 200)
        return json.loads(data)

    def test_tracks_pages_are_identical_before_and_after_the_slice(self):
        # (params, expected page ids, expected total match count)
        combos = [
            ({}, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13], 13),
            ({"limit": 5, "offset": 0}, [1, 2, 3, 4, 5], 13),
            ({"limit": 5, "offset": 5}, [6, 7, 8, 9, 10], 13),
            ({"limit": 5, "offset": 10}, [11, 12, 13], 13),
            ({"limit": 5, "offset": 50}, [], 13),
            ({"q": "alpha"}, [1, 2], 2),
            ({"q": "rock"}, [1, 2, 9], 3),
            ({"q": "thirteenth"}, [13], 1),
            ({"filter": "ready"}, [1, 3, 6, 9, 10], 5),
            ({"filter": "missing"}, [2, 4, 5, 7, 8, 11, 12, 13], 8),
            ({"filter": "requests"}, [4], 1),
            ({"genre": "rock"}, [1, 2, 9], 3),
            ({"genre": "jazz"}, [6, 13], 2),
            ({"genre": "rock", "filter": "ready"}, [1, 9], 2),
            ({"genre": "pop", "q": "song"}, [3, 7], 2),
            ({"playlist": "few"}, [3, 4, 9], 3),
            ({"playlist": "mix", "q": "golf"}, [13], 1),
        ]
        for params, ids, total in combos:
            with self.subTest(params=params):
                payload = self.fetch(params)
                self.assertEqual(payload, {
                    "tracks": [self.expected_track(index) for index in ids],
                    "total": total,
                    "offset": params.get("offset", 0),
                    "limit": params.get("limit", 60),
                    "genres": self.GENRES_FACET})

    def test_tracks_repeated_genre_parameter_unions(self):
        status, _, data = self.request(
            self.loop_port, "GET",
            "/api/tracks?" + urlencode([("genre", "rock"), ("genre", "jazz")]))
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertEqual(payload, {
            "tracks": [self.expected_track(index) for index in [1, 2, 6, 9, 13]],
            "total": 5,
            "offset": 0,
            "limit": 60,
            "genres": self.GENRES_FACET})


class _SpotifyOffline:
    def status(self):
        return {"connected": False}

    def start(self):
        raise AssertionError("the watchdog must not start an offline adapter")


class CatalogFreshnessTests(ServerFixture, unittest.TestCase):
    """Plan 018 (PERF-07): GET handlers serve the catalog as of the
    last watchdog pass. A coverage change in the database is invisible
    to GETs until a pass (or an explicit catalog.refresh(), the pass's
    first step) advances the read-path state."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        state = Path(server.STATE)
        state.mkdir(parents=True, exist_ok=True)
        record = {"index": 1, "key": "key-1", "uri": "", "artist": "Alpha Band",
                  "title": "First Song", "album": "Debut", "length": 201,
                  "isrc": "", "first_playlist": "mix", "active": True}
        manifest = {"records": [record], "playlists": {"mix": [1]},
                    "issues": [], "covers": {}, "list_names": {"mix": "Mix"}}
        CatalogStore(server.DB).write_build(manifest)
        cls.write_coverage(state, "pending")
        server.catalog.reload()
        server.catalog.refresh()

    @staticmethod
    def write_coverage(state, status):
        CatalogStore(server.DB).write_coverage(
            [{"index": 1, "status": status, "artist": "Alpha Band",
              "title": "First Song", "uri": "", "path": "", "reason": ""}])

    def settle(self, status):
        self.write_coverage(Path(server.STATE), status)
        server.catalog.refresh()
        self.assertEqual(self.track_status(), status)

    def track_status(self):
        status, _, data = self.request(self.loop_port, "GET", "/api/tracks")
        self.assertEqual(status, 200)
        return json.loads(data)["tracks"][0]["status"]

    def test_get_does_not_refresh_the_catalog(self):
        self.settle("pending")
        self.write_coverage(Path(server.STATE), "verified")
        # The stored coverage moved on, but no watchdog pass has run:
        # the GET still serves the status as of the last pass.
        self.assertEqual(self.track_status(), "pending")
        server.catalog.refresh()
        self.assertEqual(self.track_status(), "verified")

    def test_downloaded_request_offers_play_only_when_the_session_and_range_are_ready(self):
        self.settle("pending")
        store = server.requests_store()
        request_id = store.enqueue(1, "Alpha Band", "First Song", "Debut", 201, "")
        filename = "1 - First Song.flac"
        server.LIBRARY.mkdir(parents=True, exist_ok=True)
        (server.LIBRARY / filename).write_bytes(b"fLaC-test")
        store.finish(request_id, "downloaded", "Ready to play", filename)
        CatalogStore(server.DB).write_coverage([{
            "index": 1, "status": "verified", "artist": "Alpha Band", "title": "First Song",
            "path": filename,
        }])

        def reported_request():
            status, _, data = self.request(self.loop_port, "GET", "/api/requests")
            self.assertEqual(status, 200)
            return next(row for row in json.loads(data)["requests"] if row["id"] == request_id)

        with mock.patch.object(server, "MAINTENANCE_WAKE", threading.Event()) as wake:
            preparing = reported_request()
            self.assertTrue(wake.is_set())
        self.assertEqual(preparing["message"], "Preparing playback")
        self.assertNotEqual(preparing["status"], "downloaded")
        self.assertNotIn("audio", preparing)
        self.assertEqual(store.get(request_id)["status"], "downloaded")

        server.catalog.refresh()
        ready = reported_request()
        self.assertEqual(ready["status"], "downloaded")
        status, _, data = self.request(self.loop_port, "POST", "/api/session",
                                        body={"op": "play", "playlist": "all", "track_id": 1})
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)["current"]["id"], 1)
        status, _, data = self.request(self.loop_port, "GET", ready["audio"],
                                        headers={"Range": "bytes=0-3"})
        self.assertEqual((status, data), (206, b"fLaC"))

    def test_indexed_replacement_waits_for_the_new_audio_reference(self):
        original = "1 - First Song.flac"
        replacement = "1 - First Song.mp3"
        server.LIBRARY.mkdir(parents=True, exist_ok=True)
        (server.LIBRARY / original).write_bytes(b"fLaC-original")
        (server.LIBRARY / replacement).write_bytes(b"ID3-replacement")
        self.settle("verified")
        CatalogStore(server.DB).update_coverage_path(1, original)
        server.catalog.refresh()
        def first_bytes():
            status, _, data = self.request(self.loop_port, "GET", "/api/audio/1",
                                           headers={"Range": "bytes=0-2"})
            self.assertEqual(status, 206)
            return data
        self.assertEqual(first_bytes(), b"fLa")
        store = server.requests_store()
        request_id = store.enqueue(1, "Alpha Band", "First Song", "Debut", 201, "")
        store.finish(request_id, "downloaded", "Ready to play", replacement)
        CatalogStore(server.DB).update_coverage_path(1, replacement)
        with mock.patch.object(server, "MAINTENANCE_WAKE", threading.Event()) as wake:
            row = next(r for r in server.recent_requests() if r["id"] == request_id)
            self.assertTrue(wake.is_set())
            self.assertNotIn("audio", row)
        server.catalog.refresh()
        ready = next(r for r in server.recent_requests() if r["id"] == request_id)
        self.assertEqual(ready["audio"], "/api/audio/1")
        self.assertEqual(first_bytes(), b"ID3")

    def test_watchdog_pass_refreshes_the_catalog(self):
        self.settle("pending")
        self.write_coverage(Path(server.STATE), "verified")
        self.assertEqual(self.track_status(), "pending")
        with mock.patch.object(server, "refresh_library", lambda force=False: None), \
                mock.patch.object(server, "download_ready", lambda: False), \
                mock.patch.object(server, "spotify", _SpotifyOffline()):
            server._watchdog_pass()
        self.assertEqual(self.track_status(), "verified")


class OnboardingFlagTests(ServerFixture, unittest.TestCase):
    """Plan 020: onboarding completes when at least one list exists in
    the lists folder; the downloader is not part of the flag, so a
    bring-your-own-audio room finishes setup without Soulseek. The
    harness has no config seam for download_ready, so both cases stub
    it with mock.patch.object on the server module (the pattern the
    watchdog tests already use) and run with the downloader down."""

    def settings(self):
        status, _, data = self.request(self.loop_port, "GET", "/api/settings")
        self.assertEqual(status, 200)
        return json.loads(data)

    def test_onboarding_true_with_empty_lists_folder(self):
        with mock.patch.object(server, "download_ready", lambda: False):
            payload = self.settings()
        self.assertEqual(payload["list_files"], [])
        self.assertTrue(payload["onboarding"])

    def test_onboarding_false_with_a_list_and_no_downloader(self):
        target = server.source_dir() / "sample.xspf"
        target.write_text(write_xspf("Sample", [{"title": "Blue in Green",
                                                 "artist": "Miles Davis",
                                                 "album": "Kind of Blue"}]),
                          encoding="utf-8")
        self.addCleanup(target.unlink, missing_ok=True)
        with mock.patch.object(server, "download_ready", lambda: False):
            payload = self.settings()
        # The stub really was in force: the snapshot still reports the
        # downloader as not ready, yet onboarding is complete.
        self.assertFalse(payload["download_ready"])
        self.assertEqual(payload["list_files"], ["sample.xspf"])
        self.assertFalse(payload["onboarding"])


class PairedFirstLoadTests(unittest.TestCase):
    """Plan 025: the paired client's first load. Three regressions,
    one per diagnosed cause: the overview handler recomputed the
    catalog summary once per playlist (at the owner's ~10k records
    that put ~400 ms of CPU on the sidebar's data call); JSON and the
    built assets travelled uncompressed (about 800 KB over the wire
    for a cold load); and a paired client's overview and activity
    must answer promptly while a library refresh is running (the
    2026-11-16 iPad report: pairing succeeded, the data never
    landed). The startup-order half of the fix lives in the
    frontend's startup sequence and is pinned at the source by
    tests/test_startup_order.py (the rendered gate drives a
    host-tier client and cannot exercise the bootstrap path); the
    bootstrap contract it relies on is pinned by
    DeviceAccessTests above."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.workspace = base / "room"
        lists = cls.workspace / "lists"
        lists.mkdir(parents=True)
        for name, count in (("alpha", 40), ("beta", 5), ("gamma", 5)):
            rows = ["title,artist,album"]
            for index in range(count):
                rows.append(f"Song {name} {index},Artist {name},Album {name}")
            (lists / f"{name}.csv").write_text("\n".join(rows) + "\n",
                                               encoding="utf-8")
        config_dir = base / "config"
        config_dir.mkdir(parents=True)
        cls.config_path = config_dir / "config.json"
        cls.config_path.write_text(json.dumps({"folder": str(cls.workspace)}),
                                   encoding="utf-8")
        server.init(cls.config_path)
        server.refresh_library(force=True)
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

    def request(self, port, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        all_headers = dict(headers or {})
        if body is not None:
            all_headers.setdefault("Content-Type", "application/json")
        connection.request(method, path,
                           json.dumps(body) if body is not None else None,
                           all_headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        connection.close()
        return result

    def lan_device_cookie(self, device_id="first-load-device"):
        status, headers, _ = self.request(self.lan_port, "GET",
                                          f"/?token={self.token}")
        self.assertEqual(status, 303)
        bootstrap = headers["Set-Cookie"].split(";", 1)[0]
        status, headers, _ = self.request(
            self.lan_port, "POST", "/api/device",
            body={"id": device_id, "name": "First load"},
            headers={"Cookie": bootstrap,
                     "Origin": f"http://127.0.0.1:{self.lan_port}"})
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_overview_computes_the_summary_once(self):
        from catalog.catalog import Catalog
        computations = []
        original = Catalog.summary.fget

        def counting(catalog):
            computations.append(1)
            return original(catalog)

        with mock.patch.object(Catalog, "summary", property(counting)):
            status, _, data = self.request(self.loop_port, "GET", "/api/overview")
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertEqual(len(payload["playlists"]), 3)
        self.assertEqual(payload["total"], 50)
        # One computation per request, however many playlists: the
        # pre-fix handler read the property inside the playlist loop
        # and paid a full catalog rollup per list.
        self.assertEqual(len(computations), 1)

    def test_json_travels_gzipped_when_the_client_accepts_it(self):
        path = "/api/tracks?playlist=all&offset=0&limit=60"
        status, headers, plain = self.request(self.loop_port, "GET", path)
        self.assertEqual(status, 200)
        self.assertNotIn("Content-Encoding", headers)
        status, headers, packed = self.request(
            self.loop_port, "GET", path, headers={"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertLess(len(packed), len(plain))
        self.assertEqual(json.loads(gzip.decompress(packed)), json.loads(plain))

    def test_an_explicit_gzip_refusal_travels_uncompressed(self):
        # Accept-Encoding: gzip;q=0 is a refusal, not an offer: the
        # payload travels identity even though it is compressible.
        path = "/api/tracks?playlist=all&offset=0&limit=60"
        status, headers, data = self.request(
            self.loop_port, "GET", path,
            headers={"Accept-Encoding": "gzip;q=0"})
        self.assertEqual(status, 200)
        self.assertNotIn("Content-Encoding", headers)
        self.assertEqual(json.loads(data)["total"], 50)

    def test_static_assets_travel_gzipped_and_keep_their_cache_rule(self):
        dist = Path(self.tmp.name) / "dist"
        (dist / "assets").mkdir(parents=True)
        source = ("console.log('listening room');\n" * 200).encode()
        (dist / "assets" / "app-test.js").write_bytes(source)
        with mock.patch.object(server, "DIST", dist):
            status, headers, plain = self.request(
                self.loop_port, "GET", "/assets/app-test.js")
            self.assertEqual(status, 200)
            self.assertEqual(plain, source)
            self.assertIn("immutable", headers.get("Cache-Control", ""))
            status, headers, packed = self.request(
                self.loop_port, "GET", "/assets/app-test.js",
                headers={"Accept-Encoding": "gzip"})
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Content-Encoding"), "gzip")
            self.assertEqual(gzip.decompress(packed), source)
            self.assertIn("immutable", headers.get("Cache-Control", ""))

    def test_paired_data_answers_while_a_refresh_runs(self):
        cookie = self.lan_device_cookie()
        target = server.source_dir() / "alpha.csv"
        original = target.read_text(encoding="utf-8")
        self.addCleanup(target.write_text, original, encoding="utf-8")
        target.write_text(original + "Marker Song,Marker Artist,Marker Album\n",
                          encoding="utf-8")
        refresh = threading.Thread(target=server.refresh_library)
        refresh.start()
        try:
            for path in ("/api/overview", "/api/activity"):
                started = time.monotonic()
                status, _, _ = self.request(self.lan_port, "GET", path,
                                            headers={"Cookie": cookie})
                self.assertEqual(status, 200, path)
                self.assertLess(time.monotonic() - started, 5, path)
        finally:
            refresh.join(timeout=60)
        self.assertFalse(refresh.is_alive())


if __name__ == "__main__":
    unittest.main()
