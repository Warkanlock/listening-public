#!/usr/bin/env python3
"""Environment preflight for Listening Room.

Run by `make install` (the last step, inside the Poetry environment). Checks the
few external tools the room leans on and says, in plain words, what is
missing and how to fix it, reusing the launchers' wording and severity
(run.sh). The exit code is 1 only on a hard failure:
python3 missing or older than 3.9. ffprobe is a warning with a
fix, exactly as in run.sh: the room opens without it, and downloads
and verification are what wait on it. git is a warning too, and only
inside a git checkout: the in-app update (plan 023) needs it, and a
tarball install still runs without it.
"""

import shutil
import sys
from pathlib import Path

MIN_PYTHON = (3, 9)

PYTHON_MISSING = "Install Python 3 first. See README.md."
PYTHON_TOO_OLD = "Install Python 3.9 or newer first. See README.md."
FFPROBE_WARNING = ("Warning: ffprobe (FFmpeg) is missing, so downloads and "
                   "verification stay off until it is installed. Fix: brew "
                   "install ffmpeg on macOS (on other systems, see the "
                   "requirements in README.md).")
GIT_WARNING = ("Warning: git is missing, so the in-app update cannot check "
               "for new versions. See README.md.")

ROOT = Path(__file__).resolve().parent.parent


def evaluate(which, version_info, in_checkout):
    """Return (failures, warnings) for the given environment.

    The seams are parameters so tests drive the checks without touching
    the host: `which` is a PATH lookup (name -> path or None, like
    shutil.which), `version_info` is the python version tuple to judge
    (None when python itself is missing), and `in_checkout` says
    whether this tree is a git checkout.
    """
    failures = []
    warnings = []
    if version_info is None:
        failures.append(PYTHON_MISSING)
    elif tuple(version_info[:2]) < MIN_PYTHON:
        failures.append(PYTHON_TOO_OLD)
    if which("ffprobe") is None:
        warnings.append(FFPROBE_WARNING)
    if in_checkout and which("git") is None:
        warnings.append(GIT_WARNING)
    return failures, warnings


def main(which=None, version_info=None, in_checkout=None, out=None, err=None):
    if which is None:
        which = shutil.which
    if version_info is None:
        version_info = sys.version_info
    if in_checkout is None:
        in_checkout = (ROOT / ".git").exists()
    if out is None:
        out = sys.stdout
    if err is None:
        err = sys.stderr
    failures, warnings = evaluate(which, version_info, in_checkout)
    for line in failures:
        print(line, file=err)
    for line in warnings:
        print(line, file=err)
    if failures:
        return 1
    version = ".".join(str(part) for part in version_info[:3])
    suffix = (" Warnings above name the optional pieces still missing."
              if warnings else "")
    print(f"Preflight OK: python {version}.{suffix}", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
