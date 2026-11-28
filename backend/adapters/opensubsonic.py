"""Read-only OpenSubsonic projection of the room's shared verified music."""

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import random
import re
import xml.etree.ElementTree as ET

from system.normalize import normalize_title


ROUTE = re.compile(r"/rest/([A-Za-z][A-Za-z0-9]*)(?:\.view)?")
NAMESPACE = "http://subsonic.org/restapi"
IDENTITY = {"version": "1.16.1", "type": "listening-room", "serverVersion": "1.2.0",
            "openSubsonic": True}
METADATA_METHODS = {"getMusicFolders", "getArtists", "getArtist", "getAlbum", "getSong",
                    "search3", "getAlbumList2", "getPlaylists", "getPlaylist"}
_INVALID_XML = re.compile(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


class SubsonicError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def argument(query, key, default=None):
    values = query.get(key, [])
    if len(values) > 1 or (not values and default is None):
        raise SubsonicError(10, f"Supply {key} once.")
    return values[0] if values else default


def page(rows, query, count, offset, default=50):
    values = [argument(query, count, str(default)), argument(query, offset, "0")]
    if any(not re.fullmatch(r"[0-9]{1,9}", value) for value in values):
        raise SubsonicError(10, "Invalid pagination.")
    size, first = map(int, values)
    return rows[first:first + min(size, 500)]


def _id(kind, *parts):
    return kind + "-" + hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:32]


def _iso_date(stamp):
    return datetime.fromtimestamp(stamp or 0, timezone.utc).isoformat(timespec="seconds")


def _append(parent, name, value):
    if isinstance(value, list):
        for item in value:
            child = ET.SubElement(parent, name)
            if isinstance(item, dict):
                _xml_fields(child, item)
            else:
                child.text = str(item)
    elif isinstance(value, dict):
        _xml_fields(ET.SubElement(parent, name), value)
    else:
        parent.set(name, str(value).lower() if isinstance(value, bool) else _INVALID_XML.sub("", str(value)))


def _xml_fields(element, payload):
    for name, value in payload.items():
        _append(element, name, value)


def response(payload, json_format):
    result = {**IDENTITY, "status": "failed" if "error" in payload else "ok", **payload}
    if json_format:
        return "application/json", json.dumps({"subsonic-response": result}, ensure_ascii=False).encode()
    root = ET.Element("subsonic-response", xmlns=NAMESPACE)
    _xml_fields(root, result)
    return "text/xml; charset=utf-8", ET.tostring(root, encoding="utf-8", xml_declaration=True)


