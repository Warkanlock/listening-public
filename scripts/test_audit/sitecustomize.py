"""Report SQLite connection attempts that the client library rejects before open()."""

import json
import os
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

from audit_paths import comparison_path


def install():
    details = os.environ.get("LISTENING_TEST_AUDIT_DETAILS")
    if not details:
        return
    manifest = json.loads(Path(details).read_text(encoding="utf-8"))
    directories = tuple(comparison_path(path) for path in manifest["directories"])
    files = {comparison_path(path) for path in manifest["files"]}
    events = manifest["events"]
    hook_dir = str(Path(__file__).resolve().parent)

    def reject(message):
        with open(events, "a", encoding="utf-8") as log:
            log.write(f"Python (PID {os.getpid()}) {message}\n")
        raise PermissionError(message)

    def check(event, args):
        if event == "subprocess.Popen" and args[3] is not None:
            # Tests sometimes construct a fresh env for a child interpreter.
            env = args[3]
            paths = env.get("PYTHONPATH", "").split(os.pathsep)
            if hook_dir not in paths:
                env["PYTHONPATH"] = os.pathsep.join([hook_dir, *filter(None, paths)])
            env["LISTENING_TEST_AUDIT_DETAILS"] = details
        if event != "sqlite3.connect":
            return
        name = os.fsdecode(args[0])
        if name.startswith("file:"):
            name = unquote(urlsplit(name).path)
        path = Path(os.path.realpath(name))
        key = comparison_path(path)
        if key in files or any(key.is_relative_to(folder) for folder in directories):
            reject(f"attempted sqlite3.connect on {path}")

    sys.addaudithook(check)


install()
