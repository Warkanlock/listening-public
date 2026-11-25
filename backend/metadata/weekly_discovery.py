"""A room's weekly queue of recordings reached through its listening history."""

from collections import defaultdict
from datetime import date, timedelta
import hashlib
from itertools import zip_longest
import json
import sqlite3
import threading
import time

from metadata.artist_connections import ConnectionsBusy, artist_ids, clean, credit_key, items, valid_id
from metadata.discovery_trail import LibraryRecordings, recording


PATH = "/api/discover/weekly"
MAX_DEPTH = 3
ARTISTS_PER_LEVEL = 4
MAX_TRACKS = 24
SONGS_PER_ARTIST = 3
LOOKUP_ERRORS = (ConnectionsBusy, OSError, ValueError, sqlite3.Error)


def download_progress(tracks):
    attempts = {}
    for track in tracks:
        request = track.get("request")
        if request and (not track.get("in_library") or request.get("audio") or request["status"] == "missing"):
            attempts[request["id"]] = request
    total = len(attempts)
    downloaded = sum(request["status"] == "downloaded" and bool(request.get("audio")) for request in attempts.values())
    pending = sum(request["status"] in ("queued", "searching", "waiting_space") for request in attempts.values())
    return {"total": total, "downloaded": downloaded, "pending": pending,
            "needs_attention": total - downloaded - pending,
            "percent": downloaded * 100 // total if total else 0}


def _song_key(track):
    return credit_key(track.get("artist")), credit_key(track.get("title"))


def known_songs(catalog, downloaded):
    return LibraryRecordings((*catalog.records, *downloaded))


def listened_artists(catalog, events, downloaded, *, week):
    """Rank room artists by actual listens, rotating the lower-ranked roots."""
    history = events.history()
    signals = defaultdict(list)
    seen = set()
    for record in catalog.records:
        artist = clean(record.get("artist"))
        identity = record.get("linked_recording") or record["key"]
        if not artist or identity in seen or not record.get("active", True):
            continue
        seen.add(identity)
        signals[artist].extend(history[key] for key in catalog.recording_keys(record) if key in history)
    requests = {f"custom-{row['id']}": row for row in downloaded if row["track_index"] is None}
    for event in events.recent_started(250):
        row = requests.get(event["ref"])
        if row and (artist := clean(row.get("artist"))):
            signals[artist].append((event["ts"], 0))

    ranked = {}
    for artist, plays in signals.items():
        if not plays:
            continue
        key = credit_key(artist)
        stamp = max((at or 0 for at, _ in plays), default=0)
        completed = sum(count for _, count in plays)
        if key not in ranked or (stamp, completed) > ranked[key][:2]:
            ranked[key] = (stamp, completed, artist)
    ordered = sorted(ranked.values(), key=lambda row: (-row[1], -row[0], credit_key(row[2])))
    favorites, rotation = ordered[:2], ordered[2:]
    if rotation:
        offset = (date.fromisoformat(week).toordinal() // 7) % len(rotation)
        rotation = rotation[offset:] + rotation[:offset]
    return [row[2] for row in (*favorites, *rotation)][:ARTISTS_PER_LEVEL]


class WeeklyDiscovery:
    def __init__(self, metadata, connections, *, today=date.today, now=time.time):
        self.metadata = metadata
        self.connections = connections
        self._today = today
        self._now = now
        self._lock = threading.Lock()
        self._building = False
        self._retry = None

    def week(self):
        today = self._today()
        return (today - timedelta(days=today.weekday())).isoformat()

    @staticmethod
    def _key(room, week):
        scope = hashlib.sha256(room.encode()).hexdigest()
        return f"weekly-discovery:v1:{scope}:{week}"

    def saved(self, cache, room, week):
        row = cache.cache_get(self._key(room, week))
        return json.loads(row["body"]) if row else None

    def status(self, cache, requests, room, catalog, events):
        week = self.week()
        key = self._key(room, week)
        with self._lock:
            tracks = self.saved(cache, room, week)
            if tracks is not None:
                return {"week": week, "state": "ready", "tracks": tracks}
            state = "building"
            if self._retry and self._retry[0] == key and self._now() < self._retry[1]:
                state = self._retry[2]
            elif not self._building:
                self._building = True
                threading.Thread(target=self._build, args=(cache, requests, key, week, catalog, events),
                                 name="weekly-discovery", daemon=True).start()
        return {"week": week, "state": state, "tracks": []}

    def _build(self, cache, requests, key, week, catalog, events):
        state = None
        try:
            downloaded = requests.downloaded_audio(custom_only=True)
            seeds = listened_artists(catalog, events, downloaded, week=week)
            if seeds:
                tracks = self.generate(seeds, known_songs(catalog, downloaded), week)
                cache.cache_put(key, json.dumps(tracks, ensure_ascii=False), self._now())
            else:
                state = "waiting"
        except LOOKUP_ERRORS:
            state = "unavailable"
        finally:
            with self._lock:
                if state:
                    self._retry = (key, self._now() + (10 if state == "waiting" else 60), state)
                self._building = False

    def _related(self, identity):
        graph, radio, failed = {}, {}, False
        try:
            graph = self.connections.lookup("artist", identity)
        except LOOKUP_ERRORS:
            failed = True
        try:
            radio = self.metadata.cached("trail:radio:" + identity, 7 * 86400,
                                         lambda: self.metadata.listenbrainz_radio(identity))
        except LOOKUP_ERRORS:
            failed = True
        links = [(edge["node"]["id"], edge["node"]["title"])
                 for edge in graph.get("edges", ()) if edge["node"]["kind"] == "artist"]
        if isinstance(radio, dict):
            links.extend((row.get("similar_artist_mbid"), clean(row.get("similar_artist_name")))
                         for group in radio.values() for row in items(group))
        return list(dict.fromkeys((other, name) for other, name in links
                                  if valid_id(other) and other != identity and name)), failed

    def _recordings(self, identity):
        page = self.metadata.cached("weekly-discovery:recordings:" + identity, 7 * 86400,
                                    lambda: self.metadata.musicbrainz("recording", {
                                        "artist": identity, "inc": "artist-credits",
                                        "limit": 35, "fmt": "json",
                                    }))
        if not isinstance(page, dict):
            raise ValueError("Unexpected artist recording results")
        return [track for item in items(page.get("recordings")) if identity in artist_ids(item)
                and (track := recording(item)) and 0 < track["duration"] <= 86400]

    def generate(self, seeds, known, week):
        """Breadth-first traversal with a separate budget at each graph depth."""
        frontier, seen_ids, songs, failed = [], set(), [], False
        for seed in seeds:
            try:
                root = self.connections.artist_match(seed)
            except LOOKUP_ERRORS:
                failed = True
                continue
            if root and root["id"] not in seen_ids:
                seen_ids.add(root["id"])
                frontier.append((root["id"], [root["title"]]))
        seen_tracks, seen_titles = set(), set()
        for depth in range(MAX_DEPTH + 1):
            expansions = []
            for identity, trail in frontier:
                try:
                    tracks = self._recordings(identity)
                except LOOKUP_ERRORS:
                    tracks, failed = [], True
                tracks.sort(key=lambda item: hashlib.sha256((week + item["musicbrainz_id"]).encode()).digest())
                count = 0
                for track in tracks:
                    track_id, title = track["musicbrainz_id"], _song_key(track)
                    if track_id in seen_tracks or title in seen_titles or known.contains_song(track):
                        continue
                    seen_tracks.add(track_id)
                    seen_titles.add(title)
                    songs.append({**track, "id": track_id, "artist_id": identity, "depth": depth,
                                  "trail": trail, "seconds": track["duration"]})
                    count += 1
                    if count == SONGS_PER_ARTIST:
                        break
                if depth < MAX_DEPTH:
                    links, error = self._related(identity)
                    failed = failed or error
                    expansions.append([(other, [*trail, name]) for other, name in links if other not in seen_ids])
            following = []
            # Draw a neighbor from each root before giving any root a second slot.
            for batch in zip_longest(*expansions):
                for link in batch:
                    if link and link[0] not in seen_ids:
                        seen_ids.add(link[0])
                        following.append(link)
                    if len(following) == ARTISTS_PER_LEVEL:
                        break
                if len(following) == ARTISTS_PER_LEVEL:
                    break
            frontier = following
            if not frontier:
                break
        if not songs and failed:
            raise OSError("The music catalog is temporarily unavailable")
        # Interleave depths so the weekly cap cannot silently discard distant picks.
        by_depth = [[song for song in songs if song["depth"] == depth] for depth in range(MAX_DEPTH + 1)]
        return [group[index] for index in range(max(map(len, by_depth)))
                for group in by_depth if index < len(group)][:MAX_TRACKS]
