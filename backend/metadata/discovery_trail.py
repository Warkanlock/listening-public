"""On-demand Deep Dive stops. Only provider metadata is cached."""

import re
import threading
from urllib.parse import parse_qs, urlsplit

from metadata.artist_connections import (ConnectionsBusy, MBID, clean, credited_names, credit_key,
                                        credits, items, valid_id)

_BRANCH_SLOTS = threading.BoundedSemaphore(4)
BRANCH_PATH = re.compile(r"/api/discover/(artist|recording)/(" + MBID.pattern + ")")
RECORDING_LINKS = {"remix": ("Remix of", "Remix"), "edit": ("Edit of", "Edited recording"),
                   "performance": ("Other performance", "Other performance")}
# Match a recording destination. Provider search pages and artist profiles
# can also appear in imported URL relationships.
PROVIDERS = {
    "bandcamp.com": ("Bandcamp", r"/track/[^/]+"),
    "open.spotify.com": ("Spotify", r"/(?:intl-[a-z]+/)?track/[A-Za-z0-9]+"),
    "music.apple.com": ("Apple Music", r"/[a-z]{2}/song/(?:[^/]+/)?[0-9]+"),
    "soundcloud.com": ("SoundCloud", r"/(?!search/|discover/|charts/)[^/]+/(?!sets$|tracks$|albums$|likes$|reposts$|popular-tracks$)[^/]+"),
    "youtu.be": ("YouTube", r"/[A-Za-z0-9_-]{11}"),
    "youtube.com": ("YouTube", r"/shorts/[A-Za-z0-9_-]{11}"),
    "tidal.com": ("TIDAL", r"/(?:browse/)?track/[0-9]+"),
    "deezer.com": ("Deezer", r"/(?:[a-z]{2}/)?track/[0-9]+"),
}


def listening_source(relations):
    for rel in items(relations):
        if rel.get("ended") or rel.get("type") not in {"free streaming", "streaming"}:
            continue
        url = (rel.get("url") or {}).get("resource", "")
        if not isinstance(url, str) or chr(92) in url:
            continue
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
            path = parsed.path.rstrip("/")
            query = parse_qs(parsed.query)
            for domain, (name, pattern) in PROVIDERS.items():
                if parsed.hostname != domain and not parsed.hostname.endswith("." + domain):
                    continue
                matched = re.fullmatch(pattern, path)
                if domain == "youtube.com" and path == "/watch":
                    matched = re.fullmatch(r"[a-zA-Z0-9_-]{11}", query.get("v", [""])[0])
                if domain == "music.apple.com" and re.fullmatch(r"/[a-z]{2}/album/[^/]+/[0-9]+", path):
                    matched = re.fullmatch(r"[0-9]+", query.get("i", [""])[0])
                if matched:
                    return {"name": name, "url": url}
    return None


def recording(data):
    artist = credited_names(data)
    if not valid_id(data.get("id")) or not artist or not clean(data.get("title")):
        return None
    release = next(iter(items(data.get("releases"))), {})
    artist_id = credits(data)[0]["artist"].get("id", "")
    length = data.get("length")
    release_id = release.get("id")
    events = [rel for rel in items(data.get("relations")) if rel.get("type") == "performance"]
    date = next((clean(rel.get("begin"), 10) for rel in events if rel.get("begin")), "")
    places = [clean(rel["place"].get("name")) for rel in items(data.get("relations"))
              if rel.get("type") == "recorded at" and isinstance(rel.get("place"), dict)]
    return {"performance": " · ".join(part for part in [*places[:1], date] if part),
            "musicbrainz_id": data["id"], "title": clean(data["title"], 200), "artist": artist,
            "artist_id": artist_id, "version": clean(data.get("disambiguation")),
            "album": clean(release.get("title")), "duration": length / 1000 if type(length) is int and length > 0 else 0,
            "cover": f"https://coverartarchive.org/release/{release_id}/front-250" if valid_id(release_id) else ""}


class LibraryRecordings:
    """An unidentifiable same-title library song is never advertised as a discovery."""

    def __init__(self, records):
        self.ids = set()
        self.legacy = {}
        self.song_titles = set()
        for row in records:
            if not row.get("active", True):
                continue
            key = (credit_key(row["artist"]), credit_key(row["title"]))
            self.song_titles.add(key)
            identity = row.get("musicbrainz_id")
            if not valid_id(identity):
                uri = (row.get("uri") or row.get("identifier") or "").lower()
                match = re.fullmatch(r"(?:https?://musicbrainz\.org/recording/|musicbrainz:recording:)(" + MBID.pattern + ")", uri)
                identity = match[1] if match else None
            if valid_id(identity):
                self.ids.add(identity)
            else:
                self.legacy.setdefault(key, []).append(row)

    def contains_song(self, track):
        """Match the song regardless of a different edition's recording ID."""
        key = (credit_key(track["artist"]), credit_key(track["title"]))
        return key in self.song_titles or self.contains(track)

    def contains(self, track):
        if track["musicbrainz_id"] in self.ids:
            return True
        key = (credit_key(track["artist"]), credit_key(track["title"]))
        version = credit_key(track.get("version"))
        if version and (key[0], f"{key[1]} {version}") in self.legacy:
            return True
        for row in self.legacy.get(key, ()):
            if row.get("version") and track.get("version") and credit_key(row["version"]) != credit_key(track["version"]):
                continue
            length = row.get("length") or (row.get("duration_ms") or 0) / 1000
            if not (track.get("version") or row.get("version")):
                return True
            if not length or not track.get("duration") or abs(float(length) - track["duration"]) <= 5.5:
                return True
        return False


