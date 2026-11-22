"""Request a short idle cutover lease from a running Listening container."""

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from downloads.engine_contract import engine_selection, engines_ready
from system.cli import selected_config
from system.config import resolve_config_path
from system.server_control import control_path, request


def main():
    action = sys.argv[1]
    config = selected_config(resolve_config_path())
    if action == "engines" and len(sys.argv) == 3:
        try:
            settings = json.loads(config.read_text())
            return 0 if not engine_selection(settings) or engines_ready(settings, server_release=sys.argv[2]) else 1
        except (OSError, ValueError):
            return 1
    if action not in ("claim", "release"):
        return 1
    try:
        result = request(control_path(config), action)
    except (OSError, ValueError):
        return 1
    if not result or type(result.get("idle")) is not bool:
        return 1
    return 0 if result["idle"] else os.EX_TEMPFAIL


if __name__ == "__main__":
    sys.exit(main())
