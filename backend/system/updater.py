"""In-app update: check this checkout against its remote and, on the
host's demand, fast-forward it and restart the server (plan 023).

The update is deliberately narrow. It only ever fast-forwards the
checkout to origin/main, only from a clean tree, and it never touches
the workspace, the database, or the config folder, which all live
outside the checkout. Anything that is not a clean fast-forward is a
refusal that says why, never a stash, a reset, or a merge.

A copy installed as a package (plan 044: the Homebrew formula or
the Docker image) is marked through the environment and refuses
before any git work, with the directions for its own route: package
copies update through their package manager or image pulls, and
the restarter is never reached under a marker.

The server serves the built frontend from frontend/dist, which a
pull does not carry (it is a gitignored build artifact), so an
update that changes the interface would otherwise restart onto
the previously built one. After a pull, rebuild_frontend() below
brings the dist up to date when Node and Yarn are available
(plan 050 step 4); when it cannot, frontend_build_needed() stays
true and Settings shows the "Interface rebuild needed" notice
until a build happens. Packaged installs ship a built dist and
never hit the case.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REMOTE_BRANCH = "origin/main"
FETCH_INTERVAL = 60.0  # The check fetches at most once a minute.

# The install-kind marker (plan 044): the package routes set
# LISTENING_ROOM_INSTALL_KIND in the environment (the Homebrew
# wrapper, the Docker image), and a marked copy updates through its
# package route, never through git. Unset, "checkout", or anything
# unrecognized is a checkout and keeps plan 023's behavior exactly.
INSTALL_KIND_ENV = "LISTENING_ROOM_INSTALL_KIND"
BREW_PACKAGE = ("This copy was installed as a package; update it with "
                "`brew upgrade`.")
DOCKER_IMAGE = ("This copy runs as a container image; updates arrive "
                "as new images: `docker compose pull`, then "
                "`docker compose up -d`.")
MAC_APP = "Quit Listening, then install the app from the new DMG."
_PACKAGE_REFUSALS = {"brew": BREW_PACKAGE, "docker": DOCKER_IMAGE, "app": MAC_APP}


def install_kind(environ=None):
    """The route that installed this copy: "brew", "docker" or "app" when
    the marker names one, "checkout" otherwise."""
    environ = os.environ if environ is None else environ
    kind = environ.get(INSTALL_KIND_ENV, "").strip().casefold()
    return kind if kind in _PACKAGE_REFUSALS else "checkout"


NOT_A_CHECKOUT = ("This copy of Listening is not a git checkout, "
                  "so it cannot update itself")
DIRTY_TREE = ("The update needs a clean app folder; there are "
              "uncommitted changes, so nothing was changed")
DIVERGED = ("The update stopped: this copy and the remote have moved "
            "apart, so nothing was changed")
LOCAL_COMMITS = ("The update stopped: this copy has commits the remote "
                 "does not, so nothing was changed")
REBUILD_NOT_NEEDED = "not needed"
REBUILD_BUILT = "built"
REBUILD_NO_TOOLCHAIN = "no toolchain"
REBUILD_FAILED = "failed"


class UpdateError(ValueError):
    """A refusal or failure whose message is shown to the owner as-is."""


def _git(repo, *args, timeout=60):
    try:
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"Could not run git: {error}") from error


def _trimmed(result):
    return (result.stderr or result.stdout).strip()[-300:]


class Updater:
    """One checkout and its comparison with its remote's main."""

    def __init__(self, repo):
        self.repo = Path(repo)
        self._current = None
        self._checked = None

    def is_checkout(self):
        # The .git check comes first on purpose: git alone would walk
        # up and find a parent folder's checkout, which is not this app.
        if not (self.repo / ".git").exists():
            return False
        return _git(self.repo, "rev-parse", "--git-dir").returncode == 0

    def current(self):
        """The running version: `git rev-parse --short HEAD` in the app
        folder, cached per process start, or the packaged image revision."""
        if install_kind() == "docker":
            return os.environ.get("LISTENING_ROOM_IMAGE_REVISION", "")
        if self._current is None:
            self._current = ""
            if (self.repo / ".git").exists():
                try:
                    result = _git(self.repo, "rev-parse", "--short", "HEAD")
                except UpdateError:
                    return self._current
                if result.returncode == 0:
                    self._current = result.stdout.strip()
        return self._current

    def _fetch(self):
        result = _git(self.repo, "fetch", "--quiet", "origin", timeout=120)
        if result.returncode:
            raise UpdateError(f"Could not reach the remote: {_trimmed(result)}")

    def _counts(self):
        behind = _git(self.repo, "rev-list", "--count", f"HEAD..{REMOTE_BRANCH}")
        ahead = _git(self.repo, "rev-list", "--count", f"{REMOTE_BRANCH}..HEAD")
        remote = _git(self.repo, "rev-parse", "--short", REMOTE_BRANCH)
        if behind.returncode or ahead.returncode or remote.returncode:
            raise UpdateError("Could not compare this copy with the remote")
        return remote.stdout.strip(), int(behind.stdout), int(ahead.stdout)

    def _package_refusal(self):
        """The route-specific refusal for a package install (plan
        044), or None for a checkout. Checked before any git work:
        a marked copy has no checkout to compare, and its update
        story is its package route's, stated in the refusal."""
        return _PACKAGE_REFUSALS.get(install_kind())

    def check(self, force=False):
        """{current, remote, behind, checked_at} for the Settings view.

        Performs the fetch unless the previous one is still inside the
        rate-limit interval; a forced check always fetches.
        """
        refusal = self._package_refusal()
        if refusal:
            raise UpdateError(refusal)
        if not self.is_checkout():
            raise UpdateError(NOT_A_CHECKOUT)
        now = time.monotonic()
        if (not force and self._checked is not None
                and now - self._checked["fetched_at"] < FETCH_INTERVAL):
            return self._public()
        self._fetch()
        remote, behind, _ahead = self._counts()
        self._checked = {"current": self.current(), "remote": remote,
                         "behind": behind, "checked_at": time.time(),
                         "fetched_at": now}
        return self._public()

    def _public(self):
        return {key: self._checked[key]
                for key in ("current", "remote", "behind", "checked_at")}

    def _dirty(self, incoming):
        """Tracked changes always block an update. An untracked file
        blocks only when an incoming change would overwrite it: the
        server log lives untracked in the checkout and must never make
        the room unupdatable."""
        status = _git(self.repo, "status", "--porcelain", "--untracked-files=no")
        if status.returncode:
            raise UpdateError("Could not read the state of the app folder")
        if status.stdout.strip():
            return True
        if not incoming:
            return False
        changed = _git(self.repo, "diff", "--name-only", "HEAD", REMOTE_BRANCH)
        untracked = _git(self.repo, "ls-files", "--others", "--exclude-standard")
        if changed.returncode or untracked.returncode:
            return False
        return bool(set(changed.stdout.splitlines())
                    & set(untracked.stdout.splitlines()))

    def update(self, *, before_merge=None):
        """Fast-forward the checkout to origin/main.

        Returns the new short hash, or the current one when already up
        to date. Raises UpdateError before changing anything when the
        update cannot proceed as a clean fast-forward.
        """
        refusal = self._package_refusal()
        if refusal:
            raise UpdateError(refusal)
        if not self.is_checkout():
            raise UpdateError(NOT_A_CHECKOUT)
        self._fetch()
        _remote, behind, ahead = self._counts()
        if self._dirty(incoming=behind > 0):
            raise UpdateError(DIRTY_TREE)
        if behind and ahead:
            raise UpdateError(DIVERGED)
        if ahead:
            raise UpdateError(LOCAL_COMMITS)
        if not behind:
            return self.current()
        if before_merge is not None:
            before_merge()
        result = _git(self.repo, "merge", "--ff-only", REMOTE_BRANCH)
        if result.returncode:
            raise UpdateError(_trimmed(result) or "The update could not be applied")
        self._current = None
        self._checked = None
        return self.current()