class Library:
    def __init__(self, catalog, shared_playlist):
        self.songs = {}
        self.albums = {}
        self.artists = {}
        self.album_songs = defaultdict(list)
        self.artist_albums = defaultdict(list)
        for index in range(1, len(catalog.records) + 1):
            record = catalog.active_record_at(index)
            if record is None or catalog.status_of(index) != "verified":
                continue
            artist = record["artist"]
            album = record["album"] or "Unknown album"
            artist_id = _id("artist", normalize_title(artist))
            album_id = _id("album", artist_id, normalize_title(album))
            self.artists.setdefault(artist_id, {"id": artist_id, "name": artist})
            if album_id not in self.albums:
                self.albums[album_id] = {"id": album_id, "name": album, "artist": artist, "artistId": artist_id}
                self.artist_albums[artist_id].append(album_id)
            song = {"id": str(index), "title": record["title"], "artist": artist, "album": album,
                    "artistId": artist_id, "albumId": album_id, "parent": album_id,
                    "duration": int(record["length"] or 0), "isDir": False, "type": "music",
                    "created": _iso_date(catalog.coverage.get(index, {}).get("updated_at"))}
            genre = catalog.genre_canonical(record)
            if genre:
                song["genre"] = genre
            self.songs[str(index)] = song
            self.album_songs[album_id].append(song)
        self.playlists = {
            _id("playlist", key): (catalog.list_names.get(key, key),
                                   [self.songs[str(index)] for index in entries if str(index) in self.songs])
            for key, entries in catalog.playlists.items() if shared_playlist(key)
        }

    def _album(self, identifier):
        songs = self.album_songs[identifier]
        return {**self.albums[identifier], "songCount": len(songs),
                "duration": sum(song["duration"] for song in songs),
                "created": min(song["created"] for song in songs)}

    def _artist(self, identifier):
        return {**self.artists[identifier], "albumCount": len(self.artist_albums[identifier])}

    def _playlist(self, identifier):
        name, songs = self.playlists[identifier]
        dates = [song["created"] for song in songs] or [_iso_date(0)]
        return {"id": identifier, "name": name, "public": False, "readonly": True,
                "created": min(dates), "changed": max(dates), "songCount": len(songs),
                "duration": sum(song["duration"] for song in songs)}

    def dispatch(self, method, query):
        if argument(query, "musicFolderId", "1") != "1":
            raise SubsonicError(70, "Music folder not found.")
        if method == "getMusicFolders":
            return {"musicFolders": {"musicFolder": [{"id": 1, "name": "Listening"}]}}
        if method == "getArtists":
            groups = defaultdict(list)
            for identifier in sorted(self.artists, key=lambda item: self.artists[item]["name"].casefold()):
                artist = self._artist(identifier)
                initial = artist["name"][:1].upper()
                groups[initial if initial.isalpha() else "#"].append(artist)
            return {"artists": {"ignoredArticles": "",
                                "index": [{"name": initial, "artist": artists} for initial, artists in groups.items()]}}
        if method == "getPlaylists":
            return {"playlists": {"playlist": [self._playlist(key) for key in self.playlists]}}
        if method == "getAlbumList2":
            order = argument(query, "type")
            if order not in {"alphabeticalByName", "alphabeticalByArtist", "newest", "random", "byGenre"}:
                raise SubsonicError(0, "This album sort is unavailable.")
            albums = list(self.albums)
            if order.startswith("alphabetical"):
                field = "artist" if order.endswith("Artist") else "name"
                albums.sort(key=lambda key: self.albums[key][field].casefold())
            elif order == "byGenre":
                genre = argument(query, "genre").casefold()
                albums = [key for key in albums if any(
                    song.get("genre", "").casefold() == genre for song in self.album_songs[key])]
            elif order == "newest":
                albums.sort(key=lambda key: self._album(key)["created"], reverse=True)
            else:
                random.shuffle(albums)
            return {"albumList2": {"album": [self._album(key) for key in page(albums, query, "size", "offset", 10)]}}
        if method == "search3":
            needle = normalize_title(argument(query, "query"))
            groups = {}
            for kind, rows, fields, build in (
                ("artist", self.artists, ("name",), self._artist),
                ("album", self.albums, ("artist", "name"), self._album),
                ("song", self.songs, ("title", "artist", "album"), self.songs.__getitem__),
            ):
                matches = [key for key, row in rows.items() if needle in normalize_title(" ".join(row[field] for field in fields))]
                groups[kind] = [build(key) for key in page(matches, query, kind + "Count", kind + "Offset", 20)]
            return {"searchResult3": groups}
        identifier = argument(query, "id")
        if method == "getArtist" and identifier in self.artists:
            return {"artist": {**self._artist(identifier),
                               "album": [self._album(key) for key in self.artist_albums[identifier]]}}
        if method == "getAlbum" and identifier in self.albums:
            return {"album": {**self._album(identifier), "song": self.album_songs[identifier]}}
        if method == "getSong" and identifier in self.songs:
            return {"song": self.songs[identifier]}
        if method == "getPlaylist" and identifier in self.playlists:
            return {"playlist": {**self._playlist(identifier), "entry": self.playlists[identifier][1]}}
        raise SubsonicError(70, "Item not found.")
