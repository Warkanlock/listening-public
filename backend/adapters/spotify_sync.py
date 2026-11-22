"""Spotify OAuth, explicit playlist edits and scheduled mirroring for a local library."""

import base64
import csv
import hashlib
import hmac
import io
import json
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from adapters import spotify_playlists as playlists_api
from library.list_intake import preserve_legacy_copy
from library.library_protocol import canonicalize_source, derived_path, retire_derived
from system.private_files import private_write


SCOPES = "user-library-read playlist-read-private playlist-read-collaborative user-read-private " \
         "user-library-modify playlist-modify-public playlist-modify-private"
CSV_FIELDS = ("Track URI", "Track Name", "Artist Name(s)", "Album Name",
              "Track Duration (ms)", "ISRC", "Album Image URL", "Added At")
SYNC_INTERVAL = 15 * 60
FULL_SAVED_INTERVAL = 24 * 60 * 60


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def track_row(item):
    if not isinstance(item, dict):
        return None
    track = item.get("track") or item.get("item") or {}
    if not isinstance(track, dict):
        return None
    if track.get("type") != "track" or not track.get("name") or not track.get("artists"):
        return None
    uri = track.get("uri") or ""
    if not re.fullmatch(r"spotify:track:[A-Za-z0-9]+", uri):
        return None
    album = track.get("album") or {}
    images = album.get("images") or []
    cover = next((image.get("url", "") for image in images
                  if isinstance(image, dict) and str(image.get("url", "")).startswith("https://")), "")
    return {"Track URI": uri, "Track Name": track["name"],
            "Artist Name(s)": ", ".join(artist.get("name", "") for artist in track["artists"]
                                          if isinstance(artist, dict) and artist.get("name")),
            "Album Name": album.get("name", ""), "Track Duration (ms)": track.get("duration_ms") or "",
            "ISRC": (track.get("external_ids") or {}).get("isrc", ""),
            "Album Image URL": cover, "Added At": item.get("added_at", "")}


