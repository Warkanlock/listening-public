import json
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fixture_environment import isolate_room_environment as setUpModule
from test_runner_reliability import WorkspaceTestCase, runner
from downloads import engine_contract


class EngineRunnerTests(WorkspaceTestCase):
    def setUp(self):
        super().setUp()
        self.staging = self.root / "engine-staging"
        self.staging.mkdir()
        socket_dir = tempfile.TemporaryDirectory(prefix="listening-engine-", dir="/tmp")
        self.addCleanup(socket_dir.cleanup)
        environment = {"LISTENING_ENGINE_SOCKET": socket_dir.name + "/fleet.sock",
                       "LISTENING_ENGINE_STAGING": str(self.staging)}
        self.enterContext(mock.patch.dict(os.environ, environment))
        self.enterContext(mock.patch.object(runner, "MIN_FREE", 0))
        self.selection = {"search": "fixture-catalog", "fetch": "fixture-fetch"}
        (self.root / "settings.json").write_text(json.dumps({"download_engines": self.selection}))
        self.requests = []
        self.mode = "normal"
        self.media = {}

        tests = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                request = json.loads(self.rfile.readline())
                response = tests.respond(request)
                self.wfile.write(json.dumps(response).encode() + b"\n")

        manager = socketserver.UnixStreamServer(environment["LISTENING_ENGINE_SOCKET"], Handler)
        thread = threading.Thread(target=manager.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(manager.server_close)
        self.addCleanup(manager.shutdown)

    def respond(self, request):
        self.requests.append(request)
        action = request["action"]
        if action == "status":
            return {"ok": True, "engines": [
                {"id": role, "capabilities": [capability], "enabled": True, "ready": True,
                 "sources": {capability: ["unrelated" if self.mode == "incompatible" and capability == "fetch"
                                          else "fixture"]}}
                for capability, role in self.selection.items()
            ]}
        if action == "search":
            if self.mode == "login":
                return {"ok": False, "code": "login_unavailable", "error": "sensitive provider text"}
            if self.mode == "interrupted" or (self.mode == "second" and request["recording"]["id"] == "key-2"):
                return {"ok": False, "retryable": True, "error": "sensitive provider text"}
            source = "undeclared" if self.mode == "undeclared" else "fixture"
            return {"ok": True, "candidates": [{"source": source, "id": name}
                                               for name in self.media]}
        if action == "fetch" and request["candidate"]["id"] == "slow-peer.mp3":
            return {"ok": False, "code": "timeout", "retryable": True}
        folder = self.staging / request["job"]
        name = request["candidate"]["id"]
        if self.mode == "traversal":
            return {"ok": True, "file": "../" + name}
        if self.mode == "symlink":
            (folder / name).symlink_to(self.media[name])
        else:
            shutil.copyfile(self.media[name], folder / name)
        return {"ok": True, "file": name}

    def test_status_requires_both_selected_capabilities(self):
        self.assertTrue(engine_contract.engines_ready({"download_engines": self.selection}))
        self.assertFalse(engine_contract.engines_ready({"download_engines": {"search": "fixture-catalog"}}))
        self.assertIsNone(engine_contract.engine_selection({}))
        self.assertEqual(engine_contract.engine_selection({"download_engines": None}), {})
        self.assertEqual(self.requests[0]["server_release"], engine_contract.release_version(engine_contract.ROOT))

    def test_listener_summary_omits_manager_details_and_checks_source_compatibility(self):
        settings = {"download_engines": self.selection}
        engines = self.respond({"action": "status"})["engines"]
        engines[0].update(version="1.0", previous={"image": "private"}, configured_settings=["password"])
        with mock.patch.object(engine_contract, "manager_call", return_value={"engines": engines}) as status:
            summary = engine_contract.source_summary(settings)
            status.assert_called_once_with("status")
        self.assertEqual(summary["state"], "ready")
        self.assertTrue(summary["inventory_available"])
        with mock.patch.object(engine_contract, "manager_call", side_effect=engine_contract.EngineError("Offline")):
            legacy = engine_contract.source_summary({})
        self.assertEqual(legacy["state"], "legacy")
        self.assertFalse(legacy["inventory_available"])
        self.assertEqual(summary["selection"], self.selection)
        self.assertEqual(set(summary["installed"][0]), {"id", "version", "capabilities", "enabled", "ready", "sources"})
        self.mode = "incompatible"
        self.assertEqual(engine_contract.source_summary(settings)["state"], "unavailable")
        self.assertEqual(engine_contract.source_summary({})["state"], "legacy")
        self.assertEqual(engine_contract.source_summary({"download_engines": {}})["state"], "unconfigured")

    def test_selected_source_pause_prevents_search_when_only_the_fetcher_is_paused(self):
        engines = self.respond({"action": "status"})["engines"]
        settings = {"download_engines": self.selection}
        deadline = int(engine_contract.time.time()) + 300
        engines.append({**engines[1], "id": "another-fetcher", "retry_at": deadline + 600})
        with mock.patch.object(engine_contract, "manager_call", return_value={"engines": engines}):
            self.assertEqual(engine_contract.source_summary(settings)["state"], "ready")
            engines[1]["retry_at"] = deadline
            self.assertEqual(engine_contract.source_summary(settings)["retry_at"], deadline)
            self.assertEqual(engine_contract.source_summary(settings)["state"], "cooldown")
            self.assertTrue(engine_contract.engines_ready(settings))
            self.assertFalse(engine_contract.engines_ready(settings, for_acquisition=True))
            with self.assertRaisesRegex(engine_contract.EngineError, "requests stay queued"):
                engine_contract.selected_engine_sources(self.selection, for_acquisition=True)

    def test_incompatible_sources_pause_before_search_and_keep_the_cursor(self):
        record = self.record(1, "First Light")
        self.write_manifest([record])
        self.mode = "incompatible"
        self.assertFalse(engine_contract.engines_ready({"download_engines": self.selection}))
        self.assertFalse(runner.run_batch(0, 1))
        self.assertEqual(runner.cursor_offset(), 0)
        self.assert_download(record, status="queued")
        self.assertTrue(all(request["action"] == "status" for request in self.requests))

    def test_undeclared_candidate_source_never_reaches_the_fetcher(self):
        record = self.record(1, "First Light")
        self.write_manifest([record])
        self.media["first-light.flac"] = self.root / "ignored.flac"
        self.mode = "undeclared"
        self.assert_download(record, status="queued")
        self.assertEqual([request["action"] for request in self.requests], ["status", "search"])

    def test_source_login_failure_keeps_request_and_shows_room_owned_recovery(self):
        record = self.record(1, "First Light")
        self.mode = "login"
        _, reason, _ = self.assert_download(record, status="queued")
        self.assertIn("sign in", reason)
        self.assertIn("Extensions", reason)
        self.assertNotIn("sensitive provider text", reason)

    def test_transient_failure_leaves_current_track_and_request_pending(self):
        record = self.record(1, "First Light")
        self.write_manifest([record])
        self.write_cursor(0)
        self.mode = "interrupted"
        self.assertFalse(runner.run_batch(0, 1))
        self.assertEqual(runner.cursor_offset(), 0)
        _, error, _ = self.assert_download(record, status="queued")
        self.assertNotIn("sensitive provider text", error)

    def test_scoped_retry_preserves_the_job_and_attempted_recording_keys(self):
        records = [self.record(1, "First Light"), self.record(2, "Second Light")]
        self.write_manifest(records, playlists={"mix": [1, 2]})
        queue = runner.JobQueue(runner.host_database(runner.REQUESTS_DB, runner.STATE))
        job = queue.enqueue("downloads", {"type": "list", "id": "mix"})
        queue.mark_running(job["id"])
        self.mode = "second"
        with mock.patch.dict(os.environ, {"LISTENING_DOWNLOAD_JOB_ID": str(job["id"])}):
            runner.run_scoped("mix")
            progress = queue.get(job["id"])["progress"]
            self.assertEqual(progress["scoped_attempted"], ["key-1"])
            self.assertTrue(progress["engine_retry"])
            queue.record_progress(job["id"], {"scoped_attempted": progress["scoped_attempted"]})
            self.requests.clear()
            self.mode = "normal"
            runner.run_scoped("mix")
        self.assertEqual([request["recording"]["id"] for request in self.requests
                          if request["action"] == "search"], ["key-2"])
        self.assertEqual(queue.get(job["id"])["progress"]["scoped_attempted"], ["key-1", "key-2"])

    def test_storage_reserve_pauses_the_same_managed_job_before_search(self):
        self.write_manifest([self.record(1, "First Light")])
        queue = runner.JobQueue(runner.host_database(runner.REQUESTS_DB, runner.STATE))
        job = queue.enqueue("downloads", {"type": "all"})
        queue.mark_running(job["id"])
        with mock.patch.dict(os.environ, {"LISTENING_DOWNLOAD_JOB_ID": str(job["id"])}), mock.patch.object(
                runner, "free_bytes", return_value=0):
            self.assertFalse(runner.run_batch(0, 1))
        self.assertEqual(runner.cursor_offset(), 0)
        self.assertIn("free space", queue.get(job["id"])["progress"]["engine_retry"])
        self.assertEqual(self.requests, [])

    def test_requests_pause_without_losing_the_request_or_job_id(self):
        record = self.record(1, "First Light")
        self.write_manifest([record])
        store = runner.requests_store()
        request = store.enqueue(1, record["artist"], record["title"], record["album"], 200, "")
        queue = runner.JobQueue(runner.host_database(runner.REQUESTS_DB, runner.STATE))
        job = queue.enqueue("requests", {})
        queue.mark_running(job["id"])
        self.mode = "interrupted"
        with mock.patch.dict(os.environ, {"LISTENING_DOWNLOAD_JOB_ID": str(job["id"])}):
            runner.run_requests()
        self.assertEqual(store.get(request)["status"], "queued")
        self.assertTrue(queue.get(job["id"])["progress"]["engine_retry"])

    def test_refuses_provider_path_escape_and_symlinks(self):
        record = self.record(1, "First Light")
        self.write_manifest([record])
        external = self.root / "audio-outside-staging.flac"
        external.write_bytes(b"not for the runner")
        self.media["example.flac"] = external
        for mode in ("traversal", "symlink"):
            with self.subTest(mode=mode):
                self.mode = mode
                self.assert_download(record, status="queued")
                self.assertEqual(list(self.staging.iterdir()), [])
                self.assertEqual(external.read_bytes(), b"not for the runner")

    def make_audio(self, filename, title):
        path = self.root / filename
        codec = ["-c:a", "flac"] if path.suffix == ".flac" else ["-c:a", "libmp3lame", "-b:a", "256k"]
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
                        "-i", "anoisesrc=d=1:c=white", "-ac", "2", *codec,
                        "-metadata", f"title={title}", str(path)], check=True)
        self.media[filename] = path

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_bulk_verifies_owned_staging_and_request_recovers_without_another_fetch(self):
        record = self.record(1, "First Light", length=1)
        self.write_manifest([record])
        self.make_audio("candidate-with-no-title.mp3", "")
        self.make_audio("first-light.mp3", "First Light")
        self.assertTrue(runner.run_batch(0, 1))
        self.assertEqual(runner.cursor_offset(), 1)
        search = next(request for request in self.requests if request["action"] == "search")
        self.assertEqual(search["recording"]["formats"][0], ".mp3")
        self.assertEqual(search["engine"], "fixture-catalog")
        self.assertEqual([request["candidate"]["id"] for request in self.requests if request["action"] == "fetch"],
                         ["candidate-with-no-title.mp3", "first-light.mp3"])
        self.assertEqual(self.run_report()["verified"], 1)
        self.assertEqual(len(list(self.library.glob("*.mp3"))), 1)
        self.assertEqual(list(self.staging.iterdir()), [])

        self.make_audio("first-light.flac", "First Light")
        with mock.patch.object(runner, "audio_store_mode", return_value="database"):
            status, _, path = self.assert_download(record)
            self.assertTrue(path.startswith("blob:"))
            requests = len(self.requests)
            self.assertEqual(self.assert_download(record)[2], path)
            self.assertEqual(len(self.requests), requests)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_fetch_timeout_retries_another_peer_and_preserves_the_job_if_none_succeeds(self):
        record = self.record(1, "First Light", length=1)
        self.write_manifest([record])
        self.media["slow-peer.mp3"] = None
        self.assertFalse(runner.run_batch(0, 1))
        self.assertEqual(runner.cursor_offset(), 0)
        self.assertEqual(list(self.staging.iterdir()), [])

        self.make_audio("first-light.mp3", "First Light")
        self.assertTrue(runner.run_batch(0, 1))
        self.assertEqual(runner.cursor_offset(), 1)
        self.assertEqual(self.run_report()["verified"], 1)
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_search_budget_includes_the_worker_deadline(self):
        record = self.record(1, "First Light", length=1)
        self.write_manifest([record])
        with mock.patch.object(engine_contract, "manager_call", wraps=engine_contract.manager_call) as call:
            self.assertTrue(runner.run_batch(0, 1))
        search = next(request for request in call.call_args_list if request.args == ("search",))
        self.assertGreater(search.kwargs["timeout"], engine_contract.WORKER_TIMEOUT_SECONDS["search"])

    def test_transport_timeout_waits_for_the_manager_instead_of_starting_another_peer(self):
        record = self.record(1, "First Light", length=1)
        self.write_manifest([record])
        self.media.update({"idle-peer.mp3": None, "other-peer.mp3": None})
        manager_call = engine_contract.manager_call

        def time_out_on_fetch(action, **kwargs):
            if action == "fetch":
                raise engine_contract.EngineError("The download engine timed out while downloading a candidate.")
            return manager_call(action, **kwargs)

        with mock.patch.object(engine_contract, "manager_call", side_effect=time_out_on_fetch) as call:
            self.assertFalse(runner.run_batch(0, 1))
        fetches = [request for request in call.call_args_list if request.args == ("fetch",)]
        self.assertEqual(len(fetches), 1)
        self.assertEqual(runner.cursor_offset(), 0)
        self.assertEqual(list(self.staging.iterdir()), [])

    def assert_download(self, record, status="downloaded"):
        row = {"id": 7, "search_title": "", "requested_format": "flac", "format_confirmed": True}
        result = runner.run_request(row, record)
        self.assertEqual(result[0], status)
        return result
