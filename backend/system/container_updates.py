"""Connect the owner update card to an optional Compose host updater."""

import json
import os
from pathlib import Path
import socket

from system.updater import UpdateError


def available():
    path = os.environ.get("LISTENING_ROOM_UPDATE_SOCKET")
    try:
        return bool(path and Path(path).is_socket())
    except OSError:
        return False


def request(action, **fields):
    path = os.environ.get("LISTENING_ROOM_UPDATE_SOCKET")
    if not path:
        raise UpdateError("The Compose host has not enabled in-app updates.")
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(25)
            connection.connect(path)
            connection.sendall(json.dumps({"action": action, **fields}).encode() + b"\n")
            with connection.makefile("rb") as response:
                result = json.loads(response.readline(4096))
    except (OSError, ValueError) as error:
        raise UpdateError("The Compose host updater is unavailable. Try again shortly.") from error
    if not isinstance(result, dict):
        raise UpdateError("The Compose host updater returned an invalid response.")
    if "error" in result:
        raise UpdateError(result["error"])
    return result
