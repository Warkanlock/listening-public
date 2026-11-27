"""The latest activity for each song attempted by the bulk downloader."""

import json
from pathlib import Path

from system.private_files import private_write

HISTORY = "download-activity.json"
MAX_EVENTS = 80


def recent_events(state_directory, limit=MAX_EVENTS):
    try:
        events = json.loads((Path(state_directory) / HISTORY).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(events, list):
        return []
    return [event for event in events[:limit] if isinstance(event, dict)
            and all(isinstance(event.get(field), str) for field in ("batch", "song", "state", "reason"))]


def record_event(state_directory, *, batch, song, state, reason=""):
    song = song[:180]
    event = {"batch": batch, "song": song, "state": state, "reason": reason[:120]}
    events = [prior for prior in recent_events(state_directory) if prior["song"] != song]
    private_write(Path(state_directory) / HISTORY, json.dumps([event, *events[:MAX_EVENTS - 1]]))
