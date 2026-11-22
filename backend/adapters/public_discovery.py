"""Search public audio catalogs without sending room data to providers."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import re
import threading
from urllib.parse import quote, urlencode, urlsplit

from adapters import internal_catalogs, public_http
from library.playlist_edits import metadata
from metadata.external_metadata import search_cache_key
from system import state_database


MEDIA = ("music",)
KINDS = ("archive", "peertube", "funkwhale")
PAGE_SIZE = 12
MAX_PAGE = 10
MAX_SOURCES = 8
MAX_QUERY = 160
_ARCHIVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_SOURCE_ID = re.compile(r"[a-f0-9]{24}")
_SEARCH_SLOTS = threading.BoundedSemaphore(2)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS discovery_sources (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
    media TEXT NOT NULL, address TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    kind: str
    media: tuple[str, ...]
    address: str
    custom: bool = True

    def view(self, *, addresses=False):
        return {"id": self.id, "name": self.name, "kind": self.kind, "media": self.media,
                "address": self.address if addresses else "", "custom": self.custom,
                "scope": "room" if self.kind in ROOM_PROVIDERS else "public"}


DEFAULTS = (
    Source("musicbrainz", "MusicBrainz", "musicbrainz", MEDIA, "", False),
    Source("live-music", "Live Music Archive", "archive", ("music",), "etree", False),
    Source("sepia", "Sepia Search", "peertube", MEDIA, "https://sepiasearch.org", False),
    Source("apple-music", "Apple Music", "apple", ("music",), "", False),
)
ROOM_PROVIDERS = {key for key, _, _ in internal_catalogs.PROVIDERS}


def _text(value, limit=220):
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())[:limit]
    return text.encode("utf-8", errors="replace").decode("utf-8")


def new_source(body):
    kind, media, address = (body.get(key) for key in ("kind", "media", "address"))
    if kind not in KINDS or media not in MEDIA or not isinstance(address, str):
        raise ValueError("Choose a source type, media type, and address.")
    raw_name = body.get("name")
    name = _text(raw_name, 81)
    if not name or len(name) > 80 or not name.isprintable():
        raise ValueError("Enter a source name up to 80 characters.")
    address = address.strip()
    if kind == "archive":
        if address.startswith("https://archive.org/details/"):
            address = address.removeprefix("https://archive.org/details/").removesuffix("/")
        if not _ARCHIVE_ID.fullmatch(address):
            raise ValueError("Enter an Internet Archive collection ID or its archive.org/details link.")
    else:
        address = public_http.normalize_url(address)
        url = urlsplit(address)
        if url.scheme != "https":
            raise ValueError("Use a public HTTPS address.")
        if url.path != "/" or url.query:
            raise ValueError("Enter the server address without a path or query.")
        address = address.removesuffix("/")
    for source in DEFAULTS:
        if source.kind == kind and source.address == address and media in source.media:
            raise ValueError("This source is already available in Browse.")
    identity = hashlib.sha256(f"{kind}\0{media}\0{address}".encode()).hexdigest()[:24]
    return Source(identity, name, kind, (media,), address)


class DiscoverySources:
    def __init__(self, database):
        self.database = database

    def list(self):
        with closing(state_database.connect(self.database, _SCHEMA)) as db:
            custom = tuple(Source(key, name, kind, (media,), address)
                           for key, name, kind, media, address in db.execute(
                               "SELECT id,name,kind,media,address FROM discovery_sources ORDER BY name COLLATE NOCASE,id")
                           if media in MEDIA and kind in KINDS)
        room = tuple(Source(key, name, key, media, "", False)
                     for key, name, media in internal_catalogs.PROVIDERS
                     if internal_catalogs.connection(key))
        return room + DEFAULTS + custom

    def change(self, body):
        operation = body.get("op")
        if operation == "add":
            source = new_source(body)
            with closing(state_database.connect(self.database, _SCHEMA)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT kind,media FROM discovery_sources WHERE id!=?", (source.id,))
                count = sum(kind in KINDS and media in MEDIA for kind, media in existing)
                if count >= MAX_SOURCES:
                    raise ValueError(f"You can add up to {MAX_SOURCES} sources.")
                db.execute("""INSERT INTO discovery_sources VALUES (?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name""",
                           (source.id, source.name, source.kind, source.media[0], source.address))
        elif operation == "remove":
            identity = body.get("id")
            if not isinstance(identity, str) or not _SOURCE_ID.fullmatch(identity):
                raise ValueError("Choose a source you added.")
            with closing(state_database.connect(self.database, _SCHEMA)) as db, db:
                db.execute("DELETE FROM discovery_sources WHERE id=?", (identity,))
        else:
            raise ValueError("Choose Add source or Remove source.")


def _json(url):
    fetched = public_http.fetch(url, accept="application/json", limit=1024 ** 2, timeout=7)
    value = json.loads(fetched.body)
    if not isinstance(value, dict):
        raise ValueError("Unexpected catalog response.")
    return value


def _records(value):
    if not isinstance(value, list):
        raise ValueError("Unexpected catalog response.")
    return (row for row in value[:PAGE_SIZE] if isinstance(row, dict))


def _link(value):
    if not isinstance(value, str):
        return ""
    try:
        return public_http.normalize_url(value)
    except ValueError:
        return ""


def _item(title, creator, url, *, detail="", license="", internal=False, song=False, album=""):
    name, address = _text(title), url if internal else _link(url)
    if not name or not address:
        return None
    return {"title": name, "creator": _text(creator, 150), "url": address,
            "detail": _text(detail, 150), "license": _text(license, 150),
            **({"song": True, "album": _text(album, 150)} if song else {})}


def _recording(title, artist, album, url, detail, duration, **identity):
    try:
        fields = metadata({"title": title, "artist": artist, "album": album or ""})
    except ValueError:
        return None
    address = _link(url)
    if not address:
        return None
    return {"title": fields["title"], "creator": fields["artist"], "album": fields["album"],
            "url": address, "detail": _text(detail, 512), "license": "", "song": True,
            "duration_ms": duration if type(duration) is int and duration > 0 else None, **identity}


def _archive(source, query, page):
    media_filter = "(mediatype:audio OR mediatype:etree)"
    scoped = f'collection:"{source.address}" AND {media_filter}'
    for term in query.split()[:12]:
        quoted = json.dumps(term, ensure_ascii=False)
        scoped += " AND (" + " OR ".join(f"{field}:{quoted}" for field in ("title", "creator", "subject")) + ")"
    fields = ("identifier", "title", "creator", "date")
    args = {"q": scoped, "fl[]": fields, "rows": PAGE_SIZE, "page": page + 1, "output": "json"}
    if not query:
        args["sort[]"] = ("downloads desc", "identifier asc")
    body = _json("https://archive.org/advancedsearch.php?" + urlencode(args, doseq=True)).get("response")
    if not isinstance(body, dict):
        raise ValueError("Unexpected Internet Archive response.")
    found = []
    for row in _records(body.get("docs")):
        key = row.get("identifier")
        if not isinstance(key, str) or not _ARCHIVE_ID.fullmatch(key):
            continue
        names = row.get("creator")
        if isinstance(names, list):
            names = ", ".join(_text(name, 80) for name in names[:3] if isinstance(name, str))
        found.append(_item(row.get("title"), names, "https://archive.org/details/" + quote(key),
                           detail=_text(row.get("date"), 4)))
    return found, _has_more(body.get("numFound"), page)


def _peertube(source, query, page):
    args = {"start": page * PAGE_SIZE, "count": PAGE_SIZE, "nsfw": "false", "categoryOneOf[]": 1}
    if query:
        args["search"] = query
    else:
        args["sort"] = "-publishedAt"
    body = _json(source.address + "/api/v1/search/videos?" + urlencode(args, doseq=True))
    found = []
    for row in _records(body.get("data")):
        privacy = row.get("privacy")
        if row.get("nsfw") is True or row.get("isLive") is True or (isinstance(privacy, dict) and privacy.get("id") != 1):
            continue
        account = row.get("account")
        rights = row.get("licence")
        seconds = row.get("duration")
        detail = f"{seconds // 60}:{seconds % 60:02d}" if type(seconds) is int and 0 < seconds < 864000 else ""
        found.append(_item(row.get("name"), account.get("displayName") if isinstance(account, dict) else "",
                           row.get("url"), detail=detail, license=rights.get("label") if isinstance(rights, dict) else ""))
    return found, _has_more(body.get("total"), page)


def _funkwhale(source, query, page):
    args = {"page_size": PAGE_SIZE, "page": page + 1, "playable": "true"}
    if query:
        args["q"] = query
    body = _json(source.address + "/api/v1/tracks/?" + urlencode(args))
    found = []
    for row in _records(body.get("results")):
        key = row.get("id")
        if type(key) is not int or key < 1:
            continue
        artist, album = row.get("artist"), row.get("album")
        credits = row.get("artist_credit")
        if isinstance(credits, list):
            names = []
            for entry in credits[:8]:
                if not isinstance(entry, dict):
                    continue
                credited_artist = entry.get("artist")
                name = _text(entry.get("credit")) or _text(
                    credited_artist.get("name") if isinstance(credited_artist, dict) else "")
                if name:
                    names.append(name)
            creator = ", ".join(names)
        else:
            creator = artist.get("name") if isinstance(artist, dict) else ""
        album_name = album.get("title") if isinstance(album, dict) else ""
        found.append(_item(row.get("title"), creator,
                           f"{source.address}/library/tracks/{key}",
                           detail=album_name, album=album_name, song=True))
    return found, _has_more(body.get("count"), page)


def _apple(query, page):
    if not query:
        return [], False
    start = page * PAGE_SIZE
    if start >= 200:
        return [], False
    args = urlencode({"term": re.sub(r"\s+[-–—]\s+", " ", query), "media": "music", "entity": "song",
                      "limit": min(200, start + PAGE_SIZE + 1)})
    rows = _json("https://itunes.apple.com/search?" + args).get("results")
    if not isinstance(rows, list):
        raise ValueError("Unexpected Apple catalog response.")
    items = []
    for row in _records(rows[start:start + PAGE_SIZE]):
        if type(row.get("trackId")) is int and 0 < row["trackId"] < 10**19:
            item = _recording(row.get("trackName"), row.get("artistName"), row.get("collectionName"),
                              row.get("trackViewUrl"), row.get("collectionName"), row.get("trackTimeMillis"),
                              apple_id=str(row["trackId"]))
        else:
            item = _item(row.get("trackName"), row.get("artistName"), row.get("trackViewUrl"),
                         detail=row.get("collectionName"))
        items.append(item)
    return items, len(rows) > start + PAGE_SIZE


def _has_more(total, page):
    return type(total) is int and total > (page + 1) * PAGE_SIZE and page + 1 < MAX_PAGE


def _search_one(source, query, page, recordings, cached):
    try:
        if source.kind == "musicbrainz":
            rows, total = recordings(query, page, PAGE_SIZE)
            items = []
            for row in rows:
                detail = " · ".join(value for value in (row["album"], row["version"]) if isinstance(value, str) and value)
                item = _recording(row["title"], row["creator"], row["album"],
                                  "https://musicbrainz.org/recording/" + row["musicbrainz_id"], detail,
                                  row["duration_ms"], musicbrainz_id=row["musicbrainz_id"])
                if item:
                    items.append(item)
            more = (page + 1) * PAGE_SIZE < min(100000, total)
        elif source.kind in ROOM_PROVIDERS:
            rows, total = internal_catalogs.search(source.kind, query, page, PAGE_SIZE, cached=cached)
            items = [_item(title, creator, url, detail=album, internal=True, album=album, song=song)
                     for title, creator, url, album, song in rows[:PAGE_SIZE]]
            more = _has_more(total, page)
        else:
            def fetch():
                if source.kind == "apple":
                    return _apple(query, page)
                adapter = {"archive": _archive, "peertube": _peertube, "funkwhale": _funkwhale}[source.kind]
                return adapter(source, query, page)
            key = search_cache_key(source.kind, source.id, source.address, query, page, PAGE_SIZE)
            items, more = cached(key, 86400, fetch) if cached else fetch()
    except (OSError, ValueError, TypeError, RecursionError):
        return {"source": source.id, "name": source.name, "items": [], "more": False,
                "error": "This catalog is unavailable. Try it again shortly."}
    unique = {}
    for item in items:
        if item is not None:
            unique.setdefault(item["url"], item)
    return {"source": source.id, "name": source.name, "items": list(unique.values()), "more": more, "error": ""}


class DiscoveryBusy(ValueError):
    pass


def search(sources, body, recordings=None, *, cached=None):
    media, selection, raw_query = (body.get(key, "") for key in ("media", "source", "query"))
    page = body.get("page", 0)
    scope = body.get("scope", "public")
    if media not in MEDIA or not isinstance(selection, str) or not isinstance(raw_query, str):
        raise ValueError("Choose a media type and a source.")
    if not selection and scope == "public" and any(source.kind == "musicbrainz" for source in sources):
        selection = "recordings"
    song_search = scope == "public" and selection in ("recordings", "musicbrainz", "apple-music", "")
    if len(raw_query) > MAX_QUERY or type(page) is not int or page < 0 or (
            page * PAGE_SIZE >= 100000 if song_search else page >= MAX_PAGE):
        raise ValueError("Choose a valid page and use a search of up to 160 characters.")
    query = _text(raw_query, MAX_QUERY)
    if scope not in ("public", "room"):
        raise ValueError("Choose public or room catalogs.")
    available = [source for source in sources if media in source.media and (not selection or selection == "all-public" and scope == "public" or source.id == selection
                 or selection == "recordings" and source.kind in {"apple", "musicbrainz"})
                 and (source.kind in ROOM_PROVIDERS) == (scope == "room")]
    if not available:
        raise ValueError("Choose an available source for this media type.")
    if not _SEARCH_SLOTS.acquire(blocking=False):
        raise DiscoveryBusy("The room is running other public searches. Try again shortly.")
    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(_search_one, source, query, page, recordings, cached) for source in available]
            groups = [future.result() for future in futures]
            if not song_search and page + 1 == MAX_PAGE:
                for group in groups:
                    group["more"] = False
            return {"groups": groups}
    finally:
        _SEARCH_SLOTS.release()