def frontend_build_needed(repo):
    """True when the checkout's built interface is missing or older
    than the frontend sources it is built from.

    A tree with no frontend sources (a packaged install's layout)
    never needs a build: its dist shipped built. The comparison is
    the Makefile start guard's idea, widened from the lockfile to
    every source file: a pull that touched the interface leaves the
    changed files newer than the dist it did not carry.
    """
    repo = Path(repo)
    react = repo / "frontend" / "react"
    if not react.is_dir():
        return False
    dist_index = repo / "frontend" / "dist" / "index.html"
    if not dist_index.is_file():
        return True
    built_at = dist_index.stat().st_mtime
    for folder, directories, filenames in os.walk(react):
        if "node_modules" in directories:
            directories.remove("node_modules")
        for filename in filenames:
            path = Path(folder) / filename
            if path.is_file() and path.stat().st_mtime > built_at:
                return True
    return False


def frontend_toolchain_available():
    """Node and Yarn on PATH: the whole frontend toolchain the
    build needs (yarn runs node, and the build is yarn's script)."""
    return shutil.which("node") is not None and shutil.which("yarn") is not None


def rebuild_frontend(repo):
    """Build the frontend in the checkout, when a build is needed.

    Returns one of the REBUILD_* outcomes; never raises, because
    the update that pulled the new code has
    already succeeded by the time this runs: a rebuild that cannot
    happen leaves frontend_build_needed() true, and Settings shows
    the notice until a build happens some other way. The build is
    the checkout's own (the Makefile's build target): yarn install
    against the frozen lockfile first when the dependencies are
    missing or the lockfile outdates them, then `yarn build`.
    """
    if not frontend_build_needed(repo):
        return REBUILD_NOT_NEEDED
    if not frontend_toolchain_available():
        return REBUILD_NO_TOOLCHAIN
    react = Path(repo) / "frontend" / "react"
    commands = []
    modules = react / "node_modules"
    lockfile = react / "yarn.lock"
    if (not modules.is_dir()
            or (lockfile.is_file()
                and lockfile.stat().st_mtime > modules.stat().st_mtime)):
        commands.append(["yarn", "--cwd", str(react), "install",
                         "--frozen-lockfile"])
    commands.append(["yarn", "--cwd", str(react), "build"])
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=1200)
        except (OSError, subprocess.TimeoutExpired):
            return REBUILD_FAILED
        if result.returncode:
            return REBUILD_FAILED
    return REBUILD_BUILT


_RESTARTER = """\
import os
import subprocess
import sys
import time

pid = int(sys.argv[1])
log_path, cwd = sys.argv[2], sys.argv[3]
command = sys.argv[4:]
deadline = time.time() + 120
while time.time() < deadline:
    try:
        os.kill(pid, 0)
    except OSError:
        break
    time.sleep(0.2)
with open(log_path, "ab") as log:
    subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                     start_new_session=True)
"""


def spawn_restarter(pid, argv, cwd, log_path):
    """Launch the detached restarter: it waits for `pid` to exit, then
    relaunches `argv` from `cwd` with output appended to `log_path`
    (the server log the launchers already use). It runs in its own
    session so it survives this process exiting."""
    subprocess.Popen([sys.executable, "-c", _RESTARTER, str(pid), str(log_path),
                      str(cwd), *[str(arg) for arg in argv]],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
