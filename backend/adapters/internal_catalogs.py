"""Read music metadata from operator-configured services."""

from dataclasses import dataclass
import json
import os
import re
from urllib.parse import urlencode

from adapters.service_http import base_url, read_json
from metadata.external_metadata import search_cache_key

PROVIDERS = (
    ("jellyfin", "Jellyfin", ("music",)),
)
ITEM_ID = re.compile(r"[a-zA-Z0-9_-]{1,100}")


@dataclass(frozen=True, repr=False)
class Connection:
    endpoint: str
    web: str
    token: str


def connection(provider):
    prefix = f"LISTENING_{provider.upper()}"
    endpoint = os.environ.get(prefix + "_URL", "")
    token = os.environ.get(prefix + "_TOKEN", "").strip()
    if not endpoint or not token or not token.isascii() or not token.isprintable():
        return None
    try:
        return Connection(base_url(endpoint), base_url(os.environ.get(prefix + "_WEB_URL") or endpoint), token)
    except ValueError:
        return None


def _rows(value):
    if not isinstance(value, list):
        raise ValueError("Unexpected internal catalog response.")
    return (row for row in value if isinstance(row, dict))


def _jellyfin(service, user, query, page, count):
    args = {"Recursive": "true", "IncludeItemTypes": "Audio,MusicAlbum", "Limit": count,
            "StartIndex": page * count, "Fields": "Artists,Album,ProductionYear",
            "SortBy": "SortName"}
    if query:
        args["SearchTerm"] = query
    if user:
        args["UserId"] = user
    headers = {"Authorization": f"MediaBrowser Token={json.dumps(service.token)}"}
    data = read_json(f"{service.endpoint}/Items?{urlencode(args)}", headers)
    found = []
    for row in _rows(data.get("Items")):
        identity = row.get("Id")
        if not isinstance(identity, str) or not ITEM_ID.fullmatch(identity):
            continue
        artists = row.get("Artists")
        creator = ", ".join(artist for artist in artists if isinstance(artist, str)) if isinstance(artists, list) else ""
        detail = row.get("Album")
        address = {"id": identity}
        server = row.get("ServerId")
        if isinstance(server, str) and ITEM_ID.fullmatch(server):
            address["serverId"] = server
        found.append((row.get("Name"), creator, service.web + "/web/index.html#/details?" + urlencode(address),
                      detail, row.get("Type") == "Audio"))
    return found, data.get("TotalRecordCount")


def search(provider, query, page, count, *, cached=None):
    service = connection(provider)
    if service is None:
        raise ValueError("The room catalog is not configured.")
    user = os.environ.get("LISTENING_JELLYFIN_USER_ID", "")

    def fetch():
        return _jellyfin(service, user, query, page, count)

    if not cached:
        return fetch()
    key = search_cache_key(provider, service.endpoint, service.web, user, service.token, query, page, count)
    return cached(key, 300, fetch)
