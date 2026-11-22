#!/usr/bin/env python3
"""Apply the latest Listening revision through the private host updater."""
import argparse
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from system.container_updates import request
from system.updater import UpdateError


def update():
    status = request("check")
    revision = status["remote"]
    if not revision:
        raise UpdateError("The updater returned no revision from main.")
    if status["current"] == revision and not status["applying"]:
        print(f"Listening {revision[:7]} is already current.", flush=True)
        return
    if not status["applying"]:
        request("apply", revision=revision)
    print(f"Waiting for Listening update to {revision[:7]}.", flush=True)
    deadline = time.monotonic() + 1500
    while time.monotonic() < deadline:
        status = request("status")
        if not status["applying"]:
            if status["update_error"]:
                raise UpdateError(status["update_error"])
            if status["current"] != revision:
                raise UpdateError("The expected Listening revision did not start.")
            print(f"Listening {revision[:7]} is running.", flush=True)
            return
        time.sleep(5)
    raise UpdateError("Listening update is still running after 25 minutes. Check the host updater log.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    args = parser.parse_args()
    os.environ["LISTENING_ROOM_UPDATE_SOCKET"] = str(args.socket)
    try:
        update()
    except (UpdateError, KeyError, TypeError) as error:
        parser.exit(1, f"Listening update failed: {error}\n")
