"""Validate private Radio history before storing it for another client."""

import re

from playback.radio import MAX_SEEDS, VIDEO_ID


def _text(value, limit, *, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError("Invalid Radio history.")
    value.encode("utf-8")
    return value


def _video(value):
    return isinstance(value, str) and VIDEO_ID.fullmatch(value) is not None


def validate_action(body, live_stations):
    operation = body.get("op")
    tuning = body.get("tuning")
    if operation not in ("tune", "update", "clear") or not isinstance(tuning, str) or not re.fullmatch(r"[a-f0-9]{32}", tuning):
        raise ValueError("Invalid Radio history action.")
    revision = body.get("expected_revision")
    if revision is not None and (operation != "tune" or type(revision) is not int or not 0 <= revision < 2**53):
        raise ValueError("Invalid Radio history revision.")
    return operation, tuning, (None if operation == "clear" else _history(body.get("history"), live_stations)), revision


def _history(value, live_stations):
    if not isinstance(value, dict) or not isinstance(value.get("source"), dict):
        raise ValueError("Invalid Radio history.")
    source = value["source"]
    kind = source.get("kind")
    name = _text(source.get("name"), 120, required=True)
    current = value.get("current")
    selected = {"kind": kind, "name": name}
    title = _text(value.get("title"), 500)
    if kind == "library":
        if current is not None and (type(current) is not int or not 0 < current < 10**10):
            raise ValueError("Invalid Radio song.")
    elif kind == "live":
        stream = live_stations.get(name)
        if stream is None or current is not None:
            raise ValueError("Unknown live Radio station.")
        selected["stream"] = stream
        title = stream["title"]
    elif kind == "noise":
        if source.get("profile") not in ("white", "brown") or current is not None:
            raise ValueError("Unknown Radio noise.")
        selected["profile"] = source["profile"]
    elif kind == "youtube":
        seeds = source.get("seeds")
        if (not isinstance(seeds, list) or not 1 <= len(seeds) <= MAX_SEEDS
                or not all(map(_video, seeds)) or (current is not None and not _video(current))):
            raise ValueError("Invalid YouTube Radio mix.")
        selected["seeds"] = seeds
    else:
        raise ValueError("Unknown Radio source.")
    position = value.get("position", 0)
    sample = value.get("sample", 0)
    if (type(position) not in (int, float) or not 0 <= position <= 86400
            or type(sample) is not int or not 0 <= sample < 2**53):
        raise ValueError("Invalid Radio position.")
    # Older YouTube clients send unnumbered station changes on the same tuning.
    seekable = kind == "library" or (kind == "youtube" and "sample" in value)
    resume = {"position": position, "sample": sample} if seekable else {}
    return {"source": selected, "current": current, **resume,
            "title": title, "channel": _text(value.get("channel"), 500)}
