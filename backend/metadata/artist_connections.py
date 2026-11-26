"""MusicBrainz credits and documented relationships for the artist graph."""

import re
import threading
import urllib.error

from system.normalize import credit_key


MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CONNECTION_PATH = re.compile(r"/api/connections/(?:(song|album)/([1-9][0-9]{0,9})|(recording|artist|release-group)/("
                             + MBID.pattern + r"))")
INCLUDES = {
    "artist": "artist-rels+release-groups+artist-credits",
    "recording": "artist-credits+artist-rels+releases+release-groups+isrcs",
    "release-group": "artist-credits+releases",
    "release": "artist-rels",
}
ARTIST_LINKS = {
    "member of band": ("Member of", "Band member"),
    "collaboration": ("Collaboration project", "Collaborator"),
    "is person": ("Performs as", "Person behind the name"),
    "artist rename": ("Continued as", "Previously known as"),
    "subgroup": ("Parent group", "Subgroup"),
}
CREDIT_ROLES = {
    "producer": "Producer", "co-producer": "Co-producer", "remixer": "Remixer",
    "instrument": "Instrumental credit", "vocal": "Vocal credit",
    "performer": "Performer", "composer": "Composer", "lyricist": "Lyricist",
    "arranger": "Arranger",
}


def items(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def clean(value, limit=180):
    return value.strip()[:limit] if isinstance(value, str) else ""


def valid_id(value):
    return isinstance(value, str) and MBID.fullmatch(value) is not None


def credits(payload):
    return [part for part in items(payload.get("artist-credit")) if isinstance(part.get("artist"), dict)]


def credited_names(payload):
    return "".join(clean(part.get("name") or part["artist"].get("name")) +
                   (part["joinphrase"] if isinstance(part.get("joinphrase"), str) else "")
                   for part in credits(payload)).strip()


def person_kind(payload):
    return {"Person": "Person", "Group": "Group", "Orchestra": "Orchestra",
            "Choir": "Choir", "Character": "Character"}.get(clean(payload.get("type")), "Artist")


def node(kind, payload, name=None):
    identity = payload.get("id")
    label = clean(name or payload.get("name" if kind == "artist" else "title"))
    if not label or not valid_id(identity):
        return None
    if kind == "artist":
        detail = person_kind(payload)
    elif kind == "release-group":
        detail = clean(payload.get("primary-type")) or "Release"
    else:
        detail = "Recording"
    return {"kind": kind, "id": identity, "title": label, "detail": detail}


def relation_note(relation):
    attributes = relation.get("attributes")
    terms = [clean(item, 45) for item in attributes[:3] if clean(item, 45)] if isinstance(attributes, list) else []
    begin, end = clean(relation.get("begin"), 10), clean(relation.get("end"), 10)
    dates = "–".join((begin, end)) if begin and end else f"From {begin}" if begin else f"Until {end}" if end else ""
    if begin and not end and relation.get("ended") is True:
        dates = f"Started {begin}"
    return " · ".join(part for part in (", ".join(terms), dates) if part)


def quote_search(value):
    return '"' + clean(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def same_credit(payload, artist):
    expected = credit_key(artist)
    names = [credited_names(payload)]
    for part in credits(payload):
        names.extend((part.get("name"), part["artist"].get("name")))
    return bool(expected and any(credit_key(name) == expected for name in names))


def artist_ids(payload):
    return {part["artist"]["id"] for part in credits(payload) if valid_id(part["artist"].get("id"))}


class ConnectionsBusy(RuntimeError):
    pass


class ArtistConnections:
    def __init__(self, metadata):
        self.metadata = metadata
        self._slots = threading.BoundedSemaphore(4)

    def lookup(self, kind, identity, record=None):
        if not self._slots.acquire(blocking=False):
            raise ConnectionsBusy
        try:
            if record is not None:
                return self.local(kind, record)
            return self.public_graph(kind, identity)
        finally:
            self._slots.release()

    def payload(self, kind, identity):
        if kind not in INCLUDES or not valid_id(identity):
            raise ValueError("Unknown music credit")
        args = {"fmt": "json", "inc": INCLUDES[kind]}
        if kind == "artist":
            args["type"] = "album|ep"
        data = self.metadata.cached(f"artist-graph:{kind}:{identity}", 30 * 86400,
                                    lambda: self.metadata.musicbrainz(f"{kind}/{identity}", args))
        if not isinstance(data, dict) or data.get("id") != identity:
            raise ValueError("MusicBrainz returned an unexpected credit")
        return data

    def optional_payload(self, kind, identity):
        try:
            return self.payload(kind, identity)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            return {}

    def public_graph(self, kind, identity, recording=None):
        payload = self.payload(kind, identity)
        if kind == "release-group":
            payload = {**payload, "relations": self.edition_credits(payload, recording or {})}
        return self.graph(kind, payload)

    def edition_credits(self, group, recording):
        exact = {edition["id"]: edition for edition in items(recording.get("releases"))
                 if valid_id(edition.get("id")) and isinstance(edition.get("release-group"), dict)
                 and edition["release-group"].get("id") == group["id"]}
        candidates = {edition["id"]: edition for edition in items(group.get("releases"))
                      if valid_id(edition.get("id"))}
        candidates.update(exact)
        editions = sorted(candidates.values(), key=lambda item: (item.get("status") != "Official",
                          item["id"] not in exact, len(clean(item.get("date"))) != 10,
                          clean(item.get("date")) or "9999"))
        main_artists = artist_ids(group)
        for edition in editions[:4]:
            release = self.optional_payload("release", edition["id"])
            contributors = [relation for relation in items(release.get("relations"))
                            if clean(relation.get("type")) in CREDIT_ROLES and isinstance(relation.get("artist"), dict)]
            if any((contributor := node("artist", relation["artist"])) and contributor["id"] not in main_artists
                   for relation in contributors):
                return contributors
        return []

    def local(self, context, record):
        recording = self.recording_match(record)
        title = record["album"] if context == "album" else record["title"]
        if context == "song" and recording:
            graph = self.graph("recording", recording)
            if graph["edges"]:
                return graph
        if context == "album" and title:
            release = self.album_match(title, record["artist"], recording)
            if release:
                graph = self.public_graph("release-group", release, recording)
                if graph["edges"]:
                    return graph
        artist = self.artist_match(record["artist"])
        if artist:
            return self.public_graph("artist", artist["id"])
        return {"state": "unmatched", "title": title or record["title"], "artist": record["artist"],
                "credit": None}

    def artist_match(self, artist):
        expected = credit_key(artist)
        if not expected:
            return None
        name = quote_search(artist)
        query = f"artist:{name} OR alias:{name}"
        data = self.metadata.cached("artist-graph:name:" + query.casefold(), 86400,
                                    lambda: self.metadata.musicbrainz("artist", {
                                        "query": query, "fmt": "json", "limit": 6,
                                    }))
        if not isinstance(data, dict):
            raise ValueError("MusicBrainz returned an unexpected artist search result")
        candidates = [candidate for candidate in items(data.get("artists"))
                      if credit_key(candidate.get("name")) == expected
                      or any(credit_key(alias.get("name")) == expected for alias in items(candidate.get("aliases")))]
        if len(candidates) != 1 or not isinstance(candidates[0].get("score"), int) or candidates[0]["score"] < 90:
            return None
        return node("artist", candidates[0])

    def recording_match(self, record):
        isrc = clean(record.get("isrc")).upper()
        title_key = credit_key(record["title"])
        if not isrc and (not title_key or not credit_key(record["artist"])):
            return {}
        query = (f"isrc:{quote_search(isrc)}" if isrc else
                 f'recording:{quote_search(record["title"])} AND artist:{quote_search(record["artist"])}')
        data = self.metadata.cached("artist-graph:recording-search:" + query.casefold(), 86400,
                                    lambda: self.metadata.musicbrainz("recording", {
                                        "query": query, "fmt": "json", "limit": 8,
                                    }))
        if not isinstance(data, dict):
            raise ValueError("MusicBrainz returned an unexpected recording search result")
        candidates = {item["id"]: item for item in items(data.get("recordings")) if valid_id(item.get("id"))
                      and (isrc or self.matches_recording(record, item))}
        matches = list(candidates.values())
        album = credit_key(record.get("album"))
        if album:
            on_album = [item for item in matches if any(
                self.matches_album_title(release, album) for release in items(item.get("releases")))]
            matches = on_album or matches
        length = record.get("length")
        if isinstance(length, (int, float)) and length > 0 and (len(matches) > 1 or not isrc):
            measured = [(item, abs(item["length"] / 1000 - length)) for item in matches
                        if isinstance(item.get("length"), (int, float)) and item["length"] > 0]
            rounded_match = [item for item, difference in measured if difference <= 1]
            close_match = [item for item, difference in measured if difference <= 5.5]
            matches = rounded_match or close_match or [item for item in matches if not item.get("length")]
        if len(matches) != 1:
            return {}
        recording = self.optional_payload("recording", matches[0]["id"])
        return recording if self.matches_recording(record, recording) else {}

    @staticmethod
    def matches_recording(record, payload):
        isrc = clean(record.get("isrc")).upper()
        if isrc:
            codes = payload.get("isrcs")
            return isinstance(codes, list) and isrc in codes
        title = credit_key(record["title"])
        return bool(title and title == credit_key(payload.get("title")) and same_credit(payload, record["artist"]))

    @staticmethod
    def matches_album_title(release, album_key):
        group = release.get("release-group")
        return (credit_key(release.get("title")) == album_key or
                isinstance(group, dict) and credit_key(group.get("title")) == album_key)

    def album_match(self, title, artist, recording):
        expected = credit_key(title)
        if not expected:
            return None
        known = set()
        for release in items(recording.get("releases")):
            group = release.get("release-group")
            if (isinstance(group, dict) and valid_id(group.get("id")) and
                    self.matches_album_title(release, expected)):
                known.add(group["id"])
        if len(known) == 1:
            return next(iter(known))
        query = f"releasegroup:{quote_search(title)} AND artistname:{quote_search(artist)}"
        results = self.metadata.cached("artist-graph:album:" + query.casefold(), 86400,
                                       lambda: self.metadata.musicbrainz("release-group",
                                           {"query": query, "limit": 8, "fmt": "json"}))
        if not isinstance(results, dict):
            raise ValueError("MusicBrainz returned an unexpected release group search result")
        artist_keys = artist_ids(recording)
        candidates = {candidate["id"] for candidate in items(results.get("release-groups"))
                      if valid_id(candidate.get("id")) and credit_key(candidate.get("title")) == expected
                      and (bool(artist_keys & artist_ids(candidate)) if artist_keys else same_credit(candidate, artist))}
        return next(iter(candidates)) if len(candidates) == 1 else None

    def graph(self, kind, payload):
        root = node(kind, payload)
        if root is None:
            raise ValueError("MusicBrainz did not identify this credit")
        edges = []
        seen = {root["id"]}

        def add(candidate, label, note=""):
            if candidate and candidate["id"] not in seen:
                seen.add(candidate["id"])
                edges.append({"node": candidate, "label": label, "note": note})

        if kind != "artist":
            for part in credits(payload)[:5]:
                add(node("artist", part["artist"], part.get("name")), "Credited artist")
            if kind == "recording":
                releases = sorted(items(payload.get("releases")), key=lambda entry: clean(entry.get("date")) or "9999")
                for release in releases:
                    group = release.get("release-group")
                    if isinstance(group, dict):
                        add(node("release-group", group), "Appears on")
                    if len(edges) >= 7:
                        break
            for relation in items(payload.get("relations")):
                label = CREDIT_ROLES.get(clean(relation.get("type")))
                artist = relation.get("artist")
                if label and isinstance(artist, dict) and len(edges) < 9:
                    add(node("artist", artist), label, relation_note(relation))
        else:
            relations = sorted(items(payload.get("relations")), key=lambda relation:
                               (clean(relation.get("type")) != "member of band",
                                clean(relation.get("begin")) or "9999"))
            for relation in relations:
                labels = ARTIST_LINKS.get(clean(relation.get("type")))
                artist = relation.get("artist")
                if not labels or not isinstance(artist, dict):
                    continue
                label = labels[relation.get("direction") == "backward"]
                if label == "Band member" and relation.get("ended") is True:
                    label = "Former band member"
                if label == "Member of" and relation.get("ended") is True:
                    label = "Former member of"
                add(node("artist", artist), label, relation_note(relation))
                if len(edges) >= 6:
                    break
            releases = sorted(items(payload.get("release-groups")),
                              key=lambda release: clean(release.get("first-release-date")), reverse=True)
            projects = []
            for release in releases:
                credit = artist_ids(release)
                release_type = clean(release.get("primary-type"))
                secondary = release.get("secondary-types") or []
                if root["id"] not in credit or release_type not in {"Album", "EP"} or any(
                        item != "Soundtrack" for item in secondary):
                    continue
                label = "Joint release" if len(credit) > 1 else "Solo release" if root["detail"] == "Person" else release_type
                candidate = node("release-group", release)
                if candidate:
                    projects.append((candidate, label, clean(release.get("first-release-date"), 10)))
            featured = [next((project for project in projects if project[1] == label), None)
                        for label in ("Solo release", "Joint release")]
            for project in [item for item in featured if item] + projects:
                add(*project)
                if len(edges) >= 9:
                    break

        return {"state": "ready", "root": root, "edges": edges, "source": "MusicBrainz"}
