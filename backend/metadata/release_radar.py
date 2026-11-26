"""A cached feed of recent releases from artists played in the room."""

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import threading
import time

from metadata.artist_connections import artist_ids, clean, credited_names, items, valid_id
from metadata.music_profiles import cover_art
from metadata.weekly_discovery import LOOKUP_ERRORS, listened_artists


PATH = "/api/discover/releases"
WINDOW_DAYS = 60
MAX_RELEASES = 24
REFRESH_SECONDS = 86400
RETRY_SECONDS = 900
WAITING_SECONDS = 60
RELEASE_TYPES = ("Album", "EP", "Single")


def _bounds(today):
    return (today - timedelta(days=WINDOW_DAYS - 1)).isoformat(), today.isoformat()


def _release(row, root, bounds):
    identity = row.get("id")
    title = clean(row.get("title"))
    kind = row.get("primary-type")
    released = row.get("first-release-date")
    if not (valid_id(identity) and title and kind in RELEASE_TYPES and root["id"] in artist_ids(row)):
        return None
    try:
        if date.fromisoformat(released).isoformat() != released or not bounds[0] <= released <= bounds[1]:
            return None
    except (TypeError, ValueError):
        return None
    return {"id": identity, "title": title, "kind": kind, "date": released,
            "artist": clean(credited_names(row)) or root["title"],
            "cover": cover_art(identity, "release-group"), "seed_artist": root["title"]}


class ReleaseRadar:
    def __init__(self, metadata, connections, *, today=date.today, now=time.time):
        self.metadata = metadata
        self.connections = connections
        self.today = today
        self.now = now
        self._lock = threading.Lock()
        self._building = set()
        self._retry_after = {}

    @staticmethod
    def _key(room):
        scope = hashlib.sha256(room.encode()).hexdigest()
        return f"release-radar:v1:{scope}"

    @staticmethod
    def _saved(cache, key):
        row = cache.cache_get(key)
        if row:
            try:
                saved = json.loads(row["body"])
            except json.JSONDecodeError:
                return None, None
            if isinstance(saved, dict) and saved.get("state") in ("waiting", "empty", "ready") and isinstance(saved.get("releases"), list):
                return saved, row["saved_at"]
        return None, None

    def status(self, cache, requests, room, catalog, events):
        today, now = self.today(), self.now()
        key = self._key(room)
        with self._lock:
            self._retry_after = {identity: until for identity, until in self._retry_after.items() if until > now}
            saved, stamp = self._saved(cache, key)
            state = saved["state"] if saved else "building"
            lifetime = WAITING_SECONDS if state == "waiting" else REFRESH_SECONDS
            due = stamp is None or now - stamp >= lifetime
            if due and key not in self._building and key not in self._retry_after:
                self._building.add(key)
                threading.Thread(target=self._build, args=(cache, requests, key, catalog, events, today),
                                 name="release-radar", daemon=True).start()
            refreshing = key in self._building
            if due and saved and state != "waiting":
                state = "stale"
            elif due:
                state = "building" if refreshing else "unavailable"
        start, end = _bounds(today)
        releases = [row for row in saved["releases"] if start <= row["date"] <= end] if saved else []
        if state == "ready" and not releases:
            state = "empty"
        if saved and saved["state"] == "waiting":
            stamp = None
        return {"state": state, "refreshing": refreshing, "releases": releases,
                "updated_at": datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp is not None else None}

    def _build(self, cache, requests, key, catalog, events, today):
        retry = False
        try:
            week = (today - timedelta(days=today.weekday())).isoformat()
            seeds = listened_artists(catalog, events, requests.downloaded_audio(custom_only=True), week=week)
            releases = self.generate(seeds, today)
            result = {"state": "ready" if releases else "empty" if seeds else "waiting", "releases": releases}
            cache.cache_put(key, json.dumps(result, ensure_ascii=False), self.now())
        except LOOKUP_ERRORS:
            retry = True
        finally:
            with self._lock:
                if retry:
                    self._retry_after[key] = self.now() + RETRY_SECONDS
                self._building.discard(key)

    def generate(self, seeds, today):
        bounds = _bounds(today)
        groups, visited = {}, set()
        for seed in seeds:
            root = self.connections.artist_match(seed)
            if not root or root["id"] in visited:
                continue
            visited.add(root["id"])
            query = (f'arid:{root["id"]} AND firstreleasedate:[{bounds[0]} TO {bounds[1]}]'
                     " AND primarytype:(album OR ep OR single)")
            result = self.metadata.musicbrainz("release-group", {"query": query, "fmt": "json", "limit": 100})
            if not isinstance(result, dict) or not isinstance(result.get("release-groups"), list):
                raise ValueError("Unexpected MusicBrainz release results")
            for row in items(result["release-groups"]):
                release = _release(row, root, bounds)
                if release:
                    groups.setdefault(release["id"], release)
        return sorted(groups.values(), key=lambda release: (release["date"], release["id"]), reverse=True)[:MAX_RELEASES]
