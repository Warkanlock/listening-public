"""Playlist choices for a Spotify account and explicit, incremental provider edits."""

from contextlib import closing
import re
import json
import unicodedata

from system import state_database

LIKED = "liked"
PLAYLIST_ID = re.compile(r"[A-Za-z0-9]{22}")
WRITE_LIBRARY = "user-library-modify"
WRITE_PLAYLIST = {"playlist-modify-private", "playlist-modify-public"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS spotify_mirrors (
    account TEXT NOT NULL, playlist TEXT NOT NULL, source TEXT NOT NULL, linked INTEGER NOT NULL, PRIMARY KEY(account, playlist)
);
"""


def mirrors(database, account):
    """The last proven local source for each provider ID, including frozen copies."""
    with closing(state_database.connect(database, SCHEMA)) as db:
        return {key: (json.loads(source), bool(linked)) for key, source, linked in db.execute(
            "SELECT playlist,source,linked FROM spotify_mirrors WHERE account=?", (account,))}


def freeze(database, account, playlist, source):
    with closing(state_database.connect(database, SCHEMA)) as db, db:
        db.execute("INSERT OR REPLACE INTO spotify_mirrors VALUES (?,?,?,0)",
                   (account, playlist, json.dumps(source)))


def remember(database, account, sources, paused):
    with closing(state_database.connect(database, SCHEMA)) as db, db:
        db.execute("DELETE FROM spotify_mirrors WHERE account=?", (account,))
        db.executemany("INSERT INTO spotify_mirrors VALUES (?,?,?,?)",
                       [(account, key, json.dumps(entry), key not in paused) for key, entry in sources.items()])


def track_uri(value):
    if isinstance(value, str):
        match = re.fullmatch(r"(?:spotify:track:|https://open\.spotify\.com/(?:intl-[a-z]+/)?track/)"
                             r"([A-Za-z0-9]{22})(?:\?[^\s#]*)?", value.strip())
        if match:
            return "spotify:track:" + match[1]
    raise ValueError("Choose a catalog song with a Spotify ID or paste a Spotify track link")


def _name(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split()) if isinstance(value, str) else ""


def _track_to_add(sync, body):
    if body.get("track"):
        return track_uri(body["track"])
    title, artist, album = (body.get(key, "") for key in ("title", "artist", "album"))
    if not all(isinstance(value, str) and len(value) <= 250 for value in (title, artist, album)):
        raise ValueError("Enter a song title and artist of up to 250 characters")
    if not title.strip() or not artist.strip():
        raise ValueError("Enter a song title and artist or paste a Spotify track link")
    query = f"track:{title.strip()} artist:{artist.strip()}"
    if album.strip():
        query += f" album:{album.strip()}"
    title, artist, album = map(_name, (title, artist, album))
    matches = set()
    for offset in (0, 10, 20):
        page = sync._api("/search", {"type": "track", "limit": 10, "q": query, "offset": offset}).get("tracks", {})
        for track in page.get("items") or []:
            if (not isinstance(track, dict) or track.get("is_local") or track.get("type") != "track"
                    or _name(track.get("name")) != title):
                continue
            artists = [_name(item.get("name")) for item in track.get("artists") or [] if isinstance(item, dict)]
            if artist not in artists and artist != ", ".join(artists):
                continue
            if album and album != _name((track.get("album") or {}).get("name", "")):
                continue
            uri = track.get("uri") or ""
            if re.fullmatch(r"spotify:track:[A-Za-z0-9]{22}", uri):
                matches.add(uri)
        if len(matches) > 1:
            raise ValueError("Spotify found several recordings. Add the album name or paste the exact Spotify track link.")
        if not page.get("next"):
            if matches:
                return matches.pop()
            raise ValueError("Spotify found no exact artist and title match. Check the names or paste a Spotify track link.")
    raise ValueError("Narrow the Spotify search with an album name or paste the exact Spotify track link.")


def write_scopes(playlist, public):
    if playlist == LIKED:
        return {WRITE_LIBRARY}
    if isinstance(public, bool):
        return {"playlist-modify-public" if public else "playlist-modify-private"}
    return WRITE_PLAYLIST


def link_mode(playlist, connected, scopes):
    if not playlist["linked"]:
        return "paused"
    if not connected or not playlist["available"]:
        return "disconnected"
    if ((playlist["owned"] or playlist["collaborative"]) and
            write_scopes(playlist["id"], playlist["public"]).issubset(scopes)):
        return "two_way"
    return "read_only"


def edit(sync, account, playlist, op, body, scopes):
    """Apply an owner command to the provider. Never replace a playlist's full contents."""
    if op not in {"create", "rename", "add", "remove"}:
        raise ValueError("Unknown Spotify playlist edit")
    if playlist == LIKED and op in {"create", "rename"}:
        raise ValueError("Liked Songs cannot be renamed or created")
    if op in {"create", "rename"}:
        name = body.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError("Choose a playlist name of 1 to 100 characters")
        name = name.strip()
    if op == "create":
        if not write_scopes(playlist, False).issubset(scopes):
            raise ValueError("Reconnect Spotify to grant editing access")
        owned = sync._pages("/me/playlists")
        if any((p.get("owner") or {}).get("id") == account
               and (p.get("name") or "").strip().casefold() == name.casefold() for p in owned):
            raise ValueError("You already own a Spotify playlist with that name. Select it to edit.")
        result = sync._api("/me/playlists", method="POST", body={"name": name, "public": False})
        return result["id"]
    if playlist == LIKED:
        if WRITE_LIBRARY not in scopes:
            raise ValueError("Reconnect Spotify to grant editing access")
        uri = _track_to_add(sync, body) if op == "add" else track_uri(body.get("track"))
        sync._api("/me/library", {"uris": uri}, method="PUT" if op == "add" else "DELETE")
        return playlist
    remote = sync._api(f"/playlists/{playlist}")
    if not write_scopes(playlist, remote.get("public")).issubset(scopes):
        raise ValueError("Reconnect Spotify to grant editing access")
    owner = (remote.get("owner") or {}).get("id") == account
    if not owner and (not remote.get("collaborative") or op == "rename"):
        raise ValueError("Spotify requires an owned playlist for this edit")
    if op == "rename":
        if remote.get("name") != name:
            sync._api(f"/playlists/{playlist}", method="PUT", body={"name": name})
        return playlist
    uri = _track_to_add(sync, body) if op == "add" else track_uri(body.get("track"))
    path = f"/playlists/{playlist}/items"
    items = sync._pages(path)
    present = any((row.get("item") or row.get("track") or {}).get("uri") == uri for row in items)
    if op == "add" and not present:
        sync._api(path, method="POST", body={"uris": [uri]})
    if op == "remove" and present:
        change = {"items": [{"uri": uri}]}
        if remote.get("snapshot_id"):
            change["snapshot_id"] = remote["snapshot_id"]
        sync._api(path, method="DELETE", body=change)
    return playlist