def recording_lead(data, label):
    track = recording(data)
    if track:
        return {"kind": "recording", "id": track["musicbrainz_id"], "name": track["title"],
                "label": label, "track": track}
    return None


def search_leads(metadata, query, known):
    rows, _ = metadata.catalog_recordings(query, 0, 24)
    leads = {}
    for row in rows:
        identity = row["musicbrainz_id"]
        length = row.get("duration_ms")
        track = {"musicbrainz_id": identity, "title": row["title"], "artist": row["creator"],
                 "album": row.get("album", ""), "version": row.get("version", ""),
                 "duration": length / 1000 if type(length) is int and length > 0 else 0}
        if identity not in leads and not known.contains(track):
            leads[identity] = {"kind": "recording", "id": identity, "name": track["title"],
                               "label": "Catalog recording", "track": track}
    return {"leads": list(leads.values())}


def branch(metadata, connections, kind, identity, known):
    if not _BRANCH_SLOTS.acquire(blocking=False):
        raise ConnectionsBusy
    try:
        if kind == "search":
            return search_leads(metadata, identity, known)
        return _branch(metadata, connections, kind, identity, known)
    finally:
        _BRANCH_SLOTS.release()


def _branch(metadata, connections, kind, identity, known):
    partial = False
    root_track = None
    suggestions = []

    def lookup(entity, key, include):
        return metadata.cached(f"trail:{entity}:{key}:{include}", 30 * 86400, lambda: metadata.musicbrainz(
            f"{entity}/{key}", {"inc": include, "fmt": "json"}))

    if kind == "artist":
        graph = connections.lookup(kind, identity)
        try:
            page = metadata.cached("trail:artist-recordings:" + identity, 7 * 86400, lambda: metadata.musicbrainz(
                "recording", {"artist": identity, "inc": "artist-credits", "limit": 24, "fmt": "json"}))
        except (OSError, ValueError):
            page = {}
            partial = True
        leads = [recording_lead(row, "Recording") for row in items(page.get("recordings"))]
        source = None
        try:
            radio = metadata.cached("trail:radio:" + identity, 7 * 86400, lambda: metadata.listenbrainz_radio(identity))
            for group in radio.values():
                for row in items(group):
                    artist_id, name = row.get("similar_artist_mbid"), clean(row.get("similar_artist_name"))
                    if valid_id(artist_id) and name and artist_id != identity:
                        suggestions.append({"kind": "artist", "id": artist_id, "name": name, "label": "Similar artist"})
                        break
        except (OSError, ValueError):
            partial = True
    else:
        data = lookup(kind, identity, "artist-credits+artist-rels+recording-rels+work-rels+url-rels+place-rels+releases")
        if data.get("id") != identity:
            raise ValueError("Unknown recording")
        root_track = recording(data)
        graph = connections.graph(kind, data)
        relations = items(data.get("relations"))
        source = listening_source(relations)
        leads = []
        related = [(rel["recording"], RECORDING_LINKS[rel["type"]][rel.get("direction") == "backward"], "")
                   for rel in relations if rel.get("recording") and rel.get("type") in RECORDING_LINKS]
        work = next((rel["work"]["id"] for rel in relations if rel.get("type") == "performance"
                     and valid_id((rel.get("work") or {}).get("id"))), None)
        if work:
            try:
                performances = lookup("work", work, "recording-rels")
            except (OSError, ValueError):
                performances = {}
                partial = True
            for rel in items(performances.get("relations")):
                if rel.get("type") == "performance" and rel.get("recording"):
                    label = "Live recording" if "live" in (rel.get("attributes") or []) else "Other performance"
                    related.append((rel["recording"], label, clean(rel.get("begin"), 10)))
        hydrated = 0
        for row, label, date in related:
            if not valid_id(row.get("id")) or row["id"] == identity or row["id"] in known.ids:
                continue
            if len(leads) >= 12:
                break
            # Relationship targets can omit release, duration, and artist credits.
            if not credited_names(row) or not row.get("length"):
                if hydrated >= 3:
                    continue
                hydrated += 1
                try:
                    row = lookup("recording", row["id"], "artist-credits+releases")
                except (OSError, ValueError):
                    partial = True
                    continue
            lead = recording_lead(row, label)
            if lead:
                if date:
                    lead["track"]["performance"] = date
                if not lead["track"]["version"] and label in {"Live recording", "Remix"}:
                    lead["track"]["version"] = "Live" if label == "Live recording" else "Remix"
                leads.append(lead)

    artists = [{"kind": "artist", "id": e["node"]["id"], "name": e["node"]["title"], "label": e["label"],
                "note": e.get("note", "")} for e in graph["edges"] if e["node"]["kind"] == "artist"]
    unique = {}
    # A fork can offer a recording, a documented connection and a recommendation at once.
    for lead in [*leads[:1], *artists[:1], *suggestions[:1], *leads[1:], *artists[1:], *suggestions[1:]]:
        if lead and lead["id"] != identity and not (lead.get("track") and known.contains(lead["track"])):
            unique.setdefault((lead["kind"], lead["id"]), lead)
    return {"leads": list(unique.values()), "source": source, "track": root_track, "partial": partial}
