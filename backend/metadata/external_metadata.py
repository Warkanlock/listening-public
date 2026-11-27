"""The one home for external metadata: MusicBrainz, ListenBrainz, LRCLIB.

The HTTP server used to carry this subsystem inline (roughly 220 lines:
cached MusicBrainz lookups, the external search behind the UI, ListenBrainz
artist radio, and the weekly Deep Dive assembly, still named Discover
internally), reaching into server globals for the catalog, the workspace
root, and the requests database, which made it the largest untested backend
area (A1 S4, A3 PERF-08).

This module owns it behind a small interface. Callers pass the catalog read
model (and, for Discover, the workspace key) in; the module never reaches
back into server globals. Cache reads and writes go through plan 005's
requests store, handed in as a factory so the server's current database is
resolved on every access, exactly as the server's own helper did. The HTTP
transport and the clocks are injectable so tests drive the module with
fakes and never touch the network.

Behavior is the extraction's contract, unchanged: the same cache keys and
TTLs (search a day, recordings and per-track metadata 30 days, Discover a
week), the MusicBrainz pacing of one call per 1.1 seconds with up to three
attempts on 429/502/503 and a short backoff between them, Discover's
deterministic per-week seed, catalog exclusion, 12-track cap, and the
5-minute failure backoff in discover_status.
"""

from concurrent.futures import Future
import datetime
import hashlib
import json
import os
import random
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode, urlparse

from metadata.artist_connections import credit_key, credits, credited_names, quote_search, same_credit, valid_id
from metadata.discovery_trail import LibraryRecordings
from catalog.catalog import spotify_share_url
from system.normalize import normalize_title

MUSICBRAINZ_HEADERS = {"User-Agent": "ListeningRoom/1.2 (personal local music manager)",
                       "Accept": "application/json"}
LISTENBRAINZ_HEADERS = {"User-Agent": "ListeningRoom/1.2 (personal music library)",
                        "Accept": "application/json"}
LRCLIB_HEADERS = {"User-Agent": "ListeningRoom/1.2 (personal local music manager)",
                  "Accept": "application/json"}
DISCOVER_SOURCE = "ListenBrainz radio + MusicBrainz"
LRCLIB_SOURCE = "LRCLIB"
GENRE_MATCH_PREFIX = "genre-match:v2:"
GENRE_CACHE_AGE = 30 * 86400
GENRE_MISS_AGE = 15 * 60

def genre_cache_age(genre):
    return GENRE_CACHE_AGE if genre else GENRE_MISS_AGE


def _recording_genre_age(payload):
    return genre_cache_age(payload.get("genres"))


_LRC_TAG = re.compile(r"\[\d+:\d+(?:\.\d+)?\]")


