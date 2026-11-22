#!/bin/sh
# Install the user service for a source checkout on Debian or Ubuntu.
set -eu
checkout=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
unit=listening-room.service
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [ "$(uname -s)" != Linux ] || ! command -v apt-get >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
    echo "The user service requires Debian or Ubuntu booted with systemd." >&2
    exit 1
fi
if ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "Log in to the server as the account that will own the room, then run ./listening setup --systemd." >&2
    exit 1
fi
if ! command -v ffprobe >/dev/null 2>&1; then
    echo "Install FFmpeg with sudo apt install ffmpeg, then repeat setup." >&2
    exit 1
fi
# A direct invocation can also pass a config relative to its working directory.
if [ -n "${LISTENING_ROOM_CONFIG:-}" ]; then
    LISTENING_ROOM_CONFIG=$(python3 -c 'import os; from pathlib import Path; print(Path(os.environ["LISTENING_ROOM_CONFIG"]).expanduser().resolve())')
    export LISTENING_ROOM_CONFIG
fi
cd "$checkout"
# Reuse the same locked frontend build and launcher registration as a local host.
./listening setup --non-interactive --client web
./listening server configure systemd
mkdir -p "$unit_dir"
# systemd starts in a fresh login environment. Keep an explicit host config
# attached to this service across logout and reboot, including paths with spaces.
python3 - "scripts/$unit" "$unit_dir/$unit" <<'PYTHON'
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, "backend")
from system.private_files import private_write

unit = Path(sys.argv[1]).read_text()
if os.environ.get("LISTENING_ROOM_CONFIG"):
    config = Path(os.environ["LISTENING_ROOM_CONFIG"]).expanduser().resolve()
    value = json.dumps("LISTENING_ROOM_CONFIG=" + str(config).replace("%", "%%"), ensure_ascii=False)
    unit = unit.replace("[Service]\n", f"[Service]\nEnvironment={value}\n")
private_write(Path(sys.argv[2]), unit)
PYTHON
systemctl --user daemon-reload
systemctl --user enable "$unit"
if ! ./listening server restart; then
    journalctl --user -u "$unit" -n 20 --no-pager >&2 || true
    exit 1
fi

if [ "$(loginctl show-user "$(id -un)" -p Linger --value)" = yes ]; then
    echo "Linger is enabled. The server starts at boot and stays available after logout."
else
    echo "For service after logout and at the next boot, run: sudo loginctl enable-linger $(id -un)"
fi
echo "Manage this service with listening server status, restart or stop."
