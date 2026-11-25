import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from system import engine_fleet, engine_repository


class FleetStorageTests(unittest.TestCase):
    def setUp(self):
        fixture = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(fixture)
        self.fleet = engine_fleet.Fleet(root / "fleet", root / "staging", root / "catalog.json",
                                       root / "public.pem", "arm64", "0.1")
        self.engine = {"id": "fixture", "capabilities": ["search", "fetch"], "enabled": True,
                       "sources": {"search": ["fixture"], "fetch": ["fixture"]},
                       "server_versions": ["0.1"]}
        self.fleet.engines["fixture"] = self.engine
        self.fleet.mount("fixture")
        self.job = "1" * 32
        (self.fleet.staging / self.job).mkdir()

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL required to sign the fixture catalog")
    def test_signed_catalog_skips_older_entries_without_source_declarations(self):
        private_key = self.fleet.state / "private.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                        "-out", private_key], check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", private_key, "-pubout", "-out", self.fleet.public_key],
                       check=True, capture_output=True)
        entry = {**self.engine, "architecture": "arm64", "version": "1.0",
                 "image": "registry.example/engine@sha256:" + "a" * 64}
        old = {key: value for key, value in entry.items() if key != "sources"}
        foreign = {**entry, "architecture": "ppc64le"}
        self.fleet.catalog.write_text(json.dumps({
            "version": 1, "engines": [{**old, "id": "older"}, foreign, entry],
        }))
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", private_key, "-out",
                        self.fleet.catalog.with_suffix(".sig"), self.fleet.catalog], check=True, capture_output=True)
        available = self.fleet.manage({"action": "catalog"}, None)["engines"]
        self.assertEqual([item["id"] for item in available], ["fixture"])
        self.assertEqual(available[0]["sources"], self.engine["sources"])
        self.assertEqual(self.fleet.manage({"action": "catalog", "server_release": "0.2"}, None)["engines"], [])
        self.fleet.catalog.write_text(self.fleet.catalog.read_text().replace('"fixture"', '"altered"'))
        with self.assertRaisesRegex(ValueError, "signature"):
            self.fleet.manage({"action": "catalog"}, None)

    def test_status_and_acquisition_require_compatibility_with_the_calling_release(self):
        with mock.patch.object(self.fleet, "running_containers", return_value={self.fleet.container("fixture")}), \
                mock.patch.object(self.fleet, "worker") as worker:
            self.assertTrue(self.fleet.manage({"action": "status"}, None)["engines"][0]["ready"])
            request = {"server_release": "0.2", "action": "status"}
            self.assertFalse(self.fleet.manage(request, None)["engines"][0]["ready"])
            result = self.fleet.acquire({**request, "action": "search", "engine": "fixture"}, None)
            self.assertFalse(result["ok"])
            self.assertTrue(result["retryable"])
            newer = {key: value for key, value in self.engine.items() if key != "sources"}
            self.fleet.engines["fixture"] = newer
            self.assertFalse(self.fleet.manage({"action": "status"}, None)["engines"][0]["ready"])
            worker.assert_not_called()

    def test_health_checks_a_running_engine_without_starting_a_disabled_engine(self):
        request = {"action": "health", "engine": "fixture"}
        with (mock.patch.object(self.fleet, "running_containers", return_value={self.fleet.container("fixture")}),
              mock.patch.object(self.fleet, "worker", return_value={"ok": True}) as worker,
              mock.patch.object(self.fleet, "start") as start):
            self.assertTrue(self.fleet.manage({"action": "status"}, None)["health_check"])
            self.assertTrue(self.fleet.manage(request, None)["ok"])
            worker.assert_called_once_with("fixture", {"version": 1, "action": "test"}, None)
            self.fleet.engines["fixture"]["enabled"] = False
            with self.assertRaisesRegex(ValueError, "Enable a compatible engine"):
                self.fleet.manage(request, None)
            worker.assert_called_once()
            start.assert_not_called()

    def test_login_pause_is_shared_by_search_and_fetch_and_survives_manager_restart(self):
        request = {"engine": "fixture", "action": "search", "recording": {}}
        fetch = {"engine": "fixture", "action": "fetch", "candidate": {"source": "fixture"}, "job": self.job}
        failure = {"ok": False, "code": "login_unavailable", "retry_after_seconds": 1800}
        self.fleet.save()
        clock = self.enterContext(mock.patch.object(engine_fleet.time, "time", return_value=1000.5))
        self.enterContext(mock.patch.object(engine_fleet.shutil, "disk_usage",
                                            return_value=SimpleNamespace(free=engine_fleet.RESERVE * 2)))
        with mock.patch.object(self.fleet, "worker", return_value=failure) as worker:
            self.assertEqual(self.fleet.acquire(request, None)["retry_after_seconds"], 1800)
            worker.assert_called_once()
        fresh = engine_fleet.Fleet(self.fleet.state, self.fleet.staging, self.fleet.catalog,
                                   self.fleet.public_key, "arm64", "0.1")
        with mock.patch.object(fresh, "worker", return_value={"ok": True, "candidates": []}) as worker, \
                mock.patch.object(fresh, "running_containers", return_value={fresh.container("fixture")}):
            clock.return_value = 1901
            deadline = fresh.manage({"action": "status"}, None)["engines"][0]["retry_at"]
            for operation in (request, fetch):
                self.assertEqual(fresh.acquire(operation, None)["retry_after_seconds"], deadline - 1901)
            self.assertFalse(fresh.manage({"engine": "fixture", "action": "test"}, None)["ok"])
            worker.assert_not_called()
            clock.return_value = deadline
            self.assertEqual(fresh.manage({"action": "status"}, None)["engines"][0]["retry_at"], 0)
            self.assertTrue(fresh.acquire(request, None)["ok"])
            worker.assert_called_once()

    def test_failed_enable_blocks_later_enable_and_readiness_checks(self):
        self.engine["enabled"] = False
        response = {"ok": False, "code": "login_unavailable"}
        request = {"engine": "fixture", "action": "enable"}
        with mock.patch.object(self.fleet, "worker", return_value=response) as worker, \
                mock.patch.object(self.fleet, "start") as start, mock.patch.object(self.fleet, "stop"):
            first = self.fleet.manage(request, None)
            self.assertEqual(first["retry_after_seconds"], 1800)
            self.assertFalse(self.fleet.engines["fixture"]["enabled"])
            self.assertFalse(self.fleet.manage(request, None)["ok"])
            self.assertFalse(self.fleet.manage({**request, "action": "test"}, None)["ok"])
            start.assert_called_once()
            worker.assert_called_once()

    def test_fetch_does_not_dispatch_candidates_outside_its_signed_sources(self):
        with mock.patch.object(self.fleet, "worker") as worker:
            result = self.fleet.acquire({"action": "fetch", "engine": "fixture",
                                         "candidate": {"source": "unrelated", "id": "first-light"}}, None)
        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])
        worker.assert_not_called()

    def test_settings_are_scoped_and_the_status_reports_keys_without_values(self):
        fields = [{"key": "password", "label": "Password", "type": "secret", "required": True},
                  {"key": "account", "label": "Account", "type": "text", "required": False}]
        self.engine.update(settings=fields, enabled=False)
        request = {"action": "configure", "engine": "fixture",
                   "settings": {"password": "disposable value", "account": "first"}}
        self.assertEqual(self.fleet.manage(request, None), {"ok": True})
        self.fleet.manage({**request, "settings": {"account": "second"}}, None)
        credentials = self.fleet.state / "fixture" / "credentials" / "plugin-settings.json"
        self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(credentials.read_text()), {"password": "disposable value", "account": "second"})
        status = self.fleet.manage({"action": "status"}, None)
        self.assertEqual(status["engines"][0]["configured_settings"], ["password", "account"])
        self.assertNotIn("disposable value", json.dumps(status))
        with self.assertRaisesRegex(ValueError, "declared"):
            self.fleet.manage({**request, "settings": {"../ignored": "value"}}, None)
        self.assertFalse((credentials.parent.parent / "ignored").exists())

    def test_retry_configuration_recovers_an_enabled_worker_missing_its_mount(self):
        self.engine["settings"] = [
            {"key": "account", "label": "Account", "type": "text", "required": True},
        ]
        request = {"action": "configure", "engine": "fixture", "settings": {"account": "fixture"}}
        inspections = [
            SimpleNamespace(returncode=0, stdout=b"true\n"),
            SimpleNamespace(returncode=1, stdout=b""),
            SimpleNamespace(returncode=0, stdout=b"true false\n"),
        ]
        with mock.patch.object(self.fleet, "docker", side_effect=inspections), \
                mock.patch.object(self.fleet, "stop") as stop, \
                mock.patch.object(self.fleet, "start", side_effect=[RuntimeError("Docker failed."), None]) as start:
            with self.assertRaisesRegex(RuntimeError, "Docker failed"):
                self.fleet.manage(request, None)
            self.assertEqual(self.fleet.manage(request, None), {"ok": True})
            self.assertEqual(self.fleet.manage(request, None), {"ok": True})
        self.assertEqual(stop.call_count, 2)
        self.assertEqual(start.call_count, 2)

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL required to sign the fixture catalog")
    def test_repository_accepts_signed_release_and_reuses_verified_cache_offline(self):
        private_key = self.fleet.state / "private.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                        "-out", private_key], check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", private_key, "-pubout", "-out", self.fleet.public_key],
                       check=True, capture_output=True)
        entry = {**self.engine, "architecture": "arm64", "version": "1.0",
                 "image": "registry.example/engine@sha256:" + "a" * 64,
                 "settings": [{"key": "account", "label": "Account", "type": "text", "required": True}]}
        self.fleet.catalog.write_text(json.dumps({"version": 1, "engines": [entry]}))
        signature = self.fleet.catalog.with_suffix(".sig")
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", private_key, "-out",
                        signature, self.fleet.catalog], check=True, capture_output=True)
        stage = self.fleet.state / "catalog-stage"
        stage.mkdir()

        def publish():
            return engine_repository.stage_repository("https://github.com/fixture/listening-plugins", "v1",
                stage, self.fleet.state, self.fleet.public_key, "arm64", "0.1")

        with mock.patch.object(engine_repository, "download",
                               side_effect=[self.fleet.catalog.read_bytes(), signature.read_bytes()]) as fetch:
            staged = publish()
            self.assertTrue(fetch.call_args_list[0].args[1].endswith("/releases/download/v1/catalog.json"))
            self.assertEqual(engine_fleet.verify_catalog(staged, self.fleet.public_key, "arm64", "0.1")
                             ["fixture"]["settings"], entry["settings"])
        with mock.patch.object(engine_repository, "download", side_effect=TimeoutError):
            self.assertEqual(publish().read_bytes(), self.fleet.catalog.read_bytes())
        with mock.patch.object(engine_repository, "download",
                               side_effect=[self.fleet.catalog.read_bytes(), b"invalid signature"]):
            with self.assertRaisesRegex(ValueError, "signature"):
                publish()

    def test_signed_image_must_resolve_to_the_correct_linux_architecture(self):
        digest = "registry.example/engine@sha256:" + "a" * 64
        with mock.patch.object(self.fleet, "docker", return_value=SimpleNamespace(stdout=b"linux/amd64\n")) as docker:
            with self.assertRaisesRegex(ValueError, "architecture"):
                self.fleet.start({**self.engine, "image": digest})
            docker.assert_called_once_with("image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", digest)

    def test_reserve_is_checked_during_a_silent_transfer(self):
        request = {"version": 1, "action": "fetch"}
        # A disposable process supplies the same pollable pipes as Docker
        # while simulating a provider that is downloading without output.
        launch = subprocess.Popen
        client, connection = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(connection.close)
        free = [engine_fleet.RESERVE + 1, engine_fleet.RESERVE - 1]
        usage = [SimpleNamespace(free=value) for value in free]
        with mock.patch.object(self.fleet, "docker") as docker, mock.patch.object(
                engine_fleet.shutil, "disk_usage", side_effect=usage), mock.patch.object(
                engine_fleet.subprocess, "Popen",
                side_effect=lambda _command, **kwargs: launch([sys.executable, "-c", "import time; time.sleep(25)"], **kwargs)):
            with self.assertRaisesRegex(OSError, "20 GiB"):
                self.fleet.worker("fixture", request, connection, scratch=self.fleet.mount("fixture"))
        docker.assert_called_once_with("restart", self.fleet.container("fixture"))

    def test_opted_in_transfer_relays_only_valid_progress(self):
        request = {"action": "fetch", "engine": "fixture", "job": self.job, "report_progress": True,
                   "candidate": {"source": "fixture", "id": "track"}}
        target = self.fleet.staging / self.job

        def download(identity, request, connection, *, scratch, on_tick):
            directory = scratch / Path(request["staging"]).name
            progress = directory / engine_fleet.PROGRESS_FILE
            staged = target / engine_fleet.PROGRESS_FILE
            track = directory / "song.mp3"
            track.write_bytes(b"audio")
            progress.symlink_to(track)
            on_tick()
            self.assertFalse(staged.exists())
            progress.unlink()
            progress.write_text("103")
            on_tick()
            self.assertFalse(staged.exists())
            progress.write_text("48")
            on_tick()
            self.assertEqual(staged.read_text(), "48")
            return {"ok": True, "file": track.name}

        with mock.patch.object(self.fleet, "worker", side_effect=download):
            self.assertTrue(self.fleet.acquire(request, None)["ok"])
        self.assertEqual((target / "song.mp3").read_bytes(), b"audio")

    def test_snapshot_leaves_worker_symlinks_and_links_outside_staging(self):
        scratch = self.fleet.mount("fixture")
        original = scratch / "real.mp3"
        original.write_bytes(b"fixture")
        (scratch / "symlink.mp3").symlink_to(original)
        os.link(original, scratch / "hardlink.mp3")
        with self.assertRaises(OSError):
            self.fleet.snapshot(scratch, self.job, "symlink.mp3")
        with self.assertRaisesRegex(ValueError, "regular audio candidate"):
            self.fleet.snapshot(scratch, self.job, "hardlink.mp3")
        self.assertEqual(list((self.fleet.staging / self.job).iterdir()), [])