def csv_text(rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


class SpotifyError(Exception):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def _source_path(folder, key, entry):
    name = str(entry.get("filename") or key + ".csv")
    if not re.fullmatch(re.escape(key) + r"(?:\.spotify(?:-\d+)?)?\.csv", name):
        raise SpotifyError("Invalid Spotify source filename")
    return Path(folder) / name


def _unchanged_source(path, entry):
    return (entry.get("sha256") and not path.is_symlink() and path.is_file()
            and hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"])


def _write_source(folder, key, previous, rows):
    contents = csv_text(rows)
    path = _source_path(folder, key, previous)
    # Old sync state has no digest. A same-named CSV may be an original,
    # or an edited mirror. Give Spotify a fresh home in either case.
    sequence = 0
    if _unchanged_source(path, previous):
        private_write(path, contents)
    elif not (previous and not previous.get("sha256") and preserve_legacy_copy(path, contents.encode())):
        while True:
            if not path.is_symlink() and not path.exists():
                try:
                    private_write(path, contents, exclusive=True)
                    break
                except FileExistsError:
                    pass
            sequence += 1
            suffix = ".spotify" if sequence == 1 else f".spotify-{sequence}"
            path = folder / (key + suffix + ".csv")
    canonicalize_source(path)
    return {"filename": path.name, "sha256": hashlib.sha256(contents.encode()).hexdigest(),
            "key": derived_path(path).stem}


def playlist_sources(state):
    """Catalog keys and provider IDs from the last completed adapter sync."""
    return {entry.get("key", "spotify_" + playlist_id): (playlist_id, entry)
            for playlist_id, entry in state.get("playlists", {}).items()}


class SpotifySync:
    def __init__(self, config_dir, client_id, csv_folder, on_change, now=time.time, *, on_remove=None, database=None):
        self.config_dir = Path(config_dir)
        self.database = database or self.config_dir / "requests.sqlite3"
        self.account_lock = threading.RLock()
        self.auth_path = self.config_dir / "spotify-auth.json"
        self.state_path = self.config_dir / "spotify-sync.json"
        self.client_id = client_id
        self.csv_folder = csv_folder
        self.on_change = on_change
        self.on_remove = on_remove
        self.now = now
        self.lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self.pending = None
        self.running = False
        self.stage = ""
        self.generation = 0
        self.auth = read_json(self.auth_path)
        self.state = read_json(self.state_path)

    def status(self):
        with self.lock:
            return {"configured": bool(self.client_id()), "connected": bool(self.auth.get("refresh_token")),
                    "client_id": self.client_id(), "account": self.auth.get("display_name", ""),
                    "last_success": self.state.get("last_success"), "last_error": self.state.get("last_error", ""),
                    "last_attempt": self.state.get("last_attempt"), "next_sync_at": self.state.get("next_sync_at"),
                    "saved_total": self.state.get("saved_total", 0),
                    "playlist_count": len(self.state.get("playlists", {})),
                    "running": self.running, "stage": self.stage, "interval_seconds": SYNC_INTERVAL}

    def playlist_names(self):
        with self.lock:
            liked = self.state.get("liked_source", {}).get("key", "spotify_liked")
            return {liked: "Spotify · Liked songs",
                    **{key: "Spotify · " + entry.get("name", playlist_id)
                       for key, (playlist_id, entry) in playlist_sources(self.state).items()}}

    def playlist_keys(self):
        """The catalog keys this adapter currently owns (plan 046).

        The mirrored playlists recorded in the sync state, plus the
        liked list once a sync has completed (last_success set).
        Before the first successful sync the adapter owns nothing,
        whatever files sit in the folder. Read under the same lock
        status() reads; playlist_names() is a naming map and always
        names the liked list, so it is not an ownership record."""
        with self.lock:
            keys = set(playlist_sources(self.state))
            if self.state.get("last_success") is not None:
                keys.add(self.state.get("liked_source", {}).get("key", "spotify_liked"))
            return keys

    def management(self):
        """Account-scoped controls for the host's Connections panel."""
        with self.lock:
            account = self.state.get("account_id", "")
            entries = dict(self.state.get("playlists", {}))
            connected = bool(self.auth.get("refresh_token") and account and self.auth.get("user_id") == account)
            scopes = set(self.auth.get("scope", "").split())
            has_liked = self.state.get("last_success") is not None
            liked_key = self.state.get("liked_source", {}).get("key", "spotify_liked")
            running = self.running
        choices = {key for key, (_, linked) in playlists_api.mirrors(self.database, account).items() if not linked} if account else set()
        lists = [{"id": key, "key": entry.get("key", "spotify_" + key),
                  "name": entry.get("name") or "Untitled playlist", "linked": key not in choices,
                  "owned": entry.get("owned", False), "available": entry.get("available", True),
                  "collaborative": entry.get("collaborative", False), "public": entry.get("public")}
                 for key, entry in entries.items()]
        lists.sort(key=lambda entry: (entry["name"].casefold(), entry["id"]))
        if has_liked:
            lists.insert(0, {"id": playlists_api.LIKED, "key": liked_key, "name": "Liked Songs", "linked": playlists_api.LIKED not in choices,
                             "owned": True, "available": True, "public": False, "collaborative": False})
        for playlist in lists:
            playlist["mode"] = playlists_api.link_mode(playlist, connected, scopes)
        return {"account_id": account, "connected": connected, "running": running, "playlists": lists,
                "can_create": connected and "playlist-modify-private" in scopes,
                "write_playlists": playlists_api.WRITE_PLAYLIST.issubset(scopes),
                "write_likes": playlists_api.WRITE_LIBRARY in scopes}

    def playlist_links(self):
        """Bind the catalog's proven mirror keys to the account and its current editing grant."""
        account = self.management()
        return {row["key"]: {"playlist_id": row["id"], "account_id": account["account_id"], "mode": row["mode"]}
                for row in account["playlists"]}

    def add_metadata(self, local_key, expected_account, body):
        """Append one recording using the specific account and playlist the listener saw."""
        link = self.playlist_links().get(local_key)
        if not link or link["account_id"] != expected_account or link["playlist_id"] != body.get("playlist_id"):
            raise ValueError("The Spotify link has changed. Reopen this playlist before adding a song.")
        if link["mode"] != "two_way":
            raise ValueError({"disconnected": "Reconnect Spotify in Settings to add a song.",
                              "paused": "Resume the Spotify playlist link to add a song.",
                              "read_only": "This Spotify playlist has read-only access. Check its editing permissions."
                              }[link["mode"]])
        fields = {key: body[key] for key in ("artist", "title", "album", "track") if key in body}
        return self.manage({"op": "add", "account_id": expected_account, "playlist": link["playlist_id"], **fields},
                           source_key=local_key)

    def playlist_tracks(self, account, playlist, query, offset):
        with self.lock:
            if not account or account != self.state.get("account_id"):
                raise SpotifyError("Spotify account changed. Refresh the playlist list.")
            if playlist == playlists_api.LIKED:
                key, entry = "spotify_liked", self.state.get("liked_source", {})
            elif playlist in self.state.get("playlists", {}) and playlists_api.PLAYLIST_ID.fullmatch(playlist):
                key, entry = "spotify_" + playlist, self.state["playlists"][playlist]
            else:
                raise ValueError("Unknown Spotify playlist")
            path = _source_path(self.csv_folder(), key, entry)
        if offset < 0:
            raise ValueError("Invalid page")
        if not _unchanged_source(path, entry):
            raise SpotifyError("Sync the playlist to view its current local copy")
        result = []
        count = 0
        with path.open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                title, artist = row["Track Name"], row["Artist Name(s)"]
                if query.casefold() not in (title + " " + artist).casefold():
                    continue
                if offset <= count < offset + 40:
                    result.append({"title": title, "artist": artist, "uri": row["Track URI"]})
                count += 1
        return {"tracks": result, "total": count}

    def manage(self, body, *, source_key=None):
        if not isinstance(body, dict):
            raise ValueError("Expected a playlist action")
        op, playlist, account = (body.get(field) for field in ("op", "playlist", "account_id"))
        if not isinstance(playlist, str) or (op != "create" and playlist != playlists_api.LIKED
                                             and not playlists_api.PLAYLIST_ID.fullmatch(playlist)):
            raise ValueError("Choose a Spotify playlist")
        if not self.account_lock.acquire(blocking=False):
            raise SpotifyError("Another Spotify operation is running. Try again when it finishes.")
        try:
            with self.lock:
                if self.running:
                    raise SpotifyError("A Spotify sync is running. Try again when it finishes.")
                if not account or account != self.auth.get("user_id") or account != self.state.get("account_id"):
                    raise SpotifyError("Connect Spotify and sync this account before editing")
                if op != "create" and playlist != playlists_api.LIKED and playlist not in self.state.get("playlists", {}):
                    raise ValueError("Unknown Spotify playlist")
                if source_key is not None:
                    entry = (self.state.get("liked_source", {}) if playlist == playlists_api.LIKED
                             else self.state["playlists"][playlist])
                    if entry.get("key", "spotify_" + playlist) != source_key:
                        raise ValueError("The Spotify link has changed. Reopen this playlist before adding a song.")
                scopes = set(self.auth.get("scope", "").split())
                generation = self.generation
                self.running = True
                self.stage = "Updating Spotify playlist"
            try:
                if op == "unlink":
                    source = (self.state.get("liked_source", {}) if playlist == playlists_api.LIKED
                              else self.state["playlists"][playlist])
                    playlists_api.freeze(self.database, account, playlist, source)
                    return {"message": "Playlist unlinked. Its local copy is kept."}
                if op != "link":
                    if op != "create" and not playlists_api.mirrors(self.database, account).get(playlist, ({}, True))[1]:
                        raise ValueError("Link this playlist before editing it on Spotify")
                    if self._api("/me").get("id") != account:
                        raise SpotifyError("Spotify account changed. Sync before editing.")
                    playlist = playlists_api.edit(self, account, playlist, op, body, scopes)
                with self.lock:
                    self.state.update(refresh_playlist=playlist, next_sync_at=0)
                    private_write(self.state_path, json.dumps(self.state))
                try:
                    self._sync(generation, relink=playlist if op == "link" else None, expected_account=account)
                except (SpotifyError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                    self._sync_failed(error, generation)
                    if op == "link":
                        raise SpotifyError("Could not refresh this playlist: " + str(error)) from error
                    return {"playlist": playlist, "message": "Change saved. Listening is waiting to refresh: " + str(error)}
                return {"playlist": playlist, "message": "Playlist synced."}
            finally:
                with self.lock:
                    self.running = False
                    self.stage = ""
        finally:
            self.account_lock.release()

    def paused_keys(self):
        with self.lock:
            account = self.state.get("account_id")
        if not account:
            return set()
        return {source.get("key", "spotify_" + key)
                for key, (source, linked) in playlists_api.mirrors(self.database, account).items() if not linked}

    def begin(self, redirect_uri):
        client_id = self.client_id().strip()
        if not re.fullmatch(r"[A-Fa-f0-9]{32}", client_id):
            raise SpotifyError("Enter the 32-character Spotify Client ID from your developer app")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)
        with self.lock:
            self.pending = {"state": state, "verifier": verifier, "redirect_uri": redirect_uri,
                            "created_at": self.now()}
        query = urllib.parse.urlencode({"response_type": "code", "client_id": client_id,
                                        "scope": SCOPES, "redirect_uri": redirect_uri, "state": state,
                                        "code_challenge_method": "S256", "code_challenge": challenge})
        return "https://accounts.spotify.com/authorize?" + query

    def complete(self, query):
        state = query.get("state", [""])[0]
        with self.lock:
            pending = self.pending
            self.pending = None
        if not pending or self.now() - pending["created_at"] > 600 or not hmac.compare_digest(state, pending["state"]):
            raise SpotifyError("Spotify connection expired. Start again in Settings")
        if query.get("error"):
            raise SpotifyError("Spotify access was not granted")
        code = query.get("code", [""])[0]
        if not code:
            raise SpotifyError("Spotify did not return an authorization code")
        token = self._token_request({"grant_type": "authorization_code", "code": code,
                                     "redirect_uri": pending["redirect_uri"],
                                     "client_id": self.client_id(), "code_verifier": pending["verifier"]})
        if not token.get("refresh_token") or not token.get("access_token"):
            raise SpotifyError("Spotify did not provide a refresh token")
        with self.account_lock, self.lock:
            self.generation += 1
            self.running = False
            self.stage = ""
            self.auth = {"access_token": token["access_token"], "refresh_token": token["refresh_token"],
                         "expires_at": self.now() + int(token.get("expires_in", 3600)),
                         "scope": token.get("scope", "")}
            private_write(self.auth_path, json.dumps(self.auth))
            self.state.update(last_error="", next_sync_at=0)
            private_write(self.state_path, json.dumps(self.state))
        self.start(force=True)

    def disconnect(self):
        with self.account_lock, self.lock:
            self.generation += 1
            self.running = False
            self.stage = ""
            self.pending = None
            self.auth = {}
            self.auth_path.unlink(missing_ok=True)

    def record_error(self, message):
        with self.lock:
            self.state["last_error"] = str(message)
            private_write(self.state_path, json.dumps(self.state))

    def start(self, force=False):
        with self.lock:
            if not self.auth.get("refresh_token"):
                raise SpotifyError("Connect Spotify before syncing")
            if self.running:
                return False
            if not force and self.now() < self.state.get("next_sync_at", 0):
                return False
            self.running = True
            self.stage = "Connecting to Spotify"
            generation = self.generation
        threading.Thread(target=self._run, args=(generation,), daemon=True, name="spotify-sync").start()
        return True

    def _run(self, generation):
        try:
            self._sync(generation)
        except (SpotifyError, OSError, ValueError, KeyError, TypeError, RuntimeError,
                subprocess.TimeoutExpired) as error:
            self._sync_failed(error, generation)
        finally:
            with self.lock:
                if generation == self.generation:
                    self.running = False
                    self.stage = ""

    def _sync_failed(self, error, generation):
        with self.lock:
            if generation == self.generation:
                delay = max(SYNC_INTERVAL, getattr(error, "retry_after", 0))
                self.state.update(last_error=str(error), last_attempt=self.now(), next_sync_at=self.now() + delay)
                private_write(self.state_path, json.dumps(self.state))

    def _sync(self, generation, *, relink=None, expected_account=None):
        folder = Path(self.csv_folder())
        folder.mkdir(parents=True, exist_ok=True)
        with self.lock:
            old = dict(self.state)
        self._stage("Checking account")
        profile = self._api("/me")
        if not profile.get("id"):
            raise SpotifyError("Spotify did not return an account ID")
        if expected_account is not None and profile["id"] != expected_account:
            raise SpotifyError("Spotify account changed. Sync before editing.")
        registry = playlists_api.mirrors(self.database, profile["id"])
        paused = {key for key, (_, linked) in registry.items() if not linked}
        if relink:
            paused.discard(relink)
        account_changed = bool(old.get("account_id") and old["account_id"] != profile["id"])
        with self.lock:
            if generation != self.generation:
                return
            self.auth["user_id"] = profile["id"]
            self.auth["display_name"] = profile.get("display_name") or profile["id"]
            private_write(self.auth_path, json.dumps(self.auth))

        self._stage("Checking liked songs")
        local_state = {} if account_changed else old
        legacy_liked = {"key": "spotify_liked"} if local_state.get("last_success") is not None else {}
        liked = registry.get(playlists_api.LIKED, (local_state.get("liked_source", legacy_liked), True))[0]
        saved_total = liked.get("total", local_state.get("saved_total", 0))
        head = liked.get("head", local_state.get("saved_head", ""))
        full_at = liked.get("full_at", local_state.get("saved_full_at", 0))
        if playlists_api.LIKED not in paused:
            first = self._api("/me/tracks", {"limit": 50, "offset": 0})
            saved_total = int(first.get("total", 0))
            head = self._head(first.get("items", []))
        saved_path = _source_path(folder, "spotify_liked", liked)
        saved_changed = (account_changed or not _unchanged_source(saved_path, liked) or old.get("saved_total") != saved_total
                         or old.get("saved_head") != head
                         or self.now() - full_at >= FULL_SAVED_INTERVAL
                         or old.get("refresh_playlist") == playlists_api.LIKED)
        saved_rows = None
        if saved_changed and playlists_api.LIKED not in paused:
            self._stage(f"Reading {saved_total} liked songs")
            items = self._pages("/me/tracks", first=first)
            saved_rows = [row for item in items if (row := track_row(item))]

        self._stage("Checking playlists")
        playlists = self._pages("/me/playlists")
        previous = {**local_state.get("playlists", {}),
                    **{key: entry for key, (entry, _) in registry.items() if key != playlists_api.LIKED}}
        unexpected_empty = not playlists and bool(previous.keys() - paused)
        next_playlists = {key: {**previous[key], "available": False} for key in previous.keys() & paused}
        if unexpected_empty:
            next_playlists = dict(previous)
        changed = []
        for playlist in playlists:
            if not isinstance(playlist, dict):
                continue
            playlist_id = playlist.get("id", "")
            if not re.fullmatch(r"[A-Za-z0-9]{22}", playlist_id):
                continue
            owned = (playlist.get("owner") or {}).get("id") == profile["id"]
            collaborative = bool(playlist.get("collaborative"))
            if not owned and not collaborative:
                continue
            prior = previous.get(playlist_id, {})
            permissions = {"owned": owned, "available": True, "collaborative": collaborative, "public": playlist.get("public")}
            if playlist_id in paused:
                next_playlists[playlist_id] = {**prior, "name": prior.get("name") or playlist.get("name"),
                                               **permissions}
                continue
            path = _source_path(folder, "spotify_" + playlist_id, prior)
            entry = {**prior, **permissions, "name": playlist.get("name") or "Untitled playlist",
                     "snapshot": playlist.get("snapshot_id", "")}
            next_playlists[playlist_id] = entry
            if (account_changed or old.get("refresh_playlist") == playlist_id or previous.get(playlist_id, {}).get("snapshot") != entry["snapshot"]
                    or not _unchanged_source(path, prior)):
                self._stage(f"Reading {entry['name'][:60]}")
                items = self._pages(f"/playlists/{playlist_id}/items")
                changed.append((playlist_id, [row for item in items if (row := track_row(item))]))
            else:
                entry["key"] = derived_path(path).stem

        if relink and relink != playlists_api.LIKED and (unexpected_empty or relink not in next_playlists):
            raise SpotifyError("The playlist is no longer available on this account. The local copy is kept.")
        with self.lock:
            if generation != self.generation:
                return
            if saved_rows is not None:
                liked = _write_source(folder, "spotify_liked", liked, saved_rows)
            else:
                liked = {**liked, "key": derived_path(saved_path).stem}
            for playlist_id, rows in changed:
                next_playlists[playlist_id].update(_write_source(
                    folder, "spotify_" + playlist_id, previous.get(playlist_id, {}), rows))
            # Remove only CSVs created by a prior Spotify sync for this connection.
            for playlist_id in previous.keys() - next_playlists.keys() - paused:
                if not re.fullmatch(r"[A-Za-z0-9]{22}", playlist_id):
                    continue
                prior = previous[playlist_id]
                path = _source_path(folder, "spotify_" + playlist_id, prior)
                unchanged = _unchanged_source(path, prior)
                missing = not path.exists() and not path.is_symlink()
                if unchanged or missing:
                    if self.on_remove is not None and prior.get("sha256"):
                        self.on_remove(path, prior["sha256"])
                    retire_derived(path)
                    if unchanged:
                        path.unlink()
            now = self.now()
            publish = bool(saved_rows is not None or changed or previous != next_playlists or old.get("publish_pending"))
            full_at = now if saved_rows is not None else full_at
            liked.update(total=saved_total, head=head, full_at=full_at)
            playlists_api.remember(self.database, profile["id"], {**next_playlists, playlists_api.LIKED: liked}, paused)
            self.state = {"publish_pending": publish, "last_success": now, "last_attempt": now, "last_error": "",
                          "next_sync_at": now + SYNC_INTERVAL, "saved_total": saved_total,
                          "saved_head": head, "liked_source": liked, "saved_full_at": full_at,
                          "account_id": profile["id"], "playlists": next_playlists}
            private_write(self.state_path, json.dumps(self.state, ensure_ascii=False))
        if publish:
            self._stage("Updating local library")
            self.on_change()
            with self.lock:
                if generation == self.generation:
                    self.state["publish_pending"] = False
                    private_write(self.state_path, json.dumps(self.state, ensure_ascii=False))

        if unexpected_empty and relink != playlists_api.LIKED:
            raise SpotifyError("Spotify returned an empty playlist page; kept the previous linked playlists")

    def _stage(self, stage):
        with self.lock:
            self.stage = stage

    @staticmethod
    def _head(items):
        if not items:
            return ""
        first = items[0] or {}
        track = first.get("track") or first.get("item") or {}
        return f"{first.get('added_at', '')}:{track.get('uri', '')}"

    def _pages(self, path, first=None):
        result = []
        page = first
        offset = 0
        for _ in range(2000):
            if page is None:
                page = self._api(path, {"limit": 50, "offset": offset})
            items = page.get("items") or []
            result.extend(items)
            offset += len(items)
            total = int(page.get("total", offset))
            if not items and offset < total:
                raise SpotifyError("Spotify returned an incomplete playlist page; kept the previous linked playlists")
            if not items or offset >= total:
                return result
            page = None
        raise SpotifyError("Spotify library is too large to sync in one pass")

    def _token_request(self, fields):
        body = urllib.parse.urlencode(fields).encode()
        request = urllib.request.Request("https://accounts.spotify.com/api/token", data=body,
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        return self._request_json(request)

    def _refresh(self):
        with self.lock:
            refresh_token = self.auth.get("refresh_token")
            generation = self.generation
        if not refresh_token:
            raise SpotifyError("Spotify connection expired. Reconnect in Settings")
        token = self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token,
                                     "client_id": self.client_id()})
        with self.lock:
            if generation != self.generation:
                raise SpotifyError("Spotify connection changed")
            self.auth.update(access_token=token["access_token"],
                             refresh_token=token.get("refresh_token", refresh_token),
                             expires_at=self.now() + int(token.get("expires_in", 3600)))
            if isinstance(token.get("scope"), str):
                self.auth["scope"] = token["scope"]
            private_write(self.auth_path, json.dumps(self.auth))

    def _refresh_if_expired(self):
        """Refresh the access token when it has expired, without
        holding `self.lock` across the network call (plan 038):
        status() and playlist_names() answer request threads under
        that lock (playlist_names on every /api/overview), and a token
        request may take up to its 25-second timeout. The dedicated
        refresh lock keeps two threads from refreshing at once: the
        second re-checks expiry after the first finishes and finds
        the token fresh."""
        with self.lock:
            expired = self.now() >= self.auth.get("expires_at", 0) - 60
        if not expired:
            return
        with self._refresh_lock:
            with self.lock:
                expired = self.now() >= self.auth.get("expires_at", 0) - 60
            if expired:
                self._refresh()

    def _api(self, path, params=None, *, method="GET", body=None):
        self._refresh_if_expired()
        with self.lock:
            access_token = self.auth["access_token"]
        url = "https://api.spotify.com/v1" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": "Bearer " + access_token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            return self._request_json(request)
        except SpotifyError as error:
            if str(error).startswith("Spotify returned 401"):
                with self._refresh_lock:
                    self._refresh()
                with self.lock:
                    access_token = self.auth["access_token"]
                request.add_header("Authorization", "Bearer " + access_token)
                return self._request_json(request)
            raise

    @staticmethod
    def _request_json(request):
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                payload = response.read()
                return json.loads(payload) if payload.strip() else {}
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read(2048))
                detail = payload.get("error", {})
                reason = detail.get("reason", "") if isinstance(detail, dict) else ""
            except (ValueError, UnicodeDecodeError):
                reason = ""
            if error.code == 429:
                retry_after = int(error.headers.get("Retry-After", "900")) if error.headers else 900
                if reason == "QUOTA_EXCEEDED":
                    retry_after = max(retry_after, 3600)
                raise SpotifyError("Spotify quota reached; sync will retry later", retry_after) from error
            if error.code == 403:
                raise SpotifyError("Spotify denied API access. Check the app's allowlist and Premium status") from error
            raise SpotifyError(f"Spotify returned {error.code}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise SpotifyError("Could not reach Spotify") from error
