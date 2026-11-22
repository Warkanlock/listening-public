import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import system.updater as updater_module
from system.updater import (BREW_PACKAGE, DIVERGED, DIRTY_TREE, DOCKER_IMAGE, MAC_APP,
                            LOCAL_COMMITS, NOT_A_CHECKOUT, Updater, UpdateError,
                            frontend_build_needed, install_kind,
                            rebuild_frontend, spawn_restarter)

GIT_ENV = dict(os.environ,
               GIT_AUTHOR_NAME="Listening Test", GIT_AUTHOR_EMAIL="test@example.com",
               GIT_COMMITTER_NAME="Listening Test", GIT_COMMITTER_EMAIL="test@example.com")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, env=GIT_ENV, check=True)


class RepoFixture:
    """A local bare remote, a seed repo publishing to it, and a clone
    standing in for the app checkout. Local paths only, no network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
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
        self.updater = Updater(self.clone)

    def commit(self, repo, name, content, message):
        (repo / name).write_text(content, encoding="utf-8")
        git(repo, "add", name)
        git(repo, "commit", "-m", message)

    def publish(self, name="feature.py", content="NEW = True\n"):
        """Advance the remote by one commit; returns its short hash."""
        self.commit(self.seed, name, content, "advance")
        git(self.seed, "push", "origin", "main")
        return git(self.seed, "rev-parse", "--short", "HEAD").stdout.strip()

    def head(self):
        return git(self.clone, "rev-parse", "--short", "HEAD").stdout.strip()


class CheckTests(RepoFixture, unittest.TestCase):
    def test_up_to_date_reports_no_update(self):
        result = self.updater.check()
        self.assertEqual(result["behind"], 0)
        self.assertEqual(result["current"], self.head())
        self.assertEqual(result["remote"], self.head())
        self.assertGreater(result["checked_at"], 0)

    def test_new_remote_commit_reports_available(self):
        target = self.publish()
        result = self.updater.check(force=True)
        self.assertEqual(result["behind"], 1)
        self.assertEqual(result["remote"], target)
        self.assertNotEqual(result["current"], target)

    def test_check_fetches_at_most_once_per_interval(self):
        self.assertEqual(self.updater.check()["behind"], 0)
        self.publish()
        # Inside the interval the cached comparison is served...
        self.assertEqual(self.updater.check()["behind"], 0)
        # ...and a forced check fetches and sees the new commit.
        self.assertEqual(self.updater.check(force=True)["behind"], 1)


class UpdateTests(RepoFixture, unittest.TestCase):
    def test_update_fast_forwards_and_lands_the_new_file(self):
        target = self.publish()
        self.assertEqual(self.updater.update(), target)
        self.assertEqual(self.head(), target)
        self.assertTrue((self.clone / "feature.py").is_file())
        self.assertEqual(self.updater.current(), target)
        self.assertEqual(self.updater.check()["behind"], 0)

    def test_update_when_up_to_date_changes_nothing(self):
        self.assertEqual(self.updater.update(), self.head())

    def test_dirty_tree_refuses_and_changes_nothing(self):
        self.publish()
        before = self.head()
        (self.clone / "app.py").write_text("VERSION = 'local edit'\n",
                                          encoding="utf-8")
        with self.assertRaises(UpdateError) as raised:
            self.updater.update()
        self.assertEqual(str(raised.exception), DIRTY_TREE)
        self.assertEqual(self.head(), before)
        self.assertEqual((self.clone / "app.py").read_text(encoding="utf-8"),
                         "VERSION = 'local edit'\n")
        self.assertFalse((self.clone / "feature.py").exists())

    def test_diverged_history_refuses(self):
        self.publish()
        self.commit(self.clone, "local.py", "LOCAL = True\n", "local work")
        before = self.head()
        with self.assertRaises(UpdateError) as raised:
            self.updater.update()
        self.assertEqual(str(raised.exception), DIVERGED)
        self.assertEqual(self.head(), before)
        self.assertFalse((self.clone / "feature.py").exists())

    def test_local_commits_refuse(self):
        self.commit(self.clone, "local.py", "LOCAL = True\n", "local work")
        before = self.head()
        with self.assertRaises(UpdateError) as raised:
            self.updater.update()
        self.assertEqual(str(raised.exception), LOCAL_COMMITS)
        self.assertEqual(self.head(), before)

    def test_untracked_server_log_neither_blocks_nor_is_touched(self):
        (self.clone / "server.log").write_text("old server output\n",
                                               encoding="utf-8")
        target = self.publish()
        self.assertEqual(self.updater.update(), target)
        self.assertEqual((self.clone / "server.log").read_text(encoding="utf-8"),
                         "old server output\n")

    def test_update_never_touches_workspace_data_or_config(self):
        workspace = Path(self.tmp.name) / "room"
        database = workspace / "state" / "requests.sqlite3"
        database.parent.mkdir(parents=True)
        database.write_bytes(b"owner data")
        config = Path(self.tmp.name) / "config" / "config.json"
        config.parent.mkdir()
        config.write_text("{}", encoding="utf-8")
        self.publish()
        self.updater.update()
        self.assertEqual(database.read_bytes(), b"owner data")
        self.assertEqual(config.read_text(encoding="utf-8"), "{}")


class NonCheckoutTests(unittest.TestCase):
    def test_version_is_optional_when_the_deployed_server_has_no_git_binary(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"PATH": ""}):
            app = Path(tmp)
            self.assertEqual(Updater(app).current(), "")
            (app / ".git").mkdir()
            self.assertEqual(Updater(app).current(), "")

    def test_plain_folder_refuses_check_and_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "app"
            app.mkdir()
            updater = Updater(app)
            self.assertFalse(updater.is_checkout())
            self.assertEqual(updater.current(), "")
            with self.assertRaises(UpdateError) as raised:
                updater.check()
            self.assertEqual(str(raised.exception), NOT_A_CHECKOUT)
            with self.assertRaises(UpdateError) as raised:
                updater.update()
            self.assertEqual(str(raised.exception), NOT_A_CHECKOUT)


class InstallKindTests(RepoFixture, unittest.TestCase):
    """Plan 044: a package install refuses through the updater with
    its route's directions, before any git work; a checkout (unset,
    "checkout", or anything unrecognized) behaves exactly as plan
    023 built it."""

    def test_install_kind_reads_the_marker(self):
        self.assertEqual(install_kind({}), "checkout")
        self.assertEqual(install_kind({"LISTENING_ROOM_INSTALL_KIND": "checkout"}),
                         "checkout")
        self.assertEqual(install_kind({"LISTENING_ROOM_INSTALL_KIND": "brew"}), "brew")
        self.assertEqual(install_kind({"LISTENING_ROOM_INSTALL_KIND": "docker"}),
                         "docker")
        self.assertEqual(install_kind({"LISTENING_ROOM_INSTALL_KIND": " AppImage "}),
                         "checkout")

    def test_brew_kind_refuses_check_and_update_with_brew_directions(self):
        with mock.patch.dict(os.environ, {"LISTENING_ROOM_INSTALL_KIND": "brew"}):
            with self.assertRaises(UpdateError) as raised:
                self.updater.check()
            self.assertEqual(str(raised.exception), BREW_PACKAGE)
            with self.assertRaises(UpdateError) as raised:
                self.updater.update()
            self.assertEqual(str(raised.exception), BREW_PACKAGE)
        # The refusal happened before any git work: the clone is
        # untouched and a later unmarked check behaves normally.
        self.assertEqual(self.updater.check()["behind"], 0)

    def test_mac_app_updates_by_replacing_the_bundle(self):
        with mock.patch.dict(os.environ, {"LISTENING_ROOM_INSTALL_KIND": "app"}):
            for action in (self.updater.check, self.updater.update):
                with self.assertRaises(UpdateError) as raised:
                    action()
                self.assertEqual(str(raised.exception), MAC_APP)

    def test_docker_kind_refuses_check_and_update_with_image_directions(self):
        with mock.patch.dict(os.environ, {"LISTENING_ROOM_INSTALL_KIND": "docker"}):
            with self.assertRaises(UpdateError) as raised:
                self.updater.check()
            self.assertEqual(str(raised.exception), DOCKER_IMAGE)
            with self.assertRaises(UpdateError) as raised:
                self.updater.update()
            self.assertEqual(str(raised.exception), DOCKER_IMAGE)

    def test_brew_kind_refusal_wins_over_the_not_a_checkout_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            updater = Updater(Path(tmp) / "app")
            with mock.patch.dict(os.environ, {"LISTENING_ROOM_INSTALL_KIND": "brew"}):
                with self.assertRaises(UpdateError) as raised:
                    updater.check()
                self.assertEqual(str(raised.exception), BREW_PACKAGE)

    def test_checkout_marker_keeps_plan_023_behavior(self):
        with mock.patch.dict(os.environ, {"LISTENING_ROOM_INSTALL_KIND": "checkout"}):
            self.assertEqual(self.updater.check()["behind"], 0)
            self.assertEqual(self.updater.update(), self.head())


class FrontendRebuildTests(unittest.TestCase):
    """Plan 050 step 4: after a pull, the built interface is brought
    up to date when Node and Yarn are available; when the toolchain
    is missing the need stays reported (frontend_build_needed is
    what Settings turns into the "Interface rebuild needed" notice)
    instead of the update failing or silently serving the old dist."""

    OLD = 1_000_000
    NEW = 2_000_000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "app"
        self.react = self.repo / "frontend" / "react"
        (self.react / "src").mkdir(parents=True)
        (self.react / "src" / "App.tsx").write_text("export {}\n",
                                                    encoding="utf-8")
        (self.react / "yarn.lock").write_text("# lock\n", encoding="utf-8")
        (self.react / "package.json").write_text("{}\n", encoding="utf-8")

    def age(self, root, when):
        for path in root.rglob("*"):
            if path.is_file():
                os.utime(path, (when, when))

    def build_dist(self, when=NEW):
        dist = self.repo / "frontend" / "dist"
        dist.mkdir(parents=True, exist_ok=True)
        index = dist / "index.html"
        index.write_text("<html></html>\n", encoding="utf-8")
        os.utime(index, (when, when))
        return index

    def fake_run(self, returncode=0):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, returncode)

        fake = mock.Mock()
        fake.run = run
        fake.TimeoutExpired = subprocess.TimeoutExpired
        return fake, calls

    def toolchain(self, present):
        return mock.patch.object(
            updater_module.shutil, "which",
            lambda name: f"/usr/bin/{name}" if present else None)

    def test_needed_when_the_dist_is_missing(self):
        self.assertTrue(frontend_build_needed(self.repo))

    def test_not_needed_without_frontend_sources(self):
        bare = Path(self.tmp.name) / "packaged"
        bare.mkdir()
        self.assertFalse(frontend_build_needed(bare))

    def test_not_needed_when_the_dist_is_the_newest_thing(self):
        self.age(self.react, self.OLD)
        self.build_dist(self.NEW)
        self.assertFalse(frontend_build_needed(self.repo))

    def test_needed_when_a_source_outdates_the_dist(self):
        self.age(self.react, self.OLD)
        self.build_dist(self.NEW)
        source = self.react / "src" / "App.tsx"
        os.utime(source, (self.NEW + 10, self.NEW + 10))
        self.assertTrue(frontend_build_needed(self.repo))

    def test_node_modules_never_counts_as_a_source(self):
        self.age(self.react, self.OLD)
        self.build_dist(self.NEW)
        modules = self.react / "node_modules" / "pkg"
        modules.mkdir(parents=True)
        touched = modules / "index.js"
        touched.write_text("x\n", encoding="utf-8")
        os.utime(touched, (self.NEW + 10, self.NEW + 10))
        self.assertFalse(frontend_build_needed(self.repo))

    def test_rebuild_without_toolchain_runs_nothing(self):
        fake, calls = self.fake_run()
        with self.toolchain(False), \
                mock.patch.object(updater_module, "subprocess", fake):
            self.assertEqual(rebuild_frontend(self.repo), "no toolchain")
        self.assertEqual(calls, [])
        # The need is still there afterwards: that is what Settings
        # turns into the notice.
        self.assertTrue(frontend_build_needed(self.repo))

    def test_rebuild_not_needed_short_circuits(self):
        self.age(self.react, self.OLD)
        self.build_dist(self.NEW)
        fake, calls = self.fake_run()
        with self.toolchain(True), \
                mock.patch.object(updater_module, "subprocess", fake):
            self.assertEqual(rebuild_frontend(self.repo), "not needed")
        self.assertEqual(calls, [])

    def test_rebuild_runs_the_build_when_dependencies_are_present(self):
        modules = self.react / "node_modules"
        modules.mkdir()
        self.age(self.react, self.OLD)
        os.utime(modules, (self.NEW, self.NEW))
        fake, calls = self.fake_run()
        with self.toolchain(True), \
                mock.patch.object(updater_module, "subprocess", fake):
            self.assertEqual(rebuild_frontend(self.repo), "built")
        self.assertEqual(calls, [["yarn", "--cwd", str(self.react), "build"]])

    def test_rebuild_installs_first_when_dependencies_are_missing(self):
        fake, calls = self.fake_run()
        with self.toolchain(True), \
                mock.patch.object(updater_module, "subprocess", fake):
            self.assertEqual(rebuild_frontend(self.repo), "built")
        self.assertEqual(calls, [
            ["yarn", "--cwd", str(self.react), "install", "--frozen-lockfile"],
            ["yarn", "--cwd", str(self.react), "build"],
        ])

    def test_rebuild_failure_is_a_result_not_an_exception(self):
        fake, calls = self.fake_run(returncode=1)
        with self.toolchain(True), \
                mock.patch.object(updater_module, "subprocess", fake):
            self.assertEqual(rebuild_frontend(self.repo), "failed")
        self.assertTrue(frontend_build_needed(self.repo))


class RestarterTests(unittest.TestCase):
    def test_restarter_relaunches_with_the_same_arguments_after_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            marker = base / "relaunched.txt"
            script = "import os, sys; open(sys.argv[1], 'w').write(os.getcwd())"
            dying = subprocess.Popen([sys.executable, "-c",
                                      "import time; time.sleep(0.3)"])
            spawn_restarter(dying.pid,
                            [sys.executable, "-c", script, str(marker)],
                            base, base / "server.log")
            # Reap the dying process: an unreaped child stays a zombie
            # and still answers os.kill(pid, 0), which is not the
            # restarter's situation in production (there the exiting
            # server is reaped by its own parent, not by the restarter).
            dying.wait()
            deadline = time.time() + 15
            while time.time() < deadline and not marker.exists():
                time.sleep(0.1)
            self.assertTrue(marker.is_file())
            self.assertEqual(marker.read_text(encoding="utf-8"), str(base))


if __name__ == "__main__":
    unittest.main()
