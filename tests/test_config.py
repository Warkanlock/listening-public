import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import hashlib
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from fixture_environment import isolate_room_environment as setUpModule
from system.config import (AppConfig, default_config_path, default_root,
                           legacy_config_path, resolve_config_path,
                           rewrite_support_paths)

REPO_ROOT = Path(__file__).resolve().parent.parent


class ConfigLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.room = self.base / "room"
        self.config_path = self.base / "appdata" / "config.json"
        self.environ = {"OFFLINE_MUSIC_ROOT": str(self.room)}

    def tearDown(self):
        self.tmp.cleanup()

    def build(self):
        return AppConfig(self.config_path, REPO_ROOT, environ=self.environ)

    def test_load_creates_config_dir_and_generates_token(self):
        config = self.build()
        settings = config.load()
        self.assertTrue(self.config_path.parent.is_dir())
        self.assertTrue(settings["share_token"])
        self.assertEqual(config.root, self.room.resolve())
        self.assertEqual(settings["folder"], str(self.room))

    def test_load_fills_defaults(self):
        settings = self.build().load()
        self.assertIs(settings["auto_download"], False)
        # Plan 062: the media database is the default home for
        # downloaded audio; "files" is chosen, never defaulted.
        self.assertEqual(settings["audio_store"], "database")
        self.assertEqual(settings["source_folders"], [])
        self.assertEqual(settings["source_files"], [])
        self.assertEqual(settings["spotify_client_id"], "")
        self.assertEqual(settings["download_engines"], {})

    def test_token_is_generated_once_and_persisted(self):
        config = self.build()
        token = config.load()["share_token"]
        config.save()
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        reloaded = self.build().load()
        self.assertEqual(reloaded["share_token"], token)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["share_token"], token)

    def test_existing_settings_are_kept_and_defaults_filled(self):
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(json.dumps(
            {"folder": str(self.room), "share_token": "known-token", "custom": 7}),
            encoding="utf-8")
        settings = self.build().load()
        self.assertEqual(settings["share_token"], "known-token")
        self.assertEqual(settings["custom"], 7)
        self.assertIn("source_folders", settings)

    def test_missing_token_is_generated_for_existing_file(self):
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(json.dumps({"folder": str(self.room)}), encoding="utf-8")
        settings = self.build().load()
        self.assertTrue(settings["share_token"])

    def test_corrupt_config_falls_back_to_defaults(self):
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text("not json {", encoding="utf-8")
        config = self.build()
        settings = config.load()
        self.assertTrue(settings["share_token"])
        self.assertEqual(settings["folder"], str(self.room))
        self.assertEqual(config.root, self.room.resolve())

    def test_derived_paths(self):
        config = self.build()
        config.load()
        root = self.room.resolve()
        app_dir = self.config_path.parent
        self.assertEqual(config.app_dir, app_dir)
        self.assertEqual(config.state_dir, app_dir)
        self.assertEqual(config.database, app_dir / "requests.sqlite3")
        self.assertEqual(config.state_backup_dir, app_dir / "state-backup")
        self.assertEqual(config.manifest, app_dir / "state-backup/manifest.json")
        self.assertEqual(config.summary, app_dir / "state-backup/summary.json")
        self.assertEqual(config.coverage, app_dir / "state-backup/coverage.csv")
        self.assertEqual(config.library, root / "audio")
        self.assertEqual(config.playlist_output, root / "playlists")
        self.assertEqual(config.runner_log, self.config_path.parent / "runner.log")
        expected_suffix = hashlib.sha256(
            str(self.config_path.resolve()).encode()).hexdigest()[:8]
        self.assertEqual(config.session_suffix, expected_suffix)

    def test_path_resolution_from_environment(self):
        self.assertEqual(resolve_config_path({"LISTENING_ROOM_CONFIG": "/tmp/x.json"}),
                         Path("/tmp/x.json"))
        self.assertEqual(default_root({"OFFLINE_MUSIC_ROOT": "/tmp/room"}), Path("/tmp/room"))


