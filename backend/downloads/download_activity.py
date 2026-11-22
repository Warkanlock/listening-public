"""Read a small, structured history from Sockseek batch logs."""

import re
from pathlib import Path

from system.logtail import tail_lines


EVENT = re.compile(r"SongJob: (searching|downloading|succeeded|failed)(?: \[([^\]]+)\])?: (.+)")


def parse_event(line):
    match = EVENT.search(line)
    if not match:
        return None
    state, reason, details = match.groups()
    # Download and success lines append a source file after the song name.
    song = details.rsplit(": ", 1)[0] if state in {"downloading", "succeeded"} and ": " in details else details
    return {"state": state, "song": song[:180], "reason": (reason or "")[:120]}


def recent_events(state_directory, limit=80, batches=4):
    paths = sorted(Path(state_directory).glob("batch-*.log"),
                   key=lambda path: path.stat().st_mtime_ns, reverse=True)[:batches]
    events = []
    seen = set()
    for path in paths:
        try:
            lines = tail_lines(path, 700)
        except OSError:
            continue
        for line in reversed(lines):
            event = parse_event(line)
            if not event:
                continue
            # One row per song: the scan runs newest first, so the
            # first event seen for a song is its latest state. The
            # dialog presents rows as per-song status, and a song that
            # was searched, downloaded, then succeeded must read as
            # one Ready row, not three contradictory ones.
            key = event["song"]
            if key in seen:
                continue
            seen.add(key)
            event["batch"] = path.stem.removeprefix("batch-").replace("-", "–")
            events.append(event)
            if len(events) >= limit:
                return events
    return events
