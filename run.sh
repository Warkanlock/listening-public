#!/bin/sh
set -eu
cd "$(dirname "$0")"
# The server runs under the Poetry venv python when the venv exists,
# so the interpreter is the project's pinned one; the server spawns
# its workers with sys.executable, so they inherit it. Resolution:
# `poetry env info --executable` first, then the conventional .venv
# path. On a machine without Poetry a bare checkout still runs, on
# plain python3.
PYTHON=''
if command -v poetry >/dev/null 2>&1; then
  PYTHON=$(poetry env info --executable 2>/dev/null || true)
fi
if [ -z "$PYTHON" ] && [ -x .venv/bin/python ]; then
  PYTHON=.venv/bin/python
fi
if [ -z "$PYTHON" ]; then
  PYTHON=python3
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Install Python 3 first. See README.md.' >&2
  exit 1
fi
if ! command -v ffprobe >/dev/null 2>&1; then
  echo 'Warning: ffprobe (FFmpeg) is missing, so downloads and verification stay off until it is installed. Fix: brew install ffmpeg on macOS (on other systems, see the requirements in README.md).' >&2
fi
exec "$PYTHON" backend/server.py "$@"
