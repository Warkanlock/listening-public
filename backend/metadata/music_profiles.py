"""Artist pages and playable album track lists backed by MusicBrainz."""

import re
from threading import BoundedSemaphore
from urllib.error import HTTPError

from metadata.artist_connections import (
    ConnectionsBusy, artist_ids, clean, credit_key, credited_names, items, valid_id,
)

PROFILE_PATH = re.compile(
    r"/api/music-profile/(artist|album)/((?:custom-)?[1-9][0-9]{0,9})"
    r"|/api/music-profile/(artist|release-group|artist-highlights)/([a-f0-9-]{36})"
)
TRACK_LIMIT = 120
POPULAR_TRACK_LIMIT = 10
POPULAR_RELEASE_LIMIT = 6


def recording_reference(record):
    uri = record.get("uri") or record.get("identifier") or ""
    identity = uri.removeprefix("https://musicbrainz.org/recording/")
    return identity if identity != uri and valid_id(identity) else ""


def library_track(record, status):
    return {"track_id": record["index"], "ready": status(record["index"]) == "verified",
            "title": record["title"], "artist": record["artist"], "album": record.get("album", ""),
            "duration": record.get("length") or 0, "musicbrainz_id": recording_reference(record)}



def catalog_release_type(item):
    secondary = item.get("secondary-types")
    if isinstance(secondary, list) and "Compilation" in secondary:
        return "Compilation"
    return clean(item.get("primary-type"))


def cover_art(identity, kind="release"):
    return f"https://coverartarchive.org/{kind}/{identity}/front-250" if valid_id(identity) else ""


def popularity_rows(payload, identity_field, limit):
    """Keep a bounded, deduplicated ranking from ListenBrainz."""
    def count(row):
        value = row.get("total_listen_count")
        return value if type(value) is int and value > 0 else 0

    seen = set()
    for row in sorted(items(payload), key=count, reverse=True):
        identity = row.get(identity_field)
        if valid_id(identity) and count(row) and identity not in seen:
            seen.add(identity)
            yield row, identity, count(row)
            if len(seen) >= limit:
                return


def release_tracks(release):
    for medium in items(release.get("media")):
        disc = medium.get("position")
        for entry in items(medium.get("tracks")):
            recording = entry.get("recording")
            if not isinstance(recording, dict) or not valid_id(recording.get("id")):
                continue
            title = clean(entry.get("title") or recording.get("title"))
            artist = clean(credited_names(entry) or credited_names(recording) or credited_names(release))
            if not title or not artist:
                continue
            length = entry.get("length") or recording.get("length")
            yield {"title": title, "artist": artist, "album": clean(release.get("title")),
                   "musicbrainz_id": recording["id"], "version": clean(recording.get("disambiguation")),
                   "disc": disc if type(disc) is int and disc > 0 else 1,
                   "number": clean(entry.get("number"), 12),
                   "duration": length / 1000 if type(length) is int and 0 < length <= 86400000 else 0}