class SupportFolderTests(unittest.TestCase):
    """Plan 032: the ListeningMusic rename and legacy adoption."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.support = self.home / "Library/Application Support"
        self.new_dir = self.support / "ListeningMusic"
        self.legacy_dir = self.support / "OfflineMusic"
        self.new_config = self.new_dir / "listening-room.json"
        self.legacy_config = self.legacy_dir / "listening-room.json"
        self.workspace = self.base / "spotify_offline"
        self.environ = {"OFFLINE_MUSIC_ROOT": str(self.base / "fallback-room")}

    def tearDown(self):
        self.tmp.cleanup()

    def build(self):
        return AppConfig(self.new_config, REPO_ROOT, environ=self.environ)

    def write_legacy(self, settings, devices=None):
        self.legacy_dir.mkdir(parents=True)
        text = json.dumps(settings)
        self.legacy_config.write_text(text, encoding="utf-8")
        if devices is not None:
            (self.legacy_dir / "devices.json").write_text(
                json.dumps(devices), encoding="utf-8")
        return text

    def test_darwin_default_keeps_legacy_host_until_new_config_is_published(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            current = self.support / "Listening/host/listening-room.json"
            self.assertEqual(default_config_path(platform="darwin"), current)
            self.new_dir.mkdir(parents=True)
            self.new_config.touch()
            self.assertEqual(default_config_path(platform="darwin"), self.new_config)
            (self.support / "Listening/collection").mkdir(parents=True)
            self.assertEqual(default_config_path(platform="darwin"), self.new_config)
            current.parent.mkdir()
            current.touch()
            self.assertEqual(default_config_path(platform="darwin"), current)
            self.assertEqual(default_config_path(platform="linux"),
                             self.home / ".config/listening-room/config.json")

    def test_legacy_counterpart_only_for_the_support_folder(self):
        self.assertEqual(legacy_config_path(self.new_config), self.legacy_config)
        self.assertIsNone(
            legacy_config_path(self.home / ".config/listening-room/config.json"))
        self.assertIsNone(legacy_config_path(self.base / "appdata/config.json"))

    def test_rewrite_support_paths(self):
        settings = {"folder": str(self.legacy_dir / "workspace"),
                    "playlist_output": str(self.legacy_dir / "local.m3u8"),
                    "reports_folder": str(self.legacy_dir),
                    "unrelated": "/elsewhere/file",
                    "count": 3}
        rewrite_support_paths(settings, self.legacy_dir, self.new_dir)
        self.assertEqual(settings["folder"], str(self.legacy_dir / "workspace"))
        self.assertEqual(settings["playlist_output"],
                         str(self.new_dir / "local.m3u8"))
        self.assertEqual(settings["reports_folder"], str(self.new_dir))
        self.assertEqual(settings["unrelated"], "/elsewhere/file")
        self.assertEqual(settings["count"], 3)

    def test_adoption_copies_config_and_devices_and_rewrites(self):
        devices = {"dev-1": {"name": "iPad", "credential_hash": "ab" * 32,
                             "paired_at": 1.0, "last_seen_at": 2.0}}
        legacy_text = self.write_legacy(
            {"folder": str(self.workspace), "share_token": "invented-token-032",
             "playlist_output": str(self.legacy_dir / "local.m3u8"),
             "custom": 7},
            devices=devices)
        config = self.build()
        settings = config.load()
        self.assertEqual(settings["share_token"], "invented-token-032")
        self.assertEqual(settings["folder"], str(self.workspace))
        self.assertEqual(settings["playlist_output"],
                         str(self.new_dir / "local.m3u8"))
        self.assertEqual(settings["custom"], 7)
        self.assertEqual(config.root, self.workspace.resolve())
        self.assertEqual(stat.S_IMODE(self.new_config.stat().st_mode), 0o600)
        new_devices = self.new_dir / "devices.json"
        self.assertEqual(json.loads(new_devices.read_text(encoding="utf-8")),
                         devices)
        self.assertEqual(stat.S_IMODE(new_devices.stat().st_mode), 0o600)
        # The legacy folder is left untouched as a backup.
        self.assertEqual(self.legacy_config.read_text(encoding="utf-8"),
                         legacy_text)
        self.assertTrue((self.legacy_dir / "devices.json").is_file())

    def test_new_config_wins_and_legacy_is_ignored(self):
        self.write_legacy({"folder": str(self.workspace),
                           "share_token": "legacy-token"},
                          devices={"dev-1": {"credential_hash": "cd" * 32}})
        self.new_dir.mkdir(parents=True)
        new_text = json.dumps({"folder": str(self.workspace),
                               "share_token": "new-token"})
        self.new_config.write_text(new_text, encoding="utf-8")
        settings = self.build().load()
        self.assertEqual(settings["share_token"], "new-token")
        self.assertEqual(self.new_config.read_text(encoding="utf-8"), new_text)
        self.assertFalse((self.new_dir / "devices.json").exists())

    def test_neither_exists_gives_fresh_defaults(self):
        settings = self.build().load()
        self.assertTrue(settings["share_token"])
        self.assertEqual(settings["folder"], str(self.base / "fallback-room"))
        self.assertFalse(self.legacy_dir.exists())


if __name__ == "__main__":
    unittest.main()
