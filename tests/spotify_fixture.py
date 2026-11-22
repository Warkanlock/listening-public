"""In-memory Spotify transport used by the two-way playlist tests."""
import json
from urllib.parse import urlsplit, parse_qs


class SpotifyFixture:
    playlist = "A" * 22
    created = "B" * 22

    def __init__(self):
        self.owner = "fixture-owner"
        self.names = {self.playlist: "Evening"}
        self.members = {self.playlist: [self.uri(1)]}
        self.saved = [self.uri(2)]
        self.version = 1
        self.writes = []

    @staticmethod
    def uri(n):
        return "spotify:track:" + str(n).zfill(22)

    @staticmethod
    def item(uri):
        return {"item": {"uri": uri, "name": "Song " + str(int(uri[-22:])), "type": "track",
                         "artists": [{"name": "Fixture Artist"}], "album": {"name": "Fixture Album"}}}

    def details(self, key):
        return {"id": key, "name": self.names[key], "owner": {"id": self.owner},
                "snapshot_id": str(self.version), "collaborative": False}

    def request(self, req):
        assert req.get_header("Authorization") == "Bearer fixture-token"
        url = urlsplit(req.full_url)
        assert url.hostname == "api.spotify.com"
        path = url.path.removeprefix("/v1")
        params = parse_qs(url.query)
        method = req.get_method()
        body = json.loads(req.data) if req.data else {}
        key = path.split("/")[2] if path.startswith("/playlists/") else ""
        if method == "GET":
            if path == "/me":
                return {"id": self.owner}
            if path == "/me/playlists":
                items = [self.details(p) for p in self.names]
            elif path == "/me/tracks":
                items = [{"track": self.item(uri)["item"]} for uri in self.saved]
            elif path == f"/playlists/{key}/items":
                items = [self.item(uri) for uri in self.members[key]]
            elif key and path == f"/playlists/{key}":
                return self.details(key)
            else:
                raise AssertionError(path)
            offset = int(params.get("offset", [0])[0])
            return {"total": len(items), "items": items[offset:offset + int(params.get("limit", [50])[0])]}
        self.writes.append((method, path, body))
        self.version += 1
        if method == "POST" and path == "/me/playlists":
            assert body["public"] is False
            self.names[self.created] = body["name"]
            self.members[self.created] = []
            return {"id": self.created}
        if path == "/me/library" and method in ("PUT", "DELETE"):
            uri, = params["uris"]
            if method == "PUT" and uri not in self.saved:
                self.saved.insert(0, uri)
            if method == "DELETE" and uri in self.saved:
                self.saved.remove(uri)
            return {}
        if method == "PUT" and path == f"/playlists/{key}":
            self.names[key] = body["name"]
            return {}
        if path == f"/playlists/{key}/items":
            if method == "POST":
                self.members[key].extend(body["uris"])
            elif method == "DELETE":
                assert body["snapshot_id"] == str(self.version - 1)
                self.members[key] = [u for u in self.members[key] if u != body["items"][0]["uri"]]
            else:
                raise AssertionError(method)
            return {"snapshot_id": str(self.version)}
        raise AssertionError((method, path))