def search_cache_key(provider, *parameters):
    """Scope a page to its provider and settings without persisting queries or credentials in the key."""
    encoded = json.dumps(parameters, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "catalog-search:v1:" + provider + ":" + hashlib.sha256(encoded).hexdigest()


def plain_from_synced(synced):
    """The lyric text of an LRC document with its timestamps stripped."""
    return "\n".join(_LRC_TAG.sub("", line).strip() for line in synced.splitlines())


def provenance_label(uri):
    """Where a catalog track came from, from the identifier it carries.

    A `spotify:` identifier means a Spotify-shaped list or the sync
    adapter carried the track in; an http(s) identifier names its host;
    any other scheme names itself. No identifier, no provenance.
    """
    if not uri:
        return ""
    if uri.startswith("spotify:"):
        return "Spotify"
    parsed = urlparse(uri)
    if parsed.scheme in {"http", "https"}:
        return parsed.hostname or ""
    return parsed.scheme or ""


def _urlopen_json(url, headers):
    """The real transport: one GET, the parsed JSON body."""
    request = urllib.request.Request(url)
    for name, value in headers.items():
        if name.casefold() == "authorization":
            request.add_unredirected_header(name, value)
        else:
            request.add_header(name, value)
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.load(response)


def artist_credit(recording):
    return "".join(part if isinstance(part, str) else part.get("name", "")
                   for part in recording.get("artist-credit", []))


class ExternalMetadata:
    """Cached external lookups, external search, and Discover assembly.

    `store_factory` is a zero-argument callable returning the requests
    store (the server passes its own `requests_store` helper, so a
    workspace switch is honored on the next access). `transport` is a
    callable `(url, headers) -> parsed JSON` that raises
    `urllib.error.HTTPError` or `OSError` on failure. `now`, `monotonic`,
    `sleep`, `today`, and `utcnow` are the clocks, injectable for tests.
    """

    def __init__(self, store_factory, transport=None, now=None, monotonic=None,
                 sleep=None, today=None, utcnow=None):
        self._store_factory = store_factory
        self._transport = transport or _urlopen_json
        self._now = now or time.time
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._today = today or datetime.date.today
        self._utcnow = utcnow or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self._musicbrainz_lock = threading.Lock()
        self._last_musicbrainz_call = 0.0
        self._discover_lock = threading.Lock()
        self._discover_building = False
        self._discover_error_until = 0.0
        self._cache_fetch_lock = threading.Lock()
        self._cache_fetches = {}
        self._catalog_lookup_lock = threading.Lock()
        self._catalog_lookup_records = None
        self._catalog_lookup = None
        self._discovery_records = None
        self._discovery_index = None

    # -- cache -------------------------------------------------------------

    def _fresh_cache(self, store, key, max_age):
        try:
            row = store.cache_get(key)
            if row:
                age = self._now() - row["saved_at"]
                result = json.loads(row["body"])
                if age < (max_age(result) if callable(max_age) else max_age):
                    return row["body"], result
        except (sqlite3.Error, json.JSONDecodeError):
            pass

    def cached(self, key, max_age, fetch):
        store = self._store_factory()
        cached = self._fresh_cache(store, key, max_age)
        if cached is not None:
            return cached[1]

        scope = (store.path, key, max_age)
        with self._cache_fetch_lock:
            flight = self._cache_fetches.get(scope)
            leader = flight is None
            if leader:
                flight = self._cache_fetches[scope] = Future()
        if not leader:
            return json.loads(flight.result())

        try:
            # An earlier fetch can finish between the first read and registration.
            cached = self._fresh_cache(store, key, max_age)
            if cached is not None:
                payload, result = cached
            else:
                result = fetch()
                payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                try:
                    store.cache_put(key, payload, self._now())
                except sqlite3.Error:
                    pass
            flight.set_result(payload)
            return result
        except BaseException as error:
            flight.set_exception(error)
            raise
        finally:
            with self._cache_fetch_lock:
                del self._cache_fetches[scope]

    # -- MusicBrainz ---------------------------------------------------------

    def _pace_musicbrainz(self):
        """Reserve this call's MusicBrainz start slot (plan 050 F1).

        MusicBrainz asks clients for one request a second; that is
        a property of when calls START, not of how long they take.
        Until this fix the pacing lock was held across the sleep,
        the request, and every retry backoff, so one slow response
        serialized every other lookup behind it. Now the lock is
        held only long enough to reserve the next slot: the caller
        sleeps until its slot outside the lock and then requests
        without holding it, so calls overlap in flight, bounded by
        the pacing itself. Cache semantics are untouched.
        """
        with self._musicbrainz_lock:
            now = self._monotonic()
            delay = 1.1 - (now - self._last_musicbrainz_call)
            if delay > 0:
                self._last_musicbrainz_call = now + delay
            else:
                delay = 0.0
                self._last_musicbrainz_call = now
        if delay > 0:
            self._sleep(delay)

    def musicbrainz(self, path, params):
        url = f"https://musicbrainz.org/ws/2/{path}?{urlencode(params)}"
        for attempt in range(3):
            self._pace_musicbrainz()
            try:
                return self._transport(url, dict(MUSICBRAINZ_HEADERS))
            except urllib.error.HTTPError as error:
                if error.code not in {429, 502, 503} or attempt == 2:
                    raise
                self._sleep(2 + attempt)

    def _catalog_key_lookup(self, catalog):
        """(normalized artist, normalized title) -> catalog index,
        built once per catalog records list (plan 018 PERF-01 rider).

        A catalog reload replaces the records list and invalidates the map.
        """
        records = catalog.records
        with self._catalog_lookup_lock:
            if self._catalog_lookup is None or self._catalog_lookup_records is not records:
                self._catalog_lookup = {
                    (normalize_title(record["artist"]), normalize_title(record["title"])): record["index"]
                    for record in records if record.get("active", True)}
                self._catalog_lookup_records = records
            return self._catalog_lookup

    def discovery_index(self, catalog):
        records = catalog.records
        with self._catalog_lookup_lock:
            if self._discovery_records is not records:
                self._discovery_index = LibraryRecordings(records)
                self._discovery_records = records
            return self._discovery_index

    def catalog_recordings(self, query, page, size):
        """One paced, cached page of recording metadata. A failed lookup is retryable."""
        if not query:
            return [], 0
        parts = re.split(r"\s+[-–—]\s+", query, maxsplit=1)
        if len(parts) == 2:
            expression = f"artist:{quote_search(parts[0])} AND recording:{quote_search(parts[1])}"
        else:
            phrase = quote_search(query)
            expression = f"artist:{phrase} OR recording:{phrase}"
            words = query.split()
            if len(words) > 1:
                terms = [f"(artist:{quote_search(word)} OR recording:{quote_search(word)})" for word in words]
                expression = f"({expression})^4 OR (" + " AND ".join(terms) + ")"

        def fetch():
            payload = self.musicbrainz("recording", {"query": expression, "fmt": "json",
                                                     "limit": size, "offset": page * size})
            if not isinstance(payload, dict):
                raise ValueError("Unexpected recording catalog response.")
            rows = payload.get("recordings")
            total = payload.get("count")
            if not isinstance(rows, list) or type(total) is not int:
                raise ValueError("Unexpected recording catalog response.")
            items = []
            for row in rows[:size]:
                if not isinstance(row, dict) or not valid_id(row.get("id")):
                    continue
                credits = row.get("artist-credit")
                if not isinstance(credits, list):
                    continue
                names = []
                for part in credits:
                    if isinstance(part, str):
                        names.append(part)
                    elif isinstance(part, dict):
                        person = part.get("artist")
                        name = part.get("name") or (person.get("name") if isinstance(person, dict) else None)
                        if isinstance(name, str):
                            names.append(name)
                            if isinstance(part.get("joinphrase"), str):
                                names.append(part["joinphrase"])
                artist = "".join(names).strip()
                if not row.get("title") or not artist:
                    continue
                releases = row.get("releases") or []
                items.append({"title": row["title"], "creator": artist, "musicbrainz_id": row["id"],
                              "album": releases[0].get("title", "") if isinstance(releases, list) and releases and isinstance(releases[0], dict) else "",
                              "duration_ms": row.get("length"), "version": row.get("disambiguation", "")})
            return items, total

        key = "browse-recordings:" + json.dumps([query.casefold(), page, size])
        return self.cached(key, 86400, fetch)

    def _search_page(self, expression):
        def fetch():
            data = self.musicbrainz("recording", {"query": expression, "fmt": "json", "limit": 10})
            if not isinstance(data, dict) or not isinstance(data.get("recordings"), list):
                raise ValueError("Unexpected recording search response.")
            items = []
            for row in data["recordings"]:
                try:
                    artist = artist_credit(row)
                    title = row.get("title", "")
                    album = (row.get("releases") or [{}])[0].get("title", "")
                except (AttributeError, TypeError, KeyError, IndexError) as error:
                    raise ValueError("Invalid recording in search response.") from error
                if not isinstance(title, str) or not isinstance(album, str):
                    raise ValueError("Invalid recording title or album.")
                if artist and title:
                    items.append({"artist": artist, "title": title, "album": album,
                                  "musicbrainz_id": row.get("id", ""), "score": row.get("score", 0)})
            return items
        return self.cached(search_cache_key("musicbrainz-recording", expression.casefold()), 86400, fetch)

    def external_search(self, query, catalog):
        query = query.strip()[:120]
        if not query:
            return []
        if " - " in query:
            artist, title = query.split(" - ", 1)
            searches = [f'artistname:"{artist}" AND recording:"{title}"']
        else:
            searches = [f'artistname:"{query}"', f'recording:"{query}"']
        seen = set()
        results = []
        for search in searches:
            try:
                page = self._search_page(search)
            except (OSError, ValueError):
                continue
            for item in page:
                key = (item["artist"].casefold(), item["title"].casefold())
                if key in seen:
                    continue
                seen.add(key)
                results.append(item)
                if len(results) >= 8:
                    break
            if len(results) >= 8:
                break
        catalog_lookup = self._catalog_key_lookup(catalog)
        for item in results:
            index = catalog_lookup.get((normalize_title(item["artist"]), normalize_title(item["title"])))
            if index:
                item["catalog_id"] = index
                item["status"] = catalog.track(index)["status"]
        return results

    def recording_details(self, mbid, *, refresh_missing_genres=False):
        if not re.fullmatch(r"[0-9a-f-]{36}", mbid):
            raise ValueError("Invalid MusicBrainz recording")
        max_age = _recording_genre_age if refresh_missing_genres else GENRE_CACHE_AGE
        item = self.cached("recording:" + mbid, max_age,
                           lambda: self.musicbrainz("recording/" + mbid,
                                                    {"inc": "artist-credits+isrcs+releases+genres+recording-rels+work-rels",
                                                     "fmt": "json"}))
        dates = sorted(release.get("date", "") for release in item.get("releases", []) if release.get("date"))
        connections = []
        labels = {"samples material": ("Samples", "Sampled by"),
                  "remix": ("Remix of", "Remixed by"),
                  "mashes up": ("Mash-up of", "Used in mash-up")}
        for relation in item.get("relations", []):
            target = relation.get("recording") or {}
            if relation.get("type") not in labels or not target.get("title"):
                continue
            direction = 0 if relation.get("direction") == "forward" else 1
            connections.append({"relationship": labels[relation["type"]][direction],
                                "title": target["title"], "artist": artist_credit(target),
                                "musicbrainz_id": target.get("id", "")})
        return {"musicbrainz_id": mbid, "recording_title": item.get("title", ""),
                "recording_artist": artist_credit(item), "first_release": dates[0] if dates else "",
                "releases": len(item.get("releases", [])),
                "genres": [genre["name"] for genre in item.get("genres", [])[:5]],
                "connections": connections[:30],
                "musicbrainz_url": f"https://musicbrainz.org/recording/{mbid}"}

    def track_metadata(self, index, catalog):
        record = catalog.record_at(index)
        basic = {"title": record["title"], "artist": record["artist"], "album": record["album"],
                 "duration": record["length"], "isrc": record.get("isrc", ""),
                 "identifier": record.get("uri", ""),
                 "spotify_url": spotify_share_url(record.get("uri", "")),
                 "provenance": provenance_label(record.get("uri", "")),
                 "lists": [catalog.list_names.get(playlist, playlist)
                           for playlist in catalog.memberships.get(index, [])],
                 "source": "Portable music list"}
        query = (f"isrc:{record['isrc']}" if record.get("isrc") else
                 f'recording:"{record["title"]}" AND artist:"{record["artist"]}"')
        def lookup():
            results = self.musicbrainz("recording", {"query": query, "fmt": "json", "limit": 3})
            candidates = results.get("recordings", [])
            if not candidates or int(candidates[0].get("score", 0)) < 75:
                return {}
            return self.recording_details(candidates[0]["id"])
        try:
            extra = self.cached("metadata:" + record["key"], 30 * 86400, lookup)
        except (OSError, urllib.error.HTTPError, ValueError, TimeoutError):
            extra = {}
        return {**basic, **extra, "source": "MusicBrainz + portable list" if extra else "Portable music list"}

    # -- Genres (plan 029) ---------------------------------------------------

    def _match_mbid(self, record):
        """Share an ISRC or confident title/artist match across the genre sources."""
        def candidates(query):
            results = self.musicbrainz("recording", {"query": query, "fmt": "json", "limit": 3})
            rows = results.get("recordings") if isinstance(results, dict) else None
            if not isinstance(rows, list):
                raise ValueError("Unexpected MusicBrainz recording search response")
            return [row for row in rows if isinstance(row, dict) and valid_id(row.get("id"))
                    and int(row.get("score", 0)) >= 75]

        def search():
            if isrc := (record.get("isrc") or "").strip():
                matches = candidates(f"isrc:{quote_search(isrc)}")
                if matches:
                    return matches[0]["id"]
            title, artist = record["title"], record["artist"]
            title_key = credit_key(title)
            if not title_key or not credit_key(artist):
                return ""
            query = f"recording:{quote_search(title)} AND artist:{quote_search(artist)}"
            for row in candidates(query):
                if credit_key(row.get("title")) == title_key and same_credit(row, artist):
                    return row["id"]
            return ""

        return self.cached(GENRE_MATCH_PREFIX + record["key"], genre_cache_age, search)

    def musicbrainz_genre(self, record):
        """The recording's top MusicBrainz genre, or "" when none.

        Shares the recording payload with Song DNA and retries genre-less
        details after the shorter genre miss interval.
        """
        def fetch():
            mbid = self._match_mbid(record)
            if not mbid:
                return ""
            genres = self.recording_details(mbid, refresh_missing_genres=True).get("genres") or []
            return genres[0] if genres else ""
        return self.cached("genre:v2:" + record["key"], genre_cache_age, fetch)

    def listenbrainz_genre(self, record):
        """Prefer the recording, then its release group, then its sole credited artist."""
        mbid = self._match_mbid(record)
        if not mbid:
            return ""

        def top_genre(entries, artist_mbid=None):
            if not isinstance(entries, list):
                raise ValueError("Unexpected ListenBrainz genre list")
            genres = [entry for entry in entries if isinstance(entry, dict)
                      and (artist_mbid is None or entry.get("artist_mbid") == artist_mbid)
                      and valid_id(entry.get("genre_mbid")) and isinstance(entry.get("tag"), str)
                      and entry["tag"].strip() and type(entry.get("count")) is int and entry["count"] > 0]
            return max(genres, key=lambda entry: entry["count"])["tag"].strip() if genres else ""

        def fetch():
            url = "https://api.listenbrainz.org/1/metadata/recording/?" + urlencode({
                "recording_mbids": mbid, "inc": "tag artist"})
            data = self._transport(url, dict(LISTENBRAINZ_HEADERS))
            if not isinstance(data, dict) or not isinstance(data.get(mbid, {}), dict):
                raise ValueError("Unexpected ListenBrainz recording response")
            recording = data.get(mbid, {})
            tags = recording.get("tag", {})
            if not isinstance(tags, dict):
                raise ValueError("Unexpected ListenBrainz tag response")
            for level in ("recording", "release_group"):
                if genre := top_genre(tags.get(level, [])):
                    return genre
            artist = recording.get("artist")
            artists = artist.get("artists", []) if isinstance(artist, dict) else []
            if isinstance(artists, list) and len(artists) == 1 and isinstance(artists[0], dict) \
                    and valid_id(artist_mbid := artists[0].get("artist_mbid")):
                return top_genre(tags.get("artist", []), artist_mbid)
            return ""
        return self.cached("listenbrainz-genre:v3:" + mbid, genre_cache_age, fetch)

    # -- Lyrics (plan 026) ---------------------------------------------------

    @staticmethod
    def _lyrics_not_found():
        return {"found": False, "plain": "", "synced": "", "source": LRCLIB_SOURCE}

    def lyrics(self, record):
        """Lyrics for a catalog record, from LRCLIB, cached for 30 days.

        LRCLIB (lrclib.net) is a free, keyless lyrics database; the
        lookup sends the track name, artist, album, and duration, and
        the client identifies itself in the User-Agent, as LRCLIB asks.
        The duration is part of the match, not a hint: LRCLIB answers
        only when the duration matches its record within about two
        seconds and 404s otherwise, so a different version of the song
        lands in the same not-found state as a song it does not know.
        Found and not-found answers are both cached, so a song with no
        lyrics is not refetched on every dialog open. A malformed
        answer also reads as not-found; a transport failure (offline,
        429, 5xx) propagates instead, so the caller can say unavailable
        rather than no-lyrics, and is never cached.
        """
        def fetch():
            params = {"track_name": record["title"], "artist_name": record["artist"]}
            if record.get("album"):
                params["album_name"] = record["album"]
            try:
                duration = float(record.get("length") or 0)
            except (TypeError, ValueError):
                duration = 0
            if 1 <= duration <= 3600:  # LRCLIB validates this range; omit rather than 400.
                params["duration"] = int(round(duration))
            url = "https://lrclib.net/api/get?" + urlencode(params)
            try:
                data = self._transport(url, dict(LRCLIB_HEADERS))
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    return self._lyrics_not_found()
                raise
            except ValueError:
                return self._lyrics_not_found()
            if not isinstance(data, dict):
                return self._lyrics_not_found()
            plain = data.get("plainLyrics")
            synced = data.get("syncedLyrics")
            plain = plain if isinstance(plain, str) else ""
            synced = synced if isinstance(synced, str) else ""
            if not plain and synced:
                plain = plain_from_synced(synced)
            if not plain and not synced:
                return self._lyrics_not_found()
            return {"found": True, "plain": plain, "synced": synced,
                    "source": LRCLIB_SOURCE}
        return self.cached("lyrics:" + record["key"], 30 * 86400, fetch)

    # -- ListenBrainz radio and Discover (Deep Dive) -------------------------

    def artist_popularity(self, artist_mbid, *, releases=False):
        entity = "release-groups" if releases else "recordings"
        key = f"artist-popularity:{entity}:{artist_mbid}"
        url = f"https://api.listenbrainz.org/1/popularity/top-{entity}-for-artist/{quote(artist_mbid)}"

        def fetch():
            token = os.environ.get("LISTENING_ROOM_LISTENBRAINZ_TOKEN", "").strip()
            if not token:
                raise ValueError("ListenBrainz popularity requires a token")
            headers = {**LISTENBRAINZ_HEADERS, "Authorization": f"Token {token}"}
            data = self._transport(url, headers)
            if not isinstance(data, list):
                raise ValueError("Unexpected ListenBrainz popularity response")
            return data[:100]

        return self.cached(key, 86400, fetch)

    def listenbrainz_radio(self, artist_mbid):
        url = ("https://api.listenbrainz.org/1/lb-radio/artist/" + quote(artist_mbid) + "?" +
               urlencode({"mode": "medium", "max_similar_artists": 4,
                          "max_recordings_per_artist": 3, "pop_begin": 10, "pop_end": 85}))
        return self._transport(url, dict(LISTENBRAINZ_HEADERS))

    def _week(self):
        today = self._today().isocalendar()
        return f"{today.year}-W{today.week:02d}"

    def _discover_key(self, workspace, favorites=None):
        """The week's Discover cache key, fingerprinted by favorites.

        The newest favorite stamp (0 when there are none) rides in
        the key, so a new like reshapes the current week's Deep Dive
        on the next build instead of waiting for the week to roll
        over. discover_weekly and discover_status share this helper
        so they can never disagree about the key.
        """
        stamp = favorites.stamp()[1] if favorites is not None else 0
        return "discover:v3:" + self._week() + ":" + workspace + ":" + str(stamp)

    def _discover_seeds(self, catalog, rng, favorites):
        """The five seed artists for a build (plan 048).

        Favorite artists come first, most recently favorited first;
        the week-seeded shuffle of the whole catalog fills any slots
        left over. With no favorites this is exactly the shuffled
        five the build always used.
        """
        artists = list({record["artist"] for record in catalog.records if record.get("active", True) and record["artist"]})
        rng.shuffle(artists)
        seeds = []
        if favorites is not None:
            by_key = {record["key"]: record for record in catalog.records}
            for key in favorites.recent():
                record = by_key.get(key)
                if (record and record.get("active", True) and record["artist"]
                        and record["artist"] not in seeds):
                    seeds.append(record["artist"])
                if len(seeds) >= 5:
                    break
        for artist in artists:
            if len(seeds) >= 5:
                break
            if artist not in seeds:
                seeds.append(artist)
        return seeds

    def discover_weekly(self, catalog, workspace, favorites=None):
        week = self._week()
        def build():
            seed = int.from_bytes(hashlib.sha256((workspace + week).encode()).digest()[:8], "big")
            rng = random.Random(seed)
            known = self.discovery_index(catalog)
            candidates = []
            seeds = self._discover_seeds(catalog, rng, favorites)
            for seed_rank, seed_artist in enumerate(seeds):
                if len(candidates) >= 24:
                    break
                try:
                    found = self.musicbrainz("artist", {"query": f'artist:"{seed_artist}"', "fmt": "json", "limit": 1})
                    matches = found.get("artists", [])
                    if not matches or int(matches[0].get("score", 0)) < 85:
                        continue
                    radio = self.listenbrainz_radio(matches[0]["id"])
                    for group in radio.values():
                        for item in group:
                            if item.get("recording_mbid"):
                                candidates.append((item["recording_mbid"], seed_artist, seed_rank, matches[0]["id"],
                                                   item.get("similar_artist_name", "")))
                except (OSError, ValueError, urllib.error.HTTPError, TimeoutError):
                    continue
            rng.shuffle(candidates)
            tracks = []
            seen = set()
            for mbid, seed_artist, seed_rank, seed_id, suggested_artist in candidates[:24]:
                if len(tracks) >= 12:
                    break
                try:
                    item = self.cached("discover-recording:" + mbid, 30 * 86400,
                                       lambda mbid=mbid: self.musicbrainz("recording/" + mbid,
                                                                          {"inc": "artist-credits+releases+genres", "fmt": "json"}))
                except (OSError, ValueError, urllib.error.HTTPError, TimeoutError):
                    continue
                title, artist = item.get("title", ""), credited_names(item) or artist_credit(item) or suggested_artist
                if not title or not artist or mbid in seen:
                    continue
                seen.add(mbid)
                release = (item.get("releases") or [{}])[0]
                release_id = release.get("id", "")
                cover = (f"https://coverartarchive.org/release/{release_id}/front-500"
                         if isinstance(release_id, str) and re.fullmatch(
                             r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", release_id) else "")
                track = {"musicbrainz_id": mbid, "title": title, "artist": artist,
                               "album": release.get("title", ""), "cover": cover, "seed_artist": seed_artist,
                               "duration": round((item.get("length") or 0) / 1000),
                               "reason": f"From artists near {seed_artist}",
                               "genres": [genre["name"] for genre in item.get("genres", [])[:3]],
                               "art_seed": hashlib.sha256((mbid + week).encode()).hexdigest()[:12]}
                if valid_id(seed_id):
                    track.update(seed_id=seed_id, seed_rank=seed_rank)
                if credits(item):
                    track["artist_id"] = credits(item)[0]["artist"].get("id")
                if item.get("disambiguation"):
                    track["version"] = item["disambiguation"]
                if not known.contains(track):
                    tracks.append(track)
            return {"week": week, "tracks": tracks, "source": DISCOVER_SOURCE,
                    "generated_at": self._utcnow().isoformat()}
        return self.cached(self._discover_key(workspace, favorites), 7 * 86400, build)

    def discover_status(self, catalog, workspace, favorites=None):
        week = self._week()
        key = self._discover_key(workspace, favorites)
        row = self._store_factory().cache_get(key)
        if row:
            return json.loads(row["body"])
        if self._now() < self._discover_error_until:
            return {"week": week, "status": "error", "tracks": [],
                    "source": DISCOVER_SOURCE}
        with self._discover_lock:
            if not self._discover_building:
                self._discover_building = True
                def build():
                    try:
                        self.discover_weekly(catalog, workspace, favorites)
                    except (OSError, ValueError, TypeError, urllib.error.HTTPError, TimeoutError):
                        self._discover_error_until = self._now() + 300
                    finally:
                        with self._discover_lock:
                            self._discover_building = False
                threading.Thread(target=build, daemon=True, name="discover-weekly").start()
        return {"week": week, "status": "building", "tracks": [],
                "source": DISCOVER_SOURCE}