class MusicProfiles:
    def __init__(self, connections):
        self.connections = connections
        self.slots = BoundedSemaphore(2)
        self.highlight_slots = BoundedSemaphore(2)

    def lookup(self, kind, identity, seed, records, status, cover=lambda record: "", history=None):
        album = kind != "artist"
        artist = seed["artist"] if seed else ""
        title = (seed.get("album") or seed["title"]) if seed and album else artist
        try:
            if not self.slots.acquire(blocking=False):
                raise ConnectionsBusy
            try:
                public = self.album(identity, seed) if album else self.artist(identity, seed)
            finally:
                self.slots.release()
        except (ConnectionsBusy, OSError, ValueError):
            if not seed:
                raise
            public = {"unavailable": True}
        title = public.get("title") or title
        artist = public.get("artist") or artist
        if album:
            return {**public, "kind": "album", "title": title, "artist": artist,
                    "tracks": self.album_library(public, records, artist, title, status, seed)}
        credit_keys = {credit_key(title), credit_key((seed or {}).get("artist"))} - {""}
        matching = [record for record in records if credit_key(record.get("artist")) in credit_keys]
        history = history or {}
        local_tracks, albums, counted = [], {}, set()
        for record in matching:
            _, plays = history.get(record.get("key"), (None, 0))
            track = {**library_track(record, status), "cover": cover(record)}
            if plays:
                track["plays"] = plays
            local_tracks.append(track)
            if name := track["album"]:
                album_key = credit_key(name)
                entry = albums.setdefault(album_key, {"title": name, "track_id": track["track_id"]})
                if track["cover"] and not entry.get("cover"):
                    entry["cover"] = track["cover"]
                recording = record.get("linked_recording") or record.get("key") or track["track_id"]
                if plays and (album_key, recording) not in counted:
                    entry["plays"] = entry.get("plays", 0) + plays
                    counted.add((album_key, recording))
        ranked_albums = sorted(albums.values(), key=lambda item: -item.get("plays", 0))
        return {**public, "kind": "artist", "title": title, "library_albums": ranked_albums[:60],
                "library": {"tracks": len(local_tracks), "ready": sum(track["ready"] for track in local_tracks),
                            "albums": len(albums)},
                "tracks": sorted(local_tracks, key=lambda track: (-track.get("plays", 0), not track["ready"],
                                                                  credit_key(track["title"])))[:TRACK_LIMIT]}

    def highlights(self, identity, records, status, cover):
        if not self.highlight_slots.acquire(blocking=False):
            raise ConnectionsBusy
        try:
            tracks, releases = [], []
            try:
                popular = self.connections.metadata.artist_popularity(identity)
                tracks = self.popular_tracks(popular, records, status, cover)
            except (OSError, ValueError):
                pass
            try:
                popular = self.connections.metadata.artist_popularity(identity, releases=True)
                for row, release_id, count in popularity_rows(popular, "release_group_mbid", 100):
                    release = row.get("release_group")
                    if not isinstance(release, dict) or not (title := clean(release.get("name"))):
                        continue
                    releases.append({"id": release_id, "title": title, "detail": clean(release.get("type")),
                                     "year": clean(release.get("date"), 4), "listens": count,
                                     "cover": cover_art(release.get("caa_release_mbid"))
                                     or cover_art(release_id, "release-group")})
            except (OSError, ValueError):
                pass
            albums = [release for release in releases if release["detail"] == "Album"]
            other_releases = [release for release in releases if release["detail"] != "Album"]
            return {"tracks": tracks, "albums": albums[:POPULAR_RELEASE_LIMIT],
                    "releases": other_releases[:POPULAR_RELEASE_LIMIT]}
        finally:
            self.highlight_slots.release()

    @staticmethod
    def popular_tracks(payload, records, status, cover):
        by_id = {}
        by_title = {}
        for record in records:
            if reference := recording_reference(record):
                if reference not in by_id or status(record["index"]) == "verified":
                    by_id[reference] = record
            else:
                key = (credit_key(record["artist"]), credit_key(record["title"]), credit_key(record.get("album")))
                by_title.setdefault(key, []).append(record)

        tracks = []
        for row, identity, count in popularity_rows(payload, "recording_mbid", POPULAR_TRACK_LIMIT):
            title, artist = clean(row.get("recording_name")), clean(row.get("artist_name"))
            if not title or not artist:
                continue
            album = clean(row.get("release_name"))
            length = row.get("length")
            duration = length / 1000 if type(length) is int and 0 < length <= 86400000 else 0
            match = by_id.get(identity)
            if match is None and duration and album:
                key = (credit_key(artist), credit_key(title), credit_key(album))
                candidates = [record for record in by_title.get(key, ())
                              if not record.get("length") or abs(record["length"] - duration) <= 2]
                if len(candidates) == 1:
                    match = candidates[0]
            track = {"musicbrainz_id": identity, "title": title, "artist": artist, "album": album,
                     "duration": duration, "listens": count, "cover": cover_art(row.get("caa_release_mbid"))}
            if match:
                track.update(track_id=match["index"], ready=status(match["index"]) == "verified",
                             cover=cover(match) or track["cover"])
            tracks.append(track)
        return tracks

    def recording(self, seed):
        identity = recording_reference(seed)
        if identity:
            return self.connections.optional_payload("recording", identity)
        return self.connections.recording_match(seed)

    def recording_context(self, seed):
        try:
            return self.recording(seed) or {}, None
        except (OSError, ValueError) as error:
            return {}, error

    def artist(self, identity, seed):
        if seed:
            recording, recording_error = self.recording_context(seed)
            matching = artist_ids(recording)
            if len(matching) == 1:
                identity = next(iter(matching))
            else:
                artist = self.connections.artist_match(seed["artist"])
                identity = artist["id"] if artist else ""
            if not identity and recording_error:
                raise recording_error
        if not identity:
            return {}
        data = self.connections.payload("artist", identity)
        graph = self.connections.graph("artist", data)
        releases = [
            {"id": item["id"], "title": clean(item.get("title")),
             "detail": catalog_release_type(item),
             "year": clean(item.get("first-release-date"), 4),
             "cover": cover_art(item["id"], "release-group")}
            for item in items(data.get("release-groups"))
            if valid_id(item.get("id")) and clean(item.get("title")) and identity in artist_ids(item)
        ]
        area = data.get("begin-area") or data.get("area")
        return {"title": graph["root"]["title"], "identity": identity, "detail": graph["root"]["detail"],
                "description": clean(data.get("disambiguation")), "area": clean(area.get("name")) if isinstance(area, dict) else "",
                "releases": sorted(releases, key=lambda item: (item["year"], item["title"]), reverse=True),
                "connections": [{"id": edge["node"]["id"], "title": edge["node"]["title"], "detail": edge["label"]}
                                for edge in graph["edges"] if edge["node"]["kind"] == "artist"]}

    def album(self, identity, seed):
        recording = {}
        if seed:
            if not seed.get("album"):
                return {}
            recording, recording_error = self.recording_context(seed)
            identity = self.connections.album_match(seed["album"], seed["artist"], recording)
            if not identity and recording_error:
                raise recording_error
        if not identity:
            return {}
        group = self.connections.payload("release-group", identity)
        credited = credited_names(group)
        artists = artist_ids(group)
        overview = {"identity": identity, "title": clean(group.get("title")), "artist": credited,
                    "artist_id": next(iter(artists)) if len(artists) == 1 else "",
                    "detail": clean(group.get("primary-type")), "year": clean(group.get("first-release-date"), 4)}
        known = {edition["id"] for edition in items((recording or {}).get("releases")) if valid_id(edition.get("id"))}
        seed_album_key = credit_key((seed or {}).get("album"))
        editions = sorted((edition for edition in items(group.get("releases")) if valid_id(edition.get("id"))),
                          key=lambda entry: (entry["id"] not in known if known else False,
                                             credit_key(entry.get("title")) != seed_album_key if seed_album_key else False,
                                             entry.get("status") != "Official",
                                             clean(entry.get("date")) or "9999", entry["id"]))
        for edition in editions[:3]:
            release_id = edition["id"]
            try:
                release = self.connections.metadata.cached("music-profile:edition:" + release_id, 30 * 86400,
                    lambda: self.connections.metadata.musicbrainz(f"release/{release_id}",
                        {"fmt": "json", "inc": "recordings+artist-credits+release-groups"}))
            except HTTPError as error:
                if error.code != 404:
                    raise
                continue
            if not isinstance(release, dict) or release.get("id") != release_id:
                continue
            tracks = list(release_tracks(release))
            if tracks:
                return {**overview, "edition": release_id, "edition_title": clean(release.get("title")),
                        "seed_recording": (recording or {}).get("id"), "tracks": tracks[:TRACK_LIMIT],
                        "truncated": len(tracks) > TRACK_LIMIT}
        return overview

    @staticmethod
    def album_library(public, records, artist, title, status, seed):
        artist_key = credit_key(artist)
        labels = {credit_key(value) for value in (title, public.get("edition_title"),
                                                  (seed or {}).get("album")) if credit_key(value)}
        local = [record for record in records if credit_key(record.get("album")) in labels]
        by_id = {}
        by_title = {}
        for record in records:
            if identity := recording_reference(record):
                by_id.setdefault(identity, record)
        for record in local:
            by_title.setdefault((credit_key(record["artist"]), credit_key(record["title"])), []).append(record)
        tracks = []
        matched_refs = set()
        for entry in public.get("tracks", ()):
            match = by_id.get(entry["musicbrainz_id"])
            if (match is None and seed and seed.get("index") and not recording_reference(seed)
                    and entry["musicbrainz_id"] == public.get("seed_recording")):
                match = seed
            if match is None:
                candidates = [record for record in by_title.get((credit_key(entry["artist"]), credit_key(entry["title"])), ())
                              if not recording_reference(record) and (not entry["duration"] or not record.get("length")
                                  or abs(record["length"] - entry["duration"]) <= 2)]
                match = candidates[0] if len(candidates) == 1 else None
            if match:
                matched_refs.add(match["index"])
                entry = {**entry, "track_id": match["index"], "ready": status(match["index"]) == "verified"}
            tracks.append(entry)
        credit_keys = {artist_key, credit_key((seed or {}).get("artist"))} - {""}
        extra = [library_track(record, status) for record in local
                 if record["index"] not in matched_refs and credit_key(record["artist"]) in credit_keys]
        if tracks:
            extra = [{**track, "library_only": True} for track in extra if track["ready"]]
        return (tracks + extra)[:TRACK_LIMIT]
