"""Owner commands at the launcher boundary, with disposable room fixtures."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from fixture_environment import isolate_room_environment as setUpModule
from system import cli, cli_room
from system.tailscale import TailnetNode


def compose_ports(*addresses):
    return json.dumps({"Publishers": [{"URL": host, "TargetPort": 8765, "PublishedPort": 18000, "Protocol": "tcp"}
                                      for host in addresses]})


class RoomCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name) / "config.json"
        env = patch.dict(os.environ, {"HOME": temp.name, "LISTENING_ROOM_CONFIG": str(self.config)}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.info = {"host": "127.0.0.1", "port": 18765, "pid": 123}

    def invoke(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(list(args))
        return code, output.getvalue().strip()

    def test_bare_command_is_branded_and_offline(self):
        with patch.object(cli, "request", side_effect=AssertionError):
            code, text = self.invoke()
        self.assertEqual(code, 0)
        for item in ("LISTENING ROOM", "setup", "status", "invite", "settings", "config"):
            self.assertIn(item, text)
        self.assertNotIn("\033", text)
        self.assertFalse(self.config.exists())

    def test_offline_selection_and_stopped_status(self):
        self.config.write_text('{"share_token":"private fixture"}')
        self.assertEqual(self.invoke("config", "path"), (0, str(self.config.resolve())))
        self.assertNotIn("private fixture", self.invoke("config", "--json")[1])
        with patch.object(cli, "request", return_value=None):
            code, text = self.invoke("status", "--json")
            self.assertEqual((code, json.loads(text)), (1, {"manager": "local", "state": "stopped"}))
            with self.assertRaisesRegex(RuntimeError, "server start"):
                self.invoke("invite")
            self.invoke("config", "manager", "systemd", "--port", "18000")
        self.assertEqual(json.loads(self.invoke("config", "show", "--json")[1])["service_port"], 18000)

    def test_status_reports_selected_listener_and_service_failure(self):
        with patch.object(cli, "request", return_value=self.info), patch.object(cli_room, "api", side_effect=[
            {"total": 3, "verified": 2, "queued_requests": 1}, {"paired_devices": [{}]}
        ]) as api:
            code, result = self.invoke("status", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(result)["songs"], 3)
            self.assertEqual(api.call_args.args[0], self.info["port"])
        with patch.object(cli, "request", return_value=self.info), patch.object(cli_room, "api", side_effect=OSError("offline")):
            code, text = self.invoke("status", "--json")
            self.assertEqual((code, json.loads(text)["state"]), (1, "unhealthy"))

    def test_stopped_doctor_reports_origin_and_never_displays_secrets(self):
        root = self.config.parent / "room"
        self.config.write_text(json.dumps({"share_token": "private fixture", "folder": str(root)}))
        with patch.object(cli, "request", return_value=None), patch.dict(os.environ, {
            "LISTENING_ROOM_ORIGIN": "https://music.example.test:443",
        }):
            code, text = self.invoke("doctor", "--json")
        self.assertEqual(code, 1)
        self.assertNotIn("private fixture", text)
        report = json.loads(text)
        self.assertEqual(report["pairing_origin"], "https://music.example.test")
        self.assertEqual(report["collection"], "No portable collection found")
        self.assertFalse(root.exists())

    def test_settings_are_whitelisted_and_typed(self):
        snapshot = {key.replace("-", "_"): "" for key in cli_room.READABLE}
        snapshot.update(collection={"path": "/fixture"}, auto_download=False, lan_url="private credential")
        with patch.object(cli, "request", return_value=self.info), patch.object(cli_room, "api", return_value=snapshot) as api:
            self.assertNotIn("private credential", self.invoke("settings", "--json", "show")[1])
            self.assertEqual(self.invoke("settings", "get", "auto-download"), (0, "off"))
            self.invoke("settings", "set", "auto-download", "off")
            self.assertEqual(api.call_args.args, (18765, "settings", {"auto_download": False}))
            with self.assertRaisesRegex(RuntimeError, "Portable rooms"):
                self.invoke("settings", "set", "audio-store", "files")
            self.assertEqual(api.call_args.args, (18765, "settings"))
            with self.assertRaisesRegex(RuntimeError, "on or off"):
                self.invoke("settings", "set", "auto-download", "sometimes")

    def test_extensions_call_same_owner_controls_as_web(self):
        with patch.object(cli, "request", return_value=self.info), patch.object(cli_room, "api", return_value={}) as api:
            self.invoke("extensions", "list", "--json")
            self.assertEqual(api.call_args.args, (18765, "extensions"))
            self.invoke("extensions", "radio", "youtube", "off", "--json")
            self.assertEqual(api.call_args.args, (18765, "extensions", {"action": "radio", "provider": "youtube", "enabled": False}))
            self.invoke("extensions", "install", "test-engine", "--json")
            self.assertEqual(api.call_args.args, (18765, "extensions", {"action": "install", "engine": "test-engine"}))
            self.invoke("extensions", "select", "source-search", "source-fetch", "--json")
            self.assertEqual(api.call_args.args, (18765, "extensions",
                {"action": "select", "selection": {"search": "source-search", "fetch": "source-fetch"}}))
            self.invoke("extensions", "clear-selection", "--json")
            self.assertEqual(api.call_args.args, (18765, "extensions", {"action": "select", "selection": {}}))

    def test_loopback_rotation_and_unknown_device_leave_access_unchanged(self):
        with patch.object(cli, "request", return_value=self.info), patch.object(cli_room, "api", return_value={"paired_devices": []}) as api:
            with self.assertRaisesRegex(RuntimeError, "reachable"):
                self.invoke("pair", "rotate")
            api.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "Unknown paired device"):
                self.invoke("pair", "revoke", "unknown-device")
            self.assertEqual(api.call_count, 1)

    def test_join_validates_before_opening_and_keeps_link_out_of_output(self):
        link = "http://192.0.2.1:8765/?token=fixture"
        with patch.object(cli_room.webbrowser, "open", return_value=True) as opened:
            for bad in ("file:///x?token=fixture", "http://user@room/?token=fixture",
                        "http://room:wrong/?token=fixture", link + "&token=second", link + "\n"):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    self.invoke("join", bad)
            opened.assert_not_called()
            with patch.object(cli_room.sys.stdin, "isatty", return_value=True), patch.object(cli_room, "getpass", return_value=link):
                code, text = self.invoke("join")
            self.assertEqual(code, 0)
            self.assertNotIn("fixture", text)
            opened.assert_called_once_with(link)

    def test_compose_invite_uses_host_publication_and_container_owner(self):
        cli.manager_config(self.config).write_text(json.dumps({"kind": "compose", "port": 18000, "project": "fixture", "file": "/fixture"}))
        result = Mock(returncode=0, stdout='{"invitation":"http://172.16.0.2:8765/?token=fixture"}')
        with patch.object(cli, "service_commands", return_value=(["docker", "compose"], lambda: True)), patch.object(
            cli_room.subprocess, "check_output", return_value=compose_ports("192.0.2.10")
        ), patch.object(cli_room.subprocess, "run", return_value=result) as inside:
            self.assertEqual(self.invoke("pairing", "invite"), (0, "http://192.0.2.10:18000/?token=fixture"))
            self.assertIn("/app/listening", inside.call_args.args[0])
        with patch.object(cli, "service_commands", return_value=(["docker", "compose"], lambda: True)), patch.object(
            cli_room.subprocess, "check_output",
            side_effect=[compose_ports("127.0.0.1"), "{}", compose_ports("::"), "{}"],
        ), patch.object(cli_room.subprocess, "run") as inside:
            for mapping in ("loopback", "IPv6 wildcard"):
                with self.subTest(mapping=mapping), self.assertRaisesRegex(RuntimeError, "reachable"):
                    self.invoke("pair", "rotate")
            inside.assert_not_called()

    def test_compose_loopback_https_proxy_preserves_invitation(self):
        cli.manager_config(self.config).write_text(json.dumps({
            "kind": "compose", "port": 18000, "project": "fixture", "file": "/fixture",
        }))
        service = {"environment": {"LISTENING_ROOM_ORIGIN": "https://music.example.test"}}
        config = json.dumps({"services": {"listening-room": service}})
        link = "https://music.example.test/?token=fixture"
        with patch.object(cli, "service_commands", return_value=(["docker", "compose"], lambda: True)), patch.object(
            cli_room.subprocess, "check_output", side_effect=[compose_ports("127.0.0.1"), config]
        ), patch.object(cli_room, "compose_action", return_value={"invitation": link}):
            self.assertEqual(self.invoke("pair", "invite"), (0, link))

    def test_status_handles_control_timeout_and_lan_only_listener(self):
        with patch.object(cli, "request", side_effect=TimeoutError("listener timed out")):
            code, text = self.invoke("status", "--json")
            self.assertEqual((code, json.loads(text)["state"]), (1, "unhealthy"))
        info = {**self.info, "host": "192.0.2.10"}
        with patch.object(cli, "request", return_value=info), patch.object(cli, "ready", return_value=True) as ready:
            code, text = self.invoke("status", "--json")
            self.assertEqual((code, json.loads(text)["state"]), (0, "running"))
            ready.assert_called_once_with("http://192.0.2.10:18765/")
            with self.assertRaisesRegex(RuntimeError, "0.0.0.0"):
                self.invoke("settings")

    def test_compose_home_invite_keeps_the_short_name(self):
        cli.manager_config(self.config).write_text(json.dumps({"kind": "compose", "port": 18000, "project": "fixture", "file": "/fixture"}))
        with patch.object(cli, "service_commands", return_value=(["docker", "compose"], lambda: True)), patch.object(
            cli_room.subprocess, "check_output", return_value=compose_ports("192.168.1.8")
        ), patch.object(cli_room, "compose_action", return_value={"invitation": "http://server:8765/?token=fixture"}):
            for action in ("invite", "rotate"):
                self.assertEqual(self.invoke("pair", action), (0, "http://server:18000/?token=fixture"))

    def test_compose_status_checks_the_published_service(self):
        cli.manager_config(self.config).write_text(json.dumps({"kind": "compose", "port": 18000, "project": "fixture", "file": "/fixture"}))
        with patch.object(cli, "service_commands", return_value=(["docker", "compose"], lambda: True)), patch.object(
            cli_room.subprocess, "check_output", return_value=compose_ports("192.0.2.10")
        ), patch.object(cli_room, "compose_action", return_value={"state": "running"}), patch.object(cli, "ready") as ready:
            ready.return_value = False
            code, text = self.invoke("status", "--json")
            self.assertEqual((code, json.loads(text)["state"]), (1, "unhealthy"))
            ready.return_value = True
            code, text = self.invoke("status", "--json")
            self.assertEqual((code, json.loads(text)["url"]), (0, "http://192.0.2.10:18000/"))

    def test_tailscale_invite_selects_the_tailnet_publication(self):
        cli.manager_config(self.config).write_text(json.dumps({"kind": "compose", "port": 18000, "project": "fixture", "file": "/fixture"}))
        node = TailnetNode("room.family.ts.net", ("100.70.1.2", "fd7a:115c:a1e0::1234"))
        for ip in node.addresses:
            with self.subTest(ip=ip), patch.object(cli, "service_commands", return_value=(["docker", "compose"], lambda: True)), patch.object(
                cli_room.subprocess, "check_output", return_value=compose_ports("192.0.2.10", ip)
            ), patch.object(cli_room, "local_identity", return_value=node), patch.object(
                cli_room, "compose_action", return_value={"invitation": "http://172.16.0.2:8765/?token=fixture"}
            ):
                host = f"[{ip}]" if ":" in ip else ip
                self.assertEqual(self.invoke("invite", "--tailscale"), (0, f"http://{host}:18000/?token=fixture"))
