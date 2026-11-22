"""Exercise owner sign-in over fixture HTTP listeners with no live config."""

import http.client
import json
import os
import socketserver
import stat
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fixture_environment import isolate_room_environment as setUpModule
from test_http_server import ServerFixture, server
from system import owner_auth


PASSWORD = "Fixture owner password"


class OwnerAuthTests(ServerFixture, unittest.TestCase):
    def setUp(self):
        self.auth = owner_auth.OwnerAuth(self.config_path.parent / self._testMethodName)
        override = mock.patch.object(server, "owner_auth", self.auth)
        override.start()
        self.addCleanup(override.stop)

    def call(self, method, route, body=None, **headers):
        return self.request(self.loop_port, method, route, body, headers)

    def credentials(self, password=PASSWORD):
        return {"username": "homeowner", "password": password}

    def authenticate(self, password=PASSWORD):
        return self.call("POST", "/api/account/login", self.credentials(password), Origin=self.origin)

    def cookie(self, response):
        self.assertEqual(response[0], 200, response[2])
        return response[1]["Set-Cookie"].split(";", 1)[0]

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.loop_port}"

    def test_required_mode_does_not_grant_access_before_owner_setup(self):
        with mock.patch.object(server, "REQUIRE_OWNER", True):
            state = self.call("GET", "/api/account")
            self.assertEqual(json.loads(state[2]), {"enabled": True, "configured": False, "role": "anonymous"})
            self.assertEqual(self.call("GET", "/api/settings")[0], 401)
            self.assertEqual(self.call("GET", "/api/overview")[0], 401)
            self.assertEqual(self.authenticate()[0], 503)

            dist = self.config_path.parent / "fixture-dist"
            dist.mkdir()
            dist.joinpath("index.html").write_text("<title>Sign in fixture</title>")
            with mock.patch.object(server, "DIST", dist):
                status, headers, body = self.call("GET", "/", Accept="text/html")
            self.assertEqual((status, body), (200, b"<title>Sign in fixture</title>"))
            self.assertEqual(headers["Content-Security-Policy"], "frame-ancestors 'none'")

    def test_existing_pairing_retains_listener_access_without_loopback_owner_privileges(self):
        listener = self.lan_device_cookie("owner-mode-listener")
        self.auth.set_password("homeowner", PASSWORD)

        self.assertEqual(self.call("GET", "/api/overview")[0], 401)
        self.assertEqual(self.call("GET", "/api/overview", Cookie=listener)[0], 200)
        account = self.call("GET", "/api/account", Cookie=listener)
        self.assertEqual(json.loads(account[2])["role"], "listener")
        response = self.call("GET", "/api/settings", Cookie=listener)
        settings = json.loads(response[2])
        self.assertEqual(response[0], 200)
        self.assertEqual((settings["lan_url"], settings["folder"]), ("", ""))
        for route in ("/api/browse", "/api/export/settings", "/api/spotify/playlists"):
            with self.subTest(route=route):
                self.assertEqual(self.call("GET", route, Cookie=listener)[0], 403)
        self.assertEqual(self.call("POST", "/api/pairing/rotate", {}, Cookie=listener, Origin=self.origin)[0], 403)

    def test_compose_update_requires_a_signed_in_owner_and_matching_origin(self):
        listener = self.lan_device_cookie("compose-update-listener")
        self.auth.set_password("homeowner", PASSWORD)
        current, target = "a" * 40, "b" * 40
        requests = []

        class UpdaterSocket(socketserver.StreamRequestHandler):
            def handle(self):
                request = json.loads(self.rfile.readline())
                requests.append(request)
                response = ({"current": current, "remote": target, "behind": 1, "checked_at": 1}
                            if request["action"] in {"check", "status"}
                            else {"status": "Building the image", "updating_to": target})
                self.wfile.write(json.dumps(response).encode() + b"\n")

        with tempfile.TemporaryDirectory(prefix="listening-update-", dir="/tmp") as directory:
            path = str(Path(directory) / "control.sock")
            with socketserver.UnixStreamServer(path, UpdaterSocket) as updater:
                worker = threading.Thread(target=updater.serve_forever)
                worker.start()
                try:
                    with mock.patch.dict(os.environ, {"LISTENING_ROOM_INSTALL_KIND": "docker",
                                                      "LISTENING_ROOM_UPDATE_SOCKET": path,
                                                      "LISTENING_ROOM_IMAGE_REVISION": current}):
                        origin = self.lan_origin()
                        with mock.patch.object(server, "owner_auth", None), mock.patch.object(server, "REQUIRE_OWNER", False):
                            self.assertEqual(self.call("GET", "/api/update")[0], 403)
                            self.assertEqual(self.call("POST", "/api/process", {"action": "update", "revision": target})[0], 403)
                        login = self.request(self.lan_port, "POST", "/api/account/login",
                                             self.credentials(), {"Origin": origin})
                        owner = self.cookie(login)
                        headers = {"Cookie": owner, "Origin": origin}
                        action = {"action": "update", "revision": target}
                        for method, route, body in (("GET", "/api/update", None),
                                                    ("POST", "/api/process", action)):
                            denied = self.request(self.lan_port, method, route, body,
                                                  {"Cookie": listener, "Origin": origin})
                            self.assertEqual(denied[0], 403)
                        cross_origin = self.request(self.lan_port, "POST", "/api/process", action,
                                                    {"Cookie": owner, "Origin": "http://other.example"})
                        self.assertEqual(cross_origin[0], 403)
                        settings = self.request(self.lan_port, "GET", "/api/settings", headers=headers)
                        self.assertTrue(json.loads(settings[2])["managed_updates"])
                        self.assertEqual(json.loads(settings[2])["version"], current)
                        check = self.request(self.lan_port, "GET", "/api/update", headers=headers)
                        self.assertEqual(json.loads(check[2])["remote"], target)
                        status = self.request(self.lan_port, "GET", "/api/update?status=1", headers=headers)
                        self.assertEqual(json.loads(status[2])["current"], current)
                        apply = self.request(self.lan_port, "POST", "/api/process", action, headers)
                        self.assertEqual(json.loads(apply[2])["updating_to"], target)
                        self.assertEqual(requests, [{"action": "check"}, {"action": "status"},
                                                    {"action": "apply", "revision": target}])
                finally:
                    updater.shutdown()
                    worker.join()

    def test_https_origin_determines_cookie_security_and_browser_authority(self):
        self.auth.set_password("homeowner", PASSWORD)
        host = "room.example.test"
        origin = f"https://{host}"
        forwarded = {"X-Forwarded-Host": host, "X-Forwarded-Proto": "https", "X-Forwarded-For": "127.0.0.1"}
        with mock.patch.object(server, "ROOM_ORIGIN", origin):
            self.assertEqual(self.call("GET", "/api/export/settings", Host=host, **forwarded)[0], 401)
            self.assertEqual(self.call("GET", "/api/account", Host="unapproved.example.test", **forwarded)[0], 400)
            for invalid in (None, f"http://{host}", "https://other.example.test"):
                with self.subTest(origin=invalid):
                    headers = {"Origin": invalid} if invalid is not None else {}
                    denied = self.call("POST", "/api/account/login", self.credentials(), Host=host, **headers, **forwarded)
                    self.assertEqual(denied[0], 403)
            response = self.call("POST", "/api/account/login", self.credentials(), Host=host, Origin=origin)
            cookie = self.cookie(response)
            options = response[1]["Set-Cookie"].split("; ")[1:]
            self.assertTrue({"Secure", "HttpOnly"}.issubset(options))
            settings = self.call("GET", "/api/settings", Host=host, Cookie=cookie)
            self.assertEqual(settings[0], 200)
            self.assertEqual(json.loads(settings[2])["spotify_redirect_uri"], f"{origin}/api/spotify/callback")
            for headers in ({}, {"Origin": f"http://{host}"}):
                with self.subTest(headers=headers):
                    denied = self.call("POST", "/api/settings", {}, Host=host, Cookie=cookie, **headers)
                    self.assertEqual(denied[0], 403)

            uploaded = self.call("GET", f"/?token={self.token}", Host=host)
            self.assertEqual(uploaded[0], 303)
            self.assertIn("; Secure", uploaded[1]["Set-Cookie"])
            bootstrap = uploaded[1]["Set-Cookie"].split(";", 1)[0]
            state = self.call("GET", "/api/account", Host=host, Cookie=bootstrap)
            self.assertEqual(json.loads(state[2])["role"], "bootstrap")
            listener_response = self.call("POST", "/api/device", {"id": "secure-cookie-device"},
                                          Host=host, Origin=origin, Cookie=bootstrap)
            listener = self.cookie(listener_response)
            self.assertIn("; Secure", listener_response[1]["Set-Cookie"])
            self.assertEqual(self.call("GET", "/api/overview", Host=host, Cookie=listener)[0], 200)

    def test_session_survives_store_reopen_and_logout_revokes_it(self):
        self.auth.set_password("homeowner", PASSWORD)
        login = self.authenticate()
        cookie = self.cookie(login)
        self.assertIn(f"; Max-Age={365 * 24 * 60 * 60}", login[1]["Set-Cookie"])
        self.assertEqual(stat.S_IMODE(self.auth.path.stat().st_mode), 0o600)
        payload = self.auth.path.read_bytes()
        self.assertNotIn(PASSWORD.encode(), payload)
        self.assertNotIn(cookie.split("=", 1)[1].encode(), payload)

        reopened = owner_auth.OwnerAuth(self.auth.path.parent)
        with mock.patch.object(server, "owner_auth", reopened):
            self.assertEqual(self.call("GET", "/api/export/settings", Cookie=cookie)[0], 200)
            response = self.call("POST", "/api/account/logout", {}, Cookie=cookie, Origin=self.origin)
            self.assertEqual(response[0], 200)
            self.assertIn("; Max-Age=0", response[1]["Set-Cookie"])
            self.assertEqual(self.call("GET", "/api/export/settings", Cookie=cookie)[0], 401)

    def test_opening_room_renews_existing_account_sessions_near_expiry(self):
        self.auth.set_password("homeowner", PASSWORD)
        self.auth.household.add_member("listener", PASSWORD)
        day = 24 * 60 * 60
        for username, role, cookie_name in (("homeowner", "owner", owner_auth.COOKIE_NAME),
                                             ("listener", "member", owner_auth.MEMBER_COOKIE_NAME)):
            with self.subTest(role=role):
                with mock.patch.object(owner_auth, "SESSION_AGE", day):
                    _, token = self.auth.sign_in(username, PASSWORD)
                cookie = f"{cookie_name}={token}"
                status, headers, body = self.call("GET", "/api/account", Cookie=cookie)
                self.assertEqual((status, json.loads(body)["role"]), (200, role))
                self.assertEqual(headers["Set-Cookie"].split(";", 1)[0], cookie)
                self.assertIn(f"; Max-Age={owner_auth.SESSION_AGE}", headers["Set-Cookie"])

                later = SimpleNamespace(time=lambda: time.time() + 2 * day)
                with mock.patch.object(owner_auth, "time", later):
                    status, headers, body = self.call("GET", "/api/account", Cookie=cookie)
                self.assertEqual((status, json.loads(body)["role"]), (200, role))
                # The browser can still adopt the extended expiry if it missed the first response.
                self.assertEqual(headers["Set-Cookie"].split(";", 1)[0], cookie)
                age = int(headers["Set-Cookie"].split("Max-Age=", 1)[1].split(";", 1)[0])
                self.assertGreater(age, owner_auth.SESSION_AGE - 3 * day)
                self.assertLessEqual(age, owner_auth.SESSION_AGE - 2 * day)

    def test_password_reset_invalidates_browser_session_and_local_control_credential(self):
        self.auth.set_password("homeowner", PASSWORD)
        cookie = self.cookie(self.authenticate())
        bearer = f"Bearer {self.auth.cli_token()}"
        self.assertEqual(self.call("POST", "/api/devices/revoke", {"id": "absent-device"}, Authorization=bearer)[0], 200)
        self.assertEqual(self.request(self.lan_port, "GET", "/api/settings", headers={"Authorization": bearer})[0], 401)

        self.auth.set_password("homeowner", "A new owner password")
        self.assertEqual(self.call("GET", "/api/settings", Cookie=cookie)[0], 401)
        self.assertEqual(self.call("GET", "/api/settings", Authorization=bearer)[0], 401)
        self.assertEqual(self.authenticate()[0], 401)
        self.assertEqual(self.authenticate("A new owner password")[0], 200)

    def test_revoked_member_cookie_does_not_hide_a_new_owner_sign_in(self):
        self.auth.set_password("homeowner", PASSWORD)
        self.auth.household.add_member("listener", PASSWORD)
        _, token = self.auth.sign_in("listener", PASSWORD)
        old_cookie = f"{owner_auth.MEMBER_COOKIE_NAME}={token}"
        with closing(http.client.HTTPConnection("127.0.0.1", self.loop_port, timeout=10)) as connection:
            connection.request("POST", "/api/account/login", body=json.dumps(self.credentials()),
                               headers={"Origin": self.origin, "Cookie": old_cookie,
                                        "Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            cookies = response.headers.get_all("Set-Cookie")
        owner_cookie = next(cookie.partition(";")[0] for cookie in cookies if cookie.startswith("lr_owner="))
        selection_header = next(cookie for cookie in cookies if cookie.startswith("lr_owner_selected="))
        selection = selection_header.partition(";")[0]
        self.assertIn(f"; Max-Age={owner_auth.SESSION_AGE + owner_auth.SESSION_RENEWAL_WINDOW}", selection_header)
        self.assertIsNone(self.auth.household.session(token))
        self.assertEqual(json.loads(self.call("GET", "/api/account", Cookie=old_cookie)[2])["role"], "anonymous")
        self.assertEqual(self.call("GET", "/api/overview", Cookie=f"{owner_cookie}; {old_cookie}")[0], 401)
        browser_cookies = f"{owner_cookie}; {selection}; {old_cookie}"
        later = SimpleNamespace(time=lambda: time.time() + 24 * 60 * 60)
        with mock.patch.object(owner_auth, "time", later):
            self.assertEqual(self.call("GET", "/api/overview", Cookie=browser_cookies)[0], 200)
        status = self.call("GET", "/api/account", Cookie=browser_cookies)
        self.assertEqual(json.loads(status[2])["role"], "owner")
        with closing(http.client.HTTPConnection("127.0.0.1", self.loop_port, timeout=10)) as connection:
            credentials = {"username": "listener", "password": PASSWORD}
            connection.request("POST", "/api/account/login", body=json.dumps(credentials),
                               headers={"Origin": self.origin, "Cookie": browser_cookies,
                                        "Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(any(cookie.startswith("lr_owner_selected=;") and "Max-Age=0" in cookie
                                for cookie in response.headers.get_all("Set-Cookie")))

    def test_sessions_expire(self):
        self.auth.set_password("homeowner", PASSWORD)
        cookie = self.cookie(self.authenticate())
        expired = SimpleNamespace(time=lambda: time.time() + owner_auth.SESSION_AGE + 1)
        with mock.patch.object(owner_auth, "time", expired):
            self.assertEqual(self.call("GET", "/api/settings", Cookie=cookie)[0], 401)

    def test_repeated_wrong_passwords_temporarily_throttle_login(self):
        self.auth.set_password("homeowner", PASSWORD)
        for _ in range(owner_auth.MAX_LOGIN_FAILURES):
            self.assertEqual(self.authenticate("Wrong owner password")[0], 401)
        response = self.authenticate()
        self.assertEqual(response[0], 429)
        self.assertEqual(response[1]["Retry-After"], str(owner_auth.RETRY_DELAY))
