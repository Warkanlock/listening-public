"""Read-only checks for the room selected by the terminal launcher."""

from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3

from system.config import default_root
from system.hosts import configured_origin


def storage_check(collection, deep):
    if not collection.is_file():
        return "No portable collection found"
    try:
        with closing(sqlite3.connect(collection.resolve().as_uri() + "?mode=ro",
                                    uri=True, timeout=2)) as database:
            if deep:
                if database.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
                    return "SQLite consistency check failed"
            database.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.Error:
        return "SQLite could not read the collection"
    return "Consistent" if deep else "Readable"


def inspect_room(config, *, deep=False):
    try:
        settings = json.loads(config.read_text(encoding="utf-8")) if config.is_file() else {}
        if not isinstance(settings, dict):
            raise ValueError
        root = Path(settings.get("folder") or default_root()).expanduser()
    except (OSError, TypeError, ValueError):
        return {"configuration": "Settings could not be read"}
    path = root
    while not path.exists() and path != path.parent:
        path = path.parent
    return {
        "configuration": "Present" if config.is_file() else "Waiting for first start",
        "collection": storage_check(root / "collection.sqlite3", deep),
        "free_gib": round(shutil.disk_usage(path).free / (1024 ** 3), 2),
        "pairing_origin": configured_origin() or "Automatic",
        "ffprobe": "Available" if shutil.which("ffprobe") else "Install FFmpeg to import and verify audio",
    }
