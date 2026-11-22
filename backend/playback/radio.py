"""Optional Radio providers and owner-curated station data."""

import re
from urllib.parse import parse_qs, urlparse

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")

PROVIDERS = frozenset({"youtube", "somafm"})


def migrate_providers(settings, *, previous_room):
    """Retain access for a room created before providers were optional."""
    settings.setdefault("radio_providers", sorted(PROVIDERS) if previous_room else [])


def provider_enabled(settings, provider):
    return provider in settings.get("radio_providers", [])


def source_enabled(settings, source):
    provider = {"youtube": "youtube", "live": "somafm"}.get(source)
    return provider is None or provider_enabled(settings, provider)


RADIO_PRESETS = ("Study", "Ambient", "White noise", "Brown noise", "Minimal", "Lo-fi", "House",
                 "Deep focus", "Ambient noise", "Jazz", "Classical", "Downtempo")
# Official continuous streams. Listeners can use these stations even in
# rooms without a YouTube Data API key.
LIVE_STATIONS = {
    "Study": ("Groove Salad", "groovesalad"),
    "Ambient": ("Drone Zone", "dronezone"),
    "Minimal": ("Deep Space One", "deepspaceone"),
    "Lo-fi": ("Groove Salad Classic", "gsclassic"),
    "Deep focus": ("Deep Space One", "deepspaceone"),
    "Ambient noise": ("Drone Zone", "dronezone"),
    "House": ("The Trip", "thetrip"),
    "Jazz": ("Sonic Universe", "sonicuniverse"),
    "Downtempo": ("Beat Blender", "beatblender"),
}
MAX_STATIONS = 40
MAX_SEEDS = 100
MAX_NAME = 60

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
                  "music.youtube.com", "www.youtube-nocookie.com",
                  "youtube-nocookie.com"}


def video_id_from(text):
    """The bare video ID a pasted reference names, or None.

    Forgiving at the edges, strict in the middle (grill round 3):
    a bare ID, a watch URL, a youtu.be short link, a Shorts or Music
    URL, a live or embed URL all normalize to the bare ID; anything
    else (another host, a playlist link, a channel page, prose)
    names no video and is refused by the caller.
    """
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    if VIDEO_ID.fullmatch(candidate):
        return candidate
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 1 and VIDEO_ID.fullmatch(parts[0]):
            return parts[0]
        return None
    if host not in _YOUTUBE_HOSTS:
        return None
    if parsed.path == "/watch":
        values = parse_qs(parsed.query).get("v", [])
        if len(values) == 1 and VIDEO_ID.fullmatch(values[0]):
            return values[0]
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) == 2 and parts[0] in {"shorts", "live", "embed", "v"}:
        if VIDEO_ID.fullmatch(parts[1]):
            return parts[1]
    return None


def clean_stations(value):
    """Validate and normalize a stations payload for storage.

    Raises ValueError with the one plain sentence the surfaces show.
    Names are trimmed, capped, and unique (case-insensitively); seeds
    pass through video_id_from, so a stored seed is always a bare ID;
    duplicates within a station collapse, keeping first order.
    """
    if not isinstance(value, list):
        raise ValueError("Stations must be a list")
    if len(value) > MAX_STATIONS:
        raise ValueError(f"Keep at most {MAX_STATIONS} stations")
    stations = []
    seen_names = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("Each station needs a name and its seed videos")
        name = str(entry.get("name", "")).strip()[:MAX_NAME]
        if not name:
            raise ValueError("Each station needs a name")
        if name.casefold() in seen_names:
            raise ValueError(f"Two stations are named {name}")
        seen_names.add(name.casefold())
        raw_seeds = entry.get("seeds", [])
        if not isinstance(raw_seeds, list):
            raise ValueError(f"{name}'s seeds must be a list")
        if len(raw_seeds) > MAX_SEEDS:
            raise ValueError(f"Keep at most {MAX_SEEDS} seeds per station")
        seeds = []
        for raw in raw_seeds:
            video_id = video_id_from(raw)
            if video_id is None:
                raise ValueError("That link does not name a YouTube video")
            if video_id not in seeds:
                seeds.append(video_id)
        stations.append({"name": name, "seeds": seeds})
    return stations


def stations_from(settings):
    """Read saved mixes without changing their order or writing defaults."""
    try:
        return clean_stations(settings.get("radio_stations", []))
    except ValueError:
        return []


def live_catalog():
    return {name: {"title": title, "url": f"https://ice2.somafm.com/{slug}-128-mp3"}
            for name, (title, slug) in LIVE_STATIONS.items()}
