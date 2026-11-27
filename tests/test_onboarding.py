"""The terminal walkthrough and equivalent unattended client choices."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from fixture_environment import isolate_room_environment as setUpModule
from system import cli, onboarding


class OnboardingTests(unittest.TestCase):
    def test_walkthrough_joins_existing_room_without_provisioning_a_server(self):
        with tempfile.TemporaryDirectory() as work, patch.dict(os.environ, {
            "LISTENING_ROOM_CONFIG": str(Path(work) / "config.json"),
            "LISTENING_ROOM_INSTALL_KIND": "app",
        }), patch.object(sys.stdin, "isatty", return_value=True), patch("builtins.input", side_effect=[
            "phone", "join",
        ]), patch.object(cli.cli_room, "join", return_value=0) as join, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["setup"]), 0)
            join.assert_called_once_with(None)
            self.assertEqual(list(Path(work).iterdir()), [])

    def test_contributor_client_flags_preserve_multiple_targets(self):
        calls = []
        with tempfile.TemporaryDirectory() as work, patch.object(
            cli, "build", side_effect=lambda args: calls.append(args) or 0
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["setup", "--client", "apk", "--client", "dmg", "--output", work]), 0)
            self.assertEqual([a.target for a in calls], ["apk", "dmg"])
            self.assertTrue(all(a.output == Path(work) for a in calls))

    def test_explicit_room_refuses_a_nonempty_legacy_folder_before_building(self):
        with tempfile.TemporaryDirectory() as work, patch.dict(os.environ, {
            "LISTENING_ROOM_CONFIG": str(Path(work) / "config.json"),
        }), patch.object(onboarding.subprocess, "run") as run:
            (Path(work) / "my-source.mp3").write_bytes(b"source")
            with self.assertRaisesRegex(ValueError, "legacy library"):
                cli.main(["setup", "--client", "web", "--room", work, "--start"])
            run.assert_not_called()
            self.assertEqual([p.name for p in Path(work).iterdir()], ["my-source.mp3"])

    def test_fresh_default_host_groups_runtime_and_recognizes_legacy_installations(self):
        with tempfile.TemporaryDirectory() as work, patch.dict(os.environ, {"HOME": work}, clear=True):
            base = cli.default_config_path().resolve()
            selected = cli.selected_config(base)
            self.assertTrue(selected.is_relative_to(base.parent / "rooms"))
            self.assertFalse(base.parent.exists())
            base.parent.mkdir(parents=True)
            (base.parent / "requests.sqlite3").touch()
            self.assertEqual(cli.selected_config(base), base)

    def test_systemd_installer_receives_the_callers_absolute_host_config(self):
        with tempfile.TemporaryDirectory() as work:
            config = Path(work).resolve() / "selected host/config.json"
            room = Path(work) / "room"
            with patch.dict(os.environ, {"HOME": work, "LISTENING_ROOM_CONFIG": os.path.relpath(config)},
                            clear=True), patch.object(sys, "platform", "linux"), patch.object(
                onboarding.subprocess, "run"
            ) as run, redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["setup", "--systemd", "--room", str(room)]), 0)
            self.assertEqual(run.call_args.kwargs["env"]["LISTENING_ROOM_CONFIG"], str(config))
            self.assertEqual(cli.selected_config(config), cli.room_config(config, room))
            self.assertTrue((room / "collection.sqlite3").is_file())

    def test_wizard_keeps_credentials_provisioned_before_first_host_start(self):
        for override in (False, True):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as work, patch.dict(
                os.environ, {"HOME": work, "LISTENING_ROOM_INSTALL_KIND": "app"}, clear=True
            ), patch.object(cli, "install_command"), patch.object(sys.stdin, "isatty", return_value=True), patch(
                "builtins.input", side_effect=["host", "", "skip", "skip", "skip", "no"]
            ), redirect_stdout(io.StringIO()):
                config = cli.default_config_path().resolve()
                if override:
                    config = Path(work).resolve() / "private/config.json"
                    os.environ["LISTENING_ROOM_CONFIG"] = str(config)
                config.parent.mkdir(parents=True)
                credentials = config.parent / "existing-private.conf"
                credentials.write_bytes(b"fixture")
                self.assertEqual(cli.main(["setup"]), 0)
                self.assertEqual(cli.selected_config(config), config)
                self.assertTrue(config.exists())
                self.assertEqual(credentials.read_bytes(), b"fixture")
                self.assertTrue((cli.default_root() / "collection.sqlite3").exists())


    def test_portable_room_gets_fresh_opt_in_defaults_on_new_host(self):
        import json
        from catalog.collection_snapshot import create_empty_collection

        with tempfile.TemporaryDirectory() as work:
            temp = Path(work).resolve()
            base = temp / "config" / "listening-room.json"
            room = temp / "restored"
            create_empty_collection(room)
            with patch.dict(os.environ, {"LISTENING_ROOM_CONFIG": str(base)}), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["collection", "open", str(room)]), 0)
                restored = json.loads(cli.selected_config(base).read_text())
                self.assertEqual(restored["radio_providers"], [])
                self.assertEqual(restored["download_engines"], {})

    def test_unconfigured_compose_address_is_resolved_on_the_host(self):
        with tempfile.TemporaryDirectory() as work:
            temp = Path(work).resolve()
            with patch.dict(os.environ, {"LISTENING_ROOM_CONFIG": str(temp / "config" / "room.json"),
                                        "LISTENING_ROOM_INSTALL_KIND": "docker",
                                        "OFFLINE_MUSIC_ROOT": str(temp / "library"),
                                        "LISTENING_PORT": "18765"}), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["setup", "--non-interactive"]), 0)
                self.assertIn("docker compose port listening-room 8765", out.getvalue())
                self.assertNotIn("http://127.0.0.1:8765", out.getvalue())

    def test_resume_preserves_owner_and_song_index_and_accepts_a_server_address(self):
        import json
        from library import music_intake
        from system.owner_auth import OwnerAuth

        with tempfile.TemporaryDirectory() as work:
            temp = Path(work).resolve()
            config = temp / "config" / "listening-room.json"
            source = temp / "imports"
            room = temp / "library"
            source.mkdir()
            with patch.dict(os.environ, {"LISTENING_ROOM_CONFIG": str(config),
                                        "LISTENING_ROOM_INSTALL_KIND": "docker",
                                        "OFFLINE_MUSIC_ROOT": str(room)}), patch.object(
                sys.stdin, "isatty", return_value=True
            ), patch.object(onboarding, "getpass", return_value="fixture-password-123") as password, patch.object(
                music_intake, "scan_folder", return_value=music_intake.FolderScan(
                    [{"title": "First light", "artist": "Listening Room", "album": "First listen",
                      "duration_ms": 8000}], {}, 262144)
            ), redirect_stdout(io.StringIO()) as out:
                command = ["setup", "--music", str(source), "--origin", "http://listening-fixture.test:8765",
                           "--owner", "fixture-owner", "--non-interactive"]
                self.assertEqual(cli.main(command), 0)
                before = json.loads(config.read_text())
                self.assertEqual(before["radio_providers"], [])
                self.assertEqual(before["download_engines"], {})
                owner = OwnerAuth(config.parent).cli_token()
                index = Path(before["source_files"][0])
                stamp = index.stat().st_mtime_ns
                self.assertEqual(cli.main(command), 0)
                self.assertEqual(password.call_count, 2)
                self.assertEqual(OwnerAuth(config.parent).cli_token(), owner)
                self.assertEqual(json.loads(config.read_text()), before)
                self.assertEqual(index.stat().st_mtime_ns, stamp)
                self.assertTrue(index.is_relative_to(config.parent))
                self.assertFalse(list(source.iterdir()))
                self.assertIn("http://listening-fixture.test:8765/?setup=clients", out.getvalue())
                self.assertIn("http://listening-fixture.test:8765/?q=First+listen", out.getvalue())
